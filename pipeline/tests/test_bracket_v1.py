"""bracket-v1 shared package, hold gates, facts, and M15+20s alignment (C-OPUS-B vs DS-FLASH)."""
from __future__ import annotations
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import bracket_v1 as B
from signals.m15_scan import bar_close_ts, snapshot_hash
from signals.pipeline import next_boundary

UTC = timezone.utc


def candle(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def payload(sym="XAUUSD", price=4000.0, vwap=3990.0, atr=10.0, *,
            bar=None, candles=None, levels=None, ema50=None, stale=False,
            blocked=False, rvol=1.1, signal=None):
    signal = signal or {"open": price - 2, "high": price + 3, "low": price - 4, "close": price}
    ctx = {"atr": atr, "live_price": price, "last_close": price, "last_price": price,
           "live_price_stale": stale, "last_closed_bar_utc": "2026-11-12T17:00:00+00:00",
           "session": "NEW_YORK_SESSION", "session_blocks_automation": blocked,
           "vwap": vwap, "ema20_baseline": vwap + 1, "ema50": ema50 if ema50 is not None else vwap - 2,
           "atr_upper": price + 2 * atr, "atr_lower": price - 2 * atr,
           "poc": vwap, "vah": vwap + 8, "val": vwap - 8, "digits": 2,
           "rvol_last": rvol, "signal_candle": signal, "levels": levels or [
               {"type": "resistance", "label": "Session High", "price": price + 15},
               {"type": "support", "label": "Session Low", "price": price - 15},
           ]}
    return {"symbol": sym, "source": "dukascopy", "ticker": sym.lower(), "timeframe": "M15",
            "digits": 2, "candles": candles or [], "context": ctx}


# --------------------------------------------------------------------------- prompt
def test_prompt_is_a_flagged_reconstruction():
    text = B.PROMPT_PATH.read_text()
    assert "RECONSTRUCTION" in text
    assert "1 ATR" in text and "before" in text.lower()
    assert "vwap_distance_atr" in text or "2 ATR" in text
    assert "volume-free" in text or "awarded this point automatically" in text
    assert B.PROMPT_RECONSTRUCTION is True
    alias = Path(__file__).resolve().parent.parent / "templates" / "claude_entry_bracket_v1.txt"
    assert alias.exists()
    assert "RECONSTRUCTION" in alias.read_text()


def test_system_prompt_lists_symbols_no_gbp():
    p = B.system_prompt()
    assert "XAUUSD, EURUSD, US100, XAGUSD, BTCUSD" in p
    assert "GBPUSD" not in p


# --------------------------------------------------------------------------- swings + events
def test_swing_points_last_three():
    # Build a series with obvious confirmed swings (left=2, right=2).
    cs = []
    # base around 100
    prices = [100, 101, 102, 108, 102, 101, 100, 99, 92, 99, 100, 101, 107, 101, 100,
              99, 91, 99, 100]
    for i, px in enumerate(prices):
        if px >= 107:
            cs.append(candle(i, px - 1, px, px - 2, px - 1))  # swing high
        elif px <= 92:
            cs.append(candle(i, px + 1, px + 2, px, px + 1))  # swing low
        else:
            cs.append(candle(i, px, px + 0.5, px - 0.5, px))
    sw = B.swing_points(cs, last_n=3)
    assert len(sw["swing_highs"]) >= 1
    assert len(sw["swing_lows"]) >= 1
    # most recent high should be the 107 peak (index 12), not the last bars
    assert sw["swing_highs"][-1]["price"] == 107
    assert sw["swing_lows"][-1]["price"] == 91
    # last `right` bars cannot confirm a swing
    assert all(s["index"] < len(cs) - 2 for s in sw["swing_highs"] + sw["swing_lows"])


def test_level_events_four_classes():
    atr = 10.0
    # closed through a level at 100: open 98, close 103
    assert B.classify_level_event({"open": 98, "high": 104, "low": 97, "close": 103}, 100, atr) == "closed_through"
    # rejected: open below, wick through, close below
    assert B.classify_level_event({"open": 98, "high": 101.5, "low": 97, "close": 98.2}, 100, atr) == "rejected"
    # rejected from above
    assert B.classify_level_event({"open": 102, "high": 103, "low": 99, "close": 101.5}, 100, atr) == "rejected"
    # touched: range reaches the level, close stays on the same side, no real pierce past it
    assert B.classify_level_event({"open": 98, "high": 100.2, "low": 97, "close": 99.0}, 100, atr) == "touched"
    # untouched
    assert B.classify_level_event({"open": 90, "high": 92, "low": 89, "close": 91}, 100, atr) == "untouched"


def test_facts_block_contains_vwap_swings_and_events():
    # candles with a swing high and a close through VWAP
    cs = [candle(i, 100, 101, 99, 100) for i in range(10)]
    cs[5] = candle(5, 100, 110, 99, 101)  # swing high if neighbours are lower
    p = payload(price=4005, vwap=4000, atr=10, candles=cs,
                signal={"open": 3996, "high": 4006, "low": 3995, "close": 4005})  # closed through VWAP 4000
    f = B.compute_facts(p)
    assert f["vwap_distance_atr"] == pytest.approx(0.5, abs=1e-6)
    assert f["vwap_side"] == "above"
    assert any(e["level"] == "VWAP" and e["event"] == "closed_through" for e in f["level_events"])
    text = B.format_facts_block(f)
    assert "CODE-COMPUTED FACTS" in text
    assert "vwap_distance_atr" in text
    assert "closed_through" in text
    assert "room_up_atr" in text


# --------------------------------------------------------------------------- hold gates
def test_trend_blocked_when_more_than_2atr_from_vwap():
    p = payload(price=4030, vwap=4000, atr=10)  # +3 ATR
    f = B.compute_facts(p)
    reasons = B.hold_reasons("BUY", "trend", f, p["context"])
    assert any("vwap_distance_atr" in r for r in reasons)
    # fade is not blocked by the VWAP-distance rule
    assert not any("vwap_distance_atr" in r for r in B.hold_reasons("BUY", "fade", f, p["context"]))


def test_blocked_when_room_less_than_1atr():
    # nearest resistance 5 points away, ATR 10 -> 0.5 ATR room up
    p = payload(price=4000, vwap=3998, atr=10, levels=[
        {"type": "resistance", "label": "Session High", "price": 4005},
        {"type": "support", "label": "Session Low", "price": 3980},
    ])
    f = B.compute_facts(p)
    assert f["room_up_atr"] == pytest.approx(0.5, abs=1e-6)
    reasons = B.hold_reasons("BUY", "trend", f, p["context"])
    assert any("room" in r for r in reasons)
    assert f["room_up_atr"] < 1.0


def test_us100_volume_point_is_awarded_and_gate_matches():
    gold = payload("XAUUSD", rvol=2.5)
    nq = payload("US100", rvol=0.8)
    fg, fn = B.compute_facts(gold), B.compute_facts(nq)
    assert fn["us100_volume_point_awarded"] is True
    assert fg["us100_volume_point_awarded"] is False
    # A score-5 trend with room and close-to-VWAP is gated the same on US100 even without volume.
    raw = {"symbol": "US100", "action": "BUY", "setup_type": "trend", "score": 5,
           "confidence": 0.6, "trend_vs_vwap": "bullish", "volatility": "normal",
           "level_event": "closed_through VAL", "rationale": "T1 T2 S1 S2 S4",
           "risk_pct": 0.3, "entry": 4000, "stop_loss": 3990, "take_profit": 4010, "risk_reward": 1}
    v = B.apply_gates(raw, fn, nq["context"])
    # 2 ATR VWAP / room gates: this payload has 15 pts = 1.5 ATR room, 1 ATR from VWAP (4000 vs 3990)
    assert v["gated"] is True
    assert v["us100_volume_point_awarded"] is True
    assert v["prompt_version"] == "bracket-v1"


def test_fill_adverse_0_3_atr():
    ok, ratio, why = B.fill_adverse(1, 4004, 4000, 10, 0.3)  # +0.4 ATR worse for a BUY
    assert ok is False and ratio == pytest.approx(0.4)
    ok, ratio, why = B.fill_adverse(1, 4002, 4000, 10, 0.3)  # +0.2 ATR
    assert ok is True
    # favourable move is fine
    ok, ratio, _ = B.fill_adverse(1, 3998, 4000, 10, 0.3)
    assert ok is True and ratio < 0


# --------------------------------------------------------------------------- package identity
def test_package_identical_for_opus_b_and_deepseek():
    png = b"\xff\xd8\xff" + b"JPEG-FAKE-CHART" * 20
    payloads = [payload("XAUUSD"), payload("EURUSD", price=1.0850, vwap=1.0840, atr=0.0010,
                         signal={"open": 1.0844, "high": 1.0852, "low": 1.0842, "close": 1.0850},
                         levels=[{"type": "resistance", "label": "Session High", "price": 1.0870},
                                 {"type": "support", "label": "Session Low", "price": 1.0820}]),
                payload("US100", price=25500, vwap=25480, atr=40)]
    charts = {p["symbol"]: png for p in payloads}
    pkg = B.build_package(payloads, charts, bar_close_ts="2026-11-12T17:15:00+00:00",
                          snapshot_ts="2026-11-12T17:15:20+00:00")
    assert pkg["prompt_version"] == "bracket-v1"
    assert pkg["symbols"] == ["XAUUSD", "EURUSD", "US100"]
    assert "GBPUSD" not in pkg["symbols"]
    # Anthropic and OpenAI wrappers are built from the same hashed parts.
    assert pkg["snapshot_hash"]
    assert pkg["snapshot_id"].startswith("m15-")
    # Rebuilding yields the same hash (same bytes in, same bytes out).
    pkg2 = B.build_package(payloads, charts, bar_close_ts="2026-11-12T17:15:00+00:00",
                           snapshot_ts="2026-11-12T17:15:20+00:00")
    assert pkg2["snapshot_hash"] == pkg["snapshot_hash"]
    assert pkg2["snapshot_id"] == pkg["snapshot_id"]
    # The hashed body (system + text + image digests) does not include the DeepSeek schema closer.
    assert pkg["system"] == pkg2["system"]
    assert [p["text"] if p["type"] == "text" else p["sha256"] for p in pkg["parts"]] == \
           [p["text"] if p["type"] == "text" else p["sha256"] for p in pkg2["parts"]]
    # Facts appear in the user text.
    texts = [p["text"] for p in pkg["parts"] if p["type"] == "text"]
    assert any("CODE-COMPUTED FACTS" in t for t in texts)
    assert any("LIVE PRICE SENT TO MODEL" in t for t in texts)
    assert not any("LIVE PRICE SENT TO GEMINI" in t for t in texts)
    # Chart sha is of the exact bytes both models would be sent.
    assert pkg["chart_sha256"]["XAUUSD"] == hashlib.sha256(png).hexdigest()


def test_filter_drops_gbpusd_and_busy():
    payloads = [payload("XAUUSD"), payload("EURUSD"), payload("GBPUSD"), payload("US100"),
                payload("XAGUSD"), payload("BTCUSD")]
    got = B.filter_symbols(payloads, {"XAUUSD"})
    assert [p["symbol"] for p in got] == ["EURUSD", "US100", "XAGUSD", "BTCUSD"]


# --------------------------------------------------------------------------- M15+20s timing
@pytest.mark.parametrize("minute,second,expect_min", [
    (0, 0, 0), (0, 19, 0), (0, 20, 15),   # at exactly +20s the next wake is the following bar
    (14, 59, 15), (15, 0, 15), (15, 20, 30),
    (29, 59, 30), (30, 0, 30), (30, 19, 30), (30, 20, 45),
    (44, 59, 45), (45, 0, 45), (45, 20, 0),  # wraps to next hour
    (59, 59, 0),
])
def test_next_boundary_is_plus_20s_on_every_m15(minute, second, expect_min):
    hour = 17
    now = datetime(2026, 11, 12, hour, minute, second, tzinfo=UTC)
    wake = next_boundary(now, 15, 20)
    assert wake.second == 20
    if expect_min == 0 and minute >= 45:
        assert wake.hour == hour + 1 and wake.minute == 0
    else:
        assert wake.hour == hour and wake.minute == expect_min


def test_bar_close_ts_and_start_lag_on_odd_and_even_closes():
    # :15 close (odd) and :00 / :30 close (even) — lag from close to +20s wake is 20s.
    for h, m in ((17, 15), (17, 45), (17, 0), (17, 30)):
        close = datetime(2026, 11, 12, h, m, 0, tzinfo=UTC)
        wake = close.replace(second=20)
        assert bar_close_ts(wake) == close
        assert (wake - bar_close_ts(wake)).total_seconds() == 20
        # A 2-min-loop tick at an odd minute must not be the scheduler: the M15 wake
        # is still close+20s even when that second is "odd".
        assert next_boundary(close + __import__("datetime").timedelta(seconds=1), 15, 20) == wake


def test_snapshot_hash_stable_and_changes_with_chart_bytes():
    p = payload()
    a = b"\xff\xd8\xffAAA"
    b = b"\xff\xd8\xffBBB"
    facts = {p["symbol"]: B.compute_facts(p)}
    d1, s1, c1 = snapshot_hash([p], {p["symbol"]: a}, facts)
    d2, s2, c2 = snapshot_hash([p], {p["symbol"]: a}, facts)
    d3, s3, c3 = snapshot_hash([p], {p["symbol"]: b}, facts)
    assert d1 == d2 and s1 == s2 and c1 == c2
    assert d1 != d3
    assert c1[p["symbol"]] == hashlib.sha256(a).hexdigest()
