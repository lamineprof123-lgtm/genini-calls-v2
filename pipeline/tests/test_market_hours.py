"""Weekend gate (13 Nov 2026): Friday close -> Sunday reopen, no model calls of any kind; BTC alone never calls."""
from __future__ import annotations
import asyncio, os, sys
from datetime import datetime, timedelta, timezone
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


def u(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_config_on():
    mh = load_config()["market_hours"]
    assert mh["enabled"] and mh["friday_close_et"] == "17:00" and mh["sunday_open_et"] == "18:00"


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
