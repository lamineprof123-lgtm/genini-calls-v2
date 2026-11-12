"""Synthetic-price replay checks for the Lucid 100K paper sim: pass, payout, MLL blow, DLL stop, 16:45 flat."""
import copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.lucid import LucidSim, format_status

UTC = timezone.utc
TUE_0900_ET = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)  # 09:00 New York (EST)
ATR = 40.0

def _shrink(sim, qty=1, sdist=40.0, tdist=20.0):
    """Rewrite the open position to a small full-size NQ risk so legacy DLL/flat assertions stay meaningful."""
    p = sim.s["positions"][0]
    p["contract"] = "NQ"
    p["qty"] = qty
    tick = 0.25
    p["stop"] = round(p["entry"] - p["side"] * sdist, 10)
    p["target"] = round(p["entry"] + p["side"] * tdist, 10)
    p["risk_usd"] = round(sdist * qty * 20.0, 2)
    p["target_usd"] = round(tdist * qty * 20.0 - 2 * 1.75 * qty, 2)
    # Disable scalp time-cut / cross exits for these structural tests
    p["time_cut_deadline"] = None
    p["mfe_min"] = 0.0
    return p



@pytest.fixture
def sim(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg.setdefault("exit_manager", {})["gemini_exit_calls_enabled"] = True
    cfg["lucid"]["slippage_ticks"] = 0
    cfg["lucid"]["exit_mode"] = "server"; cfg["lucid"].pop("reset_archive", None)  # legacy server-target sim
    s = LucidSim(cfg, tmp_path / "state", tmp_path / "logs")
    s.s["last_processed"] = TUE_0900_ET.isoformat()
    return s


def rec(t, action="BUY", sym="US100", px=20000.0, model_action=None, rr=3.0):
    sl, tp = (px - 20, px + 20 * rr) if (model_action or action) == "BUY" else (px + 20, px - 20 * rr)
    return {"run_at_utc": t.isoformat(), "signals": [{
        "symbol": sym, "action": action, "model_action": model_action or action, "entry": px, "stop_loss": sl,
        "take_profit": tp, "confidence": 0.7, "rr_local": rr, "last_close": px, "atr": ATR, "model_risk_pct": None,
        "reasons": []}]}


def bars(rows, sym="US100"):
    """rows: list of (t, o, h, l, c)."""
    df = pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close"],
                      index=pd.DatetimeIndex([r[0] for r in rows]))
    return {sym: df}


def trade(sim, t, win=True, px=20000.0, side="BUY"):
    """Signal at t, fill at t+1m open, then hit target (win) or stop (loss) at t+2m. Returns closing time."""
    sim.process({}, t)
    assert sim.on_record(rec(t, side, px=px), t)[0]["taken"]
    sim.process(bars([(t + timedelta(minutes=1), px, px + 1, px - 1, px)]), t + timedelta(minutes=2))
    p = sim.s["positions"][0]
    lvl = p["target"] if win else p["stop"]
    sim.process(bars([(t + timedelta(minutes=2), px, max(px, lvl) + 0.25, min(px, lvl) - 0.25, lvl)]),
                t + timedelta(minutes=3))
    assert not sim.s["positions"]
    return t + timedelta(minutes=3)


def test_sizing_low_rr(sim):
    """target_usd is NET of RT commission; must be >= $381 net; stop still <= max_risk_usd."""
    assert sim.L["max_minis"] == 6  # LucidPro 100K real aggregate cap
    assert sim.L["target_usd_min"] == 381
    # Fixed US100 tactical target = 5.0 pts; pair with a 10-pt stop so unit_net is large enough
    sz = sim.size_trade("US100", 1, ATR, tdist=20.0, sdist=30.0)
    assert sz is not None and sz["contract"] == "NQ"
    assert sz["target_usd"] >= 381  # NET floor
    assert sz["risk_usd"] <= 1500
    assert "target_usd_gross" in sz and "commission_rt" in sz
    assert sz["target_usd"] == pytest.approx(sz["target_usd_gross"] - sz["commission_rt"], abs=0.02)


def test_sizing_net_of_commission_all_symbols(sim):
    """Every tradable Lucid symbol must size to >= $381 NET at a reachable distance, or return None (skip)."""
    # Distances: fixed tactical (or a bit wider) so the 6-mini cap can clear $381
    cases = {
        "US100":  (40.0, 20.0, 30.0),      # 20 pts on NQ
        "XAUUSD": (8.0,  3.0,  5.0),       # wider than 1.25 fixed
        "EURUSD": (0.0012, 0.00125, 0.0020),
        "GBPUSD": (0.0015, 0.00125, 0.0020),
    }
    for sym, (atr, td, sd) in cases.items():
        sz = sim.size_trade(sym, 1, atr, tdist=td, sdist=sd)
        assert sz is not None, f"{sym} should size at ATR={atr} tdist={td}"
        assert sz["target_usd"] >= 381, f"{sym} net target {sz['target_usd']} < 381"
        assert sz["target_usd"] == pytest.approx(sz["target_usd_gross"] - sz["commission_rt"], abs=0.02)
        assert sz["risk_usd"] <= 1500


def test_sizing_skips_when_381_unreachable_at_cap(sim):
    """A stop so wide that even 1 NQ breaks the $1,500 risk cap -> None (SKIP) with the reason."""
    sz = sim.size_trade("US100", 1, ATR, tdist=5.0, sdist=800.0)
    assert sz is None and "cannot reach $381" in sim.last_size_why


def test_sizing_extends_small_target_instead_of_skipping(sim):
    """max_minis=1, 5-pt target: extended to ~$750 NET on 1 NQ instead of skipping."""
    sim.L["max_minis"] = 1
    sz = sim.size_trade("US100", 1, ATR, tdist=5.0, sdist=10.0)
    assert sz["contract"] == "NQ" and sz["qty"] == 1 and sz["target_source"] == "extended:aim_net"
    assert 381 <= sz["target_usd"] <= 1000 and sz["tdist"] <= ATR


def test_format_status_mentions_size_cap(sim):
    text = format_status(sim.summary(), sim.cfg)
    assert "USER RULE OVERRIDE" not in text
    assert "max 6 full-size contracts" in text and "no micros" in text
    assert "LucidPro 100K cap" in text
    assert "NET of RT commission" in text


def test_rr_only_rejection_is_used_by_sim_but_wrong_side_is_not(sim):
    t = TUE_0900_ET
    sim.process({}, t)
    d = sim.on_record(rec(t, action="HOLD", model_action="BUY", rr=1.0), t)
    assert d[0]["taken"] and "R:R minimum skipped" in d[0]["why"]
    bad = rec(t, action="HOLD", model_action="BUY")
    bad["signals"][0]["stop_loss"] = 20050  # stop above entry on a BUY = nonsense
    sim.s["pending"] = []
    assert not sim.on_record(bad, t)[0]["taken"]
    btc = rec(t, sym="BTCUSD")
    assert not sim.on_record(btc, t)[0]["taken"]


def test_pass_eval_then_payout(sim):
    t = TUE_0900_ET
    while sim.s["account"]["phase"] == "eval":
        t = trade(sim, t + timedelta(minutes=5))
    assert sim.s["ledger"]["passes"] == 1 and sim.s["account"]["no"] == 2
    assert sim.s["account"]["phase"] == "funded" and sim.s["account"]["balance"] == 100000
    # one win per day for 6 days -> consistency ok, withdrawable > $500 above the $103,100 buffer
    day = TUE_0900_ET
    for i in range(1, 9):
        day = day + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        sim.process({}, day)  # crosses the previous 16:45 ET -> EOD bookkeeping
        trade(sim, day)
        if sim.s["ledger"]["payout_count"]:
            break
    sim.process({}, day + timedelta(hours=8))  # through 16:45 ET
    lg = sim.s["ledger"]
    assert lg["payout_count"] == 1
    po = sim.s["account"]["payouts"][0]
    assert 500 <= po["gross"] <= 2500 and po["received"] == round(po["gross"] * 0.9, 2)
    assert sim.s["account"]["balance"] >= 103100 and sim.s["account"]["mll_floor"] == 100100
    print(format_status(sim.summary(), sim.cfg))


def test_mll_breach_blows_and_continues_on_fresh_account(sim):
    t = TUE_0900_ET + timedelta(minutes=5)
    sim.s["account"]["balance"] = 97600.0  # only $600 above the 97,000 floor (losing streak)
    sim.s["day"]["start_balance"] = None
    sim.s["day"]["id"] = None
    sim.process({}, t)
    assert sim.on_record(rec(t), t)[0]["taken"]
    sim.process(bars([(t + timedelta(minutes=1), 20000, 20001, 19999, 20000),
                      (t + timedelta(minutes=2), 19800, 19800, 19700, 19720)]), t + timedelta(minutes=3))
    lg = sim.s["ledger"]
    assert lg["blown_evals"] == 1 and lg["resets"] == 1 and sim.s["account"]["no"] == 2
    assert lg["fees_total"] == pytest.approx(190.40 + 190.0)
    assert sim.s["history"][-1]["result"] == "blown" and sim.s["history"][-1]["fee_charged"]["amount"] == 190.0
    assert sim.s["day"]["locked"] is None  # no pause
    t2 = t + timedelta(minutes=4)
    assert sim.on_record(rec(t2), t2)[0]["taken"]  # trading continues immediately


def test_dll_soft_breach_stops_day_then_resets(sim):
    t = TUE_0900_ET + timedelta(minutes=5)
    sim.process({}, t)
    sim.on_record(rec(t), t)
    sim.process(bars([(t + timedelta(minutes=1), 20000, 20001, 19999, 20000)]), t + timedelta(minutes=2))
    p = _shrink(sim, qty=2, sdist=40.0)  # 2 NQ, risk $1,600
    # Gap through the stop so the day loss clears DLL ($1800) but stays above MLL ($3000)
    p["qty"] = 2
    p["risk_usd"] = round(40 * 2 * 20.0, 2)
    p["stop"] = p["entry"] - 40
    # gap through the stop: filled at the gap open, day loss > $1,800 but < $3,000 -> soft DLL stop, not blown
    sim.process(bars([(t + timedelta(minutes=2), p["entry"] - 60, p["entry"] - 55, p["entry"] - 70, p["entry"] - 65)]),
                t + timedelta(minutes=3))
    assert sim.s["account"]["no"] == 1 and sim.s["ledger"]["blown_evals"] == 0
    assert sim.s["day"]["locked"] and "DLL" in sim.s["day"]["locked"]
    assert 100000 - 3000 < sim.s["account"]["balance"] < 100000 - 1800
    t2 = t + timedelta(minutes=10)
    assert not sim.on_record(rec(t2), t2)[0]["taken"]
    nxt = TUE_0900_ET + timedelta(days=1)
    sim.process({}, nxt)
    assert sim.s["day"]["locked"] is None and sim.on_record(rec(nxt), nxt)[0]["taken"]


def test_dll_liquidation_with_two_open_positions(sim):
    """Two positions whose stops fit the DLL individually; a shared adverse bar liquidates both at the DLL level."""
    t = TUE_0900_ET + timedelta(minutes=5)
    sim.L["max_risk_usd"] = 1000; sim.L["stop_to_target_ratio"] = 1.3; sim.L["dll_safety_usd"] = -2000
    sim.process({}, t)
    r = rec(t); r["signals"].append(rec(t, sym="XAUUSD", px=4000.0)["signals"][0])
    r["signals"][1]["atr"] = 7.0
    assert all(d["taken"] for d in sim.on_record(r, t))
    t1 = t + timedelta(minutes=1)
    b = {**bars([(t1, 20000, 20001, 19999, 20000)]), **bars([(t1, 4000, 4000.1, 3999.9, 4000)], "XAUUSD")}
    sim.process(b, t1 + timedelta(minutes=1))
    assert len(sim.s["positions"]) == 2
    # Normalize to known small risks so the shared adverse bar liquidates at the DLL level
    for p, sdist, pv, tick in (
        (sim.s["positions"][0], 60.0, 20.0, 0.25),     # 1 NQ, $1,200
        (sim.s["positions"][1], 7.5, 100.0, 0.1),      # 1 GC, $750
    ):
        p["qty"] = 1
        p["contract"] = "NQ" if p["symbol"] == "US100" else "GC"
        p["stop"] = round(p["entry"] - sdist, 10)
        p["target"] = round(p["entry"] + sdist / 2, 10)
        p["risk_usd"] = round(sdist * p["qty"] * pv, 2)
        p["time_cut_deadline"] = None
        p["mfe_min"] = 0.0
    t2 = t1 + timedelta(minutes=1)
    p0, p1 = sim.s["positions"]
    b = {**bars([(t2, 20000, 20000, p0["stop"] + 0.25, p0["stop"] + 0.25)]),
         **bars([(t2, 4000, 4000, p1["stop"] + 0.1, p1["stop"] + 0.1)], "XAUUSD")}
    sim.process(b, t2 + timedelta(minutes=1))
    assert not sim.s["positions"] and "DLL" in sim.s["day"]["locked"]
    assert sim.s["account"]["balance"] == pytest.approx(100000 - 1800, abs=60)


def test_force_flat_at_1645_et(sim):
    # Enter at 15:30 ET (still NEW_YORK_SESSION). 16:00 ET is LOW_LIQUIDITY_DEADZONE
    # under [chart.session], which hard-blocks new Lucid entries.
    t = datetime(2026, 11, 10, 20, 30, tzinfo=UTC)  # 15:30 ET
    sim.process({}, t)
    out = sim.on_record(rec(t), t)
    assert out[0]["taken"], out
    # Fill first minute, then freeze scalp exits so only 16:45 flat can close it
    sim.process(bars([(t + timedelta(minutes=1), 20000, 20002, 19998, 20001)]), t + timedelta(minutes=2))
    _shrink(sim)
    # From 15:32 -> past 16:45 ET (21:45 UTC). t+75m = 21:45 UTC.
    rows = [(t + timedelta(minutes=i), 20000, 20002, 19998, 20001) for i in range(2, 80)]
    sim.process(bars(rows), t + timedelta(minutes=80))
    assert not sim.s["positions"]
    logs = list(Path(sim.logs_dir).rglob("lucid_trades.jsonl"))
    last = logs[0].read_text().strip().splitlines()[-1]
    assert "flat_by_16:45_ET" in last and '"2026-11-10T21:45:00+00:00"' in last
    from signals.exit_versions import GEMINI_EXIT_PROMPT_VERSION
    assert json.loads(last)["exit_prompt_version"] == GEMINI_EXIT_PROMPT_VERSION
    # no entries inside the last 15 minutes, the 16:45-18:00 ET break, OR Asian reopen
    for m in (60, 75, 90):  # 16:30 / 16:45 / 17:00 ET relative to 15:30
        tt = t + timedelta(minutes=m)
        assert not sim.on_record(rec(tt), tt)[0]["taken"]
    reopen = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)  # 18:00 ET = ASIAN_SESSION_CHOP
    sim.process({}, reopen)
    out_re = sim.on_record(rec(reopen), reopen)
    # Lucid session is open again, but automation hard-blocks Asian
    assert out_re[0]["taken"] is False
    assert out_re[0]["why"] == "session_blocked"


def test_time_cut_exits_without_mfe(sim):
    """No favourable move within time_cut_candles -> market exit."""
    t = TUE_0900_ET
    sim.cfg.setdefault("scalp", {})["time_cut_candles"] = 1  # 15 minutes
    sim.cfg["scalp"]["mfe_min_atr"] = 0.25
    sim.process({}, t)
    assert sim.on_record(rec(t), t)[0]["taken"]
    # Fill at t+1
    sim.process(bars([(t + timedelta(minutes=1), 20000, 20001, 19999, 20000)]), t + timedelta(minutes=2))
    assert sim.s["positions"]
    p = sim.s["positions"][0]
    # Force a short deadline and tiny mfe_min already unmet
    p["time_cut_deadline"] = (t + timedelta(minutes=3)).isoformat()
    p["mfe_min"] = 50.0  # huge — will not be met by flat bars
    rows = [(t + timedelta(minutes=i), 20000, 20001, 19999, 20000) for i in range(2, 6)]
    sim.process(bars(rows), t + timedelta(minutes=6))
    assert not sim.s["positions"]
    logs = list(Path(sim.logs_dir).rglob("lucid_trades.jsonl"))
    assert "time_cut_no_mfe" in logs[0].read_text()


def test_vwap_cross_15m_exit(sim):
    """15m close back under VWAP against a LONG -> exit."""
    t = TUE_0900_ET
    sim.process({}, t)
    assert sim.on_record(rec(t), t)[0]["taken"]
    sim.process(bars([(t + timedelta(minutes=1), 20000, 20001, 19999, 20000)]), t + timedelta(minutes=2))
    assert len(sim.s["positions"]) == 1
    p = sim.s["positions"][0]
    p["time_cut_deadline"] = None
    p["mfe_min"] = 0.0
    p["stop"] = p["entry"] - 100  # keep stop far away so only the VWAP rule can exit
    entry_t = t + timedelta(minutes=1)
    boundary = entry_t.replace(minute=(entry_t.minute // 15) * 15, second=0, microsecond=0) + timedelta(minutes=15)
    opened = boundary - timedelta(minutes=15)
    m15 = {"US100": pd.DataFrame(
        {"close": [19995.0], "vwap": [20010.0], "ema50": [19900.0]},
        index=pd.DatetimeIndex([opened]),
    )}
    rows = []
    tt = entry_t + timedelta(minutes=1)
    while tt <= boundary:
        rows.append((tt, 19995, 20000, 19990, 19995))  # above the far stop, below VWAP at the 15m close
        tt += timedelta(minutes=1)
    sim.process(bars(rows), boundary + timedelta(minutes=1), m15=m15)
    assert not sim.s["positions"]
    logs = list(Path(sim.logs_dir).rglob("lucid_trades.jsonl"))
    assert "vwap_cross_15m" in logs[0].read_text()


def test_session_blocked_asian_and_deadzone(tmp_path):
    """Lucid hard-blocks new entries in ASIAN / DEADZONE; reason == 'session_blocked'.
    Existing positions are still managed (not tested here — process() path unchanged)."""
    import copy
    from signals.config import load_config
    from signals.lucid import LucidSim
    from datetime import datetime, timezone, timedelta

    cfg = copy.deepcopy(load_config())
    cfg.setdefault("exit_manager", {})["gemini_exit_calls_enabled"] = True
    cfg["lucid"]["slippage_ticks"] = 0
    cfg["lucid"]["exit_mode"] = "server"; cfg["lucid"].pop("reset_archive", None)  # legacy server-target sim
    # 19:30 ET Tue = ASIAN_SESSION_CHOP (00:30 UTC Wed)
    t_asian = datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc)
    sim = LucidSim(cfg, tmp_path / "st_asian", tmp_path / "lg_asian")
    sim.s["last_processed"] = t_asian.isoformat()
    # Force entries_allowed by being inside Lucid 18:00-16:45 window
    assert sim.entries_allowed(t_asian)
    r = {"run_at_utc": t_asian.isoformat(), "signals": [{
        "symbol": "US100", "action": "BUY", "model_action": "BUY", "entry": 20000.0,
        "stop_loss": 19980.0, "take_profit": 20020.0, "confidence": 0.8, "rr_local": 1.0,
        "last_close": 20000.0, "atr": 40.0, "model_risk_pct": None, "reasons": [],
        "session": "ASIAN_SESSION_CHOP",
        "last_closed_bar_utc": t_asian.isoformat(),
    }]}
    out = sim.on_record(r, t_asian)
    assert out and out[0]["taken"] is False
    assert out[0]["why"] == "session_blocked"

    # DEADZONE 17:00 ET = 22:00 UTC — but Lucid flat_by is 16:45, so entries_allowed is False.
    # Use 02:30 ET (07:30 UTC) which is DEADZONE but still inside Lucid session (after 18:00 prior day).
    t_dz = datetime(2026, 11, 10, 7, 30, tzinfo=timezone.utc)  # 02:30 EST Tue
    sim2 = LucidSim(cfg, tmp_path / "st_dz", tmp_path / "lg_dz")
    sim2.s["last_processed"] = t_dz.isoformat()
    assert sim2.entries_allowed(t_dz)
    r2 = dict(r)
    r2["run_at_utc"] = t_dz.isoformat()
    r2["signals"] = [dict(r["signals"][0], session="LOW_LIQUIDITY_DEADZONE",
                          last_closed_bar_utc=t_dz.isoformat())]
    out2 = sim2.on_record(r2, t_dz)
    assert out2 and out2[0]["taken"] is False
    assert out2[0]["why"] == "session_blocked"

    # NEW_YORK_SESSION should NOT be blocked by session filter
    t_ny = datetime(2026, 11, 10, 16, 30, tzinfo=timezone.utc)  # 11:30 EST
    sim3 = LucidSim(cfg, tmp_path / "st_ny", tmp_path / "lg_ny")
    sim3.s["last_processed"] = t_ny.isoformat()
    r3 = dict(r)
    r3["run_at_utc"] = t_ny.isoformat()
    r3["signals"] = [dict(r["signals"][0], session="NEW_YORK_SESSION",
                          last_closed_bar_utc=t_ny.isoformat(),
                          signal_candle={"high": 20010, "low": 19990, "open": 20000, "close": 20000},
                          levels=[], poc=20000, vah=20050, val=19950)]
    out3 = sim3.on_record(r3, t_ny)
    assert out3 and out3[0]["taken"] is True, out3
