"""USER RULE 2026-11-12: hard stop -$500 NET of commission on main + 1-strike + 2-strike; 'nostop' shadow book
keeps the identical position on Gemini CLOSE only, with Gemini exit checks continuing after the stop-out."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.exit_manager import BOOK_1, BOOK_2, BOOK_NOSTOP, Books, ExitManager
from signals.hard_stop import hard_stop_level, net_at, hard_stop_reason, book_has_stop
from signals.lucid import LucidSim

UTC = timezone.utc
T0 = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)      # Wed 09:00 New York (in session)


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c.setdefault("exit_manager", {})["gemini_exit_calls_enabled"] = True
    assert c["lucid"]["exit_mode"] == "gemini_only" and c["lucid"]["hard_stop_net_usd"] == 500.0
    assert c["lucid"]["slippage_ticks"] == 1
    assert c["exit_manager"]["books"] == [BOOK_1, BOOK_NOSTOP]      # 2-strike killed live (t210u)
    c["exit_manager"]["books"] = [BOOK_1, BOOK_2, BOOK_NOSTOP]       # the stop logic is still tested with 3 books
    return c


def gc_short(pid=1, t=T0, entry=3945.1, qty=2):
    return {"id": pid, "symbol": "XAUUSD", "contract": "GC", "qty": qty, "side": -1, "entry": entry, "stop": None,
            "target": None, "entry_time": t.isoformat(), "risk_usd": None, "atr": 5.0, "candle": None}


def m1(rows, start=T0):
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def test_config_and_level_math(cfg):
    assert hard_stop_reason(cfg) == "hard_stop_500"
    assert book_has_stop(cfg, BOOK_1) and book_has_stop(cfg, BOOK_2) and not book_has_stop(cfg, BOOK_NOSTOP)
    p = gc_short()                                   # 2 GC: $200/pt, RT commission 9.20
    lvl = hard_stop_level(cfg, p)
    assert lvl == pytest.approx(3947.6)              # first tick at which NET <= -500
    assert net_at(cfg, p, lvl) <= -500 and net_at(cfg, p, lvl - 0.1) > -500
    nq = {"contract": "NQ", "qty": 6, "side": 1, "entry": 28000.0}
    lv = hard_stop_level(cfg, nq)
    assert lv == pytest.approx(27996.0) and net_at(cfg, nq, lv) <= -500 and net_at(cfg, nq, lv + 0.25) > -500
    eu = {"contract": "6E", "qty": 6, "side": -1, "entry": 1.10900}
    lv = hard_stop_level(cfg, eu)
    assert net_at(cfg, eu, lv) <= -500 and net_at(cfg, eu, lv - 0.00005) > -500


def _manager(cfg, tmp_path, decisions, rows_fn, live_fn, feed=None, books_state=None):
    st = tmp_path / "state"
    (st / "lucid").mkdir(parents=True, exist_ok=True)
    (st / "lucid" / "account.json").write_text(json.dumps({"book_feed": feed if feed is not None else [gc_short()]}))
    if books_state is not None:
        (st / "exit_books.json").write_text(json.dumps(books_state))
    calls = {"n": 0, "books": []}

    async def snapshot(sym, c, now):
        df = m1(rows_fn(now))
        ctx = {"digits": 2, "live_price": live_fn(now), "live_price_stale": False, "live_price_ts_utc": now.isoformat(),
               "atr_upper": 3960.0, "atr_lower": 3930.0, "levels": []}
        return {"symbol": sym, "context": ctx, "digits": 2}, df

    async def render(payload):
        return b"\x89PNG"

    async def decide(prompt, png, pos, m):
        d = decisions[min(calls["n"], len(decisions) - 1)]
        calls["n"] += 1
        return {"model": "fake", "raw_text": json.dumps({"structure": "healthy_pullback", "reason": "test",
                                                         "decision": d, "confidence": 0.66})}

    mgr = ExitManager(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path,
                      snapshot=snapshot, render=render, decide=decide)
    return mgr, calls


def test_live_price_stop_main_books_nostop_keeps_gemini_checks(cfg, tmp_path):
    flat = [[3945.1, 3945.3, 3944.9, 3945.0]] * 2
    live = {"px": 3945.0}
    mgr, calls = _manager(cfg, tmp_path, ["HOLD", "HOLD", "HOLD", "CLOSE"], lambda now: flat, lambda now: live["px"])
    asyncio.run(mgr.tick(T0 + timedelta(minutes=1)))                     # copy + first check: all hold
    assert calls["n"] == 1 and set(mgr.books.positions_for(1)) == {BOOK_1, BOOK_2, BOOK_NOSTOP}
    p1 = mgr.books.positions_for(1)[BOOK_1]
    assert p1["hard_stop_px"] == pytest.approx(3947.6) and p1["hard_stop_armed_at"] == T0.isoformat()
    assert "hard_stop_px" not in mgr.books.positions_for(1)[BOOK_NOSTOP]
    live["px"] = 3947.7                                                  # beyond the level (live print)
    r = asyncio.run(mgr.tick(T0 + timedelta(minutes=1, seconds=18)))
    rec = r["records"][0]
    for b in (BOOK_1, BOOK_2):
        assert rec["books"][b]["action"] == "hard_stop_500" and rec["books"][b]["trigger"] == "live_price"
        assert rec["books"][b]["exit"] == pytest.approx(3947.7)          # resting stop: level + 1 tick slippage
        assert rec["books"][b]["net"] == pytest.approx(-(2.6 * 200) - 9.2)
        assert rec["books"][b]["net_if_live_fill"] == pytest.approx(-(2.7 * 200) - 9.2)
    assert calls["n"] == 2 and rec["books_after_stop"] == [BOOK_NOSTOP]  # Gemini still called for the shadow
    assert rec["decision"] == "HOLD" and rec["confidence"] == 0.66 and rec["reason"] == "test"
    req = json.loads((tmp_path / "state" / "lucid" / "close_requests.json").read_text())
    assert req[-1]["id"] == 1 and req[-1]["reason"] == "hard_stop_500" and req[-1]["book"] == BOOK_1
    assert req[-1]["price"] == pytest.approx(3947.7)
    assert set(mgr.books.positions_for(1)) == {BOOK_NOSTOP}
    live["px"] = 3950.0                                                  # shadow keeps holding, -$1,000: no stop
    asyncio.run(mgr.tick(T0 + timedelta(minutes=1, seconds=36)))
    assert set(mgr.books.positions_for(1)) == {BOOK_NOSTOP} and calls["n"] == 3
    live["px"] = 3944.0
    r = asyncio.run(mgr.tick(T0 + timedelta(minutes=1, seconds=54)))      # Gemini CLOSE -> shadow exits
    assert r["records"][0]["books"][BOOK_NOSTOP]["action"] == "close" and not mgr.books.open_ids()
    req = json.loads((tmp_path / "state" / "lucid" / "close_requests.json").read_text())
    assert len(req) == 1                                                  # shadow close never touches main
    s = json.loads((tmp_path / "books.json").read_text())
    assert s["books"][BOOK_1]["exit_reasons"] == {"hard_stop_500": 1}
    assert s["books"][BOOK_NOSTOP]["exit_reasons"] == {"gemini_close": 1}
    assert s["books"][BOOK_NOSTOP]["net_pnl"] > 0 and s["books"][BOOK_NOSTOP]["hard_stop_net_usd"] is None
    assert "HARD STOP -$500" in s["note"]
    lines = [json.loads(x) for x in (tmp_path / "logs" / "2026-11-11" / "exits.jsonl").read_text().splitlines()]
    assert len(lines) == 4 and all(l["decision"] in ("HOLD", "CLOSE") and l["confidence"] == 0.66 for l in lines)
    assert lines[1]["hard_stop"]["trigger"] == "live_price"


def test_m1_path_stop_fills_at_level_with_slippage(cfg, tmp_path):
    rows = [[3945.1, 3945.3, 3944.9, 3945.0], [3945.0, 3948.0, 3944.9, 3946.0]]   # wick through 3947.6
    mgr, calls = _manager(cfg, tmp_path, ["HOLD"], lambda now: rows, lambda now: 3946.0)
    r = asyncio.run(mgr.tick(T0 + timedelta(minutes=2)))
    rec = r["records"][0]
    assert rec["books"][BOOK_1]["action"] == "hard_stop_500" and rec["books"][BOOK_1]["trigger"] == "m1_path"
    assert rec["books"][BOOK_1]["exit"] == pytest.approx(3947.7)          # level + 1 tick
    assert BOOK_NOSTOP in mgr.books.positions_for(1) and calls["n"] == 1


def test_positions_open_at_deploy_armed_now_not_retroactive(cfg, tmp_path):
    """Old state (no nostop book, no stop fields): nostop is cloned from 1-strike; the stop is armed at the first
    check, so an old M1 wick through the level does not close it, but a live price already beyond it does."""
    pos = gc_short()
    old = Books(cfg)
    old.s["books"].pop(BOOK_NOSTOP)
    old.sync_feed([pos], T0)
    for b in old.s["books"].values():
        for p in b["positions"]:
            p.pop("hard_stop_px", None); p.pop("hard_stop_armed_at", None)
    old.s["reset_token"] = cfg["lucid"]["reset_token"]
    old.s["books"][BOOK_1]["balance"] = 96255.0
    old.s["books"][BOOK_1]["stats"]["trades"] = 7
    later = T0 + timedelta(minutes=30)
    rows = [[3945.1, 3948.5, 3944.9, 3946.0]] + [[3946.0, 3946.5, 3945.5, 3946.0]] * 29   # old wick at T0
    live = {"px": 3946.0}
    mgr, calls = _manager(cfg, tmp_path, ["HOLD"], lambda now: rows, lambda now: live["px"], feed=[pos],
                          books_state=old.s)
    nb = mgr.books.s["books"][BOOK_NOSTOP]
    assert nb["balance"] == 96255.0 and nb["stats"]["trades"] == 7 and nb["cloned_from"]["book"] == BOOK_1
    assert [p["id"] for p in nb["positions"]] == [1]
    r = asyncio.run(mgr.tick(later))
    assert set(mgr.books.positions_for(1)) == {BOOK_1, BOOK_2, BOOK_NOSTOP}   # no retroactive stop
    p1 = mgr.books.positions_for(1)[BOOK_1]
    assert p1["hard_stop_armed_at"] == later.isoformat() and p1["hard_stop_armed_late"]
    live["px"] = 3949.0                                                   # crosses after a check with the stop working
    r = asyncio.run(mgr.tick(later + timedelta(seconds=18)))
    o = r["records"][0]["books"][BOOK_1]
    assert o["action"] == "hard_stop_500" and o["trigger"] == "live_price" and o["exit"] == pytest.approx(3947.7)
    assert set(mgr.books.positions_for(1)) == {BOOK_NOSTOP}


def test_deploy_position_already_beyond_stop_fills_at_live(cfg, tmp_path):
    """Open at deploy and already below -$500: no resting order existed -> closed at the first check, live fill."""
    pos = gc_short()
    old = Books(cfg)
    old.s["books"].pop(BOOK_NOSTOP)
    old.sync_feed([pos], T0)
    for b in old.s["books"].values():
        for p in b["positions"]:
            p.pop("hard_stop_px", None); p.pop("hard_stop_armed_at", None)
    old.s["reset_token"] = cfg["lucid"]["reset_token"]
    rows = [[3949.0, 3949.5, 3948.5, 3949.0]] * 5
    mgr, calls = _manager(cfg, tmp_path, ["HOLD"], lambda now: rows, lambda now: 3949.0, feed=[pos], books_state=old.s)
    r = asyncio.run(mgr.tick(T0 + timedelta(minutes=5)))
    o = r["records"][0]["books"][BOOK_1]
    assert o["action"] == "hard_stop_500" and o["trigger"] == "live_price_gap" and o["exit"] == pytest.approx(3949.1)
    assert r["records"][0]["books"][BOOK_NOSTOP]["action"] == "hold" and calls["n"] == 1


def test_shadow_overlap_tagged_on_reentry(cfg):
    b = Books(cfg)
    b.sync_feed([gc_short(1)], T0)
    p = b.positions_for(1)[BOOK_1]
    b.close(BOOK_1, p, 3947.8, T0 + timedelta(minutes=2), "hard_stop_500")
    b.sync_feed([gc_short(1), gc_short(2, t=T0 + timedelta(minutes=4))], T0 + timedelta(minutes=5))
    assert b.positions_for(2)[BOOK_NOSTOP].get("overlap") and not b.positions_for(2)[BOOK_1].get("overlap")
    b.close(BOOK_NOSTOP, b.positions_for(2)[BOOK_NOSTOP], 3944.1, T0 + timedelta(minutes=9), "gemini_close")
    s = b.summary()[BOOK_NOSTOP]
    assert s["overlap_trades"] == 1 and s["overlap_net"] == pytest.approx(200 - 9.2)
    assert s["net_pnl_excl_overlap"] == pytest.approx(0.0)


# ------------------------------------------------------------------ main Lucid account
@pytest.fixture
def sim(cfg, tmp_path):
    s = LucidSim(cfg, tmp_path / "state" / "lucid", tmp_path / "logs")
    s.s["last_processed"] = T0.isoformat()
    s.s["created_at"] = (T0 - timedelta(hours=1)).isoformat(); s.s.pop("reset_at", None)
    s._roll_day(T0)
    return s


def _fill(sim, t=T0, entry=3945.1):
    sim.s["pending"].append({"symbol": "XAUUSD", "side": -1, "created": t.isoformat(), "fill_after": t.isoformat(),
                             "expires": (t + timedelta(minutes=10)).isoformat(), "atr": 5.0, "scalp": {},
                             "session": "NEW_YORK_SESSION", "telemetry": None,
                             "gemini": {"action": "SELL", "entry": entry, "confidence": 0.8,
                                        "run_at_utc": t.isoformat()}})
    sim._try_fill(sim.s["pending"][0], (entry, entry, entry, entry), t)
    return sim.s["positions"][-1]


def _bars(rows, start):
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(len(rows))])
    return {"XAUUSD": pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)}


def test_main_m1_stop_reason_and_reentry(sim):
    p = _fill(sim)
    assert p["hard_stop_px"] is not None and p["hard_stop_armed_at"] == T0.isoformat()
    assert sim.s["book_feed"][-1]["hard_stop_px"] == p["hard_stop_px"]
    lvl = p["hard_stop_px"]
    rows = [[p["entry"], p["entry"] + 1, p["entry"] - 1, p["entry"]], [p["entry"], lvl + 0.5, p["entry"], lvl]]
    sim.process(_bars(rows, T0), T0 + timedelta(minutes=2))
    assert not sim.s["positions"]
    tr = sim.post_reset_trades()[-1]
    assert tr["reason"] == "hard_stop_500" and tr["stopped"] and tr["exit"] == pytest.approx(lvl + 0.1)
    assert tr["net"] == pytest.approx(net_at(sim.cfg, p, lvl + 0.1)) and -560 < tr["net"] <= -500
    # re-entry: the symbol is free again for the main account right after the stop
    assert sim._block_reason("XAUUSD", -1, T0 + timedelta(minutes=3)) is None
    j = sim.site_payload()
    assert j["card"] == "Hard stop −$500 net · TP: none · exit: Gemini" and j["hard_stop_net_usd"] == 500.0
    assert j["recent_trades"][0]["stopped"] is True and j["recent_trades"][0]["reason"] == "hard_stop_500"
    assert j["comparison"][0]["book"] == "main" and j["comparison"][0]["stopped"] == 1


def test_main_applies_exit_manager_stop_request(sim):
    p = _fill(sim)
    f = sim.state_dir / "close_requests.json"
    f.write_text(json.dumps([{"id": p["id"], "symbol": "XAUUSD", "t": (T0 + timedelta(seconds=40)).isoformat(),
                              "price": 3947.8, "book": BOOK_1, "rule": "hard_stop_500", "reason": "hard_stop_500",
                              "trigger": "live_price", "account_token": sim.s.get("reset_token")}]))
    flat = [[3945.1, 3945.2, 3945.0, 3945.1]] * 2
    sim.process(_bars(flat, T0), T0 + timedelta(minutes=2))
    tr = sim.post_reset_trades()[-1]
    assert tr["reason"] == "hard_stop_500" and tr["exit"] == pytest.approx(3947.8) and not sim.s["positions"]


def test_main_open_at_deploy_armed_from_now(sim):
    p = _fill(sim)
    p.pop("hard_stop_px"); p.pop("hard_stop_armed_at")                   # opened by the pre-stop code
    lvl = hard_stop_level(sim.cfg, p)
    rows = [[p["entry"], lvl + 2, p["entry"], lvl + 1]] * 3                # old bars already beyond the level
    sim.process(_bars(rows, T0), T0 + timedelta(minutes=3))
    assert sim.s["positions"] and sim.s["positions"][0]["hard_stop_armed_late"]   # not stopped retroactively


def test_comparison_reads_books(sim, cfg, tmp_path):
    _fill(sim)
    b = Books(cfg)
    b.sync_feed(sim.s["book_feed"], T0)
    (tmp_path / "state" / "exit_books.json").write_text(json.dumps(b.s))
    rows = {r["book"]: r for r in sim.site_payload()["comparison"]}
    assert set(rows) == {"main", BOOK_1, BOOK_2, BOOK_NOSTOP}
    assert rows[BOOK_NOSTOP]["hard_stop_net_usd"] is None and rows[BOOK_1]["hard_stop_net_usd"] == 500.0
    assert rows[BOOK_NOSTOP]["label"] == "No-stop shadow" and rows[BOOK_NOSTOP]["open_positions"] == 1
