"""prompt-v3: Signal schema property order, slim Gemini context, blocked-session throttle."""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.gemini_client import (
    SIGNAL_FIELD_ORDER, Signal, SignalBatch, slim_gemini_context, chart_user_text, system_prompt,
)
from signals.pipeline import (
    session_blocked_for_scan, gemini_bar_changed, should_throttle_gemini,
    scan_interval_minutes, THROTTLE_SKIP, _throttle_skip_record, lucid_symbols,
)
from signals.validator import parse_raw, validate_signal
from signals.scalp_levels import apply_server_levels


def test_signal_schema_field_order():
    props = list(Signal.model_json_schema()["properties"])
    assert props == list(SIGNAL_FIELD_ORDER)
    required = Signal.model_json_schema()["required"]
    # Analytical fields are listed (and required) before action/confidence.
    assert required.index("rationale") < required.index("action")
    assert required.index("trend_vs_vwap") < required.index("action")
    batch = SignalBatch.model_json_schema()
    # Signal definition inside $defs (or properties via $ref) keeps the same order
    defs = batch.get("$defs") or batch.get("defs") or {}
    sig = defs.get("Signal") or {}
    if sig:
        assert list(sig["properties"]) == list(SIGNAL_FIELD_ORDER)


def test_google_genai_property_ordering():
    """google-genai copies properties.keys() into propertyOrdering when unset."""
    try:
        from google.genai import _transformers as tr
        from google.genai import types
    except ImportError:
        pytest.skip("google-genai not installed")
    schema = SignalBatch.model_json_schema()
    tr.process_schema(schema, client=None)
    sig = schema.get("$defs", {}).get("Signal") or schema
    # After process_schema, Signal may be inlined or stay in defs.
    # Property order on the model schema must survive, and ordering must be set.
    if "properties" in schema and "signals" in schema["properties"]:
        # The batch object itself also gets property_ordering
        assert list(schema["properties"])[0] == "signals"
    # Re-process a copy of the Signal schema alone
    sig_schema = Signal.model_json_schema()
    tr.process_schema(sig_schema, client=None)
    ordering = sig_schema.get("property_ordering") or sig_schema.get("propertyOrdering")
    assert ordering == list(SIGNAL_FIELD_ORDER)


def test_parse_accepts_both_key_orders():
    base = dict(symbol="EURUSD", trend_vs_vwap="bearish below VWAP", volatility="normal",
                level_event="none", rationale="checklist...", action="HOLD", confidence=0.4)
    # New order (analytical first) and old order (action first) both validate.
    new_order = {k: base[k] for k in SIGNAL_FIELD_ORDER if k in base}
    old_order_keys = ["symbol", "action", "entry", "stop_loss", "take_profit", "risk_reward",
                      "confidence", "risk_pct", "trend_vs_vwap", "volatility", "level_event", "rationale"]
    old_order = {k: base[k] for k in old_order_keys if k in base}
    raw_new = json.dumps({"signals": [new_order], "market_summary": "ok"})
    raw_old = json.dumps({"signals": [old_order], "market_summary": "ok"})
    obj, err = parse_raw(raw_new)
    assert err is None and obj["signals"][0]["action"] == "HOLD"
    obj, err = parse_raw(raw_old)
    assert err is None and obj["signals"][0]["action"] == "HOLD"


def test_buy_with_zero_levels_is_overwritten_not_rejected():
    """Gemini now sends stop_loss=take_profit=risk_reward=0; server scalp overwrites before validate."""
    ctx = {
        "last_close": 1.10000, "atr": 0.00120, "digits": 5,
        "signal_candle": {"open": 1.10010, "high": 1.10080, "low": 1.09940, "close": 1.10000},
        "levels": [], "poc": 1.1000, "vah": 1.1010, "val": 1.0990,
    }
    cfg = {"scalp": {"enabled": True, "atr_stop_mult": 0.8, "min_stop_atr": 0.25,
                     "structure_buffer_atr": 0.05, "fixed_target": {"EURUSD": {"pips": 12.5, "pip_size": 0.0001}}},
           "risk": {"min_rr": 1.5, "min_confidence": 0.5, "max_entry_distance_atr": 1.0,
                    "min_stop_distance_atr": 0.2, "default_risk_pct": 0.5, "max_risk_pct": 1.0},
           "symbols": {"EURUSD": {"contract_size": 100000, "digits": 5}}}
    sig = {"symbol": "EURUSD", "action": "BUY", "entry": 1.10000,
           "stop_loss": 0, "take_profit": 0, "risk_reward": 0,
           "confidence": 0.8, "risk_pct": 0.5,
           "trend_vs_vwap": "bullish", "volatility": "normal", "level_event": "none",
           "rationale": "confluence ok"}
    # Raw zeros would fail the side check if validated first
    raw = validate_signal(sig, "EURUSD", ctx, cfg, equity=100000)
    assert raw["action"] == "HOLD"
    apply_server_levels(sig, ctx, cfg, "EURUSD")
    assert sig["stop_loss"] < sig["entry"] < sig["take_profit"]
    assert sig["stop_loss"] != 0 and sig["take_profit"] != 0
    v = validate_signal(sig, "EURUSD", ctx, cfg, equity=100000)
    assert v["action"] == "BUY" and not v["downgraded"]


def _payload(sym, session, blocked, bar="2026-11-11T08:00:00+00:00"):
    return {
        "symbol": sym, "source": "dukascopy", "ticker": sym.lower(), "timeframe": "M15",
        "digits": 5,
        "context": {
            "last_closed_bar_utc": bar, "session": session,
            "session_blocks_automation": blocked,
            "session_tz": "America/New_York", "vp_value_area_pct": 0.70,
            "last_close": 1.0, "last_price": 1.0, "last_price_ts_utc": bar,
            "signal_candle": {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0},
            "last_candle": {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0},
        },
    }


CFG = {
    "general": {"timeframe_minutes": 15, "session_tz": "Asia/Dubai", "logs_dir": "logs",
                "state_dir": "state", "log_mode": "compact", "symbols": ["XAUUSD", "EURUSD", "GBPUSD", "US100", "BTCUSD"]},
    "scheduler": {"interval_minutes": 2},
    "lucid": {"symbols": {"US100": {}, "XAUUSD": {}, "EURUSD": {}, "GBPUSD": {}}},
}


def test_lucid_symbols_exclude_btc():
    assert "BTCUSD" not in lucid_symbols(CFG)
    assert set(lucid_symbols(CFG)) == {"US100", "XAUUSD", "EURUSD", "GBPUSD"}


def test_throttle_when_all_lucid_blocked_and_bar_unchanged():
    bar = "2026-11-11T08:00:00+00:00"
    payloads = [
        _payload("XAUUSD", "ASIAN_SESSION_CHOP", True, bar),
        _payload("EURUSD", "ASIAN_SESSION_CHOP", True, bar),
        _payload("GBPUSD", "ASIAN_SESSION_CHOP", True, bar),
        _payload("US100", "ASIAN_SESSION_CHOP", True, bar),
        _payload("BTCUSD", "ASIAN_SESSION_CHOP", True, bar),
    ]
    seen = {p["symbol"]: bar for p in payloads}
    assert session_blocked_for_scan(payloads, CFG) is True
    assert gemini_bar_changed(payloads, seen, CFG) is False
    assert should_throttle_gemini(payloads, seen, CFG) is True
    # New M15 close -> first cycle after :00/:15/:30/:45 must call Gemini
    new = [_payload(p["symbol"], "ASIAN_SESSION_CHOP", True, "2026-11-11T08:15:00+00:00") for p in payloads]
    assert should_throttle_gemini(new, seen, CFG) is False
    assert gemini_bar_changed(new, seen, CFG) is True


def test_no_throttle_during_london_even_if_same_bar():
    bar = "2026-11-11T09:00:00+00:00"
    payloads = [
        _payload("XAUUSD", "LONDON_SESSION", False, bar),
        _payload("EURUSD", "LONDON_SESSION", False, bar),
        _payload("GBPUSD", "LONDON_SESSION", False, bar),
        _payload("US100", "LONDON_SESSION", False, bar),
        _payload("BTCUSD", "LONDON_SESSION", False, bar),
    ]
    seen = {p["symbol"]: bar for p in payloads}
    assert session_blocked_for_scan(payloads, CFG) is False
    assert should_throttle_gemini(payloads, seen, CFG) is False


def test_btc_ignored_for_session_decision():
    bar = "2026-11-11T08:00:00+00:00"
    # Four Lucid symbols blocked; BTC hypothetically unblocked must not disable the throttle.
    payloads = [
        _payload("XAUUSD", "LOW_LIQUIDITY_DEADZONE", True, bar),
        _payload("EURUSD", "LOW_LIQUIDITY_DEADZONE", True, bar),
        _payload("GBPUSD", "LOW_LIQUIDITY_DEADZONE", True, bar),
        _payload("US100", "LOW_LIQUIDITY_DEADZONE", True, bar),
        _payload("BTCUSD", "NEW_YORK_SESSION", False, bar),
    ]
    seen = {p["symbol"]: bar for p in payloads}
    assert session_blocked_for_scan(payloads, CFG) is True
    assert should_throttle_gemini(payloads, seen, CFG) is True


def test_scan_interval_minutes():
    assert scan_interval_minutes(False, CFG) == 2
    assert scan_interval_minutes(True, CFG) == 15


def test_throttle_skip_record_has_timestamps_and_reason(tmp_path):
    cfg = json.loads(json.dumps(CFG))
    cfg["general"]["logs_dir"] = str(tmp_path)
    cfg["general"]["log_mode"] = "compact"
    now = datetime(2026, 11, 11, 8, 6, 20, 123456, tzinfo=timezone.utc)
    payloads = [_payload("EURUSD", "ASIAN_SESSION_CHOP", True)]
    rec = _throttle_skip_record(cfg, payloads, {}, {"EURUSD": "2026-11-11T08:00:00+00:00"},
                                now, ZoneInfo("Asia/Dubai"))
    assert rec["skipped_reason"] == THROTTLE_SKIP
    assert rec["scan_interval_minutes"] == 15
    assert rec["signals"] == []
    assert rec["run_at_utc"] == "2026-11-11T08:06:20.123456+00:00"
    assert rec["run_at_local"].startswith("2026-11-11T12:06:20.123456+04:00")
    assert Path(rec["run_dir"]).is_file()
    disk = json.loads(Path(rec["run_dir"]).read_text())
    assert disk["skipped_reason"] == THROTTLE_SKIP
    assert "." in disk["run_at_utc"]  # sub-second


def test_slim_gemini_context_drops_redundant_keys():
    ctx = _payload("EURUSD", "LONDON_SESSION", False)["context"]
    slim = slim_gemini_context(ctx)
    for k in ("last_candle", "last_close", "vp_value_area_pct", "session_tz"):
        assert k not in slim
    assert "signal_candle" in slim and "last_price" in slim and "last_closed_bar_utc" in slim
    text = chart_user_text(_payload("EURUSD", "LONDON_SESSION", False), 1, 5)
    assert "last_candle" not in text and "vp_value_area_pct" not in text
    assert "LIVE PRICE SENT TO GEMINI" in text or "LAST PRICE SENT TO GEMINI" in text
    assert '"session_tz"' not in text


def test_system_prompt_has_v3_rules():
    # Minimal cfg for the template
    cfg = {
        "general": {"timeframe_minutes": 15},
        "risk": {"min_rr": 0, "max_entry_distance_atr": 1, "min_stop_distance_atr": 0.2, "max_risk_pct": 1.0},
        "chart": {
            "background": "#0E1117", "visible_candles": 100, "volume_pane_pct": 11,
            "vwap": {"color": "#FF9800"},
            "atr_channel": {"mult": 2.0, "atr_length": 14, "baseline_color": "#4B5563",
                            "upper_color": "#00E676", "lower_color": "#FF1744"},
            "sr": {"resistance_color": "#E91E63", "support_color": "#7C4DFF", "pivot_lookback": 12},
            "ema50": {"color": "#9CA3AF"},
        },
    }
    text = system_prompt(cfg, ["XAUUSD", "EURUSD", "GBPUSD", "US100", "BTCUSD"])
    assert "Mentally place the stop" not in text
    assert "For BUY: stop_loss < entry < take_profit" not in text
    assert "set stop_loss = 0, take_profit = 0, risk_reward = 0" in text
    assert "rationale` FIRST" in text or "rationale FIRST" in text or "in `rationale` FIRST" in text
    assert "session_blocks_automation is true" in text
    assert "high-win-rate 1–3 bar" in text or "high-win-rate 1-3 bar" in text
    assert ("live_price / live_price_ts_utc" in text) or ("last_price is not a live intrabar tick" in text)
    assert "vp_value_area_pct = 0.70" in text
    assert "trend_vs_vwap, volatility, level_event, rationale, then action" in text
