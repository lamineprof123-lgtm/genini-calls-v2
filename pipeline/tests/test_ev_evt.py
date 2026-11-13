"""ev-v1 (Claude event stream EV-FABLE / EV-OPUS + EV-CONT / EV-REV / EV-TWIN). Triggers on synthetic M5 bars and on
RECORDED Dukascopy M1 (shared builders), merging, cooldowns, caps, dry run (no API), controls, validation, late
answers, gate / fill / locks, Q3 pairing. No network."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import ev_evt as V, event_feed as EF, event_replay as R

UTC = timezone.utc
DATA = Path(__file__).resolve().parent / "data"
T0 = datetime(2026, 11, 12, 15, 0, 10, tzinfo=UTC)     # 10 s after an M5 close, NY session


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


def m5(bars, vol=None, end=T0):
    """bars: list of (o,h,l,c); 25 flat bars of volume 100 before them."""
    pre = [(100, 100.2, 99.8, 100)] * 25
    rows = pre + list(bars)
    t_last = pd.Timestamp(end).floor("5min") - pd.Timedelta(minutes=5)
    idx = [t_last - pd.Timedelta(minutes=5 * (len(rows) - 1 - i)) for i in range(len(rows))]
    v = [100.0] * len(pre) + (vol or [100.0] * len(bars))
    return pd.DataFrame({"open": [r[0] for r in rows], "high": [r[1] for r in rows], "low": [r[2] for r in rows],
                         "close": [r[3] for r in rows], "volume": v}, index=pd.DatetimeIndex(idx))


def pay(levels=(), vwap=None, atr=1.0, sym="EURUSD"):
    return {"symbol": sym, "context": {"atr": atr, "digits": 2, "vwap": vwap,
                                       "levels": [{"label": l, "price": p} for l, p in levels]}}


def det(cfg):
    return V.EvTriggerDetector(V.ev_cfg(cfg))


def types(ev):
    return [t["type"] for t in ev["triggers"]] if ev else []


def test_lvl_sweep_both_sides(cfg):
    ev = det(cfg).detect("EURUSD", pay([("Session High", 101.0)]), m5([(100.6, 101.1, 100.5, 100.8)]))
    assert types(ev) == ["LVL-SWEEP"] and ev["event_direction"] == "down" and ev["event_level"] == 101.0
    ev = det(cfg).detect("EURUSD", pay([("Session High", 101.0)]), m5([(100.6, 101.04, 100.5, 100.8)]))
    assert ev is None                                     # high only +0.04 ATR above -> no sweep
    ev = det(cfg).detect("EURUSD", pay([("Prev Low", 99.0)]), m5([(99.4, 99.5, 98.9, 99.2)]))
    assert types(ev) == ["LVL-SWEEP"] and ev["event_direction"] == "up"
    # unlabelled S/R lines are not event levels
    assert det(cfg).detect("EURUSD", pay([("S/R", 101.0)]), m5([(100.6, 101.1, 100.5, 100.8)])) is None


def test_lvl_break_needs_prev_close_other_side(cfg):
    ev = det(cfg).detect("EURUSD", pay([("VAH", 100.5)]), m5([(100.3, 100.4, 100.2, 100.3), (100.3, 100.8, 100.3, 100.7)]))
    assert types(ev) == ["LVL-BREAK"] and ev["event_direction"] == "up"
    ev = det(cfg).detect("EURUSD", pay([("VAH", 100.5)]), m5([(100.3, 100.4, 100.2, 100.3), (100.3, 100.7, 100.3, 100.6)]))
    assert ev is None                                     # closed only +0.10 ATR beyond


def test_vol_burst_rvol_spike_not_us100(cfg):
    ev = det(cfg).detect("EURUSD", pay(), m5([(100, 100.95, 99.95, 100.9)]))
    assert types(ev) == ["VOL-BURST"] and ev["event_direction"] == "up"
    assert det(cfg).detect("EURUSD", pay(), m5([(100, 100.95, 99.95, 100.4)])) is None        # body < 60%
    ev = det(cfg).detect("EURUSD", pay(), m5([(100, 100.45, 100, 99.99 + 0.4)], vol=[260.0]))
    assert types(ev) == ["RVOL-SPIKE"] and ev["triggers"][0]["detail"].startswith("volume 2.6x")
    assert det(cfg).detect("US100", pay(sym="US100"), m5([(100, 100.45, 100, 100.4)], vol=[260.0])) is None


def test_vwap_stretch_fires_once_and_rearms(cfg):
    d = det(cfg)
    p = pay(vwap=97.0)
    assert types(d.detect("EURUSD", p, m5([(99.4, 99.6, 99.4, 99.6)]))) == ["VWAP-STRETCH"]
    assert d.detect("EURUSD", p, m5([(99.6, 99.8, 99.6, 99.7)])) is None           # still stretched -> no refire
    assert d.detect("EURUSD", p, m5([(98.5, 98.5, 98.4, 98.4)])) is None           # back within 1.5 ATR -> re-arm
    ev = d.detect("EURUSD", p, m5([(99.4, 99.6, 99.4, 99.6)]))
    assert types(ev) == ["VWAP-STRETCH"] and ev["event_direction"] == "up"


def test_merge_order_primary_and_room(cfg):
    p = pay([("Session High", 101.0), ("24h High", 102.5), ("Session Low", 98.0)])
    ev = det(cfg).detect("EURUSD", p, m5([(100.0, 101.1, 99.9, 100.95)], vol=[300.0]))
    assert types(ev)[0] == "LVL-SWEEP" and "VOL-BURST" in types(ev) and "RVOL-SPIKE" in types(ev)
    assert ev["primary"] == "LVL-SWEEP" and ev["event_direction"] == "down"
    assert ev["room_up_atr"] == pytest.approx(0.05, abs=1e-6) and ev["room_down_atr"] == pytest.approx(2.95, abs=1e-6)
    assert V.room_ok(ev, -1, 1.0) and not V.room_ok(ev, 1, 1.0)
    assert ev["id"] == f"EURUSD|{(pd.Timestamp(T0).floor('5min')).isoformat()}"


def test_validation_play_vs_event_direction():
    assert V.validate({"play": "continue", "action": "SELL", "confidence": 1.4}, "down") == ("SELL", None, 1.0)
    assert V.validate({"play": "reverse", "action": "BUY", "confidence": 0.6}, "down")[0] == "BUY"
    a, why, c = V.validate({"play": "continue", "action": "BUY", "confidence": -2}, "down")
    assert a == "HOLD" and why and c == 0.0
    assert V.validate({"play": "none", "action": "HOLD", "confidence": 0.4}, "up")[0] == "HOLD"


def test_recorded_m1_replay_counts_deterministic(cfg, m1):
    out = []
    for _ in range(2):
        d, got = det(cfg), []
        t = datetime(2026, 11, 12, 13, 0, 10, tzinfo=UTC)
        while t < datetime(2026, 11, 12, 16, 0, tzinfo=UTC):
            for s in ("XAUUSD", "EURUSD", "US100"):
                p, up = R.replay_snapshot(s, m1[s], cfg, t)
                ev = d.detect(s, p, EF.m5_bars(up, t))
                if ev:
                    got.append((ev["id"], tuple(types(ev))))
                    assert ev["primary"] == types(ev)[0]
                    assert types(ev) == sorted(types(ev), key=V.ORDER.index)
            t += timedelta(minutes=5)
        out.append(got)
    assert out[0] == out[1] and len(out[0]) > 0
    assert not any("RVOL-SPIKE" in ty for i, ty in out[0] if i.startswith("US100"))


# ------------------------------------------------------------------ engine
def store_of(m1):
    st = EF.M1Store({})
    for s, df in m1.items():
        st.df[s] = df
    return st


def snaps_at(cfg, m1, now):
    out = {}
    for s in ("XAUUSD", "EURUSD", "US100"):
        out[s], _ = R.replay_snapshot(s, m1[s], cfg, now)
    return out


def read(root, name):
    return [json.loads(x) for f in sorted((Path(root) / "logs").glob(f"*/{name}")) for x in f.read_text().splitlines()]


def mk_event(sym="XAUUSD", now=T0, direction="down", room_up=2.0, room_dn=2.0, c=4000.0, atr=8.0):
    close_t = pd.Timestamp(now).floor("5min").to_pydatetime()
    return {"id": f"{sym}|{close_t.isoformat()}", "symbol": sym, "bar_close_utc": close_t.isoformat(),
            "triggers": [{"type": "LVL-SWEEP", "level_label": "Session High", "level": c + 2, "detail": "x"}],
            "primary": "LVL-SWEEP", "event_direction": direction, "event_level": c + 2,
            "event_level_label": "Session High", "event_bar": {"o": c, "h": c + 3, "l": c - 1, "c": c, "range_atr": 0.5},
            "room_up_atr": room_up, "room_down_atr": room_dn, "atr": atr,
            "_bar_time": (close_t - timedelta(minutes=5)).isoformat(), "_keys": [f"LVL-SWEEP|Session High|{c + 2}"]}


class Boom:
    n = 0

    async def __call__(self, *a, **k):
        Boom.n += 1
        raise AssertionError("no API call in the dry run")


def test_dry_run_logs_triggers_controls_no_api(cfg, m1, tmp_path):
    e = V.EvEngine(cfg, call_fn=Boom(), clock=lambda: T0)
    assert not e.live
    t = datetime(2026, 11, 12, 13, 0, 10, tzinfo=UTC)
    st = store_of(m1)
    while t < datetime(2026, 11, 12, 16, 0, tzinfo=UTC):
        asyncio.run(e.on_feed(t, snaps_at(cfg, m1, t), st))
        t += timedelta(minutes=1)
    assert Boom.n == 0
    tr = read(tmp_path, "event_triggers.jsonl")
    assert tr and all(r["dry_run"] and r["called_by"] == [] for r in tr)
    assert len({r["trigger_id"] for r in tr}) == len(tr)                    # one event per symbol per M5 bar
    calls = read(tmp_path, "event_calls.jsonl")
    assert calls and all(c["dry_run"] for c in calls)
    # symbol cooldown: no two would-be calls for the same symbol + book within 10 min
    by = {}
    for c in calls:
        k = (c["book"], c["trigger_id"].split("|")[0])
        tt = datetime.fromisoformat(c["ts_utc"])
        assert k not in by or tt - by[k] >= timedelta(minutes=10)
        by[k] = tt
    assert all(not b.s["positions"] and not b.s["pending"] for b in e.books.values())
    wi = read(tmp_path, "event_trades.jsonl")
    assert wi and {w["book"] for w in wi} <= set(V.CONTROLS) and all(w["kind"] == "whatif" for w in wi)
    view = json.loads((tmp_path / "ev_evt.json").read_text())
    assert view["live"] is False and len(view["triggers"]) == 5 and len(view["cards"]) == 2


def test_cooldowns_and_caps(cfg, tmp_path):
    e = V.EvEngine(cfg, call_fn=Boom(), clock=lambda: T0)
    ev = mk_event()
    assert e.skip_reason("EV-OPUS", ev, T0) is None
    e._mark_called("EV-OPUS", ev, T0, 0.1)
    assert e.skip_reason("EV-OPUS", ev, T0 + timedelta(minutes=5)) == "symbol_cooldown_10m"
    assert e.skip_reason("EV-OPUS", ev, T0 + timedelta(minutes=15)) == "same_trigger_cooldown_30m"
    assert e.skip_reason("EV-OPUS", ev, T0 + timedelta(minutes=31)) is None
    assert e.skip_reason("EV-FABLE", ev, T0 + timedelta(minutes=5)) is None   # cooldowns are per AI book
    g = e.books["EV-FABLE"].guards()
    g["calls"] = [{"t": (T0 - timedelta(minutes=i)).isoformat(), "cost": 0.0, "symbol": "EURUSD"} for i in range(50)]
    assert e.skip_reason("EV-FABLE", ev, T0) == "daily_call_cap"
    g["calls"] = [{"t": T0.isoformat(), "cost": 20.0, "symbol": "EURUSD"}]
    assert e.skip_reason("EV-FABLE", ev, T0) == "daily_usd_cap"
    e.books["EV-OPUS"].s["positions"].append({"symbol": "XAUUSD", "side": 1})
    assert e.skip_reason("EV-OPUS", ev, T0 + timedelta(hours=1)) == "position_held"


def test_controls_room_filter_and_twin_seed(cfg, tmp_path):
    e = V.EvEngine(cfg, call_fn=Boom(), clock=lambda: T0)
    e.add_controls(mk_event(room_up=0.4, room_dn=1.7))           # sweep down: CONT=SELL ok, REV=BUY room 0.4 -> skip
    books = {w["book"]: w["side"] for w in e.e["whatif"]}
    assert books.get("EV-CONT") == -1 and "EV-REV" not in books
    e2 = V.EvEngine(copy.deepcopy(cfg) | {}, call_fn=Boom(), clock=lambda: T0)
    a = [w for w in e.e["whatif"] if w["book"] == "EV-TWIN"]
    e2.e["whatif"] = []
    e2.add_controls(mk_event(room_up=0.4, room_dn=1.7))
    b = [w for w in e2.e["whatif"] if w["book"] == "EV-TWIN"]
    assert [w["side"] for w in a] == [w["side"] for w in b]       # seeded by trigger id


# ------------------------------------------------------------------ live path (mocked calls)
def q(sym, price, t):
    return {"symbol": sym, "price": price, "ts": t.isoformat(), "fetched_at": t.isoformat(), "age_s": 1.0,
            "stale": False, "source": "dukascopy:tick"}


def live(cfg, tmp_path, answers, *, quote=4000.0, delay_s=20.0, tag="a"):
    c = copy.deepcopy(cfg)
    c["ev_evt"]["live"] = True
    c["general"]["state_dir"] = str(tmp_path / f"state_{tag}")
    clock = {"t": T0}
    seen = []

    async def call(bc, content, timeout):
        seen.append((bc["model"], bc["effort"], timeout, content))
        clock["t"] = T0 + timedelta(seconds=delay_s)
        a = answers[bc["model"]]
        return {"ok": True, "model": bc["model"], "parsed": a, "usage": {"input_tokens": 4500, "output_tokens": 900},
                "thinking_tokens": 600, "cost_usd": 0.05, "latency_ms": int(delay_s * 1000), "stop_reason": "end_turn"}

    async def quotes(syms, t):
        return {s: q(s, quote, t) for s in syms}, {}

    async def render(payloads, key):
        return {p["symbol"]: b"\xff\xd8\xff jpeg" for p in payloads}
    e = V.EvEngine(c, call_fn=call, render_fn=render, quotes=quotes, clock=lambda: clock["t"])
    return e, seen


def ans(play, action, conf=0.6):
    return {"read": "r", "checks": "c", "play": play, "action": action, "confidence": conf, "invalidation": None}


def payload_for(cfg, m1, sym="XAUUSD"):
    p, up = R.replay_snapshot(sym, m1[sym], cfg, T0)
    return p, up


def test_live_both_books_parallel_gate_fill_lock(cfg, m1, tmp_path):
    e, seen = live(cfg, tmp_path, {"claude-fable-5-2": ans("continue", "SELL"), "claude-opus-5-5": ans("reverse", "BUY")})
    p, up = payload_for(cfg, m1)
    ev = mk_event()
    out = asyncio.run(e.handle_event(ev, p, up, T0, store_of(m1)))
    assert sorted(out["called"]) == ["EV-FABLE", "EV-OPUS"]
    assert {(m, ef, to) for m, ef, to, _ in seen} == {("claude-fable-5-2", "xhigh", 90.0), ("claude-opus-5-5", "high", 60.0)}
    content = seen[0][3]
    assert content[0]["text"].startswith("Event on XAUUSD at ") and content[-1]["text"] == "Return the JSON now."
    assert any(x.get("type") == "image" for x in content) and '"event_direction": "down"' in content[1]["text"]
    fab, opus = e.books["EV-FABLE"], e.books["EV-OPUS"]
    assert [(x["symbol"], x["side"], x["contract"]) for x in fab.s["positions"]] == [("XAUUSD", -1, "GC")]
    assert [(x["symbol"], x["side"]) for x in opus.s["positions"]] == [("XAUUSD", 1)]   # own book, own USD side
    pos = fab.s["positions"][0]
    assert pos["fill_delay_s"] == pytest.approx(20.0 + 10.0, abs=0.01) and pos["slippage_atr"] is not None
    sg = [s for s in read(tmp_path, "event_signals.jsonl") if s.get("gate_decision")]
    assert {s["gate_decision"] for s in sg} == {"passed"} and all(s["validation"] == "ok" for s in sg)
    calls = read(tmp_path, "event_calls.jsonl")
    assert {c["book"] for c in calls} == {"EV-FABLE", "EV-OPUS"} and all(c["thinking_tokens"] == 600 for c in calls)
    # lock: 15 min after any exit
    fab.close(pos, 4001.0, T0 + timedelta(minutes=30), "target")
    assert fab.lock_reason("XAUUSD", -1, T0 + timedelta(minutes=40))
    assert fab.lock_reason("XAUUSD", -1, T0 + timedelta(minutes=46)) is None
    assert any(r.get("kind") == "real" for r in read(tmp_path, "event_trades.jsonl"))


def test_live_late_mismatch_room_and_fill_guard(cfg, m1, tmp_path):
    p, up = payload_for(cfg, m1)
    e, _ = live(cfg, tmp_path, {"claude-fable-5-2": ans("continue", "SELL"), "claude-opus-5-5": ans("continue", "BUY")},
                delay_s=95.0, tag="late")
    asyncio.run(e.handle_event(mk_event(), p, up, T0, store_of(m1)))
    sg = {s["book"]: s for s in read(tmp_path, "event_signals.jsonl") if s.get("gate_decision")}
    assert sg["EV-FABLE"]["late"] and sg["EV-FABLE"]["skip_reason"].startswith("late")
    assert sg["EV-OPUS"]["validated_action"] == "HOLD" and "does not match" in sg["EV-OPUS"]["validation"]
    assert not e.books["EV-FABLE"].s["positions"] and any(w["book"] == "EV-FABLE" for w in e.e["whatif"])
    # room filter (server) and the 0.3 ATR fill guard vs the event bar close
    e, _ = live(cfg, tmp_path, {"claude-fable-5-2": ans("reverse", "BUY"), "claude-opus-5-5": ans("continue", "SELL")},
                quote=4000.0 - 0.4 * 8.0, tag="guard")
    asyncio.run(e.handle_event(mk_event(room_up=0.5), p, up, T0, store_of(m1)))
    assert not e.books["EV-FABLE"].s["positions"]
    assert not e.books["EV-OPUS"].s["positions"]                       # SELL filled 0.4 ATR worse -> skipped
    assert any("chase" in k or "worse" in k for k in e.books["EV-OPUS"].s["stats"]["skips"])


def test_q3_pairing_vs_c_opus(cfg, m1, tmp_path):
    e = V.EvEngine(cfg, call_fn=Boom(), clock=lambda: T0)
    day = T0.astimezone(V.DUBAI).strftime("%Y-%m-%d")
    d = tmp_path / "logs" / day
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"t": (T0 + timedelta(minutes=12)).isoformat(), "sim": "C-OPUS", "symbol": "EURUSD", "action": "BUY",
             "live_price": float(m1["EURUSD"].loc[:T0 + timedelta(minutes=12)]["close"].iloc[-1])},
            {"t": (T0 + timedelta(minutes=50)).isoformat(), "sim": "C-OPUS", "symbol": "XAUUSD", "action": "SELL"}]
    (d / "claude_signals.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    w = {"book": "EV-OPUS", "trigger_id": f"EURUSD|{T0.isoformat()}", "symbol": "EURUSD", "side": 1,
         "entry": float(m1["EURUSD"].loc[:T0]["close"].iloc[-1]), "t0": T0.isoformat(), "atr": 0.0007}
    e.pair_q3(w, 1.0, store_of(m1), T0 + timedelta(hours=4))
    pr = e.e["pairs"][-1]
    assert pr["paired"] and pr["R_timed"] is not None and pr["R_diff"] == round(1.0 - pr["R_timed"], 3)
    assert e.missed_by_triggers(T0 + timedelta(hours=2), hours=3) == 2          # no events logged at all
