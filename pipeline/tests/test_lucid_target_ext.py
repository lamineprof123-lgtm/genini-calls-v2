"""Lucid target extension: the three live skips of 2026-11-11 now enter with sensible size, target and NET;
panel take_profit matches the sim plan; one-shot account reset."""
import copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.lucid import LucidSim, format_held, trade_hold_seconds
from signals.scalp_levels import apply_server_levels, compute_scalp_levels

UTC = timezone.utc
T_NY = datetime(2026, 11, 11, 16, 0, tzinfo=UTC)      # Wed 11:00 New York (RTH)


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c.setdefault("exit_manager", {})["uniform_1atr_exits"] = False
    c["lucid"]["slippage_ticks"] = 0
    c["lucid"]["exit_mode"] = "server"; c["lucid"].pop("reset_archive", None)  # legacy server-target sim
    return c


@pytest.fixture
def sim(cfg, tmp_path):
    s = LucidSim(cfg, tmp_path / "state", tmp_path / "logs")
    s.s["last_processed"] = T_NY.isoformat()
    return s


def _ok(sz, sim):
    assert sz is not None, sim.last_size_why
    assert 381 <= sz["target_usd"] <= 1000
    assert sz["target_usd"] == pytest.approx(sz["target_usd_gross"] - sz["commission_rt"], abs=0.02)
    assert sz["risk_usd"] <= 1500
    assert sz["target_source"] and sz["sizing"]


def test_us100_session_high_4p6_extends_to_next_structure(sim):
    # Live skip: BUY, Session High 4.83 above (target 4.58 after the 1-tick buffer), stop 46 pts, ATR 44.
    entry = 28533.25
    scalp = {"target_source": "structure:Session High",
             "levels": [{"price": entry + 4.83, "label": "Session High"}, {"price": entry + 30.0, "label": "Prev High"}]}
    sz = sim.size_trade("US100", 1, 44.1, tdist=4.58, sdist=46.0, entry=entry, scalp=scalp)
    _ok(sz, sim)
    assert sz["target_source"] == "structure:next:Prev High"
    # full-size only: 1 NQ at the next structure nets $591.50 inside the cap (2 NQ would risk $1,840)
    assert (sz["contract"], sz["qty"]) == ("NQ", 1) and sz["tdist"] == pytest.approx(29.75)
    assert sz["target_usd"] == pytest.approx(591.5) and sz["risk_usd"] == pytest.approx(920.0)


def test_us100_no_structure_beyond_extends_to_aim(sim):
    entry = 28533.25
    scalp = {"target_source": "structure:Session High", "levels": [{"price": entry + 4.83, "label": "Session High"}]}
    sz = sim.size_trade("US100", 1, 44.1, tdist=4.58, sdist=46.0, entry=entry, scalp=scalp)
    _ok(sz, sim)
    assert sz["target_source"] == "extended:aim_net"
    assert (sz["contract"], sz["qty"]) == ("NQ", 1)          # fewest contracts: 1 NQ risks $920
    assert sz["tdist"] == pytest.approx(37.75) and sz["target_usd"] == pytest.approx(751.5)


def test_xauusd_fixed_1p25_extends(sim):
    # Live skip 11:21Z: fixed 1.25 target, structure stop 4.8231, ATR 5.302
    sz = sim.size_trade("XAUUSD", -1, 5.302, tdist=1.25, sdist=4.8231, entry=4040.0,
                        scalp={"target_source": "fixed", "levels": []})
    _ok(sz, sim)
    assert sz["target_source"] == "extended:aim_net"
    assert (sz["contract"], sz["qty"]) == ("GC", 2) and sz["tdist"] == pytest.approx(3.8)
    assert sz["risk_usd"] == pytest.approx(980.0)


def test_eurusd_fixed_0p00125_enters_and_dll_room_skip_is_explained(sim):
    # Live skip 11:01Z: fixed 12.5 pips, stop 4.57 pips. With a full risk budget it sizes at the fixed target.
    sz = sim.size_trade("EURUSD", 1, 0.000536, tdist=0.00125, sdist=0.0004568, entry=1.1100,
                        scalp={"target_source": "fixed", "levels": []})
    _ok(sz, sim)
    assert sz["target_source"] == "fixed" and sz["contract"] == "6E" and sz["qty"] == 5
    # The live account had already lost $1,411.85 of its $1,800 DLL that day -> budget < $381: explicit reason
    sim._roll_day(T_NY)
    sim.s["account"]["balance"] -= 1411.85
    assert sim.size_trade("EURUSD", 1, 0.000536, tdist=0.00125, sdist=0.0004568) is None
    assert sim.last_size_why.startswith("DLL room")


def _rec(t, sym="US100", px=20000.0, atr=44.0, levels=None):
    return {"run_at_utc": t.isoformat(), "signals": [{
        "symbol": sym, "action": "BUY", "model_action": "BUY", "entry": px, "stop_loss": px - 46, "take_profit": px + 5,
        "confidence": 0.8, "rr_local": 0.1, "last_close": px, "atr": atr, "model_risk_pct": None, "reasons": [],
        "signal_candle": {"high": px + 2, "low": px - 43.8}, "levels": levels or [
            {"type": "resistance", "label": "Session High", "price": px + 4.83}]}]}


def test_end_to_end_fill_logs_target_source_and_sizing(sim):
    t = T_NY
    assert sim.on_record(_rec(t), t)[0]["taken"]
    px = 20000.0
    df = pd.DataFrame([[px, px + 1, px - 1, px]], columns=["open", "high", "low", "close"],
                      index=pd.DatetimeIndex([t + timedelta(minutes=1)]))
    sim.process({"US100": df}, t + timedelta(minutes=2))
    assert sim.s["positions"], "should have entered"
    p = sim.s["positions"][0]
    assert p["target_source"].startswith("extended") and 381 <= p["target_usd"] <= 1000 and p["risk_usd"] <= 1500
    assert p["target"] - p["entry"] == pytest.approx(p["telemetry"]["planned"]["tdist"])
    day = t.astimezone(__import__("zoneinfo").ZoneInfo(sim.cfg["general"]["session_tz"])).strftime("%Y-%m-%d")
    ev = [json.loads(x) for x in (sim.logs_dir / day / "lucid_events.jsonl").read_text().splitlines()]
    entry = [e for e in ev if e["event"] == "entry"][0]
    assert entry["target_source"] == p["target_source"] and "fewest" in entry["sizing"]


def test_panel_take_profit_matches_sim_plan(cfg):
    ctx = {"live_price": 20000.0, "atr": 44.0, "digits": 2, "signal_candle": {"high": 20002.0, "low": 19956.2},
           "levels": [{"type": "resistance", "label": "Session High", "price": 20004.83}]}
    sig = apply_server_levels({"action": "BUY", "entry": 20000.0}, ctx, cfg, "US100")
    plan = sig["scalp"]["lucid_plan"]
    assert sig["take_profit"] == pytest.approx(20000.0 + plan["tdist"])
    assert sig["scalp"]["target_source"] == plan["target_source"] and plan["target_source"].startswith("extended")
    # BTC is not a Lucid product: scalp target unchanged
    b = apply_server_levels({"action": "BUY", "entry": 74000.0},
                            {"live_price": 74000.0, "atr": 300.0, "digits": 2,
                             "signal_candle": {"high": 74100, "low": 73800}}, cfg, "BTCUSD")
    assert "lucid_plan" not in b["scalp"] and b["take_profit"] == pytest.approx(74100.0)


def test_reset_token_archives_and_starts_fresh(cfg, tmp_path):
    st = tmp_path / "state"
    cfg["lucid"]["reset_token"] = None
    s = LucidSim(cfg, st, tmp_path / "logs")
    s.s["account"].update(balance=98588.15, realized_pnl=-1411.85, trades=7)
    s.s["pending"] = [{"symbol": "US100"}]
    s.save()
    cfg["lucid"]["reset_token"] = "tok-1"
    cfg["lucid"]["reset_reason"] = "target sizing bug fix"
    s2 = LucidSim(cfg, st, tmp_path / "logs")
    a = s2.s["account"]
    assert (a["balance"], a["highest_close"], a["mll_floor"], a["realized_pnl"], a["trades"]) == \
        (100000.0, 100000.0, 97000.0, 0.0, 0)
    assert s2.s["positions"] == [] and s2.s["pending"] == [] and s2.s["reset_token"] == "tok-1"
    arch = list(st.glob("account_pre_reset_*.json"))
    assert len(arch) == 1 and json.loads(arch[0].read_text())["account"]["balance"] == 98588.15
    # idempotent: loading again does not reset again
    s2.s["account"]["balance"] = 100250.0; s2.save()
    assert LucidSim(cfg, st, tmp_path / "logs").s["account"]["balance"] == 100250.0
    evs = [json.loads(x) for f in (tmp_path / "logs").rglob("lucid_events.jsonl") for x in f.read_text().splitlines()]
    r = [e for e in evs if e["event"] == "reset"]
    assert len(r) == 1 and r[0]["reason"] == "target sizing bug fix" and r[0]["previous"]["balance"] == 98588.15


def test_full_size_only_never_micros(sim, cfg):
    from signals.scalp_levels import plan_target_and_size
    assert all(not n.startswith("M") for n in cfg["lucid"]["contracts"])
    for sym in cfg["lucid"]["symbols"]:
        assert set(cfg["lucid"]["symbols"][sym]["contracts"]) <= {"NQ", "GC", "SI", "6E", "6B"}
    # many shapes: every plan is full-size, 1..max_minis, NET in [381, 1000], risk <= 1500
    for sym, atr, tds, sds in (("US100", 44.0, (2, 5, 10, 20, 40), (5, 10, 30, 46, 70)),
                               ("XAUUSD", 5.3, (0.5, 1.25, 3, 5), (0.8, 2, 4.8, 7)),
                               ("EURUSD", 0.0006, (0.0002, 0.00125), (0.0002, 0.0005, 0.001)),
                               ("GBPUSD", 0.0008, (0.0002, 0.00125), (0.0003, 0.0008, 0.002))):
        for td in tds:
            for sd in sds:
                sz = sim.size_trade(sym, 1, atr, tdist=td, sdist=sd)
                if sz is None:
                    assert "cannot reach" in sim.last_size_why or "no ATR" in sim.last_size_why
                    continue
                assert sz["contract"] in ("NQ", "GC", "6E", "6B") and 1 <= sz["qty"] <= int(sim.L["max_minis"])
                assert 381 <= sz["target_usd"] <= 1000 and sz["risk_usd"] <= 1500
    # a micro spec handed in directly is refused
    plan, why = plan_target_and_size(tdist0=20, sdist=30, atr=44, contracts=[("MNQ", {"point_value": 2, "tick": .25,
                                     "commission": .5, "micro": True})], struct_dists=[], net_min=381, net_aim=750,
                                     net_max=1000, max_risk_usd=1500, budget=1500, room_minis=40)
    assert plan is None and "full-size" in why


def test_gold_tight_stop_skips_when_one_gc_overshoots_risk(sim):
    # 1 GC with a 16-pt stop = $1,600 > $1,500 cap and no micro fallback -> clear skip
    assert sim.size_trade("XAUUSD", 1, 20.0, tdist=3.0, sdist=16.0) is None
    assert "cannot reach $381" in sim.last_size_why and "full-size" in sim.last_size_why


def test_site_lucid_json_post_reset_only(cfg, tmp_path):
    st, lg = tmp_path / "state", tmp_path / "logs"
    cfg["lucid"]["reset_token"] = None
    s = LucidSim(cfg, st, lg)
    old = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
    s._jsonl("lucid_trades.jsonl", old, {"t_exit": old.isoformat(), "symbol": "XAUUSD", "contract": "GC", "qty": 6,
                                         "side": "LONG", "net": -738.6})
    s.save()
    cfg["lucid"]["reset_token"] = "tok-2"; cfg["lucid"]["reset_reason"] = "target sizing bug fix"
    s = LucidSim(cfg, st, lg)
    new = datetime.now(UTC) + timedelta(minutes=1)
    s._jsonl("lucid_trades.jsonl", new, {"t_exit": new.isoformat(), "symbol": "US100", "contract": "NQ", "qty": 1,
                                         "side": "LONG", "net": 751.5, "reason": "target"})
    p = s.write_site(tmp_path / "site")
    j = json.loads(p.read_text())
    assert j["account"]["balance"] == 100000.0 and j["account"]["mll_floor"] == 97000.0
    assert j["account"]["trades_since_reset"] == 1 and j["account"]["realized_pnl_since_reset"] == 751.5
    assert [t["symbol"] for t in j["recent_trades"]] == ["US100"] and j["reset"]["reason"] == "target sizing bug fix"
    assert "full-size" in j["sizing_rule"] and j["paper_only"] is True
    from signals.lucid_report import write_report
    r, md = write_report(lg)
    assert r["eras"]["pre_reset"]["count"] == 1 and r["eras"]["post_reset"]["count"] == 1
    assert r["summary"]["count"] == 1 and "Pre-reset" in md and "Post-reset" in md


def test_format_held():
    assert format_held(None) is None
    assert format_held(0) == "0s"
    assert format_held(45) == "45s"
    assert format_held(120) == "2m 00s"
    assert format_held(134) == "2m 14s"
    assert format_held(3600 + 5 * 60) == "1h 05m"
    assert format_held(3600 + 5 * 60 + 7) == "1h 05m"  # seconds dropped from the hour+ form


def test_site_recent_trades_include_held_and_opened(cfg, tmp_path):
    """lucid.json recent_trades carry entry_time, hold_seconds, and a formatted held string."""
    st, lg = tmp_path / "state", tmp_path / "logs"
    cfg["lucid"]["reset_token"] = "held-test"
    s = LucidSim(cfg, st, lg)
    t0 = datetime.now(UTC) + timedelta(minutes=1)  # after this sim's reset_at
    t1 = t0 + timedelta(minutes=2)          # 2m 00s, hold_seconds already on the close record
    t2 = t0 + timedelta(hours=1, minutes=5, seconds=7)  # backfill from times only
    s._jsonl("lucid_trades.jsonl", t1, {
        "t_exit": t1.isoformat(), "entry_time": t0.isoformat(),
        "symbol": "US100", "contract": "NQ", "qty": 2, "side": "LONG",
        "entry": 28182.75, "exit": 28188.25, "reason": "gemini_close",
        "net": 213.0, "hold_seconds": 120, "balance_after": 100213.0,
    })
    s._jsonl("lucid_trades.jsonl", t2, {
        "t_exit": t2.isoformat(), "entry_time": t0.isoformat(),
        "symbol": "EURUSD", "contract": "6E", "qty": 1, "side": "LONG",
        "net": 10.0,
    })
    j = json.loads(s.write_site(tmp_path / "site").read_text())
    # newest first
    euru, us = j["recent_trades"]
    assert us["symbol"] == "US100" and us["entry_time"] == t0.isoformat()
    assert us["hold_seconds"] == 120 and us["held"] == "2m 00s"
    assert euru["symbol"] == "EURUSD"
    assert euru["hold_seconds"] == trade_hold_seconds(euru) == 3907
    assert euru["held"] == "1h 05m" and euru["entry_time"] == t0.isoformat()


def test_site_open_positions_include_entry_time(cfg, tmp_path):
    """Open-position rows already carry entry_time; Held is computed live in the browser, not snapshotted."""
    st, lg = tmp_path / "state", tmp_path / "logs"
    cfg["lucid"]["reset_token"] = "open-held-test"
    s = LucidSim(cfg, st, lg)
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    s.s["positions"].append({
        "id": 1, "symbol": "US100", "contract": "NQ", "qty": 2, "side": 1,
        "entry": 28189.25, "stop": None, "target": None, "entry_time": t0.isoformat(),
    })
    s.s["last_price"]["US100"] = 28190.0
    j = json.loads(s.write_site(tmp_path / "site").read_text())
    assert len(j["open_positions"]) == 1
    p = j["open_positions"][0]
    assert p["symbol"] == "US100" and p["qty"] == 2 and p["entry_time"] == t0.isoformat()
    assert "held" not in p and p.get("hold_seconds") is None


def test_panel_open_positions_show_live_held():
    html = Path(__file__).resolve().parents[2].joinpath("index.html").read_text()
    assert html.count("<th>Opened</th>") >= 2 and html.count("<th>Held</th>") >= 2
    assert "data-entry-time" in html and "heldSince(p.entry_time)" in html
    assert "tickOpenHeld" in html and "setInterval(tickOpenHeld, 1000)" in html
    assert "$('luPos')" in html
