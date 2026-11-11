"""Intrabar live price from Dukascopy (or Binance) M1 bars, used by Gemini context, scalp entry, and Lucid.

The M15 signal_candle stays the last CLOSED bar. live_price is the latest M1 close (including the
forming M1 when the feed returns it). On fetch failure we fall back to the last M15 close and set
live_price_stale=True.
"""
from __future__ import annotations
import asyncio, glob, json, logging, shutil, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .data_providers import BinanceProvider, COLS, DukascopyProvider, _get_httpx

log = logging.getLogger("signals.live")

# Short window is enough to build the forming M15 bar; keeps the extra fetch well inside the 2-min cycle.
_M1_LOOKBACK_MIN = 180
_M1_TIMEOUT = 20.0

LIVE_CONTEXT_KEYS = (
    "live_price", "live_price_ts_utc", "live_price_stale", "live_price_source",
    "live_price_fetched_at_utc", "forming_m15_open", "forming_m15_high", "forming_m15_low",
    "forming_m15_open_utc", "minutes_into_bar",
)


def _now_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def forming_m15_open(now: datetime, tf_min: int = 15) -> pd.Timestamp:
    now = _now_utc(now)
    step = tf_min * 60
    ts = int(now.timestamp())
    open_ts = (ts // step) * step
    return pd.Timestamp(open_ts, unit="s", tz="UTC")


def minutes_into_bar(now: datetime, tf_min: int = 15) -> float:
    now = _now_utc(now)
    opened = forming_m15_open(now, tf_min)
    return round(max(0.0, (now - opened.to_pydatetime()).total_seconds() / 60.0), 2)


def m1_ticker(symbol: str, cfg: dict, provider: str) -> str | None:
    scfg = (cfg.get("symbols") or {}).get(symbol) or {}
    return scfg.get(provider)


def live_from_m1(df: pd.DataFrame, *, tf_min: int = 15, now: datetime | None = None,
                 digits: int = 5, source: str = "dukascopy:m1") -> dict:
    """Build live_price + forming M15 OHLC from an M1 frame (may include the incomplete last minute)."""
    now = _now_utc(now)
    fetched = now.isoformat()
    if df is None or len(df) == 0:
        raise ValueError("empty M1 frame")
    d = df.copy()
    if d.index.tz is None:
        d.index = d.index.tz_localize("UTC")
    d = d[~d.index.duplicated(keep="last")].sort_index()
    d = d[d.index <= pd.Timestamp(now)]
    if d.empty:
        raise ValueError("no M1 bars at or before now")
    last = d.iloc[-1]
    last_ts = d.index[-1]
    # Timestamp of the last M1 observation: bar close if the minute has closed, else 'now'
    bar_end = last_ts.to_pydatetime() + timedelta(minutes=1)
    live_ts = min(bar_end, now)
    live = round(float(last["close"]), digits)
    opened = forming_m15_open(now, tf_min)
    forming = d[d.index >= opened]
    if forming.empty:
        # Rare: feed lag straddling the M15 boundary — use the latest M1 as a 1-bar stub
        forming = d.tail(1)
        opened = forming.index[0].floor(f"{tf_min}min")
    fo, fh, fl = (round(float(forming["open"].iloc[0]), digits),
                  round(float(forming["high"].max()), digits),
                  round(float(forming["low"].min()), digits))
    return {
        "live_price": live,
        "live_price_ts_utc": live_ts.isoformat(),
        "live_price_stale": False,
        "live_price_source": source,
        "live_price_fetched_at_utc": fetched,
        "forming_m15_open": fo,
        "forming_m15_high": fh,
        "forming_m15_low": fl,
        "forming_m15_open_utc": opened.isoformat(),
        "minutes_into_bar": minutes_into_bar(now, tf_min),
    }


def fallback_live(ctx: dict, *, now: datetime | None = None, tf_min: int = 15,
                  reason: str = "m1_fetch_failed") -> dict:
    """Copy the last closed M15 close when M1 is unavailable."""
    now = _now_utc(now)
    digits = int(ctx.get("digits") or 5)
    close = ctx.get("last_close")
    if close is None and isinstance(ctx.get("signal_candle"), dict):
        close = ctx["signal_candle"].get("close")
    close = None if close is None else round(float(close), digits)
    ts = ctx.get("last_closed_bar_utc") or now.isoformat()
    return {
        "live_price": close,
        "live_price_ts_utc": ts,
        "live_price_stale": True,
        "live_price_source": f"fallback_last_close:{reason}"[:160],
        "live_price_fetched_at_utc": now.isoformat(),
        "forming_m15_open": None,
        "forming_m15_high": None,
        "forming_m15_low": None,
        "forming_m15_open_utc": forming_m15_open(now, tf_min).isoformat(),
        "minutes_into_bar": minutes_into_bar(now, tf_min),
    }


def apply_live_fields(payload: dict, live: dict, tf_min: int = 15) -> dict:
    """Write live fields onto payload context, last_price aliases, and a paler forming_candle for the chart."""
    ctx = payload.setdefault("context", {})
    ctx.update(live)
    # last_price follows the live quote when fresh, so existing "near-market entry" paths stay correct.
    if live.get("live_price") is not None:
        ctx["last_price"] = live["live_price"]
        ctx["last_price_ts_utc"] = live.get("live_price_ts_utc") or ctx.get("last_price_ts_utc")
    payload["forming_candle"] = None
    if (not live.get("live_price_stale")
            and live.get("forming_m15_open") is not None
            and live.get("live_price") is not None):
        opened = live.get("forming_m15_open_utc")
        try:
            t = int(pd.Timestamp(opened).timestamp()) if opened else None
        except Exception:
            t = None
        if t is not None:
            payload["forming_candle"] = {
                "time": t,
                "open": live["forming_m15_open"],
                "high": live["forming_m15_high"],
                "low": live["forming_m15_low"],
                "close": live["live_price"],
            }
    return payload


def _including_forming(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df[df.index <= pd.Timestamp(_now_utc(now))]


async def _fetch_duka_m1(ticker: str, since: datetime, now: datetime, timeout: float) -> pd.DataFrame:
    dp = DukascopyProvider()
    out = tempfile.mkdtemp(prefix="duka_live_")
    try:
        args = dp.cmd + ["-i", ticker, "-from", (since - timedelta(minutes=5)).strftime("%Y-%m-%d"),
                         "-to", (now + timedelta(days=1)).strftime("%Y-%m-%d"), "-t", "m1", "-f", "json",
                         "-v", "true", "-dir", out, "-r", "2"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
        files = glob.glob(f"{out}/*.json")
        if not files:
            raise RuntimeError(f"dukascopy m1 {ticker}: no file ({stdout.decode()[-200:].strip()})")
        rows = json.load(open(files[0]))
    finally:
        shutil.rmtree(out, ignore_errors=True)
    if not rows:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    return _including_forming(df[COLS].astype(float), now)


async def _fetch_binance_m1(ticker: str, since: datetime, now: datetime) -> pd.DataFrame:
    # Pull a short 1m window including the forming bar (Binance returns the in-progress candle last).
    client = await _get_httpx()
    # 3 hours of M1 = 180 bars; pad a little
    r = await client.get(BinanceProvider.URL, params={"symbol": ticker, "interval": "1m", "limit": 240})
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame([[x[0], *x[1:6]] for x in rows], columns=["t", *COLS])
    df.index = pd.to_datetime(df.pop("t"), unit="ms", utc=True)
    df = _including_forming(df.astype(float), now)
    return df[df.index >= pd.Timestamp(since)]


async def fetch_symbol_m1(symbol: str, cfg: dict, now: datetime | None = None) -> tuple[pd.DataFrame, str]:
    """Return (m1_df, source). Tries Dukascopy first, then Binance if a ticker is configured."""
    now = _now_utc(now)
    since = now - timedelta(minutes=_M1_LOOKBACK_MIN)
    errors = []
    duka = m1_ticker(symbol, cfg, "dukascopy")
    if duka:
        try:
            df = await _fetch_duka_m1(duka, since, now, _M1_TIMEOUT)
            df = df[df.index >= pd.Timestamp(since)]
            if len(df):
                return df, f"dukascopy:{duka}:m1"
            errors.append(f"dukascopy:{duka}: empty")
        except Exception as e:
            errors.append(f"dukascopy:{duka}: {e}")
            log.warning("%s live M1 dukascopy failed: %s", symbol, e)
    bina = m1_ticker(symbol, cfg, "binance")
    if bina:
        try:
            df = await _fetch_binance_m1(bina, since, now)
            if len(df):
                return df, f"binance:{bina}:m1"
            errors.append(f"binance:{bina}: empty")
        except Exception as e:
            errors.append(f"binance:{bina}: {e}")
            log.warning("%s live M1 binance failed: %s", symbol, e)
    raise RuntimeError(f"{symbol}: live M1 failed: {errors or 'no m1 ticker'}")


async def enrich_live_prices(payloads: list[dict], cfg: dict, now: datetime | None = None) -> dict:
    """Attach live_price fields to every payload. Never raises; failed symbols are marked stale."""
    now = _now_utc(now)
    tf = int(cfg.get("general", {}).get("timeframe_minutes") or 15)
    t0 = time.monotonic()
    results = await asyncio.gather(*[fetch_symbol_m1(p["symbol"], cfg, now) for p in payloads],
                                   return_exceptions=True)
    timings, stale_n = {}, 0
    for p, res in zip(payloads, results):
        digits = int((p.get("context") or {}).get("digits") or p.get("digits") or 5)
        if isinstance(res, Exception):
            live = fallback_live(p.get("context") or {}, now=now, tf_min=tf, reason=str(res)[:120])
            stale_n += 1
            log.warning("%s live price fallback: %s", p.get("symbol"), res)
        else:
            df, source = res
            try:
                live = live_from_m1(df, tf_min=tf, now=now, digits=digits, source=source)
            except Exception as e:
                live = fallback_live(p.get("context") or {}, now=now, tf_min=tf, reason=str(e)[:120])
                stale_n += 1
        apply_live_fields(p, live, tf_min=tf)
        timings[p["symbol"]] = {"stale": live["live_price_stale"], "source": live.get("live_price_source"),
                                "live_price": live.get("live_price")}
    ms = round((time.monotonic() - t0) * 1000, 1)
    log.info("timing live_m1_total=%.1fms stale=%d/%d prices=%s", ms, stale_n, len(payloads), timings)
    return {"ms": ms, "stale": stale_n, "n": len(payloads), "prices": timings}
