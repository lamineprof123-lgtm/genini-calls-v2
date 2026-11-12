"""DS-FLASH DeepSeek entry-only book: pricing, spend cap, isolation, mechanical exits, signal logging."""
from __future__ import annotations
import asyncio, copy, json, sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import deepseek as D
from signals.exit_versions import GEMINI_MECH_EXIT_VERSION

UTC = timezone.utc
T_OFFPEAK = datetime(2026, 11, 12, 16, 44, tzinfo=UTC)  # Thu 20:44 Dubai, off-peak
T_PEAK = datetime(2026, 11, 12, 7, 30, tzinfo=UTC)       # Thu 07:30 UTC, peak
T_WEEKEND = datetime(2026, 11, 14, 7, 30, tzinfo=UTC)    # Sat, off-peak even in the weekday window

PROBE_USAGE = {
    "prompt_tokens": 719,
    "completion_tokens": 655,
    "total_tokens": 1374,
    "prompt_tokens_details": {"cached_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 610},
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 719,
}


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    c.setdefault("deepseek", {})["enabled"] = True
    c.setdefault("exit_manager", {})["gemini_exit_calls_enabled"] = True  # DS must still be mechanical
    c.setdefault("exit_manager", {})["uniform_1atr_exits"] = False
    return c


def payload(sym, price=3869.0, atr=12.5, blocked=False, bar="2026-11-12T16:30:00+00:00"):
    return {"symbol": sym, "source": "dukascopy", "ticker": sym.lower(), "timeframe": "M15", "digits": 2,
            "style": {"width": 1280, "height": 720},
            "volume": [{"value": 100.0}] * 20,
            "context": {"atr": atr, "live_price": price, "last_close": price, "live_price_stale": False,
                        "last_closed_bar_utc": bar, "last_price": price, "session": "NEW_YORK_SESSION",
                        "session_blocks_automation": blocked, "levels": [], "poc": None, "vah": None,
                        "val": None, "digits": 2,
                        "signal_candle": {"open": price, "high": price, "low": price, "close": price}}}


class Api:
    def __init__(self, signals, usage=None, ok=True, delay=0.0):
        self.signals, self.calls, self.ok, self.delay = signals, [], ok, delay
        self.usage = usage or PROBE_USAGE

    async def __call__(self, cc, *, messages):
        self.calls.append({"cc": cc, "messages": messages})
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.ok:
            return {"ok": False, "raw_text": "", "usage": None, "latency_s": 60.0, "error": "TimeoutError"}
        return {"ok": True, "raw_text": json.dumps({"signals": self.signals, "market_summary": "ok"}),
                "usage": self.usage, "latency_s": 5.3, "model": "deepseek-flash", "http_status": 200}


async def fake_render(payloads):
    return {p["symbol"]: b"\xff\xd8\xff fake" for p in payloads}


def _engine(cfg, api=None):
    return D.DeepSeekEngine(cfg, call_fn=api or Api([]), render_fn=fake_render)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- pricing
def test_peak_windows():
    assert D.is_peak(T_PEAK) is True
    assert D.is_peak(T_OFFPEAK) is False
    assert D.is_peak(T_WEEKEND) is False
    assert D.is_peak(datetime(2026, 11, 12, 0, 59, tzinfo=UTC)) is False
    assert D.is_peak(datetime(2026, 11, 12, 1, 0, tzinfo=UTC)) is True
    assert D.is_peak(datetime(2026, 11, 12, 4, 0, tzinfo=UTC)) is False
    assert D.is_peak(datetime(2026, 11, 12, 6, 0, tzinfo=UTC)) is True
    assert D.is_peak(datetime(2026, 11, 12, 10, 0, tzinfo=UTC)) is False


def test_probe_cost_matches_official_pricing():
    # Probe call at 16:44 UTC was off-peak: 719 miss * $0.15 + 655 out * $0.60 per 1M.
    # reasoning_tokens are already inside completion_tokens, so they are not billed twice.
    cost = D.call_cost(PROBE_USAGE, T_OFFPEAK)
    expected = (719 * 0.15 + 655 * 0.60) / 1e6
    assert abs(cost - expected) < 1e-9
    peak = D.call_cost(PROBE_USAGE, T_PEAK)
    assert abs(peak - expected * 2) < 1e-9
    cached = {**PROBE_USAGE, "prompt_cache_hit_tokens": 400, "prompt_cache_miss_tokens": 319}
    c = D.call_cost(cached, T_OFFPEAK)
    exp = (400 * 0.003 + 319 * 0.15 + 655 * 0.60) / 1e6
    assert abs(c - exp) < 1e-9


# --------------------------------------------------------------------------- engine helpers
def test_mech_exits_even_when_gemini_exits_enabled(cfg):
    eng = _engine(cfg)
    assert cfg["exit_manager"]["gemini_exit_calls_enabled"] is True
    assert eng.sim.mech_exits() is True
    rec = {"t_exit": T_OFFPEAK.isoformat(), "net": -10, "reason": "target", "symbol": "XAUUSD"}
    eng.sim._jsonl("lucid_trades.jsonl", T_OFFPEAK, rec)
    day = Path(cfg["general"]["logs_dir"]) / "2026-11-12"
    rows = [json.loads(l) for l in (day / "deepseek_trades.jsonl").read_text().splitlines()]
    assert rows[-1]["model"] == "deepseek-flash"
    assert rows[-1]["exit_mode"] in ("mech", "uniform-1atr-v1")
    assert rows[-1]["book"] == "DS-FLASH"
    assert rows[-1]["exit_prompt_version"] == GEMINI_MECH_EXIT_VERSION
    assert not (day / "lucid_trades.jsonl").exists()


def test_fresh_account_is_100k_no_eval_fee(cfg):
    eng = _engine(cfg)
    assert eng.sim.s["account"]["balance"] == 100000.0
    assert eng.sim.s["account"]["start_balance"] == 100000.0
    assert eng.sim.s["ledger"]["fees_total"] == 0


def test_filter_skips_btc_and_busy(cfg):
    payloads = [payload("XAUUSD"), payload("EURUSD"), payload("GBPUSD"), payload("US100"), payload("BTCUSD")]
    got = D.filter_ds_payloads(payloads, {"XAUUSD", "BTCUSD"}, cfg)
    assert [p["symbol"] for p in got] == ["EURUSD", "US100"]


def test_build_messages_include_images_and_prompt(cfg):
    payloads = [payload("XAUUSD"), payload("EURUSD")]
    pngs = {"XAUUSD": b"\xff\xd8\xff AAA", "EURUSD": b"\x89PNG" + b"\x00" * 8}
    msgs = D.build_messages(cfg, payloads, pngs)
    assert msgs[0]["role"] == "system"
    assert "XAUUSD" in msgs[0]["content"] and "EURUSD" in msgs[0]["content"]
    parts = msgs[1]["content"]
    images = [p for p in parts if p.get("type") == "image_url"]
    texts = [p for p in parts if p.get("type") == "text"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert images[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert any("Numeric context" in p["text"] or "CODE-COMPUTED FACTS" in p["text"] for p in texts)
    assert any("CODE-COMPUTED FACTS" in p["text"] for p in texts)
    assert any("LIVE PRICE SENT TO MODEL" in p["text"] for p in texts)
    assert not any("LIVE PRICE SENT TO GEMINI" in p["text"] for p in texts)
    # Default (Gemini) wording is unchanged when the optional tag is omitted.
    from signals.gemini_client import chart_user_text, SignalBatch
    assert "LIVE PRICE SENT TO GEMINI" in chart_user_text(payloads[0], 1, 1)
    closer = [p["text"] for p in texts if "SignalBatch" in p["text"]]
    assert closer, "SignalBatch schema must be in the prompt"
    assert '"action"' in closer[0] and "BUY" in closer[0] and "market_summary" in closer[0]
    schema = json.dumps(SignalBatch.model_json_schema())
    # The prompt carries the same schema Gemini enforces via response_schema.
    assert '"trend_vs_vwap"' in closer[0] and '"rationale"' in closer[0]
    assert "symbol" in schema


def test_spend_cap_soft_stops_entries(cfg, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    api = Api([{"symbol": "XAUUSD", "action": "HOLD", "confidence": 0.2, "trend_vs_vwap": "n",
                "volatility": "normal", "level_event": "none", "rationale": "x"}])
    eng = _engine(cfg, api)
    eng.e["spend"][D.spend_day(T_OFFPEAK)] = {"usd": 5.01, "calls": 3, "failed": 0,
                                              "tokens": {"prompt": 0, "cache_hit": 0, "cache_miss": 0,
                                                         "completion": 0, "reasoning": 0}}
    res = run(eng.on_cycle([payload("XAUUSD")], pngs={"XAUUSD": b"\xff\xd8\xff x"}, now=T_OFFPEAK))
    assert res["skipped"] is True
    assert "soft spend cap" in res["why"]
    assert api.calls == []


def test_logs_every_signal_with_confidence_and_writes_site(cfg, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    sigs = [
        {"symbol": "XAUUSD", "action": "SELL", "confidence": 0.81, "trend_vs_vwap": "bearish",
         "volatility": "normal", "level_event": "none", "rationale": "below vwap", "entry": 3869.7},
        {"symbol": "EURUSD", "action": "HOLD", "confidence": 0.2, "trend_vs_vwap": "n",
         "volatility": "normal", "level_event": "none", "rationale": "chop"},
    ]
    api = Api(sigs)
    eng = _engine(cfg, api)
    # Avoid live-quote fill in this unit test: leave no pending due by making on_record see HOLD-only
    # for EURUSD and a SELL for gold — size/fill may skip without a quote. That's fine; we care
    # about the signal log and the site file.
    res = run(eng.on_cycle([payload("XAUUSD"), payload("EURUSD")],
                           pngs={"XAUUSD": b"\xff\xd8\xff x", "EURUSD": b"\xff\xd8\xff y"},
                           now=T_OFFPEAK))
    assert res["skipped"] is False
    assert api.calls and api.calls[0]["cc"]["effort"] == "low"
    day = Path(cfg["general"]["logs_dir"]) / "2026-11-12"
    rows = [json.loads(l) for l in (day / "deepseek_signals.jsonl").read_text().splitlines()]
    actions = {s["symbol"]: s for s in rows[-1]["signals"]}
    assert actions["XAUUSD"]["action"] == "SELL"
    assert actions["XAUUSD"]["confidence"] == 0.81
    assert actions["EURUSD"]["action"] == "HOLD"
    assert rows[-1]["model"] == "deepseek-flash"
    assert rows[-1]["cost_usd"] == D.call_cost(PROBE_USAGE, T_OFFPEAK)
    site = json.loads((Path(cfg["site"]["dir"]) / "deepseek.json").read_text())
    assert site["book"] == "DS-FLASH"
    assert site["model"] == "deepseek-flash"
    assert site["exit_mode"] in ("mech", "uniform-1atr-v1")
    assert site["account"]["balance"] == 100000.0 or site["account"]["start_balance"] == 100000.0
    assert site["spend"]["soft_usd"] == 5.0
    assert "lucid.json" not in [p.name for p in Path(cfg["site"]["dir"]).iterdir() if p.suffix == ".json"] or \
        not (Path(cfg["site"]["dir"]) / "lucid.json").exists()


def test_skips_symbol_with_open_position(cfg, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    api = Api([{"symbol": "EURUSD", "action": "HOLD", "confidence": 0.1, "trend_vs_vwap": "n",
                "volatility": "normal", "level_event": "none", "rationale": "x"}])
    eng = _engine(cfg, api)
    eng.sim.s["positions"].append({"id": 1, "symbol": "XAUUSD", "contract": "GC", "qty": 1, "side": -1,
                                   "entry": 3869.7, "entry_time": T_OFFPEAK.isoformat(), "atr": 12.5})
    run(eng.on_cycle([payload("XAUUSD"), payload("EURUSD")],
                     pngs={"XAUUSD": b"\xff\xd8\xff x", "EURUSD": b"\xff\xd8\xff y"},
                     now=T_OFFPEAK))
    parts = api.calls[0]["messages"][1]["content"]
    texts = " ".join(p["text"] for p in parts if p.get("type") == "text")
    assert "EURUSD" in texts
    assert "XAUUSD" not in texts


def test_throttle_skip_does_not_call(cfg, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    api = Api([])
    eng = _engine(cfg, api)
    from signals.pipeline import THROTTLE_SKIP
    res = run(eng.on_cycle([payload("XAUUSD", blocked=True)], pngs={"XAUUSD": b"\xff\xd8\xff x"},
                           skipped=THROTTLE_SKIP, now=T_OFFPEAK))
    assert res["skipped"] is True
    assert res["why"] == THROTTLE_SKIP
    assert api.calls == []


def test_kick_is_fire_and_forget(cfg, monkeypatch):
    """t251u: kick is a no-op — DS-FLASH must not ride the 2-min Gemini loop."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    api = Api([], delay=0.4)
    eng = _engine(cfg, api)
    D.set_engine(eng)
    try:
        task = D.kick(cfg, [payload("XAUUSD")], pngs={"XAUUSD": b"\xff\xd8\xff x"}, now=T_OFFPEAK)
        assert task is None
        assert api.calls == []
    finally:
        D.set_engine(None)


def test_kick_noop_without_engine(cfg):
    D.set_engine(None)
    assert D.kick(cfg, [payload("XAUUSD")], pngs={"XAUUSD": b"\xff\xd8\xff x"}) is None


def test_config_and_workflow_wire_the_key():
    cfg = load_config()
    assert cfg["deepseek"]["model"] == "deepseek-flash"
    assert cfg["deepseek"]["effort"] == "low"
    assert cfg["deepseek"]["soft_spend_usd"] == 5.0
    wf = (Path(__file__).resolve().parents[2] / ".github/workflows/pipeline.yml").read_text()
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in wf
    # Do not require the value to appear anywhere else in the repo.
    src = (Path(__file__).resolve().parents[1] / "signals" / "deepseek.py").read_text()
    assert "os.environ.get(\"DEEPSEEK_API_KEY\")" in src


def test_kick_is_noop_off_the_two_min_loop(cfg, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    api = Api([])
    eng = _engine(cfg, api)
    D.set_engine(eng)
    try:
        task = D.kick(cfg, [payload("XAUUSD")], pngs={"XAUUSD": b"\xff\xd8\xff x"}, now=T_OFFPEAK)
        assert task is None
        assert api.calls == []
    finally:
        D.set_engine(None)
