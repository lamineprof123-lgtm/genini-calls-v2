"""Carbon side books CB-BRACKET / CB-RUNNER (playbook t221u/t222u): gemini-4-carbon entries, binding risk rules,
spend cap, triggered checks, what-if scoring, isolation from every other book."""
import asyncio, copy, hashlib, json, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import carbon as C
from signals import gha_loop

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 14, 0, 20, tzinfo=UTC)   # Thu, inside 08:00-20:30 UTC, in session


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c.setdefault("exit_manager", {})["gemini_exit_calls_enabled"] = True
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


def q(sym, price, t=T0, age=1.0, stale=False, source="dukascopy:tick"):
    return {"symbol": sym, "price": price, "ts": t.isoformat(), "fetched_at": t.isoformat(), "age_s": age,
            "stale": stale, "source": source}


class Feed:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), 0

    async def __call__(self, symbols, now):
        i = min(self.calls, len(self.steps) - 1)
        self.calls += 1
        return self.steps[i]


def payload(sym, price, atr, blocked=False, levels=None, vols=None, bar="2026-11-12T13:45:00+00:00"):
    return {"symbol": sym, "source": "dukascopy", "ticker": sym.lower(), "timeframe": "M15", "digits": 2,
            "style": {"width": 1280, "height": 720},
            "volume": [{"value": v} for v in (vols or [100.0] * 20)],
            "context": {"atr": atr, "live_price": price, "last_close": price, "live_price_stale": False,
                        "last_closed_bar_utc": bar, "session": "NEW_YORK_SESSION",
                        "session_blocks_automation": blocked, "levels": levels or [], "poc": None, "vah": None,
                        "val": None, "digits": 2, "signal_candle": {"open": price, "high": price, "low": price,
                                                                    "close": price}}}


class Snap:
    def __init__(self, table):
        self.table, self.calls = table, []

    async def __call__(self, sym, cfg, now):
        self.calls.append(sym)
        return self.table[sym], None


async def render(payloads):
    return {p["symbol"]: b"\xff\xd8\xff fake" for p in payloads}


class Api:
    def __init__(self, signals, usage=None, ok=True):
        self.signals, self.calls, self.ok = signals, [], ok
        self.usage = usage or {"prompt_token_count": 8000, "candidates_token_count": 1000, "thoughts_token_count": 2000,
                               "total_token_count": 11000}

    async def __call__(self, cc, **kw):
        self.calls.append(kw)
        if not self.ok:
            return {"ok": False, "raw_text": None, "usage": None, "latency_s": 90.0, "error": "TimeoutError"}
        return {"ok": True, "raw_text": json.dumps({"signals": self.signals, "market_summary": "x"}),
                "usage": self.usage, "latency_s": 7.5, "model_version": "gemini-4-carbon"}


def sig(sym, action, conf, entry):
    return {"symbol": sym, "trend_vs_vwap": "x", "volatility": "normal", "level_event": "none", "rationale": "r",
            "action": action, "confidence": conf, "entry": entry, "stop_loss": 0, "take_profit": 0, "risk_reward": 0}


SNAP = {"XAUUSD": payload("XAUUSD", 3900.0, 8.0), "EURUSD": payload("EURUSD", 1.1500, 0.0005),
        "US100": payload("US100", 25000.0, 40.0)}


def engine(cfg, api=None, feed=None, snap=None):
    e = C.CarbonEngine(cfg, snapshot=snap or Snap(copy.deepcopy(SNAP)), render=render, api=api or Api([]),
                       quotes=feed or Feed(({}, {})))
    for b in e.books.values():
        b.s["created_at"] = (T0 - timedelta(hours=1)).isoformat()
    return e


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ------------------------------------------------------------------ config / pricing / spend
def test_config_matches_playbook(cfg):
    cc = C.carbon_cfg(cfg)
    assert cc["model"] == "gemini-4-carbon" and cc["entry_thinking"] == "medium" and cc["exit_thinking"] == "low"
    assert cc["symbols"] == ["XAUUSD", "EURUSD", "US100"] and cc["entry_window_utc"] == ["08:00", "20:30"]
    assert cc["timeout_seconds"] == 90 and cc["min_confidence"] == 0.75 and cc["max_chase_atr"] == 0.5
    assert cc["books"]["CB-BRACKET"] == {"stop_atr": 1.0, "target_atr": 1.0, "max_hold_min": 90, "model_exits": False}
    r = cc["books"]["CB-RUNNER"]
    assert (r["lock_at_atr"], r["lock_to_atr"], r["trail_atr"], r["target_atr"], r["max_hold_min"]) == (1.0, 0.5, 0.75, 2.5, 150)
    assert cc["sizing"]["caps"] == {"US100": 1, "XAUUSD": 1, "EURUSD": 4} and cc["sizing"]["aggregate_max"] == 6
    assert cc["spend_cap"]["soft_usd"] == 15 and cc["spend_cap"]["hard_usd"] == 19
    assert cc["spend_cap"]["max_output_tokens"] == {"entry": 6000, "exit": 800}
    assert cc["triggered_checks"]["max_per_day"] == 40 and cc["triggered_checks"]["max_per_symbol_per_bar"] == 1


def test_price_by_date_and_cost(cfg):
    cc = C.carbon_cfg(cfg)
    assert C.price_for(cc, date(2026, 11, 12)) == (2.0, 10.0)
    assert C.price_for(cc, date(2026, 11, 30)) == (2.0, 10.0)
    assert C.price_for(cc, date(2026, 12, 1)) == (4.0, 20.0)
    u = {"prompt_token_count": 10000, "candidates_token_count": 1000, "thoughts_token_count": 2000}
    assert C.call_cost(u, (2.0, 10.0)) == pytest.approx(0.02 + 0.03)
    assert C.call_cost(u, (4.0, 20.0)) == pytest.approx(0.04 + 0.06)


def test_spend_gates(cfg):
    e = engine(cfg)
    sp = e.spend_today(T0)
    assert e.gate("entry", T0) is None and e.gate("trigger", T0) is None
    sp["usd"] = 15.0
    assert e.gate("entry", T0) is None
    assert "soft" in e.gate("trigger", T0) and "soft" in e.gate("exit", T0)
    sp["usd"] = 19.0
    assert "hard" in e.gate("entry", T0)
    # the running total is per Dubai day: 20:00 UTC is already the next Dubai day
    assert C.spend_day(datetime(2026, 11, 12, 20, 1, tzinfo=UTC)) == "2026-11-13"
    assert e.gate("entry", datetime(2026, 11, 12, 20, 1, tzinfo=UTC)) is None


# ------------------------------------------------------------------ sizing
def test_sizing_caps_and_skip(cfg):
    b = engine(cfg).books["CB-BRACKET"]
    assert b.size("XAUUSD", "GC", 8.0)[0] == 1                       # $800 -> 1, cap 1
    assert b.size("XAUUSD", "GC", 13.0)[1].startswith("risk>max")      # $1,300 > $1,200
    assert b.size("US100", "NQ", 40.0)[0] == 1                         # $800
    assert b.size("US100", "NQ", 61.0)[1].startswith("risk>max")       # $1,220
    assert b.size("EURUSD", "6E", 0.0005)[0] == 4                      # floor(900/62.5)=14 -> cap 4
    assert b.size("EURUSD", "6E", 0.0012)[0] == 4                      # floor(900/150)=6 -> cap 4
    assert b.size("EURUSD", "6E", 0.0020)[0] == 3                      # floor(900/250)=3


# ------------------------------------------------------------------ end-to-end scan
def test_scan_opens_both_books_and_scores_whatif(cfg, tmp_path):
    api = Api([sig("XAUUSD", "SELL", 0.82, 3900.0), sig("EURUSD", "BUY", 0.60, 1.1500), sig("US100", "HOLD", 0.5, 25000)])
    feed = Feed(({"XAUUSD": q("XAUUSD", 3900.0), "EURUSD": q("EURUSD", 1.1500)}, {}))
    e = engine(cfg, api, feed)
    r = run(e.scan(T0))
    assert r["ok"] and r["sent"] == ["XAUUSD", "EURUSD", "US100"]
    kw = api.calls[0]
    assert kw["thinking"] == "medium" and kw["max_output_tokens"] == 6000
    for name in C.BOOKS:
        P = e.books[name].s["positions"]
        assert [p["symbol"] for p in P] == ["XAUUSD"] and P[0]["side"] == -1 and P[0]["qty"] == 1
        assert P[0]["entry"] == pytest.approx(3899.9)                   # live quote - 1 tick (selling)
    rb = e.books["CB-RUNNER"].s["positions"][0]
    assert rb["target"] == pytest.approx(3899.9 - 2.5 * 8.0, abs=0.11)
    # what-if: both BUY/SELL (0.82 and 0.60) through both exits = 4 virtual trades
    assert len(e.e["whatif"]) == 4 and all(w["entry"] for w in e.e["whatif"])
    # spend booked from usage: 8000*2 + (1000+2000)*10 per 1M = 0.016 + 0.03
    assert e.spend_today(T0)["usd"] == pytest.approx(0.046)
    view = json.loads((tmp_path / "carbon.json").read_text())
    assert view["spend"]["usd"] == pytest.approx(0.046) and view["first_call"]["ok"]
    assert view["first_call"]["thoughts_token_count"] == 2000
    assert {b["book"] for b in view["books"]} == set(C.BOOKS)
    day = (tmp_path / "logs").glob("*/carbon_calls.jsonl")
    assert len(list(day)) == 1


def test_chase_limit_skips(cfg):
    api = Api([sig("XAUUSD", "SELL", 0.9, 3910.0)])                    # quoted 3910, live 3900 -> 1.26 ATR worse
    feed = Feed(({"XAUUSD": q("XAUUSD", 3900.0)}, {}))
    e = engine(cfg, api, feed)
    run(e.scan(T0, symbols=["XAUUSD"]))
    assert all(not b.s["positions"] for b in e.books.values())
    assert e.books["CB-BRACKET"].s["stats"]["skips"].get("chase") == 1


def test_blocked_session_and_window(cfg):
    snap = copy.deepcopy(SNAP)
    snap["EURUSD"] = payload("EURUSD", 1.15, 0.0005, blocked=True)
    api = Api([])
    e = engine(cfg, api, snap=Snap(snap))
    r = run(e.scan(T0))
    assert "EURUSD" not in r["sent"] and r["blocked_session"] == ["EURUSD"]
    r2 = run(e.scan(datetime(2026, 11, 12, 20, 45, 20, tzinfo=UTC)))
    assert r2["skipped"].startswith("outside") and len(api.calls) == 1
    assert not e.entries_allowed(datetime(2026, 11, 12, 7, 59, tzinfo=UTC))
    assert e.entries_allowed(datetime(2026, 11, 12, 8, 0, tzinfo=UTC))
    assert not e.entries_allowed(datetime(2026, 11, 14, 12, 0, tzinfo=UTC))   # Saturday


def test_hard_cap_blocks_all_calls_soft_cap_blocks_triggers(cfg):
    api = Api([])
    e = engine(cfg, api)
    e.spend_today(T0)["usd"] = 19.5
    r = run(e.scan(T0))
    assert "hard" in r["skipped"] and not api.calls
    e.spend_today(T0)["usd"] = 16.0
    run(e.scan(T0))
    assert len(api.calls) == 1                                            # M15 entry still allowed at soft cap
    assert run(e.trigger_poll(T0 + timedelta(minutes=5))) == []           # triggers off at soft cap


def test_symbol_held_by_both_books_not_sent(cfg):
    e = engine(cfg)
    for b in e.books.values():
        b.s["positions"].append({"symbol": "XAUUSD", "qty": 1, "id": 99, "side": 1, "contract": "GC", "entry": 3900,
                                 "stop": 3892, "atr": 8, "entry_time": T0.isoformat()})
    assert e.symbols_to_send() == ["EURUSD", "US100"]
    e.books["CB-RUNNER"].s["positions"] = []
    assert e.symbols_to_send() == ["XAUUSD", "EURUSD", "US100"]


def test_failed_call_logged_no_trade(cfg):
    api = Api([], ok=False)
    e = engine(cfg, api)
    r = run(e.scan(T0))
    assert r["ok"] is False and e.e["last_call"]["error"] == "TimeoutError"
    assert e.spend_today(T0)["failed"] == 1 and len(api.calls) == 1      # one attempt, no fallback


# ------------------------------------------------------------------ exits
def _open(e, name, sym="XAUUSD", side=1, price=3900.0, atr=8.0, t=T0):
    b = e.books[name]
    b.intake({"symbol": sym, "action": "BUY" if side > 0 else "SELL", "confidence": 0.8, "atr": atr, "entry": price,
              "kind": "entry", "signal_at": t.isoformat()}, t)
    b.fill_pendings({sym: q(sym, price, t)}, t)
    return b, b.s["positions"][0]


def test_bracket_target_and_R(cfg):
    e = engine(cfg)
    b, p = _open(e, "CB-BRACKET")
    assert p["stop"] == pytest.approx(3892.1) and p["target"] == pytest.approx(3908.1)
    out = b.manage({"XAUUSD": q("XAUUSD", 3908.5, T0 + timedelta(minutes=5))}, {}, T0 + timedelta(minutes=5))
    c = out[0]
    assert c["reason"] == "target" and c["exit"] == pytest.approx(3908.0)
    assert c["net"] == pytest.approx((3908.0 - 3900.1) * 100 - 4.6, abs=0.01)
    assert c["R"] == pytest.approx(c["net"] / p["risk_usd"], abs=1e-3)


def test_bracket_90min_and_flat(cfg):
    e = engine(cfg)
    b, p = _open(e, "CB-BRACKET")
    out = b.manage({"XAUUSD": q("XAUUSD", 3901.0, T0 + timedelta(minutes=91))}, {}, T0 + timedelta(minutes=91))
    assert out[0]["reason"] == "time_90min"
    t = datetime(2026, 11, 12, 21, 30, tzinfo=UTC)
    b2, p2 = _open(e, "CB-RUNNER", t=datetime(2026, 11, 12, 20, 0, tzinfo=UTC))
    tf = datetime(2026, 11, 12, 21, 45, 10, tzinfo=UTC)
    out = b2.manage({"XAUUSD": q("XAUUSD", 3901.0, tf)}, {}, tf)
    assert out[0]["reason"] == "flat_2145utc"


def test_runner_lock_trail_and_stop(cfg):
    e = engine(cfg)
    b, p = _open(e, "CB-RUNNER")                   # long 3900.1, ATR 8
    t = T0 + timedelta(minutes=3)
    assert not b.manage({"XAUUSD": q("XAUUSD", 3908.2, t)}, {}, t)     # +1.01 ATR -> lock
    assert p["locked"] and p["stop"] == pytest.approx(3904.1, abs=0.11)   # entry + 0.5 ATR (trail 3902.2 is lower)
    t = T0 + timedelta(minutes=6)
    assert not b.manage({"XAUUSD": q("XAUUSD", 3914.0, t)}, {}, t)
    assert p["stop"] == pytest.approx(3914.0 - 6.0, abs=0.11)            # trail 0.75 ATR behind best
    t = T0 + timedelta(minutes=8)
    out = b.manage({"XAUUSD": q("XAUUSD", 3907.5, t)}, {}, t)
    assert out[0]["reason"] == "trail_stop" and out[0]["net"] > 0
    assert out[0]["exit"] == pytest.approx(3907.9, abs=0.11)            # resting stop - 1 tick


def test_runner_target_25atr_via_m1(cfg):
    e = engine(cfg)
    b, p = _open(e, "CB-RUNNER")
    idx = pd.date_range(T0.replace(second=0) + timedelta(minutes=1), periods=3, freq="min", tz=UTC)
    bars = pd.DataFrame({"open": [3901, 3912, 3915], "high": [3912, 3916, 3921], "low": [3900.5, 3910, 3914],
                         "close": [3911, 3915, 3920]}, index=idx)
    t = T0 + timedelta(minutes=4)
    out = b.manage({"XAUUSD": q("XAUUSD", 3920.0, t)}, {"XAUUSD": bars}, t)
    assert out[0]["reason"] == "target" and out[0]["trigger"] == "m1_path"


def test_reentry_locks(cfg):
    e = engine(cfg)
    b, p = _open(e, "CB-BRACKET")
    t = T0 + timedelta(minutes=2)
    c = b.manage({"XAUUSD": q("XAUUSD", 3891.0, t)}, {}, t)[0]          # stop -> loss
    assert c["net"] < 0
    lk = b.s["locks"]["XAUUSD"]
    assert C._dt(lk["until"]) == t + timedelta(minutes=30)
    assert b.lock_reason("XAUUSD", -1, t + timedelta(minutes=10)).startswith("reentry_lock")
    assert b.lock_reason("XAUUSD", -1, t + timedelta(minutes=31)) is None       # opposite side ok after 30 min
    assert "same-side" in b.lock_reason("XAUUSD", 1, t + timedelta(minutes=31))
    # a win: locked only until the next M15 close
    b.s["locks"] = {}
    b2, p2 = _open(e, "CB-BRACKET", sym="EURUSD", price=1.15, atr=0.0005)
    t2 = T0 + timedelta(minutes=3)
    b2.manage({"EURUSD": q("EURUSD", 1.1506, t2)}, {}, t2)
    assert C._dt(b2.s["locks"]["EURUSD"]["until"]) == datetime(2026, 11, 12, 14, 15, tzinfo=UTC)


def test_daily_stop_flattens_and_blocks(cfg):
    e = engine(cfg)
    b, p = _open(e, "CB-BRACKET", sym="EURUSD", price=1.15, atr=0.0005)
    b.s["day"]["realized"] = -1400.0
    t = T0 + timedelta(minutes=1)
    out = b.manage({"EURUSD": q("EURUSD", 1.1498, t)}, {}, t)           # 4 x 6E x -0.0002 = -$100 open
    assert out and out[0]["reason"] == "daily_stop_flatten" and b.s["day"]["flattened"]
    assert b.intake({"symbol": "XAUUSD", "action": "BUY", "confidence": 0.9, "atr": 8.0, "entry": 3900}, t) is None
    assert b.s["stats"]["skips"]["daily_stop"] == 1


def test_trailing_guard(cfg):
    e = engine(cfg)
    b = e.books["CB-BRACKET"]
    b.s["mll_floor"] = b.s["balance"] - 1000.0          # room $1,000: $800 + $4.60 new risk leaves < $300
    assert b.intake({"symbol": "XAUUSD", "action": "BUY", "confidence": 0.9, "atr": 8.0, "entry": 3900}, T0) is None
    assert b.s["stats"]["skips"]["trailing_guard"] == 1


def test_one_per_symbol_and_aggregate(cfg):
    e = engine(cfg)
    b, p = _open(e, "CB-BRACKET")
    assert b.intake({"symbol": "XAUUSD", "action": "BUY", "confidence": 0.9, "atr": 8.0, "entry": 3900}, T0) is None
    b.s["positions"].append({"symbol": "EURUSD", "qty": 5, "id": 50, "side": 1, "contract": "6E", "entry": 1.15,
                             "stop": 1.1495, "atr": 0.0005, "entry_time": T0.isoformat()})
    assert b.intake({"symbol": "US100", "action": "BUY", "confidence": 0.9, "atr": 40.0, "entry": 25000}, T0) is None
    assert b.s["stats"]["skips"]["aggregate_6"] == 1


# ------------------------------------------------------------------ triggers
def test_triggers_level_range_rvol_and_caps(cfg):
    lv = [{"label": "Session High", "price": 3905.0}, {"label": "S/R", "price": 3902.0}]
    snap = copy.deepcopy(SNAP)
    snap["XAUUSD"] = payload("XAUUSD", 3900.0, 8.0, levels=lv)
    snap["XAUUSD"]["context"]["levels"] = lv
    api = Api([sig("XAUUSD", "HOLD", 0.5, 3900)])
    e = engine(cfg, api, snap=Snap(snap))
    run(e.scan(T0))
    assert [l[0] for l in e.e["cache"]["XAUUSD"]["levels"]] == ["Session High"]      # plain S/R is not labelled
    t = T0 + timedelta(minutes=4)
    # not past by 0.1 ATR (0.8) yet
    assert e.trigger_for("XAUUSD", q("XAUUSD", 3905.5, t), {}, None, t) is None
    hit = e.trigger_for("XAUUSD", q("XAUUSD", 3905.9, t), {}, None, t)
    assert hit["trigger"] == "level_cross" and hit["level"] == "Session High"
    assert e.trigger_for("XAUUSD", q("XAUUSD", 3905.9, t), {}, None, t) is None       # side flipped, no repeat
    hit = e.trigger_for("XAUUSD", q("XAUUSD", 3906, t), {"forming_m15_high": 3910, "forming_m15_low": 3897.9}, None, t)
    assert hit["trigger"] == "bar_range"
    idx = pd.date_range("2026-11-12T14:00:00Z", periods=4, freq="min")
    m1 = pd.DataFrame({"open": 1, "high": 1, "low": 1, "close": 1, "volume": [60.0, 60, 50, 40]}, index=idx)
    hit = e.trigger_for("XAUUSD", q("XAUUSD", 3906, t), {}, m1, t)
    assert hit["trigger"] == "rvol" and hit["rvol"] == pytest.approx(2.1)
    # trigger_poll: fires one single-symbol call, then the per-bar cap holds
    feed = Feed(({"XAUUSD": q("XAUUSD", 3895.0, t), "EURUSD": q("EURUSD", 1.15, t), "US100": q("US100", 25000.0, t)},
                 {}, {"XAUUSD": {}, "EURUSD": {}, "US100": {}}))
    e._quotes = feed
    e.e["cache"]["XAUUSD"]["levels"] = [["Session Low", 3896.0, 1]]
    n0 = len(api.calls)
    out = run(e.trigger_poll(t))
    assert len(out) == 1 and len(api.calls) == n0 + 1 and out[0]["sent"] == ["XAUUSD"]
    assert e.e["triggers"]["n"] == 1
    e.e["cache"]["XAUUSD"]["levels"] = [["Session Low", 3896.0, 1]]
    assert run(e.trigger_poll(t + timedelta(minutes=2))) == []                        # 1 per symbol per bar
    e.e["triggers"]["n"] = 40
    e.e["triggers"]["per_bar"] = {}
    assert run(e.trigger_poll(t + timedelta(minutes=20))) == []                       # 40 per day


# ------------------------------------------------------------------ runner model exit
def test_runner_model_exit_only_after_1atr(cfg):
    class ExitApi(Api):
        async def __call__(self, cc, **kw):
            self.calls.append(kw)
            return {"ok": True, "raw_text": json.dumps({"structure": "giveback_limit", "reason": "x", "decision": "CLOSE",
                                                        "confidence": 0.8}),
                    "usage": {"prompt_token_count": 3000, "candidates_token_count": 50, "thoughts_token_count": 100},
                    "latency_s": 4.0}
    api = ExitApi([])
    snap = copy.deepcopy(SNAP)
    snap["XAUUSD"] = payload("XAUUSD", 3910.0, 8.0)
    feed = Feed(({"XAUUSD": q("XAUUSD", 3910.0, T0)}, {}))
    e = engine(cfg, api, feed, snap=Snap(snap))
    b, p = _open(e, "CB-RUNNER")
    p["mfe"] = 5.0                                         # < 1 ATR -> no call
    assert run(e.runner_exit_checks(T0 + timedelta(minutes=15))) == [] and not api.calls
    p["mfe"] = 9.0
    out = run(e.runner_exit_checks(T0 + timedelta(minutes=15)))
    assert out[0]["decision"] == "CLOSE" and out[0]["closed"]["net"] > 0
    assert api.calls[0]["thinking"] == "low" and api.calls[0]["max_output_tokens"] == 800
    assert not b.s["positions"] and e.spend_today(T0)["by_kind"]["exit"]["calls"] == 1
    # bracket book never calls the model
    assert not C.carbon_cfg(cfg)["books"]["CB-BRACKET"]["model_exits"]


# ------------------------------------------------------------------ isolation
def test_isolation_from_other_books(cfg, tmp_path):
    st = tmp_path / "state"
    others = {"lucid/account.json": {"balance": 1}, "bracket/account.json": {"balance": 2},
              "exit_books.json": {"x": 3}, "claude/engine.json": {"y": 4}}
    for rel, obj in others.items():
        (st / rel).parent.mkdir(parents=True, exist_ok=True)
        (st / rel).write_text(json.dumps(obj))
    (tmp_path / "bracket.json").write_text("{}")
    (tmp_path / "lucid.json").write_text("{}")
    before = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    api = Api([sig("XAUUSD", "SELL", 0.9, 3900.0)])
    feed = Feed(({"XAUUSD": q("XAUUSD", 3900.0)}, {}))
    e = engine(cfg, api, feed)
    run(e.scan(T0))
    run(e.tick(T0 + timedelta(minutes=1)))
    after = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    for p, h in before.items():
        assert after[p] == h, f"{p} was modified"
    new = sorted(str(p.relative_to(tmp_path)) for p in after if p not in before)
    assert new and all(n.startswith("state/carbon/") or "/carbon_" in n or n == "carbon.json" for n in new), new


def test_gha_loop_wiring(cfg, tmp_path):
    assert "carbon.json" in gha_loop.SITE_JSON
    lp = gha_loop.GhaLoop(cfg, tmp_path, "t", "holder", dry_run=True, deploy=False)
    lp.start_carbon()
    assert lp.carbon is None and lp.carbon_tasks == []          # never in dry runs


def test_scan_does_not_block_event_loop(cfg):
    """Carbon calls are awaited in their own task: a slow call does not delay other coroutines."""
    class SlowApi(Api):
        async def __call__(self, cc, **kw):
            await asyncio.sleep(0.3)
            return await super().__call__(cc, **kw)
    e = engine(cfg, SlowApi([]))

    async def main():
        task = asyncio.create_task(e.scan(T0))
        t0 = asyncio.get_running_loop().time()
        await asyncio.sleep(0.01)
        lag = asyncio.get_running_loop().time() - t0
        await task
        return lag
    assert run(main()) < 0.1


def test_runner_model_exits_paused_when_flag_off(cfg, tmp_path):
    cfg["exit_manager"]["gemini_exit_calls_enabled"] = False
    class Api:
        def __init__(self):
            self.calls = []
        async def __call__(self, cc, **kw):
            self.calls.append(kw)
            raise AssertionError("Carbon exit API must not be called when gemini_exit_calls_enabled is false")
    api = Api()
    snap = copy.deepcopy(SNAP)
    snap["XAUUSD"] = payload("XAUUSD", 3910.0, 8.0)
    feed = Feed(({"XAUUSD": q("XAUUSD", 3910.0, T0)}, {}))
    e = engine(cfg, api, feed, snap=Snap(snap))
    b, p = _open(e, "CB-RUNNER")
    p["mfe"] = 9.0
    out = run(e.runner_exit_checks(T0 + timedelta(minutes=15)))
    assert out == [] and not api.calls
    assert b.s["positions"]  # still open; mechanical stop/target/time cap still apply via tick
    view = e.site_view(T0)
    assert view["gemini_exit_calls_enabled"] is False
    assert "paused" in (view.get("exit_calls_note") or "").lower() or "paused" in view["rules"].lower()
