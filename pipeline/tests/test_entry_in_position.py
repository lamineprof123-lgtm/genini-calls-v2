"""Exclude Lucid symbols with an open or pending position from the batched entry scan."""
from __future__ import annotations
import copy, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config, entry_model, exit_model
from signals.pipeline import (
    lucid_busy_symbols, filter_entry_payloads, ALL_IN_POS_SKIP, lucid_symbols,
)
from signals.site import latest_signal_from_record, write_in_position_overlay
from signals.gemini_client import system_prompt, chart_user_text


def _cfg(tmp_path: Path):
    cfg = copy.deepcopy(load_config())
    cfg["general"]["state_dir"] = str(tmp_path / "state")
    cfg["general"]["logs_dir"] = str(tmp_path / "logs")
    cfg["site"]["dir"] = str(tmp_path / "site")
    Path(cfg["general"]["state_dir"], "lucid").mkdir(parents=True)
    Path(cfg["site"]["dir"]).mkdir(parents=True)
    return cfg


def _account(positions=None, pending=None, **extra):
    return {"positions": positions or [], "pending": pending or [], **extra}


def test_lucid_busy_includes_open_and_pending(tmp_path):
    cfg = _cfg(tmp_path)
    acct = _account(
        positions=[{"id": 1, "symbol": "XAUUSD", "side": -1}],
        pending=[{"symbol": "US100", "side": 1}],
    )
    (Path(cfg["general"]["state_dir"]) / "lucid" / "account.json").write_text(json.dumps(acct))
    busy = lucid_busy_symbols(cfg)
    assert busy == {"XAUUSD", "US100"}


def test_btcusd_is_never_excluded(tmp_path):
    cfg = _cfg(tmp_path)
    acct = _account(positions=[{"id": 1, "symbol": "BTCUSD"}])
    (Path(cfg["general"]["state_dir"]) / "lucid" / "account.json").write_text(json.dumps(acct))
    assert "BTCUSD" not in lucid_busy_symbols(cfg)
    assert "BTCUSD" not in set(lucid_symbols(cfg))


def test_filter_drops_busy_keeps_btcusd_and_free_fx():
    payloads = [{"symbol": s, "context": {}} for s in ["XAUUSD", "EURUSD", "GBPUSD", "US100", "BTCUSD"]]
    entry = filter_entry_payloads(payloads, {"XAUUSD"})
    assert [p["symbol"] for p in entry] == ["EURUSD", "GBPUSD", "US100", "BTCUSD"]


def test_all_lucid_busy_still_leaves_btcusd():
    payloads = [{"symbol": s} for s in ["XAUUSD", "EURUSD", "GBPUSD", "US100", "BTCUSD"]]
    entry = filter_entry_payloads(payloads, {"XAUUSD", "EURUSD", "GBPUSD", "US100"})
    assert [p["symbol"] for p in entry] == ["BTCUSD"]
    assert filter_entry_payloads(payloads, {p["symbol"] for p in payloads}) == []
    assert ALL_IN_POS_SKIP == "all_symbols_in_position"


def test_prompt_omits_excluded_symbol_chart_and_schema_slot(tmp_path):
    cfg = _cfg(tmp_path)
    payloads = []
    for i, s in enumerate(["XAUUSD", "EURUSD", "BTCUSD"], 1):
        payloads.append({"symbol": s, "source": "test", "ticker": s, "timeframe": "M15",
                         "context": {"last_close": 1.0 * i, "live_price": 1.0 * i,
                                     "last_closed_bar_utc": "2026-11-12T01:00:00+00:00"}})
    entry = filter_entry_payloads(payloads, {"XAUUSD"})
    text = system_prompt(cfg, [p["symbol"] for p in entry])
    assert "EURUSD" in text and "BTCUSD" in text
    # Schema slot / output order lists only the symbols in this batch (generic GEX/FX wording may still mention XAUUSD).
    assert "in this order: EURUSD, BTCUSD" in text
    assert "in this order: XAUUSD" not in text
    user = "\n".join(chart_user_text(p, i, len(entry)) for i, p in enumerate(entry, 1))
    assert "XAUUSD" not in user and "EURUSD" in user and "BTCUSD" in user
    assert "Chart 1/2" in user and "Chart 2/2" in user


def test_panel_marks_in_position_and_drops_stale_trade(tmp_path):
    cfg = _cfg(tmp_path)
    record = {
        "run_at_utc": "2026-11-12T01:00:00+00:00",
        "model": "gemini-3.8-pro",
        "dry_run": False,
        "signals": [
            {"symbol": "XAUUSD", "action": "SELL", "entry": 4000, "stop_loss": None,
             "take_profit": None, "rr_local": None, "confidence": 0.8},
            {"symbol": "EURUSD", "action": "HOLD", "entry": None, "stop_loss": None,
             "take_profit": None, "rr_local": None, "confidence": 0.4},
            {"symbol": "BTCUSD", "action": "BUY", "entry": 90000, "stop_loss": None,
             "take_profit": None, "rr_local": None, "confidence": 0.7},
        ],
        "in_position": ["XAUUSD"],
        "ai_error": None,
        "circuit_breaker": {},
    }
    out = latest_signal_from_record(record, cfg)
    assert out["in_position"] == ["XAUUSD"]
    assert [t["symbol"] for t in out["trades"]] == ["BTCUSD"]
    assert "XAUUSD" not in out["no_trade"] and "EURUSD" in out["no_trade"]


def test_overlay_strips_stale_signal_without_history(tmp_path):
    cfg = _cfg(tmp_path)
    site = Path(cfg["site"]["dir"])
    stale = {"signal_at": "2026-11-12T00:00:00+00:00", "trades": [
        {"symbol": "XAUUSD", "action": "SELL", "entry": 4000},
        {"symbol": "BTCUSD", "action": "BUY", "entry": 90000},
    ], "no_trade": ["EURUSD"], "in_position": []}
    (site / "latest_signal.json").write_text(json.dumps(stale))
    (site / "history.json").write_text("[]")
    out = write_in_position_overlay(cfg, {"XAUUSD"})
    assert out["in_position"] == ["XAUUSD"]
    assert [t["symbol"] for t in out["trades"]] == ["BTCUSD"]
    assert out["no_trade"] == ["EURUSD"]
    # history untouched
    assert json.loads((site / "history.json").read_text()) == []


def test_entry_and_exit_models_are_independent(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_ENTRY_MODEL", "gemini-3.8-pro")
    monkeypatch.setenv("GEMINI_EXIT_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")  # must not override ENTRY
    cfg = load_config()
    assert entry_model(cfg) == "gemini-3.8-pro"
    assert exit_model(cfg) == "gemini-3.8-flash"
    # --model / override is entry-only; exit stays flash
    assert entry_model(cfg, "gemini-other") == "gemini-other"
    assert exit_model(cfg) == "gemini-3.8-flash"
    # config defaults are both 3.8-flash; legacy GEMINI_MODEL still must not win over ENTRY
    monkeypatch.delenv("GEMINI_ENTRY_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_EXIT_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    cfg = load_config()
    assert entry_model(cfg) == "gemini-3.8-flash"
    assert exit_model(cfg) == "gemini-3.8-flash"


def test_legacy_gemini_model_does_not_override_entry_env(monkeypatch):
    monkeypatch.setenv("GEMINI_ENTRY_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")
    cfg = load_config()
    assert entry_model(cfg) == "gemini-3.8-flash"
