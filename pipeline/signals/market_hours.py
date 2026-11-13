"""Market-closed gate (weekend + full-day CME Globex/FX holidays): from Friday's close to
Sunday's reopen, and from the prior session's 17:00 ET close to 18:00 ET on a listed holiday,
NO model call of any kind (Gemini MAIN, G-LOW-80, Carbon, Claude / C-OPUS-B, DeepSeek, the
P5 books, Gemini exit calls, model probes). BTC trades 24/7 but is never the only reason to
call. Clock-based on America/New_York (CME Globex / spot FX week): closed Fri >= 17:00 ET
.. Sun < 18:00 ET (= Fri 22:00 UTC .. Sun 23:00 UTC in winter, 02:00 Sat .. 03:00 Mon Dubai).
A holiday is closed from 17:00 ET the previous calendar day until 18:00 ET on the holiday
(reopen evening); if the holiday is a Friday, the weekend gate keeps the close through
Sunday 18:00 ET. Default holidays are CME Globex/FX full closures through end of 2027
(New Year, Christmas, Good Friday, and Christmas 2027 observed). Early-close / midday-halt
days (Thanksgiving, day after Thanksgiving, MLK, etc.) are not treated as fully closed.
New Year's Day 2028 falls on Saturday and is not observed on Friday 31 Dec 2027; the
weekend gate already covers it.
Config [market_hours]: enabled (default true), friday_close_et, sunday_open_et, holidays
(list of YYYY-MM-DD, default DEFAULT_HOLIDAYS).

Two layers: callers skip cleanly (pipeline.run_once, M15Coordinator.dispatch, gha_loop model
probe), and every model client calls guard() right before the network request as a hard
backstop (raises MarketsClosed)."""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# CME Globex / FX full closures through end of 2027. Source: CME Group Holiday and Trading
# Hours, https://www.cmegroup.com/trading-hours.html (checked 14 Nov 2026). Thanksgiving
# and other abbreviated sessions are omitted on purpose.
DEFAULT_HOLIDAYS = (
    "2026-01-01",  # New Year's Day (Thu) — closed, reopen 18:00 ET same day
    "2026-12-25",  # Christmas Day (Fri) — closed; weekend gate holds through Sun 18:00 ET
    "2027-01-01",  # New Year's Day (Fri) — closed; weekend gate holds through Sun 18:00 ET
    "2027-03-26",  # Good Friday — no trading for the Friday trade date
    "2027-12-24",  # Christmas Day observed (Fri; Christmas is Sat 25 Dec 2027)
)
DEFAULTS = {
    "enabled": True,
    "friday_close_et": "17:00",
    "sunday_open_et": "18:00",
    "holidays": list(DEFAULT_HOLIDAYS),
}
_CFG: dict = {}          # set once by configure() so client-level guards see the live config


class MarketsClosed(RuntimeError):
    pass


def configure(cfg: dict | None) -> None:
    global _CFG
    _CFG = dict((cfg or {}).get("market_hours") or {})


def _hm(s: str) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def _holiday_dates(mh: dict) -> set[str]:
    raw = mh.get("holidays", DEFAULT_HOLIDAYS)
    if raw is None:
        raw = DEFAULT_HOLIDAYS
    if isinstance(raw, str):
        raw = [raw]
    return {str(item).strip() for item in raw if str(item).strip()}


def _holiday_closed(et: datetime, holidays: set[str], close_m: int, open_m: int) -> bool:
    d = et.date()
    m = et.hour * 60 + et.minute
    if d.isoformat() in holidays and m < open_m:
        return True
    if (d + timedelta(days=1)).isoformat() in holidays and m >= close_m:
        return True
    return False


def markets_closed(now: datetime | None = None, cfg: dict | None = None) -> bool:
    mh = {**DEFAULTS, **(_CFG if cfg is None else ((cfg or {}).get("market_hours") or {}))}
    if not mh.get("enabled", True):
        return False
    t = (now or datetime.now(timezone.utc))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    et = t.astimezone(ET)
    wd, m = et.weekday(), et.hour * 60 + et.minute
    if wd == 5 or (wd == 4 and m >= _hm(mh["friday_close_et"])) or (wd == 6 and m < _hm(mh["sunday_open_et"])):
        return True
    return _holiday_closed(et, _holiday_dates(mh), _hm(mh["friday_close_et"]), _hm(mh["sunday_open_et"]))


def reopen_note() -> str:
    return ("markets closed (weekend: Fri 17:00 ET close to Sun 18:00 ET reopen, "
            "or a full-day CME/FX holiday from the prior session's 17:00 ET close "
            "to 18:00 ET reopen) - no model calls")


def guard(what: str, now: datetime | None = None) -> None:
    """Hard backstop inside every model client. BB_MARKET_GUARD=0 disables it (unit tests only)."""
    if os.environ.get("BB_MARKET_GUARD", "1") == "0":
        return
    if markets_closed(now):
        raise MarketsClosed(f"{what}: {reopen_note()}")
