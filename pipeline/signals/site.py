"""Writes the static site's data files: site/status.json and site/latest_signal.json (no secrets, no logs)."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from .risk import _atomic_write


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(cfg: dict, state: str, interval: int | None = None, **extra) -> dict:
    """state: running (waiting for next_run_at) | analyzing (cycle in progress) | paused | stopped."""
    p = Path(cfg["site"]["dir"]) / "status.json"
    st = {"state": state, "interval_minutes": interval or cfg["scheduler"]["interval_minutes"],
          "align_offset_seconds": cfg["scheduler"]["align_offset_seconds"], "tz": cfg["general"]["session_tz"],
          "next_run_at": None, "updated_at": _now(), **extra}
    _atomic_write(p, st)
    return st


def latest_signal_from_record(record: dict, cfg: dict) -> dict:
    trades, no_trade = [], []
    busy = {str(s).upper().replace("/", "").strip() for s in (record.get("in_position") or [])}
    for s in record["signals"]:
        sym = s["symbol"]
        if sym in busy:
            continue  # never show the previous scan's BUY/SELL/HOLD for a live Lucid position
        if s["action"] in ("BUY", "SELL"):
            row = {"symbol": sym, "name": str(cfg.get("symbols", {}).get(sym, {}).get("name", "")).upper(),
                   "action": s["action"], "entry": s["entry"], "stop_loss": s["stop_loss"],
                   "take_profit": s["take_profit"], "rr": s["rr_local"], "confidence": s["confidence"],
                   "digits": cfg.get("symbols", {}).get(sym, {}).get("digits", 5)}
            L = cfg.get("lucid") or {}
            if L.get("enabled") and sym in (L.get("symbols") or {}) and L.get("exit_mode") == "gemini_only":
                # USER RULE: 100% Gemini - no stop loss, no take profit; exit on Gemini CLOSE only.
                row["stop_loss"] = None
                row["take_profit"] = None
                row["rr"] = None
                row["exit"] = "Gemini"
                hs = L.get("hard_stop_net_usd")
                try:
                    hs = float(hs) if hs else None
                except (TypeError, ValueError):
                    hs = None
                row["card"] = (f"Hard stop −${hs:,.0f} net · TP: none · exit: Gemini" if hs and hs > 0
                               else "SL: none · TP: none · exit: Gemini")
            elif L.get("enabled") and sym in (L.get("symbols") or {}) and L.get("exit_mode") == "gemini":
                # USER RULE: Gemini entry + Gemini exit. No take-profit on the panel: hard stop + "exit: Gemini".
                row["take_profit"] = None
                row["rr"] = None
                row["exit"] = "Gemini"
            if s.get("session"):
                row["session"] = s["session"]
            if s.get("cvd_label"):
                row["cvd_label"] = s["cvd_label"]
            trades.append(row)
        else:
            no_trade.append(sym)
    return {"signal_at": record["run_at_utc"], "tz": cfg["general"]["session_tz"],
            "model": record.get("model_version") or record.get("model"), "dry_run": record.get("dry_run", False),
            "trades": trades, "no_trade": no_trade, "in_position": sorted(busy),
            "analysis_failed": bool(record.get("ai_error")),
            **({"error": _short_error(record["ai_error"])} if record.get("ai_error") else {}),
            "breaker_halted": bool((record.get("circuit_breaker") or {}).get("halted")),
            "written_at": _now()}


def _short_error(err: str) -> str:
    e = str(err)
    low = e.lower()
    if "resource_exhausted" in low or "429" in e or "quota" in low:
        return "Gemini quota exhausted (free key)"
    if "503" in e or "unavailable" in low or "overloaded" in low:
        return "Gemini overloaded (503)"
    return e[:120]


HISTORY_MAX = 50


def write_latest_signal(record: dict, cfg: dict) -> dict:
    out = latest_signal_from_record(record, cfg)
    site = Path(cfg["site"]["dir"])
    _atomic_write(site / "latest_signal.json", out)
    # history.json: newest-last list of the last HISTORY_MAX latest_signal payloads (same shape)
    hp = site / "history.json"
    try:
        import json
        hist = json.loads(hp.read_text()) if hp.exists() else []
        if not isinstance(hist, list):
            hist = []
    except Exception:
        hist = []
    hist = (hist + [out])[-HISTORY_MAX:]
    _atomic_write(hp, hist)
    return out


def write_in_position_overlay(cfg: dict, busy: set[str] | list[str]) -> dict | None:
    """Patch latest_signal.json so symbols with an open/pending Lucid position are not left on a stale BUY/SELL.
    Does not append history.json (this is a status overlay, not a new scan)."""
    import json
    busy = {str(s).upper().replace("/", "").strip() for s in busy}
    site = Path(cfg["site"]["dir"])
    p = site / "latest_signal.json"
    try:
        cur = json.loads(p.read_text()) if p.exists() else {}
        if not isinstance(cur, dict):
            cur = {}
    except Exception:
        cur = {}
    cur.setdefault("tz", cfg["general"]["session_tz"])
    cur["trades"] = [t for t in (cur.get("trades") or []) if t.get("symbol") not in busy]
    cur["no_trade"] = [s for s in (cur.get("no_trade") or []) if s not in busy]
    cur["in_position"] = sorted(busy)
    cur["written_at"] = _now()
    site.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, cur)
    return cur
