"""OTHER-VENUE CFD books + Lucid SI routing (t253u / t254u)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from signals.other_venue import (OtherVenueHub, FEE_RATE, RISK_USD, VENUE, fee_usd,
                                 is_other_venue_symbol, other_venue_symbols, size_qty,
                                 slip_px, spec_for)
from signals.uniform_exit import UNIFORM_EXIT_VERSION

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def _cfg(tmp: Path, symbols=None) -> dict:
    return {
        "general": {"state_dir": str(tmp / "state"), "logs_dir": str(tmp / "logs")},
        "site": {"dir": str(tmp)},
        "other_venue": {
            "enabled": True,
            "start_balance": 100000.0,
            "risk_usd": 900.0,
            "fee_rate": 0.0005,
            "slippage_ticks": 1,
            "symbols": list(symbols or ["BTCUSD"]),
            "books": ["MAIN", "1-strike", "nostop", "CB-BRACKET", "C-OPUS", "DS-FLASH"],
            "specs": {"BTCUSD": {"tick": 5.0, "point_value": 1.0, "digits": 2, "qty_step": 0.001}},
        },
        "lucid": {"symbols": {"XAUUSD": {}, "EURUSD": {}, "GBPUSD": {}, "US100": {}, "XAGUSD": {}}},
    }


def _bars(start, n, *, o, h, l, c):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def test_btc_is_other_venue_silver_stays_lucid(tmp_path):
    cfg = _cfg(tmp_path)
    assert other_venue_symbols(cfg) == ["BTCUSD"]
    assert is_other_venue_symbol(cfg, "BTCUSD")
    assert not is_other_venue_symbol(cfg, "XAGUSD")
    assert "XAGUSD" in (cfg["lucid"]["symbols"])


def test_size_risk_about_900_and_fees_slip():
    cfg = _cfg(Path("/tmp"))
    atr = 200.0
    qty = size_qty(cfg, "BTCUSD", atr)
    # 900 / 200 = 4.5
    assert qty == pytest.approx(4.5)
    px = 100_000.0
    assert fee_usd(cfg, qty, px) == pytest.approx(0.0005 * 4.5 * 100_000)
    # long entry: raw + 1 tick (5)
    assert slip_px(cfg, "BTCUSD", 1, px, entry=True) == 100_005.0
    # long exit: raw - 1 tick
    assert slip_px(cfg, "BTCUSD", 1, px, entry=False) == 99_995.0


def test_main_fill_copies_and_separate_100k(tmp_path):
    cfg = _cfg(tmp_path)
    hub = OtherVenueHub(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    sig = {"symbol": "BTCUSD", "action": "BUY", "atr": 200.0, "confidence": 0.8,
           "model": "gemini-3.8-flash", "thinking_level": "high"}
    pos = hub.intake_signal("MAIN", sig, T0, copy_to=["1-strike", "nostop"], quote=100_000.0)
    assert pos["entry"] == 100_005.0
    assert pos["qty"] == pytest.approx(4.5)
    assert pos["exit_mode"] == UNIFORM_EXIT_VERSION
    assert pos["stop"] < pos["entry"] < pos["target"]
    for name in ("MAIN", "1-strike", "nostop"):
        b = hub.book(name)
        assert len(b["positions"]) == 1
        assert b["account"]["start_balance"] == 100000.0
        # entry fee deducted: 0.05% * 4.5 * 100005
        fee = 0.0005 * 4.5 * 100005.0
        assert b["account"]["balance"] == pytest.approx(100000.0 - fee, rel=1e-6)
    # CB-BRACKET is independent and still flat
    assert hub.book("CB-BRACKET")["positions"] == []
    assert hub.book("CB-BRACKET")["account"]["balance"] == 100000.0


def test_uniform_stop_closes_and_tags(tmp_path):
    cfg = _cfg(tmp_path)
    hub = OtherVenueHub(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    sig = {"symbol": "BTCUSD", "action": "BUY", "atr": 200.0, "confidence": 0.7}
    pos = hub.intake_signal("MAIN", sig, T0, quote=100_000.0)
    # stop ~ entry - 200 = 99805; walk through it
    start = T0 + timedelta(minutes=1)
    df = _bars(start, 5, o=[99900]*5, h=[99950]*5, l=[99700]*5, c=[99800]*5)
    closed = hub.tick_book("MAIN", {"BTCUSD": df}, T0 + timedelta(minutes=6))
    assert len(closed) == 1
    rec = closed[0]
    assert rec["reason"] == "stop_1atr"
    assert rec["exit_mode"] == UNIFORM_EXIT_VERSION
    assert rec["net"] < 0
    assert rec["venue"] == VENUE
    assert hub.book("MAIN")["positions"] == []
    assert hub.book("MAIN")["account"]["trades"] == 1
    assert hub.book("MAIN")["account"]["losses"] == 1


def test_site_payload_lists_other_venue_line(tmp_path):
    cfg = _cfg(tmp_path)
    hub = OtherVenueHub(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    hub.intake_signal("C-OPUS", {"symbol": "BTCUSD", "action": "SELL", "atr": 100.0}, T0, quote=99_000.0)
    payload = hub.write_site(T0)
    assert (tmp_path / "other_venue.json").exists()
    names = [r["book"] for r in payload["books"]]
    assert "C-OPUS" in names and "MAIN" in names and "DS-FLASH" in names
    opus = next(r for r in payload["books"] if r["book"] == "C-OPUS")
    assert opus["open_positions"] and opus["account"]["balance"] < 100000
    assert "Other venue" in (payload["note"] or "")
    assert payload["symbols"] == ["BTCUSD"]


def test_xag_not_routed_when_si_is_on_lucid(tmp_path):
    cfg = _cfg(tmp_path, symbols=["BTCUSD"])  # SI listed in fees → not in other_venue.symbols
    hub = OtherVenueHub(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    r = hub.intake_signal("MAIN", {"symbol": "XAGUSD", "action": "BUY", "atr": 0.12}, T0, quote=50.0)
    assert r["skipped"] == "not_other_venue_symbol"
    assert hub.book("MAIN")["positions"] == []
