"""Unit tests for session Volume Profile (POC / contiguous VAH-VAL)."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.indicators import volume_profile, session_volume_profile, build_payload, session_key


def _bars(prices_hlc, vols, start="2026-11-09 00:00", freq="15min", tz="UTC"):
    idx = pd.date_range(start, periods=len(prices_hlc), freq=freq, tz=tz)
    rows = []
    for (h, l, c), v in zip(prices_hlc, vols):
        o = (h + l) / 2
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
    return pd.DataFrame(rows, index=idx)


def test_empty_and_degenerate():
    empty = volume_profile(pd.DataFrame())
    assert empty["poc"] == 0.0 and empty["bars"] == 0

    # single flat price
    df = _bars([(100, 100, 100)] * 3, [10, 20, 30])
    out = volume_profile(df, digits=2)
    assert out["poc"] == out["vah"] == out["val"] == 100.0
    assert out["bars"] == 3


def test_poc_is_highest_volume_zone():
    # Cluster most volume around 105 via overlapping ranges
    hlc = [
        (101, 99, 100),
        (102, 100, 101),
        (106, 104, 105),  # high vol zone
        (106.5, 104.5, 105.2),
        (107, 104, 105.5),
        (103, 101, 102),
        (110, 108, 109),  # sparse high
    ]
    vols = [5, 5, 40, 50, 45, 8, 3]
    df = _bars(hlc, vols)
    out = volume_profile(df, price_bins=40, value_area_pct=0.70, digits=2)
    assert 104.0 <= out["poc"] <= 106.5, out
    assert out["val"] <= out["poc"] <= out["vah"]
    assert out["value_area_pct_achieved"] >= 0.70 - 1e-9


def test_value_area_is_contiguous_and_includes_poc():
    """The sorted-cumsum snippet can yield non-contiguous bins and drop POC.
    Contiguous expansion from POC must keep VAL <= POC <= VAH and achieve ~70%."""
    rng = np.random.default_rng(42)
    # Two separated clusters: mid + far high. Mid has more volume -> POC there.
    n = 60
    lows = np.concatenate([rng.uniform(100, 102, 40), rng.uniform(120, 122, 20)])
    highs = lows + rng.uniform(0.5, 1.5, n)
    closes = (lows + highs) / 2
    vols = np.concatenate([rng.integers(20, 60, 40), rng.integers(5, 15, 20)]).astype(float)
    df = _bars(list(zip(highs, lows, closes)), vols)
    out = volume_profile(df, price_bins=50, value_area_pct=0.70, digits=2)
    assert out["val"] <= out["poc"] <= out["vah"]
    assert out["value_area_pct_achieved"] >= 0.70 - 1e-9
    # POC should sit in the dense 100-104 region, not the sparse 120s
    assert out["poc"] < 110, out


def test_range_distribution_not_just_close():
    """A bar with wide range and high volume should paint bins across [low, high],
    not dump everything on the close bin."""
    # One wide bar covering 100-110, close at 110, huge volume.
    # Plus a tiny bar at 110. If we only used close, POC would be ~110;
    # with range distribution, volume spreads and POC moves toward mid-range.
    df_close_bias = _bars(
        [(110, 100, 110), (110.2, 109.8, 110)],
        [1000, 10],
    )
    out = volume_profile(df_close_bias, price_bins=20, digits=2)
    # mid of [100,110] is 105; POC should not be glued to 110
    assert out["poc"] < 109.0, out
    assert out["val"] < out["vah"]


def test_fx_digits_precision():
    df = _bars(
        [(1.10500, 1.10000, 1.10250), (1.10600, 1.10100, 1.10300),
         (1.10450, 1.10150, 1.10280)],
        [100, 120, 90],
    )
    out = volume_profile(df, price_bins=30, digits=5)
    # rounded to 5 decimals (not 2)
    for k in ("poc", "vah", "val"):
        s = f"{out[k]:.10f}".rstrip("0")
        # at most 5 decimals after point
        decimals = len(s.split(".")[1]) if "." in s else 0
        assert decimals <= 5, (k, out[k])
    assert out["val"] <= out["poc"] <= out["vah"]


def test_session_filter_utc_day_and_globex():
    # Two UTC days of data; session_volume_profile must use only the last day
    idx = pd.date_range("2026-11-08 20:00", periods=20, freq="15min", tz="UTC")
    df = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 10.0,
    }, index=idx)
    # bump volume on Nov 9 bars so POC is identifiable
    df.loc[df.index.normalize() == pd.Timestamp("2026-11-09", tz="UTC"), "volume"] = 50.0
    out = session_volume_profile(df, "utc_day", digits=2)
    assert out["session_anchor"] == "utc_day"
    assert out["session_bars"] == int((df.index.normalize() == pd.Timestamp("2026-11-09", tz="UTC")).sum())
    assert out["bars"] == out["session_bars"]

    # Globex: session resets 18:00 ET (= 23:00 UTC in November EST)
    out_g = session_volume_profile(df, "cme_globex", digits=2)
    assert out_g["session_anchor"] == "cme_globex"
    assert out_g["session_bars"] >= 1


def test_build_payload_injects_vp_context():
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
        },
        "symbols": {"EURUSD": {"digits": 5, "vwap_anchor": "utc_day"}},
    }
    idx = pd.date_range("2026-11-09 00:00", periods=80, freq="15min", tz="UTC")
    rng = np.random.default_rng(0)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0003, len(idx)))
    df = pd.DataFrame({
        "open": close, "high": close + 0.0004, "low": close - 0.0004,
        "close": close, "volume": rng.integers(50, 200, len(idx)).astype(float),
    }, index=idx)
    payload = build_payload("EURUSD", df, cfg, "dukascopy", "eurusd")
    ctx = payload["context"]
    assert "poc" in ctx and "vah" in ctx and "val" in ctx
    assert ctx["val"] <= ctx["poc"] <= ctx["vah"]
    assert ctx["vp_session_anchor"] == "utc_day"
    assert ctx["vp_session_bars"] >= 1
    assert ctx["price_vs_value_area"] in {"inside", "above_vah", "below_val"}
    assert ctx["close_vs_poc"] in {"above", "below", "at"}
    # FX precision: 5 decimals
    for k in ("poc", "vah", "val"):
        assert round(ctx[k], 5) == ctx[k]


def test_va_not_noncontiguous_trap():
    """Construct a profile where sorted-by-volume would pick distant bins.
    Contiguous expansion must keep a connected [VAL, VAH]."""
    # Three isolated price pockets: 100, 150 (POC), 200. Mid has most volume.
    hlc, vols = [], []
    for _ in range(10):
        hlc.append((101, 99, 100)); vols.append(5)
    for _ in range(30):
        hlc.append((151, 149, 150)); vols.append(20)
    for _ in range(10):
        hlc.append((201, 199, 200)); vols.append(5)
    df = _bars(hlc, vols)
    out = volume_profile(df, price_bins=60, value_area_pct=0.70, digits=2)
    assert 148 <= out["poc"] <= 152, out
    # Contiguous VA around 150 should NOT stretch all the way to 100 and 200
    # once 70% is satisfied by the dense mid cluster alone.
    assert out["vah"] - out["val"] < 30, out
    assert out["val"] <= out["poc"] <= out["vah"]
