"""ds-evt-v1 (DeepSeek event-triggered books DS-IMG-E / DS-TXT-E). Triggers T1-T8 on synthetic and RECORDED
Dukascopy M1 (tests/data/m1_*_2026-11-10_12.csv.gz, replayed through the shared builders), guards, caps, batch
collapse, dry run (no API), and the live path with a mocked call. No network."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import ds_evt as D, event_feed as EF, event_replay as R

UTC = timezone.utc
DATA = Path(__file__).resolve().parent / "data"
T0 = datetime(2026, 11, 12, 14, 0, 10, tzinfo=UTC)      # Thu, NY session, inside 08:00-20:30


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


@pytest.fixture(scope="module")
def m1():
    return {s: R.load_m1(DATA / f"m1_{s}_2026-11-10_12.csv.gz") for s in ("XAUUSD", "EURUSD", "US100")}


def P(live, *, sym="EURUSD", atr=1.0, levels=(), fo=None, fh=None, fl=None, vwap=None, ema50=None, last_close=None,
      up=None, lo=None, cvd=None, rvol=None, gf=None, bar="2026-11-12T14:00:00+00:00"):
    return {"symbol": sym, "context": {
        "live_price": live, "atr": atr, "digits": 2, "levels": [{"label": l, "price": p} for l, p in levels],
        "forming_m15_open": fo, "forming_m15_high": fh, "forming_m15_low": fl, "forming_m15_open_utc": bar,
        "vwap": vwap, "ema50": ema50, "last_close": last_close, "atr_upper": up, "atr_lower": lo, "cvd_slope": cvd,
        "forming_rvol": rvol, "gex": {"gamma_flip": gf} if gf is not None else None, "minutes_into_bar": 3.0,
        "live_price_ts_utc": T0.isoformat()}}


def det(cfg):
    return D.DsTriggerDetector(D.ds_evt_cfg(cfg))


def types(out):
    return [t["type"] for t in out]


# ------------------------------------------------------------------ triggers (synthetic M1 steps)
def test_t1_band_entry_once_per_bar(cfg):
    d = det(cfg)
    lv = [("Session Low", 100.0)]
    assert d.detect("EURUSD", P(102, levels=lv, fo=102, fh=102, fl=101.5), T0) == []
    out = d.detect("EURUSD", P(100.05, levels=lv, fo=102, fh=102, fl=100.05), T0 + timedelta(minutes=1))
    assert types(out) == ["T1"] and out[0]["level_label"] == "Session Low" and out[0]["level_price"] == 100.0
    assert d.detect("EURUSD", P(100.02, levels=lv, fo=102, fh=102, fl=100.02), T0 + timedelta(minutes=2)) == []
    d.detect("EURUSD", P(100.5, levels=lv, fo=102, fh=102, fl=100.02), T0 + timedelta(minutes=3))
    assert "T1" not in types(d.detect("EURUSD", P(100.05, levels=lv, fo=102, fh=102, fl=100.02), T0 + timedelta(minutes=4)))
    # new M15 bar -> can fire again
    out = d.detect("EURUSD", P(100.5, levels=lv, fo=100.5, fh=100.5, fl=100.5, bar="2026-11-12T14:15:00+00:00"),
                   T0 + timedelta(minutes=15))
    out = d.detect("EURUSD", P(100.08, levels=lv, fo=100.5, fh=100.5, fl=100.08, bar="2026-11-12T14:15:00+00:00"),
                   T0 + timedelta(minutes=16))
    assert "T1" in types(out)


def test_t2_closed_through_and_swept_closed_back(cfg):
    lv = [("Session Low", 100.0)]
    d = det(cfg)
    d.detect("EURUSD", P(102, levels=lv, fo=102, fh=102, fl=101.5), T0)
    d.detect("EURUSD", P(100.05, levels=lv, fo=102, fh=102, fl=100.05), T0 + timedelta(minutes=1))
    out = d.detect("EURUSD", P(99.7, levels=lv, fo=102, fh=102, fl=99.7), T0 + timedelta(minutes=2))
    t2 = [t for t in out if t["type"] == "T2"]
    assert t2 and t2[0]["event_resolution"] == "closed_through"
    d = det(cfg)
    d.detect("EURUSD", P(102, levels=lv, fo=102, fh=102, fl=101.5), T0)
    d.detect("EURUSD", P(100.05, levels=lv, fo=102, fh=102, fl=100.05), T0 + timedelta(minutes=1))
    d.detect("EURUSD", P(99.85, levels=lv, fo=102, fh=102, fl=99.85), T0 + timedelta(minutes=2))  # T2 closed_through
    d2 = det(cfg)
    d2.detect("EURUSD", P(102, levels=lv, fo=102, fh=102, fl=101.5), T0)
    d2.detect("EURUSD", P(100.05, levels=lv, fo=102, fh=102, fl=100.05), T0 + timedelta(minutes=1))
    out = d2.detect("EURUSD", P(100.6, levels=lv, fo=102, fh=102, fl=99.8), T0 + timedelta(minutes=2))
    t2 = [t for t in out if t["type"] == "T2"]
    assert t2 and t2[0]["event_resolution"] == "swept_closed_back"


def test_t3_rvol_cross_not_us100(cfg):
    d = det(cfg)
    d.detect("EURUSD", P(100, rvol=1.5), T0)
    assert types(d.detect("EURUSD", P(100, rvol=2.1), T0 + timedelta(minutes=1))) == ["T3"]
    assert d.detect("EURUSD", P(100, rvol=2.5), T0 + timedelta(minutes=2)) == []
    d.detect("US100", P(100, sym="US100", rvol=1.5), T0)
    assert d.detect("US100", P(100, sym="US100", rvol=3.0), T0 + timedelta(minutes=1)) == []


def test_t4_t5_sign_flip_vs_last_close(cfg):
    d = det(cfg)
    assert d.detect("EURUSD", P(99.5, vwap=100, ema50=101, last_close=99), T0) == []
    out = d.detect("EURUSD", P(100.4, vwap=100, ema50=101, last_close=99), T0 + timedelta(minutes=1))
    assert types(out) == ["T4"] and out[0]["level_label"] is None
    out = d.detect("EURUSD", P(101.2, vwap=100, ema50=101, last_close=99), T0 + timedelta(minutes=2))
    assert types(out) == ["T5"]
    assert d.detect("EURUSD", P(99.5, vwap=100, ema50=101, last_close=99), T0 + timedelta(minutes=3)) == []
    assert d.detect("EURUSD", P(100.4, vwap=100, ema50=101, last_close=99), T0 + timedelta(minutes=4)) == []  # once / bar


def test_t6_atr_band_entry(cfg):
    d = det(cfg)
    d.detect("XAUUSD", P(104, sym="XAUUSD", up=105, lo=95), T0)
    out = d.detect("XAUUSD", P(105.2, sym="XAUUSD", up=105, lo=95), T0 + timedelta(minutes=1))
    assert types(out) == ["T6"] and out[0]["level_label"] == "ATR Upper"


def test_t7_cvd_flip_holds_two_closes(cfg):
    d = det(cfg)
    for i, c in enumerate([0.5, 0.4, -0.2]):
        assert d.detect("EURUSD", P(100, cvd=c), T0 + timedelta(minutes=i)) == []
    assert types(d.detect("EURUSD", P(100, cvd=-0.3), T0 + timedelta(minutes=3))) == ["T7"]
    d = det(cfg)
    for i, c in enumerate([0.5, -0.2, 0.3, -0.1, 0.2]):          # flip does not hold -> nothing
        assert d.detect("EURUSD", P(100, cvd=c), T0 + timedelta(minutes=i)) == []


def test_t8_gamma_flip_ny_cash_only(cfg):
    d = det(cfg)
    out = d.detect("US100", P(100.5, sym="US100", gf=100, last_close=99.5), T0 + timedelta(hours=1))  # 10:00 ET
    assert types(out) == ["T8"]
    d = det(cfg)
    early = datetime(2026, 11, 12, 10, 0, 10, tzinfo=UTC)
    assert d.detect("US100", P(100.5, sym="US100", gf=100, last_close=99.5), early) == []
    assert det(cfg).detect("EURUSD", P(100.5, gf=100, last_close=99.5), T0 + timedelta(hours=1)) == []


def test_recorded_m1_replay_deterministic_and_once_per_bar(cfg, m1):
    """Recorded Dukascopy M1 (12 Nov 2026 13:00-15:00 UTC) through the shared builders."""
    runs = []
    for _ in range(2):
        d = det(cfg)
        seen, got = set(), []
        for now in R.minutes(datetime(2026, 11, 12, 13, 0, tzinfo=UTC), datetime(2026, 11, 12, 15, 0, tzinfo=UTC)):
            for s in ("XAUUSD", "EURUSD", "US100"):
                p, _ = R.replay_snapshot(s, m1[s], cfg, now)
                for t in d.detect(s, p, now):
                    key = (s, t["type"], t["bar_open_utc"])
                    assert key not in seen, key          # max once per M15 bar per trigger per symbol
                    seen.add(key)
                    got.append(key)
                    assert t["trigger_price"] is not None and t["bar_open_utc"]
        runs.append(got)
    assert runs[0] == runs[1] and len(runs[0]) > 0
    assert not any(t == "T3" for s, t, _ in runs[0] if s == "US100")


# ------------------------------------------------------------------ engine (guards, caps, batch, dry run)
class Boom:
    def __init__(self):
        self.n = 0

    async def __call__(self, messages):
        self.n += 1
        raise AssertionError("API must not be called in dry run")


def fixed(trigs_by_sym):
    def f(sym, payload, now):
        return [dict(t, bar_open_utc=EF.m15_open(now).isoformat(), trigger_utc=now.isoformat(),
                     trigger_price=payload["context"]["live_price"]) for t in trigs_by_sym.get(sym, [])]
    return f


def snaps_at(cfg, m1, now, syms=("XAUUSD", "EURUSD", "US100")):
    out = {}
    for s in syms:
        out[s], _ = R.replay_snapshot(s, m1[s], cfg, now)
        out[s]["context"]["session"] = "NEW_YORK_SESSION"
        out[s]["context"]["session_blocks_automation"] = False
    return out


def store_of(m1):
    st = EF.M1Store({})
    for s, df in m1.items():
        st.df[s] = df
        st._seeded.add(s)
    return st


def read(tmp_path, name):
    out = []
    for f in sorted((tmp_path / "logs").glob(f"*/{name}")):
        out += [json.loads(x) for x in f.read_text().splitlines()]
    return out


def test_dry_run_no_api_no_fill_batch_collapse(cfg, m1, tmp_path):
    boom = Boom()
    e = D.DsEvtEngine(cfg, call_fn=boom, clock=lambda: T0)
    assert not e.live
    e.detector.detect = fixed({"XAUUSD": [{"type": "T4"}], "EURUSD": [{"type": "T5"}, {"type": "T3"}]})
    out = asyncio.run(e.on_feed(T0, snaps_at(cfg, m1, T0), store_of(m1)))
    assert boom.n == 0
    calls = read(tmp_path, "ds_evt_calls.jsonl")
    assert len(calls) == 2 and {c["book"] for c in calls} == {"DS-IMG-E", "DS-TXT-E"}     # one batch per book
    assert all(c["dry_run"] and sorted(c["symbols"]) == ["EURUSD", "XAUUSD"] for c in calls)
    assert calls[0]["primary"]["EURUSD"] == "T3"          # PRIORITY: T3 before T5
    assert all(not b.s["pending"] and not b.s["positions"] for b in e.books)
    assert len(read(tmp_path, "ds_evt_triggers.jsonl")) == 3
    view = json.loads((tmp_path / "ds_evt.json").read_text())
    assert view["live"] is False and view["cards"][0]["calls_today"] == 1 and len(view["per_trigger"]) == 8


def test_guards_debounce_and_hour_cap(cfg, m1, tmp_path):
    e = D.DsEvtEngine(cfg, call_fn=Boom(), clock=lambda: T0)
    snaps = snaps_at(cfg, m1, T0)
    st = store_of(m1)
    e.detector.detect = fixed({"EURUSD": [{"type": "T4"}]})
    asyncio.run(e.on_feed(T0, snaps, st))
    e.detector.detect = fixed({"EURUSD": [{"type": "T5"}]})
    asyncio.run(e.on_feed(T0 + timedelta(minutes=1), snaps, st))          # < 3 min -> debounced
    e.detector.detect = fixed({"EURUSD": [{"type": "T4"}]})
    asyncio.run(e.on_feed(T0 + timedelta(minutes=16), snaps, st))         # next bar, same ID -> debounced
    sup = read(tmp_path, "ds_evt_suppressed.jsonl")
    assert [s["suppressed_reason"] for s in sup if s["book"] == "DS-IMG-E"] == ["debounced", "debounced"]
    # hour cap: 4 calls / rolling hour per book
    cfg2 = copy.deepcopy(cfg)
    cfg2["ds_evt"].update({"min_call_gap_s": 0, "trigger_gap_bars": -1, "level_gap_bars": -1})
    cfg2["general"]["logs_dir"] = str(tmp_path / "logs2")
    cfg2["general"]["state_dir"] = str(tmp_path / "state2")
    e = D.DsEvtEngine(cfg2, call_fn=Boom(), clock=lambda: T0)
    e.detector.detect = fixed({"EURUSD": [{"type": "T4"}]})
    for i in range(6):
        asyncio.run(e.on_feed(T0 + timedelta(minutes=i), snaps, st))
    assert e.calls_last_hour(e.books[0], T0 + timedelta(minutes=5)) == 4
    sup = [json.loads(x) for f in (tmp_path / "logs2").glob("*/ds_evt_suppressed.jsonl")
           for x in f.read_text().splitlines()]
    assert [s["suppressed_reason"] for s in sup if s["book"] == "DS-IMG-E"] == ["per_hour_cap", "per_hour_cap"]


def test_day_cap_and_session_stale_held_locked(cfg, m1, tmp_path):
    e = D.DsEvtEngine(cfg, call_fn=Boom(), clock=lambda: T0)
    b = e.books[0]
    b.guards()["calls"] = [{"t": (T0 - timedelta(hours=2)).isoformat(), "cost": 3.0, "symbols": ["EURUSD"]}]
    assert e.cap_reason(b, T0) == "per_day_cap"
    ctx = {"session": "ASIAN_SESSION_CHOP"}
    assert e.symbol_reason(b, "EURUSD", ctx, T0) == "session_blocked"
    assert e.symbol_reason(b, "EURUSD", {"session_blocks_automation": True}, T0) == "session_blocked"
    assert e.symbol_reason(b, "EURUSD", {"live_price_stale": True}, T0) == "stale_quote"
    b.s["positions"].append({"symbol": "EURUSD", "side": 1})
    assert e.symbol_reason(b, "EURUSD", {}, T0) == "already_held"
    b.s["positions"] = []
    b.s["locks"]["EURUSD"] = {"until": (T0 + timedelta(minutes=5)).isoformat(), "loss": False, "side": 1}
    assert e.symbol_reason(b, "EURUSD", {}, T0) == "locked_reentry"
    # outside 08:00-20:30 UTC: triggers are not even logged
    late = datetime(2026, 11, 12, 20, 40, 10, tzinfo=UTC)
    e.detector.detect = fixed({"US100": [{"type": "T4"}]})
    out = asyncio.run(e.on_feed(late, snaps_at(cfg, m1, late), store_of(m1)))
    assert out["fired"] == {}


# ------------------------------------------------------------------ live path (mocked call)
def q(sym, price, t):
    return {"symbol": sym, "price": price, "ts": t.isoformat(), "fetched_at": t.isoformat(), "age_s": 1.0,
            "stale": False, "source": "dukascopy:tick"}


_N = [0]


def live_engine(cfg, m1, tmp_path, resp, price_shift=0.0, now=T0):
    cfg = copy.deepcopy(cfg)
    _N[0] += 1
    cfg["general"]["state_dir"] = str(tmp_path / f"state{_N[0]}")
    cfg["ds_evt"]["live"] = True
    snaps = snaps_at(cfg, m1, now)
    prices = {s: snaps[s]["context"]["live_price"] + price_shift for s in snaps}

    async def quotes(syms, t):
        return {s: q(s, prices[s], t) for s in syms}, {}

    calls = []

    async def call(messages):
        calls.append(messages)
        return resp

    async def render(payloads, key):
        return {p["symbol"]: b"\xff\xd8\xff jpeg" for p in payloads}

    e = D.DsEvtEngine(cfg, call_fn=call, render_fn=render, quotes=quotes, clock=lambda: now)
    return e, snaps, calls


def ok_resp(sigs, model="deepseek-V4.1-Flash-1017"):
    return {"ok": True, "model": model, "raw_text": json.dumps({"signals": sigs, "market_summary": "x"}),
            "usage": {"prompt_tokens": 9000, "completion_tokens": 400, "prompt_cache_hit_tokens": 0,
                      "prompt_cache_miss_tokens": 9000}}


def sig(sym, action="BUY", score=6):
    return {"symbol": sym, "level_event": "x", "rationale": "y", "setup_type": "trend", "score": score,
            "action": action, "confidence": 0.6, "stop_loss": None, "take_profit": None}


def test_live_gate_fill_trigger_object_and_prompt(cfg, m1, tmp_path):
    e, snaps, calls = live_engine(cfg, m1, tmp_path, ok_resp([sig("EURUSD")]))
    e.detector.detect = fixed({"EURUSD": [{"type": "T4"}]})
    asyncio.run(e.on_feed(T0, snaps, store_of(m1)))
    assert len(calls) == 2                               # one call per book
    img, txt = calls
    assert "Answer in JSON only" in img[0]["content"] and "M15 chart image" in img[0]["content"]
    assert "CSV of last 100 closed M15 bars" in txt[0]["content"]
    parts = img[1]["content"]
    assert parts[0]["text"].startswith("Chart 1/1: EURUSD (M15)") and '"trigger"' in parts[0]["text"]
    assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["detail"] == "original"
    assert parts[-1]["text"] == "Return the JSON for all symbols now."
    csv = calls[1][1]["content"][1]["text"].splitlines()
    assert csv[0] == "t_utc,o,h,l,c,vol,vwap,ema20,ema50,rvol" and csv[-1].startswith("FORMING ") and len(csv) == 102
    img_b, txt_b = e.books
    # USD-side correlation is per book (13 Nov): both books take the EURUSD long
    for b in e.books:
        assert len(b.s["positions"]) == 1 and b.s["positions"][0]["symbol"] == "EURUSD"
    sg = [s for s in read(tmp_path, "ds_evt_signals.jsonl") if s.get("gate_decision")]
    assert [s["gate_decision"] for s in sg] == ["passed", "passed"]
    assert sg[0]["trigger"]["type"] == "T4" and sg[0]["trigger"]["level_label"] is None
    wi = read(tmp_path, "ds_evt_whatif.jsonl") + e.e["whatif"]
    assert len(wi) == 4 and sorted(w["kind"] for w in wi) == ["coin_flip_twin"] * 2 + ["signal"] * 2
    c = read(tmp_path, "ds_evt_calls.jsonl")[0]
    assert c["cost_usd"] > 0 and c["rate_window"] in ("peak", "off_peak") and c["parse_ok"]


def test_live_model_assert_score_gate_hold_cooldown(cfg, m1, tmp_path):
    e, snaps, calls = live_engine(cfg, m1, tmp_path, ok_resp([sig("EURUSD")], model="deepseek-v4-flash"))
    e.detector.detect = fixed({"EURUSD": [{"type": "T4"}]})
    asyncio.run(e.on_feed(T0, snaps, store_of(m1)))
    assert all(not b.s["positions"] and not b.s["pending"] for b in e.books)
    assert "model_assert" in read(tmp_path, "ds_evt_calls.jsonl")[0]["parse_error"]
    assert read(tmp_path, "ds_evt_calls.jsonl")[0]["model_returned"] == "deepseek-v4-flash"
    e, snaps, _ = live_engine(cfg, m1, tmp_path, ok_resp([sig("EURUSD", score=4)]))
    e.detector.detect = fixed({"EURUSD": [{"type": "T4"}]})
    asyncio.run(e.on_feed(T0, snaps, store_of(m1)))
    assert all(not b.s["positions"] for b in e.books)
    e, snaps, _ = live_engine(cfg, m1, tmp_path, ok_resp([sig("EURUSD", action="HOLD", score=0)]))
    e.detector.detect = fixed({"EURUSD": [{"type": "T4"}]})
    asyncio.run(e.on_feed(T0, snaps, store_of(m1)))
    b = e.books[0]
    assert b.g_sym("EURUSD")["hold_until"]["T4"] == EF.bar_index(T0) + 2
    t3 = T0 + timedelta(minutes=31)
    assert e.trigger_reason(b, "EURUSD", {"type": "T4"}, t3) == "cooldown_after_hold"
    assert e.trigger_reason(b, "EURUSD", {"type": "T4"}, T0 + timedelta(minutes=46)) is None


def test_live_fill_guard_vs_trigger_price(cfg, m1, tmp_path):
    # quote 0.4 ATR worse than the trigger price, but trigger price is 0.25 ATR stale vs live -> 0.5 ATR guard
    e, snaps, _ = live_engine(cfg, m1, tmp_path, ok_resp([sig("EURUSD")]))
    atr = snaps["EURUSD"]["context"]["atr"]
    live = snaps["EURUSD"]["context"]["live_price"]

    def f(sym, payload, now):
        if sym != "EURUSD":
            return []
        return [{"type": "T4", "bar_open_utc": EF.m15_open(now).isoformat(), "trigger_utc": now.isoformat(),
                 "trigger_price": live - 0.35 * atr}]
    e.detector.detect = f

    async def quotes(syms, t):
        return {s: q(s, live + 0.2 * atr, t) for s in syms}, {}
    for b in e.books:
        b._quotes = quotes
    asyncio.run(e.on_feed(T0, snaps, store_of(m1)))
    for b in e.books:
        assert not b.s["positions"]
        assert any("trigger_price" in k for k in b.s["stats"]["skips"])


def test_correlation_locks_daily_stop(cfg, m1, tmp_path):
    e, snaps, _ = live_engine(cfg, m1, tmp_path, ok_resp([sig("XAUUSD", "SELL")]))
    root = Path(e.books[0].state_dir).parent
    (root / "carbon").mkdir(parents=True, exist_ok=True)
    (root / "carbon" / "CB-BRACKET.json").write_text(json.dumps({"positions": [{"symbol": "EURUSD", "side": -1}]}))
    b = e.books[0]
    assert b.risk_block("XAUUSD", -1, 500.0, T0) is None          # other books' positions no longer count
    b.s["positions"].append({"id": 9, "symbol": "EURUSD", "side": -1, "contract": "6E", "qty": 1, "risk_usd": 100.0})
    why = b.risk_block("XAUUSD", -1, 500.0, T0)
    assert why and why.startswith("correlation")                 # own EURUSD short + XAUUSD short = long USD
    assert b.risk_block("XAUUSD", 1, 500.0, T0) is None
    assert e.books[1].risk_block("XAUUSD", -1, 500.0, T0) is None    # the other DS book is independent
    b.s["positions"] = []
    # re-entry lock: until the next M15 close; after a stop-out 30 min + no same side this session
    p = {"id": 1, "symbol": "US100", "contract": "NQ", "qty": 1, "side": 1, "entry": 100.0, "entry_time": T0.isoformat(),
         "stop": 99.0, "target": 101.0, "atr": 1.0, "risk_usd": 20.0}
    b.s["positions"].append(p)
    b.close(p, 99.0, T0, "stop")
    assert b.lock_reason("US100", -1, T0 + timedelta(minutes=20))
    assert b.lock_reason("US100", -1, T0 + timedelta(minutes=31)) is None
    assert b.lock_reason("US100", 1, T0 + timedelta(minutes=31))
    # daily stop: realized + open <= -1500 -> flatten + no entries
    b.s["day"]["realized"] = -1600.0
    assert b.daily_stop_check(T0) == [] and b.s["day"]["flattened"]
    assert b.risk_block("US100", 1, 100.0, T0).startswith("daily_stop")


def test_whatif_resolves_on_recorded_m1(cfg, m1, tmp_path):
    e, snaps, _ = live_engine(cfg, m1, tmp_path, ok_resp([sig("EURUSD")]), now=datetime(2026, 11, 12, 12, 0, 10, tzinfo=UTC))
    now = datetime(2026, 11, 12, 12, 0, 10, tzinfo=UTC)
    e.add_whatif(e.books[0], "EURUSD", 1, snaps["EURUSD"]["context"]["live_price"], snaps["EURUSD"]["context"]["atr"],
                 now, {"type": "T4", "trigger_utc": now.isoformat()})
    e.resolve_whatifs(now + timedelta(hours=3), store_of(m1))
    rows = read(tmp_path, "ds_evt_whatif.jsonl")
    assert len(rows) == 2 and {r["kind"] for r in rows} == {"signal", "coin_flip_twin"}
    assert all(r["R"] is not None and r["exit_reason"] in ("stop", "target", "time") for r in rows)


def test_live_us100_trades_mnq_and_gold_fills(cfg, m1, tmp_path):
    e, snaps, calls = live_engine(cfg, m1, tmp_path, ok_resp([sig("XAUUSD", "SELL", 5), sig("US100", "SELL", 5)]))
    e.detector.detect = fixed({"US100": [{"type": "T1", "level_label": "Session High", "level_key": "Session High@1"}],
                               "XAUUSD": [{"type": "T6"}]})
    asyncio.run(e.on_feed(T0, snaps, store_of(m1)))
    assert len(calls) == 2 and "Chart 2/2" in json.dumps(calls[0][1]["content"])       # one batch, two symbols
    atr = snaps["US100"]["context"]["atr"]
    want = min(10, int(900 // (atr * 2.0)))                          # $900 / (1 ATR x $2), MNQ cap 10
    for b in e.books:
        got = {p["symbol"]: (p["side"], p["contract"], p["qty"]) for p in b.s["positions"]}
        assert got["XAUUSD"][:2] == (-1, "GC") and got["US100"] == (-1, "MNQ", want)
        mnq = next(p for p in b.s["positions"] if p["contract"] == "MNQ")
        assert mnq["risk_usd"] == pytest.approx(abs(mnq["entry"] - mnq["stop"]) * 2.0 * want + 2 * 0.5 * want, abs=0.01)
    assert e.books[0].g_sym("US100")["by_level"]["Session High@1"] == EF.bar_index(T0)


def test_mnq_sizing_skip_and_aggregate(cfg, tmp_path):
    b = D.DsImgBook(cfg)
    assert b.contract_for("US100") == "MNQ" and b.contract_for("XAUUSD") == "GC"
    assert b.size("MNQ", 68.0) == (6, None)                          # floor(900 / 136) = 6
    assert b.size("MNQ", 30.0) == (10, None)                         # floor(900 / 60) = 15 -> cap 10
    q, why = b.size("MNQ", 650.0)                                    # 1 MNQ risks $1,300 > $1,200 -> skip
    assert q is None and "risks $1,300" in why
    b.s["positions"] = [{"symbol": "XAUUSD", "contract": "GC", "qty": 1}, {"symbol": "EURUSD", "contract": "6E", "qty": 4},
                        {"symbol": "X", "contract": "MNQ", "qty": 7}]
    assert b.minis_used() == pytest.approx(5.7)
    assert b.size("MNQ", 30.0) == (3, None)                          # 0.3 mini of room = 3 MNQ
    assert b.size("GC", 5.0)[0] is None                              # no full-size room left


# ------------------------------------------------------------------ time-based go-live gate
def golive_cfg(cfg, tmp_path, tag):
    c = copy.deepcopy(cfg)
    c["general"]["state_dir"] = str(tmp_path / f"st_{tag}")
    c["general"]["logs_dir"] = str(tmp_path / f"lg_{tag}")
    c["ds_evt"].update({"live": False, "live_after_utc": "2026-11-17T08:00:00+00:00", "dry_run_day_utc": "2026-11-16"})
    return c


def fill_dry_day(e, triggers_per_symbol=20, calls=None):
    day = datetime(2026, 11, 16, 8, 0, 10, tzinfo=UTC)
    for i in range(750):
        EF.note_feed_step(e.e, day + timedelta(minutes=i), e.cc["window_utc"])
    for s_ in ("XAUUSD", "EURUSD"):
        for i in range(triggers_per_symbol):
            t = day + timedelta(minutes=30 * i % 740)
            e._jsonl("ds_evt_triggers.jsonl", t, {"ts_utc": t.isoformat(), "symbol": s_, "type": "T4"})
    for b in e.books:
        for i in range(calls or 10):
            t = day + timedelta(minutes=20 * i)
            e._jsonl("ds_evt_calls.jsonl", t, {"ts_utc": t.isoformat(), "book": b.BOOK_NAME, "dry_run": True,
                                               "symbols": ["EURUSD"], "cost_estimate_usd": 0.004,
                                               "symbol_state": {"EURUSD": {"session": "LONDON_SESSION"}}})


def test_golive_gate_time_and_review(cfg, m1, tmp_path):
    c = golive_cfg(cfg, tmp_path, "ok")
    e = D.DsEvtEngine(c, call_fn=Boom(), clock=lambda: T0)
    fill_dry_day(e)
    assert e.gate.update(datetime(2026, 11, 17, 7, 59, tzinfo=UTC)) is False and "golive" not in e.e
    assert e.gate.update(datetime(2026, 11, 17, 8, 0, 10, tzinfo=UTC)) is True
    g = e.e["golive"]
    assert g["live"] and g["reasons"] == [] and g["stats"]["triggers_per_symbol"] == {"XAUUSD": 20, "EURUSD": 20}
    rows = [json.loads(x) for f in (tmp_path / "lg_ok").glob("*/ds_evt_golive.jsonl") for x in f.read_text().splitlines()]
    assert rows[-1]["event"] == "go_live"
    asyncio.run(e.persist(T0))
    e2 = D.DsEvtEngine(c, call_fn=Boom(), clock=lambda: T0)            # decision persists across restarts
    assert e2.live


def test_golive_gate_storm_cap_breach_or_gap_stays_dry(cfg, m1, tmp_path):
    after = datetime(2026, 11, 17, 8, 0, 10, tzinfo=UTC)
    e = D.DsEvtEngine(golive_cfg(cfg, tmp_path, "storm"), call_fn=Boom(), clock=lambda: T0)
    fill_dry_day(e, triggers_per_symbol=150)
    assert e.gate.update(after) is False and any(r.startswith("storm: XAUUSD") for r in e.e["golive"]["reasons"])
    assert e.gate.update(after + timedelta(hours=2)) is False          # decided once; stays in dry run
    e = D.DsEvtEngine(golive_cfg(cfg, tmp_path, "cap"), call_fn=Boom(), clock=lambda: T0)
    fill_dry_day(e)
    t = datetime(2026, 11, 16, 9, 1, 10, tzinfo=UTC)
    for k in range(4):                                                 # 4 more calls inside one hour -> 5+/hour
        tt = t + timedelta(minutes=4 * k)
        e._jsonl("ds_evt_calls.jsonl", tt, {"ts_utc": tt.isoformat(), "book": "DS-IMG-E", "dry_run": True,
                                            "symbols": ["XAUUSD"], "cost_estimate_usd": 0.004})
    assert e.gate.update(after) is False
    assert any(r.startswith("cap_breach: DS-IMG-E") for r in e.e["golive"]["reasons"])
    e = D.DsEvtEngine(golive_cfg(cfg, tmp_path, "gap"), call_fn=Boom(), clock=lambda: T0)
    assert e.gate.update(after) is False and e.e["golive"]["reasons"][0].startswith("dry_run_incomplete")
