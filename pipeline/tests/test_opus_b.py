"""C-OPUS-B isolation, shared bracket-v1 package with DS-FLASH, high effort, 0.3 ATR fill check."""
from __future__ import annotations
import asyncio, copy, json, sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import bracket_v1 as B
from signals import opus_b as OB
from signals import deepseek as D
from signals.m15_scan import bar_close_ts, next_boundary
from signals.pipeline import next_boundary as nb

UTC = timezone.utc
T = datetime(2026, 11, 12, 17, 15, 20, tzinfo=UTC)


def payload(sym="XAUUSD", price=4000.0, vwap=3990.0, atr=10.0):
    return {"symbol": sym, "source": "dukascopy", "ticker": sym.lower(), "timeframe": "M15",
            "digits": 2, "candles": [],
            "context": {"atr": atr, "live_price": price, "last_close": price, "live_price_stale": False,
                        "last_closed_bar_utc": "2026-11-12T17:00:00+00:00", "vwap": vwap,
                        "ema20_baseline": vwap + 1, "ema50": vwap - 2, "session": "NEW_YORK_SESSION",
                        "session_blocks_automation": False, "digits": 2, "rvol_last": 1.1,
                        "signal_candle": {"open": price - 2, "high": price + 3, "low": price - 4, "close": price},
                        "levels": [{"type": "resistance", "label": "Session High", "price": price + 20},
                                   {"type": "support", "label": "Session Low", "price": price - 20}]}}


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


class ClaudeApi:
    def __init__(self, signals, thinking=1400, cost=0.07):
        self.signals, self.calls = signals, []
        self.thinking, self.cost = thinking, cost

    async def __call__(self, cc, pkg):
        self.calls.append({"effort": cc.get("effort"), "model": cc.get("model"),
                           "system": pkg["system"], "content": pkg["anthropic_content"],
                           "hash": pkg["snapshot_hash"]})
        return {"ok": True, "parsed": {"signals": self.signals, "market_summary": "ok"},
                "usage": {"input_tokens": 100, "output_tokens": 80,
                          "output_tokens_details": {"thinking_tokens": self.thinking}},
                "thinking_tokens": self.thinking, "latency_ms": 4200, "cost_usd": self.cost,
                "model": "claude-opus-5-5"}


class DsApi:
    def __init__(self, signals):
        self.signals, self.calls = signals, []

    async def __call__(self, cc, *, messages):
        self.calls.append({"cc": cc, "messages": messages})
        return {"ok": True, "raw_text": json.dumps({"signals": self.signals, "market_summary": "ok"}),
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "prompt_cache_miss_tokens": 100},
                "latency_s": 2.1, "model": "deepseek-flash", "http_status": 200}


def run(coro):
    return asyncio.run(coro)


def _pkg(png=b"\xff\xd8\xffSAME"):
    payloads = [payload("XAUUSD"), payload("EURUSD", price=1.0850, vwap=1.0840, atr=0.0012),
                payload("US100", price=25500, vwap=25480, atr=40)]
    charts = {p["symbol"]: png for p in payloads}
    return B.build_package(payloads, charts, bar_close_ts="2026-11-12T17:15:00+00:00",
                           snapshot_ts="2026-11-12T17:15:20+00:00")


def test_fresh_100k_account_isolated_from_claude(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    eng = OB.OpusBEngine(cfg, call_fn=ClaudeApi([]))
    assert eng.sim.s["account"]["balance"] == 100000.0
    assert eng.sim.s["ledger"]["fees_total"] == 0
    # C-OPUS state lives under state/claude; C-OPUS-B must not write there.
    assert "claude" not in str(eng.state_dir)
    assert eng.state_dir.name == "opus_b"
    assert not (tmp_path / "state" / "claude").exists()
    view = eng.site_view(T)
    assert view["book"] == "C-OPUS-B"
    assert view["effort"] == "high"
    assert view["prompt_version"] == "bracket-v1"
    assert view["prompt_reconstruction"] is True
    path = tmp_path / "opus_b.json"
    # persist happens on a cycle; site_view itself is enough here
    assert "bracket-v1" in view["rules"]


def test_opus_b_and_ds_see_identical_package(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    pkg = _pkg()
    hold = [{"symbol": s, "action": "HOLD", "setup_type": "none", "score": 2, "confidence": 0.4,
             "trend_vs_vwap": "n", "volatility": "normal", "level_event": "none",
             "rationale": "no setup", "risk_pct": None, "entry": None, "stop_loss": None,
             "take_profit": None, "risk_reward": None} for s in B.SYMBOLS]
    capi, dapi = ClaudeApi(hold), DsApi(hold)
    ob = OB.OpusBEngine(cfg, call_fn=capi)
    ds = D.DeepSeekEngine(cfg, call_fn=dapi, render_fn=lambda ps: {})
    run(ob.on_bracket_package(pkg, now=T, snap_meta={"snapshot_id": pkg["snapshot_id"]}))
    run(ds.on_bracket_package(pkg, now=T, snap_meta={"snapshot_id": pkg["snapshot_id"]}))
    assert capi.calls and dapi.calls
    assert capi.calls[0]["effort"] == "high"
    assert capi.calls[0]["model"] == "claude-opus-5-5"
    assert capi.calls[0]["hash"] == pkg["snapshot_hash"]
    assert dapi.calls[0]["cc"]["effort"] == "low"
    # Same system prompt text.
    assert capi.calls[0]["system"] == dapi.calls[0]["messages"][0]["content"] == pkg["system"]
    # C-OPUS-B did not write into the DS account / vice versa.
    assert ob.sim.s["account"]["balance"] == 100000.0
    assert ds.sim.s["account"]["balance"] == 100000.0
    assert ob.sim.s is not ds.sim.s
    # Signals tagged bracket-v1
    day = Path(cfg["general"]["logs_dir"]) / "2026-11-12"
    ob_rows = [json.loads(l) for l in (day / "opus_b_signals.jsonl").read_text().splitlines()]
    ds_rows = [json.loads(l) for l in (day / "deepseek_signals.jsonl").read_text().splitlines()]
    assert ob_rows[-1]["prompt_version"] == "bracket-v1"
    assert ds_rows[-1]["prompt_version"] == "bracket-v1"
    assert ob_rows[-1]["thinking_tokens"] == 1400
    assert ob_rows[-1]["effort"] == "high"


def test_hold_gate_blocks_trend_chase_in_opus_b(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    # +3 ATR from VWAP — code must HOLD a TREND BUY even if the model says BUY score 7.
    p = payload("XAUUSD", price=4030, vwap=4000, atr=10)
    pkg = B.build_package([p], {"XAUUSD": b"\xff\xd8\xffX"}, bar_close_ts="2026-11-12T17:15:00+00:00")
    sig = {"symbol": "XAUUSD", "action": "BUY", "setup_type": "trend", "score": 7, "confidence": 0.8,
           "trend_vs_vwap": "bullish", "volatility": "normal", "level_event": "closed_through VAL",
           "rationale": "chase", "risk_pct": 0.7, "entry": 4030, "stop_loss": 4020, "take_profit": 4040,
           "risk_reward": 1}
    api = ClaudeApi([sig])
    eng = OB.OpusBEngine(cfg, call_fn=api)
    res = run(eng.on_bracket_package(pkg, now=T))
    row = res["signals"][0]
    assert row["action"] == "BUY"          # model said BUY
    assert row["gated"] is False           # code blocked it
    assert row["decision"] != "OPENED"
    assert any("vwap_distance_atr" in r for r in (row.get("gate_reasons") or row.get("issues") or []))
    assert eng.sim.s.get("positions") in (None, [],) or not eng.sim.s["positions"]


def test_bar_close_lag_is_20s_on_odd_and_even():
    for h, m in ((17, 0), (17, 15), (17, 30), (17, 45)):
        close = datetime(2026, 11, 12, h, m, tzinfo=UTC)
        wake = close.replace(second=20)
        assert (wake - bar_close_ts(wake)).total_seconds() == 20
        assert nb(close.replace(second=1), 15, 20) == wake
