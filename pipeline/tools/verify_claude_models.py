"""ev-v1 pre-go-live check: list the Anthropic models visible to ANTHROPIC_API_KEY, then one small structured-output
smoke call per (model, effort) with adaptive thinking. Never prints the key. Exit 1 if a model ID is missing or a
smoke call fails (no substitution: the caller stops and reports)."""
from __future__ import annotations
import asyncio, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signals import claude_client as C  # noqa: E402

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["read", "checks", "play", "action", "confidence", "invalidation"],
          "properties": {"read": {"type": "string"}, "checks": {"type": "string"},
                         "play": {"type": "string", "enum": ["continue", "reverse", "none"]},
                         "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                         "confidence": {"type": "number"}, "invalidation": {"type": ["number", "null"]}}}
PRICES = {"claude-fable-5-2": (8.0, 40.0, 10.0, 0.80), "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20)}


async def main(spec: str) -> int:
    if not C.key_present():
        print("ANTHROPIC_API_KEY not set")
        return 1
    want = [tuple(x.split(":")) for x in spec.split(",") if x.strip()]   # model:effort:max_tokens:timeout
    ids = []
    try:
        async for m in C._client().models.list(limit=1000):
            ids.append(m.id)
    except Exception as ex:
        print(f"models.list failed: {type(ex).__name__}: {str(ex)[:200]}")
    print("models visible:", ", ".join(sorted(ids)) or "(none)")
    rc = 0
    for model, effort, mt, to in want:
        listed = model in ids
        print(f"\n== {model} effort={effort}: listed={listed}")
        if not listed:
            print(f"MISSING: {model} is not in the model list -> stop, do not substitute")
            rc = 1
            continue
        t0 = time.monotonic()
        r = await C.messages_call(
            model=model, effort=effort, max_tokens=int(mt), timeout=float(to), max_attempts=1,
            thinking={"type": "adaptive"}, schema=SCHEMA, prices=PRICES,
            system="You are judging one market event detected by code on a closed 5-minute bar. Answer in JSON only.",
            content=[{"type": "text", "text": (
                "Smoke test (no trade). Event on EURUSD: LVL-SWEEP of Session High 1.10533, bar high 1.10560 "
                "(+0.18 ATR), closed 1.10520 back below. event_direction down. room_up_atr 0.4, room_down_atr 1.7. "
                "ATR 0.00150. Return the JSON now.")}])
        u = r.get("usage") or {}
        print(json.dumps({"ok": r.get("ok"), "model_returned": r.get("model"), "stop_reason": r.get("stop_reason"),
                          "error": r.get("error"), "latency_ms": r.get("latency_ms"),
                          "wall_ms": round((time.monotonic() - t0) * 1000),
                          "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                          "thinking_tokens": r.get("thinking_tokens"), "usage_keys": sorted(u),
                          "cost_usd": r.get("cost_usd"), "parsed": r.get("parsed")}, default=str, indent=1))
        if not r.get("ok"):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else
                              "claude-fable-5-2:xhigh:32000:90,claude-opus-5-5:high:16000:60")))
