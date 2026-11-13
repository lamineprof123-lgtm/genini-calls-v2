"""G-LOW-80 (t272u): own LOW-thinking Gemini call on the shared M15 snapshot, conf >= 0.80, own $100k Lucid +
OTHER-VENUE accounts, uniform 1 ATR / 1 ATR / 90-min, 0.3 ATR fill check, m15_calls + scorer + panel. No network."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import gha_loop
from signals.glow80 import GLow80Book, glow80_cfg, gemini_low_cfg, BOOK
from signals.gemini_client import _gen_config
from signals.other_venue import OtherVenueHub, DEFAULT_BOOKS
from signals.m15_scan import M15Coordinator, M15Snapshot
from signals.signal_score import SignalScorer
from signals.panel import PanelLedger, BOOKS, write_reset_marker

UTC = timezone.utc
T0 = datetime(2026, 11, 13, 14, 0, 20, tzinfo=UTC)   # Fri 09:00 New York, in session


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


def q(sym, price, t=T0):
    return {"symbol": sym, "price": price, "ts": t.isoformat(), "fetched_at": t.isoformat(), "age_s": 2.0,
            "stale": False, "source": "dukascopy:tick"}


class Feed:
    def __init__(self, prices):
        self.prices = prices

    async def __call__(self, symbols, now):
        return {s: q(s, self.prices[s], now) for s in symbols if s in self.prices}, {}


def ctx(price, atr, digits=2):
    return {"last_close": price, "atr": atr, "digits": digits, "live_price": price, "live_price_stale": False,
            "last_closed_bar_utc": "2026-11-13T13:45:00+00:00", "session": "NEW_YORK_SESSION",
            "signal_candle": {"open": price, "high": price + atr / 2, "low": price - atr / 2, "close": price},
            "levels": [], "poc": price, "vah": price + atr, "val": price - atr}


def snap():
    payloads = [{"symbol": "XAUUSD", "source": "dukascopy", "ticker": "xauusd", "timeframe": "M15",
                 "context": ctx(3945.0, 5.0)},
                {"symbol": "EURUSD", "source": "dukascopy", "ticker": "eurusd", "timeframe": "M15",
                 "context": ctx(1.10000, 0.0012, 5)},
                {"symbol": "US100", "source": "dukascopy", "ticker": "usatechidxusd", "timeframe": "M15",
                 "context": ctx(21000.0, 40.0)},
                {"symbol": "BTCUSD", "source": "binance", "ticker": "BTCUSDT", "timeframe": "M15",
                 "context": ctx(90000.0, 300.0)}]
    charts = {p["symbol"]: b"\xff\xd8\xff fake" for p in payloads}
    return M15Snapshot(snapshot_id="m15-test", snapshot_hash="h" * 64, package_id="pkg", package_hash="p",
                       bar_close_ts=T0 - timedelta(seconds=20), snapshot_ts=T0, now=T0, payloads=payloads,
                       charts=charts, facts={}, htf={}, chart_sha256={}, package={}, render_ms=1, start_lag_s=20)


def gem(sigs):
    calls = []

    async def call(lcfg, payloads, pngs):
        calls.append({"cfg": lcfg, "symbols": [p["symbol"] for p in payloads]})
        return {"model": "gemini-3.8-flash", "model_version": "gemini-3.8-flash", "latency_s": 3.2,
                "usage": {"prompt_token_count": 9000, "candidates_token_count": 600, "thoughts_token_count": 250,
                          "total_token_count": 9850},
                "raw_text": json.dumps({"signals": sigs, "market_summary": "test"}), "attempts": []}
    call.calls = calls
    return call


def s(sym, action, conf, entry):
    return {"symbol": sym, "trend_vs_vwap": "bearish", "volatility": "normal", "level_event": "none",
            "rationale": "t", "action": action, "confidence": conf, "entry": entry}


def make(cfg, tmp_path, call, prices=None):
    hub = OtherVenueHub(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    bk = GLow80Book(cfg, call=call, ov_hub=hub, quotes=Feed(prices or {"XAUUSD": 3945.0, "BTCUSD": 90000.0,
                                                                         "EURUSD": 1.1, "US100": 21000.0}))
    bk.s["created_at"] = (T0 - timedelta(hours=1)).isoformat()
    return bk, hub


def test_config_low_thinking_and_main_untouched(cfg):
    g = glow80_cfg(cfg)
    assert g["enabled"] and g["model"] == "gemini-3.8-flash" and g["thinking_level"] == "low"
    assert g["min_confidence"] == 0.80 and g["stop_atr"] == 1.0 and g["target_atr"] == 1.0
    assert g["max_hold_minutes"] == 90 and g["risk_usd"] == 900.0 and g["max_chase_atr"] == 0.3
    assert set(g["scan_symbols"]) == set(cfg["general"]["symbols"])          # MAIN's symbol set
    assert "BTCUSD" not in g["symbols"] and g["other_venue_symbols"] == ["BTCUSD"]
    # MAIN Gemini stays on high thinking
    assert cfg["gemini"]["entry_thinking_level"] == "high"
    lc = gemini_low_cfg(cfg, g)
    assert cfg["gemini"]["entry_thinking_level"] == "high"                  # copy, not mutated
    gc = _gen_config(lc, "gemini-3.8-flash", [{"symbol": "XAUUSD"}])
    assert str(gc.thinking_config.thinking_level).lower().endswith("low")
    assert lc["gemini"]["timeout_seconds"] == 45 and lc["gemini"]["fallback_models"] == []
    assert BOOK in DEFAULT_BOOKS and BOOK in cfg["other_venue"]["books"]
    assert "glow80.json" in gha_loop.SITE_JSON
    assert any(b == BOOK and m == "gemini-3.8-flash" for b, m, _ in BOOKS)


def test_on_snapshot_conf_filter_lucid_and_btc(cfg, tmp_path):
    call = gem([s("XAUUSD", "SELL", 0.85, 3945.0), s("EURUSD", "BUY", 0.79, 1.1),
                s("US100", "HOLD", 0.3, 21000.0), s("BTCUSD", "BUY", 0.9, 90000.0)])
    bk, hub = make(cfg, tmp_path, call)
    r = asyncio.run(bk.on_snapshot(snap(), now=T0))
    assert r["ok"] and r["thinking_level"] == "low" and r["thinking_tokens"] == 250
    assert call.calls[0]["cfg"]["gemini"]["entry_thinking_level"] == "low"
    assert r["queued"] == ["XAUUSD"]                                  # EURUSD 0.79 < 0.80 skipped
    pos = bk.s["positions"]
    assert len(pos) == 1 and pos[0]["symbol"] == "XAUUSD" and pos[0]["side"] == -1 and pos[0]["book"] == BOOK
    # 1 ATR stop / target, $900 risk on GC (1 ATR = $500 per contract -> 1 contract)
    assert pos[0]["qty"] == 1 and abs(pos[0]["stop"] - pos[0]["entry"] - 5.0) < 0.11
    assert abs(pos[0]["entry"] - pos[0]["target"] - 5.0) < 0.11
    # BTC went to its OWN other-venue account
    assert hub.held(BOOK, "BTCUSD") and not hub.held("MAIN", "BTCUSD")
    sc = {x["symbol"]: x for x in r["score_signals"]}
    assert sc["XAUUSD"]["traded"] and sc["BTCUSD"]["traded"] and not sc["EURUSD"]["traded"]
    assert all(x["thinking_level"] == "low" and x["book"] == BOOK for x in r["score_signals"])
    day = Path(cfg["general"]["logs_dir"]) / "2026-11-13"
    line = json.loads((day / "glow80_signals.jsonl").read_text().splitlines()[0])
    assert line["thinking_level"] == "low" and line["ok"] and len(line["signals"]) == 4
    assert (Path(cfg["site"]["dir"]) / "glow80.json").exists()
    # next close: held symbols are not re-sent (like MAIN)
    asyncio.run(bk.on_snapshot(snap(), now=T0 + timedelta(minutes=15)))
    assert "XAUUSD" not in call.calls[1]["symbols"] and "BTCUSD" not in call.calls[1]["symbols"]


def test_fill_check_0p3_atr(cfg, tmp_path):
    call = gem([s("XAUUSD", "SELL", 0.9, 3945.0)])
    bk, _ = make(cfg, tmp_path, call, prices={"XAUUSD": 3943.0})   # 2.0 lower = 0.4 ATR worse for a short
    asyncio.run(bk.on_snapshot(snap(), now=T0))
    assert bk.s["positions"] == [] and bk.s["stats"]["skips"].get("chase > max_chase_atr") == 1


def test_uniform_exit_target_and_daily_loss(cfg, tmp_path):
    call = gem([s("XAUUSD", "SELL", 0.9, 3945.0)])
    bk, _ = make(cfg, tmp_path, call)
    asyncio.run(bk.on_snapshot(snap(), now=T0))
    p = bk.s["positions"][0]
    bk._quotes = Feed({"XAUUSD": p["target"] - 0.1})
    asyncio.run(bk.tick(T0 + timedelta(minutes=5)))
    assert bk.s["positions"] == [] and bk.s["stats"]["exit_reasons"] == {"target_1atr": 1}
    trades = (Path(cfg["general"]["logs_dir"]) / "2026-11-13" / "glow80_trades.jsonl").read_text()
    assert json.loads(trades)["book"] == BOOK
    # daily loss stop blocks new entries
    from signals.glow80 import lucid_day
    bk.s["day"] = {"day": lucid_day(datetime.now(UTC)), "net": -1900.0}
    why = bk.eligible({"symbol": "XAUUSD", "action": "SELL", "confidence": 0.9, "atr": 5.0})
    assert why and why.startswith("daily loss")


def test_m15_logs_call_and_scorer(cfg, tmp_path):
    call = gem([s("XAUUSD", "SELL", 0.85, 3945.0)])
    bk, _ = make(cfg, tmp_path, call)
    scorer = SignalScorer(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    co = M15Coordinator(cfg, glow80=bk, scorer=scorer, state_dir=tmp_path / "state",
                        logs_dir=tmp_path / "logs", site_dir=tmp_path)
    out = asyncio.run(co.dispatch(snap()))
    assert out["glow80"]["ok"]
    rows = [json.loads(x) for x in next((tmp_path / "logs").glob("*/m15_calls.jsonl")).read_text().splitlines()]
    g = [x for x in rows if x["book"] == BOOK]
    assert len(g) == 1 and g[0]["ok"] and g[0]["thinking_level"] == "low" and g[0]["model"] == "gemini-3.8-flash"
    q_ = [x for x in scorer.s["queue"] if x.get("book") == BOOK and x["role"] == "signal"]
    assert len(q_) == 1 and q_[0]["thinking_level"] == "low" and q_[0]["symbol"] == "XAUUSD"
    # backfill from logs is idempotent (same key)
    n0 = len(scorer.s["queue"])
    scorer.ingest_logs()
    assert len([x for x in scorer.s["queue"] if x.get("book") == BOOK and x["role"] == "signal"]) == 1
    assert len(scorer.s["queue"]) >= n0


def test_failed_call_logged_not_traded(cfg, tmp_path):
    async def bad(lcfg, payloads, pngs):
        raise RuntimeError("503 UNAVAILABLE")
    bk, _ = make(cfg, tmp_path, bad)
    r = asyncio.run(bk.on_snapshot(snap(), now=T0))
    assert not r["ok"] and "UNAVAILABLE" in r["error"] and bk.s["positions"] == [] and r["score_signals"] == []


def test_panel_shows_glow80(cfg, tmp_path):
    cfg["reset"] = {"token": "tok-1"}
    cfg["other_venue"]["reset_token"] = "tok-1"
    st = tmp_path / "state"
    write_reset_marker(cfg, st, T0 - timedelta(hours=1))
    call = gem([s("XAUUSD", "SELL", 0.85, 3945.0)])
    bk, _ = make(cfg, tmp_path, call)
    asyncio.run(bk.on_snapshot(snap(), now=T0))

    async def qf(syms):
        return {"XAUUSD": {"price": 3944.0}}
    led = PanelLedger(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path, quote_fn=qf,
                      history_fn=False)
    asyncio.run(led.tick(T0 + timedelta(minutes=1)))
    site = json.loads((tmp_path / "panel.json").read_text())
    perf = [p for p in site["performance"] if p["book"] == BOOK]
    assert {p["venue"] for p in perf} == {"LUCID", "OTHER-VENUE"}
    lu = next(p for p in perf if p["venue"] == "LUCID")
    assert lu["open_positions"] == 1 and lu["balance"] < 100000.0   # entry commission only
    row = next(r for r in site["positions"] if r["book"] == BOOK)
    assert row["status"] == "OPEN" and "went_positive_first" in row
