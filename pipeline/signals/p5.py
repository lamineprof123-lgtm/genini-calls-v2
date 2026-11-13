"""Prompt P5 + server gates + event triggers (user t299u-t301u, 13 Nov 2026; Gemini "P5 spec" PDF). PAPER ONLY.

Used by the Gemini low-thinking side books G-LOW-80, EVAL-80, EVAL-R4, G-P5 and G-P5-EVENT (one shared
gemini-3.8-flash call per M15 close +20 s, made by G-LOW-80). MAIN, 1-strike, nostop, bracket, Carbon, Claude,
C-OPUS-B and DS-FLASH do NOT use anything in this module (MAIN keeps P4 / high thinking / 1 ATR bracket).

  Prompt   templates/system_prompt_p5.j2 (PDF section 4, drop-in). Fixes P4's false "~15-25 ticks / ~10-15 pips
           scalp target" text: the P5 books really use a 0.8 ATR stop / 1.0 ATR target / 90-min cap.
  Fields   five numeric-context fields added to the per-symbol JSON sent to Gemini (PDF 3.2), computed from the
           CLOSED M15 bars of the shared snapshot (+ an H1 history for the H1 spread):
             atr_ratio_vs_50bar      ATR14 now / mean of the last 50 ATR14 values
             dist_from_ema20_atr     |live - M15 EMA20| / ATR
             last_3bars_move_atr     |last close - close 3 bars earlier| / ATR
             h1_ema20_50_spread_atr  signed (H1 EMA20 - H1 EMA50) / M15 ATR (closed H1 bars)
             room_to_nearest_wall_atr  {"buy": room up, "sell": room down}: nearest opposing S/R / Session H/L /
                                     Prev / 24h level or GEX Call / Put Wall #1, in ATR (null = no level that side).
                                     The direction is unknown before Gemini answers, so both sides are sent.
  Gates    validate_entry_gates (PDF 3.3 / 5), confidence handled per book:
             anti-stretch: side*(live-EMA20)/ATR >= 2.40, side*(last close - close 3 ago)/ATR >= 2.20, or
                           side*(live-VWAP)/ATR >= 3.50 -> reject (PDF 3.3 prose; the reference code's extra
                           "and EMA20 stretch > 1.0" on the 3-bar rule is not used)
             room-to-wall: nearest opposing wall < 0.85 ATR -> reject unless atr_ratio_vs_50bar >= 1.30 AND
                           rvol_last >= 2.0 (PDF prose; the reference code's atr_expanding_vs_20bar_avg is not used)
             exhaustion:   signal candle range >= 1.80 ATR and the trade is in the candle's direction -> reject
                           unless live price pulled back >= 25% of the candle range from its extreme. Trades against
                           the candle (reversal / sweep rejection) are exempt.
  Event    G-P5-EVENT trigger (t298u/t299u), evaluated on the bar that just closed: price within 0.25 ATR of a
           key level (Session High/Low, VAH / VAL / POC, FirmTape GEX call/put walls), or RVOL >= 2.0, or
           atr_ratio_vs_50bar >= 1.30.
"""
from __future__ import annotations
import logging, math

import numpy as np
import pandas as pd

log = logging.getLogger("signals.p5")

PROMPT_TEMPLATE = "system_prompt_p5.j2"
PROMPT_VERSION = "p5"
FIELD_KEYS = ("atr_ratio_vs_50bar", "dist_from_ema20_atr", "last_3bars_move_atr", "h1_ema20_50_spread_atr",
              "room_to_nearest_wall_atr")
P5_SYMBOLS = ["XAUUSD", "EURUSD", "US100", "XAGUSD"]     # PDF 3.1: no GBPUSD, no BTCUSD

GATE_DEFAULTS = {
    "max_ema20_stretch_atr": 2.40,
    "max_last3_atr": 2.20,
    "max_vwap_stretch_atr": 3.50,
    "min_room_atr": 0.85,
    "room_override_atr_ratio": 1.30,
    "room_override_rvol": 2.0,
    "exhaustion_range_atr": 1.80,
    "exhaustion_pullback_frac": 0.25,
}
TRIGGER_DEFAULTS = {"level_atr": 0.25, "rvol_min": 2.0, "atr_ratio_min": 1.30}


def _f(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def candles_frame(payload: dict) -> pd.DataFrame | None:
    cs = payload.get("candles") or []
    if not cs:
        return None
    df = pd.DataFrame([{k: c[k] for k in ("time", "open", "high", "low", "close")} for c in cs])
    df.index = pd.to_datetime(df.pop("time"), unit="s", utc=True)
    return df.astype(float).sort_index()


def atr_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df.high, df.low, df.close
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def h1_spread(hist: pd.DataFrame | None, t_close) -> tuple[float | None, str | None]:
    """(H1 EMA20 - H1 EMA50) on CLOSED H1 bars of an M15 history (bars indexed by open time), or None."""
    if hist is None or not len(hist):
        return None, None
    ts = pd.Timestamp(t_close)
    d = hist[hist.index + pd.Timedelta(minutes=15) <= ts]
    hc = d.close.resample("1h", label="left", closed="left").last().dropna()
    hc.index = hc.index + pd.Timedelta(hours=1)
    hc = hc[hc.index <= ts]
    if len(hc) < 20:
        return None, None
    e20 = hc.ewm(span=20, adjust=False).mean().iloc[-1]
    e50 = hc.ewm(span=50, adjust=False).mean().iloc[-1]
    return float(e20 - e50), f"{len(hc)} closed H1 bars"


def _opposing(ctx: dict, price: float, side: int) -> list[tuple[float, str]]:
    out = []
    for lv in ctx.get("levels") or []:
        p = _f(lv.get("price"))
        if p is not None and side * (p - price) > 0:
            out.append((p, str(lv.get("label") or lv.get("type") or "level")))
    gex = ctx.get("gex") or {}
    for w in gex.get("call_walls" if side > 0 else "put_walls") or []:
        p = _f(w.get("price"))
        if p is not None and int(w.get("rank") or 0) == 1 and side * (p - price) > 0:
            out.append((p, "GEX Call Wall #1" if side > 0 else "GEX Put Wall #1"))
    return out


def room_to_wall(ctx: dict, price: float, atr: float, side: int) -> tuple[float | None, str | None]:
    ops = _opposing(ctx, price, side)
    if not ops or not atr:
        return None, None
    p, lab = min(ops, key=lambda x: abs(x[0] - price))
    return abs(p - price) / atr, lab


def compute_fields(payload: dict, hist: pd.DataFrame | None = None) -> dict:
    """The five P5 fields (sent to Gemini) + `_p5` internals (signed values, sources; NOT sent)."""
    ctx = payload.get("context") or {}
    atr = _f(ctx.get("atr"))
    live = _f(ctx.get("live_price")) if not ctx.get("live_price_stale") else None
    live = live if live is not None else _f(ctx.get("last_close") or ctx.get("last_price"))
    out = {k: None for k in FIELD_KEYS}
    internal = {"price": live, "atr": atr}
    df = candles_frame(payload)
    if not atr or atr <= 0 or live is None or df is None or len(df) < 20:
        out["_p5"] = {**internal, "error": "no ATR / price / candles"}
        return out
    a = atr_series(df).dropna()
    last50 = a.iloc[-50:]
    ratio = atr / float(last50.mean()) if len(last50) and float(last50.mean()) > 0 else None
    ema20 = _f(ctx.get("ema20_baseline"))
    if ema20 is None:
        ema20 = float(df.close.ewm(span=20, adjust=False).mean().iloc[-1])
    last_close = float(df.close.iloc[-1])
    close3 = float(df.close.iloc[-4]) if len(df) >= 4 else None
    move3 = (last_close - close3) / atr if close3 is not None else None
    t_close = df.index[-1] + pd.Timedelta(minutes=15)
    spread, h1_src = h1_spread(hist, t_close)
    if spread is None:      # fallback: the snapshot's own ~30 H1 bars (EMA50 only partly warmed)
        spread, h1_src = h1_spread(df, t_close)
        h1_src = f"snapshot only ({h1_src})" if spread is not None else None
    up, up_lab = room_to_wall(ctx, live, atr, 1)
    dn, dn_lab = room_to_wall(ctx, live, atr, -1)
    out.update({
        "atr_ratio_vs_50bar": _r(ratio),
        "dist_from_ema20_atr": _r(abs(live - ema20) / atr),
        "last_3bars_move_atr": _r(abs(move3)) if move3 is not None else None,
        "h1_ema20_50_spread_atr": _r(spread / atr) if spread is not None else None,
        "room_to_nearest_wall_atr": {"buy": _r(up), "sell": _r(dn)},
    })
    vwap = _f(ctx.get("vwap"))
    internal.update({"ema20": ema20, "vwap": vwap, "ema20_signed_atr": (live - ema20) / atr,
                     "vwap_signed_atr": (live - vwap) / atr if vwap is not None else None,
                     "last3_signed_atr": move3, "h1_source": h1_src,
                     "wall_buy": up_lab, "wall_sell": dn_lab})
    out["_p5"] = internal
    return out


def public_fields(fields: dict) -> dict:
    return {k: fields.get(k) for k in FIELD_KEYS}


def validate_entry_gates(action: str, ctx: dict, fields: dict, g: dict | None = None) -> dict:
    """P5 server gates for a BUY / SELL (confidence is checked by each book). Returns
    {"pass": bool, "reason": str|None, "checks": {name: {"pass", "value", "threshold", "reason"}}}."""
    g = {**GATE_DEFAULTS, **(g or {})}
    res = {"pass": False, "reason": None, "checks": {}}
    if action not in ("BUY", "SELL"):
        res["reason"] = "hold"
        return res
    side = 1 if action == "BUY" else -1
    i = (fields or {}).get("_p5") or {}
    atr, live = _f(i.get("atr")), _f(i.get("price"))
    if not atr or atr <= 0 or live is None:
        res["reason"] = "invalid_atr_or_price"
        return res
    ch = res["checks"]

    def put(name, ok, value, thr, why):
        ch[name] = {"pass": bool(ok), "value": _r(value, 3), "threshold": thr, "reason": None if ok else why}

    e = side * float(i.get("ema20_signed_atr") or 0.0)
    put("stretch_ema20", e < g["max_ema20_stretch_atr"], e, g["max_ema20_stretch_atr"],
        f"stretched_vs_ema20 ({e:.2f} ATR >= {g['max_ema20_stretch_atr']:.2f})")
    vw = i.get("vwap_signed_atr")
    v = side * float(vw) if vw is not None else 0.0
    put("stretch_vwap", v < g["max_vwap_stretch_atr"], v, g["max_vwap_stretch_atr"],
        f"stretched_vs_vwap ({v:.2f} ATR >= {g['max_vwap_stretch_atr']:.2f})")
    m3 = i.get("last3_signed_atr")
    m = side * float(m3) if m3 is not None else 0.0
    put("chase_3bar", m < g["max_last3_atr"], m, g["max_last3_atr"],
        f"chasing_3bar_move ({m:.2f} ATR >= {g['max_last3_atr']:.2f})")
    room = ((fields or {}).get("room_to_nearest_wall_atr") or {}).get("buy" if side > 0 else "sell")
    rvol = _f(ctx.get("rvol_last")) or 0.0
    ratio = _f((fields or {}).get("atr_ratio_vs_50bar")) or 0.0
    override = ratio >= g["room_override_atr_ratio"] and rvol >= g["room_override_rvol"]
    ok = room is None or room >= g["min_room_atr"] or override
    wall = i.get("wall_buy" if side > 0 else "wall_sell")
    put("room_to_wall", ok, room, g["min_room_atr"],
        f"wall_too_close ({room:.2f} ATR < {g['min_room_atr']:.2f} to {wall})" if room is not None else None)
    if override and room is not None and room < g["min_room_atr"]:
        ch["room_to_wall"]["override"] = f"atr_ratio {ratio:.2f} >= {g['room_override_atr_ratio']} and rvol {rvol:.2f} >= {g['room_override_rvol']}"
    sc = ctx.get("signal_candle") or {}
    o, h, l, c = (_f(sc.get(k)) for k in ("open", "high", "low", "close"))
    if None not in (o, h, l, c) and h > l:
        rng = h - l
        rr = rng / atr
        cdir = 1 if c > o else (-1 if c < o else 0)
        with_candle = cdir == side
        pull = ((h - live) if side > 0 else (live - l)) / rng
        exhausted = rr >= g["exhaustion_range_atr"] and with_candle and pull < g["exhaustion_pullback_frac"]
        put("exhaustion", not exhausted, rr, g["exhaustion_range_atr"],
            f"signal_candle_exhaustion (range {rr:.2f} ATR, pullback {100 * pull:.0f}% < "
            f"{100 * g['exhaustion_pullback_frac']:.0f}%)")
        ch["exhaustion"].update({"with_candle": with_candle, "pullback_frac": _r(pull, 3)})
    fails = [x["reason"] for x in ch.values() if not x["pass"]]
    res.update({"pass": not fails, "reason": "; ".join(fails) or None})
    return res


def key_levels(ctx: dict, exclude=()) -> list[tuple[float, str]]:
    """Session High/Low, VAH / VAL / POC and FirmTape GEX walls (the G-P5-EVENT level list)."""
    out = []
    for lv in ctx.get("levels") or []:
        lab = str(lv.get("label") or "")
        p = _f(lv.get("price"))
        if p is not None and lab.lower().startswith("session"):
            out.append((p, lab))
    for k in ("vah", "val", "poc"):
        if k.upper() in {str(x).upper() for x in exclude}:
            continue
        p = _f(ctx.get(k))
        if p is not None:
            out.append((p, k.upper()))
    gex = ctx.get("gex") or {}
    for side in ("call_walls", "put_walls"):
        for w in gex.get(side) or []:
            p = _f(w.get("price"))
            if p is not None:
                out.append((p, f"GEX {'Call' if side == 'call_walls' else 'Put'} Wall #{w.get('rank')}"))
    return out


def event_trigger(payload: dict, fields: dict, t: dict | None = None) -> dict:
    """Did something happen in the bar that just closed? pass/fail per condition (any one fires)."""
    t = {**TRIGGER_DEFAULTS, **(t or {})}
    ctx = payload.get("context") or {}
    atr = _f(ctx.get("atr"))
    sc = ctx.get("signal_candle") or {}
    h, l = _f(sc.get("high")), _f(sc.get("low"))
    conds = {}
    near = None
    if atr and h is not None and l is not None:
        best = None
        for p, lab in key_levels(ctx, t.get("exclude_levels") or ()):
            d = 0.0 if l <= p <= h else min(abs(p - h), abs(p - l))
            if best is None or d < best[0]:
                best = (d, p, lab)
        if best is not None:
            near = {"level": best[2], "price": best[1], "dist_atr": _r(best[0] / atr, 3)}
        ok = best is not None and best[0] <= t["level_atr"] * atr + 1e-12
        conds["level"] = {"pass": bool(ok), "value": near["dist_atr"] if near else None,
                          "threshold": t["level_atr"], "nearest": near}
    else:
        conds["level"] = {"pass": False, "value": None, "threshold": t["level_atr"], "nearest": None,
                          "reason": "no ATR / signal candle"}
    rv = _f(ctx.get("rvol_last"))
    conds["rvol"] = {"pass": rv is not None and rv >= t["rvol_min"], "value": rv, "threshold": t["rvol_min"]}
    ar = _f((fields or {}).get("atr_ratio_vs_50bar"))
    conds["atr_ratio"] = {"pass": ar is not None and ar >= t["atr_ratio_min"], "value": ar,
                          "threshold": t["atr_ratio_min"]}
    fired = [k for k, v in conds.items() if v["pass"]]
    return {"fired": bool(fired), "by": fired, "conds": conds,
            "bar": ctx.get("last_closed_bar_utc"), "session": ctx.get("session")}


def with_fields(payloads: list[dict], fields: dict[str, dict]) -> list[dict]:
    """Copies of the snapshot payloads whose context carries the five P5 fields (other books never see them)."""
    out = []
    for p in payloads:
        f = fields.get(p["symbol"]) or {}
        out.append({**p, "context": {**(p.get("context") or {}), **public_fields(f)}})
    return out
