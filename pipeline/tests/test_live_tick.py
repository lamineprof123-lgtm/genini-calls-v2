"""Tick live price, forming-candle math, fallbacks, and the FirmTape NQ override (no network)."""
from __future__ import annotations
import asyncio, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import live_price as LP
from signals import firmtape_live as FT
from signals.gemini_client import slim_gemini_context

UTC = timezone.utc


def _m1(start, rows):
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": [1.0] * len(rows)}, index=idx)


def _ticks(pairs):
    idx = pd.to_datetime([p[0] for p in pairs], utc=True, format="ISO8601")
    return pd.DataFrame({"bid": [p[1] for p in pairs], "ask": [p[1] + 0.0001 for p in pairs]}, index=idx)


NOW = datetime(2026, 11, 11, 8, 21, 30, tzinfo=UTC)   # forming bar opened 08:15


def _m1_window():
    # 08:10..08:20 finished minutes (08:20 bar ends 08:21:00 <= now)
    rows = [(1.1000, 1.1005, 1.0995, 1.1001)] * 5 + [
        (1.1010, 1.1020, 1.1008, 1.1015),   # 08:15 (window open)
        (1.1015, 1.1030, 1.1012, 1.1025),
        (1.1025, 1.1026, 1.1001, 1.1004),   # 08:17 low
        (1.1004, 1.1010, 1.1003, 1.1009),
        (1.1009, 1.1012, 1.1006, 1.1011),
        (1.1011, 1.1013, 1.1009, 1.1012),   # 08:20
    ]
    return _m1("2026-11-11 08:10:00", rows)


def test_forming_candle_from_m1_plus_ticks_after_last_minute():
    ticks = _ticks([("2026-11-11 08:20:30", 1.0990),     # inside the last finished minute: ignored for H/L
                    ("2026-11-11 08:21:05", 1.1035),     # new high after the last finished minute
                    ("2026-11-11 08:21:20", 1.1000),
                    ("2026-11-11 08:21:29.400", 1.1018)])
    live = LP.live_from_ticks(_m1_window(), ticks, tf_min=15, now=NOW, digits=5)
    assert live["live_price_source"] == "dukascopy:tick"
    assert live["live_price"] == pytest.approx(1.1018)
    assert live["live_price_ts_utc"].startswith("2026-11-11T08:21:29.4")
    assert live["forming_m15_open"] == pytest.approx(1.1010)      # first M1 open of the window
    assert live["forming_m15_high"] == pytest.approx(1.1035)      # tick after the last minute
    assert live["forming_m15_low"] == pytest.approx(1.1000)       # tick 1.1000 < M1 low 1.1001
    assert live["forming_m15_open_utc"].startswith("2026-11-11T08:15:00")
    assert live["live_price_stale"] is False


def test_forming_candle_ignores_in_progress_m1_and_previous_window_ticks():
    m1 = _m1_window()
    # an in-progress 08:21 M1 bar must be dropped (ticks cover it)
    m1.loc[pd.Timestamp("2026-11-11 08:21:00", tz="UTC")] = [1.2, 1.3, 1.0, 1.25, 1.0]
    ticks = _ticks([("2026-11-11 08:14:59", 0.9000), ("2026-11-11 08:21:10", 1.1011)])
    live = LP.live_from_ticks(m1, ticks, tf_min=15, now=NOW, digits=5)
    assert live["forming_m15_high"] == pytest.approx(1.1030)
    assert live["forming_m15_low"] == pytest.approx(1.1001)
    assert live["live_price"] == pytest.approx(1.1011)


def test_ticks_only_at_start_of_bar():
    now = datetime(2026, 11, 11, 8, 15, 20, tzinfo=UTC)
    m1 = _m1("2026-11-11 08:10:00", [(1.1, 1.1, 1.1, 1.1)] * 5)   # last finished minute 08:14
    ticks = _ticks([("2026-11-11 08:15:02", 1.2002), ("2026-11-11 08:15:10", 1.2010),
                    ("2026-11-11 08:15:19", 1.1990)])
    live = LP.live_from_ticks(m1, ticks, tf_min=15, now=now, digits=4)
    assert (live["forming_m15_open"], live["forming_m15_high"], live["forming_m15_low"], live["live_price"]) == \
        (1.2002, 1.201, 1.199, 1.199)


def test_ticks_lagging_m1_raise_and_build_live_falls_back_to_m1():
    ticks = _ticks([("2026-11-11 08:18:00", 1.1)])            # older than the last finished M1 (08:20)
    with pytest.raises(ValueError, match="lag"):
        LP.live_from_ticks(_m1_window(), ticks, now=NOW)
    live = LP.build_live("EURUSD", (_m1_window(), "dukascopy:m1"), ticks, tf_min=15, now=NOW, digits=5, ctx={})
    assert live["live_price_source"] == "dukascopy:m1"
    assert live["live_price"] == pytest.approx(1.1012)
    assert "lag" in live["live_price_fallback"]


def test_build_live_tick_error_falls_back_to_m1_and_total_failure_to_last_close():
    live = LP.build_live("EURUSD", (_m1_window(), "dukascopy:m1"), RuntimeError("timeout"), tf_min=15,
                         now=NOW, digits=5, ctx={})
    assert live["live_price_source"] == "dukascopy:m1" and "timeout" in live["live_price_fallback"]
    ctx = {"last_close": 1.1, "digits": 5, "last_closed_bar_utc": "2026-11-11T08:00:00+00:00"}
    live = LP.build_live("EURUSD", RuntimeError("m1 down"), RuntimeError("tick down"), tf_min=15,
                         now=NOW, digits=5, ctx=ctx)
    assert live["live_price_stale"] is True and live["live_price"] == 1.1
    assert live["live_price_source"].startswith("fallback_last_close")


def test_build_live_prefers_ticks():
    ticks = _ticks([("2026-11-11 08:21:29", 1.1020)])
    live = LP.build_live("EURUSD", (_m1_window(), "dukascopy:m1"), ticks, tf_min=15, now=NOW, digits=5, ctx={})
    assert live["live_price_source"] == "dukascopy:tick" and live["live_price"] == pytest.approx(1.102)


def _payload(sym, digits=5, last_close=1.1):
    return {"symbol": sym, "digits": digits,
            "context": {"digits": digits, "last_close": last_close, "last_closed_bar_utc": "2026-11-11T08:00:00+00:00"}}


def test_enrich_live_prices_tick_path_sets_age_source_and_forming_candle(monkeypatch):
    async def fake_m1(symbol, cfg, now):
        return _m1_window(), "dukascopy:m1"

    async def fake_ticks(ticker, since, timeout):
        return _ticks([("2026-11-11 08:21:29", 1.1020)])

    monkeypatch.setattr(LP, "_fetch_m1_labelled", fake_m1)
    monkeypatch.setattr(LP, "_fetch_duka_ticks", fake_ticks)
    cfg = {"general": {"timeframe_minutes": 15}, "symbols": {"EURUSD": {"dukascopy": "eurusd"}},
           "live_price": {"firmtape": {"enabled": False}}}
    p = _payload("EURUSD")
    info = asyncio.run(LP.enrich_live_prices([p], cfg, NOW))
    c = p["context"]
    assert c["live_price_source"] == "dukascopy:tick"
    assert isinstance(c["live_price_age_s"], float) and c["live_price_age_s"] > 0
    assert p["forming_candle"]["close"] == pytest.approx(1.102)
    assert p["forming_candle"]["time"] == int(pd.Timestamp("2026-11-11 08:15:00", tz="UTC").timestamp())
    assert info["prices"]["EURUSD"]["source"] == "dukascopy:tick"
    # diagnostics stay out of the Gemini payload
    assert "live_price_age_s" not in slim_gemini_context(c) and "live_price_source" in slim_gemini_context(c)


def test_enrich_live_prices_tick_failure_uses_m1(monkeypatch):
    async def fake_m1(symbol, cfg, now):
        return _m1_window(), "dukascopy:m1"

    async def bad_ticks(ticker, since, timeout):
        raise RuntimeError("dukascopy tick eurusd: empty")

    monkeypatch.setattr(LP, "_fetch_m1_labelled", fake_m1)
    monkeypatch.setattr(LP, "_fetch_duka_ticks", bad_ticks)
    cfg = {"general": {"timeframe_minutes": 15}, "symbols": {"EURUSD": {"dukascopy": "eurusd"}}}
    p = _payload("EURUSD")
    asyncio.run(LP.enrich_live_prices([p], cfg, NOW))
    assert p["context"]["live_price_source"] == "dukascopy:m1"
    assert p["context"]["live_price_ts_utc"].startswith("2026-11-11T08:21:00")


# ------------------------------------------------------------------ FirmTape NQ (US100)
RTH = datetime(2026, 11, 11, 16, 30, 0, tzinfo=UTC)       # 11:30 ET, Wednesday
CFG = {"live_price": {"firmtape": {"enabled": True}}}


def _ft(price=28560.0, as_of=RTH - timedelta(seconds=1), **kw):
    e = {"sym": "NQ", "price": price, "price_as_of": as_of.isoformat(), "age_s": 40, "session": "open", "error": None}
    e.update(kw)
    return {"levels": [e]}


def _us100_payload(live=28460.0, ts=RTH - timedelta(seconds=2)):
    p = {"symbol": "US100", "context": {"digits": 2, "live_price": live, "live_price_ts_utc": ts.isoformat(),
                                        "live_price_source": "dukascopy:tick", "live_price_stale": False,
                                        "forming_m15_high": 28465.0, "forming_m15_low": 28440.0}}
    p["forming_candle"] = {"time": 0, "open": 28450.0, "high": 28465.0, "low": 28440.0, "close": live}
    return p


def test_firmtape_override_uses_rolling_median_basis():
    tr = FT.BasisTracker()
    p = _us100_payload()
    out = FT.apply_firmtape_live(p, _ft(), None, RTH, CFG, tracker=tr)
    c = p["context"]
    assert out["firmtape"] == "ok" and out["basis"] == pytest.approx(-100.0)
    assert c["live_price_source"] == "firmtape:NQ" and c["live_price"] == pytest.approx(28460.0)
    assert c["live_price_dukascopy"] == 28460.0
    # second sample with a different basis -> median of (-100, -90) = -95; NQ moved to 28580 -> 28485
    p2 = _us100_payload(live=28470.0, ts=RTH + timedelta(seconds=60))
    t2 = RTH + timedelta(seconds=61)
    FT.apply_firmtape_live(p2, _ft(price=28560.0, as_of=t2 - timedelta(seconds=1)), None, t2, CFG, tracker=tr)
    assert p2["context"]["live_basis"] == pytest.approx(-95.0) and p2["context"]["live_basis_n"] == 2
    p3 = _us100_payload(live=28400.0, ts=RTH - timedelta(minutes=5))   # not aligned -> no new sample
    t3 = RTH + timedelta(seconds=120)
    FT.apply_firmtape_live(p3, _ft(price=28580.0, as_of=t3), None, t3, CFG, tracker=tr)
    assert p3["context"]["live_basis_n"] == 2 and p3["context"]["live_price"] == pytest.approx(28485.0)
    assert p3["forming_candle"]["high"] == pytest.approx(28485.0) and p3["forming_candle"]["close"] == 28485.0
    assert p3["context"]["forming_m15_high"] == pytest.approx(28485.0)


@pytest.mark.parametrize("data,err,now,reason", [
    (_ft(), None, datetime(2026, 11, 11, 22, 0, tzinfo=UTC), "outside_ny_rth"),
    (_ft(), None, datetime(2026, 11, 14, 16, 30, tzinfo=UTC), "outside_ny_rth"),     # Saturday
    (_ft(session="closed"), None, RTH, "session_closed"),
    (_ft(age_s=301), None, RTH, "stale_age_s_301"),
    (_ft(error="symbol unavailable"), None, RTH, "symbol_error"),
    (_ft(as_of=RTH - timedelta(seconds=30)), None, RTH, "price_stale"),
    (None, "HTTP 401", RTH, "request_failed"),
    ({"levels": []}, None, RTH, "no_data"),
])
def test_firmtape_gate_falls_back_to_dukascopy(data, err, now, reason):
    p = _us100_payload()
    out = FT.apply_firmtape_live(p, data, err, now, CFG, tracker=FT.BasisTracker())
    assert out["firmtape"].startswith(reason)
    assert p["context"]["live_price_source"] == "dukascopy:tick" and p["context"]["live_price"] == 28460.0


def test_firmtape_no_aligned_basis_keeps_dukascopy():
    p = _us100_payload(ts=RTH - timedelta(minutes=3))
    out = FT.apply_firmtape_live(p, _ft(), None, RTH, CFG, tracker=FT.BasisTracker())
    assert out["firmtape"] == "no_aligned_basis" and p["context"]["live_price_source"] == "dukascopy:tick"


def test_firmtape_other_symbols_untouched_and_api_error_in_200():
    p = {"symbol": "EURUSD", "context": {"live_price": 1.1}}
    assert FT.apply_firmtape_live(p, _ft(), None, RTH, CFG)["firmtape"] == "n/a"

    async def f(_):
        return {"error": "invalid key"}
    data, err = asyncio.run(FT.fetch_levels(CFG, fetch=f))
    assert data is None and "invalid key" in err


def test_fetch_levels_without_key_never_raises(monkeypatch):
    monkeypatch.delenv("FIRMTAPE_API_KEY", raising=False)
    assert asyncio.run(FT.fetch_levels(CFG)) == (None, "no_api_key")


def test_no_ticks_when_bars_are_not_dukascopy(monkeypatch):
    called = []

    async def fake_m1(symbol, cfg, now):
        return _m1_window(), "binance:m1"

    async def fake_ticks(ticker, since, timeout):
        called.append(ticker)
        return _ticks([("2026-11-11 08:21:29", 1.1020)])

    monkeypatch.setattr(LP, "_fetch_m1_labelled", fake_m1)
    monkeypatch.setattr(LP, "_fetch_duka_ticks", fake_ticks)
    cfg = {"general": {"timeframe_minutes": 15}, "symbols": {"BTCUSD": {"dukascopy": "btcusd"}}}
    p = {**_payload("BTCUSD"), "source": "binance"}
    asyncio.run(LP.enrich_live_prices([p], cfg, NOW))
    assert called == [] and p["context"]["live_price_source"] == "binance:m1"
