"""Prompt assembly, Gemini call (google-genai), and a deterministic mock for --dry-run."""
from __future__ import annotations
import asyncio, json, logging, os, random, time
from typing import Literal, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, Field

from .config import ROOT

log = logging.getLogger("signals.gemini")


# Field order is the Gemini response_schema / propertyOrdering. Analytical fields
# come first so the model writes rationale before it commits to action/confidence.
# google-genai process_schema copies properties.keys() into propertyOrdering when
# that field is unset, so declaration order here is what the API enforces.
SIGNAL_FIELD_ORDER = (
    "symbol", "trend_vs_vwap", "volatility", "level_event", "rationale",
    "action", "confidence", "risk_pct", "entry", "stop_loss", "take_profit", "risk_reward",
)


class Signal(BaseModel):
    symbol: str
    trend_vs_vwap: str = Field(description="bullish / bearish / neutral + one short reason")
    volatility: str = Field(description="expanding / contracting / normal")
    level_event: str = Field(description="sweep / rejection / acceptance / none, with the level")
    rationale: str
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(description="0..1")
    risk_pct: Optional[float] = Field(default=None, description="suggested % of equity to risk")
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward: Optional[float] = None


class SignalBatch(BaseModel):
    signals: list[Signal]
    market_summary: str


_env = Environment(loader=FileSystemLoader(ROOT / "templates"), undefined=StrictUndefined)


def system_prompt(cfg: dict, symbols: list[str]) -> str:
    r, ch = cfg["risk"], cfg["chart"]
    return _env.get_template("system_prompt.j2").render(
        n=len(symbols), symbols=symbols, timeframe=f"M{cfg['general']['timeframe_minutes']}",
        min_rr=r["min_rr"], max_entry_atr=r["max_entry_distance_atr"], min_stop_atr=r["min_stop_distance_atr"],
        max_risk_pct=r["max_risk_pct"], c=ch)


def _image_mime(blob: bytes) -> str:
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith(b"\x89PNG"):
        return "image/png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


# Dropped from the per-symbol JSON sent to Gemini (still present on the internal
# payload context used by scalp_levels / Lucid / the validator):
#   last_candle          — identical to signal_candle (closed M15 only)
#   last_close           — duplicate of signal_candle.close (and of last_price today)
#   vp_value_area_pct    — constant 0.70; stated once in the system prompt
#   session_tz           — constant America/New_York; stated once in the system prompt
_GEMINI_CTX_DROP = ("last_candle", "last_close", "vp_value_area_pct", "session_tz")


def slim_gemini_context(ctx: dict) -> dict:
    """Per-symbol numeric context for the Gemini user message (deduped)."""
    return {k: v for k, v in ctx.items() if k not in _GEMINI_CTX_DROP}


def chart_user_text(payload: dict, index: int, total: int) -> str:
    ctx = {"symbol": payload["symbol"], "source": f"{payload['source']}:{payload['ticker']}",
           "timeframe": payload["timeframe"], **slim_gemini_context(payload["context"])}
    raw = payload.get("context") or {}
    price = ctx.get("live_price", ctx.get("last_price", raw.get("last_close")))
    ts = ctx.get("live_price_ts_utc", ctx.get("last_price_ts_utc", ctx.get("last_closed_bar_utc")))
    stale = ctx.get("live_price_stale")
    tag = "LIVE PRICE SENT TO GEMINI"
    extra = " (STALE — last closed M15 close)" if stale else ""
    return (f"Chart {index}/{total} - {payload['symbol']}\n"
            f"{tag}: {price} @ {ts}{extra}\n"
            f"Numeric context: {json.dumps(ctx)}")


def build_contents(payloads: list[dict], pngs: dict[str, bytes]):
    from google.genai import types
    parts = []
    for i, p in enumerate(payloads, 1):
        parts.append(types.Part.from_text(text=chart_user_text(p, i, len(payloads))))
        blob = pngs[p["symbol"]]
        parts.append(types.Part.from_bytes(data=blob, mime_type=_image_mime(blob)))
    parts.append(types.Part.from_text(text="Return the JSON signal batch for all symbols now."))
    return [types.Content(role="user", parts=parts)]


RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}


def _is_retryable(e: Exception) -> bool:
    from google.genai import errors
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return True
    if isinstance(e, errors.APIError):
        code, status, msg = getattr(e, "code", None), str(getattr(e, "status", "") or ""), str(e).lower()
        if code in (402, 404) or "prepayment" in msg or "billing" in msg or "no longer available" in msg:
            return False  # billing / model gone: retrying the same model can't help
        if code == 429 and ("perday" in msg.replace(" ", "").replace("_", "") or "per day" in msg):
            return False  # daily quota gone: retrying within this cycle cannot help
        return code in RETRYABLE_CODES or status in ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL",
                                                     "DEADLINE_EXCEEDED")
    return "timed out" in str(e).lower() or "connection" in str(e).lower()


def _gen_config(cfg: dict, model: str, payloads: list[dict]):
    from google.genai import types
    g = cfg["gemini"]
    kw = dict(system_instruction=system_prompt(cfg, [p["symbol"] for p in payloads]),
              response_mime_type="application/json", response_schema=SignalBatch)
    if model.startswith("gemini-3"):
        if g.get("thinking_level"):
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=g["thinking_level"])
    elif g.get("temperature") is not None:
        kw["temperature"] = g["temperature"]
    return types.GenerateContentConfig(**kw)


async def call_gemini(cfg: dict, payloads: list[dict], pngs: dict[str, bytes], model: str | None = None) -> dict:
    """Single multimodal request with all charts, with retry/backoff and model fallback.
    Returns {'model', 'raw_text', 'usage', 'attempts'}; raises RuntimeError if every model fails."""
    from google import genai
    from google.genai import types
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set (put it in .env or the environment) - or use --dry-run")
    g = cfg["gemini"]
    models = [model or g["model"]] + [m for m in g.get("fallback_models", []) if m != (model or g["model"])]
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(g["timeout_seconds"] * 1000)))
    contents = build_contents(payloads, pngs)
    attempts: list[dict] = []
    for m in models:
        t0 = time.monotonic()
        for n in range(1, int(g.get("max_attempts_per_model", 4)) + 1):
            try:
                log.info("Gemini request: model=%s attempt=%d", m, n)
                resp = await client.aio.models.generate_content(model=m, contents=contents,
                                                                config=_gen_config(cfg, m, payloads))
                if not resp.text:
                    raise RuntimeError(f"empty response (finish_reason="
                                       f"{resp.candidates[0].finish_reason if resp.candidates else None})")
                usage = resp.usage_metadata.model_dump(exclude_none=True) if resp.usage_metadata else None
                attempts.append({"model": m, "attempt": n, "ok": True})
                answered = getattr(resp, "model_version", None) or m
                log.info("Gemini answered: requested=%s model_version=%s (attempt %d)", m, answered, n)
                return {"model": m, "model_version": answered, "raw_text": resp.text, "usage": usage,
                        "attempts": attempts}
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:300]}"
                retry = _is_retryable(e)
                attempts.append({"model": m, "attempt": n, "ok": False, "retryable": retry, "error": err})
                log.warning("Gemini %s attempt %d failed (%s): %s", m, n, "retryable" if retry else "fatal", err)
                if not retry:
                    break
                delay = min(g.get("backoff_max_seconds", 30.0), g.get("backoff_base_seconds", 2.0) * 2 ** (n - 1))
                delay = delay * (0.5 + random.random())  # jitter
                if n == g.get("max_attempts_per_model", 4) or \
                        time.monotonic() - t0 + delay > g.get("retry_budget_seconds", 90):
                    break
                await asyncio.sleep(delay)
        log.warning("Gemini model %s exhausted, trying next fallback", m)
    raise GeminiFailed(attempts)


class GeminiFailed(RuntimeError):
    def __init__(self, attempts: list[dict]):
        self.attempts = attempts
        last = attempts[-1]["error"] if attempts else "no attempts"
        super().__init__(f"all models failed ({len(attempts)} attempts); last: {last}")


def mock_response(payloads: list[dict], cfg: dict) -> dict:
    """Deterministic fake Gemini output for --dry-run. Deliberately includes one bad-R:R signal and one
    wrong-side stop so the local validator's downgrades are exercised."""
    sigs = []
    for i, p in enumerate(payloads):
        c, d = p["context"], p["digits"]
        last, atr = c["last_close"], c["atr"]
        side = "BUY" if c["close_vs_vwap"] == "above" else "SELL"
        k = 1 if side == "BUY" else -1
        entry, sl, tp = last, last - k * 1.0 * atr, last + k * 3.0 * atr
        note = "mock: trend-following vs VWAP, 1:3 target"
        if i == 3:
            tp = last + k * 1.5 * atr; note = "mock: deliberately R:R 1.5 (validator must downgrade)"
        if i == 4:
            sl = last + k * 1.0 * atr; note = "mock: deliberately stop on wrong side (validator must downgrade)"
        rr = abs(tp - entry) / abs(entry - sl) if entry != sl else None
        sigs.append({"symbol": p["symbol"], "action": side, "entry": round(entry, d), "stop_loss": round(sl, d),
                     "take_profit": round(tp, d), "risk_reward": round(rr, 2) if rr else None, "confidence": 0.7,
                     "risk_pct": 0.5, "trend_vs_vwap": f"{c['close_vs_vwap']} VWAP",
                     "volatility": "expanding" if c["atr_expanding_vs_20bar_avg"] else "normal",
                     "level_event": "none", "rationale": note})
    return {"model": "mock-dry-run", "raw_text": json.dumps({"signals": sigs, "market_summary": "dry-run mock"}),
            "usage": None}
