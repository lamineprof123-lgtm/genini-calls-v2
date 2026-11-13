"""EVAL-R4 (t288u): EVAL-80 + the backtested R4 entry filter on its own fresh $100k LucidPro eval. G-LOW-80's shared
call (no extra Gemini call), filter values logged per 0.8+ signal, panel card + scorer keys. No network."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import gha_loop
from signals.eval80 import Eval80Book, eval80_cfg
from signals.evalr4 import EvalR4Book, evalr4_cfg, compute_r4, BarHistory, payload_frame, BOOK, DEFAULTS
from signals.glow80 import GLow80Book, lucid_day
from signals.other_venue import OtherVenueHub
from signals.m15_scan import M15Coordinator
from signals.signal_score import SignalScorer, score_key
from signals.panel import PanelLedger, BOOKS, write_reset_marker
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_glow80 import T0, Feed, snap, gem, s

UTC = timezone.utc
PRICES = {"XAUUSD": 3945.0, "BTCUSD": 90000.0, "EURUSD": 1.1, "US100": 21000.0}
LAST_OPEN = pd.Timestamp("2026-11-13T13:45:00Z")          # last closed bar at T0 (14:00:20)


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


def trend(end_close=3945.0, step=0.5, n=800, last_open=LAST_OPEN):
    """Steady M15 uptrend (step per bar) ending at end_close on the bar opened at last_open."""
    idx = pd.date_range(end=last_open, periods=n, freq="15min", tz="UTC")
    close = end_close - step * np.arange(n)[::-1]
    open_ = close - step
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + 0.5, "low": np.minimum(open_, close) - 0.5,
                         "close": close, "volume": 100.0}, index=idx)


def fetcher(frames):
    calls = []

    async def f(sym, n, since=None):
        calls.append((sym, n, since))
        if sym not in frames:
            raise RuntimeError("no data")
        return frames[sym].tail(n)
    f.calls = calls
    return f


def make(cfg, tmp_path, call, frames=None, prices=None):
    hub = OtherVenueHub(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    g = GLow80Book(cfg, call=call, ov_hub=hub, quotes=Feed(prices or PRICES))
    e = Eval80Book(cfg, quotes=Feed(prices or PRICES))
    r = EvalR4Book(cfg, quotes=Feed(prices or PRICES), bars_fetch=fetcher({"XAUUSD": trend()} if frames is None else frames))
    for b in (g, e, r):
        b.s["created_at"] = (T0 - timedelta(hours=1)).isoformat()
    return g, e, r


def run(g, e, r, now=T0, sn=None):
    sn = sn or snap()
    out = asyncio.run(g.on_snapshot(sn, now=now))
    return out, asyncio.run(e.on_shared(out, sn, now=now)), asyncio.run(r.on_shared(out, sn, now=now))


def test_config_and_other_books_unchanged(cfg):
    b = evalr4_cfg(cfg)
    assert b["enabled"] and b["min_confidence"] == 0.80 and "BTCUSD" not in b["symbols"]
    assert (b["h1_trend_min_atr"], b["max_px_vs_ema20_atr"], b["max_last3_atr"], b["max_vwap_dist_atr"]) == (2.7, 3.0, 2.2, 3.5)
    for k in ("stop_atr", "target_atr", "max_hold_minutes", "max_chase_atr", "risk_usd", "max_minis_per_position",
              "max_minis", "start_balance", "profit_target", "mll", "initial_trail_balance", "locked_mll_balance",
              "dll", "flat_utc", "no_new_entries_last_minutes"):
        assert b[k] == eval80_cfg(cfg)[k], k
    assert b["reset_token"] != eval80_cfg(cfg)["reset_token"]
    assert "evalr4.json" in gha_loop.SITE_JSON and "eval80.json" in gha_loop.SITE_JSON
    assert [x for x, _, _ in BOOKS].index(BOOK) == [x for x, _, _ in BOOKS].index("EVAL-80") + 1
    assert EvalR4Book.STATE_SUBDIR != Eval80Book.STATE_SUBDIR and EvalR4Book.SIGNALS_LOG != Eval80Book.SIGNALS_LOG


def test_compute_r4_formulas_and_no_lookahead():
    df = trend()
    t = T0
    r = compute_r4(df, 1, 3945.0, 5.0, t, DEFAULTS)
    assert r["pass"] and r["reason"] is None and set(r["conds"]) == {"h1_trend", "m15_ema20", "chase", "vwap"}
    c = {k: v["value"] for k, v in r["conds"].items()}
    # hand check against the backtest formulas
    close = df.close
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    hc = close.resample("1h", label="left", closed="left").last().dropna()
    hc = hc[hc.index + pd.Timedelta(hours=1) <= pd.Timestamp(t)]
    h = (hc.ewm(span=20, adjust=False).mean().iloc[-1] - hc.ewm(span=50, adjust=False).mean().iloc[-1]) / 5.0
    day = df[df.index.floor("D") == LAST_OPEN.floor("D")]
    tp = (day.high + day.low + day.close) / 3
    vw = (tp * day.volume).sum() / day.volume.sum()
    assert abs(c["h1_trend"] - round(h, 3)) < 1e-9 and c["h1_trend"] >= 2.7
    assert abs(c["m15_ema20"] - round((3945.0 - ema20) / 5.0, 3)) < 1e-9
    assert abs(c["chase"] - round((3945.0 - df.open.iloc[-3]) / 5.0, 3)) < 1e-9
    assert abs(c["vwap"] - round((3945.0 - vw) / 5.0, 3)) < 1e-9
    # SELL against the H1 trend fails that condition only (signed formulas)
    rs = compute_r4(df, -1, 3945.0, 5.0, t, DEFAULTS)
    assert not rs["pass"] and not rs["conds"]["h1_trend"]["pass"] and rs["conds"]["chase"]["pass"]
    assert "H1 EMA20-EMA50" in rs["reason"]
    # stretched + far from VWAP
    rx = compute_r4(df, 1, 3965.0, 5.0, t, DEFAULTS)
    assert not rx["conds"]["m15_ema20"]["pass"] and not rx["conds"]["vwap"]["pass"] and rx["conds"]["h1_trend"]["pass"]
    # chasing: last 3 bars ran 3 ATR
    dc = df.copy()
    dc.iloc[-3:, dc.columns.get_loc("close")] = [3935.0, 3945.0, 3955.0]
    dc.iloc[-3, dc.columns.get_loc("open")] = 3940.0
    assert not compute_r4(dc, 1, 3955.0, 5.0, t, DEFAULTS)["conds"]["chase"]["pass"]
    # no lookahead: a bar that has not closed at t (and anything later) changes nothing
    fut = pd.DataFrame({"open": [3945.0, 4100.0], "high": [4200.0, 4300.0], "low": [3000.0, 3000.0],
                        "close": [4100.0, 4200.0], "volume": [1e6, 1e6]},
                       index=[LAST_OPEN + pd.Timedelta(minutes=15), LAST_OPEN + pd.Timedelta(minutes=30)])
    r2 = compute_r4(pd.concat([df, fut]), 1, 3945.0, 5.0, t, DEFAULTS)
    assert r2["conds"] == r["conds"] and r2["last_bar"] == LAST_OPEN.isoformat()
    # not enough history -> fails with "no data"
    r3 = compute_r4(df.tail(100), 1, 3945.0, 5.0, t, DEFAULTS)
    assert not r3["pass"] and r3["reason"].startswith("no data") and r3["conds"] == {}
    assert compute_r4(None, 1, 3945.0, 5.0, t, DEFAULTS)["reason"].startswith("no data")


def test_shared_call_filter_trades_and_logs(cfg, tmp_path):
    call = gem([s("XAUUSD", "BUY", 0.85, 3945.0), s("US100", "BUY", 0.9, 21000.0),
                s("EURUSD", "BUY", 0.75, 1.1), s("BTCUSD", "BUY", 0.9, 90000.0)])
    g, e, r = make(cfg, tmp_path, call)
    _, oe, orr = run(g, e, r)
    assert len(call.calls) == 1                                       # one Gemini call for G-LOW-80/EVAL-80/EVAL-R4
    assert oe["queued"] == ["US100", "XAUUSD"]                        # EVAL-80 unchanged: both 0.8+ Lucid signals
    assert orr["ok"] and orr["queued"] == ["XAUUSD"]                  # US100 has no history -> filtered (no data)
    assert orr["r4"]["XAUUSD"]["pass"] and not orr["r4"]["US100"]["pass"]
    assert "EURUSD" not in orr["r4"] and "BTCUSD" not in orr["r4"]  # < 0.80 / not a Lucid symbol: not evaluated
    p = r.s["positions"][0]
    assert p["book"] == BOOK and p["contract"] == "GC" and p["qty"] == 1 and p["r4"]["pass"]
    assert abs(p["entry"] - p["stop"] - 4.0) < 0.11 and abs(p["target"] - p["entry"] - 5.0) < 0.11
    day = Path(cfg["general"]["logs_dir"]) / "2026-11-13"
    line = json.loads((day / "evalr4_signals.jsonl").read_text().splitlines()[0])
    sx = {x["symbol"]: x for x in line["signals"]}
    assert sx["XAUUSD"]["traded"] and sx["XAUUSD"]["r4"]["pass"]
    for k in ("h1_trend", "m15_ema20", "chase", "vwap"):
        cnd = sx["XAUUSD"]["r4"]["conds"][k]
        assert {"value", "threshold", "pass", "reason", "feature"} <= set(cnd)
    assert sx["US100"]["r4"]["reason"].startswith("no data") and "r4" not in sx["EURUSD"]
    ev = [json.loads(x) for x in (day / "evalr4_events.jsonl").read_text().splitlines()]
    sk = next(x for x in ev if x["event"] == "skip" and x["symbol"] == "US100")
    assert sk["reason"].startswith("R4 filter") and sk["r4"]["pass"] is False
    assert r.s["stats"]["skips"]["r4_filter"] == 1
    v = r.eval_view(T0)["filter"]
    assert (v["seen"], v["passed"], v["filtered_out"], v["no_data"]) == (2, 1, 1, 1)
    site = json.loads((tmp_path / "evalr4.json").read_text())
    assert site["book"] == BOOK and site["eval"]["mll_floor"] == 97000.0 and site["r4"]["passed"] == 1
    assert json.loads((tmp_path / "eval80.json").read_text())["book"] == "EVAL-80"
    assert not (day / "eval80_signals.jsonl").read_text().count('"r4"')   # EVAL-80 log unchanged


def test_filter_failure_counted_per_condition(cfg, tmp_path):
    g, e, r = make(cfg, tmp_path, gem([s("XAUUSD", "SELL", 0.9, 3945.0)]))
    _, oe, orr = run(g, e, r)
    assert oe["queued"] == ["XAUUSD"] and orr["queued"] == []        # EVAL-80 sells; R4: against the H1 trend
    assert r.s["r4"]["fail_by"]["h1_trend"] == 1 and r.s["r4"]["filtered_out"] == 1
    assert r.s["r4"]["last"]["XAUUSD"]["pass"] is False


def test_hold_and_missed_when_glow80_holds(cfg, tmp_path):
    call = gem([s("XAUUSD", "BUY", 0.75, 3945.0)])               # G-LOW-80 takes it (0.70); EVAL books do not
    g, e, r = make(cfg, tmp_path, call)
    _, _, o1 = run(g, e, r)
    assert o1["ok"] and o1["queued"] == [] and r.s["r4"]["seen"] == 0
    _, _, o2 = run(g, e, r, now=T0 + timedelta(minutes=15))
    assert o2["missed_held_by_glow80"] == ["XAUUSD"] and r.eval_view(T0)["missed_scans"] == {"XAUUSD": 1}


def test_eval_rules_same_as_eval80(cfg, tmp_path):
    r = EvalR4Book(cfg, bars_fetch=fetcher({}))
    r.enforce(T0)
    assert r.ev["mll_floor"] == 97000.0 and r.ev["status"] == "ACTIVE"
    r.s["balance"] = 103200.0
    r.enforce(T0 + timedelta(days=1))
    assert r.ev["mll_floor"] == 100100.0 and r.ev["mll_locked"]
    r.s["balance"] = 100100.0
    r.enforce(T0 + timedelta(days=1, hours=1))
    assert r.ev["status"] == "FAILED"
    r.enforce(T0 + timedelta(days=3))
    assert r.ev["status"] == "FAILED"                                 # no auto-reset
    r2 = EvalR4Book(copy.deepcopy(cfg) | {"general": {**cfg["general"], "state_dir": str(tmp_path / "s2")}},
                    bars_fetch=fetcher({}))
    r2.s["day"] = {"day": lucid_day(T0), "net": -1850.0}
    r2.enforce(T0)
    assert r2.dll_paused(T0) and r2.active()
    r2.s["balance"] = 106000.0
    r2.enforce(T0)
    assert r2.ev["status"] == "PASSED"


def test_history_seed_and_update(cfg):
    b = evalr4_cfg(cfg)
    full = trend()
    f = fetcher({"XAUUSD": full.iloc[:-4]})
    h = BarHistory(cfg, ["XAUUSD", "EURUSD"], b, fetch=f)
    asyncio.run(h.seed_all())
    assert len(h.df["XAUUSD"]) == 796 and "EURUSD" not in h.df
    last4 = full.tail(4)
    p = {"symbol": "XAUUSD", "source": "dukascopy",
         "candles": [{"time": int(t.timestamp()), "open": o, "high": hi, "low": lo, "close": c}
                     for t, o, hi, lo, c in zip(last4.index, last4.open, last4.high, last4.low, last4.close)],
         "volume": [{"time": int(t.timestamp()), "value": 100.0} for t in last4.index]}
    assert len(payload_frame(p)) == 4
    sn = type("S", (), {"payloads": [p]})()
    notes = asyncio.run(h.update(sn, T0))
    assert notes["XAUUSD"] == "snapshot" and h.df["XAUUSD"].index[-1] == LAST_OPEN and len(h.df["XAUUSD"]) == 800
    # non-Dukascopy snapshot -> short Dukascopy refresh instead
    f2 = fetcher({"XAUUSD": full})
    h.__dict__["_fetch"] = f2
    notes = asyncio.run(h.update(type("S", (), {"payloads": [{**p, "source": "yfinance"}]})(), T0))
    assert notes["XAUUSD"] == "dukascopy-refresh" and f2.calls[0][2] is not None


def test_m15_dispatch_logs_and_scorer_keys(cfg, tmp_path):
    call = gem([s("XAUUSD", "BUY", 0.85, 3945.0)])
    g, e, r = make(cfg, tmp_path, call)
    scorer = SignalScorer(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    co = M15Coordinator(cfg, glow80=g, eval80=e, evalr4=r, scorer=scorer, state_dir=tmp_path / "state",
                        logs_dir=tmp_path / "logs", site_dir=tmp_path)
    out = asyncio.run(co.dispatch(snap()))
    assert len(call.calls) == 1 and out["glow80"]["eval80"]["ok"] and out["glow80"]["evalr4"]["ok"]
    assert out["glow80"]["evalr4"]["queued"] == ["XAUUSD"]
    rows = [json.loads(x) for x in next((tmp_path / "logs").glob("*/m15_calls.jsonl")).read_text().splitlines()]
    ev = [x for x in rows if x["book"] == BOOK]
    assert len(ev) == 1 and ev[0]["shared_from"] == "G-LOW-80" and ev[0]["cost_usd"] == 0.0
    assert ev[0]["r4"]["XAUUSD"]["pass"] and ev[0]["history"]["XAUUSD"]["bars"] == 800
    sig = lambda b: [x for x in scorer.s["queue"] if x.get("book") == b and x["role"] == "signal"]
    assert len(sig(BOOK)) == 1 and len(sig("EVAL-80")) == 1 and len(sig("G-LOW-80")) == 1
    scorer.ingest_logs()                                              # idempotent backfill
    assert len(sig(BOOK)) == 1 and len(sig("EVAL-80")) == 1 and len(sig("G-LOW-80")) == 1
    keys = {score_key(sig(b)[0]) for b in (BOOK, "EVAL-80", "G-LOW-80")}
    assert len(keys) == 3 and score_key(sig(BOOK)[0]).endswith("|" + BOOK)
    assert not score_key(sig("G-LOW-80")[0]).endswith("G-LOW-80") and score_key(sig("EVAL-80")[0]).endswith("|EVAL-80")
    assert "r4" not in sig(BOOK)[0]


def test_panel_card_and_positions(cfg, tmp_path):
    cfg["reset"] = {"token": "tok-1"}
    cfg["other_venue"]["reset_token"] = "tok-1"
    st = tmp_path / "state"
    write_reset_marker(cfg, st, T0 - timedelta(hours=1))
    g, e, r = make(cfg, tmp_path, gem([s("XAUUSD", "BUY", 0.9, 3945.0), s("US100", "BUY", 0.9, 21000.0)]))
    run(g, e, r)

    async def qf(syms):
        return {"XAUUSD": {"price": 3946.0}, "US100": {"price": 21010.0}}
    led = PanelLedger(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path, quote_fn=qf, history_fn=False)
    asyncio.run(led.tick(T0 + timedelta(minutes=1)))
    site = json.loads((tmp_path / "panel.json").read_text())
    perf = [p for p in site["performance"] if p["book"] == BOOK]
    assert [p["venue"] for p in perf] == ["LUCID"] and perf[0]["open_positions"] == 1
    ev = perf[0]["eval"]
    assert ev["status"] == "ACTIVE" and ev["mll_floor"] == 97000.0
    assert ev["filter"]["seen"] == 2 and ev["filter"]["filtered_out"] == 1 and ev["filter"]["no_data"] == 1
    assert "went_positive_first" in perf[0] and "went_positive_first_pct" in perf[0]
    e80 = next(p for p in site["performance"] if p["book"] == "EVAL-80")
    assert e80["eval"]["status"] == "ACTIVE" and "filter" not in e80["eval"] and e80["open_positions"] == 2
    row = next(x for x in site["positions"] if x["book"] == BOOK)
    assert row["symbol"] == "XAUUSD" and row["status"] == "OPEN" and "went_positive_first" in row


def test_index_html_filter_card():
    html = (Path(__file__).resolve().parent.parent.parent / "index.html").read_text()
    assert "filtTxt" in html and "Filtered out by the R4 rules" in html and "evalCard" in html
    assert "countdown" not in html.lower().replace("no countdown", "")
