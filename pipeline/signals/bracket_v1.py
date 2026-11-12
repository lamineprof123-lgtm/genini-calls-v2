"""Shared bracket-v1 entry package (t249u / t250u / t251u). PAPER ONLY.

C-OPUS-B and DS-FLASH consume this exact package: the reconstructed bracket prompt,
the same JPEG q78 charts, the same code-computed facts block, the same 3-symbol set
(XAUUSD, EURUSD, US100), and the same hold gates. The only intended differences
between those two books are the model and the effort setting.

This module does not place orders and does not call any model API.
"""
from __future__ import annotations
import hashlib, json, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT
from . import claude_client as CC

log = logging.getLogger("signals.bracket_v1")
UTC = timezone.utc

PROMPT_VERSION = "bracket-v1"
PROMPT_PATH = ROOT / "templates" / "bracket_entry_v1.txt"
PROMPT_RECONSTRUCTION = True  # original DeepSeek v1.1 file was not in the repo
SYMBOLS = ["XAUUSD", "EURUSD", "US100", "XAGUSD", "BTCUSD"]
FILL_MAX_ADVERSE_ATR = 0.3
MIN_SCORE = 5
MAX_VWAP_ATR_TREND = 2.0
MIN_ROOM_ATR = 1.0
CHASE_ATR = 0.5
SWING_LEFT = 2
SWING_RIGHT = 2
LEVEL_TOL_ATR = 0.05
EVENT_WORDS = ("closed_through", "rejected", "touched", "untouched")

ENTRY_FIELDS = CC.ENTRY_FIELDS


def load_prompt() -> str:
    text = PROMPT_PATH.read_text()
    entry = None
    for head, body in CC._split(text):
        if "BRACKET-V1" in head or "ENTRY SYSTEM PROMPT" in head:
            entry = body
            break
    if not entry:
        # file may be the raw prompt with a leading === header already stripped
        entry = text
    return entry.strip()


def system_prompt(symbols: list[str] | None = None) -> str:
    syms = list(symbols or SYMBOLS)
    return load_prompt().replace("{n}", str(len(syms))).replace("{symbol_order}", ", ".join(syms))


def signal_schema(symbols: list[str] | None = None) -> dict:
    return CC.signal_batch_schema(list(symbols or SYMBOLS))


# ------------------------------------------------------------------ swings + level events

def _bar(payload: dict) -> dict | None:
    ctx = payload.get("context") or {}
    return ctx.get("signal_candle") or ctx.get("last_candle")


def _atr(ctx: dict) -> float:
    try:
        return float(ctx.get("atr") or 0)
    except (TypeError, ValueError):
        return 0.0


def _price(ctx: dict) -> float | None:
    for k in ("live_price", "last_price", "last_close"):
        if ctx.get(k) is not None:
            try:
                return float(ctx[k])
            except (TypeError, ValueError):
                continue
    sc = ctx.get("signal_candle") or {}
    if sc.get("close") is not None:
        return float(sc["close"])
    return None


def swing_points(candles: list[dict], *, left: int = SWING_LEFT, right: int = SWING_RIGHT,
                 last_n: int = 3) -> dict:
    """Confirmed fractal swings: a high (low) that is strictly higher (lower) than
    `left` bars before it and `right` bars after it. The last `right` bars cannot
    confirm, so they are not emitted. Returns the most recent `last_n` of each.
    """
    highs, lows = [], []
    n = len(candles or [])
    for i in range(left, max(left, n - right)):
        row = candles[i]
        try:
            h, l = float(row["high"]), float(row["low"])
        except (TypeError, ValueError, KeyError):
            continue
        window = candles[i - left:i + right + 1]
        others_h, others_l = [], []
        for j, o in enumerate(window):
            if j == left:
                continue
            try:
                others_h.append(float(o["high"]))
                others_l.append(float(o["low"]))
            except (TypeError, ValueError, KeyError):
                pass
        if others_h and all(h > x for x in others_h):
            highs.append({"price": h, "time": row.get("time"), "index": i,
                          "bars_ago": n - 1 - i})
        if others_l and all(l < x for x in others_l):
            lows.append({"price": l, "time": row.get("time"), "index": i,
                         "bars_ago": n - 1 - i})
    return {"swing_highs": highs[-last_n:], "swing_lows": lows[-last_n:]}


def classify_level_event(bar: dict, level: float, atr: float,
                         tol_atr: float = LEVEL_TOL_ATR) -> str:
    """closed_through / rejected / touched / untouched on the last closed bar."""
    try:
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        level = float(level)
    except (TypeError, ValueError, KeyError):
        return "untouched"
    tol = max(float(tol_atr) * atr, 0.0) if atr else 0.0

    def above(x):
        return x > level + tol

    def below(x):
        return x < level - tol

    # Body crossed the level (open one side, close the other).
    if (below(o) and above(c)) or (above(o) and below(c)):
        return "closed_through"
    # Close exactly at the level after opening on one side also counts as through.
    if (below(o) and not below(c) and not above(c) and c >= level) or (
            above(o) and not above(c) and not below(c) and c <= level):
        return "closed_through"
    # Wick pierced (or reached) the level, close back on the open's side.
    # A rejection must actually wick THROUGH the level (beyond tolerance), then
    # close back on the open's side. Merely reaching the tolerance band is a touch.
    pierced_up = h > level + tol
    pierced_dn = l < level - tol
    if below(o) and pierced_up and below(c):
        return "rejected"
    if above(o) and pierced_dn and above(c):
        return "rejected"
    # Touched: the bar's range reached the tolerance band, but no through/reject.
    if l - tol <= level <= h + tol:
        return "touched"
    return "untouched"


def _gex_levels(gex: dict | None) -> list[tuple[str, float]]:
    out = []
    if not gex:
        return out
    if gex.get("gamma_flip") is not None:
        try:
            out.append(("Gamma Flip", float(gex["gamma_flip"])))
        except (TypeError, ValueError):
            pass
    for side, name in (("call_walls", "Call Wall"), ("put_walls", "Put Wall")):
        for w in gex.get(side) or []:
            if not isinstance(w, dict):
                continue
            if w.get("price") is None:
                continue
            try:
                rank = w.get("rank", 1)
                out.append((f"{name} #{rank}", float(w["price"])))
            except (TypeError, ValueError):
                pass
    return out


def key_levels(payload: dict) -> list[tuple[str, float]]:
    """Named levels used for the facts block and the 1 ATR room check."""
    ctx = payload.get("context") or {}
    digits = int(ctx.get("digits") or payload.get("digits") or 5)
    items: list[tuple[str, float]] = []

    def add(label, val):
        if val is None:
            return
        try:
            items.append((label, round(float(val), digits)))
        except (TypeError, ValueError):
            return

    add("VWAP", ctx.get("vwap"))
    add("EMA20", ctx.get("ema20_baseline") if ctx.get("ema20_baseline") is not None else ctx.get("ema20"))
    add("EMA50", ctx.get("ema50"))
    add("POC", ctx.get("poc"))
    add("VAH", ctx.get("vah"))
    add("VAL", ctx.get("val"))
    add("ATR Upper", ctx.get("atr_upper"))
    add("ATR Lower", ctx.get("atr_lower"))
    for lv in ctx.get("levels") or payload.get("levels") or []:
        if not isinstance(lv, dict) or lv.get("price") is None:
            continue
        lab = str(lv.get("label") or lv.get("type") or "level")
        add(lab, lv["price"])
    for lab, px in _gex_levels(ctx.get("gex")):
        add(lab, px)
    # de-dupe identical (label, price)
    seen, uniq = set(), []
    for lab, px in items:
        k = (lab, px)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((lab, px))
    return uniq


def room_to_levels(price: float, atr: float, levels: list[tuple[str, float]]) -> dict:
    above = sorted([(lab, px) for lab, px in levels if px > price], key=lambda x: x[1])
    below = sorted([(lab, px) for lab, px in levels if px < price], key=lambda x: -x[1])
    up = above[0] if above else None
    dn = below[0] if below else None
    def pack(hit):
        if not hit or not atr:
            return None, None, None
        lab, px = hit
        return round(abs(px - price) / atr, 3), lab, px
    up_atr, up_lab, up_px = pack(up)
    dn_atr, dn_lab, dn_px = pack(dn)
    return {"room_up_atr": up_atr, "room_up_level": up_lab, "room_up_price": up_px,
            "room_down_atr": dn_atr, "room_down_level": dn_lab, "room_down_price": dn_px}


def compute_facts(payload: dict) -> dict:
    """VWAP distance (ATR), last 3 swing highs/lows, per-level event on the last closed bar, room."""
    ctx = payload.get("context") or {}
    atr = _atr(ctx)
    price = _price(ctx)
    vwap = ctx.get("vwap")
    try:
        vwap_d = round((float(price) - float(vwap)) / atr, 3) if price is not None and vwap is not None and atr else None
    except (TypeError, ValueError):
        vwap_d = None
    swings = swing_points(payload.get("candles") or [])
    levels = key_levels(payload)
    bar = _bar(payload) or {}
    events = []
    for lab, px in levels:
        ev = classify_level_event(bar, px, atr) if bar else "untouched"
        events.append({"level": lab, "price": px, "event": ev})
    room = room_to_levels(price, atr, levels) if price is not None else {
        "room_up_atr": None, "room_up_level": None, "room_up_price": None,
        "room_down_atr": None, "room_down_level": None, "room_down_price": None}
    rvol = ctx.get("rvol_last")
    return {
        "symbol": payload.get("symbol"),
        "vwap_distance_atr": vwap_d,
        "vwap_side": ("above" if vwap_d is not None and vwap_d > 0 else
                      "below" if vwap_d is not None and vwap_d < 0 else "at" if vwap_d == 0 else None),
        "price": price, "vwap": vwap, "atr": atr,
        "swing_highs": swings["swing_highs"],
        "swing_lows": swings["swing_lows"],
        "level_events": events,
        "any_level_event": any(e["event"] != "untouched" for e in events),
        "rvol_last": rvol,
        "us100_volume_point_awarded": payload.get("symbol") == "US100",
        **room,
    }


def format_facts_block(facts: dict) -> str:
    """Plain-text facts block prepended to each symbol's user message."""
    def fmt_swing(rows, kind):
        if not rows:
            return f"  {kind}: none confirmed"
        bits = []
        for r in rows:
            ba = r.get("bars_ago")
            bits.append(f"{r['price']} ({ba} bars ago)" if ba is not None else str(r["price"]))
        return f"  {kind}: " + ", ".join(bits)

    ev_lines = []
    for e in facts.get("level_events") or []:
        ev_lines.append(f"  - {e['level']} {e['price']}: {e['event']}")
    if not ev_lines:
        ev_lines = ["  - none"]
    vd = facts.get("vwap_distance_atr")
    side = facts.get("vwap_side") or ""
    vd_s = "n/a" if vd is None else f"{vd:+.3f} ({side})"
    ru, rd = facts.get("room_up_atr"), facts.get("room_down_atr")
    ru_s = "n/a" if ru is None else f"{ru:.3f} ATR to {facts.get('room_up_level')} {facts.get('room_up_price')}"
    rd_s = "n/a" if rd is None else f"{rd:.3f} ATR to {facts.get('room_down_level')} {facts.get('room_down_price')}"
    us100 = " (S2 volume point awarded automatically)" if facts.get("us100_volume_point_awarded") else ""
    rvol = facts.get("rvol_last")
    return "\n".join([
        "CODE-COMPUTED FACTS (authoritative; do not re-read these from the chart):",
        f"  vwap_distance_atr: {vd_s}",
        fmt_swing(facts.get("swing_highs") or [], "swing_highs"),
        fmt_swing(facts.get("swing_lows") or [], "swing_lows"),
        "  level_events_last_closed_bar:",
        *ev_lines,
        f"  room_up_atr: {ru_s}",
        f"  room_down_atr: {rd_s}",
        f"  rvol_last: {rvol if rvol is not None else 'n/a'}{us100}",
    ])


# ------------------------------------------------------------------ gates

def fill_adverse(side: int, fill_price: float, chart_price: float, atr: float,
                 limit: float = FILL_MAX_ADVERSE_ATR) -> tuple[bool, float, str | None]:
    """0.3 ATR fill check: skip if the live quote is worse than the chart price by more than limit."""
    if not atr:
        return False, 0.0, "no ATR for fill check"
    adverse = side * (float(fill_price) - float(chart_price))
    ratio = adverse / atr
    if adverse > limit * atr:
        return False, ratio, (f"live quote {ratio:.2f} ATR worse than chart_live_price "
                              f"(> {limit} ATR)")
    return True, ratio, None


def hold_reasons(action: str, setup_type: str, facts: dict, ctx: dict) -> list[str]:
    """Code-side HOLD gates. Empty list means the signal may be traded (score still applies)."""
    reasons = []
    act = (action or "HOLD").upper()
    st = (setup_type or "none").lower()
    if act not in ("BUY", "SELL"):
        return reasons
    if ctx.get("live_price_stale"):
        reasons.append("live price stale")
    if ctx.get("session_blocks_automation"):
        reasons.append("session_blocks_automation")
    if st == "trend":
        vd = facts.get("vwap_distance_atr")
        if vd is not None and abs(float(vd)) > MAX_VWAP_ATR_TREND:
            reasons.append(f"trend chase: |vwap_distance_atr| {abs(float(vd)):.2f} > {MAX_VWAP_ATR_TREND}")
    room = facts.get("room_up_atr") if act == "BUY" else facts.get("room_down_atr")
    if room is None:
        reasons.append("no opposing level in trade direction (room unknown)")
    elif float(room) < MIN_ROOM_ATR:
        reasons.append(f"room {float(room):.2f} ATR < {MIN_ROOM_ATR} ATR in trade direction")
    # 0.5 ATR chase vs signal close
    sc = (ctx.get("signal_candle") or {}).get("close")
    px = _price(ctx)
    atr = _atr(ctx)
    if sc is not None and px is not None and atr:
        side = 1 if act == "BUY" else -1
        ran = side * (float(px) - float(sc)) / atr
        if ran > CHASE_ATR:
            reasons.append(f"live price already {ran:.2f} ATR past signal close (>{CHASE_ATR})")
    return reasons


def apply_gates(signal: dict, facts: dict, ctx: dict, *, min_score: int = MIN_SCORE) -> dict:
    """Validate + enforce hold gates. Mutates a copy; sets gated, gate_reasons, prompt_version."""
    v = CC.validate_entry_signal(signal or {"action": "HOLD"}, ctx, min_score)
    # US100 volume-free point is a scoring instruction to the model (S2 auto-awarded,
    # still out of 7). Code does not impose a volume requirement on any symbol.
    if v.get("symbol") is None:
        v["symbol"] = (signal or {}).get("symbol") or facts.get("symbol")
    reasons = list(v.get("issues") or [])
    extra = hold_reasons(v.get("action") or "HOLD", v.get("setup_type") or "none", facts, ctx)
    reasons.extend(extra)
    if extra and v.get("gated"):
        v["gated"] = False
        v["issues"] = reasons
        v["gate_override"] = "hold_rules"
    else:
        v["issues"] = reasons
    v["gate_reasons"] = extra
    v["prompt_version"] = PROMPT_VERSION
    v["us100_volume_point_awarded"] = bool(facts.get("us100_volume_point_awarded"))
    v["vwap_distance_atr"] = facts.get("vwap_distance_atr")
    v["room_up_atr"] = facts.get("room_up_atr")
    v["room_down_atr"] = facts.get("room_down_atr")
    return v


# ------------------------------------------------------------------ package / hash

def _slim_context(payload: dict) -> dict:
    from .gemini_client import slim_gemini_context
    ctx = payload.get("context") or {}
    out = {"symbol": payload.get("symbol"), "source": f"{payload.get('source')}:{payload.get('ticker')}",
           "timeframe": payload.get("timeframe"), **slim_gemini_context(ctx)}
    return out


def symbol_user_text(payload: dict, facts: dict, index: int, total: int,
                     htf: dict | None = None) -> str:
    ctx = _slim_context(payload)
    if htf:
        d = int((payload.get("context") or {}).get("digits") or 5)
        for k in ("h1_trend", "prior_day_high", "prior_day_low"):
            v = htf.get(k)
            if v is not None:
                ctx[k] = round(v, d) if isinstance(v, float) else v
    raw = payload.get("context") or {}
    price = ctx.get("live_price", ctx.get("last_price", raw.get("last_close")))
    ts = ctx.get("live_price_ts_utc", ctx.get("last_price_ts_utc", ctx.get("last_closed_bar_utc")))
    stale = ctx.get("live_price_stale")
    extra = " (STALE — last closed M15 close)" if stale else ""
    return (f"Chart {index}/{total} - {payload['symbol']}\n"
            f"LIVE PRICE SENT TO MODEL: {price} @ {ts}{extra}\n"
            f"Numeric context (authoritative): {json.dumps(ctx, default=str)}\n"
            f"{format_facts_block(facts)}")


def canonical_parts(payloads: list[dict], charts: dict[str, bytes],
                    facts_by_sym: dict[str, dict], htf: dict | None = None) -> list[dict]:
    """Model-neutral parts: alternating text / image, then a closer. Used to hash and to wrap."""
    parts = []
    n = len(payloads)
    for i, p in enumerate(payloads, 1):
        sym = p["symbol"]
        parts.append({"type": "text", "text": symbol_user_text(p, facts_by_sym[sym], i, n,
                                                               (htf or {}).get(sym))})
        blob = charts.get(sym)
        if blob:
            parts.append({"type": "image", "symbol": sym, "sha256": hashlib.sha256(blob).hexdigest(),
                          "bytes": blob})
    parts.append({"type": "text", "text": "Return the JSON signal batch for all symbols now."})
    return parts


def package_hash(system: str, parts: list[dict], *, bar_close_ts: str | None = None,
                 symbols: list[str] | None = None) -> tuple[str, str]:
    """Stable hash of the text + image bytes the models see. Returns (sha256, snapshot_id)."""
    acc = hashlib.sha256()
    acc.update(b"bracket-v1\n")
    acc.update(system.encode("utf-8"))
    acc.update(b"\n")
    charts = {}
    for p in parts:
        if p.get("type") == "text":
            acc.update(b"T:")
            acc.update(p["text"].encode("utf-8"))
            acc.update(b"\n")
        elif p.get("type") == "image":
            digest = p.get("sha256") or hashlib.sha256(p["bytes"]).hexdigest()
            acc.update(f"I:{p.get('symbol')}:{digest}\n".encode())
            charts[p.get("symbol")] = digest
    digest = acc.hexdigest()
    bar = (bar_close_ts or "")[:19].replace(":", "").replace("-", "")
    sid = f"m15-{bar}-{digest[:12]}" if bar else f"m15-{digest[:16]}"
    return digest, sid


def wrap_anthropic(parts: list[dict]) -> list[dict]:
    out = []
    for p in parts:
        if p.get("type") == "text":
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image" and p.get("bytes"):
            out.append(CC.image_block(p["bytes"]))
    return out


def wrap_openai(parts: list[dict]) -> list[dict]:
    """DeepSeek / OpenAI-compatible multimodal content list."""
    from .gemini_client import _image_mime
    import base64
    out = []
    for p in parts:
        if p.get("type") == "text":
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image" and p.get("bytes"):
            blob = p["bytes"]
            mime = _image_mime(blob)
            b64 = base64.b64encode(blob).decode()
            out.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return out


def schema_closer_openai(symbols: list[str] | None = None) -> str:
    schema = json.dumps(signal_schema(symbols), indent=2)
    return ("Return a single JSON object now, matching this SignalBatch schema exactly "
            "(field names and types unchanged). No markdown, no commentary outside the JSON.\n"
            + schema)


def build_package(payloads: list[dict], charts: dict[str, bytes], *,
                  htf: dict | None = None, bar_close_ts: str | None = None,
                  snapshot_ts: str | None = None) -> dict:
    """One shared package for C-OPUS-B and DS-FLASH. Charts must already be JPEG q78 bytes."""
    wanted = [p for p in payloads if p.get("symbol") in SYMBOLS]
    order = [s for s in SYMBOLS if any(p["symbol"] == s for p in wanted)]
    wanted.sort(key=lambda p: order.index(p["symbol"]) if p["symbol"] in order else 99)
    facts = {p["symbol"]: compute_facts(p) for p in wanted}
    system = system_prompt([p["symbol"] for p in wanted])
    parts = canonical_parts(wanted, charts, facts, htf=htf)
    digest, sid = package_hash(system, parts, bar_close_ts=bar_close_ts,
                               symbols=[p["symbol"] for p in wanted])
    # DeepSeek gets the schema reminder as an extra text part (API has no json_schema tool).
    # The reminder is appended AFTER the hash so the hashed body stays identical for both models.
    ds_parts = list(parts)
    ds_parts.append({"type": "text", "text": schema_closer_openai([p["symbol"] for p in wanted])})
    return {
        "prompt_version": PROMPT_VERSION,
        "reconstruction": PROMPT_RECONSTRUCTION,
        "snapshot_id": sid,
        "snapshot_hash": digest,
        "bar_close_ts": bar_close_ts,
        "snapshot_ts": snapshot_ts,
        "symbols": [p["symbol"] for p in wanted],
        "payloads": wanted,
        "facts": facts,
        "charts": {p["symbol"]: charts[p["symbol"]] for p in wanted if p["symbol"] in charts},
        "chart_sha256": {p["symbol"]: hashlib.sha256(charts[p["symbol"]]).hexdigest()
                         for p in wanted if p["symbol"] in charts},
        "system": system,
        "parts": parts,
        "anthropic_content": wrap_anthropic(parts),
        "openai_messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": wrap_openai(ds_parts)},
        ],
        "schema": signal_schema([p["symbol"] for p in wanted]),
    }


def filter_symbols(payloads: list[dict], busy: set[str] | None = None) -> list[dict]:
    busy = {str(s).upper() for s in (busy or set())}
    return [p for p in payloads if p.get("symbol") in SYMBOLS and p.get("symbol") not in busy]
