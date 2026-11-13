"""EVAL-80 (t284u): G-LOW-80's shared call (no extra Gemini call), conf >= 0.80, 0.8 / 1.0 ATR / 90-min,
~$450 risk minis-else-micros with 2-mini / 6-mini caps, enforced LucidPro 100K eval rules, panel + scorer. No network."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import gha_loop
from signals.eval80 import Eval80Book, eval80_cfg, BOOK
from signals.glow80 import GLow80Book, glow80_cfg, lucid_day
from signals.other_venue import OtherVenueHub
from signals.m15_scan import M15Coordinator
from signals.signal_score import SignalScorer, score_key
from signals.panel import PanelLedger, BOOKS, write_reset_marker
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_glow80 import T0, Feed, snap, gem, s

UTC = timezone.utc


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


PRICES = {"XAUUSD": 3945.0, "BTCUSD": 90000.0, "EURUSD": 1.1, "US100": 21000.0}


def make(cfg, tmp_path, call, prices=None):
    hub = OtherVenueHub(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    g = GLow80Book(cfg, call=call, ov_hub=hub, quotes=Feed(prices or PRICES))
    e = Eval80Book(cfg, quotes=Feed(prices or PRICES))
    for b in (g, e):
        b.s["created_at"] = (T0 - timedelta(hours=1)).isoformat()
    return g, e, hub


def run(g, e, now=T0, sn=None):
    sn = sn or snap()
    r = asyncio.run(g.on_snapshot(sn, now=now))
    return r, asyncio.run(e.on_shared(r, sn, now=now))


def test_config_and_existing_books_unchanged(cfg):
    b = eval80_cfg(cfg)
    assert b["enabled"] and b["min_confidence"] == 0.80 and b["stop_atr"] == 0.8 and b["target_atr"] == 1.0
    assert b["max_hold_minutes"] == 90 and b["max_chase_atr"] == 0.3 and b["risk_usd"] == 450.0
    assert b["max_minis_per_position"] == 2 and b["max_minis"] == 6 and "BTCUSD" not in b["symbols"]
    assert b["dll"] == 1800 and b["mll"] == 3000 and b["profit_target"] == 6000
    assert b["locked_mll_balance"] == 100100 and b["initial_trail_balance"] == 103100
    assert b["flat_utc"] == glow80_cfg(cfg)["flat_utc"] and b["no_new_entries_last_minutes"] == 15
    g = glow80_cfg(cfg)   # G-LOW-80 stays at 0.70 with its 1 / 1 bracket
    assert g["min_confidence"] == 0.70 and g["stop_atr"] == 1.0 and g["target_atr"] == 1.0
    assert cfg["gemini"]["entry_thinking_level"] == "high"
    assert "eval80.json" in gha_loop.SITE_JSON
    assert any(x == BOOK for x, _, _ in BOOKS)
    assert BOOK not in cfg["other_venue"]["books"]
    assert not any(c.get("micro") for c in cfg["lucid"]["contracts"].values())   # MAIN's Lucid specs untouched


def test_sizing_minis_else_micros_and_caps(cfg, tmp_path):
    e = Eval80Book(cfg)
    # gold ATR 5: 0.8*5*100 = $400 per GC -> 1 GC
    assert e.size_for("XAUUSD", 5.0)[:2] == ("GC", 1)
    # gold ATR 8: $640 per GC > 450 -> micros: 0.8*8*10 = $64 -> 7 MGC
    assert e.size_for("XAUUSD", 8.0)[:2] == ("MGC", 7)
    # NQ ATR 50: $800 per NQ -> MNQ $80 -> 5
    assert e.size_for("US100", 50.0)[:2] == ("MNQ", 5)
    # EUR ATR 0.0006: $60 per 6E -> 7 wanted, capped at 2 minis
    assert e.size_for("EURUSD", 0.0006)[:2] == ("6E", 2)
    # silver ATR 0.15: $600 per SI -> SIL $120 -> 3
    assert e.size_for("XAGUSD", 0.15)[:2] == ("SIL", 3)
    # micro cap 20 per position
    assert e.size_for("US100", 2.0)[:2] == ("NQ", 2)          # $32 per NQ -> 14 wanted -> 2 minis
    assert e.size_for("XAUUSD", 0.5)[:2] == ("GC", 2)
    # total cap: 55 micro-eq used -> 5 left -> micros
    e.s["positions"] = [{"symbol": "X", "contract": "NQ", "qty": 2}, {"symbol": "Y", "contract": "6E", "qty": 2},
                        {"symbol": "Z", "contract": "MGC", "qty": 15}]
    assert e.micro_eq_used() == 55
    assert e.size_for("EURUSD", 0.0006)[:2] == ("M6E", 5)     # 2 minis wanted -> downsized into 5 micros
    e.s["positions"].append({"symbol": "W", "contract": "MNQ", "qty": 5})
    c, q, why = e.size_for("EURUSD", 0.0006)
    assert c is None and why.startswith("max 6 minis")


def test_shared_call_conf_080_bracket_and_no_btc(cfg, tmp_path):
    call = gem([s("XAUUSD", "SELL", 0.85, 3945.0), s("EURUSD", "BUY", 0.75, 1.1),
                s("US100", "HOLD", 0.3, 21000.0), s("BTCUSD", "BUY", 0.9, 90000.0)])
    g, e, hub = make(cfg, tmp_path, call)
    r, out = run(g, e)
    assert len(call.calls) == 1                                    # ONE Gemini call shared by both books
    assert out["ok"] and out["queued"] == ["XAUUSD"]                # EUR 0.75 < 0.80; BTC not traded here
    assert [p["symbol"] for p in g.s["positions"]] == ["XAUUSD", "EURUSD"]   # G-LOW-80 unchanged (0.70)
    p = e.s["positions"][0]
    assert p["book"] == BOOK and p["side"] == -1 and p["contract"] == "GC" and p["qty"] == 1   # 0.8*5*100 = $400
    assert abs(p["stop"] - p["entry"] - 4.0) < 0.11 and abs(p["entry"] - p["target"] - 5.0) < 0.11
    assert not any(x["symbol"] == "BTCUSD" for x in e.s["positions"] + e.s["pending"])
    day = Path(cfg["general"]["logs_dir"]) / "2026-11-13"
    line = json.loads((day / "eval80_signals.jsonl").read_text().splitlines()[0])
    assert line["source_book"] == "G-LOW-80" and line["eval"]["status"] == "ACTIVE"
    assert {x["symbol"] for x in line["signals"]} == {"XAUUSD", "EURUSD", "US100"}
    site = json.loads((tmp_path / "eval80.json").read_text())
    assert site["eval"]["mll_floor"] == 97000.0 and site["eval"]["progress_pct"] == 0
    # 0.3 ATR fill check
    call2 = gem([s("XAUUSD", "SELL", 0.9, 3945.0)])
    c2 = copy.deepcopy(cfg)
    c2["general"]["state_dir"] = str(tmp_path / "b" / "state")
    c2["general"]["logs_dir"] = str(tmp_path / "b" / "logs")
    c2["site"]["dir"] = str(tmp_path / "b")
    g2, e2, _ = make(c2, tmp_path / "b", call2, prices={"XAUUSD": 3943.0})
    run(g2, e2)
    assert e2.s["positions"] == [] and e2.s["stats"]["skips"].get("chase > max_chase_atr") == 1


def test_hold_cycle_runs(cfg, tmp_path):
    g, e, _ = make(cfg, tmp_path, gem([s("XAUUSD", "HOLD", 0.3, 3945.0)]))
    _, out = run(g, e)
    assert out["ok"] and out["queued"] == [] and e.s["cycles"]["seen"] == 1


def test_stop_08_and_target_and_time(cfg, tmp_path):
    g, e, _ = make(cfg, tmp_path, gem([s("XAUUSD", "SELL", 0.9, 3945.0)]))
    run(g, e)
    p = e.s["positions"][0]
    e._quotes = Feed({"XAUUSD": p["stop"] + 0.1})
    asyncio.run(e.tick(T0 + timedelta(minutes=3)))
    assert e.s["positions"] == [] and e.s["stats"]["exit_reasons"] == {"stop_0.8atr": 1}
    rec = json.loads((Path(cfg["general"]["logs_dir"]) / "2026-11-13" / "eval80_trades.jsonl").read_text())
    assert rec["book"] == BOOK and rec["net"] < 0 and e.day_net(T0) == rec["net"]


def _open(e, sym="XAUUSD", contract="GC", qty=6, entry=3945.0, side=1, atr=5.0):
    e.s["positions"].append({"id": e.s["next_id"], "symbol": sym, "contract": contract, "qty": qty, "side": side,
                             "entry": entry, "entry_time": T0.isoformat(), "stop": entry - side * 4, "target": entry + side * 5,
                             "deadline": (T0 + timedelta(minutes=90)).isoformat(), "atr": atr, "confidence": 0.9,
                             "mfe": 0, "mae": 0, "scan_from": T0.isoformat(), "checks": 0})
    e.s["next_id"] += 1


def test_dll_soft_pause(cfg, tmp_path):
    e = Eval80Book(cfg)
    e.s["day"] = {"day": lucid_day(T0), "net": -1000.0}
    _open(e)
    e.s["last_price"]["XAUUSD"] = 3945.0 - 1.5          # 6 GC * 100 * -1.5 = -900 open -> -1,900 on the day
    assert e.enforce(T0)
    assert e.s["positions"] == [] and e.dll_paused(T0) and e.active()
    assert e.eligible({"symbol": "XAUUSD", "action": "BUY", "confidence": 0.9, "atr": 5.0}) is not None
    # next Lucid day: unpaused
    assert not e.dll_paused(T0 + timedelta(days=1))


def test_mll_trailing_lock_and_intraday_fail(cfg, tmp_path):
    e = Eval80Book(cfg)
    e.enforce(T0)
    assert e.ev["mll_floor"] == 97000.0
    e.s["balance"] = 101500.0
    e.enforce(T0 + timedelta(days=1))                       # EOD close 101,500 -> floor 98,500
    assert e.ev["mll_floor"] == 98500.0 and not e.ev["mll_locked"]
    e.s["balance"] = 100800.0
    e.enforce(T0 + timedelta(days=2))                       # lower close: floor does not move down
    assert e.ev["mll_floor"] == 98500.0
    e.s["balance"] = 103200.0
    e.enforce(T0 + timedelta(days=3))                       # close above 103,100 -> locked at 100,100
    assert e.ev["mll_floor"] == 100100.0 and e.ev["mll_locked"]
    e.s["balance"] = 105500.0
    e.enforce(T0 + timedelta(days=4))
    assert e.ev["mll_floor"] == 100100.0                    # stays locked
    # intraday breach -> FAILED, flattened, trading stops, record kept
    t = T0 + timedelta(days=4, hours=1)
    e.s["balance"] = 100400.0
    _open(e, qty=1)
    e.s["last_price"]["XAUUSD"] = 3945.0 - 4.0               # -$400 open -> equity 100,000 <= 100,100
    e.enforce(t)
    assert e.ev["status"] == "FAILED" and e.s["positions"] == [] and "MLL" in e.ev["reason"]
    why = e.eligible({"symbol": "XAUUSD", "action": "BUY", "confidence": 0.9, "atr": 5.0})
    assert why and "FAILED" in why
    e.enforce(t + timedelta(days=1))
    assert e.ev["status"] == "FAILED"                        # no auto-reset


def test_profit_target_passes_and_stops(cfg, tmp_path):
    e = Eval80Book(cfg)
    e.s["balance"] = 105600.0
    _open(e, qty=1)
    e.s["last_price"]["XAUUSD"] = 3945.0 + 5.0               # +$500 open -> equity 106,100
    e.enforce(T0)
    assert e.ev["status"] == "PASSED" and e.s["positions"] == [] and e.s["balance"] >= 106000.0
    assert e.eval_view(T0)["progress_pct"] >= 100
    g, _, _ = make(cfg, tmp_path, gem([s("XAUUSD", "SELL", 0.9, 3945.0)]))
    r = asyncio.run(g.on_snapshot(snap(), now=T0))
    out = asyncio.run(e.on_shared(r, snap(), now=T0))
    assert out["queued"] == [] and e.s["stats"]["skips"]                 # no new trades after PASSED


def test_m15_dispatch_logs_scorer_and_single_call(cfg, tmp_path):
    call = gem([s("XAUUSD", "SELL", 0.85, 3945.0)])
    g, e, _ = make(cfg, tmp_path, call)
    scorer = SignalScorer(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    co = M15Coordinator(cfg, glow80=g, eval80=e, scorer=scorer, state_dir=tmp_path / "state",
                        logs_dir=tmp_path / "logs", site_dir=tmp_path)
    out = asyncio.run(co.dispatch(snap()))
    assert len(call.calls) == 1 and out["glow80"]["ok"] and out["glow80"]["eval80"]["ok"]
    rows = [json.loads(x) for x in next((tmp_path / "logs").glob("*/m15_calls.jsonl")).read_text().splitlines()]
    ev = [x for x in rows if x["book"] == BOOK]
    assert len(ev) == 1 and ev[0]["shared_from"] == "G-LOW-80" and ev[0]["cost_usd"] == 0.0
    sig = lambda b: [x for x in scorer.s["queue"] if x.get("book") == b and x["role"] == "signal"]
    assert len(sig(BOOK)) == 1 and len(sig("G-LOW-80")) == 1 and sig(BOOK)[0]["traded"]
    scorer.ingest_logs()                                      # idempotent backfill
    assert len(sig(BOOK)) == 1 and len(sig("G-LOW-80")) == 1
    # G-LOW-80 keys unchanged (no book suffix); EVAL-80 has its own
    assert not score_key(sig("G-LOW-80")[0]).endswith("G-LOW-80") and score_key(sig(BOOK)[0]).endswith(BOOK)


def test_panel_card_and_positions(cfg, tmp_path):
    cfg["reset"] = {"token": "tok-1"}
    cfg["other_venue"]["reset_token"] = "tok-1"
    st = tmp_path / "state"
    write_reset_marker(cfg, st, T0 - timedelta(hours=1))
    g, e, _ = make(cfg, tmp_path, gem([s("US100", "BUY", 0.9, 21000.0)]))
    # NQ ATR 40 -> 0.8*40*20 = $640 per NQ -> MNQ $64 -> 7 micros
    run(g, e)
    assert e.s["positions"][0]["contract"] == "MNQ" and e.s["positions"][0]["qty"] == 7

    async def qf(syms):
        return {"US100": {"price": 21010.0}}
    led = PanelLedger(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path, quote_fn=qf,
                      history_fn=False)
    asyncio.run(led.tick(T0 + timedelta(minutes=1)))
    site = json.loads((tmp_path / "panel.json").read_text())
    perf = [p for p in site["performance"] if p["book"] == BOOK]
    assert [p["venue"] for p in perf] == ["LUCID"]                     # no other-venue account
    assert perf[0]["eval"]["status"] == "ACTIVE" and perf[0]["eval"]["mll_floor"] == 97000.0
    assert perf[0]["open_positions"] == 1
    row = next(r for r in site["positions"] if r["book"] == BOOK)
    assert row["contract"] == "MNQ" and row["status"] == "OPEN" and "went_positive_first" in row
    assert row["last_pnl_usd"] == round((21010.0 - row["entry"]) * 2.0 * 7, 2)   # micro point value used
    assert abs(row["fees_usd"] - 2 * 0.5 * 7) < 1e-9


def test_index_html_eval_card():
    html = (Path(__file__).resolve().parent.parent.parent / "index.html").read_text()
    assert "evalCard" in html and "MLL floor" in html and "DLL used" in html and "Missed (G-LOW-80 held" in html
    assert "countdown" not in html.lower().replace("no countdown", "")


def test_missed_when_glow80_holds(cfg, tmp_path):
    call = gem([s("XAUUSD", "SELL", 0.75, 3945.0)])            # G-LOW-80 takes it (0.70), EVAL-80 does not
    g, e, _ = make(cfg, tmp_path, call)
    run(g, e)
    assert g.held("XAUUSD") and not e.held("XAUUSD")
    _, out = run(g, e, now=T0 + timedelta(minutes=15))
    assert "XAUUSD" not in call.calls[1]["symbols"] and out["missed_held_by_glow80"] == ["XAUUSD"]
    v = e.eval_view(T0)
    assert v["missed_total"] == 1 and v["missed_scans"] == {"XAUUSD": 1}


def test_sizing_table_expected(cfg):
    e = Eval80Book(cfg)
    rows = {r["symbol"]: r for r in e.sizing_table({"XAUUSD": 8.0, "US100": 50.0, "XAGUSD": 0.25,
                                                     "EURUSD": 0.0006, "GBPUSD": 0.0008})}
    assert (rows["XAUUSD"]["contract"], rows["XAUUSD"]["qty"]) == ("MGC", 7)
    assert (rows["US100"]["contract"], rows["US100"]["qty"]) == ("MNQ", 5)
    assert (rows["XAGUSD"]["contract"], rows["XAGUSD"]["qty"]) == ("SIL", 2)
    assert (rows["EURUSD"]["contract"], rows["EURUSD"]["qty"]) == ("6E", 2)
    assert (rows["GBPUSD"]["contract"], rows["GBPUSD"]["qty"]) == ("6B", 2)
