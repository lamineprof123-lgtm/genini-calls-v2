"""Entry fills at the LIVE quote at fill time (same feed as the exit checks / hard stop) + 1 tick, never at a
stale M1 close. Regression for 12 Nov 2026: the 12:17Z gold short was booked at 3942.9 (the 12:15 M1 close)
while the market was ~3946.2-3947.0, so the -$500 stop fired 16 s after entry."""
from __future__ import annotations
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import live_price as LP
from signals import lucid as LU
from signals.config import load_config
from signals.hard_stop import hard_stop_level, net_at
from signals.lucid import LucidSim, apply_due_fills, normalize_fill_quotes

UTC = timezone.utc
T_SIG = datetime(2026, 11, 12, 12, 16, 39, tzinfo=UTC)
T_NOW = datetime(2026, 11, 12, 12, 17, 5, tzinfo=UTC)
STALE_M1_CLOSE = 3942.96        # 12:15 M1 close: what the old path booked (3943.0 - 1 tick = 3942.9)
MARKET = 3947.0                 # Dukascopy bid at 12:17:05


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


@pytest.fixture
def sim(cfg, tmp_path):
    s = LucidSim(cfg, tmp_path / "state" / "lucid", tmp_path / "logs")
    s.s["last_processed"] = datetime(2026, 11, 12, 12, 16, 0, tzinfo=UTC).isoformat()
    s._roll_day(T_SIG)
    return s


def _rec(t=T_SIG, sym="XAUUSD", px=3943.0, atr=7.1, action="SELL"):
    return {"run_at_utc": t.isoformat(), "signals": [{
        "symbol": sym, "action": action, "model_action": action, "entry": px,
        "stop_loss": None, "take_profit": None, "confidence": 0.8, "rr_local": None,
        "last_close": px, "atr": atr, "model_risk_pct": None, "reasons": [],
        "session": "LONDON_SESSION",
        "signal_candle": {"high": px + 2, "low": px - 2}, "levels": []}]}


def _lagging_closed(sym="XAUUSD"):
    # closed-M1 feed one minute behind: last bar 12:15 (close = the stale price the old path used)
    return {sym: pd.DataFrame([{"open": 3943.42, "high": 3943.64, "low": 3942.04, "close": STALE_M1_CLOSE}],
                              index=pd.DatetimeIndex([datetime(2026, 11, 12, 12, 15, tzinfo=UTC)]))}


def _q(price=MARKET, at=T_NOW, source="dukascopy:tick", age=0.6, stale=False):
    return {"price": price, "ts": (at - timedelta(seconds=age)).isoformat(), "fetched_at": at.isoformat(),
            "source": source, "age_s": age, "stale": stale}


def _events(tmp_path, name="lucid_events.jsonl"):
    return [json.loads(l) for f in (tmp_path / "logs").glob(f"*/{name}") for l in open(f)]


def test_short_fills_at_live_quote_minus_one_tick_not_stale_m1_close(sim, cfg, tmp_path):
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    filled = apply_due_fills(sim, _lagging_closed(), T_NOW, live_prices={"XAUUSD": _q()})
    assert filled
    p = sim.s["positions"][0]
    assert p["entry"] == pytest.approx(MARKET - 0.1)                 # SELL: 1 tick adverse (lower)
    assert abs(p["entry"] - STALE_M1_CLOSE) > 3                       # the old booking is gone
    assert p["entry_time"] == T_NOW.isoformat()                       # fill time = quote time, not 12:17:00
    assert p["fill_source"] == "live_quote:dukascopy:tick"
    fc = p["fill_check"]
    assert fc["quote_price"] == MARKET and fc["booked"] == p["entry"] and fc["within_slippage"] is True
    assert fc["adverse_ticks_vs_quote"] == pytest.approx(1.0) and fc["quote_source"] == "dukascopy:tick"
    # stop consistent with the booked entry; at the market price the trade is only 1 tick + commission down
    assert p["hard_stop_px"] == hard_stop_level(cfg, p) and p["hard_stop_px"] > MARKET + 2
    assert net_at(cfg, p, MARKET) == pytest.approx(-0.1 * 100 * p["qty"] - 2 * 2.30 * p["qty"])
    feed = sim.s["book_feed"][-1]
    assert feed["entry"] == p["entry"] and feed["hard_stop_px"] == p["hard_stop_px"]
    assert feed["fill_source"] == p["fill_source"]
    ev = [e for e in _events(tmp_path) if e["event"] == "entry"][-1]
    assert ev["fill_source"] == p["fill_source"] and ev["fill_check"]["within_slippage"] is True
    assert ev["t"] == T_NOW.isoformat()


def test_long_fills_at_live_quote_plus_one_tick(sim):
    assert sim.on_record(_rec(sym="US100", px=25000.0, atr=40.0, action="BUY"), T_SIG)[0]["taken"]
    closed = {"US100": pd.DataFrame([{"open": 24990.0, "high": 24995.0, "low": 24985.0, "close": 24990.0}],
                                    index=pd.DatetimeIndex([datetime(2026, 11, 12, 12, 15, tzinfo=UTC)]))}
    apply_due_fills(sim, closed, T_NOW, live_prices={"US100": _q(25010.5)})
    p = sim.s["positions"][0]
    assert p["entry"] == pytest.approx(25010.75) and p["fill_check"]["within_slippage"]


@pytest.mark.parametrize("q, why", [
    (_q(STALE_M1_CLOSE, source="dukascopy:m1", age=65.0), "not a tick-level feed"),
    (_q(source="fallback_last_close:m1_fetch_failed", stale=True), "stale"),
    (_q(age=120.0), "s old"),
    (None, "no live quote"),
])
def test_no_fill_without_a_fresh_tick_quote(sim, tmp_path, q, why):
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    apply_due_fills(sim, _lagging_closed(), T_NOW, live_prices={"XAUUSD": q} if q else {})
    assert not sim.s["positions"] and len(sim.s["pending"]) == 1
    ev = [e for e in _events(tmp_path) if e["event"] == "fill_deferred"]
    assert len(ev) == 1 and why in ev[0]["why"]
    # the next 18 s replay with a fresh tick quote fills at that quote
    t2 = T_NOW + timedelta(seconds=18)
    apply_due_fills(sim, _lagging_closed(), t2, live_prices={"XAUUSD": _q(3946.5, at=t2)})
    p = sim.s["positions"][0]
    assert p["entry"] == pytest.approx(3946.4) and p["entry_time"] == t2.isoformat()


def test_deferral_logged_once_per_reason(sim, tmp_path):
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    for k in range(3):
        t = T_NOW + timedelta(seconds=18 * k)
        apply_due_fills(sim, _lagging_closed(), t,
                        live_prices={"XAUUSD": _q(at=t, source="dukascopy:m1", age=70)})
    assert [e["event"] for e in _events(tmp_path)].count("fill_deferred") == 1
    assert sim.s["pending"][0]["fill_deferrals"] == 3


def test_quote_in_a_later_minute_fills_there_and_entry_bar_range_is_ignored(sim):
    """Replay starts before the quote minute (restart / feed gap): the fill waits for the quote's minute, and
    that minute's (mostly pre-entry) M1 range must not feed MFE/MAE or the stop."""
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    at = datetime(2026, 11, 12, 12, 18, 20, tzinfo=UTC)
    bars = {"XAUUSD": pd.DataFrame(
        [{"open": 3943.0, "high": 3943.5, "low": 3942.5, "close": 3943.2},
         {"open": 3946.0, "high": 3960.0, "low": 3930.0, "close": 3947.0}],      # 12:18: huge pre-entry range
        index=pd.DatetimeIndex([datetime(2026, 11, 12, 12, 17, tzinfo=UTC), datetime(2026, 11, 12, 12, 18, tzinfo=UTC)]))}
    apply_due_fills(sim, bars, T_NOW, live_prices={"XAUUSD": _q(at=at)})
    p = sim.s["positions"][0]
    assert p["entry_time"] == at.isoformat() and p["entry"] == pytest.approx(MARKET - 0.1)
    assert p["mfe"] == 0.0 and p["mae"] == 0.0 and sim.s["positions"]          # not stopped by 3960 pre-entry


def test_offline_replay_without_quotes_keeps_bar_open_fill(sim):
    """process() without the live path wired (offline replays / older tests) still fills at the bar open."""
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    bars = {"XAUUSD": pd.DataFrame([{"open": 3946.2, "high": 3947.4, "low": 3946.1, "close": 3947.0}],
                                   index=pd.DatetimeIndex([datetime(2026, 11, 12, 12, 17, tzinfo=UTC)]))}
    sim.process(bars, datetime(2026, 11, 12, 12, 18, tzinfo=UTC))
    p = sim.s["positions"][0]
    assert p["fill_source"] == "m1_bar_open" and p["entry"] == pytest.approx(3946.1)


def test_normalize_plain_float_is_an_injected_quote_at_now():
    q = normalize_fill_quotes({"XAUUSD": 3947.0, "EURUSD": None}, T_NOW)
    assert list(q) == ["XAUUSD"] and q["XAUUSD"]["injected"] and q["XAUUSD"]["fetched_at"] == T_NOW.isoformat()
    assert LP.fill_quote_problem(q["XAUUSD"]) is None


def test_lucid_step_fetches_fill_quotes_from_exit_check_live_path(cfg, tmp_path, monkeypatch):
    """End to end: lucid_replay with a due pending calls live_price.fetch_fill_quotes (not the M1 close)."""
    cfg["general"]["state_dir"] = str(tmp_path / "state")
    sim = LucidSim(cfg)
    sim.s["last_processed"] = datetime(2026, 11, 12, 12, 16, 0, tzinfo=UTC).isoformat()
    sim._roll_day(T_SIG)
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    sim.save()
    asked = []

    async def fake_m1(ticker, since, now, timeout=60, include_forming=False):
        return _lagging_closed()["XAUUSD"]

    async def fake_quotes(symbols, c, now=None):
        asked.append(list(symbols))
        return {"XAUUSD": _q()}

    monkeypatch.setattr(LU, "fetch_m1", fake_m1)
    monkeypatch.setattr(LP, "fetch_fill_quotes", fake_quotes)
    out = asyncio.run(LU.lucid_replay(cfg, T_NOW))
    assert asked == [["XAUUSD"]] and out["filled"] == [1]
    p = LucidSim(cfg).s["positions"][0]
    assert p["entry"] == pytest.approx(MARKET - 0.1) and p["fill_check"]["within_slippage"]


def test_fetch_fill_quotes_uses_enrich_live_prices_tick_path(monkeypatch):
    now = datetime(2026, 11, 12, 12, 17, 5, tzinfo=UTC)
    m1 = pd.DataFrame({"open": [3943.0], "high": [3943.6], "low": [3942.0], "close": [STALE_M1_CLOSE], "volume": [1.0]},
                      index=pd.DatetimeIndex([datetime(2026, 11, 12, 12, 15, tzinfo=UTC)]))

    async def fake_m1(symbol, cfg, now):
        return m1, "dukascopy:m1"

    async def fake_ticks(ticker, since, timeout):
        idx = pd.to_datetime(["2026-11-12 12:16:59", "2026-11-12 12:17:04.5"], utc=True, format="ISO8601")
        return pd.DataFrame({"bid": [3946.9, MARKET], "ask": [3947.2, 3947.3]}, index=idx)

    monkeypatch.setattr(LP, "_fetch_m1_labelled", fake_m1)
    monkeypatch.setattr(LP, "_fetch_duka_ticks", fake_ticks)
    cfg = {"general": {"timeframe_minutes": 15}, "symbols": {"XAUUSD": {"dukascopy": "xauusd", "digits": 2}},
           "live_price": {"firmtape": {"enabled": False}}}
    q = asyncio.run(LP.fetch_fill_quotes(["XAUUSD"], cfg, now))["XAUUSD"]
    assert q["price"] == MARKET and q["source"] == "dukascopy:tick" and q["stale"] is False

    async def bad_ticks(ticker, since, timeout):
        raise RuntimeError("tick feed down")
    monkeypatch.setattr(LP, "_fetch_duka_ticks", bad_ticks)
    q = asyncio.run(LP.fetch_fill_quotes(["XAUUSD"], cfg, now))["XAUUSD"]
    assert q["source"] == "dukascopy:m1" and q["price"] == STALE_M1_CLOSE
    assert "tick-level" in LP.fill_quote_problem(q)                   # the stale M1 close can never fill


def _m1bars(start, rows):
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def test_exit_books_ignore_pre_entry_range_of_the_fill_minute(cfg):
    """Books copy the live-quote fill (entry_time 12:17:05). The 12:17 M1 bar's spike to 3947.5 happened before the
    fill: no MFE/MAE from it and no stop; the 12:18 bar counts."""
    from signals.exit_manager import Books
    b = Books(cfg)
    p = {"id": 1, "symbol": "XAUUSD", "contract": "GC", "qty": 2, "side": -1, "entry": 3946.9, "stop": None,
         "entry_time": T_NOW.isoformat(), "scan_from": T_NOW.isoformat(), "atr": 7.1,
         "hard_stop_px": 3949.4, "hard_stop_armed_at": T_NOW.isoformat(), "mfe": 0.0, "mae": 0.0}
    bars = _m1bars("2026-11-12 12:17", [(3946.2, 3949.9, 3946.1, 3947.0), (3946.9, 3947.2, 3946.0, 3946.5)])
    assert b.scan_m1(p, bars) is None                       # 3949.9 pre-entry high must not stop it
    assert p["mae"] == pytest.approx(0.3) and p["mfe"] == pytest.approx(0.9)    # from the 12:18 bar only


def test_claude_advance_ignores_pre_entry_range_of_the_fill_minute():
    from signals.claude_rules import advance, rules_cfg
    from signals.claude_sim import claude_cfg
    r = rules_cfg(claude_cfg(load_config()))
    t = {"side": -1, "entry": 3946.9, "stop": 3949.0, "init_stop": 3949.0, "atr": 7.1, "tick": 0.1,
         "entry_time": T_NOW.isoformat(), "scan_from": T_NOW.isoformat(), "comm_pts": 0.046,
         "mfe_pts": 0.0, "mae_pts": 0.0, "floor": None}
    bars = _m1bars("2026-11-12 12:17", [(3946.2, 3949.9, 3946.1, 3947.0), (3946.9, 3947.2, 3946.0, 3946.5)])
    assert advance(t, bars, r, now=datetime(2026, 11, 12, 12, 19, 30, tzinfo=UTC)) is None
    assert t["mae_pts"] == pytest.approx(0.3)
