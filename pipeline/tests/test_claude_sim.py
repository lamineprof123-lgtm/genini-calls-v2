"""Claude parallel paper sims: prompts, schema, server rules, engine (fake API / data). No network."""
import asyncio, json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from signals import claude_client as CC
from signals.claude_rules import (rules_cfg, clamp_stop, size_for, advance, trade_result, floor_for, m5_summary,
                                  h1_context, phase_of, risk_per_contract)
from signals.claude_sim import ClaudeEngine, claude_cfg, next_m15_close, in_exit_grace
from signals.config import load_config, ROOT

UTC = timezone.utc
ATT = ROOT / "templates" / "claude"
R = rules_cfg({})
GC = {"point_value": 100.0, "tick": 0.10, "commission": 2.30}
NQ = {"point_value": 20.0, "tick": 0.25, "commission": 1.75}
E6 = {"point_value": 125000.0, "tick": 0.00005, "commission": 2.40}


# ------------------------------------------------------------------ prompts / schema
def test_prompts_verbatim_and_substitution():
    raw = (ATT / "entry_prompt_v2.txt").read_text()
    p = CC.load_prompts()
    assert p["entry"].startswith("You read M15 charts") and p["entry"] in raw
    sp = CC.entry_system_prompt(["XAUUSD", "EURUSD"], p)
    assert "You receive 2 chart images" in sp and "in this order: XAUUSD, EURUSD." in sp
    assert "{n}" not in sp and "{symbol_order}" not in sp
    xr = (ATT / "exit_prompt_v2.txt").read_text()
    assert p["exit"] in xr and p["exit"].startswith("You are watching one open futures position")
    assert CC.M5_LINE in CC.exit_system_prompt(True, p)
    fb = CC.exit_system_prompt(False, p)
    assert CC.M15_LINE in fb and CC.M5_LINE not in fb
    assert "Deliberately not sent" not in p["exit_user"]


def test_exit_user_template_all_placeholders_filled():
    import re
    p = CC.load_prompts()
    keys = re.findall(r"\{([a-z0-9_]+)\}", p["exit_user"])
    assert {"phase", "mae_atr", "floor_price", "floor_label", "m5_c1", "m5_o", "entry_stop", "setup_type",
            "entry_rationale_short"} <= set(keys)
    txt = CC.render_exit_user({k: "1" for k in keys}, p)
    assert "{" not in txt
    assert "n/a" in CC.render_exit_user({}, p)


def test_signal_schema_order_and_types():
    s = CC.signal_batch_schema(["XAUUSD", "US100"])
    it = s["properties"]["signals"]["items"]
    assert it["required"] == list(CC.ENTRY_FIELDS) and list(it["properties"]) == list(CC.ENTRY_FIELDS)
    assert it["properties"]["setup_type"]["enum"] == ["trend", "fade", "none"]
    assert it["properties"]["score"]["type"] == "integer"
    for k in ("risk_pct", "entry", "stop_loss", "take_profit", "risk_reward"):
        assert it["properties"][k]["type"] == ["number", "null"]
    assert s["required"] == ["signals", "market_summary"] and s["additionalProperties"] is False
    assert CC.EXIT_DECISION_SCHEMA["required"] == ["structure", "reason", "decision", "confidence"]


def test_validate_entry_signal_gate_and_ranges():
    v = CC.validate_entry_signal({"action": "BUY", "score": 4, "setup_type": "trend", "confidence": 1.4}, {}, 5)
    assert v["gated"] is False and v["confidence"] == 1.0
    v = CC.validate_entry_signal({"action": "SELL", "score": 5, "setup_type": "fade", "confidence": 0.55}, {}, 5)
    assert v["gated"] is True
    v = CC.validate_entry_signal({"action": "BUY", "score": 7, "setup_type": "none"}, {}, 5)
    assert v["gated"] is False


def test_cost_from_usage():
    c = CC.call_cost("claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 100_000})
    assert c == pytest.approx(4.0 + 2.0)
    assert CC.call_cost("claude-haiku-5-5", {"input_tokens": 2000, "output_tokens": 40}) == pytest.approx(0.00022)


# ------------------------------------------------------------------ rules
def test_clamp_stop():
    s, d, n = clamp_stop(1, 100.0, 99.8, 1.0, R)       # 0.2 ATR -> 0.6
    assert d == pytest.approx(0.6) and s == pytest.approx(99.4) and "up" in n
    s, d, n = clamp_stop(-1, 100.0, 103.0, 1.0, R)     # 3 ATR -> 1.5
    assert d == pytest.approx(1.5) and s == pytest.approx(101.5)
    assert clamp_stop(1, 100.0, 100.5, 1.0, R)[0] is None
    assert clamp_stop(1, 100.0, None, 1.0, R)[0] is None


def test_sizing_caps():
    assert size_for("NQ", 1, 50.0, NQ, R, [])[0] == 0              # 50 pts * $20 = $1,000 > $900
    assert size_for("NQ", 1, 40.0, NQ, R, [])[0] == 1
    assert size_for("GC", 1, 3.0, GC, R, [])[0] == 1               # never 2 GC
    assert size_for("6E", 1, 0.0005, E6, R, [])[0] == 4            # $62.5+4.8 per contract -> cap 4
    open_ = [{"contract": "6B", "side": 1, "qty": 3}]
    assert size_for("6E", 1, 0.0005, E6, R, open_)[0] == 1         # 6E+6B same side share 4
    assert size_for("6E", -1, 0.0005, E6, R, open_)[0] == 3        # account cap 6 - 3
    assert size_for("6E", 1, 0.0005, E6, R, [{"contract": "GC", "side": 1, "qty": 6}])[0] == 0


def test_floors():
    assert floor_for(1, 100, 0.4, 1.0, 0.05, R) == (None, None)
    f, l = floor_for(1, 100, 0.5, 1.0, 0.05, R)
    assert f == pytest.approx(100.05) and l.startswith("break")
    assert floor_for(1, 100, 1.2, 1.0, 0.05, R)[0] == pytest.approx(100.6)
    assert floor_for(-1, 100, 2.0, 1.0, 0.05, R)[0] == pytest.approx(98.8)
    assert phase_of(0.1, R) == "A" and phase_of(0.3, R) == "B" and phase_of(0.8, R) == "C"


def _bars(t0, rows):
    idx = pd.DatetimeIndex([t0 + timedelta(minutes=i) for i in range(len(rows))])
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


T0 = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)   # Wed 10:00 ET, in session


def _vt(side=1, entry=100.0, stop=99.0, atr=1.0):
    return {"side": side, "entry": entry, "stop": stop, "init_stop": stop, "atr": atr, "entry_time": T0.isoformat(),
            "scan_from": T0.isoformat(), "tick": 0.1, "comm_pts": 0.05, "qty": 1, "mfe_pts": 0, "mae_pts": 0}


def test_advance_hard_stop_and_reach_flag():
    t = _vt()
    ev = advance(t, _bars(T0, [[100, 100.2, 99.5, 99.6], [99.6, 99.7, 98.9, 99.0]]), R, now=T0 + timedelta(hours=1))
    assert ev["reason"] == "hard_stop" and ev["price"] == pytest.approx(98.9)   # stop 99.0 - 1 tick
    assert t["reach_05"] is False


def test_advance_break_even_then_profit_lock():
    t = _vt()
    ev = advance(t, _bars(T0, [[100, 100.6, 99.9, 100.5], [100.5, 100.5, 100.0, 100.1]]), R, now=T0 + timedelta(hours=1))
    assert ev["reason"] == "break_even" and t["reach_05"] is True
    t = _vt()
    ev = advance(t, _bars(T0, [[100, 101.6, 100, 101.5], [101.5, 101.5, 100.9, 101.0]]), R, now=T0 + timedelta(hours=1))
    assert ev["reason"] == "profit_lock" and ev["price"] == pytest.approx(100.9)   # 60% of 1.6 = 100.96 -> tick 101.0 - slip


def test_advance_time_stop_45m():
    t = _vt()
    rows = [[100, 100.1, 99.9, 100.0]] * 50
    ev = advance(t, _bars(T0, rows), R, now=T0 + timedelta(hours=2))
    assert ev["reason"] == "time_stop_45m" and ev["t"].startswith("2026-11-11T15:45")
    t = _vt()
    assert advance(t, _bars(T0, rows[:30]), R, now=T0 + timedelta(minutes=30)) is None


def test_trade_result_R():
    t = {"side": 1, "entry": 2000.0, "stop": 1995.0, "qty": 1}
    r = trade_result(t, 2005.0, GC)
    assert r["net"] == pytest.approx(500 - 4.6) and r["risk_usd"] == pytest.approx(504.6)
    assert r["R"] == pytest.approx((500 - 4.6) / 504.6, abs=1e-3)


def test_m5_and_h1_context():
    now = datetime(2026, 11, 11, 15, 17, 30, tzinfo=UTC)
    idx = pd.date_range("2026-11-11 14:50", periods=28, freq="1min", tz="UTC")
    df = pd.DataFrame({"open": range(28), "high": range(1, 29), "low": range(28), "close": [x + .5 for x in range(28)]},
                      index=idx)
    m = m5_summary(df, now)
    assert m["m5_c1"] == pytest.approx(24.5) and m["m5_c3"] == pytest.approx(14.5) and m["m5_o"] == 25
    h1i = pd.date_range("2026-11-07", periods=100, freq="1h", tz="UTC")
    h1 = pd.DataFrame({"open": [float(i) for i in range(100)], "high": [i + 2.0 for i in range(100)],
                       "low": [i - 1.0 for i in range(100)], "close": [i + 1.0 for i in range(100)]}, index=h1i)
    c = h1_context(h1, datetime(2026, 11, 11, 3, tzinfo=UTC))
    assert c["h1_trend"] == "up" and c["prior_day_high"] == pytest.approx(95 + 2.0) and c["prior_day_low"] == pytest.approx(71.0)


# ------------------------------------------------------------------ engine with fakes
def _cfg(tmp):
    cfg = load_config()
    cfg["general"]["state_dir"] = str(tmp / "state")
    cfg["general"]["logs_dir"] = str(tmp / "logs")
    cfg["site"]["dir"] = str(tmp)
    return cfg


def _payload(sym, price, atr, bar="2026-11-11T14:45:00+00:00", session="NEW_YORK_SESSION"):
    return {"symbol": sym, "source": "dukascopy", "ticker": sym.lower(), "timeframe": "M15", "digits": 2,
            "style": {"template": "chart_v2.html", "width": 1280, "height": 720}, "candles": [],
            "context": {"last_closed_bar_utc": bar, "digits": 2, "atr": atr, "live_price": price,
                        "live_price_source": "dukascopy:tick", "live_price_age_s": 0.4,
                        "last_close": price, "live_price_stale": False, "session": session,
                        "session_blocks_automation": session in ("ASIAN_SESSION_CHOP", "LOW_LIQUIDITY_DEADZONE"),
                        "signal_candle": {"open": price, "high": price + 1, "low": price - 1, "close": price},
                        "vwap": price, "ema20_baseline": price, "ema50": price, "poc": price, "vah": price + 2,
                        "val": price - 2, "levels": [], "cvd_label": "NEUTRAL", "cvd_slope": 0}}


PAYLOADS = [_payload("XAUUSD", 2000.0, 5.0), _payload("EURUSD", 1.1, 0.0008),
            _payload("GBPUSD", 1.3, 0.001), _payload("US100", 20000.0, 40.0)]


def _sig(sym, action="HOLD", score=0, setup="none", conf=0.0, stop=None, entry=None):
    return {"symbol": sym, "trend_vs_vwap": "above", "volatility": "normal", "level_event": "none",
            "rationale": "T1 yes", "setup_type": setup, "score": score, "action": action, "confidence": conf,
            "risk_pct": None, "entry": entry, "stop_loss": stop, "take_profit": None, "risk_reward": None}


def _engine(tmp, answers, exit_answer=None, key=True, now=None):
    calls = []

    async def entry_api(cc, model, system, content, schema):
        calls.append(("entry", model, len(content)))
        a = answers[model]
        if isinstance(a, Exception):
            raise a
        return {"ok": True, "model": model, "parsed": {"signals": a, "market_summary": "x"},
                "usage": {"input_tokens": 6000, "output_tokens": 900}, "latency_ms": 1234, "cost_usd": 0.01,
                "thinking_tokens": 300}

    async def exit_api(cc, system, user, png):
        calls.append(("exit", user))
        if isinstance(exit_answer, Exception):
            raise exit_answer
        return exit_answer or {"ok": False, "error": "timeout"}

    async def render(ps):
        return {p["symbol"]: b"\x89PNGfake" for p in ps}

    async def h1(sym, cfg, n):
        raise RuntimeError("offline")

    async def m1(sym, cfg, n):
        p = {x["symbol"]: x for x in PAYLOADS}[sym]["context"]["live_price"]
        t = (now or T0).replace(second=0, microsecond=0)
        return _bars(t - timedelta(minutes=2), [[p, p, p, p]] * 3)

    async def snap(sym, cfg, n):
        pl = json.loads(json.dumps({x["symbol"]: x for x in PAYLOADS}[sym]))
        return pl, await m1(sym, cfg, n)

    eng = ClaudeEngine(_cfg(tmp), entry_api=entry_api, exit_api=exit_api, render=render, h1_fetch=h1, m1_fetch=m1,
                       snapshot=snap, key_fn=lambda: key)
    eng.eng["created_at"] = "2026-01-01T00:00:00+00:00"
    return eng, calls


def _stamp_entry(eng, t=T0):
    """Fill path stamps entry_time to wall-clock now; pin it to the simulated clock so grace/hard-stop tests work."""
    iso = t.isoformat()
    for sim in eng.sims.values():
        for pos in sim["positions"]:
            pos["entry_time"] = iso
            pos["scan_from"] = iso
    for pos in eng.eng.get("shadow") or []:
        pos["entry_time"] = iso
        pos["scan_from"] = iso



OPUS, SON = "claude-opus-5-5", "claude-sonnet-5-5"


def test_no_key_skips_cleanly_and_shows_waiting(tmp_path):
    eng, calls = _engine(tmp_path, {}, key=False)
    out = asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    assert out == {"skipped": "no_key"} and calls == []
    site = json.loads((tmp_path / "claude.json").read_text())
    assert site["status"] == "waiting for key" and {s["id"] for s in site["sims"]} == {"C-OPUS", "C-SONNET"}


def test_entry_cycle_opens_highest_score_one_per_cycle(tmp_path):
    ans = {OPUS: [_sig("XAUUSD", "BUY", 6, "trend", 0.6, stop=1996.0), _sig("EURUSD", "SELL", 7, "fade", 0.62,
                                                                             stop=1.1006),
                  _sig("GBPUSD"), _sig("US100", "BUY", 4, "trend", 0.5, stop=19970)],
           SON: [_sig("XAUUSD"), _sig("EURUSD"), _sig("GBPUSD"), _sig("US100")]}
    eng, calls = _engine(tmp_path, ans)
    out = asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    assert [c[1] for c in calls if c[0] == "entry"] == [OPUS, SON]
    assert calls[0][2] == 9                                    # 4 x (text + image) + final instruction
    op = eng.sims["C-OPUS"]["positions"]
    assert len(op) == 1 and op[0]["symbol"] == "EURUSD" and op[0]["side"] == -1 and op[0]["qty"] == 4
    assert op[0]["stop_note"] == "model stop"
    assert eng.sims["C-SONNET"]["positions"] == []
    books = sorted(t["book"] for t in eng.eng["shadow"])
    assert books == ["mech", "twin", "twin", "twin", "whatif", "whatif", "whatif"]   # 3 BUY/SELL signals
    sig = [json.loads(l) for l in open(next((tmp_path / "logs").glob("*/claude_signals.jsonl")))]
    xau = [s for s in sig if s["symbol"] == "XAUUSD" and s["sim"] == "C-OPUS"][0]
    assert xau["decision"] == "skip: one new entry per cycle"
    assert asyncio.run(eng.on_cycle(PAYLOADS, now=T0)) == {"skipped": "same_bar"}
    site = json.loads((tmp_path / "claude.json").read_text())
    assert site["status"] == "running" and site["sims"][0]["open_positions"][0]["symbol"] == "EURUSD"


def test_entry_api_exception_never_raises(tmp_path):
    eng, _ = _engine(tmp_path, {OPUS: RuntimeError("boom"), SON: [_sig("XAUUSD")]})
    out = asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    assert out["sims"]["C-OPUS"]["ok"] is False
    eng.entry_task = None
    assert asyncio.run(eng._safe_cycle(None)) == {"error": True}


def test_blocked_session_skips_call(tmp_path):
    ps = [_payload(p["symbol"], p["context"]["live_price"], p["context"]["atr"], session="ASIAN_SESSION_CHOP")
          for p in PAYLOADS]
    eng, calls = _engine(tmp_path, {})
    assert asyncio.run(eng.on_cycle(ps, now=T0)) == {"skipped": "blocked_session"} and calls == []


def test_guards_daily_and_trailing(tmp_path):
    eng, _ = _engine(tmp_path, {})
    sim = eng.sims["C-OPUS"]
    assert eng.guard(sim, 500, T0) is None
    sim["day"]["realized"] = -1200.0
    assert eng.guard(sim, 500, T0).startswith("daily guard")
    sim["day"]["realized"] = 0.0
    sim["balance"] = sim["mll_floor"] + 700
    assert eng.guard(sim, 500, T0).startswith("trailing guard")


def test_loss_lock_same_side_until_retest(tmp_path):
    eng, _ = _engine(tmp_path, {})
    sim = eng.sims["C-OPUS"]
    pos = eng._vtrade("real", "C-OPUS", "XAUUSD", 1, 2000.0, 1995.0, 5.0, T0, qty=1)
    pos["id"] = 1
    sim["positions"].append(pos)
    te = T0 + timedelta(minutes=10)
    eng._close_real(sim, pos, 1995.0, te, "hard_stop")
    assert sim["locks"]["XAUUSD"]["loss"] is True
    assert "lock until" in eng.lock_reason(sim, "XAUUSD", 1, te + timedelta(minutes=20), None)
    later = te + timedelta(minutes=40)
    pl = _payload("XAUUSD", 2001.0, 5.0)
    assert "retests" in eng.lock_reason(sim, "XAUUSD", 1, later, pl)
    assert eng.lock_reason(sim, "XAUUSD", -1, later, pl) is None            # opposite side is fine
    pl["context"]["forming_m15_low"] = 1994.9
    assert eng.lock_reason(sim, "XAUUSD", 1, later, pl) is None
    assert next_m15_close(datetime(2026, 11, 11, 15, 7, tzinfo=UTC)).minute == 15


def test_exit_tick_close_and_failure_fallback(tmp_path):
    now = T0 + timedelta(minutes=5)
    ans = {OPUS: [_sig("XAUUSD", "BUY", 6, "trend", 0.6, stop=1996.0), _sig("EURUSD"), _sig("GBPUSD"),
                  _sig("US100")], SON: [_sig("XAUUSD"), _sig("EURUSD"), _sig("GBPUSD"), _sig("US100")]}
    eng, calls = _engine(tmp_path, ans, exit_answer={"ok": False, "error": "timeout"}, now=now)
    asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    _stamp_entry(eng, T0)
    p = eng.sims["C-OPUS"]["positions"][0]
    for i in range(3):
        asyncio.run(eng.exit_tick(now + timedelta(seconds=18 * i)))
    assert p["exit_fail"] == 3 and p["server_only"] is True
    user = [c[1] for c in calls if c[0] == "exit"][0]
    assert "XAUUSD LONG | phase A" in user and "server floor now:" in user and "$" not in user
    eng._exit_api = lambda *a: _ret({"ok": True, "parsed": {"structure": "momentum_stall", "reason": "stall",
                                                            "decision": "CLOSE", "confidence": 0.3},
                                     "latency_ms": 900, "cost_usd": 0.0002, "thinking_tokens": 0})
    # after the first M15 close (15:15); a CLOSE at now+2m (15:07) would now be ignored by the grace rule
    asyncio.run(eng.exit_tick(T0 + timedelta(minutes=16)))
    s = eng.sims["C-OPUS"]
    assert s["positions"] == [] and s["trades"] == 1 and s["recent"][0]["reason"] == "claude_close"
    assert s["recent"][0]["exit_prompt_version"] == "v2-grace"
    assert eng.eng["probes"] and any(t["book"] == "mech" for t in eng.eng["shadow"])
    rep = eng.build_report()
    assert rep["sims"]["C-OPUS"]["real"]["n"] == 1 and rep["sims"]["C-OPUS"]["ops"]["exit"]["failed"] == 3
    assert rep["sims"]["C-OPUS"]["real"]["since_fix"]["n"] == 1


async def _ret(x):
    return x


def test_exit_api_exception_contained(tmp_path):
    now = T0 + timedelta(minutes=5)
    ans = {OPUS: [_sig("XAUUSD", "BUY", 6, "trend", 0.6, stop=1996.0), _sig("EURUSD"), _sig("GBPUSD"),
                  _sig("US100")], SON: [_sig(s) for s in ("XAUUSD", "EURUSD", "GBPUSD", "US100")]}
    eng, _ = _engine(tmp_path, ans, exit_answer=RuntimeError("api down"), now=now)
    asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    out = asyncio.run(eng.exit_tick(now))
    assert out["checks"] == 0 and eng.sims["C-OPUS"]["positions"]


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CLAUDE_ENTRY_MODELS", "claude-sonnet-5-5")
    monkeypatch.setenv("CLAUDE_EXIT_MODEL", "claude-haiku-5-5")
    cc = claude_cfg(load_config())
    assert cc["entry_models"] == ["claude-sonnet-5-5"] and cc["exit_model"] == "claude-haiku-5-5"


def test_gha_loop_claude_isolated(tmp_path):
    from signals.gha_loop import GhaLoop, SITE_JSON
    assert "claude.json" in SITE_JSON
    g = GhaLoop(_cfg(tmp_path), tmp_path, "t-1", "holder", dry_run=True)
    g.start_claude()
    assert g.claude is None
    g.schedule_claude({"payloads": PAYLOADS})      # no engine -> no-op, no error



def test_in_exit_grace_until_first_m15_close():
    assert in_exit_grace(T0, T0) is True
    assert in_exit_grace(T0, T0 + timedelta(minutes=14, seconds=59)) is True
    assert in_exit_grace(T0, T0 + timedelta(minutes=15)) is False
    mid = datetime(2026, 11, 11, 15, 1, tzinfo=UTC)
    assert in_exit_grace(mid, datetime(2026, 11, 11, 15, 14, 59, tzinfo=UTC)) is True
    assert in_exit_grace(mid, datetime(2026, 11, 11, 15, 15, tzinfo=UTC)) is False
    assert in_exit_grace(T0.isoformat(), T0 + timedelta(minutes=1)) is True


def _close_ans():
    return {"ok": True, "parsed": {"structure": "momentum_stall", "reason": "stall",
                                   "decision": "CLOSE", "confidence": 0.3},
            "latency_ms": 900, "cost_usd": 0.0002, "thinking_tokens": 0}


def _xau_buy():
    return {OPUS: [_sig("XAUUSD", "BUY", 6, "trend", 0.6, stop=1996.0), _sig("EURUSD"), _sig("GBPUSD"),
                   _sig("US100")], SON: [_sig(s) for s in ("XAUUSD", "EURUSD", "GBPUSD", "US100")]}


def test_claude_close_ignored_during_grace(tmp_path):
    now = T0 + timedelta(minutes=5)   # 15:05; first M15 close after 15:00 entry is 15:15
    eng, _ = _engine(tmp_path, _xau_buy(), exit_answer=_close_ans(), now=now)
    asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    _stamp_entry(eng, T0)
    assert eng.sims["C-OPUS"]["positions"]
    out = asyncio.run(eng.exit_tick(now))
    assert out.get("real_closed", 0) == 0
    assert eng.sims["C-OPUS"]["positions"] and eng.sims["C-OPUS"]["trades"] == 0
    evs = [json.loads(l) for f in (tmp_path / "logs").glob("*/claude_events.jsonl") for l in open(f)]
    ign = [e for e in evs if e.get("event") == "close_ignored_grace"]
    assert ign and ign[0]["symbol"] == "XAUUSD"
    assert "2026-11-11T15:15:00" in ign[0]["grace_until"]
    checks = [json.loads(l) for f in (tmp_path / "logs").glob("*/claude_exit_checks.jsonl") for l in open(f)]
    assert checks[-1]["ignored"] == "close_ignored_grace" and "closed" not in checks[-1]


def test_claude_close_honored_after_first_m15_close(tmp_path):
    now = T0 + timedelta(minutes=16)
    eng, _ = _engine(tmp_path, _xau_buy(), exit_answer=_close_ans(), now=now)
    asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    _stamp_entry(eng, T0)
    asyncio.run(eng.exit_tick(now))
    s = eng.sims["C-OPUS"]
    assert s["positions"] == [] and s["trades"] == 1 and s["recent"][0]["reason"] == "claude_close"
    assert s["recent"][0]["exit_prompt_version"] == "v2-grace"
    site = eng.site_payload(eng.build_report())
    opus = next(x for x in site["sims"] if x["id"] == "C-OPUS")
    assert opus["since_fix"]["n"] == 1 and opus["exit_prompt_version"] == "v2-grace"


def test_hard_stop_and_daily_flatten_still_work_during_grace(tmp_path):
    eng, _ = _engine(tmp_path, _xau_buy(), now=T0 + timedelta(minutes=3))
    asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    _stamp_entry(eng, T0)
    p = eng.sims["C-OPUS"]["positions"][0]
    stop = p["init_stop"]

    async def m1_hit(sym, cfg, n):
        if sym != "XAUUSD":
            px = {x["symbol"]: x for x in PAYLOADS}[sym]["context"]["live_price"]
            return _bars(T0, [[px, px, px, px]] * 3)
        return _bars(T0, [[2000.0, 2000.1, 1999.0, 1999.5], [1999.5, 1999.6, stop - 1.0, stop - 0.5]])

    async def snap(sym, cfg, n):
        pl = json.loads(json.dumps({x["symbol"]: x for x in PAYLOADS}[sym]))
        return pl, await m1_hit(sym, cfg, n)

    eng._m1, eng._snapshot = m1_hit, snap
    asyncio.run(eng.exit_tick(T0 + timedelta(minutes=3)))
    assert eng.sims["C-OPUS"]["positions"] == []
    assert eng.sims["C-OPUS"]["recent"][0]["reason"] == "hard_stop"

    eng2, _ = _engine(tmp_path / "flat", _xau_buy(), now=T0 + timedelta(minutes=3))
    asyncio.run(eng2.on_cycle(PAYLOADS, now=T0))
    _stamp_entry(eng2, T0)
    sim = eng2.sims["C-OPUS"]
    assert sim["positions"]
    sim["day"]["realized"] = -1500.0
    sim["last_price"] = {sim["positions"][0]["symbol"]: sim["positions"][0]["entry"]}
    eng2._account_limits(sim, T0 + timedelta(minutes=3))
    assert sim["positions"] == [] and sim["recent"][0]["reason"] == "daily_flatten_-1500"
    assert sim["recent"][0]["exit_prompt_version"] == "v2-grace"


def test_backfill_tags_pre_grace_and_since_fix_excludes_them(tmp_path):
    from signals.exit_versions import CLAUDE_EXIT_PROMPT_VERSION, CLAUDE_EXIT_PROMPT_VERSION_PRE
    eng, _ = _engine(tmp_path, {OPUS: [_sig(s) for s in ("XAUUSD", "EURUSD", "GBPUSD", "US100")],
                                SON: [_sig(s) for s in ("XAUUSD", "EURUSD", "GBPUSD", "US100")]})
    day = tmp_path / "logs" / "2026-11-12"
    day.mkdir(parents=True)
    rows = [
        {"t_exit": "2026-11-12T13:30:03+00:00", "book": "real", "sim": "C-OPUS", "symbol": "EURUSD",
         "net": -69.20, "R": -0.08, "reason": "claude_close"},
        {"t_exit": "2026-11-12T13:35:00+00:00", "book": "real", "sim": "C-OPUS", "symbol": "GBPUSD",
         "net": -80.0, "R": -0.1, "reason": "claude_close"},
        {"t_exit": "2026-11-12T14:00:00+00:00", "book": "real", "sim": "C-OPUS", "symbol": "XAUUSD",
         "net": 100.0, "R": 0.2, "reason": "claude_close", "exit_prompt_version": CLAUDE_EXIT_PROMPT_VERSION},
    ]
    (day / "claude_trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    eng.sims["C-OPUS"]["recent"] = [{"symbol": "EURUSD", "net": -69.20, "reason": "claude_close"}]
    n = eng._backfill_exit_prompt_versions()
    assert n >= 2
    tagged = [json.loads(l) for l in (day / "claude_trades.jsonl").read_text().splitlines()]
    assert tagged[0]["exit_prompt_version"] == CLAUDE_EXIT_PROMPT_VERSION_PRE
    assert tagged[1]["exit_prompt_version"] == CLAUDE_EXIT_PROMPT_VERSION_PRE
    assert tagged[2]["exit_prompt_version"] == CLAUDE_EXIT_PROMPT_VERSION
    assert eng.sims["C-OPUS"]["recent"][0]["exit_prompt_version"] == CLAUDE_EXIT_PROMPT_VERSION_PRE
    rep = eng.build_report()
    real = rep["sims"]["C-OPUS"]["real"]
    assert real["n"] == 3
    assert real["since_fix"]["n"] == 1 and real["since_fix"]["net"] == 100.0
    site = eng.site_payload(rep)
    sf = next(x for x in site["sims"] if x["id"] == "C-OPUS")["since_fix"]
    assert sf["n"] == 1 and sf["net"] == 100.0

# ------------------------------------------------------------------ fills at the live quote (12 Nov fix)
def _eur_sell_answers():
    return {OPUS: [_sig("XAUUSD"), _sig("EURUSD", "SELL", 7, "fade", 0.62, stop=1.1006), _sig("GBPUSD"),
                   _sig("US100")],
            SON: [_sig("XAUUSD"), _sig("EURUSD"), _sig("GBPUSD"), _sig("US100")]}


def test_claude_fill_at_fresh_live_quote_minus_one_tick(tmp_path):
    eng, _ = _engine(tmp_path, _eur_sell_answers())
    asked = []

    async def quotes(syms, cfg, t):
        asked.append(list(syms))
        return {"EURUSD": {"price": 1.10012, "ts": t.isoformat(), "fetched_at": t.isoformat(),
                           "source": "dukascopy:tick", "age_s": 0.8, "stale": False}}
    eng._quote_fetch = quotes
    asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    assert asked == [["EURUSD"]]
    p = eng.sims["C-OPUS"]["positions"][0]
    assert p["entry"] == pytest.approx(1.10005)                 # 1.10012 - 1 tick (0.00005), on the tick grid
    assert p["fill_source"] == "live_quote:dukascopy:tick"
    fc = p["fill_check"]
    assert fc["quote_price"] == 1.10012 and fc["booked"] == p["entry"] and fc["within_slippage"] is True
    assert fc["chart_live_price"] == 1.1                        # the chart price is NOT the fill
    assert p["entry_time"] == eng.eng["fill_quotes"]["EURUSD"]["at"]
    ev = [json.loads(l) for l in open(next((tmp_path / "logs").glob("*/claude_events.jsonl")))]
    assert [e for e in ev if e["event"] == "open"][0]["fill_check"]["within_slippage"] is True


def test_claude_no_fill_on_stale_m1_quote(tmp_path):
    eng, _ = _engine(tmp_path, _eur_sell_answers())

    async def quotes(syms, cfg, t):
        return {"EURUSD": {"price": 1.0990, "ts": t.isoformat(), "fetched_at": t.isoformat(),
                           "source": "dukascopy:m1", "age_s": 70.0, "stale": False}}
    eng._quote_fetch = quotes
    asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    assert eng.sims["C-OPUS"]["positions"] == []
    sig = [json.loads(l) for l in open(next((tmp_path / "logs").glob("*/claude_signals.jsonl")))]
    eur = [s for s in sig if s["symbol"] == "EURUSD" and s["sim"] == "C-OPUS"][0]
    assert eur["decision"].startswith("skip: no fresh live quote at fill time")


def test_claude_default_quote_path_is_the_exit_check_snapshot(tmp_path):
    """With an injected snapshot (as the exit checks use), the fill quote comes from that same snapshot."""
    eng, _ = _engine(tmp_path, _eur_sell_answers())
    asyncio.run(eng.on_cycle(PAYLOADS, now=T0))
    p = eng.sims["C-OPUS"]["positions"][0]
    assert p["fill_source"] == "live_quote:dukascopy:tick" and p["fill_check"]["quote_price"] == 1.1
