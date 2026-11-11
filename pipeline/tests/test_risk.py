import json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.risk import DailyDrawdownBreaker
from signals.validator import validate_batch, validate_signal

T0 = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)  # 10:00 Dubai


def test_no_halt_within_limit(tmp_path):
    b = DailyDrawdownBreaker(tmp_path / "cb.json", 3.5, "Asia/Dubai")
    assert not b.check(100_000, T0).halted
    s = b.check(96_500, T0 + timedelta(hours=1))  # exactly 3.5% -> not "exceeds"
    assert s.loss_pct == 3.5 and not s.halted


def test_halt_above_limit_and_latch(tmp_path):
    p = tmp_path / "cb.json"
    b = DailyDrawdownBreaker(p, 3.5, "Asia/Dubai")
    b.check(100_000, T0)
    s = b.check(96_400, T0 + timedelta(hours=1))  # -3.6%
    assert s.halted and "3.60%" in s.reason
    # equity recovers -> still halted (latched) for the rest of the session
    assert b.check(101_000, T0 + timedelta(hours=2)).halted
    # survives a restart (state on disk)
    assert DailyDrawdownBreaker(p, 3.5, "Asia/Dubai").check(101_000, T0 + timedelta(hours=3)).halted
    assert json.loads(p.read_text())["halted"] is True


def test_resets_next_session_day(tmp_path):
    b = DailyDrawdownBreaker(tmp_path / "cb.json", 3.5, "Asia/Dubai")
    b.check(100_000, T0); assert b.check(90_000, T0 + timedelta(hours=1)).halted
    nxt = b.check(90_000, T0 + timedelta(days=1))  # new Dubai day: start equity re-snapshotted
    assert not nxt.halted and nxt.start_equity == 90_000


def test_breaker_overrides_all_signals_to_hold():
    cfg = load_config()
    ctx = {"last_close": 100.0, "atr": 1.0}
    good = {"symbol": "XAUUSD", "action": "BUY", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0,
            "confidence": 0.8, "risk_pct": 0.5}
    out = validate_batch({"signals": [good]}, {"XAUUSD": ctx}, cfg, 100_000, False)
    assert out[0]["action"] == "BUY" and out[0]["rr_local"] == 3.0 and out[0]["position_lots"] == 5.0
    out = validate_batch({"signals": [good]}, {"XAUUSD": ctx}, cfg, 100_000, True, "daily loss 3.6% > 3.5%")
    assert out[0]["action"] == "HOLD" and out[0]["downgraded"] and "CIRCUIT BREAKER" in out[0]["reasons"][0]


def test_validator_rules():
    cfg = load_config()
    ctx = {"last_close": 100.0, "atr": 1.0}
    base = {"symbol": "EURUSD", "entry": 100.0, "confidence": 0.8}
    # Scalp mode: default min_rr=0. Explicit override still enforces a floor.
    v = lambda **k: validate_signal({**base, **k}, "EURUSD", ctx, cfg, 100_000)
    assert v(action="BUY", stop_loss=99, take_profit=101.5)["action"] == "BUY"         # low R:R allowed
    assert v(action="BUY", stop_loss=101, take_profit=103)["action"] == "HOLD"         # SL wrong side
    assert v(action="SELL", stop_loss=101, take_profit=97)["action"] == "SELL"         # valid
    assert v(action="SELL", stop_loss=99, take_profit=97)["action"] == "HOLD"          # SL wrong side
    assert v(action="BUY", stop_loss=99, take_profit=103, entry=102.5)["action"] == "HOLD"  # entry far from price
    assert v(action="BUY", stop_loss=99, take_profit=103, confidence=0.3)["action"] == "HOLD"
    assert v(action="BUY", stop_loss=99, take_profit=103, confidence=80)["confidence"] == 0.8
    assert validate_signal(None, "EURUSD", ctx, cfg, 100_000)["action"] == "HOLD"
    # Legacy R:R floor still works when explicitly requested
    bad = validate_signal({**base, "action": "BUY", "stop_loss": 99, "take_profit": 101.5},
                          "EURUSD", ctx, cfg, 100_000, min_rr=2.5)
    assert bad["action"] == "HOLD" and "R:R" in bad["reasons"][0]
