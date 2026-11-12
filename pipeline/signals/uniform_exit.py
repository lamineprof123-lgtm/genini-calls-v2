"""Shared 1 ATR / 1 ATR / 90-min mechanical exit (t247u) and scoring helpers (t248u). PAPER ONLY.

Every book closes only on a 1 ATR (M15 ATR at entry) stop, a 1 ATR first-touch target, or the 90-min
time cap. Stop has priority when both trade in the same bar. No model exit calls.
"""
from __future__ import annotations
import hashlib
import math
import random
from datetime import datetime, timedelta, timezone

import pandas as pd

UTC = timezone.utc

UNIFORM_EXIT_VERSION = "uniform-1atr-v1"
UNIFORM_BANNER = "All books: 1 ATR / 1 ATR bracket, no model exits"
STOP_ATR = 1.0
TARGET_ATR = 1.0
TIME_CAP_MINUTES = 90
HORIZONS_H = (2, 3, 4)
TWIN_SEED_SALT = "uniform-1atr-v1-twin"


def uniform_exits_enabled(cfg: dict | None) -> bool:
    """Live config sets this true. Default False so older tests keep their previous exit rules."""
    cfg = cfg or {}
    for sec in ("exits", "exit_manager", "lucid"):
        d = cfg.get(sec) or {}
        if "uniform_1atr_exits" in d:
            return bool(d["uniform_1atr_exits"])
    return False


def claude_exit_calls_enabled(cfg: dict | None) -> bool:
    """Reversible switch for Anthropic Haiku exit / shadow-exit API calls. Default True if unset."""
    cfg = cfg or {}
    cl = cfg.get("claude") or {}
    if "exit_calls_enabled" in cl:
        return bool(cl["exit_calls_enabled"])
    if "claude_exit_calls_enabled" in cl:
        return bool(cl["claude_exit_calls_enabled"])
    return True


def uniform_exit_tag() -> str:
    return UNIFORM_EXIT_VERSION


def close_tag(cfg: dict | None = None) -> str:
    """Tag written onto new closes. Uniform bracket wins when that mode is on."""
    if uniform_exits_enabled(cfg):
        return UNIFORM_EXIT_VERSION
    from .exit_versions import gemini_close_tag
    return gemini_close_tag(cfg)


def _round_tick(x: float, tick: float | None) -> float:
    if not tick or tick <= 0:
        return x
    return round(round(x / tick) * tick, 10)


def _round_away(x: float, entry: float, tick: float | None) -> float:
    if not tick or tick <= 0:
        return x
    n = x / tick
    n = math.ceil(n - 1e-9) if x >= entry else math.floor(n + 1e-9)
    return round(n * tick, 10)


def bracket_levels(side: int, entry: float, atr: float, tick: float | None = None,
                   stop_atr: float = STOP_ATR, target_atr: float = TARGET_ATR) -> dict:
    """Stop and target 1 ATR from entry, rounded away from the entry (conservative)."""
    stop = entry - side * stop_atr * atr
    target = entry + side * target_atr * atr
    if tick:
        stop = _round_away(stop, entry, tick)
        target = _round_away(target, entry, tick)
    return {"stop": stop, "target": target, "stop_atr": stop_atr, "target_atr": target_atr}


def arm_uniform(p: dict, now: datetime, *, late: bool = False, tick: float | None = None,
                cap_min: float = TIME_CAP_MINUTES) -> dict:
    """Attach 1 ATR stop + 1 ATR target + 90-min deadline. Late-arm stamps from `now` (not retroactive)."""
    atr = float(p.get("atr") or 0)
    if atr <= 0 or p.get("side") is None or p.get("entry") is None:
        return p
    side = int(p["side"])
    entry = float(p["entry"])
    lv = bracket_levels(side, entry, atr, tick)
    p["stop"] = lv["stop"]
    p["target"] = lv["target"]
    p["stop_note"] = "uniform_1atr"
    p["mech_target_atr"] = lv["target_atr"]
    p["uniform_exit"] = UNIFORM_EXIT_VERSION
    et = p.get("entry_time")
    try:
        entry_t = datetime.fromisoformat(str(et).replace("Z", "+00:00"))
        if entry_t.tzinfo is None:
            entry_t = entry_t.replace(tzinfo=UTC)
    except Exception:
        entry_t = now
    if late:
        p["mech_armed_at"] = now.isoformat()
        p["mech_stop_armed_at"] = now.isoformat()
        p["deadline"] = (now + timedelta(minutes=cap_min)).isoformat()
    else:
        p["deadline"] = (entry_t + timedelta(minutes=cap_min)).isoformat()
        p.pop("mech_armed_at", None)
        p.pop("mech_stop_armed_at", None)
    return p


def _as_ts(t) -> pd.Timestamp:
    if isinstance(t, pd.Timestamp):
        ts = t
    else:
        ts = pd.Timestamp(datetime.fromisoformat(str(t).replace("Z", "+00:00")) if not isinstance(t, datetime)
                          else t)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts


def first_touch(bars: pd.DataFrame | None, *, side: int, entry: float, stop: float, target: float,
                entry_time, armed_at=None, deadline=None, tick: float | None = None,
                slippage_ticks: int = 1) -> dict | None:
    """Walk M1 bars: stop first, then target, then the 90-min cap at the bar close.

    Returns {reason, price, t, mfe, mae} or None if still open through the available bars.
    """
    if bars is None or not len(bars):
        return None
    entered = _as_ts(entry_time)
    start = entered.floor("min")
    if armed_at:
        start = max(start, _as_ts(armed_at).ceil("min"))
    due = _as_ts(deadline) if deadline else entered + pd.Timedelta(minutes=TIME_CAP_MINUTES)
    slip = (slippage_ticks or 0) * (tick or 0)
    mfe = mae = 0.0
    last_close = entry
    last_end = None
    for ts, row in bars[bars.index >= start].iterrows():
        if ts < entered:
            continue
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        fav, adv = (h, l) if side > 0 else (l, h)
        # Stop first (conservative if both trade in the same bar)
        stop_hit = (l <= stop) if side > 0 else (h >= stop)
        if stop_hit:
            gap = (o <= stop) if side > 0 else (o >= stop)
            px = (o if gap else stop) - side * slip
            if tick:
                px = _round_tick(px, tick)
            return {"reason": "stop_1atr", "price": px, "t": ts.to_pydatetime(),
                    "mfe": mfe, "mae": max(mae, max(0.0, -side * (adv - entry)))}
        mfe = max(mfe, max(0.0, side * (fav - entry)))
        mae = max(mae, max(0.0, -side * (adv - entry)))
        tgt_hit = (h >= target) if side > 0 else (l <= target)
        if tgt_hit:
            gap = (o >= target) if side > 0 else (o <= target)
            px = (o if gap else target) - side * slip
            if tick:
                px = _round_tick(px, tick)
            return {"reason": "target_1atr", "price": px, "t": ts.to_pydatetime(),
                    "mfe": mfe, "mae": mae}
        last_close, last_end = c, ts + pd.Timedelta(minutes=1)
        if last_end >= due:
            px = last_close - side * slip
            if tick:
                px = _round_tick(px, tick)
            return {"reason": f"max_hold_{int(TIME_CAP_MINUTES)}m", "price": px,
                    "t": due.to_pydatetime() if last_end > due else last_end.to_pydatetime(),
                    "mfe": mfe, "mae": mae}
    return None


def excursion(bars: pd.DataFrame | None, *, side: int, entry: float, start, horizons_h=HORIZONS_H) -> dict:
    """MFE/MAE in price and time-to-extreme from `start`, for each horizon (hours). Independent of the bracket."""
    out = {}
    if bars is None or not len(bars) or not entry:
        for h in horizons_h:
            out[f"{h}h"] = {"mfe": None, "mae": None, "mfe_at": None, "mae_at": None,
                            "time_to_mfe_s": None, "time_to_mae_s": None, "bars": 0}
        return out
    t0 = _as_ts(start)
    for h in horizons_h:
        end = t0 + pd.Timedelta(hours=h)
        mfe = mae = 0.0
        mfe_at = mae_at = None
        n = 0
        for ts, row in bars[(bars.index >= t0.floor("min")) & (bars.index < end)].iterrows():
            if ts + pd.Timedelta(minutes=1) <= t0:
                continue
            hi, lo = float(row["high"]), float(row["low"])
            fav, adv = (hi, lo) if side > 0 else (lo, hi)
            fav_ex = max(0.0, side * (fav - entry))
            adv_ex = max(0.0, -side * (adv - entry))
            if fav_ex > mfe:
                mfe, mfe_at = fav_ex, ts.to_pydatetime()
            if adv_ex > mae:
                mae, mae_at = adv_ex, ts.to_pydatetime()
            n += 1
        out[f"{h}h"] = {
            "mfe": round(mfe, 8), "mae": round(mae, 8),
            "mfe_at": mfe_at.isoformat() if mfe_at else None,
            "mae_at": mae_at.isoformat() if mae_at else None,
            "time_to_mfe_s": int((mfe_at - t0.to_pydatetime()).total_seconds()) if mfe_at else None,
            "time_to_mae_s": int((mae_at - t0.to_pydatetime()).total_seconds()) if mae_at else None,
            "bars": n,
        }
    return out


def net_and_R(side: int, entry: float, exit_px: float, point_value: float, commission: float,
              qty: int = 1) -> dict:
    comm = 2 * float(commission or 0) * qty
    gross = side * (exit_px - entry) * float(point_value) * qty
    net = gross - comm
    risk = abs(entry - (entry - side * abs(exit_px - entry)))  # placeholder, overwritten by caller with stop
    return {"gross": round(gross, 2), "commission": round(comm, 2), "net": round(net, 2)}


def result_R(side: int, entry: float, exit_px: float, stop: float, point_value: float,
             commission: float, qty: int = 1) -> dict:
    comm = 2 * float(commission or 0) * qty
    gross = side * (exit_px - entry) * float(point_value) * qty
    net = gross - comm
    risk = abs(entry - stop) * float(point_value) * qty + comm
    return {"gross": round(gross, 2), "commission": round(comm, 2), "net": round(net, 2),
            "risk_usd": round(risk, 2), "R": round(net / risk, 3) if risk > 0 else None}


def twin_side(key: str, salt: str = TWIN_SEED_SALT) -> tuple[int, int]:
    """Deterministic coin-flip side (+1 or -1) and the integer seed that produced it."""
    digest = hashlib.sha256(f"{salt}|{key}".encode()).hexdigest()
    seed = int(digest[:16], 16)
    rng = random.Random(seed)
    side = 1 if rng.randrange(2) == 0 else -1
    return side, seed
