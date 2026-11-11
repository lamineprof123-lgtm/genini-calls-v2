"""Server-side dynamic stop / take-profit for Project Blue Bird scalp framework.

Source of truth for stop_loss and take_profit on both the public signals site and
the Lucid paper sim. Gemini supplies direction + confidence (+ rationale); levels
are computed here from the signal candle, ATR, S/R, session H/L, and Volume Profile.
"""
from __future__ import annotations
import math
from typing import Any


def _cfg(cfg: dict) -> dict:
    return cfg.get("scalp") or {}


def fixed_target_distance(symbol: str, cfg: dict) -> float:
    """Price-unit distance for the fixed tactical scalp target (midpoint of user ranges)."""
    sc = _cfg(cfg).get("fixed_target") or {}
    row = sc.get(symbol) or {}
    if "price" in row:
        return float(row["price"])
    if "ticks" in row and "tick_size" in row:
        return float(row["ticks"]) * float(row["tick_size"])
    if "pips" in row and "pip_size" in row:
        return float(row["pips"]) * float(row["pip_size"])
    # Sensible fallbacks matching the midpoint of the user ranges
    defaults = {
        "US100": 20 * 0.25,       # 20 ticks
        "XAUUSD": 12.5 * 0.10,    # 12.5 gold pips
        "EURUSD": 12.5 * 0.0001,  # 12.5 FX pips
        "GBPUSD": 12.5 * 0.0001,
        "BTCUSD": 20 * 5.0,       # public-site only; not a Lucid product
    }
    return float(defaults.get(symbol, 0.0))


def _round(px: float, digits: int) -> float:
    return round(float(px), digits)


def compute_stop(
    side: int,
    entry: float,
    candle_high: float,
    candle_low: float,
    atr: float,
    cfg: dict,
) -> dict:
    """Stop just beyond the signal candle's opposing side, or 0.8x ATR — tighter of the two
    that still sits beyond the structure, with a minimum ATR floor.

    side: +1 BUY, -1 SELL.
    """
    sc = _cfg(cfg)
    atr = float(atr or 0.0)
    buffer = float(sc.get("structure_buffer_atr", 0.05)) * atr
    atr_mult = float(sc.get("atr_stop_mult", 0.8))
    min_atr = float(sc.get("min_stop_atr", 0.25))
    entry = float(entry)

    if side > 0:  # BUY: stop below
        structure = float(candle_low) - buffer
        atr_stop = entry - atr_mult * atr
        # Must sit at/beyond structure (at or below structure)
        cands = [s for s in (structure, atr_stop) if s <= structure + 1e-12]
        chosen = max(cands) if cands else structure  # tighter = closer to entry = higher
        source = "structure" if abs(chosen - structure) <= abs(chosen - atr_stop) else "atr_0.8"
        if cands and atr_stop in cands and atr_stop >= chosen - 1e-12 and abs(atr_stop - chosen) < 1e-12:
            source = "atr_0.8" if abs(atr_stop - structure) > 1e-12 else "structure"
        if cands:
            # Prefer labeling by which candidate won
            if abs(chosen - atr_stop) < abs(chosen - structure):
                source = "atr_0.8"
            else:
                source = "structure"
        dist = entry - chosen
        if atr > 0 and dist < min_atr * atr:
            dist = min_atr * atr
            chosen = entry - dist
            source = f"{source}+min_floor"
        return {"stop": chosen, "sdist": dist, "source": source,
                "structure_stop": structure, "atr_stop": atr_stop}
    else:  # SELL: stop above
        structure = float(candle_high) + buffer
        atr_stop = entry + atr_mult * atr
        cands = [s for s in (structure, atr_stop) if s >= structure - 1e-12]
        chosen = min(cands) if cands else structure  # tighter = closer to entry = lower
        if cands:
            if abs(chosen - atr_stop) < abs(chosen - structure):
                source = "atr_0.8"
            else:
                source = "structure"
        else:
            source = "structure"
        dist = chosen - entry
        if atr > 0 and dist < min_atr * atr:
            dist = min_atr * atr
            chosen = entry + dist
            source = f"{source}+min_floor"
        return {"stop": chosen, "sdist": dist, "source": source,
                "structure_stop": structure, "atr_stop": atr_stop}


def _structural_targets(side: int, entry: float, levels: list[dict] | None,
                        vp: dict | None) -> list[tuple[float, str]]:
    """Price levels in the trade direction that can act as liquidity / structural targets."""
    out: list[tuple[float, str]] = []
    for lv in levels or []:
        px = lv.get("price")
        if px is None:
            continue
        px = float(px)
        label = str(lv.get("label") or lv.get("type") or "S/R")
        if side > 0 and px > entry:
            out.append((px, label))
        elif side < 0 and px < entry:
            out.append((px, label))
    if vp:
        for key, label in (("poc", "POC"), ("vah", "VAH"), ("val", "VAL")):
            if vp.get(key) is None:
                continue
            px = float(vp[key])
            if side > 0 and px > entry:
                out.append((px, label))
            elif side < 0 and px < entry:
                out.append((px, label))
    # nearest first
    out.sort(key=lambda x: abs(x[0] - entry))
    return out


def compute_target(
    side: int,
    entry: float,
    symbol: str,
    atr: float,
    levels: list[dict] | None,
    vp: dict | None,
    cfg: dict,
    tick: float | None = None,
) -> dict:
    """Nearer of the fixed tactical scalp target and the next structural level (just before it)."""
    sc = _cfg(cfg)
    entry = float(entry)
    fixed_dist = fixed_target_distance(symbol, cfg)
    fixed_tp = entry + side * fixed_dist
    before = float(sc.get("tp_before_level_ticks", 1))
    tick = float(tick or sc.get("default_tick") or 0.0)
    # Infer a tiny before-buffer from ATR if no tick given
    before_buf = before * tick if tick > 0 else max(fixed_dist * 0.02, (atr or 0) * 0.02)

    struct = _structural_targets(side, entry, levels, vp)
    struct_tp = None
    struct_label = None
    if struct:
        lvl, struct_label = struct[0]
        struct_tp = lvl - side * before_buf  # just before the wall

    # Nearer of the two (in the trade direction)
    candidates: list[tuple[float, str]] = [("fixed", fixed_tp)]
    if struct_tp is not None and side * (struct_tp - entry) > 0:
        candidates.append((f"structure:{struct_label}", struct_tp))

    # Pick nearer to entry
    best_name, best_px = min(candidates, key=lambda c: abs(c[1] - entry))
    # Guard: never pick a target on the wrong side
    if side * (best_px - entry) <= 0:
        best_name, best_px = "fixed", fixed_tp
    tdist = abs(best_px - entry)
    # Floor: at least a tiny positive distance
    if tdist <= 0:
        tdist = fixed_dist or (atr or 1) * 0.25
        best_px = entry + side * tdist
        best_name = "fixed_fallback"
    return {
        "target": best_px,
        "tdist": tdist,
        "source": best_name,
        "fixed_tp": fixed_tp,
        "fixed_dist": fixed_dist,
        "structure_tp": struct_tp,
        "structure_label": struct_label,
    }


def compute_scalp_levels(
    action: str,
    entry: float,
    candle: dict,
    atr: float,
    symbol: str,
    cfg: dict,
    levels: list[dict] | None = None,
    vp: dict | None = None,
    tick: float | None = None,
    digits: int = 5,
) -> dict | None:
    """Full stop + target package. action is BUY/SELL. Returns None if inputs are unusable."""
    action = (action or "").upper()
    if action not in ("BUY", "SELL"):
        return None
    side = 1 if action == "BUY" else -1
    try:
        hi = float(candle["high"])
        lo = float(candle["low"])
        entry = float(entry)
        atr = float(atr)
    except (TypeError, ValueError, KeyError):
        return None
    if not math.isfinite(entry) or not math.isfinite(atr) or atr < 0:
        return None
    stop = compute_stop(side, entry, hi, lo, atr, cfg)
    tgt = compute_target(side, entry, symbol, atr, levels, vp, cfg, tick=tick)
    rr = (tgt["tdist"] / stop["sdist"]) if stop["sdist"] > 0 else None
    return {
        "action": action,
        "side": side,
        "entry": _round(entry, digits),
        "stop_loss": _round(stop["stop"], digits),
        "take_profit": _round(tgt["target"], digits),
        "sdist": stop["sdist"],
        "tdist": tgt["tdist"],
        "stop_source": stop["source"],
        "target_source": tgt["source"],
        "rr": round(rr, 3) if rr is not None else None,
        "structure_stop": stop["structure_stop"],
        "atr_stop": stop["atr_stop"],
        "fixed_tp": tgt["fixed_tp"],
        "structure_tp": tgt.get("structure_tp"),
        "structure_label": tgt.get("structure_label"),
        "candle": {"high": hi, "low": lo},
    }


def apply_server_levels(sig: dict, ctx: dict, cfg: dict, symbol: str) -> dict:
    """Overwrite sig stop/target (and optionally entry) with server-side scalp levels.

    Keeps Gemini action/confidence/rationale. Mutates and returns sig.
    """
    if not (_cfg(cfg).get("enabled", True)):
        return sig
    action = str(sig.get("action") or "").upper()
    if action not in ("BUY", "SELL"):
        return sig
    candle = ctx.get("signal_candle") or ctx.get("last_candle") or {}
    if not candle:
        # Fall back to last close only — structure stop collapses to a buffer under/over close
        lc = float(ctx.get("last_close") or sig.get("entry") or 0)
        candle = {"high": lc, "low": lc, "close": lc, "open": lc}
    entry = sig.get("entry")
    if entry is None:
        entry = ctx.get("last_close")
    # Prefer last_close as market entry when Gemini entry is missing/far — caller may already clamp
    atr = float(ctx.get("atr") or 0)
    levels = ctx.get("levels") or []
    vp = {k: ctx.get(k) for k in ("poc", "vah", "val") if ctx.get(k) is not None}
    digits = int(ctx.get("digits") or cfg.get("symbols", {}).get(symbol, {}).get("digits", 5))
    tick = ctx.get("tick")
    pkg = compute_scalp_levels(action, float(entry), candle, atr, symbol, cfg,
                               levels=levels, vp=vp or None, tick=tick, digits=digits)
    if not pkg:
        return sig
    sig["entry"] = pkg["entry"]
    sig["stop_loss"] = pkg["stop_loss"]
    sig["take_profit"] = pkg["take_profit"]
    sig["risk_reward"] = pkg["rr"]
    sig["scalp"] = pkg
    sig["levels_source"] = "server_scalp"
    return sig


def size_for_target(
    *,
    point_value: float,
    tick: float,
    commission: float,
    tdist: float,
    sdist: float,
    max_qty: int,
    net_min: float,
    net_aim: float,
    net_max: float,
    max_risk_usd: float,
    budget: float,
    micro: bool = False,
    micros_per_mini: int = 10,
) -> dict | None:
    """Ceil qty so NET of RT commission at ``tdist`` is >= net_min, capped by max_qty / risk.

    Returns None when $net_min is unreachable within the cap (caller should SKIP).
    """
    if tdist <= 0 or sdist <= 0 or max_qty < 1 or point_value <= 0:
        return None
    unit_gross = tdist * point_value
    unit_comm_rt = 2.0 * commission
    unit_net = unit_gross - unit_comm_rt
    if unit_net <= 0:
        return None

    def q_for(net: float) -> int:
        return int(math.ceil(net / unit_net - 1e-12))

    q_min = q_for(net_min)
    q_aim = q_for(net_aim)
    q = min(max_qty, max(q_min, min(q_aim, max_qty)))
    # Shrink for risk budget / max_risk
    while q >= q_min:
        risk = sdist * q * point_value
        comm_rt = unit_comm_rt * q
        if risk <= max_risk_usd + 1e-9 and risk + comm_rt <= budget + 1e-9:
            break
        q -= 1
    if q < q_min or q < 1:
        return None
    actual_gross = tdist * q * point_value
    comm_rt = unit_comm_rt * q
    actual_net = actual_gross - comm_rt
    if actual_net + 1e-9 < net_min:
        return None
    if actual_net > net_max + 1.0 and q > q_min:
        # Prefer not overshooting max too far: step down toward min while staying >= net_min
        while q > q_min:
            trial_net = tdist * (q - 1) * point_value - unit_comm_rt * (q - 1)
            if trial_net + 1e-9 < net_min:
                break
            risk = sdist * (q - 1) * point_value
            if risk > max_risk_usd:
                break
            q -= 1
            actual_net = trial_net
            actual_gross = tdist * q * point_value
            comm_rt = unit_comm_rt * q
    risk_usd = round(sdist * q * point_value, 2)
    return {
        "qty": q,
        "tdist": tdist,
        "sdist": sdist,
        "target_usd": round(actual_net, 2),
        "target_usd_gross": round(actual_gross, 2),
        "risk_usd": risk_usd,
        "commission_rt": round(comm_rt, 2),
        "unit_net": round(unit_net, 4),
    }
