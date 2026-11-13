"""List models available to GEMINI_API_KEY and probe configured IDs. Never logs the key."""
from __future__ import annotations
import os, time
from typing import Iterable


def _ids_from_list(models) -> list[str]:
    out = []
    for m in models:
        name = getattr(m, "name", None) or str(m)
        out.append(name.replace("models/", "", 1) if isinstance(name, str) else str(name))
    return out


def closest_ids(target: str, pool: Iterable[str], k: int = 10) -> list[str]:
    scored = []
    for p in pool:
        if target == p:
            scored.append((1000, p)); continue
        score = 0
        if target in p or p in target:
            score += 80 + min(len(p), 20)
        tt, pp = set(target.replace("-", ".").split(".")), set(p.replace("-", ".").split("."))
        score += 12 * len(tt & pp)
        i = 0
        for a, b in zip(target, p):
            if a == b:
                i += 1
            else:
                break
        score += i
        scored.append((score, p))
    scored.sort(reverse=True)
    return [p for s, p in scored if s > 0][:k]


def probe_model(client, mid: str, thinking: str | None = None) -> dict:
    """thinking: optional thinking level ("minimal" | "low" | "medium" | "high") sent as
    ThinkingConfig(thinking_level=...); the response's thoughts_token_count is reported."""
    t0 = time.monotonic()
    try:
        kw = {}
        if thinking:
            from google.genai import types
            kw["config"] = types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level=thinking), max_output_tokens=2000)
        from .market_hours import guard as _mh_guard; _mh_guard("model probe")
        resp = client.models.generate_content(
            model=mid, contents=("Think briefly: which is larger, 9.11 or 9.9? Then reply with the single word pong."
                                 if thinking else "Reply with the single word pong."), **kw)
        usage = None
        if getattr(resp, "usage_metadata", None):
            try:
                usage = resp.usage_metadata.model_dump(exclude_none=True)
            except Exception:
                usage = str(resp.usage_metadata)
        thoughts = None
        if getattr(resp, "usage_metadata", None) is not None:
            thoughts = getattr(resp.usage_metadata, "thoughts_token_count", None)
        return {"id": mid, "ok": True, "latency_s": round(time.monotonic() - t0, 3), "thinking": thinking,
                "thoughts_token_count": thoughts,
                "model_version": getattr(resp, "model_version", None),
                "text": (getattr(resp, "text", None) or "")[:80], "usage": usage,
                "error": None}
    except Exception as e:
        return {"id": mid, "ok": False, "latency_s": round(time.monotonic() - t0, 3), "thinking": thinking,
                "model_version": None, "text": None, "usage": None,
                "error": f"{type(e).__name__}: {str(e)[:500]}"}


def verify_models(cfg: dict | None = None, probe: list[str] | None = None, thinking: str | None = None) -> dict:
    """List models, then probe each ID. Does not swap a rejected ID for a neighbour."""
    from google import genai
    from .config import entry_model, exit_model
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return {"ok": False, "error": "GEMINI_API_KEY not set", "listed": [], "probes": []}
    client = genai.Client(api_key=key)
    listed_error = None
    listed: list[str] = []
    t0 = time.monotonic()
    try:
        listed = _ids_from_list(client.models.list())
        list_s = round(time.monotonic() - t0, 3)
    except Exception as e:
        list_s = round(time.monotonic() - t0, 3)
        listed_error = f"{type(e).__name__}: {str(e)[:500]}"
    if probe is None:
        if cfg is None:
            from .config import load_config
            cfg = load_config()
        probe = [entry_model(cfg), exit_model(cfg)]
    # unique, preserve order
    seen, ids = set(), []
    for m in probe:
        if m and m not in seen:
            seen.add(m); ids.append(m)
    probes = []
    for mid in ids:
        r = probe_model(client, mid, thinking)
        r["exact_in_list"] = mid in listed if listed else None
        r["closest"] = closest_ids(mid, listed) if listed else []
        probes.append(r)
    return {"ok": all(p["ok"] for p in probes) and not listed_error, "list_seconds": list_s,
            "listed_error": listed_error,
            "listed_gemini3_or_4": sorted(n for n in listed if "gemini-3" in n or "gemini-4" in n),
            "listed_count": len(listed), "probes": probes}


def format_report(res: dict) -> str:
    lines = []
    if res.get("listed_error"):
        lines.append(f"LIST FAILED ({res.get('list_seconds')}s): {res['listed_error']}")
    else:
        lines.append(f"LIST OK ({res.get('list_seconds')}s): {res.get('listed_count')} models; "
                     f"gemini 3/4: {', '.join(res.get('listed_gemini3_or_4') or []) or 'none'}")
    for p in res.get("probes") or []:
        if p["ok"]:
            lines.append(f"PROBE OK  {p['id']}  thinking={p.get('thinking')}  thoughts_token_count={p.get('thoughts_token_count')}  latency={p['latency_s']}s  "
                         f"version={p.get('model_version')}  usage={p.get('usage')}")
        else:
            lines.append(f"PROBE FAIL  {p['id']}  thinking={p.get('thinking')}  latency={p['latency_s']}s  error={p['error']}")
            if p.get("closest"):
                lines.append(f"  closest valid IDs: {', '.join(p['closest'])}")
    return "\n".join(lines)
