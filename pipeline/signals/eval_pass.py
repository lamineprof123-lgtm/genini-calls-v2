"""Simulated LucidPro 100K eval passing stats for the panel. Display only.

Replays a book's Lucid-only closed trades from $100,000. Other-venue BTC is excluded
because prop firms do not offer it. Each attempt ends when it hits the $6,000 profit
target (pass) or breaches the $1,800 daily loss limit / the $3,000 end-of-day trailing
max loss (fail), then restarts at $100,000 with the next trade. This never touches a
book's account, balances, or trading.
"""
from __future__ import annotations
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from .glow80 import lucid_day

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

START = 100_000.0
PROFIT_TARGET = 6_000.0
MLL = 3_000.0
DLL = 1_800.0
LOCKED_MLL_BALANCE = 100_100.0
INITIAL_TRAIL_BALANCE = 103_100.0   # floor locks at $100,100 once an EOD close exceeds this
FLAT_ET = dtime(16, 45)


def is_lucid_tradable(symbol: str | None) -> bool:
    """True for Lucid-tradable symbols. BTC and blanks are dropped (prop firms have no BTC)."""
    s = str(symbol or "").upper().replace("/", "").replace("-", "").replace("_", "")
    if not s or s.startswith("BTC") or s.startswith("XBT") or s in {"BITCOIN"}:
        return False
    return True


def _dt(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


def _fresh() -> dict:
    return {
        "balance": START,
        "mll_floor": START - MLL,
        "highest_eod": START,
        "mll_locked": False,
        "day": None,
        "day_net": 0.0,
    }


def _eod(st: dict) -> str | None:
    """Apply the previous session's EOD trailing MLL. Returns 'mll' on a fail, else None."""
    if st["balance"] <= st["mll_floor"] + 1e-9:
        return "mll"
    if not st["mll_locked"]:
        st["highest_eod"] = max(st["highest_eod"], st["balance"])
        if st["highest_eod"] > INITIAL_TRAIL_BALANCE + 1e-9:
            st["mll_locked"] = True
            st["mll_floor"] = LOCKED_MLL_BALANCE
        else:
            st["mll_floor"] = min(st["highest_eod"] - MLL, LOCKED_MLL_BALANCE)
    return None


def _past_eod(now: datetime, day: str) -> bool:
    """True when `now` is at or after 16:45 ET of Lucid day `day`."""
    nd = lucid_day(now)
    if nd > day:
        return True
    if nd < day:
        return False
    return now.astimezone(ET).time() >= FLAT_ET


def _roll_day(st: dict, day: str) -> str | None:
    if st["day"] is None:
        st["day"] = day
        st["day_net"] = 0.0
        return None
    if st["day"] == day:
        return None
    reason = _eod(st)
    st["day"] = day
    st["day_net"] = 0.0
    return reason


def _snapshot(st: dict, *, open_pnl: float, passes: int, fails: int, lucid_trades: int) -> dict:
    equity = round(st["balance"] + float(open_pnl or 0), 2)
    profit = round(equity - START, 2)
    finished = passes + fails
    return {
        "passes": passes,
        "fails": fails,
        "attempts_finished": finished,
        "passing_pct": round(100.0 * passes / finished, 1) if finished else None,
        "passing_label": f"{passes}/{finished}" if finished else None,
        "current_profit": profit,
        "current_progress_pct": round(100.0 * profit / PROFIT_TARGET, 1),
        "profit_target": PROFIT_TARGET,
        "mll_floor": round(st["mll_floor"], 2),
        "mll_buffer": round(equity - st["mll_floor"], 2),
        "mll_locked": bool(st["mll_locked"]),
        "dll_used": round(max(0.0, -(st["day_net"] + float(open_pnl or 0))), 2),
        "dll_limit": DLL,
        "lucid_trades": lucid_trades,
        "current_equity": equity,
        "start_balance": START,
        "dll": DLL,
        "mll": MLL,
        "locked_mll_balance": LOCKED_MLL_BALANCE,
    }


def eval_pass_stats(closed: list[dict], *, open_pnl: float = 0.0, now: datetime | None = None) -> dict:
    """Replay Lucid-only closed trades in exit-time order.

    Each item needs ``net`` (or ``net_at_close_usd``), ``t_exit``, and ``symbol``.
    ``open_pnl`` is the book's current Lucid open P&L (BTC excluded by the caller)
    and is counted only on the unfinished attempt. ``now`` applies a pending EOD
    check when the last session has already closed.
    """
    trades = []
    for r in closed or []:
        if not is_lucid_tradable(r.get("symbol")):
            continue
        net = r.get("net") if r.get("net") is not None else r.get("net_at_close_usd")
        if net is None:
            continue
        t = _dt(r.get("t_exit")) or _dt(r.get("entry_time"))
        if t is None:
            continue
        trades.append((t, str(r.get("symbol") or ""), float(net)))
    trades.sort(key=lambda x: (x[0], x[1]))

    passes = fails = 0
    st = _fresh()

    def finish_fail() -> None:
        nonlocal fails, st
        fails += 1
        st = _fresh()

    def finish_pass() -> None:
        nonlocal passes, st
        passes += 1
        st = _fresh()

    for t, _sym, net in trades:
        day = lucid_day(t)
        if _roll_day(st, day) == "mll":
            finish_fail()
            st["day"] = day
            st["day_net"] = 0.0
        st["balance"] = round(st["balance"] + net, 2)
        st["day_net"] = round(st["day_net"] + net, 2)
        if st["day_net"] <= -DLL + 1e-9:
            finish_fail()
            continue
        if st["balance"] >= START + PROFIT_TARGET - 1e-9:
            finish_pass()
            continue

    if st["day"] is not None and now is not None and _past_eod(now, st["day"]):
        if _eod(st) == "mll":
            finish_fail()
        else:
            nd = lucid_day(now)
            if nd != st["day"]:
                st["day"] = nd
                st["day_net"] = 0.0

    # Open P&L counts only on an unfinished attempt. After a pass/fail reset with no further
    # trades, day is None and leftover opens are ignored (the simulated attempt already ended).
    # With no closed trades at all, day is also None, but any open P&L is the first attempt.
    op = float(open_pnl or 0) if (st["day"] is not None or not trades) else 0.0
    return _snapshot(st, open_pnl=op, passes=passes, fails=fails, lucid_trades=len(trades))


def from_panel_rows(rows: list[dict], *, open_pnl: float = 0.0, now: datetime | None = None) -> dict:
    """Convenience wrapper over ledger position rows (venue already filtered to LUCID)."""
    closed = [r for r in rows if r.get("status") != "OPEN" and r.get("net_at_close_usd") is not None]
    return eval_pass_stats(closed, open_pnl=open_pnl, now=now)
