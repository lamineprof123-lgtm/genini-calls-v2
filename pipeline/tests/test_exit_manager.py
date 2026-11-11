"""Gemini exit manager: strike logic, MFE/giveback math, book accounting, walls, prompt/schema, tick flow."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import exit_manager as em
from signals.exit_manager import (BOOK_1, BOOK_2, Books, ExitDecision, ExitManager, apply_strike,
                                  build_exit_prompt, find_walls, parse_decision, position_metrics)

UTC = timezone.utc
T0 = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)  # Wed 09:00 New York -> in Lucid session


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["lucid"]["slippage_ticks"] = 0
    c["exit_manager"]["enabled"] = True
    c["lucid"]["exit_mode"] = "gemini"          # legacy books with hard stop / max hold / breaker
    return c


def feed_entry(pid=1, side=-1, entry=1.11455, stop=1.1157, t=T0, sym="EURUSD", contract="6E", qty=5, atr=0.0004):
    return {"id": pid, "symbol": sym, "contract": contract, "qty": qty, "side": side, "entry": entry, "stop": stop,
            "target": entry + side * 0.00125, "entry_time": t.isoformat(), "risk_usd": 718.75, "atr": atr,
            "candle": {"high": 1.11573, "low": 1.1147}}


def m1(rows, start=T0):
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


# ------------------------------------------------------------------ strike logic
def test_one_strike_closes_on_first_close():
    r = apply_strike(BOOK_1, 0, "CLOSE", 0.1, 0.8)
    assert r["close"] and r["rule"] == "1-strike"
    assert not apply_strike(BOOK_1, 0, "HOLD", 0.1, 0.8)["close"]


def test_two_strike_needs_two_consecutive_closes_below_runner():
    r1 = apply_strike(BOOK_2, 0, "CLOSE", 0.3, 0.8)
    assert not r1["close"] and r1["strikes_after"] == 1 and r1["rule"] == "strike_1_of_2"
    r2 = apply_strike(BOOK_2, r1["strikes_after"], "CLOSE", 0.3, 0.8)
    assert r2["close"] and r2["rule"] == "2nd_consecutive_close"


def test_two_strike_hold_resets_streak():
    r1 = apply_strike(BOOK_2, 0, "CLOSE", 0.3, 0.8)
    r2 = apply_strike(BOOK_2, r1["strikes_after"], "HOLD", 0.3, 0.8)
    assert r2["strikes_after"] == 0 and not r2["close"]
    r3 = apply_strike(BOOK_2, r2["strikes_after"], "CLOSE", 0.3, 0.8)
    assert not r3["close"]


def test_two_strike_single_close_once_runner():
    r = apply_strike(BOOK_2, 0, "CLOSE", 0.8, 0.8)
    assert r["close"] and r["rule"] == "runner_single_close"
    assert not apply_strike(BOOK_2, 0, "CLOSE", 0.79, 0.8)["close"]


# ------------------------------------------------------------------ MFE / giveback
def test_metrics_short_giveback():
    # short from 1.1000, best 1.0990 (10 pips), now 1.0994 -> gave back 40%
    m = position_metrics(-1, 1.1000, 1.0994, 0.0010, 0.0005, 625000.0)
    assert m["pnl_atr"] == pytest.approx(1.2) and m["mfe_atr"] == pytest.approx(2.0)
    assert m["giveback_pct"] == pytest.approx(40.0)
    assert m["pnl_usd"] == pytest.approx(375.0)


def test_metrics_long_underwater_and_live_extends_mfe():
    m = position_metrics(1, 100.0, 99.0, 0.0, 2.0, 20.0)
    assert m["pnl_atr"] == -0.5 and m["mfe_atr"] == 0 and m["giveback_pct"] == 0
    m2 = position_metrics(1, 100.0, 103.0, 1.0, 2.0, 20.0)  # live beyond the recorded MFE
    assert m2["mfe_atr"] == 1.5 and m2["giveback_pct"] == 0


# ------------------------------------------------------------------ walls
def test_walls_short_and_long():
    ctx = {"levels": [{"type": "support", "label": "Session Low", "price": 1.1140},
                      {"type": "support", "label": "S/R", "price": 1.1130},
                      {"type": "resistance", "label": "Prev High", "price": 1.1160}],
           "poc": 1.1165, "vah": 1.1170, "val": 1.1150, "atr_upper": 1.118, "atr_lower": 1.110}
    w = find_walls(ctx, -1, 1.1145, 1.1146, 0.0005)
    assert w["next_wall_label"] == "Session Low" and w["dist_to_wall_atr"] == pytest.approx(1.0)
    assert w["invalid_level_label"] == "VAL"
    wl = find_walls(ctx, 1, 1.1172, 1.1150, 0.0005, {"low": 1.1144, "high": 1.1175})
    assert wl["next_wall_label"] == "+2 ATR band"
    assert wl["invalid_level_label"] == "Session Low"  # below min(entry, price)
    # Session Low printed by the move itself (last closed bar low) is price discovery, not a wall
    ctx2 = {**ctx, "signal_candle": {"low": 1.1140, "high": 1.1150}}
    w2 = find_walls(ctx2, -1, 1.11405, 1.1146, 0.0005)
    assert w2["next_wall_label"] == "S/R" and w2["next_wall_price"] == 1.1130
    w3 = find_walls({"levels": [], "atr_upper": 99.5}, 1, 100.0, 100.0, 1.0)   # band already broken
    assert w3["next_wall_price"] is None and w3["dist_to_wall_atr"] is None
    w0 = find_walls({"levels": []}, 1, 100.0, 100.0, 1.0, {"low": 99.0, "high": 101.0})
    assert w0["invalid_level_label"] == "Signal-candle low" and w0["invalid_level_price"] == 99.0


# ------------------------------------------------------------------ schema / prompt
def test_schema_field_order_analysis_first():
    assert tuple(ExitDecision.model_json_schema()["properties"]) == em.EXIT_FIELD_ORDER
    assert em.EXIT_FIELD_ORDER == ("structure", "reason", "decision", "confidence")


def test_parse_decision_ok_bad_and_clamped():
    d, err = parse_decision('{"structure":"expanding","reason":"rides EMA20","decision":"HOLD","confidence":1.4}')
    assert err is None and d["decision"] == "HOLD"
    d2, err2 = parse_decision('{"structure":"moon","reason":"x","decision":"SELL","confidence":0.5}')
    assert d2 is None and err2
    d3, err3 = parse_decision("")
    assert d3 is None and err3 == "empty response"


def test_prompt_is_m15_with_values(cfg):
    pos = {**feed_entry(), "mfe": 0.0004}
    ctx = {"digits": 5, "live_price": 1.1140, "live_price_ts_utc": "t", "vwap": 1.1160, "ema20_baseline": 1.1150,
           "ema50": 1.1165, "signal_candle": {"open": 1.1150, "high": 1.1152, "low": 1.1141, "close": 1.1143},
           "forming_m15_open": 1.1143, "forming_m15_high": 1.1145, "forming_m15_low": 1.1138,
           "minutes_into_bar": 6.5, "poc": 1.1165, "vah": 1.117, "val": 1.115, "session": "LONDON_NY_OVERLAP",
           "cvd_label": "BEARISH", "cvd_slope": -1200.5, "rvol_last": 1.3, "rvol_bars_since_spike": 2, "levels": []}
    m = position_metrics(-1, pos["entry"], 1.1140, pos["mfe"], pos["atr"], 625000.0)
    w = find_walls(ctx, -1, 1.1140, pos["entry"], pos["atr"], pos["candle"])
    txt = build_exit_prompt(cfg, pos, ctx, m, w, T0 + timedelta(minutes=7))
    assert "15-minute (M15)" in txt and "EURUSD (SHORT)" in txt and "1.11455" in txt
    assert "M15 EMA20" in txt and "Time in Trade: 7.0 min" in txt and "BEARISH" in txt
    assert "M1 EMA20" not in txt and "M1 candles" not in txt
    assert '"structure"' in txt and txt.index('"reason"') < txt.index('"decision"')
    assert "MFE < 0.8" in txt and "35%" in txt


# ------------------------------------------------------------------ book accounting
def test_books_copy_entries_and_net_after_commission(cfg):
    b = Books(cfg)
    opened = b.sync_feed([feed_entry()], T0 + timedelta(minutes=1))
    assert {o["book"] for o in opened} == {BOOK_1, BOOK_2}
    assert b.sync_feed([feed_entry()], T0 + timedelta(minutes=2)) == []  # never copied twice
    p = b.positions_for(1)[BOOK_1]
    rec = b.close(BOOK_1, p, 1.11355, T0 + timedelta(minutes=20), "gemini_close")
    # 10 pips x 5 x 125000 = 625; RT commission 2 x 2.40 x 5 = 24
    assert rec["gross"] == pytest.approx(625.0) and rec["net"] == pytest.approx(601.0)
    s = b.summary()
    assert s[BOOK_1]["trades"] == 1 and s[BOOK_1]["win_rate"] == 1.0 and s[BOOK_1]["net_pnl"] == 601.0
    assert s[BOOK_1]["balance"] == pytest.approx(100000 + 601.0)
    assert s[BOOK_2]["open_positions"] == 1 and s[BOOK_2]["balance"] == pytest.approx(100000 - 12.0)


def test_stale_feed_entries_not_copied(cfg):
    b = Books(cfg)
    assert b.sync_feed([feed_entry()], T0 + timedelta(minutes=45)) == []
    assert 1 in b.s["seen_ids"] and not b.open_ids()


def test_hard_stop_from_m1_and_gap(cfg):
    b = Books(cfg)
    b.sync_feed([feed_entry()], T0)
    p = b.positions_for(1)[BOOK_1]
    bars = m1([[1.11455, 1.11460, 1.11400, 1.11410],   # MFE 5.5 pips
               [1.11410, 1.11500, 1.11405, 1.11490],
               [1.11580, 1.11600, 1.11560, 1.11590]])  # gaps above the 1.1157 stop
    hit = b.scan_m1(p, bars)
    assert hit and hit[0] == pytest.approx(1.1158)  # fills at the gap open, not the stop
    assert p["mfe"] == pytest.approx(0.00055)
    p2 = b.positions_for(1)[BOOK_2]
    assert b.scan_m1(p2, bars.iloc[:2]) is None and p2["stop"] == 1.1157  # stop never moved


def test_circuit_breaker_flattens_and_halts(cfg):
    b = Books(cfg)
    b.sync_feed([feed_entry(qty=40, stop=1.2)], T0)
    b.s["last_price"]["EURUSD"] = 1.11455 + 0.0008   # 8 pips x 40 x 125000 = 4,000 > 3.5% of 100k
    out = b.breaker(T0 + timedelta(minutes=5))
    assert len(out) == 2 and all(r["reason"] == "circuit_breaker_3.5pct" for r in out)
    assert b.s["books"][BOOK_1]["day"]["halted"]
    assert b.sync_feed([feed_entry(pid=2)], T0 + timedelta(minutes=6)) == []  # halted books take no entries


# ------------------------------------------------------------------ manager tick (no network)
def _manager(cfg, tmp_path, decisions, price_rows):
    st = tmp_path / "state"
    (st / "lucid").mkdir(parents=True)
    (st / "lucid" / "account.json").write_text(json.dumps({"book_feed": [feed_entry()]}))
    calls = {"n": 0}

    async def snapshot(sym, c, now):
        df = m1(price_rows)
        ctx = {"digits": 5, "live_price": float(df["close"].iloc[-1]), "live_price_stale": False,
               "live_price_ts_utc": now.isoformat(), "atr_upper": 1.118, "atr_lower": 1.110, "levels": [],
               "vwap": 1.116, "ema20_baseline": 1.115, "ema50": 1.1165}
        return {"symbol": sym, "context": ctx, "digits": 5}, df

    async def render(payload):
        assert payload["entry_line"]["price"] == 1.11455 and payload["page_key"] == "exit-EURUSD"
        return b"\x89PNG fake"

    async def decide(prompt, png, pos, m):
        d = decisions[min(calls["n"], len(decisions) - 1)]
        calls["n"] += 1
        if d == "ERR":
            return {"model": None, "raw_text": None, "error": "TimeoutError: budget"}
        return {"model": "fake", "raw_text": json.dumps({"structure": "chopping", "reason": "test",
                                                         "decision": d, "confidence": 0.7})}

    mgr = ExitManager(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path,
                      snapshot=snapshot, render=render, decide=decide)
    return mgr, calls


def test_tick_one_call_shared_by_both_books(cfg, tmp_path):
    rows = [[1.11455, 1.11460, 1.11440, 1.11445]] * 3
    mgr, calls = _manager(cfg, tmp_path, ["CLOSE", "CLOSE"], rows)
    r = asyncio.run(mgr.tick(T0 + timedelta(minutes=3)))
    assert calls["n"] == 1                                  # one Gemini call for two books
    rec = r["records"][0]
    assert rec["books"][BOOK_1]["action"] == "close" and rec["books"][BOOK_2]["action"] == "hold"
    assert rec["books"][BOOK_2]["strikes_after"] == 1
    asyncio.run(mgr.tick(T0 + timedelta(minutes=3, seconds=18)))
    assert calls["n"] == 2 and not mgr.books.open_ids()     # 2nd consecutive CLOSE exits book B
    day = tmp_path / "logs" / "2026-11-11"
    lines = [json.loads(x) for x in (day / "exits.jsonl").read_text().splitlines()]
    assert len(lines) == 2 and lines[0]["t_dubai"].endswith("+04:00") and "." in lines[0]["t_utc"]
    assert lines[0]["gemini_raw_text"] and lines[0]["decision"] == "CLOSE"
    trades = [json.loads(x) for x in (day / "exit_book_trades.jsonl").read_text().splitlines()]
    assert {t["book"] for t in trades} == {BOOK_1, BOOK_2}
    summ = json.loads((tmp_path / "books.json").read_text())
    assert summ["books"][BOOK_1]["trades"] == 1 and "baseline" in summ
    assert asyncio.run(mgr.tick(T0 + timedelta(minutes=4)))["idle"]   # nothing open -> no call
    assert calls["n"] == 2


def test_tick_error_is_hold_and_resets_strike(cfg, tmp_path):
    rows = [[1.11455, 1.11460, 1.11440, 1.11445]] * 3
    mgr, calls = _manager(cfg, tmp_path, ["CLOSE", "ERR", "CLOSE"], rows)
    mgr.books.s["books"].pop(BOOK_1)                         # watch book B only
    asyncio.run(mgr.tick(T0 + timedelta(minutes=3)))
    r = asyncio.run(mgr.tick(T0 + timedelta(minutes=3, seconds=18)))
    rec = r["records"][0]
    assert rec["decision"] == "HOLD" and rec["decision_source"] == "error_default_hold" and rec["ai_error"]
    assert rec["books"][BOOK_2]["strikes_after"] == 0
    asyncio.run(mgr.tick(T0 + timedelta(minutes=3, seconds=36)))
    assert mgr.books.open_ids() == {1}                       # CLOSE, error, CLOSE != two consecutive


def test_tick_max_hold_without_gemini(cfg, tmp_path):
    rows = [[1.11455, 1.11460, 1.11440, 1.11445]] * 3
    mgr, calls = _manager(cfg, tmp_path, ["HOLD"], rows)
    asyncio.run(mgr.tick(T0 + timedelta(minutes=1)))
    r = asyncio.run(mgr.tick(T0 + timedelta(minutes=91)))
    assert calls["n"] == 1 and not mgr.books.open_ids()
    assert r["records"][0]["books"][BOOK_1]["action"] == "max_hold_90m"


def test_exit_loop_does_not_stack(cfg, tmp_path):
    class Slow:
        period = 0.05
        n = 0

        async def tick(self):
            Slow.n += 1
            await asyncio.sleep(0.12)   # slower than the period

    async def go():
        t = asyncio.create_task(em.run_exit_loop(Slow()))
        await asyncio.sleep(0.5)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert 2 <= Slow.n <= 5   # sequential, missed slots skipped (no pile-up)


def test_lucid_fill_feeds_books(tmp_path):
    from signals.lucid import LucidSim
    c = copy.deepcopy(load_config())
    sim = LucidSim(c, tmp_path / "state", tmp_path / "logs")
    sim.s["pending"].append({"symbol": "EURUSD", "side": -1, "created": T0.isoformat(),
                             "fill_after": T0.isoformat(), "expires": (T0 + timedelta(minutes=10)).isoformat(),
                             "atr": 0.0005, "scalp": {"tdist": 0.00125, "sdist": 0.0010}, "session": "LONDON_SESSION",
                             "telemetry": None, "gemini": {"action": "SELL", "entry": 1.1145, "confidence": 0.8,
                                                           "run_at_utc": T0.isoformat()}})
    sim._roll_day(T0)
    sim._try_fill(sim.s["pending"][0], (1.1145, 1.1146, 1.1144, 1.1145), T0)
    assert sim.s["positions"] and sim.s["book_feed"][0]["id"] == sim.s["positions"][0]["id"]
    assert sim.s["book_feed"][0]["stop"] == sim.s["positions"][0]["stop"]
