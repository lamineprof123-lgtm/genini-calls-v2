"""Live M1 quote, forming M15 bar, stale fallback, and live entry for scalp / validator."""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.live_price import (
    live_from_m1, fallback_live, apply_live_fields, forming_m15_open, minutes_into_bar,
)
from signals.scalp_levels import apply_server_levels
from signals.validator import validate_signal
from signals.gemini_client import slim_gemini_context, chart_user_text, system_prompt


def _m1(start, rows, freq="1min"):
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    o, h, l, c, v = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def test_live_from_m1_builds_forming_bar_and_minutes():
    # Forming M15 opened at 08:15; last M1 at 08:21 (in progress relative to now=08:21:30)
    rows = []
    # 08:15..08:21 inclusive = 7 minutes
    px = 1.1000
    for i in range(7):
        o = px + i * 0.0001
        rows.append((o, o + 0.0003, o - 0.0002, o + 0.0001, 100))
    df = _m1("2026-11-11 08:15:00", rows)
    now = datetime(2026, 11, 11, 8, 21, 30, 123456, tzinfo=timezone.utc)
    live = live_from_m1(df, tf_min=15, now=now, digits=5, source="dukascopy:eurusd:m1")
    assert live["live_price_stale"] is False
    assert live["forming_m15_open"] == pytest.approx(1.1000)
    assert live["forming_m15_high"] == pytest.approx(1.1000 + 6 * 0.0001 + 0.0003)
    assert live["forming_m15_low"] == pytest.approx(1.1000 - 0.0002)
    assert live["live_price"] == pytest.approx(1.1000 + 6 * 0.0001 + 0.0001)
    assert live["forming_m15_open_utc"].startswith("2026-11-11T08:15:00")
    assert live["minutes_into_bar"] == pytest.approx(6.5, abs=0.02)  # 6 min 30s into the 08:15 bar
    assert "2026-11-11T08:21:30.123456+00:00" == live["live_price_fetched_at_utc"]
    # last M1 08:21 has not closed at 08:21:30, so ts is 'now'
    assert live["live_price_ts_utc"].startswith("2026-11-11T08:21:30")


def test_minutes_into_bar_and_open():
    now = datetime(2026, 11, 11, 8, 16, 0, tzinfo=timezone.utc)
    assert forming_m15_open(now, 15) == pd.Timestamp("2026-11-11 08:15:00", tz="UTC")
    assert minutes_into_bar(now, 15) == 1.0


def test_fallback_marks_stale_and_copies_last_close():
    now = datetime(2026, 11, 11, 8, 21, 30, 654321, tzinfo=timezone.utc)
    ctx = {"last_close": 1.11476, "digits": 5, "last_closed_bar_utc": "2026-11-11T08:00:00+00:00",
           "signal_candle": {"open": 1.11, "high": 1.12, "low": 1.10, "close": 1.11476}}
    live = fallback_live(ctx, now=now, tf_min=15, reason="dukascopy timeout")
    assert live["live_price_stale"] is True
    assert live["live_price"] == 1.11476
    assert live["forming_m15_open"] is None
    assert live["live_price_ts_utc"] == "2026-11-11T08:00:00+00:00"
    assert live["live_price_fetched_at_utc"] == "2026-11-11T08:21:30.654321+00:00"
    assert "fallback_last_close" in live["live_price_source"]


def test_apply_live_fields_sets_forming_candle_and_last_price_alias():
    payload = {"symbol": "EURUSD", "context": {"last_close": 1.10, "last_price": 1.10}, "candles": []}
    now = datetime(2026, 11, 11, 8, 21, 30, tzinfo=timezone.utc)
    live = {
        "live_price": 1.10125, "live_price_ts_utc": now.isoformat(), "live_price_stale": False,
        "live_price_source": "dukascopy:eurusd:m1", "live_price_fetched_at_utc": now.isoformat(),
        "forming_m15_open": 1.10000, "forming_m15_high": 1.10150, "forming_m15_low": 1.09980,
        "forming_m15_open_utc": "2026-11-11T08:15:00+00:00", "minutes_into_bar": 6.5,
    }
    apply_live_fields(payload, live)
    assert payload["context"]["last_price"] == 1.10125
    assert payload["forming_candle"]["time"] == int(pd.Timestamp("2026-11-11T08:15:00+00:00").timestamp())
    assert payload["forming_candle"]["close"] == 1.10125
    assert payload["forming_candle"]["high"] == 1.10150
    # stale: no forming candle
    payload2 = {"symbol": "EURUSD", "context": {}, "candles": []}
    apply_live_fields(payload2, fallback_live({"last_close": 1.1, "digits": 5}, now=now))
    assert payload2["forming_candle"] is None
    assert payload2["context"]["live_price_stale"] is True


def test_scalp_and_validator_use_live_price_as_entry():
    ctx = {
        "last_close": 1.10000, "live_price": 1.10040, "live_price_stale": False,
        "atr": 0.00120, "digits": 5,
        "signal_candle": {"open": 1.10010, "high": 1.10080, "low": 1.09940, "close": 1.10000},
        "levels": [], "poc": 1.1000, "vah": 1.1010, "val": 1.0990,
    }
    cfg = {"scalp": {"enabled": True, "atr_stop_mult": 0.8, "min_stop_atr": 0.25,
                     "structure_buffer_atr": 0.05, "fixed_target": {"EURUSD": {"pips": 12.5, "pip_size": 0.0001}}},
           "risk": {"min_rr": 1.5, "min_confidence": 0.5, "max_entry_distance_atr": 0.1,
                    "min_stop_distance_atr": 0.2, "default_risk_pct": 0.5, "max_risk_pct": 1.0},
           "symbols": {"EURUSD": {"contract_size": 100000, "digits": 5}}}
    # Gemini might still send last_close as entry; server overwrites with live_price
    sig = {"symbol": "EURUSD", "action": "BUY", "entry": 1.10000,
           "stop_loss": 0, "take_profit": 0, "risk_reward": 0,
           "confidence": 0.8, "trend_vs_vwap": "bullish", "volatility": "normal",
           "level_event": "none", "rationale": "ok"}
    apply_server_levels(sig, ctx, cfg, "EURUSD")
    assert sig["entry"] == pytest.approx(1.10040)
    assert sig["stop_loss"] < sig["entry"] < sig["take_profit"]
    v = validate_signal(sig, "EURUSD", ctx, cfg, equity=100000)
    assert v["action"] == "BUY" and not v["downgraded"]

    # If we compared entry to last_close with a tight max_entry_distance, 0.40 / 0.00120 ≈ 0.33 ATR
    # would fail the 0.1 ATR cap. Using live_price as the market ref must accept it.
    tight = validate_signal(sig, "EURUSD", ctx, cfg, equity=100000)
    assert tight["action"] == "BUY"


def test_stale_live_does_not_override_entry():
    ctx = {
        "last_close": 1.10000, "live_price": 1.10000, "live_price_stale": True,
        "atr": 0.00120, "digits": 5,
        "signal_candle": {"open": 1.1, "high": 1.101, "low": 1.099, "close": 1.10000},
        "levels": [],
    }
    cfg = {"scalp": {"enabled": True, "atr_stop_mult": 0.8, "min_stop_atr": 0.25,
                     "structure_buffer_atr": 0.05, "fixed_target": {"EURUSD": {"pips": 12.5, "pip_size": 0.0001}}}}
    sig = {"action": "BUY", "entry": 1.10000, "stop_loss": 0, "take_profit": 0}
    apply_server_levels(sig, ctx, cfg, "EURUSD")
    assert sig["entry"] == pytest.approx(1.10000)


def test_slim_context_keeps_live_fields_and_prompt_text():
    ctx = {
        "last_close": 1.10, "last_candle": {"open": 1}, "vp_value_area_pct": 0.7,
        "session_tz": "America/New_York", "last_price": 1.10125,
        "live_price": 1.10125, "live_price_ts_utc": "2026-11-11T08:21:30.123456+00:00",
        "live_price_stale": False, "forming_m15_open": 1.1, "forming_m15_high": 1.102,
        "forming_m15_low": 1.099, "minutes_into_bar": 6.5, "last_closed_bar_utc": "2026-11-11T08:00:00+00:00",
        "signal_candle": {"open": 1.1, "high": 1.11, "low": 1.09, "close": 1.10},
    }
    slim = slim_gemini_context(ctx)
    assert "last_close" not in slim and "live_price" in slim
    assert "forming_m15_high" in slim and "minutes_into_bar" in slim
    text = chart_user_text({"symbol": "EURUSD", "source": "dukascopy", "ticker": "eurusd",
                            "timeframe": "M15", "context": ctx}, 1, 5)
    assert "LIVE PRICE SENT TO GEMINI" in text
    assert "STALE" not in text
    stale_ctx = dict(ctx, live_price_stale=True)
    stale = chart_user_text({"symbol": "EURUSD", "source": "dukascopy", "ticker": "eurusd",
                             "timeframe": "M15", "context": stale_ctx}, 1, 5)
    assert "STALE" in stale


def test_prompt_explains_live_vs_closed():
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
    assert "set entry = live_price" in text
    assert "intrabar pullback or rejection" in text
    assert "live_price_stale is true" in text
    assert "signal_candle is the last CLOSED" in text
    assert "not a live intrabar tick" not in text  # v3 wording replaced
