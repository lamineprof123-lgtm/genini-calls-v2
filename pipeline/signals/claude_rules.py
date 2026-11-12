"""Claude sims: pure server-rule math (no I/O). PAPER ONLY.

Binding rules from templates/claude/claude_wiring_handoff.md section 3:
  hard stop = entry model stop_loss clamped to [0.6, 1.5] ATR; size = floor($900 / risk per contract), NQ/GC 1,
  6E/6B up to 4, account cap 6, 6E+6B same side share 4; break-even floor at best >= +0.5 ATR (entry + round-trip
  commission); profit lock 50% of best at >= +1.0 ATR, 60% at >= +1.5 ATR; time stop 45 min if never +0.25 ATR.
Every book (real, mechanical, what-if, random twin) is advanced with the same `advance()` so they are comparable.
"""
from __future__ import annotations
import math
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

RULE_DEFAULTS = {
    "stop_min_atr": 0.6, "stop_max_atr": 1.5, "risk_usd": 900.0,
    "max_qty": {"NQ": 1, "GC": 1, "6E": 4, "6B": 4}, "account_max_contracts": 6,
    "fx_group": ["6E", "6B"], "fx_group_max": 4,
    "be_trigger_atr": 0.5, "lock1_trigger_atr": 1.0, "lock1_pct": 0.50, "lock2_trigger_atr": 1.5, "lock2_pct": 0.60,
    "time_stop_minutes": 45, "time_stop_mfe_atr": 0.25,
    "daily_loss_usd": 1500.0, "trail_buffer_usd": 300.0,
    "loss_lock_minutes": 30, "exit_fail_limit": 3, "stale_price_seconds": 60,
    "max_chase_atr": 0.5, "min_score": 5, "slippage_ticks": 1,
    "phase_b_atr": 0.25, "phase_c_atr": 0.75,
}


def rules_cfg(cc: dict) -> dict:
    r = dict(RULE_DEFAULTS)
    r.update((cc or {}).get("rules") or {})
    return r


def round_tick(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10)


def clamp_stop(side: int, entry: float, stop_loss, atr: float, r: dict) -> tuple[float | None, float | None, str]:
    """Returns (stop_price, distance, note). None when the model stop is missing / on the wrong side."""
    if not atr or atr <= 0:
        return None, None, "no ATR"
    if stop_loss is None:
        return None, None, "stop_loss missing"
    dist = side * (entry - float(stop_loss))
    if dist <= 0:
        return None, None, "stop_loss on the wrong side of entry"
    lo, hi = r["stop_min_atr"] * atr, r["stop_max_atr"] * atr
    note = "model stop"
    if dist < lo:
        dist, note = lo, f"clamped up to {r['stop_min_atr']} ATR"
    elif dist > hi:
        dist, note = hi, f"clamped down to {r['stop_max_atr']} ATR"
    return entry - side * dist, dist, note


def risk_per_contract(dist: float, spec: dict) -> float:
    """Dollars lost per contract at the stop, incl. round-trip commission."""
    return dist * float(spec["point_value"]) + 2 * float(spec.get("commission") or 0)


def size_for(contract: str, side: int, dist: float, spec: dict, r: dict, open_positions: list[dict]) -> tuple[int, str]:
    """(qty, reason). qty 0 = skip."""
    rpc = risk_per_contract(dist, spec)
    if rpc > r["risk_usd"]:
        return 0, f"1 {contract} would risk ${rpc:,.0f} > ${r['risk_usd']:,.0f}"
    q = int(math.floor(r["risk_usd"] / rpc))
    q = min(q, int((r.get("max_qty") or {}).get(contract, 1)))
    used = sum(int(p["qty"]) for p in open_positions)
    q = min(q, int(r["account_max_contracts"]) - used)
    grp = r.get("fx_group") or []
    if contract in grp:
        same = sum(int(p["qty"]) for p in open_positions if p["contract"] in grp and p["side"] == side)
        q = min(q, int(r["fx_group_max"]) - same)
    if q < 1:
        return 0, "contract caps reached (account 6 / 6E+6B same side 4)"
    return q, "ok"


def floor_for(side: int, entry: float, best_pts: float, atr: float, comm_pts: float, r: dict) -> tuple[float | None, str | None]:
    """Server profit floor from the best favourable excursion (price points)."""
    if not atr or atr <= 0:
        return None, None
    b = best_pts / atr
    if b >= r["lock2_trigger_atr"]:
        return entry + side * r["lock2_pct"] * best_pts, "profit lock 60% of best"
    if b >= r["lock1_trigger_atr"]:
        return entry + side * r["lock1_pct"] * best_pts, "profit lock 50% of best"
    if b >= r["be_trigger_atr"]:
        return entry + side * comm_pts, "break-even + commission"
    return None, None


def effective_stop(t: dict) -> float:
    s = t["stop"]
    f = t.get("floor")
    if f is None:
        return s
    return max(s, f) if t["side"] > 0 else min(s, f)


def phase_of(mfe_atr: float | None, r: dict) -> str:
    m = mfe_atr or 0.0
    return "C" if m >= r["phase_c_atr"] else "B" if m >= r["phase_b_atr"] else "A"


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def lucid_day(t: datetime, open_et: str = "18:00"):
    et = t.astimezone(ET)
    return (et + timedelta(days=1)).date() if et.time() >= _hm(open_et) else et.date()


def in_session(t: datetime, flat_et: str = "16:45", open_et: str = "18:00") -> bool:
    et = t.astimezone(ET)
    d = lucid_day(t, open_et)
    if d.weekday() >= 5:
        return False
    return not (et.date() == d and et.time() >= _hm(flat_et))


def advance(t: dict, bars: pd.DataFrame | None, r: dict, *, flat_et: str = "16:45", open_et: str = "18:00",
            now: datetime | None = None) -> dict | None:
    """Advance one virtual/real trade along M1 bars since t['scan_from'] with the server rules only.
    Mutates t (mfe/mae/floor/reach flags). Returns {'reason','price','t'} when the rules close it, else None.
    Order inside a bar: stop/floor check against the CURRENT effective stop first (conservative), then
    excursion update, then floor update; time stop and the 16:45 ET flat at the bar's close."""
    if bars is None or not len(bars):
        return None
    sd, atr = t["side"], float(t["atr"])
    entry_t = pd.Timestamp(datetime.fromisoformat(t["entry_time"])).floor("min")
    start = pd.Timestamp(datetime.fromisoformat(t.get("scan_from") or t["entry_time"])).floor("min")
    start = max(start, entry_t)
    tick = float(t.get("tick") or 0)
    slip = int(r.get("slippage_ticks") or 0) * tick
    last_ts = None
    now_ts = pd.Timestamp(now or datetime.now(UTC))
    entered = pd.Timestamp(datetime.fromisoformat(t["entry_time"]))
    for ts, row in bars[bars.index >= start].iterrows():
        if ts < entered:
            # entry minute of a live-quote fill (entry_time hh:mm:ss): the bar's range is mostly pre-entry, so it
            # never triggers the stop or feeds MFE/MAE (the 18 s live-price exit checks cover that minute)
            continue
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        fav, adv = (h, l) if sd > 0 else (l, h)
        stop = effective_stop(t)
        hit = (l <= stop) if sd > 0 else (h >= stop)
        if hit:
            gap = (o <= stop) if sd > 0 else (o >= stop)
            px = (o if gap else stop) - sd * slip
            if t.get("reach_05") is None and t.get("floor") is None:
                t["reach_05"] = False
            lbl = t.get("floor_label") if (t.get("floor") is not None and stop == t["floor"]) else None
            reason = "hard_stop" if lbl is None else ("break_even" if lbl.startswith("break") else "profit_lock")
            return {"reason": reason, "price": round_tick(px, tick) if tick else px,
                    "t": ts.to_pydatetime().isoformat()}
        t["mfe_pts"] = max(float(t.get("mfe_pts") or 0.0), sd * (fav - t["entry"]), 0.0)
        t["mae_pts"] = max(float(t.get("mae_pts") or 0.0), -sd * (adv - t["entry"]), 0.0)
        if t.get("reach_05") is None and t["mfe_pts"] >= 0.5 * atr:
            t["reach_05"] = True
        f, lbl = floor_for(sd, t["entry"], t["mfe_pts"], atr, float(t.get("comm_pts") or 0.0), r)
        if f is not None:
            old = t.get("floor")
            if old is None or (sd > 0 and f > old) or (sd < 0 and f < old):
                t["floor"], t["floor_label"] = (round_tick(f, tick) if tick else f), lbl
        bar_end = ts + pd.Timedelta(minutes=1)
        last_ts = ts
        if bar_end > now_ts:
            break                                   # forming bar: no time-based exits yet
        mins = (bar_end - entry_t).total_seconds() / 60.0
        if mins >= r["time_stop_minutes"] and t["mfe_pts"] < r["time_stop_mfe_atr"] * atr:
            return {"reason": "time_stop_45m", "price": round_tick(c - sd * slip, tick) if tick else c,
                    "t": bar_end.to_pydatetime().isoformat()}
        if not in_session(bar_end.to_pydatetime(), flat_et, open_et):
            return {"reason": "flat_16:45_ET", "price": round_tick(c - sd * slip, tick) if tick else c,
                    "t": bar_end.to_pydatetime().isoformat()}
    if last_ts is not None:
        t["scan_from"] = last_ts.isoformat()       # re-scan the possibly forming minute next time
    return None


def trade_result(t: dict, exit_px: float, spec: dict) -> dict:
    """Dollars (net of round-trip commission) and R (= result / dollars at risk at the initial stop)."""
    pv = float(spec["point_value"])
    qty = int(t.get("qty") or 1)
    comm = 2 * float(spec.get("commission") or 0) * qty
    gross = t["side"] * (exit_px - t["entry"]) * pv * qty
    net = gross - comm
    risk = abs(t["entry"] - t["stop"]) * pv * qty + comm
    return {"gross": round(gross, 2), "commission": round(comm, 2), "net": round(net, 2),
            "risk_usd": round(risk, 2), "R": round(net / risk, 3) if risk > 0 else None}


def m5_summary(m1: pd.DataFrame | None, now: datetime) -> dict:
    """Last 3 CLOSED M5 closes (oldest first) and the forming M5 OHLC from M1 bars (forming M1 included)."""
    out = {"m5_c3": None, "m5_c2": None, "m5_c1": None, "m5_o": None, "m5_h": None, "m5_l": None}
    if m1 is None or not len(m1):
        return out
    df = m1.sort_index()
    agg = df.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if not len(agg):
        return out
    nt = pd.Timestamp(now)
    closed = agg[agg.index + pd.Timedelta(minutes=5) <= nt]
    forming = agg[agg.index + pd.Timedelta(minutes=5) > nt]
    cl = list(closed["close"].tail(3))
    while len(cl) < 3:
        cl.insert(0, None)
    out.update({"m5_c3": cl[0], "m5_c2": cl[1], "m5_c1": cl[2]})
    if len(forming):
        f = forming.iloc[-1]
        out.update({"m5_o": float(f["open"]), "m5_h": float(f["high"]), "m5_l": float(f["low"])})
    return out


def h1_context(h1: pd.DataFrame | None, now: datetime, anchor: str = "utc_day") -> dict:
    """h1_trend from the H1 EMA50 slope over the last 5 closed H1 bars (in H1 ATR14 units: > +0.05 up,
    < -0.05 down, else flat) and the prior session day's high / low (UTC day; CME 18:00 ET day for US100)."""
    out = {"h1_trend": None, "h1_ema50_slope_atr": None, "prior_day_high": None, "prior_day_low": None}
    if h1 is None or len(h1) < 20:
        return out
    d = h1.sort_index()
    ema = d["close"].ewm(span=50, adjust=False).mean()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    a = float(atr.iloc[-1])
    if a > 0 and len(ema) > 6:
        s = float(ema.iloc[-1] - ema.iloc[-6]) / 5.0 / a
        out["h1_ema50_slope_atr"] = round(s, 3)
        out["h1_trend"] = "up" if s > 0.05 else "down" if s < -0.05 else "flat"
    if anchor == "cme_globex":
        et = d.index.tz_convert(ET)
        key = pd.Index([(x + pd.Timedelta(days=1)).date() if x.hour >= 18 else x.date() for x in et])
        cur = lucid_day(now)
    else:
        key = pd.Index([x.date() for x in d.index])
        cur = pd.Timestamp(now).tz_convert(UTC).date()
    prev = sorted({k for k in key if k < cur})
    if prev:
        sel = d[key == prev[-1]]
        if len(sel):
            out["prior_day_high"], out["prior_day_low"] = float(sel["high"].max()), float(sel["low"].min())
    return out


def pctl(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f), 1)
