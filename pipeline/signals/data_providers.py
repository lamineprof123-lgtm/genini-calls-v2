"""Pluggable OHLCV providers. Every provider returns a DataFrame indexed by UTC bar-open time
with columns open/high/low/close/volume, containing CLOSED candles only.

When chart.template == chart_v2.html, fetch_symbol uses an in-process bar cache and
incremental refreshes so repeat cycles (5-min loop on 15-min bars) skip the network."""
from __future__ import annotations
import asyncio, glob, json, logging, os, shutil, tempfile, time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import ROOT

log = logging.getLogger(__name__)
COLS = ["open", "high", "low", "close", "volume"]

# Shared keep-alive HTTP client (Binance). Created lazily.
_httpx_client = None
_httpx_lock = asyncio.Lock()

# symbol -> {df, source, ticker, fetched_at, last_bar}
_BAR_CACHE: dict[str, dict] = {}
_cache_timings: dict[str, float] = {}


def closed_only(df: pd.DataFrame, tf_min: int, now: datetime | None = None) -> pd.DataFrame:
    now = now or datetime.now(timezone.utc)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[df.index + pd.Timedelta(minutes=tf_min) <= pd.Timestamp(now)]


def expected_last_closed_open(tf_min: int, now: datetime | None = None) -> pd.Timestamp:
    """UTC open time of the most recently CLOSED tf_min bar."""
    now = now or datetime.now(timezone.utc)
    step = tf_min * 60
    ts = int(now.timestamp())
    open_ts = (ts // step) * step - step
    return pd.Timestamp(open_ts, unit="s", tz="UTC")


def _fast_enabled(cfg: dict) -> bool:
    return (cfg.get("chart") or {}).get("template") == "chart_v2.html"


async def _get_httpx():
    global _httpx_client
    import httpx
    async with _httpx_lock:
        if _httpx_client is None or _httpx_client.is_closed:
            _httpx_client = httpx.AsyncClient(
                timeout=20,
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                headers={"User-Agent": "gemini-signals/1.0"},
            )
        return _httpx_client


class DataProvider(ABC):
    name = "base"

    @abstractmethod
    async def fetch(self, ticker: str, tf_min: int, n: int, *, since: pd.Timestamp | None = None) -> pd.DataFrame: ...


class DukascopyProvider(DataProvider):
    """Dukascopy Bank public historical feed via the dukascopy-node CLI (installed in ./node_modules).
    Real spot FX / XAUUSD / index CFD prices with tick volume. No account needed."""
    name = "dukascopy"

    def __init__(self, bin_path: str | None = None, timeout: float = 60):
        local = ROOT / "node_modules/.bin/dukascopy-node"
        self.cmd = [str(local)] if local.exists() else ["npx", "-y", "dukascopy-node"]
        if bin_path:
            self.cmd = [bin_path]
        self.timeout = timeout

    async def fetch(self, ticker, tf_min, n, *, since: pd.Timestamp | None = None):
        if tf_min not in (1, 5, 15, 30, 60, 240, 1440):
            raise ValueError(f"unsupported timeframe {tf_min}")
        tf = {1: "m1", 5: "m5", 15: "m15", 30: "m30", 60: "h1", 240: "h4", 1440: "d1"}[tf_min]
        now = datetime.now(timezone.utc)
        if since is not None:
            # Incremental: pull a short recent window and let the caller merge.
            days = max(2, int((now - since.to_pydatetime().replace(tzinfo=timezone.utc)).total_seconds() / 86400) + 2)
            days = min(days, 6)
        else:
            days = max(6, int(n * tf_min / 1440 * 2.2) + 4)
        out = tempfile.mkdtemp(prefix="duka_")
        try:
            args = self.cmd + ["-i", ticker, "-from", (now - timedelta(days=days)).strftime("%Y-%m-%d"),
                               "-to", (now + timedelta(days=1)).strftime("%Y-%m-%d"), "-t", tf, "-f", "json",
                               "-v", "true", "-dir", out, "-r", "2"]
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            stdout, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
            files = glob.glob(f"{out}/*.json")
            if not files:
                raise RuntimeError(f"dukascopy {ticker}: no file ({stdout.decode()[-300:].strip()})")
            rows = json.load(open(files[0]))
        finally:
            shutil.rmtree(out, ignore_errors=True)
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
        return closed_only(df[COLS].astype(float), tf_min).tail(n if since is None else max(n, 64))


class BinanceProvider(DataProvider):
    """Binance public REST klines (no key). Used for BTCUSD -> BTCUSDT. Keep-alive client."""
    name = "binance"
    URL = "https://api.binance.com/api/v3/klines"

    async def fetch(self, ticker, tf_min, n, *, since: pd.Timestamp | None = None):
        iv = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h"}[tf_min]
        limit = min(1000, n + 2)
        if since is not None:
            limit = min(1000, max(8, int((datetime.now(timezone.utc) - since.to_pydatetime().replace(tzinfo=timezone.utc)).total_seconds() / (tf_min * 60)) + 4))
        client = await _get_httpx()
        r = await client.get(self.URL, params={"symbol": ticker, "interval": iv, "limit": limit})
        r.raise_for_status()
        rows = r.json()
        df = pd.DataFrame([[x[0], *x[1:6]] for x in rows], columns=["t", *COLS])
        df.index = pd.to_datetime(df.pop("t"), unit="ms", utc=True)
        return closed_only(df.astype(float), tf_min).tail(n if since is None else max(n, 64))


class YFinanceProvider(DataProvider):
    """Yahoo Finance via yfinance (free, delayed for some instruments; FX volume is 0)."""
    name = "yfinance"

    async def fetch(self, ticker, tf_min, n, *, since: pd.Timestamp | None = None):
        import yfinance as yf

        def _get():
            period = "5d" if n * tf_min <= 5 * 1440 * 0.6 else "30d"
            return yf.Ticker(ticker).history(period=period, interval=f"{tf_min}m", auto_adjust=False)
        df = await asyncio.to_thread(_get)
        if df is None or df.empty:
            raise RuntimeError(f"yfinance {ticker}: empty")
        df = df.rename(columns=str.lower)[COLS].astype(float)
        df.index = pd.to_datetime(df.index, utc=True)
        return closed_only(df, tf_min).tail(n)


class MT5Provider(DataProvider):
    """MetaTrader5 terminal (Windows only)."""
    name = "mt5"

    def _module(self):
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as e:
            raise RuntimeError("MetaTrader5 package not available on this OS (Windows only). "
                               "Use a bridge (e.g. mt5linux) or the dukascopy/yfinance providers.") from e
        if not mt5.initialize():
            raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
        return mt5

    async def fetch(self, ticker, tf_min, n, *, since: pd.Timestamp | None = None):
        def _get():
            mt5 = self._module()
            tf = {1: mt5.TIMEFRAME_M1, 5: mt5.TIMEFRAME_M5, 15: mt5.TIMEFRAME_M15, 30: mt5.TIMEFRAME_M30,
                  60: mt5.TIMEFRAME_H1}[tf_min]
            rates = mt5.copy_rates_from_pos(ticker, tf, 1, n)
            if rates is None:
                raise RuntimeError(f"mt5 {ticker}: {mt5.last_error()}")
            df = pd.DataFrame(rates)
            df.index = pd.to_datetime(df.pop("time"), unit="s", utc=True)
            return df.rename(columns={"tick_volume": "volume"})[COLS].astype(float)
        return closed_only(await asyncio.to_thread(_get), tf_min).tail(n)


class RestBrokerProvider(DataProvider):
    name = "rest_broker"

    async def fetch(self, ticker, tf_min, n, *, since: pd.Timestamp | None = None):
        raise NotImplementedError("Configure your broker REST endpoint/credentials and implement fetch().")


PROVIDERS: dict[str, type[DataProvider]] = {p.name: p for p in
    (DukascopyProvider, BinanceProvider, YFinanceProvider, MT5Provider, RestBrokerProvider)}


def _merge(old: pd.DataFrame, new: pd.DataFrame, n: int) -> pd.DataFrame:
    df = pd.concat([old, new])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.tail(n)


async def _fetch_chain(symbol: str, cfg: dict, *, since: pd.Timestamp | None = None) -> tuple[pd.DataFrame, str, str]:
    g = cfg["general"]
    scfg = cfg.get("symbols", {}).get(symbol, {})
    chain = scfg.get("chain", cfg["data"]["default_chain"])
    need = g["candles"] + g["warmup_candles"]
    errors = []
    for name in chain:
        ticker = scfg.get(name, symbol)
        try:
            df = await PROVIDERS[name]().fetch(ticker, g["timeframe_minutes"], need, since=since)
            if since is None and len(df) < g["candles"]:
                raise RuntimeError(f"only {len(df)} candles")
            return df, name, ticker
        except Exception as e:
            errors.append(f"{name}:{ticker}: {e}")
            log.warning("%s provider %s failed: %s", symbol, name, e)
    raise RuntimeError(f"{symbol}: all providers failed: {errors}")


async def fetch_symbol(symbol: str, cfg: dict) -> tuple[pd.DataFrame, str, str]:
    """Try the symbol's provider chain. Returns (df, provider_name, ticker).
    With chart_v2.html: serve from bar cache when the last closed bar is unchanged;
    otherwise incremental-fetch and merge."""
    global _cache_timings
    t0 = time.monotonic()
    g = cfg["general"]
    need = g["candles"] + g["warmup_candles"]
    tf = g["timeframe_minutes"]
    expect = expected_last_closed_open(tf)

    if _fast_enabled(cfg):
        hit = _BAR_CACHE.get(symbol)
        if hit is not None and hit["last_bar"] == expect and len(hit["df"]) >= g["candles"]:
            _cache_timings[symbol] = round((time.monotonic() - t0) * 1000, 1)
            log.debug("%s cache hit last_bar=%s (%.1fms)", symbol, expect, _cache_timings[symbol])
            return hit["df"].tail(need), hit["source"], hit["ticker"]
        since = hit["df"].index[-8] if hit is not None and len(hit["df"]) else None
        try:
            fresh, src, ticker = await _fetch_chain(symbol, cfg, since=since)
            if hit is not None and since is not None:
                df = _merge(hit["df"], fresh, need)
            else:
                df = fresh.tail(need)
            if len(df) < g["candles"]:
                # incremental came up short — full refetch
                df, src, ticker = await _fetch_chain(symbol, cfg, since=None)
                df = df.tail(need)
        except Exception:
            if hit is not None and len(hit["df"]) >= g["candles"]:
                log.warning("%s refresh failed; serving stale cache", symbol)
                _cache_timings[symbol] = round((time.monotonic() - t0) * 1000, 1)
                return hit["df"].tail(need), hit["source"], hit["ticker"]
            raise
        _BAR_CACHE[symbol] = {
            "df": df, "source": src, "ticker": ticker,
            "fetched_at": time.time(), "last_bar": df.index[-1],
        }
        _cache_timings[symbol] = round((time.monotonic() - t0) * 1000, 1)
        return df, src, ticker

    # v1 / cold path (unchanged behaviour)
    df, src, ticker = await _fetch_chain(symbol, cfg, since=None)
    _cache_timings[symbol] = round((time.monotonic() - t0) * 1000, 1)
    return df.tail(need), src, ticker


def last_fetch_timings() -> dict[str, float]:
    return dict(_cache_timings)
