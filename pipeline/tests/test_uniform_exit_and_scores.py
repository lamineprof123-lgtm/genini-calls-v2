"""t247u uniform 1 ATR bracket + t248u universal signal scorer."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.uniform_exit import (TIME_CAP_MINUTES, UNIFORM_EXIT_VERSION, arm_uniform, bracket_levels,
                                  excursion, first_touch, result_R, twin_side, uniform_exits_enabled)
from signals.signal_score import SignalScorer, conf_band, score_key

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)


def bars(start, closes, high=None, low=None):
    idx = pd.date_range(start, periods=len(closes), freq="1min", tz="UTC")
    h = high or [c + 1 for c in closes]
    l = low or [c - 1 for c in closes]
    return pd.DataFrame({"open": closes, "high": h, "low": l, "close": closes, "volume": 1.0}, index=idx)


def test_bracket_levels_and_stop_beats_target():
    lv = bracket_levels(1, 100.0, 10.0, tick=0.25)
    assert lv["stop"] == pytest.approx(90.0) and lv["target"] == pytest.approx(110.0)
    # one bar trades both stop and target: stop wins
    df = bars(T0, [100], high=[111], low=[89])
    hit = first_touch(df, side=1, entry=100, stop=lv["stop"], target=lv["target"], entry_time=T0, tick=0.25, slippage_ticks=0)
    assert hit["reason"] == "stop_1atr" and hit["price"] == pytest.approx(90.0)


def test_first_touch_target_and_time_cap():
    lv = bracket_levels(-1, 100.0, 10.0, tick=0.25)
    df = bars(T0, [100, 99, 90], high=[101, 100, 91], low=[99, 98, 89])
    hit = first_touch(df, side=-1, entry=100, stop=lv["stop"], target=lv["target"], entry_time=T0, tick=0.25, slippage_ticks=0)
    assert hit["reason"] == "target_1atr" and hit["price"] == pytest.approx(90.0)
    # 90-min cap: price never reaches either level
    flat = bars(T0, [100] * (TIME_CAP_MINUTES + 2))
    cap = first_touch(flat, side=1, entry=100, stop=90, target=110, entry_time=T0, tick=0.25, slippage_ticks=0)
    assert cap["reason"] == f"max_hold_{TIME_CAP_MINUTES}m"


def test_late_arm_skips_earlier_bars():
    df = bars(T0, [100, 80, 80], high=[101, 81, 81], low=[99, 79, 79])
    armed = T0 + timedelta(minutes=2)
    hit = first_touch(df, side=1, entry=100, stop=90, target=110, entry_time=T0, armed_at=armed,
                      tick=0.25, slippage_ticks=0)
    # the 80 bar is at T0+1min, before late-arm ceil; should not stop out on it
    assert hit is None or hit["t"] >= armed


def test_twin_side_is_deterministic():
    a, s1 = twin_side("flash|XAUUSD|t|BUY|1|entry")
    b, s2 = twin_side("flash|XAUUSD|t|BUY|1|entry")
    assert a == b and s1 == s2 and a in (-1, 1)
    c, _ = twin_side("flash|EURUSD|t|BUY|1|entry")
    # different key may or may not flip; just ensure it returns a valid side
    assert c in (-1, 1)


def test_excursion_horizons():
    # 1h later +20, 2.5h later -15, 3.5h later +40
    closes = [100] * (4 * 60 + 2)
    highs = [100] * len(closes)
    lows = [100] * len(closes)
    highs[60] = 120
    lows[150] = 85
    highs[210] = 140
    df = bars(T0, closes, high=highs, low=lows)
    ex = excursion(df, side=1, entry=100, start=T0)
    assert ex["2h"]["mfe"] == pytest.approx(20)
    assert ex["3h"]["mae"] == pytest.approx(15)
    assert ex["4h"]["mfe"] == pytest.approx(40)
    assert ex["2h"]["time_to_mfe_s"] == 60 * 60


def test_uniform_flag_default_false_without_key():
    assert uniform_exits_enabled({}) is False
    assert uniform_exits_enabled({"exit_manager": {"uniform_1atr_exits": True}}) is True


def test_scorer_buy_and_twin_and_hold(tmp_path):
    cfg = {"general": {"state_dir": str(tmp_path / "state"), "logs_dir": str(tmp_path / "logs"),
                       "session_tz": "Asia/Dubai"},
           "site": {"dir": str(tmp_path)},
           "lucid": {"contracts": {"GC": {"point_value": 100.0, "tick": 0.1, "commission": 2.3}},
                     "symbols": {"XAUUSD": {"contracts": ["GC"]}}},
           "carbon_books": {}}
    # 4h+ of bars: long stop at 3900-8=3892 never hits; target 3908 hits at +20 min
    n = 4 * 60 + 10
    closes = [3900] * n
    highs = [3900.5] * n
    lows = [3899.5] * n
    highs[20] = 3910.0
    df = bars(T0, closes, high=highs, low=lows)

    async def m1(sym, start, end):
        return df

    sc = SignalScorer(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs",
                      site_dir=tmp_path, m1_fetch=m1)
    added = sc.enqueue({"model": "gemini-3.8-flash", "thinking_level": "high", "symbol": "XAUUSD",
                        "action": "BUY", "confidence": 0.8, "signal_at": T0.isoformat(),
                        "entry": 3900.0, "atr": 8.0, "session": "NEW_YORK_SESSION", "kind": "entry"})
    # signal + twin
    assert len(added) == 2 and {r["role"] for r in added} == {"signal", "twin"}
    # HOLD -> long + short + two twins
    h = sc.enqueue({"model": "gemini-3.8-flash", "thinking_level": "high", "symbol": "XAUUSD",
                    "action": "HOLD", "confidence": 0.4, "signal_at": T0.isoformat(),
                    "entry": 3900.0, "atr": 8.0, "kind": "entry"})
    assert {r["role"] for r in h} >= {"hold_long", "hold_short"}
    # pretend it's 5h later
    import signals.signal_score as ss
    real_now = ss.datetime
    class FakeDT:
        @staticmethod
        def now(tz=None):
            return T0 + timedelta(hours=5)
        min = real_now.datetime.min if False else None
    # simpler: call _score_one directly
    rec = sc._score_one(added[0], df)
    assert rec and rec["exit_mode"] == UNIFORM_EXIT_VERSION
    assert rec["bracket_reason"] == "target_1atr"
    assert rec["R"] is not None and rec["win"] is True
    assert rec["thinking_level"] == "high" and rec["conf_band"] == "0.75-0.90"
    assert rec["excursions"]["2h"]["mfe_atr"] is not None
    sc._append(rec)
    # twin of the same entry with opposite-or-same side still writes R
    rec2 = sc._score_one(added[1], df)
    assert rec2 and rec2["role"] == "twin" and rec2.get("twin_seed") is not None
    sc._append(rec2)
    summ = sc.summary()
    assert "gemini-3.8-flash" in summ["models"]
    m = summ["models"]["gemini-3.8-flash"]
    assert m["n"] == 1 and m["avg_R"] is not None
    assert (tmp_path / "logs" / "2026-11-12" / "signal_scores.jsonl").exists()


def test_scorer_ingest_cycles(tmp_path):
    logs = tmp_path / "logs" / "2026-11-12" / "cycles"
    logs.mkdir(parents=True)
    rec = {"run_at_utc": T0.isoformat(), "model": "gemini-3.8-flash", "thinking_level": "high",
           "signals": [{"symbol": "EURUSD", "action": "SELL", "confidence": 0.7, "entry": 1.15, "atr": 0.0005}],
           "data": {"EURUSD": {"atr": 0.0005, "live_price": 1.15, "session": "LONDON"}}}
    (logs / "140000.json").write_text(json.dumps(rec))
    cfg = {"general": {"state_dir": str(tmp_path / "state"), "logs_dir": str(tmp_path / "logs"),
                       "session_tz": "Asia/Dubai"},
           "site": {"dir": str(tmp_path)},
           "lucid": {"contracts": {"6E": {"point_value": 125000.0, "tick": 0.00005, "commission": 2.4}},
                     "symbols": {"EURUSD": {"contracts": ["6E"]}}},
           "carbon_books": {}}
    sc = SignalScorer(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path,
                      m1_fetch=lambda *a, **k: None)
    n = sc.ingest_logs()
    assert n >= 2  # signal + twin
    assert any(r["symbol"] == "EURUSD" and r["action"] == "SELL" for r in sc.s["queue"])
    # second ingest is idempotent
    assert sc.ingest_logs() == 0
