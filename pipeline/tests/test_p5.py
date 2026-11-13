"""P5 (13 Nov 2026): Gemini's P5 spec as three NEW books (G-P5 own call, G-P5-EVAL on G-P5's response, G-P5-EVENT
trigger-only calls); every existing book untouched. No network."""
import asyncio, copy, json, sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from signals.config import load_config
from signals import gha_loop, p5
from signals.glow80 import GLow80Book, glow80_cfg, gemini_low_cfg
from signals.eval80 import eval80_cfg
from signals.evalr4 import evalr4_cfg
from signals.gp5 import GP5Book, GP5EvalBook, GP5EventBook, gp5_cfg, gp5eval_cfg, gp5event_cfg
from signals.gemini_client import system_prompt, chart_user_text
from signals.m15_scan import M15Snapshot
from signals.panel import PanelLedger, BOOKS, write_reset_marker
from test_glow80 import T0, Feed, gem, s

UTC = timezone.utc


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


def payload(sym, price, atr, *, last=None, levels=(), rvol=1.0, n=120, digits=2, vah=None, val=None, poc=None):
    """n flat candles of range ~atr around `price`; `last` overrides the final (signal) candle OHLC."""
    t0 = int((T0 - timedelta(seconds=20)).timestamp()) - n * 900
    cs = []
    for i in range(n):
        o = price + (atr * 0.1 if i % 2 else -atr * 0.1)
        c = price - (atr * 0.1 if i % 2 else -atr * 0.1)
        cs.append({"time": t0 + i * 900, "open": o, "high": max(o, c) + atr * 0.4, "low": min(o, c) - atr * 0.4,
                   "close": c})
    if last:
        cs[-1].update(last)
    sc = {k: cs[-1][k] for k in ("open", "high", "low", "close")}
    ctx = {"last_close": sc["close"], "atr": atr, "digits": digits, "live_price": sc["close"], "live_price_stale": False,
           "last_closed_bar_utc": "2026-11-13T13:45:00+00:00", "session": "NEW_YORK_SESSION", "signal_candle": sc,
           "levels": [{"type": t, "label": lab, "price": p} for t, lab, p in levels], "ema20_baseline": price,
           "vwap": price, "rvol_last": rvol, "poc": poc if poc is not None else price + 3 * atr,
           "vah": vah if vah is not None else price + 4 * atr, "val": val if val is not None else price - 4 * atr}
    return {"symbol": sym, "source": "dukascopy", "ticker": sym.lower(), "timeframe": "M15", "candles": cs,
            "context": ctx}


def snap_of(payloads):
    return M15Snapshot(snapshot_id="m15-p5", snapshot_hash="h" * 64, package_id="pkg", package_hash="p",
                       bar_close_ts=T0 - timedelta(seconds=20), snapshot_ts=T0, now=T0, payloads=payloads,
                       charts={p["symbol"]: b"\xff\xd8\xff fake" for p in payloads}, facts={}, htf={},
                       chart_sha256={}, package={}, render_ms=1, start_lag_s=20)


async def no_bars(sym, n, since=None):
    raise RuntimeError("no data")


PRICES = {"XAUUSD": 3945.0, "EURUSD": 1.1, "US100": 21000.0, "XAGUSD": 50.0}


def test_live_config_new_p5_books_and_existing_untouched(cfg):
    # existing books unchanged (no P5, same thresholds / brackets / reset tokens)
    g = glow80_cfg(cfg)
    assert not g["p5"] and not g.get("p5_gates") and g["min_confidence"] == 0.70
    assert g["stop_atr"] == 1.0 and g["target_atr"] == 1.0 and g["reset_token"] == "glow80-v1"
    assert "GBPUSD" in g["symbols"] and g["other_venue_symbols"] == ["BTCUSD"]
    e = eval80_cfg(cfg)
    assert e["min_confidence"] == 0.80 and e["reset_token"] == "eval80-v1" and not e.get("p5_gates")
    assert not e.get("dead_cut_minutes") and e["source_book"] == "G-LOW-80"
    r4 = evalr4_cfg(cfg)
    assert r4["min_confidence"] == 0.80 and r4["reset_token"] == "evalr4-v1" and r4["h1_trend_min_atr"] == 2.7
    assert "prompt_template" not in gemini_low_cfg(cfg, g)["gemini"]
    # MAIN: P4 prompt, high thinking, M15 cadence
    assert cfg["gemini"]["entry_thinking_level"] == "high" and cfg["gemini"]["main_entry_cadence"] == "m15"
    assert "prompt_template" not in cfg["gemini"]
    assert "~15-25 ticks on indices" in system_prompt(cfg, ["XAUUSD"])
    # new books
    for b in (gp5_cfg(cfg), gp5event_cfg(cfg)):
        assert b["enabled"] and b["p5"] and b["p5_gates"] and b["send_held"] and b["thinking_level"] == "low"
        assert b["min_confidence"] == 0.72 and b["stop_atr"] == 0.8 and b["target_atr"] == 1.0
        assert b["dead_cut_minutes"] == 30 and b["max_hold_minutes"] == 90
        assert b["symbols"] == b["scan_symbols"] == p5.P5_SYMBOLS and b["other_venue_symbols"] == []
        assert b["reset_token"] == "p5-2026-11-13" and b["start_balance"] == 100000.0
    ev = gp5eval_cfg(cfg)
    assert ev["enabled"] and ev["min_confidence"] == 0.75 and ev["p5_gates"] and ev["dead_cut_minutes"] == 30
    assert ev["source_book"] == "G-P5" and ev["dll"] == 1800 and ev["mll"] == 3000 and ev["stop_atr"] == 0.8
    assert ev["symbols"] == p5.P5_SYMBOLS
    gp = gp5_cfg(cfg)
    p5txt = system_prompt(gemini_low_cfg(cfg, gp), ["XAUUSD", "EURUSD"])
    assert "Stop Loss = 0.8 x ATR14, Take Profit = 1.0 x ATR14" in p5txt and "room_to_nearest_wall_atr" in p5txt
    assert "XAUUSD, EURUSD" in p5txt
    assert "prompt_template" not in cfg["gemini"]          # copy only
    for f in ("gp5.json", "gp5eval.json", "gp5event.json"):
        assert f in gha_loop.SITE_JSON
    lab = {b: l for b, _, l in BOOKS}
    assert {"G-P5", "G-P5-EVAL", "G-P5-EVENT", "MAIN", "G-LOW-80", "EVAL-80", "EVAL-R4"} <= set(lab)
    assert lab["G-LOW-80"] == "Gemini flash · G-LOW-80 (own call, thinking low, min 0.70)"


def test_fields_values_and_room(cfg):
    pl = payload("XAUUSD", 3945.0, 5.0, levels=[("resistance", "Session High", 3950.0),
                                                ("support", "S/R", 3937.0)])
    f = p5.compute_fields(pl)
    assert set(p5.FIELD_KEYS) <= set(f)
    assert 0.8 < f["atr_ratio_vs_50bar"] < 1.3
    assert f["room_to_nearest_wall_atr"]["buy"] == round((3950.0 - pl["context"]["live_price"]) / 5.0, 2)
    assert f["room_to_nearest_wall_atr"]["sell"] == round((pl["context"]["live_price"] - 3937.0) / 5.0, 2)
    assert f["_p5"]["wall_buy"] == "Session High"
    txt = chart_user_text(p5.with_fields([pl], {"XAUUSD": f})[0], 1, 1)
    for k in p5.FIELD_KEYS:
        assert k in txt
    assert "_p5" not in txt and "atr_ratio_vs_50bar" not in pl["context"]      # original payload untouched


def test_gates(cfg):
    base = payload("XAUUSD", 3945.0, 5.0)
    f = p5.compute_fields(base)
    assert p5.validate_entry_gates("BUY", base["context"], f)["pass"]
    # stretched 2.5 ATR above EMA20 -> BUY rejected, SELL fine on that check
    st = payload("XAUUSD", 3945.0, 5.0, last={"close": 3957.5, "high": 3958.0, "low": 3956.0, "open": 3956.5})
    fs = p5.compute_fields(st)
    r = p5.validate_entry_gates("BUY", st["context"], fs)
    assert not r["pass"] and not r["checks"]["stretch_ema20"]["pass"] and "stretched_vs_ema20" in r["reason"]
    assert p5.validate_entry_gates("SELL", st["context"], fs)["checks"]["stretch_ema20"]["pass"]
    # wall 0.4 ATR away -> rejected unless atr_ratio >= 1.3 and rvol >= 2
    w = payload("XAUUSD", 3945.0, 5.0, levels=[("resistance", "S/R", 3947.0)])
    fw = p5.compute_fields(w)
    r = p5.validate_entry_gates("BUY", w["context"], fw)
    assert not r["checks"]["room_to_wall"]["pass"] and "wall_too_close" in r["reason"]
    fw2 = {**fw, "atr_ratio_vs_50bar": 1.4}
    w["context"]["rvol_last"] = 2.5
    assert p5.validate_entry_gates("BUY", w["context"], fw2)["checks"]["room_to_wall"]["pass"]
    # exhaustion: 2 ATR bullish candle, BUY at the high -> rejected; 30% pullback ok; SELL (reversal) exempt
    ex = payload("XAUUSD", 3945.0, 5.0, last={"open": 3944.0, "low": 3943.5, "high": 3953.5, "close": 3953.0})
    fe = p5.compute_fields(ex)
    r = p5.validate_entry_gates("BUY", ex["context"], fe)
    assert not r["checks"]["exhaustion"]["pass"]
    ex["context"]["live_price"] = 3950.4       # 31% of the 10.0 range below the high
    assert p5.validate_entry_gates("BUY", ex["context"], p5.compute_fields(ex))["checks"]["exhaustion"]["pass"]
    assert p5.validate_entry_gates("SELL", ex["context"], fe)["checks"]["exhaustion"]["pass"]


def test_event_trigger(cfg):
    quiet = payload("EURUSD", 1.1, 0.0012, digits=5)
    t = p5.event_trigger(quiet, p5.compute_fields(quiet))
    assert not t["fired"] and set(t["conds"]) == {"level", "rvol", "atr_ratio"}
    lv = payload("EURUSD", 1.1, 0.0012, digits=5, levels=[("resistance", "Session High", 1.1009)])
    t = p5.event_trigger(lv, p5.compute_fields(lv))      # bar high ~1.10072, level 0.15 ATR above
    assert t["fired"] and t["by"] == ["level"] and t["conds"]["level"]["nearest"]["level"] == "Session High"
    poc = payload("EURUSD", 1.1, 0.0012, digits=5, poc=1.1)
    assert p5.event_trigger(poc, p5.compute_fields(poc))["by"] == ["level"]
    rv = payload("EURUSD", 1.1, 0.0012, digits=5, rvol=2.3)
    assert p5.event_trigger(rv, p5.compute_fields(rv))["by"] == ["rvol"]
    far = payload("EURUSD", 1.1, 0.0012, digits=5, levels=[("resistance", "S/R", 1.1009)])
    assert not p5.event_trigger(far, p5.compute_fields(far))["fired"]      # S/R is not a key level here
    ar = payload("EURUSD", 1.1, 0.0012, digits=5)
    assert p5.event_trigger(ar, {**p5.compute_fields(ar), "atr_ratio_vs_50bar": 1.35})["by"] == ["atr_ratio"]


def test_gp5event_trigger_t2_no_poc_atr_ratio_150(cfg, tmp_path):
    """13 Nov (user-approved): G-P5-EVENT drops POC and needs ATR ratio >= 1.50; defaults (other books) unchanged."""
    tc = gp5event_cfg(cfg)["trigger"]
    assert tc["atr_ratio_min"] == 1.5 and tc["exclude_levels"] == ["POC"] and tc["level_atr"] == 0.25
    poc = payload("EURUSD", 1.1, 0.0012, digits=5, poc=1.1)
    assert not p5.event_trigger(poc, p5.compute_fields(poc), tc)["fired"]
    vah = payload("EURUSD", 1.1, 0.0012, digits=5, vah=1.1)
    assert p5.event_trigger(vah, p5.compute_fields(vah), tc)["by"] == ["level"]
    ar = payload("EURUSD", 1.1, 0.0012, digits=5)
    f = p5.compute_fields(ar)
    assert not p5.event_trigger(ar, {**f, "atr_ratio_vs_50bar": 1.45}, tc)["fired"]
    assert p5.event_trigger(ar, {**f, "atr_ratio_vs_50bar": 1.55}, tc)["by"] == ["atr_ratio"]
    assert "trigger" not in gp5_cfg(cfg)                                  # G-P5 itself unchanged
    ev = GP5EventBook(cfg)
    GP5EventBook(cfg)                                                     # logged once, not on every start
    rows = [json.loads(x) for f_ in (tmp_path / "logs").glob("*/gp5event_events.jsonl") for x in f_.read_text().splitlines()]
    ch = [r for r in rows if r.get("event") == "trigger_change"]
    assert len(ch) == 1 and ch[0]["new"]["atr_ratio_min"] == 1.5 and ch[0]["previous"]["atr_ratio_min"] == 1.3
    assert ev.s["balance"] == ev.s["start_balance"] and "ATR ratio vs 50 bars >= 1.50" in ev.site_view()["rules"]


def make_books(cfg, call, ecall):
    kw = dict(quotes=Feed(PRICES))
    gp = GP5Book(cfg, call=call, bars_fetch=no_bars, **kw)
    ge = GP5EvalBook(cfg, **kw)
    gv = GP5EventBook(cfg, call=ecall, bars_fetch=no_bars, shared_from=gp, **kw)
    for b in (gp, ge, gv):
        b.s["created_at"] = (T0 - timedelta(hours=1)).isoformat()
    return gp, ge, gv


def four():
    return [payload("XAUUSD", 3945.0, 5.0, levels=[("resistance", "Session High", 3946.0)]),   # wall 0.2 ATR
            payload("EURUSD", 1.1, 0.004, digits=5),         # nothing happened
            payload("US100", 21000.0, 40.0, rvol=2.4),       # RVOL trigger
            payload("XAGUSD", 50.0, 0.25, digits=3)]


SIGS = [s("XAUUSD", "BUY", 0.80, 3945.0), s("EURUSD", "SELL", 0.73, 1.1),
        s("US100", "BUY", 0.76, 21000.0), s("XAGUSD", "HOLD", 0.3, 50.0)]


def test_gp5_call_eval_shares_event_own_trigger_call(cfg, tmp_path):
    sn = snap_of(four())
    call, ecall = gem(SIGS), gem([s("US100", "BUY", 0.76, 21000.0), s("XAUUSD", "BUY", 0.80, 3945.0)])
    gp, ge, gv = make_books(cfg, call, ecall)
    r = asyncio.run(gp.on_snapshot(sn, now=T0))
    assert r["ok"] and r["prompt_version"] == "p5"
    c0 = call.calls[0]
    assert c0["cfg"]["gemini"]["prompt_template"] == p5.PROMPT_TEMPLATE
    assert c0["cfg"]["gemini"]["entry_thinking_level"] == "low"
    assert c0["symbols"] == p5.P5_SYMBOLS
    v = {x["symbol"]: x for x in r["validated"]}
    assert not v["XAUUSD"]["p5_gate"]["pass"] and "wall_too_close" in v["XAUUSD"]["p5_gate"]["reason"]
    assert v["EURUSD"]["p5_gate"]["pass"] and v["US100"]["p5_gate"]["pass"]
    assert sorted(r["queued"]) == ["EURUSD", "US100"]                 # 0.73 / 0.76 >= 0.72, XAUUSD gated
    pos = {p["symbol"]: p for p in gp.s["positions"]}
    assert abs(abs(pos["US100"]["entry"] - pos["US100"]["stop"]) - 32.0) < 0.6     # 0.8 ATR stop
    re_ = asyncio.run(ge.on_shared(r, sn, now=T0))
    assert re_["queued"] == ["US100"]                                 # 0.73 < 0.75; XAUUSD gated
    assert len(call.calls) == 1                                       # EVAL: no own call
    rv = asyncio.run(gv.on_snapshot(sn, now=T0))
    assert sorted(rv["fired"]) == ["US100", "XAUUSD"] and not rv["shared_call"]
    assert ecall.calls[0]["symbols"] == ["XAUUSD", "US100"]           # only triggered symbols sent
    assert ecall.calls[0]["cfg"]["gemini"]["prompt_template"] == p5.PROMPT_TEMPLATE
    assert rv["queued"] == ["US100"]
    st = gv.s["triggers"]
    assert st["closes"] == 1 and st["evaluated"] == 4 and st["fired"] == 2 and st["own_calls"] == 1
    assert st["by"]["rvol"] == 1 and st["by"]["level"] >= 1 and st["symbols_sent"] == 2
    day = Path(cfg["general"]["logs_dir"]) / "2026-11-13"
    tl = [json.loads(x) for x in (day / "gp5event_triggers.jsonl").read_text().splitlines()]
    assert len(tl) == 1 and set(tl[0]["triggers"]) == set(p5.P5_SYMBOLS) and tl[0]["gemini_called"]
    assert tl[0]["triggers"]["EURUSD"]["conds"]["rvol"]["pass"] is False
    gl = json.loads((day / "gp5_signals.jsonl").read_text().splitlines()[0])
    assert gl["prompt_version"] == "p5" and gl["p5_triggers"]
    # next close: G-P5 holds EURUSD / US100 but still sends every P5 symbol
    asyncio.run(gp.on_snapshot(sn, now=T0 + timedelta(minutes=15)))
    assert call.calls[1]["symbols"] == p5.P5_SYMBOLS


def test_event_reuses_identical_request_and_skips_quiet_close(cfg, tmp_path):
    hot = [payload(sy, pr, at, digits=d, rvol=2.5) for sy, pr, at, d in
           (("XAUUSD", 3945.0, 5.0, 2), ("EURUSD", 1.1, 0.004, 5), ("US100", 21000.0, 40.0, 2), ("XAGUSD", 50.0, 0.25, 3))]
    sn = snap_of(hot)
    call, ecall = gem(SIGS), gem([])
    gp, ge, gv = make_books(cfg, call, ecall)
    asyncio.run(gp.on_snapshot(sn, now=T0))
    rv = asyncio.run(gv.on_snapshot(sn, now=T0))
    assert rv["shared_call"] is True and ecall.calls == [] and rv.get("cost_usd") == 0.0
    assert gv.s["triggers"]["shared_calls"] == 1
    # quiet close: nothing fired -> no Gemini call at all
    quiet = [payload(sy, pr, at, digits=d) for sy, pr, at, d in
             (("EURUSD", 1.1, 0.004, 5), ("XAGUSD", 50.0, 0.25, 3))]
    sq = snap_of(quiet)
    sq.snapshot_id = "m15-quiet"
    rq = asyncio.run(gv.on_snapshot(sq, now=T0 + timedelta(minutes=15)))
    assert ecall.calls == [] and not rq.get("symbols") and rq["fired"] == []
    assert gv.s["triggers"]["no_call_closes"] == 1


def test_dead_trade_cut(cfg, tmp_path):
    bk = GP5Book(cfg, quotes=Feed(PRICES), bars_fetch=no_bars)
    p = {"symbol": "XAUUSD", "side": 1, "entry": 3945.0, "stop": 3941.0, "target": 3950.0, "atr": 5.0,
         "entry_time": T0.isoformat(), "deadline": (T0 + timedelta(minutes=90)).isoformat(), "mfe": 0.5,
         "mae": 0.0, "contract": "GC", "qty": 1}
    assert bk.check_price(dict(p), 3943.5, T0 + timedelta(minutes=29)) is None       # < 30 min
    assert bk.check_price(dict(p), 3944.0, T0 + timedelta(minutes=31)) is None       # only -0.2 ATR
    r = bk.check_price(dict(p), 3943.5, T0 + timedelta(minutes=31))
    assert r and r[0] == "dead_30min"
    assert bk.check_price({**p, "mfe": 1.5}, 3943.5, T0 + timedelta(minutes=31)) is None   # moved 0.3 ATR first
    assert bk.check_price(dict(p), 3940.9, T0 + timedelta(minutes=5))[0] == "stop_0.8atr"


def test_new_books_do_not_touch_existing_state(cfg, tmp_path):
    st = tmp_path / "state"
    for d, tok, bal in (("glow80", "glow80-v1", 99585.3), ("eval80", "eval80-v1", 101234.0)):
        (st / d).mkdir(parents=True)
        (st / d / "account.json").write_text(json.dumps({"reset_token": tok, "balance": bal, "positions": [],
                                                          "pending": []}))
    before = {d: (st / d / "account.json").read_text() for d in ("glow80", "eval80")}
    gp, ge, gv = make_books(cfg, gem([]), gem([]))
    for b in (gp, ge, gv):
        asyncio.run(b.persist())
        assert b.s["balance"] == 100000.0 and b.s["reset_token"] == "p5-2026-11-13"
    assert {d: (st / d / "account.json").read_text() for d in ("glow80", "eval80")} == before
    assert sorted(x.name for x in st.iterdir()) == ["eval80", "glow80", "gp5", "gp5eval", "gp5event"]
    assert GLow80Book(cfg, bars_fetch=no_bars).s["balance"] == 99585.3


def test_panel_new_books(cfg, tmp_path):
    cfg["reset"]["token"] = "tok-1"
    stt = tmp_path / "state"
    write_reset_marker(cfg, stt, T0 - timedelta(days=1))
    call = gem([s("US100", "BUY", 0.76, 21000.0)])
    gp, ge, gv = make_books(cfg, call, gem([s("US100", "BUY", 0.76, 21000.0)]))
    sn = snap_of([payload("US100", 21000.0, 40.0, rvol=2.4)])
    r = asyncio.run(gp.on_snapshot(sn, now=T0))
    asyncio.run(ge.on_shared(r, sn, now=T0))
    asyncio.run(gv.on_snapshot(sn, now=T0))

    async def qf(syms):
        return {"US100": {"price": 21001.0}}
    led = PanelLedger(cfg, state_dir=stt, logs_dir=tmp_path / "logs", site_dir=tmp_path, quote_fn=qf,
                      history_fn=False)
    asyncio.run(led.tick(T0 + timedelta(minutes=1)))
    site = json.loads((tmp_path / "panel.json").read_text())
    perf = {(p["book"], p["venue"]): p for p in site["performance"]}
    for b in ("G-P5", "G-P5-EVAL", "G-P5-EVENT"):
        assert perf[(b, "LUCID")]["open_positions"] == 1 and perf[(b, "LUCID")]["own_reset"]
        assert perf[(b, "LUCID")]["since"] == (T0 - timedelta(hours=1)).isoformat()
        assert (b, "OTHER-VENUE") not in perf
    assert perf[("G-P5-EVAL", "LUCID")]["eval"]["status"] == "ACTIVE"
    assert perf[("G-P5-EVENT", "LUCID")]["trigger"]["fired"] == 1
    assert "eval" not in perf[("G-P5", "LUCID")] and "eval" not in perf[("G-P5-EVENT", "LUCID")]
    assert not perf[("G-LOW-80", "LUCID")].get("own_reset")
    assert "since" not in perf[("MAIN", "LUCID")]          # existing books: panel unchanged
