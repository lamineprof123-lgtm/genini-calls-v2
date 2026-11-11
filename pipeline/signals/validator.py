"""Parse Gemini JSON, sanity-check every signal, recompute R:R and size locally, apply the circuit breaker."""
from __future__ import annotations
import json, math, re

from .gemini_client import SignalBatch

HOLD_FIELDS = ("entry", "stop_loss", "take_profit")


def parse_raw(raw_text: str) -> tuple[dict | None, str | None]:
    """Return (obj, error). Tolerates stray ```json fences even though the API is asked for pure JSON."""
    txt = (raw_text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1).strip()
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    try:
        SignalBatch.model_validate(obj)
    except Exception as e:  # schema errors are reported but per-signal checks still run
        return obj, f"schema: {str(e)[:300]}"
    return obj, None


def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _hold(out: dict, reason: str) -> dict:
    if out["action"] != "HOLD" or reason:
        out["reasons"].append(reason)
    if out["model_action"] in ("BUY", "SELL"):
        out["downgraded"] = True
    out["action"] = "HOLD"
    out.update(position_units=0.0, position_lots=0.0, risk_amount=0.0)
    return out


def validate_signal(sig: dict | None, symbol: str, ctx: dict, cfg: dict, equity: float,
                    min_rr: float | None = None, min_confidence: float | None = None) -> dict:
    """min_rr / min_confidence override [risk] for callers other than the public site (the Lucid paper sim
    passes min_rr=0 to skip the R:R minimum; the site path never passes overrides)."""
    r = dict(cfg["risk"])
    # Server-side scalp levels are the SL/TP source of truth (low / negative R:R thesis).
    if (cfg.get("scalp") or {}).get("enabled", False) and min_rr is None:
        r["min_rr"] = 0.0
    if min_rr is not None:
        r["min_rr"] = min_rr
    if min_confidence is not None:
        r["min_confidence"] = min_confidence
    scfg = cfg.get("symbols", {}).get(symbol, {})
    sig = sig or {}
    action = str(sig.get("action", "")).upper().strip()
    out = {"symbol": symbol, "model_action": action or None, "action": action,
           "entry": _num(sig.get("entry")), "stop_loss": _num(sig.get("stop_loss")),
           "take_profit": _num(sig.get("take_profit")), "model_risk_reward": _num(sig.get("risk_reward")),
           "rr_local": None, "confidence": _num(sig.get("confidence")), "model_risk_pct": _num(sig.get("risk_pct")),
           "risk_pct": None, "risk_amount": 0.0, "position_units": 0.0, "position_lots": 0.0,
           "last_close": ctx["last_close"], "atr": ctx["atr"],
           "rationale": sig.get("rationale"), "trend_vs_vwap": sig.get("trend_vs_vwap"),
           "volatility": sig.get("volatility"), "level_event": sig.get("level_event"),
           "downgraded": False, "reasons": []}
    if not sig:
        return _hold(out, "missing from model output")
    if action not in ("BUY", "SELL", "HOLD"):
        return _hold(out, f"invalid action {action!r}")
    conf = out["confidence"]
    if conf is not None and 1 < conf <= 100:
        conf = conf / 100
    out["confidence"] = None if conf is None else max(0.0, min(1.0, conf))
    if action == "HOLD":
        out.update(position_units=0.0, position_lots=0.0)
        return out
    e, sl, tp = out["entry"], out["stop_loss"], out["take_profit"]
    if None in (e, sl, tp):
        return _hold(out, "missing entry/stop_loss/take_profit")
    if action == "BUY" and not (sl < e < tp):
        return _hold(out, "BUY requires stop_loss < entry < take_profit")
    if action == "SELL" and not (tp < e < sl):
        return _hold(out, "SELL requires take_profit < entry < stop_loss")
    risk, reward = abs(e - sl), abs(tp - e)
    out["rr_local"] = round(reward / risk, 3)
    atr = ctx["atr"] or 0
    market = ctx.get("live_price") if ctx.get("live_price") is not None else ctx["last_close"]
    if atr > 0 and abs(e - market) > r["max_entry_distance_atr"] * atr:
        return _hold(out, f"entry {abs(e - market) / atr:.2f} ATR from market "
                          f"(max {r['max_entry_distance_atr']})")
    if atr > 0 and risk < r["min_stop_distance_atr"] * atr:
        return _hold(out, f"stop {risk / atr:.2f} ATR from entry (min {r['min_stop_distance_atr']})")
    if out["rr_local"] < r["min_rr"]:
        return _hold(out, f"R:R 1:{out['rr_local']} below minimum 1:{r['min_rr']}")
    if out["confidence"] is None or out["confidence"] < r["min_confidence"]:
        return _hold(out, f"confidence {out['confidence']} below {r['min_confidence']}")
    # local position sizing: risk% of equity / stop distance. Units are base units (oz, EUR, GBP, index pts x $1, BTC)
    rp = out["model_risk_pct"] if out["model_risk_pct"] and out["model_risk_pct"] > 0 else r["default_risk_pct"]
    rp = min(rp, r["max_risk_pct"])
    out["risk_pct"] = rp
    out["risk_amount"] = round(equity * rp / 100, 2)
    units = out["risk_amount"] / risk
    out["position_units"] = round(units, 6)
    out["position_lots"] = math.floor(units / scfg.get("contract_size", 1) * 100) / 100  # 0.01 lot step
    return out


def validate_batch(obj: dict | None, contexts: dict[str, dict], cfg: dict, equity: float,
                   breaker_halted: bool, breaker_reason: str | None = None) -> list[dict]:
    by_sym = {}
    for s in (obj or {}).get("signals", []) or []:
        if isinstance(s, dict) and s.get("symbol"):
            by_sym[str(s["symbol"]).upper().replace("/", "").strip()] = s
    out = []
    for sym, ctx in contexts.items():
        if breaker_halted and sym not in by_sym:  # AI call skipped while halted
            v = validate_signal({"action": "HOLD", "confidence": None}, sym, ctx, cfg, equity)
        else:
            v = validate_signal(by_sym.get(sym), sym, ctx, cfg, equity)
        if breaker_halted:
            if v["action"] != "HOLD":
                _hold(v, f"CIRCUIT BREAKER: {breaker_reason}")
            elif "CIRCUIT BREAKER" not in " ".join(v["reasons"]):
                v["reasons"].append(f"CIRCUIT BREAKER: {breaker_reason}")
        out.append(v)
    return out
