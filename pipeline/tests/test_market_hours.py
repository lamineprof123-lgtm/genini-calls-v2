"""Weekend + holiday gate: Friday close -> Sunday reopen, and full-day CME/FX holidays; BTC alone never calls."""
from __future__ import annotations
import asyncio, os, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from signals import market_hours as MH
from signals import pipeline as P
from signals.config import load_config
from signals.m15_scan import M15Coordinator
from test_main_m15_cadence import _cfg, _snap, patched  # noqa: F401

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)



def u(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_config_on():
    mh = load_config()["market_hours"]
    assert mh["enabled"] and mh["friday_close_et"] == "17:00" and mh["sunday_open_et"] == "18:00"
    assert mh["holidays"] == list(MH.DEFAULT_HOLIDAYS)
    for d in ("2026-12-25", "2027-01-01", "2027-03-26", "2027-12-24"):
        assert d in mh["holidays"]


def test_closed_window_et():
    c = {"market_hours": {"enabled": True}}
    assert not MH.markets_closed(u(2026, 11, 13, 21, 59), c)    # Fri 16:59 ET
    assert MH.markets_closed(u(2026, 11, 13, 22, 0), c)         # Fri 17:00 ET (02:00 Sat Dubai)
    assert MH.markets_closed(u(2026, 11, 14, 12, 0), c)         # Saturday
    assert MH.markets_closed(u(2026, 11, 15, 22, 59), c)        # Sun 17:59 ET
    assert not MH.markets_closed(u(2026, 11, 15, 23, 0), c)     # Sun 18:00 ET reopen (03:00 Mon Dubai)
    assert not MH.markets_closed(u(2026, 11, 16, 14, 0), c)     # Monday
    assert MH.markets_closed(u(2026, 7, 17, 21, 0), c)          # summer: Fri 17:00 EDT = 21:00 UTC
    assert not MH.markets_closed(u(2026, 7, 19, 22, 0), c)      # summer: Sun 18:00 EDT = 22:00 UTC
    assert not MH.markets_closed(u(2026, 11, 14, 12, 0), {"market_hours": {"enabled": False}})


def test_client_guard_backstop(monkeypatch):
    monkeypatch.setenv("BB_MARKET_GUARD", "1")
    MH.configure({"market_hours": {"enabled": True}})
    with pytest.raises(MH.MarketsClosed):
        MH.guard("Gemini", u(2026, 11, 14, 12, 0))
    MH.guard("Gemini", u(2026, 11, 16, 14, 0))                  # weekday: fine
    monkeypatch.setattr(MH, "markets_closed", lambda now=None, cfg=None: True)
    from signals import deepseek, claude_client
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    r = asyncio.run(deepseek.call_deepseek({}, messages=[{"role": "user", "content": "x"}]))
    assert not r["ok"] and "markets closed" in r["error"]
    monkeypatch.setattr(claude_client, "key_present", lambda: True)
    r = asyncio.run(claude_client.messages_call(model="m", system="s", content=[], schema={}, max_tokens=10, thinking=None, effort=None, timeout=5))
    assert not r["ok"] and "markets closed" in r["error"] and r["attempts"] == 0
    from signals import gemini_client
    with pytest.raises(MH.MarketsClosed):
        asyncio.run(gemini_client.call_gemini(load_config(), [], {}))


def test_main_run_once_no_call_on_weekend(tmp_path, patched, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(MH, "markets_closed", lambda now=None, cfg=None: True)
    c = u(2026, 11, 13, 13, 15)
    for kw in ({}, {"m15_close": c, "snapshot": _snap(c)}):
        res = asyncio.run(P.run_once(cfg, force=True, **kw))
        assert res["skipped_reason"] == P.MARKETS_CLOSED_SKIP and res["record"]["signals"] == []
    assert patched["gemini"] == []


class Boom:
    def __getattr__(self, k):
        raise AssertionError(f"model book touched on a closed market: {k}")


def test_m15_dispatch_no_book_called_on_weekend(tmp_path):
    cfg = load_config()
    co = M15Coordinator.__new__(M15Coordinator)
    co.cfg = cfg
    for a in ("claude", "carbon", "opus_b", "deepseek", "glow80", "gp5", "gp5event", "gp5eval", "eval80", "evalr4"):
        setattr(co, a, Boom())
    sat = _snap(u(2026, 11, 14, 12, 0))
    out = asyncio.run(co.dispatch(sat))
    assert out == {"markets_closed": {"skipped": "markets_closed"}}
    sun_eve = _snap(u(2026, 11, 15, 22, 45))                    # 17:45 ET Sunday: still closed (only BTC trades)
    assert asyncio.run(co.dispatch(sun_eve)) == {"markets_closed": {"skipped": "markets_closed"}}


def test_bracket_ignores_closed_records():
    import inspect
    from signals import bracket
    assert "markets_closed" in inspect.getsource(bracket.BracketBook.intake)


def test_holiday_new_year_2026_day_before_closed_and_reopen():
    """Thu 1 Jan 2026 is a midweek full close: Wed 17:00 ET -> Thu 18:00 ET reopen."""
    c = {"market_hours": {"enabled": True}}
    assert not MH.markets_closed(et(2025, 12, 31, 16, 59), c)   # day before, still open
    assert MH.markets_closed(et(2025, 12, 31, 17, 0), c)        # prior session 17:00 ET close
    assert MH.markets_closed(et(2026, 1, 1, 0, 0), c)           # holiday midnight
    assert MH.markets_closed(et(2026, 1, 1, 12, 0), c)          # holiday midday
    assert MH.markets_closed(et(2026, 1, 1, 17, 59), c)         # still closed
    assert not MH.markets_closed(et(2026, 1, 1, 18, 0), c)      # reopen evening
    assert not MH.markets_closed(et(2026, 1, 2, 10, 0), c)      # next session day


def test_holiday_christmas_2026_friday_held_through_weekend():
    """Fri 25 Dec 2026 is closed from Thu 17:00 ET; weekend gate holds through Sun 18:00 ET."""
    c = {"market_hours": {"enabled": True}}
    assert not MH.markets_closed(et(2026, 12, 24, 16, 59), c)   # day before, still open
    assert MH.markets_closed(et(2026, 12, 24, 17, 0), c)
    assert MH.markets_closed(et(2026, 12, 25, 12, 0), c)        # Christmas Day
    assert MH.markets_closed(et(2026, 12, 25, 18, 0), c)        # Friday evening stays closed (weekend)
    assert MH.markets_closed(et(2026, 12, 26, 12, 0), c)        # Saturday
    assert MH.markets_closed(et(2026, 12, 27, 17, 59), c)       # Sunday just before reopen
    assert not MH.markets_closed(et(2026, 12, 27, 18, 0), c)    # Sunday 18:00 ET reopen


def test_holiday_good_friday_2027_and_christmas_observed():
    c = {"market_hours": {"enabled": True}}
    assert not MH.markets_closed(et(2027, 3, 25, 16, 59), c)
    assert MH.markets_closed(et(2027, 3, 25, 17, 0), c)
    assert MH.markets_closed(et(2027, 3, 26, 12, 0), c)
    assert not MH.markets_closed(et(2027, 3, 28, 18, 0), c)     # Sunday reopen after Good Friday
    assert not MH.markets_closed(et(2027, 12, 23, 16, 59), c)
    assert MH.markets_closed(et(2027, 12, 23, 17, 0), c)
    assert MH.markets_closed(et(2027, 12, 24, 12, 0), c)        # Christmas observed
    assert not MH.markets_closed(et(2027, 12, 26, 18, 0), c)


def test_thanksgiving_and_early_closes_are_not_full_closures():
    """CME Globex/FX are not fully closed on Thanksgiving (midday halt, evening reopen; FX normal)."""
    c = {"market_hours": {"enabled": True}}
    assert not MH.markets_closed(et(2026, 11, 25, 17, 0), c)    # Wednesday evening session stays open
    assert not MH.markets_closed(et(2026, 11, 26, 10, 0), c)    # Thanksgiving morning
    assert not MH.markets_closed(et(2026, 11, 27, 10, 0), c)    # day after Thanksgiving (early close only)
    assert not MH.markets_closed(et(2027, 12, 30, 17, 0), c)    # New Year 2028 not observed Fri 31 Dec
    assert not MH.markets_closed(et(2027, 12, 31, 16, 59), c)   # Friday session still open until weekend close


def test_holidays_config_override():
    only = {"market_hours": {"enabled": True, "holidays": ["2026-06-10"]}}
    assert not MH.markets_closed(et(2026, 6, 9, 16, 59), only)
    assert MH.markets_closed(et(2026, 6, 9, 17, 0), only)
    assert MH.markets_closed(et(2026, 6, 10, 12, 0), only)
    assert not MH.markets_closed(et(2026, 6, 10, 18, 0), only)
    # override replaces the built-in list: a default holiday is not closed
    assert not MH.markets_closed(et(2026, 12, 24, 17, 0), only)
    empty = {"market_hours": {"enabled": True, "holidays": []}}
    assert not MH.markets_closed(et(2026, 1, 1, 12, 0), empty)
    off = {"market_hours": {"enabled": False, "holidays": ["2026-01-01"]}}
    assert not MH.markets_closed(et(2026, 1, 1, 12, 0), off)


def test_reopen_note_mentions_holidays():
    note = MH.reopen_note().lower()
    assert "holiday" in note and "weekend" in note and "no model calls" in note
