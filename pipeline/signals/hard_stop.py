"""Hard stop of -$N NET of round-trip commission (USER RULE 2026-11-12 15:5x Dubai: "apply the best stop").

PAPER ONLY. Applies to the main Lucid account and the 1-strike / 2-strike exit books. The "nostop" shadow book
never stops (same entries, Gemini CLOSE only) so stop vs no-stop is a same-trade comparison.

Level: the first tick at which the position's NET P&L (gross - full RT commission) is <= -hard_stop_net_usd.
Trigger: live price (18 s exit checks) or the M1 path since the last scan, only from the arming time on (positions
open at deploy are armed at the first check on the new code: never retroactive). Fill: the stop level (or the
live price / bar open if price is already beyond it) with [lucid].slippage_ticks adverse ticks.
"""
from __future__ import annotations
import math

HARD_STOP_REASON_FMT = "hard_stop_{usd:g}"
SHADOW_BOOKS = ("nostop",)


def hard_stop_usd(cfg: dict) -> float | None:
    v = (cfg.get("lucid") or {}).get("hard_stop_net_usd")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def hard_stop_reason(cfg: dict) -> str:
    usd = hard_stop_usd(cfg) or 0
    return HARD_STOP_REASON_FMT.format(usd=usd)


def shadow_books(cfg: dict) -> tuple[str, ...]:
    return tuple((cfg.get("exit_manager") or {}).get("shadow_books") or SHADOW_BOOKS)


def book_has_stop(cfg: dict, book: str) -> bool:
    return hard_stop_usd(cfg) is not None and book not in shadow_books(cfg)


def _spec(cfg: dict, contract: str) -> dict | None:
    return ((cfg.get("lucid") or {}).get("contracts") or {}).get(contract)


def hard_stop_level(cfg: dict, pos: dict) -> float | None:
    """Price where NET (after full RT commission) first reaches -hard_stop_net_usd, rounded to the tick AWAY from
    the entry (so the trigger is never early). None when the stop is off or the contract is unknown."""
    usd = hard_stop_usd(cfg)
    sp = _spec(cfg, pos.get("contract") or "")
    if usd is None or not sp or not pos.get("qty"):
        return None
    qty = int(pos["qty"])
    upp = float(sp["point_value"]) * qty
    comm_rt = 2 * float(sp["commission"]) * qty
    dist = (usd - comm_rt) / upp
    if dist <= 0:
        dist = float(sp["tick"])
    tick = float(sp["tick"])
    raw = float(pos["entry"]) - int(pos["side"]) * dist
    n = raw / tick
    n = math.floor(n + 1e-9) if int(pos["side"]) > 0 else math.ceil(n - 1e-9)
    return round(n * tick, 10)


def beyond(side: int, level: float, price: float) -> bool:
    """True when price is at/through the stop level against a position of `side`."""
    return price <= level + 1e-12 if side > 0 else price >= level - 1e-12


def slip(cfg: dict, pos: dict, price: float) -> float:
    sp = _spec(cfg, pos.get("contract") or "") or {"tick": 0}
    tick = float(sp["tick"])
    n = int((cfg.get("lucid") or {}).get("slippage_ticks", 0) or 0)
    px = float(price) - int(pos["side"]) * n * tick
    return round(round(px / tick) * tick, 10) if tick else px


def net_at(cfg: dict, pos: dict, price: float) -> float | None:
    sp = _spec(cfg, pos.get("contract") or "")
    if not sp:
        return None
    qty = int(pos["qty"])
    return round(int(pos["side"]) * (price - float(pos["entry"])) * float(sp["point_value"]) * qty
                 - 2 * float(sp["commission"]) * qty, 2)
