"""VWAP, ATR channel, EMA50, RVOL, Volume Profile (POC/VAH/VAL), CVD, session timing, and S/R levels (spec: see [chart.*] in config.toml)."""
from __future__ import annotations
import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()  # Wilder RMA


def session_key(index: pd.DatetimeIndex, anchor: str) -> pd.DatetimeIndex:
    """Session id per bar. utc_day: resets 00:00 UTC. cme_globex: resets 18:00 America/New_York."""
    if anchor == "cme_globex":
        ny = index.tz_convert("America/New_York")
        return (ny + pd.Timedelta(hours=6)).normalize()
    return index.normalize()


def vwap(df: pd.DataFrame, anchor: str = "utc_day") -> pd.Series:
    """Session-anchored VWAP on typical price, offset 0, no deviation bands."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].clip(lower=0)
    if vol.sum() <= 0:  # e.g. yfinance FX has no volume -> equal weights (documented)
        vol = pd.Series(1.0, index=df.index)
    key = session_key(df.index, anchor)
    return (tp * vol).groupby(key).cumsum() / vol.groupby(key).cumsum().replace(0, np.nan)


def pivots(df: pd.DataFrame, lb: int) -> list[tuple[str, float]]:
    h, l = df["high"].values, df["low"].values
    out = []
    for i in range(lb, len(df) - lb):
        if h[i] == h[i - lb:i + lb + 1].max():
            out.append(("high", float(h[i])))
        if l[i] == l[i - lb:i + lb + 1].min():
            out.append(("low", float(l[i])))
    return out


def sr_levels(df: pd.DataFrame, cfg_sr: dict, anchor: str, tf_min: int, atr_now: float, digits: int) -> list[dict]:
    """Candidates in priority order: current session H/L, previous session H/L, rolling 24h H/L, pivots (S/R).
    Levels within dedupe_atr x ATR are merged (higher priority label kept); capped per side."""
    last = float(df["close"].iloc[-1])
    key = session_key(df.index, anchor)
    cands: list[tuple[int, str, float]] = []
    cur = df[key == key[-1]]
    cands += [(0, "Session High", float(cur["high"].max())), (0, "Session Low", float(cur["low"].min()))]
    prev_keys = sorted(set(key[key < key[-1]]))
    if prev_keys:
        prev = df[key == prev_keys[-1]]
        cands += [(1, "Prev High", float(prev["high"].max())), (1, "Prev Low", float(prev["low"].min()))]
    n24 = int(cfg_sr["rolling_hours"] * 60 / tf_min)
    w = df.tail(n24)
    cands += [(2, "24h High", float(w["high"].max())), (2, "24h Low", float(w["low"].min()))]
    cands += [(3, "S/R", p) for _, p in pivots(df, cfg_sr["pivot_lookback"])]
    tol = cfg_sr["dedupe_atr"] * atr_now if atr_now > 0 else 0
    kept: list[dict] = []
    for pri, label, price in sorted(cands, key=lambda c: c[0]):
        dup = next((k for k in kept if abs(k["price"] - price) <= tol), None)
        if dup:
            dup["touches"] += 1
            continue
        kept.append({"label": label, "price": price, "priority": pri, "touches": 1})
    above = sorted([k for k in kept if k["price"] > last], key=lambda k: k["price"])[:cfg_sr["max_per_side"]]
    below = sorted([k for k in kept if k["price"] <= last], key=lambda k: -k["price"])[:cfg_sr["max_per_side"]]
    return ([{"type": "resistance", "label": k["label"], "price": round(k["price"], digits), "touches": k["touches"]}
             for k in above] +
            [{"type": "support", "label": k["label"], "price": round(k["price"], digits), "touches": k["touches"]}
             for k in below])


def build_payload(symbol: str, df: pd.DataFrame, cfg: dict, source: str, ticker: str) -> dict:
    """Indicators are computed on the full fetched history; the last `candles` closed bars are drawn."""
    ch, g = cfg["chart"], cfg["general"]
    ac, vc, sc = ch["atr_channel"], ch["vwap"], ch["sr"]
    e50c = ch.get("ema50", {})
    rvol_c = ch.get("rvol", {})
    scfg = cfg.get("symbols", {}).get(symbol, {})
    anchor = scfg.get("vwap_anchor", vc["default_anchor"])
    digits = scfg.get("digits", 5)
    ema50_len = int(e50c.get("length", 50))
    rvol_len = int(rvol_c.get("length", 20))
    rvol_thr = float(rvol_c.get("threshold", 2.0))
    rvol_hi = rvol_c.get("spike_color", "#FACC15")
    rvol_lo = rvol_c.get("normal_color", "#374151")
    d = df.copy()
    d["atr"] = atr(d, ac["atr_length"])
    d["mid"] = d["close"].ewm(span=ac["ema_length"], adjust=False).mean()
    d["ema50"] = d["close"].ewm(span=ema50_len, adjust=False).mean()
    d["upper"] = d["mid"] + ac["mult"] * d["atr"]
    d["lower"] = d["mid"] - ac["mult"] * d["atr"]
    d["vwap"] = vwap(d, anchor)
    vol_sma = d["volume"].rolling(rvol_len, min_periods=1).mean().replace(0, np.nan)
    d["rvol"] = d["volume"] / vol_sma
    vpc = ch.get("volume_profile", {})
    vp = session_volume_profile(
        d, anchor,
        price_bins=int(vpc.get("price_bins", 50)),
        value_area_pct=float(vpc.get("value_area_pct", 0.70)),
        digits=digits,
    )
    cvd_c = ch.get("cvd", {})
    cvd = session_cumulative_delta(
        d, anchor,
        neutral_pct=float(cvd_c.get("neutral_pct", 0.05)),
        slope_bars=int(cvd_c.get("slope_bars", 4)),
    )
    sess_c = ch.get("session", {})
    sess_windows = {
        "LONDON_SESSION": tuple(sess_c.get("london", ["03:00", "08:00"])),
        "LONDON_NY_OVERLAP": tuple(sess_c.get("overlap", ["08:00", "11:00"])),
        "NEW_YORK_SESSION": tuple(sess_c.get("new_york", ["11:00", "16:00"])),
    }
    asian = tuple(sess_c.get("asian", ["18:00", "02:00"]))
    bar_ts = d.index[-1]
    session_name = market_session(bar_ts, windows=sess_windows, asian=asian)
    a_now = float(d["atr"].iloc[-1])
    levels = sr_levels(d, sc, anchor, g["timeframe_minutes"], a_now, digits)
    view = d.tail(g["candles"])
    t = [int(x) for x in (view.index - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)]
    r = lambda s: [None if pd.isna(x) else round(float(x), digits + 1) for x in s]
    line = lambda col: [{"time": ti, "value": v} for ti, v in zip(t, r(view[col])) if v is not None]
    atr_s = d["atr"]
    last = view.iloc[-1]
    rvol_flags = 0
    rvol_bars_since_spike = None  # 0 = last closed bar is a spike; None = no spike in view
    volume = []
    for i, (ti, v, o, c, rv) in enumerate(zip(t, view["volume"], view["open"], view["close"], view["rvol"])):
        spike = bool(pd.notna(rv) and float(rv) > rvol_thr)
        if spike:
            rvol_flags += 1
            rvol_bars_since_spike = len(t) - 1 - i  # overwritten each spike; ends as bars since last
        volume.append({
            "time": ti, "value": float(v), "up": bool(c >= o),
            "rvol": None if pd.isna(rv) else round(float(rv), 3),
            "color": rvol_hi if spike else rvol_lo,
        })
    style = {k: v for k, v in ch.items() if not isinstance(v, dict)} | {
        "vwap": vc, "atr_channel": ac, "sr": sc,
        "ema50": {"color": e50c.get("color", "#9CA3AF"), "width": e50c.get("width", 1), "length": ema50_len},
        "rvol": {"length": rvol_len, "threshold": rvol_thr, "spike_color": rvol_hi, "normal_color": rvol_lo},
        "template": ch.get("template", "chart.html"),
    }
    return {
        "symbol": symbol, "source": source, "ticker": ticker, "timeframe": f"M{g['timeframe_minutes']}",
        "digits": digits, "visible": ch["visible_candles"],
        "style": style,
        "candles": [{"time": ti, "open": o, "high": h, "low": l, "close": c} for ti, o, h, l, c in
                    zip(t, r(view["open"]), r(view["high"]), r(view["low"]), r(view["close"]))],
        "volume": volume,
        "vwap": line("vwap"), "mid": line("mid"), "ema50": line("ema50"),
        "upper": line("upper"), "lower": line("lower"),
        "levels": levels,
        "rvol_spike_count": rvol_flags,
        "context": {
            "last_closed_bar_utc": view.index[-1].isoformat(),
            "digits": digits,
            "signal_candle": {
                "open": round(float(last["open"]), digits),
                "high": round(float(last["high"]), digits),
                "low": round(float(last["low"]), digits),
                "close": round(float(last["close"]), digits),
            },
            "last_candle": {
                "open": round(float(last["open"]), digits),
                "high": round(float(last["high"]), digits),
                "low": round(float(last["low"]), digits),
                "close": round(float(last["close"]), digits),
            },
            "last_close": round(float(last["close"]), digits),
            "last_price": round(float(last["close"]), digits),
            "last_price_ts_utc": view.index[-1].isoformat(),
            "vwap": round(float(last["vwap"]), digits),
            "vwap_anchor": anchor, "atr": round(a_now, digits + 1),
            "ema20_baseline": round(float(last["mid"]), digits),
            "ema50": round(float(last["ema50"]), digits),
            "atr_upper": round(float(last["upper"]), digits), "atr_lower": round(float(last["lower"]), digits),
            "atr_expanding_vs_20bar_avg": bool(atr_s.iloc[-1] > atr_s.iloc[-21:-1].mean()),
            "close_vs_vwap": "above" if last["close"] > last["vwap"] else "below",
            "rvol_last": None if pd.isna(last["rvol"]) else round(float(last["rvol"]), 3),
            "rvol_spike_count": rvol_flags,
            "rvol_bars_since_spike": rvol_bars_since_spike,
            "poc": vp["poc"], "vah": vp["vah"], "val": vp["val"],
            "vp_session_anchor": vp.get("session_anchor", anchor),
            "vp_session_bars": vp.get("session_bars", vp.get("bars", 0)),
            "vp_value_area_pct": float(vpc.get("value_area_pct", 0.70)),
            "price_vs_value_area": (
                "above_vah" if float(last["close"]) > vp["vah"]
                else "below_val" if float(last["close"]) < vp["val"]
                else "inside"
            ),
            "close_vs_poc": (
                "above" if float(last["close"]) > vp["poc"]
                else "below" if float(last["close"]) < vp["poc"]
                else "at"
            ),
            "cvd": cvd["cvd"],
            "cvd_label": cvd["cvd_label"],
            "cvd_slope": cvd["cvd_slope"],
            "cvd_session_volume": cvd["session_volume"],
            "cvd_header": format_cvd_header(cvd["cvd"], cvd["cvd_label"]),
            "session": session_name,
            "session_tz": "America/New_York",
            "session_blocks_automation": session_blocks_automation(
                session_name, sess_c.get("blocked_for_automation")),
            "levels": levels,
        },
    }

# --- Volume Profile (session POC / VAH / VAL) ---------------------------------

def _session_slice(df: pd.DataFrame, anchor: str) -> pd.DataFrame:
    """Bars belonging to the current session (same anchors as VWAP)."""
    if df.empty:
        return df
    key = session_key(df.index, anchor)
    return df.loc[key == key[-1]].copy()


def volume_profile(
    df: pd.DataFrame,
    *,
    price_bins: int = 50,
    value_area_pct: float = 0.70,
    digits: int = 2,
) -> dict:
    """Session Volume Profile: POC + contiguous 70% Value Area (VAH/VAL).

    Fixes vs the naive "sort-by-volume cumsum" approach:
      - Value Area expands from the POC bin to the higher-volume adjacent bin
        (ties expand both sides), so the area stays contiguous and always
        includes the POC.
      - Each bar's volume is distributed uniformly across the bins overlapping
        its [low, high] range (typical-price fallback when high == low).
      - Prices are rounded to ``digits`` (FX = 5), not hard-coded to 2.

    ``df`` should already be the session slice (caller filters). Dukascopy FX /
    gold / index volume is tick volume; Binance BTC is exchange volume — the
    profile shape is still meaningful either way.
    """
    empty = {
        "poc": 0.0, "vah": 0.0, "val": 0.0,
        "bars": 0, "bins": 0, "total_volume": 0.0,
        "value_area_pct_achieved": 0.0,
    }
    if df is None or df.empty:
        return empty

    need = {"high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        # tolerate close-only input via typical-price fallback
        if "close" not in df.columns or "volume" not in df.columns:
            return empty
        d = df.copy()
        d["high"] = d["close"]
        d["low"] = d["close"]
    else:
        d = df

    highs = d["high"].astype(float).to_numpy()
    lows = d["low"].astype(float).to_numpy()
    closes = d["close"].astype(float).to_numpy()
    vols = d["volume"].astype(float).clip(lower=0).to_numpy()
    n_bars = int(len(d))

    lo = float(np.nanmin(lows))
    hi = float(np.nanmax(highs))
    if not np.isfinite(lo) or not np.isfinite(hi):
        return empty
    if hi <= lo or n_bars < 1 or float(vols.sum()) <= 0:
        # degenerate session: pin all three levels to last close / mid
        px = float(closes[-1]) if np.isfinite(closes[-1]) else lo
        px = round(px, digits)
        return {
            "poc": px, "vah": px, "val": px,
            "bars": n_bars, "bins": 1, "total_volume": float(vols.sum()),
            "value_area_pct_achieved": 1.0,
        }

    n_bins = max(5, int(price_bins))
    edges = np.linspace(lo, hi, n_bins + 1)
    bin_vol = np.zeros(n_bins, dtype=float)

    # Distribute each bar's volume across overlapping price bins.
    for h, l, c, v in zip(highs, lows, closes, vols):
        if not np.isfinite(v) or v <= 0:
            continue
        if not np.isfinite(h) or not np.isfinite(l):
            continue
        if h < l:
            h, l = l, h
        if h == l:
            # Flat bar: put volume on the bin containing typical/close price.
            px = c if np.isfinite(c) else h
            # rightmost edge belongs to last bin
            idx = int(np.searchsorted(edges, px, side="right") - 1)
            idx = max(0, min(n_bins - 1, idx))
            bin_vol[idx] += v
            continue
        span = h - l
        # Bins that overlap [l, h]
        left = int(np.searchsorted(edges, l, side="right") - 1)
        right = int(np.searchsorted(edges, h, side="left") - 1)
        left = max(0, left)
        right = min(n_bins - 1, max(left, right))
        for i in range(left, right + 1):
            overlap_lo = max(l, edges[i])
            overlap_hi = min(h, edges[i + 1])
            overlap = overlap_hi - overlap_lo
            if overlap > 0:
                bin_vol[i] += v * (overlap / span)

    total = float(bin_vol.sum())
    if total <= 0:
        px = round(float(closes[-1]), digits)
        return {
            "poc": px, "vah": px, "val": px,
            "bars": n_bars, "bins": n_bins, "total_volume": 0.0,
            "value_area_pct_achieved": 0.0,
        }

    poc_idx = int(np.argmax(bin_vol))
    # POC = midpoint of the densest bin
    poc = 0.5 * (edges[poc_idx] + edges[poc_idx + 1])

    # Contiguous Value Area: expand from POC to the higher-volume neighbour.
    target = total * float(value_area_pct)
    left = right = poc_idx
    cum = float(bin_vol[poc_idx])
    while cum < target and (left > 0 or right < n_bins - 1):
        lv = float(bin_vol[left - 1]) if left > 0 else -1.0
        rv = float(bin_vol[right + 1]) if right < n_bins - 1 else -1.0
        if lv < 0 and rv < 0:
            break
        if lv == rv and left > 0 and right < n_bins - 1:
            left -= 1
            right += 1
            cum += lv + rv
        elif rv > lv:
            right += 1
            cum += rv
        else:
            left -= 1
            cum += lv

    val = float(edges[left])
    vah = float(edges[right + 1])
    return {
        "poc": round(float(poc), digits),
        "vah": round(vah, digits),
        "val": round(val, digits),
        "bars": n_bars,
        "bins": n_bins,
        "total_volume": round(total, 3),
        "value_area_pct_achieved": round(cum / total, 4) if total else 0.0,
    }


def session_volume_profile(
    df: pd.DataFrame,
    anchor: str,
    *,
    price_bins: int = 50,
    value_area_pct: float = 0.70,
    digits: int = 2,
) -> dict:
    """Filter ``df`` to the current session, then compute POC/VAH/VAL."""
    sess = _session_slice(df, anchor)
    out = volume_profile(
        sess, price_bins=price_bins, value_area_pct=value_area_pct, digits=digits)
    out["session_anchor"] = anchor
    out["session_bars"] = int(len(sess))
    return out


# --- Cumulative Delta (approx) + session timing ---------------------------------

def bar_delta(high: float, low: float, close: float, volume: float) -> float:
    """Buying vs selling pressure for one bar. Flat range -> 0 (never divide by epsilon)."""
    try:
        h, l, c, v = float(high), float(low), float(close), float(volume)
    except (TypeError, ValueError):
        return 0.0
    if not (np.isfinite(h) and np.isfinite(l) and np.isfinite(c) and np.isfinite(v)) or v <= 0:
        return 0.0
    rng = h - l
    if rng <= 0:
        return 0.0
    # (close-low) - (high-close) = 2*close - high - low
    return v * (2.0 * c - h - l) / rng


def cumulative_delta(
    df: pd.DataFrame,
    *,
    neutral_pct: float = 0.05,
    slope_bars: int = 4,
) -> dict:
    """Approximate session CVD from OHLCV (tick volume OK).

    Per-bar delta = volume * ((close-low) - (high-close)) / range; range==0 => 0.
    Label: BULLISH / BEARISH / NEUTRAL when |CVD| / session_volume < neutral_pct.
    ``cvd_slope`` = change in cumulative over the last ``slope_bars`` bars of the slice.
    """
    empty = {
        "cvd": 0.0, "cvd_label": "NEUTRAL", "cvd_slope": 0.0,
        "session_volume": 0.0, "bars": 0, "deltas": [],
    }
    if df is None or df.empty:
        return empty
    need = {"high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        return empty

    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    vols = df["volume"].astype(float).clip(lower=0).to_numpy()
    deltas = np.array(
        [bar_delta(h, l, c, v) for h, l, c, v in zip(highs, lows, closes, vols)],
        dtype=float,
    )
    cvd = float(np.nansum(deltas))
    sess_vol = float(np.nansum(vols))
    n = int(len(deltas))
    sb = max(1, int(slope_bars))
    if n >= sb:
        slope = float(np.nansum(deltas[-sb:]))
    else:
        slope = float(np.nansum(deltas))
    thr = float(neutral_pct) * sess_vol if sess_vol > 0 else 0.0
    if sess_vol <= 0 or abs(cvd) < thr:
        label = "NEUTRAL"
    elif cvd > 0:
        label = "BULLISH"
    else:
        label = "BEARISH"
    return {
        "cvd": round(cvd, 2),
        "cvd_label": label,
        "cvd_slope": round(slope, 2),
        "session_volume": round(sess_vol, 3),
        "bars": n,
        "deltas": [round(float(x), 3) for x in deltas.tolist()],
    }


def session_cumulative_delta(
    df: pd.DataFrame,
    anchor: str,
    *,
    neutral_pct: float = 0.05,
    slope_bars: int = 4,
) -> dict:
    """Filter to the current VWAP/VP session, then compute approximate CVD."""
    sess = _session_slice(df, anchor)
    out = cumulative_delta(sess, neutral_pct=neutral_pct, slope_bars=slope_bars)
    out["session_anchor"] = anchor
    out["session_bars"] = int(len(sess))
    return out


def format_cvd_header(cvd: float, label: str) -> str:
    """Header fragment: 'CVD +14,250 (BULLISH)'."""
    n = float(cvd)
    if abs(n) >= 100:
        body = f"{n:+,.0f}"
    else:
        body = f"{n:+,.2f}"
    return f"CVD {body} ({label})"


# Session windows are America/New_York, half-open [start, end). Configurable via [chart.session].
_DEFAULT_SESSION_WINDOWS = {
    "LONDON_SESSION": ("03:00", "08:00"),
    "LONDON_NY_OVERLAP": ("08:00", "11:00"),
    "NEW_YORK_SESSION": ("11:00", "16:00"),
}
_DEFAULT_ASIAN = ("18:00", "02:00")  # wraps midnight
_DEFAULT_BLOCKED = ("ASIAN_SESSION_CHOP", "LOW_LIQUIDITY_DEADZONE")


def _parse_hm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")
    return int(h), int(m)


def _time_val(h: int, m: int) -> float:
    return h + m / 60.0


def market_session(
    ts,
    *,
    windows: dict | None = None,
    asian: tuple[str, str] | list | None = None,
) -> str:
    """Institutional liquidity window for a bar timestamp (America/New_York).

    Uses the SIGNAL BAR time (not wall clock) so replay stays correct.
    Fixed vs the naive snippet: LONDON (03-08) / OVERLAP (08-11) / NEW_YORK (11-16)
    are distinct; NEW_YORK is no longer shadowed by the 03-11 branch.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    if ts is None:
        return "LOW_LIQUIDITY_DEADZONE"
    if isinstance(ts, str):
        ts = pd.Timestamp(ts)
    if isinstance(ts, pd.Timestamp):
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        et = ts.tz_convert("America/New_York")
        tv = _time_val(int(et.hour), int(et.minute))
    elif isinstance(ts, _dt):
        et = ts.astimezone(ZoneInfo("America/New_York"))
        tv = _time_val(et.hour, et.minute)
    else:
        return "LOW_LIQUIDITY_DEADZONE"

    wins = windows or _DEFAULT_SESSION_WINDOWS
    # Check named windows in order: London, Overlap, New York
    ordered = (
        "LONDON_SESSION",
        "LONDON_NY_OVERLAP",
        "NEW_YORK_SESSION",
    )
    for name in ordered:
        bounds = wins.get(name)
        if not bounds:
            continue
        a, b = _parse_hm(bounds[0]), _parse_hm(bounds[1])
        lo, hi = _time_val(*a), _time_val(*b)
        if lo <= tv < hi:
            return name

    asian = asian or _DEFAULT_ASIAN
    a_s, a_e = _parse_hm(asian[0]), _parse_hm(asian[1])
    a_lo, a_hi = _time_val(*a_s), _time_val(*a_e)
    # Asian wraps midnight: e.g. 18:00 -> 02:00
    if a_lo > a_hi:
        if tv >= a_lo or tv < a_hi:
            return "ASIAN_SESSION_CHOP"
    else:
        if a_lo <= tv < a_hi:
            return "ASIAN_SESSION_CHOP"
    return "LOW_LIQUIDITY_DEADZONE"


def session_blocks_automation(session: str, blocked: list | tuple | None = None) -> bool:
    blocked = blocked or _DEFAULT_BLOCKED
    return session in set(blocked)
