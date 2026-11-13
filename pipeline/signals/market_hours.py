"""Weekend market-closed gate (user, 13 Nov 2026): from Friday's close to Sunday's reopen NO model call of any kind
(Gemini MAIN, G-LOW-80, Carbon, Claude / C-OPUS-B, DeepSeek, the P5 books, Gemini exit calls, model probes).
BTC trades 24/7 but is never the only reason to call. Clock-based on America/New_York (CME Globex / spot FX week):
closed Fri >= 17:00 ET .. Sun < 18:00 ET (= Fri 22:00 UTC .. Sun 23:00 UTC in winter, 02:00 Sat .. 03:00 Mon Dubai).
Config [market_hours]: enabled (default true), friday_close_et, sunday_open_et.

Two layers: callers skip cleanly (pipeline.run_once, M15Coordinator.dispatch, gha_loop model probe), and every
model client calls guard() right before the network request as a hard backstop (raises MarketsClosed)."""
from __future__ import annotations
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULTS = {"enabled": True, "friday_close_et": "17:00", "sunday_open_et": "18:00"}
_CFG: dict = {}          # set once by configure() so client-level guards see the live config


class MarketsClosed(RuntimeError):
    pass


def configure(cfg: dict | None) -> None:
    global _CFG
    _CFG = dict((cfg or {}).get("market_hours") or {})


def _hm(s: str) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def markets_closed(now: datetime | None = None, cfg: dict | None = None) -> bool:
    mh = {**DEFAULTS, **(_CFG if cfg is None else ((cfg or {}).get("market_hours") or {}))}
    if not mh.get("enabled", True):
        return False
    t = (now or datetime.now(timezone.utc))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    et = t.astimezone(ET)
    wd, m = et.weekday(), et.hour * 60 + et.minute
    return wd == 5 or (wd == 4 and m >= _hm(mh["friday_close_et"])) or (wd == 6 and m < _hm(mh["sunday_open_et"]))


def reopen_note() -> str:
    return "markets closed (weekend: Fri 17:00 ET close to Sun 18:00 ET reopen) - no model calls"


def guard(what: str, now: datetime | None = None) -> None:
    """Hard backstop inside every model client. BB_MARKET_GUARD=0 disables it (unit tests only)."""
    if os.environ.get("BB_MARKET_GUARD", "1") == "0":
        return
    if markets_closed(now):
        raise MarketsClosed(f"{what}: {reopen_note()}")
