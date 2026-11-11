"""Unit tests for approximate CVD and America/New_York session timing."""
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.indicators import (
    bar_delta, cumulative_delta, session_cumulative_delta, format_cvd_header,
    market_session, session_blocks_automation, build_payload,
)


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _bars(hlcv, start="2026-11-09 00:00", freq="15min", tz="UTC"):
    idx = pd.date_range(start, periods=len(hlcv), freq=freq, tz=tz)
    rows = []
    for h, l, c, v in hlcv:
        rows.append({"open": (h + l) / 2, "high": h, "low": l, "close": c, "volume": v})
    return pd.DataFrame(rows, index=idx)


def test_bar_delta_flat_is_zero_not_epsilon():
    # Flaw fixed: never divide by 0.0001
    assert bar_delta(100, 100, 100, 1000) == 0.0
    assert bar_delta(100, 100, 100, 0) == 0.0


def test_bar_delta_full_buy_and_sell():
    # Close at high => buying_pressure=1, selling=0 => delta = +volume
    assert bar_delta(110, 100, 110, 50) == pytest.approx(50.0)
    # Close at low => delta = -volume
    assert bar_delta(110, 100, 100, 50) == pytest.approx(-50.0)
    # Mid close => ~0
    assert abs(bar_delta(110, 100, 105, 50)) < 1e-9


def test_cumulative_delta_and_label():
    # Mostly closes near highs -> BULLISH
    hlcv = [(101, 99, 100.8, 100), (102, 100, 101.7, 120), (103, 101, 102.9, 150),
            (104, 102, 103.8, 130)]
    df = _bars(hlcv)
    out = cumulative_delta(df, neutral_pct=0.05, slope_bars=4)
    assert out["cvd"] > 0
    assert out["cvd_label"] == "BULLISH"
    assert out["bars"] == 4
    assert out["session_volume"] == 500
    # slope over all 4 bars == cvd when only 4 bars
    assert out["cvd_slope"] == out["cvd"]

    # Balanced small CVD vs volume -> NEUTRAL
    hlcv_n = [(101, 99, 100, 1000), (101, 99, 100, 1000)]
    out_n = cumulative_delta(_bars(hlcv_n), neutral_pct=0.05)
    assert out_n["cvd_label"] == "NEUTRAL"


def test_format_cvd_header():
    assert format_cvd_header(14250, "BULLISH") == "CVD +14,250 (BULLISH)"
    assert format_cvd_header(-14250.4, "BEARISH") == "CVD -14,250 (BEARISH)"
    assert format_cvd_header(12.34, "NEUTRAL") == "CVD +12.34 (NEUTRAL)"


def test_session_windows_not_shadowing_new_york():
    """Flaw fixed: old snippet's 03-11 branch made NEW_YORK unreachable."""
    # 09:30 ET = LONDON_NY_OVERLAP
    ts = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)  # 09:30 EST
    assert market_session(ts) == "LONDON_NY_OVERLAP"
    # 11:30 ET = NEW_YORK_SESSION (must NOT be OVERLAP)
    ts2 = datetime(2026, 11, 9, 16, 30, tzinfo=UTC)  # 11:30 EST
    assert market_session(ts2) == "NEW_YORK_SESSION"
    # 04:00 ET = LONDON
    ts3 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)  # 04:00 EST
    assert market_session(ts3) == "LONDON_SESSION"
    # 19:00 ET = ASIAN
    ts4 = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)  # 19:00 EST Nov 9
    assert market_session(ts4) == "ASIAN_SESSION_CHOP"
    # 01:00 ET = ASIAN (wrap)
    ts5 = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)  # 01:00 EST
    assert market_session(ts5) == "ASIAN_SESSION_CHOP"
    # 02:30 ET = DEADZONE
    ts6 = datetime(2026, 11, 9, 7, 30, tzinfo=UTC)  # 02:30 EST
    assert market_session(ts6) == "LOW_LIQUIDITY_DEADZONE"
    # 17:00 ET = DEADZONE
    ts7 = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)  # 17:00 EST
    assert market_session(ts7) == "LOW_LIQUIDITY_DEADZONE"


def test_session_uses_bar_time_not_wall():
    # Pass a Timestamp explicitly; result must follow that bar, not datetime.now()
    bar = pd.Timestamp("2026-11-09 16:30:00", tz="UTC")  # 11:30 EST
    assert market_session(bar) == "NEW_YORK_SESSION"
    bar2 = pd.Timestamp("2026-11-10 00:15:00", tz="UTC")  # 19:15 EST
    assert market_session(bar2) == "ASIAN_SESSION_CHOP"


def test_session_blocks_automation():
    assert session_blocks_automation("ASIAN_SESSION_CHOP") is True
    assert session_blocks_automation("LOW_LIQUIDITY_DEADZONE") is True
    assert session_blocks_automation("NEW_YORK_SESSION") is False
    assert session_blocks_automation("LONDON_NY_OVERLAP") is False
    assert session_blocks_automation("LONDON_SESSION") is False


def test_session_cumulative_delta_filters_to_session():
    # Two UTC days; only Nov 9 should count under utc_day
    idx = pd.date_range("2026-11-08 20:00", periods=20, freq="15min", tz="UTC")
    df = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.8,
        "volume": 10.0,
    }, index=idx)
    # Nov 9 bars: bigger volume, closes near high
    mask = df.index.normalize() == pd.Timestamp("2026-11-09", tz="UTC")
    df.loc[mask, "volume"] = 50.0
    out = session_cumulative_delta(df, "utc_day", neutral_pct=0.05)
    assert out["session_anchor"] == "utc_day"
    assert out["session_bars"] == int(mask.sum())
    assert out["bars"] == out["session_bars"]
    assert out["cvd"] > 0
    assert out["cvd_label"] == "BULLISH"


def test_build_payload_injects_cvd_and_session():
    cfg = {
        "general": {"candles": 30, "timeframe_minutes": 15},
        "chart": {
            "visible_candles": 30, "width": 1280, "height": 720,
            "template": "chart_v2.html",
            "vwap": {"default_anchor": "utc_day", "color": "#FF9800", "width": 2},
            "atr_channel": {
                "ema_length": 20, "atr_length": 14, "mult": 2.0,
                "baseline_color": "#4B5563", "upper_color": "#00E676",
                "lower_color": "#FF1744", "fill_color": "#64B5F6", "fill_opacity": 0.08,
            },
            "sr": {
                "pivot_lookback": 5, "rolling_hours": 24, "dedupe_atr": 0.35,
                "max_per_side": 2, "resistance_color": "#E91E63",
                "support_color": "#7C4DFF", "line_width": 2, "line_style": "dashed",
            },
            "ema50": {"length": 50, "color": "#9CA3AF", "width": 1},
            "rvol": {"length": 20, "threshold": 2.0, "spike_color": "#FACC15",
                     "normal_color": "#374151"},
            "volume_profile": {"price_bins": 40, "value_area_pct": 0.70},
            "cvd": {"neutral_pct": 0.05, "slope_bars": 4},
            "session": {
                "london": ["03:00", "08:00"], "overlap": ["08:00", "11:00"],
                "new_york": ["11:00", "16:00"], "asian": ["18:00", "02:00"],
                "blocked_for_automation": ["ASIAN_SESSION_CHOP", "LOW_LIQUIDITY_DEADZONE"],
            },
        },
        "symbols": {"US100": {"digits": 2, "vwap_anchor": "cme_globex"}},
    }
    # Last bar at 16:30 UTC = 11:30 EST = NEW_YORK_SESSION
    idx = pd.date_range("2026-11-09 08:00", periods=40, freq="15min", tz="UTC")
    rng = np.random.default_rng(1)
    close = 25000 + np.cumsum(rng.normal(0, 5, len(idx)))
    df = pd.DataFrame({
        "open": close, "high": close + 8, "low": close - 8,
        "close": close + 3, "volume": rng.integers(80, 200, len(idx)).astype(float),
    }, index=idx)
    payload = build_payload("US100", df, cfg, "dukascopy", "usatechidxusd")
    ctx = payload["context"]
    assert "cvd" in ctx and "cvd_label" in ctx and "cvd_slope" in ctx
    assert ctx["cvd_label"] in {"BULLISH", "BEARISH", "NEUTRAL"}
    assert ctx["session"] == "NEW_YORK_SESSION"
    assert ctx["session_blocks_automation"] is False
    assert "CVD" in ctx["cvd_header"] and "(" in ctx["cvd_header"]
