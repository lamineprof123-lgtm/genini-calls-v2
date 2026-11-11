"""Intrabar live price, used by Gemini context, the chart's forming candle, scalp entry, and Lucid.

The M15 signal_candle stays the last CLOSED bar; indicators never see live data.
Live price, best first (live_price_source):
  firmtape:NQ     US100 in NY regular hours: FirmTape 1-s NQ + rolling basis (signals/firmtape_live.py)
  dukascopy:tick  last Dukascopy tick (BID, same side as the bars), seconds old
  dukascopy:m1 / binance:m1   last finished M1 close (1-2 min old) when ticks fail or lag M1
  fallback_last_close:...     last closed M15 close, live_price_stale=True
The forming M15 candle = finished M1 bars of the current 15-min window + ticks after the last finished minute.
live_price_age_s = wall-clock time of the fetch minus the live price timestamp.
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
_TICK_TIMEOUT = 15.0
_TICK_LOOKBACK_MIN = 20     # tick window: covers the whole forming M15 bar + a quiet-market cushion

# Diagnostics kept on the context for the cycle log but not sent to Gemini (gemini_client.slim_gemini_context).
LIVE_LOG_ONLY_KEYS = (
    "live_price_age_s", "live_price_fallback", "live_price_dukascopy", "live_price_dukascopy_ts_utc",
    "live_price_dukascopy_source", "live_basis", "live_basis_n", "firmtape_status",
)

LIVE_CONTEXT_KEYS = (
    "live_price", "live_price_ts_utc", "live_price_stale", "live_price_source",
    "live_price_fetched_at_utc", "forming_m15_open", "forming_m15_high", "forming_m15_low",
    "forming_m15_open_utc", "minutes_into_bar", "live_price_age_s",
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


def live_from_ticks(m1: pd.DataFrame | None, ticks: pd.DataFrame, *, tf_min: int = 15,
                    now: datetime | None = None, digits: int = 5, source: str = "dukascopy:tick") -> dict:
    """live_price = last tick bid; forming M15 = finished M1 bars of the window + ticks after the last finished
    minute. Raises ValueError when the ticks are unusable (empty, lag the M1 feed, or nothing in the window)."""
    now = _now_utc(now)
    if ticks is None or len(ticks) == 0:
        raise ValueError("no ticks")
    t = ticks[~ticks.index.duplicated(keep="last")].sort_index()
    last_ts = t.index[-1]
    m = None
    if m1 is not None and len(m1):
        m = m1[~m1.index.duplicated(keep="last")].sort_index()
        if m.index.tz is None:
            m.index = m.index.tz_localize("UTC")
        m = m[m.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]   # finished minutes only
        if len(m) and last_ts < m.index[-1]:
            raise ValueError(f"ticks lag M1 (last tick {last_ts.isoformat()} < last M1 {m.index[-1].isoformat()})")
    opened = forming_m15_open(now, tf_min)
    m1_end = (m.index[-1] + pd.Timedelta(minutes=1)) if m is not None and len(m) else opened
    mw = m[m.index >= opened] if m is not None else pd.DataFrame(columns=COLS)
    tw = t[t.index >= max(opened, m1_end)]
    if mw.empty and tw.empty:
        raise ValueError("no M1 or ticks inside the forming bar")
    o = float(mw["open"].iloc[0]) if len(mw) else float(tw["bid"].iloc[0])
    highs = ([float(mw["high"].max())] if len(mw) else []) + ([float(tw["bid"].max())] if len(tw) else [])
    lows = ([float(mw["low"].min())] if len(mw) else []) + ([float(tw["bid"].min())] if len(tw) else [])
    live = round(float(t["bid"].iloc[-1]), digits)
    return {
        "live_price": live,
        "live_price_ts_utc": last_ts.to_pydatetime().isoformat(),
        "live_price_stale": False,
        "live_price_source": source,
        "live_price_fetched_at_utc": now.isoformat(),
        "forming_m15_open": round(o, digits),
        "forming_m15_high": round(max(highs + [live]), digits),
        "forming_m15_low": round(min(lows + [live]), digits),
        "forming_m15_open_utc": opened.isoformat(),
        "minutes_into_bar": minutes_into_bar(now, tf_min),
    }


async def _fetch_duka_ticks(ticker: str, since: datetime, timeout: float) -> pd.DataFrame:
    """Dukascopy ticks since `since` (seconds-fresh, includes the current hour). Columns bid, ask."""
    dp = DukascopyProvider()
    out = tempfile.mkdtemp(prefix="duka_tick_")
    try:
        args = dp.cmd + ["-i", ticker, "-from", since.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "-to", "now",
                         "-t", "tick", "-f", "json", "-dir", out, "-r", "1", "-s"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        files = glob.glob(f"{out}/*.json")
        if not files:
            raise RuntimeError(f"dukascopy tick {ticker}: no file ({stdout.decode()[-200:].strip()})")
        rows = json.load(open(files[0]))
    finally:
        shutil.rmtree(out, ignore_errors=True)
    if not rows:
        raise RuntimeError(f"dukascopy tick {ticker}: empty")
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    return df.rename(columns={"bidPrice": "bid", "askPrice": "ask"})[["bid", "ask"]].astype(float)


def _age_s(live: dict, at: datetime) -> float | None:
    try:
        return round((at - datetime.fromisoformat(str(live["live_price_ts_utc"]))).total_seconds(), 2)
    except Exception:
        return None


def build_live(symbol: str, m1_res, tick_res, *, tf_min: int, now: datetime, digits: int, ctx: dict) -> dict:
    """Pick the freshest usable live price: ticks -> M1 -> last close. Never raises."""
    m1_df = None if isinstance(m1_res, BaseException) else m1_res[0]
    why = []
    if tick_res is not None and not isinstance(tick_res, BaseException):
        try:
            return live_from_ticks(m1_df, tick_res, tf_min=tf_min, now=now, digits=digits)
        except Exception as e:
            why.append(f"tick: {e}")
    elif isinstance(tick_res, BaseException):
        why.append(f"tick: {type(tick_res).__name__}: {tick_res}")
    if m1_df is not None:
        try:
            src = m1_res[1]
            live = live_from_m1(m1_df, tf_min=tf_min, now=now, digits=digits, source=src)
            if why:
                live["live_price_fallback"] = "; ".join(why)[:200]
            return live
        except Exception as e:
            why.append(f"m1: {e}")
    else:
        why.append(f"m1: {m1_res}")
    log.warning("%s live price fallback: %s", symbol, why)
    return fallback_live(ctx, now=now, tf_min=tf_min, reason="; ".join(why)[:120])


async def _fetch_m1_labelled(symbol: str, cfg: dict, now: datetime):
    df, src = await fetch_symbol_m1(symbol, cfg, now)
    return df, ("dukascopy:m1" if src.startswith("dukascopy") else "binance:m1" if src.startswith("binance") else src)


async def _none():
    return None


async def enrich_live_prices(payloads: list[dict], cfg: dict, now: datetime | None = None) -> dict:
    """Attach live_price fields to every payload. Never raises; failed symbols are marked stale."""
    from . import firmtape_live as FT
    now = _now_utc(now)
    lcfg = cfg.get("live_price") or {}
    tf = int(cfg.get("general", {}).get("timeframe_minutes") or 15)
    use_ticks = bool(lcfg.get("ticks", True))
    tick_since = now - timedelta(minutes=float(lcfg.get("tick_lookback_minutes", _TICK_LOOKBACK_MIN)))
    t0 = time.monotonic()
    m1_tasks = [_fetch_m1_labelled(p["symbol"], cfg, now) for p in payloads]
    tick_tasks = []
    for p in payloads:
        duka = m1_ticker(p["symbol"], cfg, "dukascopy")
        # ticks only when the chart's bars are Dukascopy too (same price scale and bid side)
        if p.get("source") not in (None, "dukascopy"):
            duka = None
        tick_tasks.append(_fetch_duka_ticks(duka, tick_since, float(lcfg.get("tick_timeout_seconds", _TICK_TIMEOUT)))
                          if (use_ticks and duka) else _none())
    want_ft = FT.wanted(payloads, cfg, now)
    res = await asyncio.gather(*m1_tasks, *tick_tasks, *([FT.shared_levels(cfg, max_age_s=2.0)] if want_ft else []),
                               return_exceptions=True)
    n = len(payloads)
    m1_res, tick_res = res[:n], res[n:2 * n]
    ft_data, ft_err = None, None
    if want_ft:
        r = res[2 * n]
        ft_data, ft_err = (None, f"{type(r).__name__}: {r}") if isinstance(r, BaseException) else r
    fetched = datetime.now(timezone.utc)
    timings, stale_n, m1_frames = {}, 0, {}
    for p, mr, tr in zip(payloads, m1_res, tick_res):
        if not isinstance(mr, BaseException):
            m1_frames[p["symbol"]] = mr[0]
        ctx = p.get("context") or {}
        digits = int(ctx.get("digits") or p.get("digits") or 5)
        live = build_live(p["symbol"], mr, tr, tf_min=tf, now=now, digits=digits, ctx=ctx)
        live["live_price_fetched_at_utc"] = fetched.isoformat()
        stale_n += bool(live["live_price_stale"])
        apply_live_fields(p, live, tf_min=tf)
        ft = FT.apply_firmtape_live(p, ft_data, ft_err, fetched, cfg) if want_ft else None
        c = p["context"]
        c["live_price_age_s"] = _age_s(c, fetched)
        timings[p["symbol"]] = {"source": c.get("live_price_source"), "age_s": c.get("live_price_age_s"),
                                "live_price": c.get("live_price"), "stale": c.get("live_price_stale"),
                                **({"firmtape": ft.get("firmtape"), "basis": ft.get("basis")}
                                   if ft and ft.get("firmtape") != "n/a" else {}),
                                **({"fallback": live["live_price_fallback"]} if live.get("live_price_fallback") else {})}
    ms = round((time.monotonic() - t0) * 1000, 1)
    log.info("timing live_total=%.1fms stale=%d/%d prices=%s", ms, stale_n, len(payloads), timings)
    return {"ms": ms, "stale": stale_n, "n": len(payloads), "prices": timings, "m1": m1_frames}
