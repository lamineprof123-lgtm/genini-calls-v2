"""Entry telemetry snapshots on trade + skip, and lucid-report grouping math."""
import copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.lucid import LucidSim
from signals.lucid_report import build_report, write_report, stats, confidence_band
from signals.telemetry import build_entry_snapshot, enrich_close_metrics

UTC = timezone.utc
T_NY = datetime(2026, 11, 10, 16, 30, tzinfo=UTC)  # 11:30 EST = NEW_YORK
ATR = 40.0


def _cfg(tmp):
    cfg = copy.deepcopy(load_config())
    cfg.setdefault("exit_manager", {})["uniform_1atr_exits"] = False
    cfg["lucid"]["slippage_ticks"] = 0
    cfg["lucid"]["exit_mode"] = "server"; cfg["lucid"].pop("reset_archive", None)  # legacy server-target sim
    return cfg


def _ctx(px=20000.0):
    return {
        "last_close": px, "atr": ATR, "vwap": px - 20, "ema20_baseline": px - 10, "ema50": px - 30,
        "atr_upper": px + 2 * ATR, "atr_lower": px - 2 * ATR,
        "poc": px - 15, "vah": px + 5, "val": px - 40,
        "price_vs_value_area": "inside", "close_vs_vwap": "above",
        "cvd": 12.5, "cvd_label": "BULLISH", "cvd_slope": 1.2,
        "session": "NEW_YORK_SESSION",
        "rvol_last": 2.4, "rvol_bars_since_spike": 0,
        "levels": [
            {"type": "resistance", "label": "Session High", "price": px + 50},
            {"type": "support", "label": "Prev Low", "price": px - 60},
        ],
        "signal_candle": {"open": px, "high": px + 10, "low": px - 10, "close": px},
        "digits": 2,
    }


def _rec(t, action="BUY", px=20000.0, conf=0.72, session="NEW_YORK_SESSION"):
    ctx = _ctx(px)
    sl, tp = (px - 20, px + 60) if action == "BUY" else (px + 20, px - 60)
    sig = {
        "symbol": "US100", "action": action, "model_action": action,
        "entry": px, "stop_loss": sl, "take_profit": tp,
        "confidence": conf, "rr_local": 3.0, "last_close": px, "atr": ATR,
        "model_risk_pct": None, "reasons": [], "rationale": "breakout above VAH with CVD agree",
        "session": session, "last_closed_bar_utc": t.isoformat(),
        "poc": ctx["poc"], "vah": ctx["vah"], "val": ctx["val"],
        "cvd": ctx["cvd"], "cvd_label": ctx["cvd_label"], "cvd_slope": ctx["cvd_slope"],
        "signal_candle": ctx["signal_candle"], "levels": ctx["levels"],
        "scalp": {"tdist": 20.0, "sdist": 30.0, "stop_source": "atr_0.8",
                  "target_source": "fixed", "candle": ctx["signal_candle"],
                  "levels": ctx["levels"], "vp": {"poc": ctx["poc"], "vah": ctx["vah"], "val": ctx["val"]}},
    }
    return {
        "run_at_utc": t.isoformat(),
        "run_dir": "/tmp/fake_run",
        "model": "gemini-test",
        "data": {"US100": ctx},
        "signals": [sig],
    }


def _bars(rows, sym="US100"):
    df = pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close"],
                      index=pd.DatetimeIndex([r[0] for r in rows]))
    return {sym: df}


def test_build_entry_snapshot_fields():
    snap = build_entry_snapshot(
        symbol="US100", side=1, signal=_rec(T_NY)["signals"][0],
        ctx=_ctx(), scalp={"tdist": 20, "sdist": 30, "stop_source": "structure", "target_source": "fixed"},
        size={"qty": 7, "contract": "NQ", "target_usd": 500, "risk_usd": 800, "tdist": 20, "sdist": 30},
        record={"run_at_utc": T_NY.isoformat(), "model": "m", "run_dir": "/r"},
    )
    required = {
        "symbol", "session", "last_close", "atr", "vwap", "ema20", "ema50",
        "price_vs_vwap", "price_vs_ema50", "poc", "vah", "val", "price_vs_value_area",
        "dist_to_poc_atr", "cvd", "cvd_label", "cvd_slope", "cvd_agrees_with_side",
        "rvol", "rvol_spike", "rvol_bars_since_spike", "atr_band_z", "nearest_sr",
        "gemini", "scalp", "planned", "run_at_utc", "chart_path",
    }
    assert required <= set(snap)
    assert snap["cvd_agrees_with_side"] is True
    assert snap["rvol_spike"] is True
    assert snap["price_vs_value_area"] == "inside"
    assert snap["price_vs_vwap"]["side"] == "above"
    assert snap["gemini"]["confidence_band"] == "0.7-0.8"
    assert snap["chart_path"] == "/r/US100.png"
    assert snap["planned"]["qty"] == 7
    assert snap["nearest_sr"]["above"]["label"] == "Session High"
    assert snap["atr_band_z"] is not None


def test_snapshot_on_accepted_trade_and_close(tmp_path):
    cfg = _cfg(tmp_path)
    sim = LucidSim(cfg, tmp_path / "st", tmp_path / "lg")
    sim.s["last_processed"] = T_NY.isoformat()
    t = T_NY
    out = sim.on_record(_rec(t), t)
    assert out[0]["taken"] is True
    assert sim.s["pending"] and "telemetry" in sim.s["pending"][0]
    snap = sim.s["pending"][0]["telemetry"]
    assert snap["session"] == "NEW_YORK_SESSION"
    assert snap["cvd_agrees_with_side"] is True
    assert snap["gemini"]["rationale"]
    # fill + win
    px = 20000.0
    sim.process(_bars([(t + timedelta(minutes=1), px, px + 1, px - 1, px)]), t + timedelta(minutes=2))
    assert sim.s["positions"] and sim.s["positions"][0].get("telemetry")
    # shrink for a clean target hit
    p = sim.s["positions"][0]
    p["contract"] = "NQ"; p["qty"] = 1
    p["stop"] = px - 40; p["target"] = px + 20
    p["time_cut_deadline"] = None; p["mfe_min"] = 0
    # drive MFE/MAE: first dip then hit target
    sim.process(_bars([
        (t + timedelta(minutes=2), px, px + 5, px - 15, px - 5),
        (t + timedelta(minutes=3), px - 5, px + 25, px - 6, px + 20),
    ]), t + timedelta(minutes=4))
    assert not sim.s["positions"]
    trades = list((tmp_path / "lg" / t.astimezone(
        __import__("zoneinfo").ZoneInfo(cfg["general"]["session_tz"])).strftime("%Y-%m-%d")
                   ).glob("lucid_trades.jsonl"))
    assert trades
    rec = json.loads(trades[0].read_text().strip().splitlines()[-1])
    assert "telemetry" in rec
    assert rec["telemetry"]["cvd_agrees_with_side"] is True
    assert rec["telemetry"]["mfe_usd"] is not None
    assert rec["telemetry"]["mae_usd"] is not None
    assert rec["telemetry"]["mae_usd"] >= 0
    assert rec["bars_to_exit_m1"] >= 1
    assert rec["mfe_atr"] is not None


def test_snapshot_on_session_blocked_skip(tmp_path):
    cfg = _cfg(tmp_path)
    t = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)  # 20:00 ET previous = ASIAN
    sim = LucidSim(cfg, tmp_path / "st", tmp_path / "lg")
    sim.s["last_processed"] = t.isoformat()
    r = _rec(t, session="ASIAN_SESSION_CHOP")
    r["signals"][0]["session"] = "ASIAN_SESSION_CHOP"
    out = sim.on_record(r, t)
    assert out[0]["taken"] is False
    assert out[0]["why"] == "session_blocked"
    day = t.astimezone(__import__("zoneinfo").ZoneInfo(cfg["general"]["session_tz"])).strftime("%Y-%m-%d")
    ev_path = tmp_path / "lg" / day / "lucid_events.jsonl"
    events = [json.loads(x) for x in ev_path.read_text().splitlines()]
    skips = [e for e in events if e["event"] == "entry_skipped"]
    assert skips and skips[0]["why"] == "session_blocked"
    assert "telemetry" in skips[0]
    assert skips[0]["telemetry"]["session"] == "ASIAN_SESSION_CHOP"
    assert skips[0]["telemetry"]["poc"] is not None


def test_snapshot_on_sizing_skip(tmp_path):
    """Tiny unreachable target distance -> entry_skipped with telemetry at fill."""
    cfg = _cfg(tmp_path)
    cfg["lucid"]["target_usd_min"] = 5000  # unreachable at normal distances
    cfg["lucid"]["target_usd_aim"] = 5000
    cfg["lucid"]["target_usd_max"] = 6000
    sim = LucidSim(cfg, tmp_path / "st", tmp_path / "lg")
    sim.s["last_processed"] = T_NY.isoformat()
    t = T_NY
    assert sim.on_record(_rec(t), t)[0]["taken"]
    px = 20000.0
    sim.process(_bars([(t + timedelta(minutes=1), px, px + 1, px - 1, px)]), t + timedelta(minutes=2))
    assert not sim.s["positions"]
    day = t.astimezone(__import__("zoneinfo").ZoneInfo(cfg["general"]["session_tz"])).strftime("%Y-%m-%d")
    events = [json.loads(x) for x in (tmp_path / "lg" / day / "lucid_events.jsonl").read_text().splitlines()]
    skips = [e for e in events if e["event"] == "entry_skipped" and "cannot reach" in e["why"]]
    assert skips
    assert "telemetry" in skips[0]
    assert skips[0]["telemetry"]["symbol"] == "US100"


def test_report_grouping_math_and_zero_trades(tmp_path):
    # empty
    r, md = write_report(tmp_path / "empty_logs")
    assert r["summary"]["count"] == 0
    assert r["summary"]["win_rate"] is None
    assert r["summary"]["expectancy"] is None
    assert "No closed paper trades yet" in md
    assert r["skips"]["count"] == 0

    # synthetic trades spanning buckets
    day = tmp_path / "logs" / "2026-11-10"
    day.mkdir(parents=True)
    trades = [
        {"net": 100, "symbol": "US100", "reason": "target",
         "telemetry": {"session": "NEW_YORK_SESSION", "price_vs_value_area": "above_vah",
                       "cvd_agrees_with_side": True, "rvol_spike": True,
                       "vwap_aligned_with_side": True, "ema50_aligned_with_side": True,
                       "gemini": {"confidence": 0.85}}},
        {"net": -50, "symbol": "US100", "reason": "stop",
         "telemetry": {"session": "NEW_YORK_SESSION", "price_vs_value_area": "above_vah",
                       "cvd_agrees_with_side": True, "rvol_spike": False,
                       "vwap_aligned_with_side": True, "ema50_aligned_with_side": False,
                       "gemini": {"confidence": 0.65}}},
        {"net": 80, "symbol": "EURUSD", "reason": "target",
         "telemetry": {"session": "LONDON_NY_OVERLAP", "price_vs_value_area": "inside",
                       "cvd_agrees_with_side": False, "rvol_spike": True,
                       "vwap_aligned_with_side": False, "ema50_aligned_with_side": False,
                       "gemini": {"confidence": 0.55}}},
        {"net": 0, "symbol": "XAUUSD", "reason": "flat_by_16:45_ET",
         "telemetry": {}},  # missing -> unknown buckets
    ]
    (day / "lucid_trades.jsonl").write_text("\n".join(json.dumps(t) for t in trades) + "\n")
    (day / "lucid_events.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"event": "entry_skipped", "why": "session_blocked"},
        {"event": "entry_skipped", "why": "session_blocked"},
        {"event": "entry_skipped", "why": "cannot reach $381 NET within max_minis=40"},
        {"event": "entry", "symbol": "US100"},
    ]) + "\n")

    r2, md2 = write_report(tmp_path / "logs")
    s = r2["summary"]
    assert s["count"] == 4
    assert s["wins"] == 2 and s["losses"] == 1 and s["breakeven"] == 1
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["net_pnl"] == pytest.approx(130.0)
    assert s["avg_win"] == pytest.approx(90.0)
    assert s["avg_loss"] == pytest.approx(-50.0)
    assert s["expectancy"] == pytest.approx(32.5)
    assert s["low_confidence"] is True  # n<20

    ny = r2["groups"]["session"]["NEW_YORK_SESSION"]
    assert ny["count"] == 2 and ny["net_pnl"] == pytest.approx(50.0)
    va = r2["groups"]["value_area_position"]["above_vah"]
    assert va["count"] == 2
    cvd_yes = r2["groups"]["cvd_agrees"]["yes"]
    assert cvd_yes["count"] == 2 and cvd_yes["wins"] == 1
    spike = r2["groups"]["rvol_spike"]["yes"]
    assert spike["count"] == 2
    hi = r2["groups"]["confidence_band"][">=0.8"]
    assert hi["count"] == 1 and hi["net_pnl"] == 100
    unk = r2["groups"]["session"]["unknown"]
    assert unk["count"] == 1  # missing telemetry
    assert r2["skips"]["count"] == 3
    assert r2["skips"]["by_reason"]["session_blocked"] == 2
    assert r2["skips"]["by_reason"]["sizing_unreachable"] == 1
    assert any(c["session"] == "NEW_YORK_SESSION" and c["cvd_agrees"] == "yes" for c in r2["top_combinations"])
    assert "LOW CONFIDENCE" in md2
    assert confidence_band(0.59) == "<0.6"
    assert confidence_band(0.8) == ">=0.8"
    # stats helper direct
    assert stats([])["win_rate"] is None


def test_enrich_close_metrics():
    out = enrich_close_metrics({"atr": 10.0}, mfe_pts=5.0, mae_pts=2.0, atr=10.0,
                               point_value_total=10.0, hold_seconds=900)
    assert out["mfe_usd"] == 50.0 and out["mae_usd"] == 20.0
    assert out["mfe_atr"] == 0.5 and out["mae_atr"] == 0.2
    assert out["bars_to_exit_m1"] == 15 and out["bars_to_exit_m15"] == 1
