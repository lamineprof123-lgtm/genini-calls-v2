"""Entry-confluence telemetry snapshots for the Lucid paper sim.

Built from the same indicator context the chart header shows (POC/VAH/VAL, CVD,
session, VWAP, EMA50, RVOL, ATR bands, S/R). Attached to accepted pendings,
carried through fill -> close, and written on every entry_skipped event.
"""
from __future__ import annotations
from typing import Any


def _f(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        return v if v == v else default  # NaN guard
    except (TypeError, ValueError):
        return default


def _side_from(action_or_side) -> int | None:
    if action_or_side in (1, -1):
        return int(action_or_side)
    a = str(action_or_side or "").upper()
    if a in ("BUY", "LONG"):
        return 1
    if a in ("SELL", "SHORT"):
        return -1
    return None


def _vs_level(price: float | None, level: float | None, atr: float | None) -> dict:
    """above/below + signed distance in ATR (positive = price above level)."""
    p, lv, a = _f(price), _f(level), _f(atr)
    if p is None or lv is None:
        return {"side": None, "dist_atr": None}
    side = "above" if p > lv else ("below" if p < lv else "at")
    dist = None if not a or a <= 0 else round((p - lv) / a, 4)
    return {"side": side, "dist_atr": dist}


def _price_vs_value_area(price, vah, val, existing=None) -> str | None:
    if existing in ("above_vah", "inside", "below_val"):
        return existing
    p, hi, lo = _f(price), _f(vah), _f(val)
    if p is None or hi is None or lo is None:
        return None
    if p > hi:
        return "above_vah"
    if p < lo:
        return "below_val"
    return "inside"


def _atr_band_z(price, mid, atr, mult: float = 2.0) -> float | None:
    """z within +/- ``mult`` ATR bands: (price-mid)/atr. Bands sit at +/-mult."""
    p, m, a = _f(price), _f(mid), _f(atr)
    if p is None or m is None or not a or a <= 0:
        return None
    return round((p - m) / a, 4)


def _nearest_sr(levels: list | None, price: float | None, atr: float | None) -> dict:
    """Nearest resistance above and support below, distances in ATR."""
    p, a = _f(price), _f(atr)
    out = {"above": None, "below": None}
    if p is None or not levels:
        return out
    above, below = None, None
    for lv in levels:
        try:
            px = float(lv.get("price"))
        except (TypeError, ValueError, AttributeError):
            continue
        label = lv.get("label") or lv.get("type")
        if px > p and (above is None or px < above["price"]):
            above = {"price": px, "label": label,
                     "dist_atr": None if not a or a <= 0 else round((px - p) / a, 4)}
        if px < p and (below is None or px > below["price"]):
            below = {"price": px, "label": label,
                     "dist_atr": None if not a or a <= 0 else round((p - px) / a, 4)}
    out["above"], out["below"] = above, below
    return out


def _cvd_agrees(side: int | None, cvd_label: str | None, cvd_slope: float | None) -> bool | None:
    if side is None:
        return None
    lab = (cvd_label or "").upper()
    slope = _f(cvd_slope, 0.0) or 0.0
    if side > 0:
        if lab == "BULLISH":
            return True
        if lab == "BEARISH":
            return False
        return slope > 0
    if lab == "BEARISH":
        return True
    if lab == "BULLISH":
        return False
    return slope < 0


def _conf_band(c: float | None) -> str | None:
    if c is None:
        return None
    if c < 0.6:
        return "<0.6"
    if c < 0.7:
        return "0.6-0.7"
    if c < 0.8:
        return "0.7-0.8"
    return ">=0.8"


def bars_since_rvol_spike(rvol_series, threshold: float = 2.0) -> int | None:
    """Bars since the most recent RVOL > threshold (0 = last bar is a spike). None if never."""
    if rvol_series is None:
        return None
    try:
        vals = list(rvol_series)
    except TypeError:
        return None
    for i, v in enumerate(reversed(vals)):
        try:
            if v is not None and float(v) == float(v) and float(v) > threshold:
                return i
        except (TypeError, ValueError):
            continue
    return None


def build_entry_snapshot(
    *,
    symbol: str,
    side: int | str | None,
    signal: dict | None = None,
    ctx: dict | None = None,
    scalp: dict | None = None,
    size: dict | None = None,
    record: dict | None = None,
    chart_path: str | None = None,
    run_at_utc: str | None = None,
) -> dict[str, Any]:
    """Full confluence telemetry at the signal bar. Safe with partial inputs (tests / skips)."""
    sig = signal or {}
    ctx = ctx or {}
    scalp = scalp or sig.get("scalp") or {}
    size = size or {}
    record = record or {}
    side_i = _side_from(side if side is not None else sig.get("action") or sig.get("model_action"))
    action = ("BUY" if side_i == 1 else "SELL" if side_i == -1
              else (sig.get("action") or sig.get("model_action")))

    last_close = _f(sig.get("last_close") if sig.get("last_close") is not None else ctx.get("last_close"))
    atr_v = _f(sig.get("atr") if sig.get("atr") is not None else ctx.get("atr"))
    vwap_v = _f(ctx.get("vwap") if ctx.get("vwap") is not None else sig.get("vwap"))
    ema20_v = _f(ctx.get("ema20_baseline") if ctx.get("ema20_baseline") is not None else ctx.get("ema20"))
    ema50_v = _f(ctx.get("ema50") if ctx.get("ema50") is not None else sig.get("ema50"))
    poc = _f(sig.get("poc") if sig.get("poc") is not None else ctx.get("poc"))
    vah = _f(sig.get("vah") if sig.get("vah") is not None else ctx.get("vah"))
    val = _f(sig.get("val") if sig.get("val") is not None else ctx.get("val"))
    cvd = _f(sig.get("cvd") if sig.get("cvd") is not None else ctx.get("cvd"))
    cvd_label = sig.get("cvd_label") if sig.get("cvd_label") is not None else ctx.get("cvd_label")
    cvd_slope = _f(sig.get("cvd_slope") if sig.get("cvd_slope") is not None else ctx.get("cvd_slope"))
    session = sig.get("session") or ctx.get("session")
    rvol = _f(ctx.get("rvol_last") if ctx.get("rvol_last") is not None else sig.get("rvol_last") or sig.get("rvol"))
    rvol_thr = 2.0
    rvol_spike = bool(rvol is not None and rvol > rvol_thr)
    bars_since = ctx.get("rvol_bars_since_spike")
    if bars_since is None and ctx.get("rvol_series") is not None:
        bars_since = bars_since_rvol_spike(ctx["rvol_series"], rvol_thr)

    vs_vwap = _vs_level(last_close, vwap_v, atr_v)
    vs_ema50 = _vs_level(last_close, ema50_v, atr_v)
    # Prefer chart's own label when present
    if ctx.get("close_vs_vwap") in ("above", "below", "at"):
        vs_vwap["side"] = ctx["close_vs_vwap"]
    va_pos = _price_vs_value_area(last_close, vah, val, ctx.get("price_vs_value_area"))
    dist_poc = None
    if last_close is not None and poc is not None and atr_v and atr_v > 0:
        dist_poc = round((last_close - poc) / atr_v, 4)

    atr_upper = _f(ctx.get("atr_upper"))
    atr_lower = _f(ctx.get("atr_lower"))
    mid = ema20_v
    if mid is None and atr_upper is not None and atr_lower is not None:
        mid = (atr_upper + atr_lower) / 2.0
    band_z = _atr_band_z(last_close, mid, atr_v, mult=2.0)

    levels = ctx.get("levels") or sig.get("levels") or []
    nearest = _nearest_sr(levels, last_close, atr_v)

    conf = _f(sig.get("confidence"))
    stop_basis = scalp.get("stop_source")  # "structure" | "atr_0.8" etc.
    tgt_basis = scalp.get("target_source")  # "fixed" | "structure:..."
    tdist = _f(scalp.get("tdist") if scalp.get("tdist") is not None else size.get("tdist"))
    sdist = _f(scalp.get("sdist") if scalp.get("sdist") is not None else size.get("sdist"))

    # Side alignment helpers used by the report
    vwap_aligned = None
    if side_i is not None and vs_vwap["side"] in ("above", "below"):
        vwap_aligned = (side_i > 0 and vs_vwap["side"] == "above") or (side_i < 0 and vs_vwap["side"] == "below")
    ema50_aligned = None
    if side_i is not None and vs_ema50["side"] in ("above", "below"):
        ema50_aligned = (side_i > 0 and vs_ema50["side"] == "above") or (side_i < 0 and vs_ema50["side"] == "below")

    chart = chart_path
    if chart is None:
        rd = record.get("run_dir")
        if rd:
            chart = f"{rd}/{symbol}.png"

    return {
        "symbol": symbol,
        "session": session,
        "last_close": last_close,
        "atr": atr_v,
        "vwap": vwap_v,
        "ema20": ema20_v,
        "ema50": ema50_v,
        "price_vs_vwap": vs_vwap,
        "price_vs_ema50": vs_ema50,
        "vwap_aligned_with_side": vwap_aligned,
        "ema50_aligned_with_side": ema50_aligned,
        "poc": poc,
        "vah": vah,
        "val": val,
        "price_vs_value_area": va_pos,
        "dist_to_poc_atr": dist_poc,
        "cvd": cvd,
        "cvd_label": cvd_label,
        "cvd_slope": cvd_slope,
        "cvd_agrees_with_side": _cvd_agrees(side_i, cvd_label, cvd_slope),
        "rvol": rvol,
        "rvol_spike": rvol_spike,
        "rvol_bars_since_spike": bars_since,
        "atr_band_z": band_z,  # within +/-2 ATR of EMA20 mid
        "atr_upper": atr_upper,
        "atr_lower": atr_lower,
        "nearest_sr": nearest,
        "gemini": {
            "action": action,
            "confidence": conf,
            "confidence_band": _conf_band(conf),
            "rationale": sig.get("rationale") or sig.get("reason"),
            "model": record.get("model") or sig.get("model"),
            "model_action": sig.get("model_action"),
        },
        "scalp": {
            "stop_basis": stop_basis,
            "target_basis": tgt_basis,
            "tdist": tdist,
            "sdist": sdist,
            "structure_label": scalp.get("structure_label"),
        },
        "planned": {
            "tdist": tdist,
            "sdist": sdist,
            "qty": size.get("qty"),
            "contract": size.get("contract"),
            "target_usd_net": size.get("target_usd"),
            "risk_usd": size.get("risk_usd"),
        },
        "run_at_utc": run_at_utc or record.get("run_at_utc") or sig.get("run_at_utc"),
        "chart_path": chart,
    }


def enrich_close_metrics(snapshot: dict | None, *, mfe_pts: float, mae_pts: float,
                         atr: float | None, point_value_total: float,
                         hold_seconds: float) -> dict:
    """Attach MFE/MAE ($ and ATR) and bars-to-exit onto a copy of the entry snapshot."""
    snap = dict(snapshot or {})
    atr_v = _f(atr) or _f((snapshot or {}).get("atr"))
    pv = float(point_value_total or 0.0)
    mfe_pts = float(mfe_pts or 0.0)
    mae_pts = float(mae_pts or 0.0)
    snap["mfe_pts"] = round(mfe_pts, 6)
    snap["mae_pts"] = round(mae_pts, 6)
    snap["mfe_usd"] = round(mfe_pts * pv, 2)
    snap["mae_usd"] = round(mae_pts * pv, 2)
    snap["mfe_atr"] = round(mfe_pts / atr_v, 4) if atr_v and atr_v > 0 else None
    snap["mae_atr"] = round(mae_pts / atr_v, 4) if atr_v and atr_v > 0 else None
    hold = int(hold_seconds or 0)
    snap["hold_seconds"] = hold
    snap["bars_to_exit_m1"] = hold // 60
    snap["bars_to_exit_m15"] = hold // (15 * 60)
    return snap
