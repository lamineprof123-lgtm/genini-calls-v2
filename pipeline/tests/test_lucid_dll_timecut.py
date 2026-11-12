"""DLL room counts open + pending risk at queue AND at fill; time cut consistent with extended targets."""
import copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.lucid import LucidSim, time_cut_rule

UTC = timezone.utc
T = datetime(2026, 11, 11, 18, 16, 30, tzinfo=UTC)      # Wed 13:16 New York (RTH)


@pytest.fixture
def sim(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg.setdefault("exit_manager", {})["uniform_1atr_exits"] = False
    cfg["lucid"]["slippage_ticks"] = 0
    cfg["lucid"]["exit_mode"] = "server"; cfg["lucid"].pop("reset_archive", None)  # legacy server-target sim
    s = LucidSim(cfg, tmp_path / "state", tmp_path / "logs")
    s.s["last_processed"] = T.replace(second=0).isoformat()
    s._roll_day(T)
    return s


def _rec(t, sym="US100", px=28500.0, atr=60.0, action="BUY"):
    sl = px - 48.75 if action == "BUY" else px + 48.75
    return {"run_at_utc": t.isoformat(), "signals": [{
        "symbol": sym, "action": action, "model_action": action, "entry": px, "stop_loss": sl,
        "take_profit": px + 5, "confidence": 0.8, "rr_local": 0.1, "last_close": px, "atr": atr,
        "model_risk_pct": None, "reasons": [], "signal_candle": {"high": px + 2, "low": px - 46},
        "levels": [{"type": "resistance", "label": "Session High", "price": px + 4.83}]}]}


def _bars(sym, t, px):
    return {sym: pd.DataFrame([[px, px + 1, px - 1, px]], columns=["open", "high", "low", "close"],
                              index=pd.DatetimeIndex([t]))}


def _live_1817_state(sim):
    """Account as at 18:17 UTC: realized -693.10, open GC short risking $1,010, DLL $1,800."""
    a = sim.s["account"]
    a["balance"] = round(100000 - 693.1 - 2.3, 2); a["realized_pnl"] = -693.1
    sim.s["day"]["realized"] = -693.1; sim.s["day"]["start_balance"] = 100000.0
    sim.s["positions"].append({"id": 9, "symbol": "XAUUSD", "contract": "GC", "qty": 1, "side": -1, "entry": 3991.2,
                               "stop": 4001.3, "target": 3984.7, "entry_time": "2026-11-11T18:07:00+00:00",
                               "risk_usd": 1010.0, "target_usd": 645.4, "mfe": 0.0, "mae": 0.0, "mfe_min": 0.0,
                               "time_cut_deadline": None})
    sim.s["last_price"]["XAUUSD"] = 3991.2


def _events(sim):
    return [json.loads(x) for f in Path(sim.logs_dir).rglob("lucid_events.jsonl") for x in f.read_text().splitlines()]


def test_1817_case_skipped_at_queue_not_pending(sim):
    _live_1817_state(sim)
    assert sim.risk_budget() < 381
    out = sim.on_record(_rec(T), T)
    assert out[0]["taken"] is False and out[0]["why"].startswith("DLL room at queue")
    assert sim.s["pending"] == []


def test_pending_cancelled_at_fill_when_dll_room_gone(sim):
    out = sim.on_record(_rec(T), T)
    assert out[0]["taken"] and sim.s["pending"][0]["reserved_risk"] > 0
    _live_1817_state(sim)                       # losses + another position before the fill minute
    t1 = T.replace(second=0) + timedelta(minutes=1)
    sim.process(_bars("US100", t1, 28500.0), t1 + timedelta(minutes=1))
    assert not any(p["symbol"] == "US100" for p in sim.s["positions"]) and sim.s["pending"] == []
    ev = [e for e in _events(sim) if e["event"] == "pending_cancelled"]
    assert ev and "DLL room" in ev[0]["why"] and ev[0]["open_risk"] == pytest.approx(1010.0)


def test_pending_reservation_counts_for_next_signal(sim):
    a = sim.s["account"]
    a["balance"] = 100000 - 600.0; sim.s["day"]["realized"] = -600.0; sim.s["day"]["start_balance"] = 100000.0
    # DLL room 1200 - 50 safety = 1150: the US100 pending reserves its planned risk; XAU then has < $381 left
    out = sim.on_record(_rec(T), T)
    assert out[0]["taken"], out
    res = sim.s["pending"][0]["reserved_risk"]
    assert 381 <= res <= 1150
    assert sim.risk_budget() == pytest.approx(min(1500, 1200 - res - 50))
    out2 = sim.on_record(_rec(T, sym="XAUUSD", px=4000.0, atr=6.5, action="SELL"), T)
    assert sim.risk_budget() < 381
    assert not out2[0]["taken"] and "DLL room at queue" in out2[0]["why"] and "pending $" in out2[0]["why"]


def test_filled_entry_risk_never_exceeds_dll_room(sim):
    a = sim.s["account"]
    a["balance"] = 100000 - 693.1; sim.s["day"]["realized"] = -693.1; sim.s["day"]["start_balance"] = 100000.0
    sim.on_record(_rec(T), T)
    t1 = T.replace(second=0) + timedelta(minutes=1)
    sim.process(_bars("US100", t1, 28500.0), t1 + timedelta(minutes=1))
    for p in sim.s["positions"]:
        assert p["risk_usd"] <= 1800 - 693.1 - 50 + 1e-6


def test_time_cut_rule_scalp_unchanged_extended_scaled():
    sc = {"time_cut_candles": 2, "mfe_min_atr": 0.25, "mfe_min_target_frac": 0.3333, "time_cut_candles_extended": 4}
    bars, mfe, rule = time_cut_rule(sc, 68.487, 5.0, "fixed")
    assert bars == 2 and mfe == pytest.approx(17.12175) and "scalp target" in rule
    # Trade 1 (NQ, 37.75-pt extended target, ATR 68.487, MFE 16.36): threshold 12.58 -> would NOT be cut
    bars, mfe, rule = time_cut_rule(sc, 68.487, 37.75, "extended:aim_net")
    assert bars == 4 and mfe == pytest.approx(0.3333 * 37.75) and 16.359 >= mfe and "extended target" in rule
    bars, mfe, _ = time_cut_rule(sc, 6.542, 6.5, "extended:min_net")      # GC: 0.25 ATR (1.64) < 1/3 target
    assert bars == 4 and mfe == pytest.approx(1.6355)
    assert time_cut_rule(sc, 44.0, 29.75, "structure:next:Prev High")[0] == 4


def test_extended_entry_logs_time_cut_rule(sim):
    sim.on_record(_rec(T), T)
    t1 = T.replace(second=0) + timedelta(minutes=1)
    sim.process(_bars("US100", t1, 28500.0), t1 + timedelta(minutes=1))
    p = sim.s["positions"][0]
    assert p["target_source"].startswith("extended") and p["time_cut_candles"] == 4
    assert p["time_cut_deadline"] == (t1 + timedelta(minutes=60)).isoformat()
    ent = [e for e in _events(sim) if e["event"] == "entry"][0]
    assert ent["time_cut_rule"].startswith("4x15m (extended target)") and ent["mfe_min"] == pytest.approx(p["mfe_min"])
