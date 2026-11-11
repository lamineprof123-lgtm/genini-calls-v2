"""exit_mode = "gemini": Gemini entry opens, the exit manager's Gemini CLOSE closes. No server target / time cut /
VWAP-EMA cross exits; hard stop, DLL (open + pending), 16:45 flat and max hold kept; sizing by stop only."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.lucid import LucidSim
from signals.exit_manager import BOOK_1, ExitManager
from signals.site import latest_signal_from_record

UTC = timezone.utc
T = datetime(2026, 11, 11, 16, 0, 30, tzinfo=UTC)      # Wed 11:00 New York


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["lucid"]["slippage_ticks"] = 0
    assert c["lucid"]["exit_mode"] == "gemini"
    return c


@pytest.fixture
def sim(cfg, tmp_path):
    s = LucidSim(cfg, tmp_path / "state" / "lucid", tmp_path / "logs")
    s.s["last_processed"] = T.replace(second=0).isoformat()
    s._roll_day(T)
    return s


def _rec(t, sym="US100", px=28500.0, atr=60.0, action="BUY", stop_dist=46.0):
    sl = px - stop_dist if action == "BUY" else px + stop_dist
    return {"run_at_utc": t.isoformat(), "signals": [{
        "symbol": sym, "action": action, "model_action": action, "entry": px, "stop_loss": sl,
        "take_profit": px + 5, "confidence": 0.8, "rr_local": 0.1, "last_close": px, "atr": atr,
        "model_risk_pct": None, "reasons": [], "signal_candle": {"high": px + 2, "low": px - stop_dist + 2},
        "levels": []}]}


def _bars(sym, rows):
    return {sym: pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close"],
                              index=pd.DatetimeIndex([r[0] for r in rows]))}


def _events(sim):
    return [json.loads(x) for f in Path(sim.logs_dir).rglob("lucid_events.jsonl") for x in f.read_text().splitlines()]


def test_size_by_stop_full_size_no_381_floor(sim):
    us = sim.size_trade("US100", 1, 44.0, tdist=5.0, sdist=46.0)
    assert (us["contract"], us["qty"], us["risk_usd"]) == ("NQ", 1, 920.0) and us["tdist"] is None
    assert us["target_usd"] is None and "no $381 floor" in us["sizing"]
    gc = sim.size_trade("XAUUSD", -1, 5.3, tdist=1.25, sdist=4.83)
    assert (gc["contract"], gc["qty"], gc["risk_usd"]) == ("GC", 3, 1470.0)
    eu = sim.size_trade("EURUSD", 1, 0.0005, tdist=0.00125, sdist=0.000457)
    assert eu["contract"] == "6E" and eu["qty"] == 24 and eu["risk_usd"] == pytest.approx(1500.0)
    assert sim.size_trade("US100", 1, 44.0, sdist=80.0) is None and sim.last_size_why.startswith("risk cap")


def test_1817_dll_case_skipped_at_queue(sim):
    a = sim.s["account"]
    a["balance"] = 99304.6; sim.s["day"]["realized"] = -693.1; sim.s["day"]["start_balance"] = 100000.0
    sim.s["positions"].append({"id": 9, "symbol": "XAUUSD", "contract": "GC", "qty": 1, "side": -1, "entry": 3991.2,
                               "stop": 4001.3, "target": None, "entry_time": T.isoformat(), "risk_usd": 1010.0,
                               "exit_mode": "gemini"})
    sim.s["last_price"]["XAUUSD"] = 3991.2
    out = sim.on_record(_rec(T, stop_dist=49.0), T)
    assert not out[0]["taken"] and out[0]["why"].startswith("DLL room at queue") and not sim.s["pending"]


def _enter(sim, px=28500.0):
    assert sim.on_record(_rec(T, px=px), T)[0]["taken"]
    t1 = T.replace(second=0) + timedelta(minutes=1)
    sim.process(_bars("US100", [(t1, px, px + 1, px - 1, px)]), t1 + timedelta(minutes=1))
    assert sim.s["positions"]
    return sim.s["positions"][0], t1


def test_fill_has_no_target_no_time_cut_and_feeds_books(sim):
    p, t1 = _enter(sim)
    assert p["target"] is None and p["time_cut_deadline"] is None and p["exit_mode"] == "gemini"
    assert sim.s["book_feed"][-1]["id"] == p["id"] and sim.s["book_feed"][-1]["target"] is None
    # 60 minutes far above any old target and below VWAP-ish chop: still open (no target / time cut / cross)
    rows = [(t1 + timedelta(minutes=i), 28510, 28700, 28490, 28520) for i in range(1, 60)]
    sim.process(_bars("US100", rows), t1 + timedelta(minutes=60))
    assert sim.s["positions"], "only Gemini / hard stop / max hold / flat may close"
    ent = [e for e in _events(sim) if e["event"] == "entry"][0]
    assert ent["time_cut_rule"].startswith("none: exit = Gemini CLOSE")


def test_max_hold_90_and_hard_stop(sim):
    p, t1 = _enter(sim)
    rows = [(t1 + timedelta(minutes=i), 28510, 28520, 28490, 28515) for i in range(1, 95)]
    sim.process(_bars("US100", rows), t1 + timedelta(minutes=95))
    assert not sim.s["positions"]
    tr = [json.loads(x) for f in Path(sim.logs_dir).rglob("lucid_trades.jsonl") for x in f.read_text().splitlines()]
    assert tr[-1]["reason"] == "max_hold_90m"


def test_close_request_from_exit_manager_closes_lucid(sim, cfg, tmp_path):
    p, t1 = _enter(sim)
    sim.save()
    st = tmp_path / "state"
    rows = [[28500, 28512, 28498, 28510]] * 3

    async def snapshot(sym, c, now):
        idx = pd.date_range(t1, periods=3, freq="1min", tz="UTC")
        df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)
        return {"symbol": sym, "context": {"digits": 2, "live_price": 28510.0, "live_price_stale": False,
                                           "levels": [], "atr_upper": 28600, "atr_lower": 28400}}, df

    async def render(payload):
        return b"\x89PNG"

    async def decide(prompt, png, pos, m):
        return {"model": "fake", "raw_text": json.dumps({"structure": "wall_rejection", "reason": "test",
                                                         "decision": "CLOSE", "confidence": 0.8})}
    mgr = ExitManager(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path,
                      snapshot=snapshot, render=render, decide=decide)
    now = t1 + timedelta(minutes=2, seconds=40)
    r = asyncio.run(mgr.tick(now))
    assert r["records"][0]["books"][BOOK_1]["action"] == "close"
    reqs = json.loads((st / "lucid" / "close_requests.json").read_text())
    assert len(reqs) == 1 and reqs[0]["id"] == p["id"] and reqs[0]["book"] == BOOK_1   # 2-strike held
    s2 = LucidSim(cfg, st / "lucid", tmp_path / "logs")
    t2 = t1 + timedelta(minutes=1)
    s2.process(_bars("US100", [(t2 + timedelta(minutes=i), 28505, 28512, 28500, 28510) for i in range(0, 3)]),
               t2 + timedelta(minutes=3))
    assert not s2.s["positions"]
    tr = [json.loads(x) for f in Path(tmp_path / "logs").rglob("lucid_trades.jsonl") for x in f.read_text().splitlines()]
    assert tr[-1]["reason"] == "gemini_close" and tr[-1]["exit"] == pytest.approx(28510.0)
    assert any(e["event"] == "gemini_close" for e in _events(s2))


def test_reset2_archive_discards_positions(cfg, tmp_path):
    st = tmp_path / "state" / "lucid"
    c0 = copy.deepcopy(cfg); c0["lucid"]["reset_token"] = "2026-11-11-target-sizing-fix"
    s = LucidSim(c0, st, tmp_path / "logs")
    s.s["account"].update(balance=99304.6, realized_pnl=-693.1, trades=1)
    s.s["positions"] = [{"id": 2, "symbol": "XAUUSD", "contract": "GC", "qty": 1, "side": -1, "entry": 3991.2,
                         "stop": 4001.3, "target": 3984.7, "entry_time": T.isoformat(), "risk_usd": 1010.0}]
    s.s["pending"] = [{"symbol": "US100"}]
    s.save()
    s2 = LucidSim(cfg, st, tmp_path / "logs")
    a = s2.s["account"]
    assert (a["balance"], a["mll_floor"], a["trades"]) == (100000.0, 97000.0, 0)
    assert s2.s["positions"] == [] and s2.s["pending"] == []
    arch = st / "account_pre_reset2_2026-11-11.json"
    assert arch.exists() and json.loads(arch.read_text())["positions"][0]["contract"] == "GC"
    ev = [e for e in _events(s2) if e["event"] == "reset"]
    assert ev[-1]["reason"] == "switch to Gemini entry+exit, removed server target"


def test_site_row_shows_stop_and_gemini_exit(cfg):
    rec = {"run_at_utc": T.isoformat(), "signals": [
        {"symbol": "US100", "action": "BUY", "entry": 28500.0, "stop_loss": 28454.0, "take_profit": 28505.0,
         "rr_local": 0.1, "confidence": 0.8},
        {"symbol": "BTCUSD", "action": "BUY", "entry": 74000.0, "stop_loss": 73800.0, "take_profit": 74100.0,
         "rr_local": 0.5, "confidence": 0.8}]}
    out = latest_signal_from_record(rec, cfg)
    us, btc = out["trades"]
    assert us["take_profit"] is None and us["exit"] == "Gemini" and us["stop_loss"] == 28454.0
    assert btc["take_profit"] == 74100.0 and "exit" not in btc
