"""Anthropic Messages API calls for the Claude paper sims. PAPER ONLY. The key comes ONLY from the env var
ANTHROPIC_API_KEY (repo secret in GitHub Actions) and is never logged. Every function here returns a dict and
never raises, so a Claude failure can never reach the Gemini loop."""
from __future__ import annotations
import asyncio, base64, json, logging, os, time

from .config import ROOT

log = logging.getLogger("signals.claude")

PROMPT_DIR = ROOT / "templates" / "claude"
ENTRY_FIELDS = ("symbol", "trend_vs_vwap", "volatility", "level_event", "rationale", "setup_type", "score",
                "action", "confidence", "risk_pct", "entry", "stop_loss", "take_profit", "risk_reward")
EXIT_FIELDS = ("structure", "reason", "decision", "confidence")
EXIT_STRUCTURES = ("expanding", "healthy_pullback", "wall_touch_harvest", "momentum_stall",
                   "closed_bar_invalidated", "spike_hold_wait", "session_decay")
M5_LINE = "- M5 candles (cyan up, magenta down); the right-most paler bar is still forming."
M15_LINE = "- M15 candles (cyan up, magenta down); the right-most paler bar is still forming."

# USD per million tokens (docs.anthropic.com/en/docs/about-claude/pricing, checked 2026-11-12).
# (input, output, cache_write_5m, cache_read). Haiku 5.5 price is for prompts up to 100k tokens.
DEFAULT_PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}


def key_present() -> bool:
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


# ------------------------------------------------------------------ prompts (verbatim files)
def _split(text: str) -> list[tuple[str, str]]:
    """[(header, body)] for '=== ... ===' sections."""
    out, head, buf = [], None, []
    for line in text.splitlines():
        if line.startswith("=== ") and line.rstrip().endswith("==="):
            if head is not None:
                out.append((head, "\n".join(buf).strip("\n")))
            head, buf = line.strip(), []
        else:
            buf.append(line)
    if head is not None:
        out.append((head, "\n".join(buf).strip("\n")))
    return out


def load_prompts() -> dict:
    e = _split((PROMPT_DIR / "entry_prompt_v2.txt").read_text())
    x = _split((PROMPT_DIR / "exit_prompt_v2.txt").read_text())
    entry = next(b for h, b in e if "ENTRY SYSTEM PROMPT" in h)
    exit_sys = next(b for h, b in x if "EXIT SYSTEM PROMPT" in h)
    tmpl = next(b for h, b in x if "EXIT USER MESSAGE TEMPLATE" in h)
    # the template section ends with a "(Deliberately not sent: ...)" note that is documentation, not input
    tmpl = "\n".join(l for l in tmpl.splitlines() if not l.startswith("(Deliberately not sent")).strip()
    return {"entry": entry, "exit": exit_sys, "exit_user": tmpl}


def entry_system_prompt(symbols: list[str], prompts: dict | None = None) -> str:
    p = (prompts or load_prompts())["entry"]
    return p.replace("{n}", str(len(symbols))).replace("{symbol_order}", ", ".join(symbols))


def exit_system_prompt(m5_chart: bool, prompts: dict | None = None) -> str:
    p = (prompts or load_prompts())["exit"]
    return p if m5_chart else p.replace(M5_LINE, M15_LINE)


def render_exit_user(values: dict, prompts: dict | None = None) -> str:
    import re
    t = (prompts or load_prompts())["exit_user"]
    def repl(m):
        v = values.get(m.group(1))
        return "n/a" if v is None or str(v).strip() == "" else str(v)
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, t)


# ------------------------------------------------------------------ schemas
def signal_batch_schema(symbols: list[str]) -> dict:
    num_null = {"type": ["number", "null"]}
    item = {"type": "object", "additionalProperties": False, "required": list(ENTRY_FIELDS), "properties": {
        "symbol": {"type": "string", "enum": list(symbols)},
        "trend_vs_vwap": {"type": "string"}, "volatility": {"type": "string"},
        "level_event": {"type": "string"}, "rationale": {"type": "string"},
        "setup_type": {"type": "string", "enum": ["trend", "fade", "none"]},
        "score": {"type": "integer"},
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number"}, "risk_pct": num_null, "entry": num_null,
        "stop_loss": num_null, "take_profit": num_null, "risk_reward": num_null}}
    return {"type": "object", "additionalProperties": False, "required": ["signals", "market_summary"],
            "properties": {"signals": {"type": "array", "items": item}, "market_summary": {"type": "string"}}}


EXIT_DECISION_SCHEMA = {"type": "object", "additionalProperties": False, "required": list(EXIT_FIELDS),
                        "properties": {"structure": {"type": "string", "enum": list(EXIT_STRUCTURES)},
                                       "reason": {"type": "string"},
                                       "decision": {"type": "string", "enum": ["HOLD", "CLOSE"]},
                                       "confidence": {"type": "number"}}}


def validate_entry_signal(s: dict, ctx: dict, min_score: int) -> dict:
    """Range checks the API does not enforce. Returns a normalised row with 'gated' (tradable) and 'issues'."""
    out = {k: s.get(k) for k in ENTRY_FIELDS}
    issues = []
    act = str(out.get("action") or "HOLD").upper()
    out["action"] = act
    try:
        c = float(out.get("confidence") or 0)
    except (TypeError, ValueError):
        c = 0.0
    if not 0 <= c <= 1:
        issues.append(f"confidence {c} out of 0..1 (clipped)")
        c = min(1.0, max(0.0, c))
    out["confidence"] = c
    rp = out.get("risk_pct")
    if rp is not None and not (0 <= float(rp) <= 1.0):
        issues.append(f"risk_pct {rp} out of 0..1 (ignored)")
        out["risk_pct"] = None
    try:
        sc = int(out.get("score") or 0)
    except (TypeError, ValueError):
        sc = 0
    out["score"] = sc
    st = str(out.get("setup_type") or "none").lower()
    out["setup_type"] = st
    gated = act in ("BUY", "SELL") and sc >= min_score and st in ("trend", "fade")
    if act in ("BUY", "SELL") and not gated:
        issues.append(f"not tradable: score {sc} < {min_score} or setup_type {st}")
    out["gated"] = gated
    out["issues"] = issues
    return out


# ------------------------------------------------------------------ cost
def call_cost(model: str, usage: dict | None, prices: dict | None = None) -> float | None:
    if not usage:
        return None
    p = (prices or {}).get(model) or DEFAULT_PRICES.get(model)
    if not p:
        return None
    pin, pout, pw, pr = p
    cost = (usage.get("input_tokens") or 0) * pin + (usage.get("output_tokens") or 0) * pout \
        + (usage.get("cache_creation_input_tokens") or 0) * pw + (usage.get("cache_read_input_tokens") or 0) * pr
    return round(cost / 1e6, 6)


# ------------------------------------------------------------------ client
_CLIENT = None


def _client():
    global _CLIENT
    import anthropic
    if _CLIENT is None:
        _CLIENT = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip(), max_retries=0)
    return _CLIENT


def _retryable(ex: Exception) -> bool:
    try:
        import anthropic
    except Exception:
        return False
    if isinstance(ex, (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError,
                       anthropic.InternalServerError, asyncio.TimeoutError)):
        return True
    code = getattr(ex, "status_code", None)
    return code in (408, 409, 429, 500, 502, 503, 504, 529)


def image_block(blob: bytes) -> dict:
    from .gemini_client import _image_mime
    return {"type": "image", "source": {"type": "base64", "media_type": _image_mime(blob),
                                        "data": base64.b64encode(blob).decode()}}


async def messages_call(*, model: str, system: str, content: list, schema: dict, max_tokens: int,
                        thinking: dict, effort: str, timeout: float, max_attempts: int = 2,
                        prices: dict | None = None) -> dict:
    """One structured-output call with retries on transient errors. Never raises."""
    if not key_present():
        return {"ok": False, "model": model, "error": "ANTHROPIC_API_KEY not set", "attempts": 0}
    attempts, err = 0, None
    t0 = time.monotonic()
    for i in range(max(1, int(max_attempts))):
        attempts += 1
        t1 = time.monotonic()
        try:
            resp = await asyncio.wait_for(_client().messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": content}],
                thinking=thinking,
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                timeout=timeout), timeout + 2)
            usage = resp.usage.model_dump(exclude_none=True) if resp.usage else None
            text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
            parsed, perr = None, None
            try:
                parsed = json.loads(text) if text else None
            except Exception as ex:
                perr = f"{type(ex).__name__}: {str(ex)[:120]}"
            thinking_tokens = ((usage or {}).get("output_tokens_details") or {}).get("thinking_tokens")
            return {"ok": parsed is not None, "model": getattr(resp, "model", model), "raw_text": text,
                    "parsed": parsed, "parse_error": perr, "stop_reason": resp.stop_reason, "usage": usage,
                    "thinking_tokens": thinking_tokens, "latency_ms": round((time.monotonic() - t1) * 1000),
                    "total_ms": round((time.monotonic() - t0) * 1000), "attempts": attempts,
                    "cost_usd": call_cost(model, usage, prices), "request_id": getattr(resp, "_request_id", None),
                    "error": None if parsed is not None else (perr or f"empty/invalid output (stop={resp.stop_reason})")}
        except Exception as ex:
            err = f"{type(ex).__name__}: {str(ex)[:240]}"
            log.warning("claude %s attempt %d failed: %s", model, attempts, err)
            if not _retryable(ex) or i + 1 >= max_attempts:
                break
            await asyncio.sleep(min(4.0, 1.0 * (i + 1)))
    return {"ok": False, "model": model, "error": err, "attempts": attempts,
            "latency_ms": round((time.monotonic() - t0) * 1000), "total_ms": round((time.monotonic() - t0) * 1000)}


async def entry_call(cc: dict, model: str, system: str, content: list, schema: dict) -> dict:
    return await messages_call(model=model, system=system, content=content, schema=schema,
                               max_tokens=int(cc.get("entry_max_tokens", 8000)), thinking={"type": "adaptive"},
                               effort=cc.get("entry_effort", "medium"),
                               timeout=float(cc.get("timeout_seconds_entry", 60)),
                               max_attempts=int(cc.get("max_attempts", 2)), prices=cc.get("_prices"))


async def exit_call(cc: dict, system: str, user_text: str, image: bytes | None) -> dict:
    content = [{"type": "text", "text": user_text}] + ([image_block(image)] if image else [])
    return await messages_call(model=cc.get("exit_model", "claude-haiku-5-5"), system=system, content=content,
                               schema=EXIT_DECISION_SCHEMA, max_tokens=int(cc.get("exit_max_tokens", 200)),
                               thinking={"type": "disabled"}, effort=cc.get("exit_effort", "low"),
                               timeout=float(cc.get("timeout_seconds_exit", 5)), max_attempts=1,
                               prices=cc.get("_prices"))
