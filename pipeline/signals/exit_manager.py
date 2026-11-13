"""Gemini EXIT MANAGER + two paper books (PAPER ONLY - nothing here places an order anywhere).

Every Lucid sim entry (signals/lucid.py -> state["book_feed"]) is copied into two extra paper books with the same
contract / qty / entry / hard stop:

  book "1-strike": the first Gemini CLOSE exits.
  book "2-strike": while MFE < runner_mfe_atr (0.8 M15 ATR) two CONSECUTIVE Gemini CLOSEs are required (~36 s at
                   18 s checks); once MFE >= runner_mfe_atr a single CLOSE exits (protect banked profit).

While any book position is open, ExitManager.tick() runs every `check_seconds` (18 s) beside the 2-minute entry
loop. Each tick, per open position: 1-minute live price (Dukascopy M1, Binance fallback) -> hard-stop / MFE scan on
the M1 bars since the last tick -> the SAME M15 chart as the entry model (all entry indicators + forming bar) plus a
bright ENTRY line -> ONE Gemini call (HOLD/CLOSE) -> applied to BOTH books. Errors / timeouts / unparseable replies
are HOLD (logged). Gemini can only close early: the server hard stop is never moved.

Server-side safety kept in both books: hard stop (inherited from the Lucid sizing, so risk <= $1,500), 16:45 ET
flat, 3.5% daily circuit breaker per book, and a max-hold safety exit. Dropped (Gemini owns the exit): the fixed
take-profit, the 2-bar time cut, and the deterministic 15m VWAP/EMA50 cross exits. The baseline Lucid sim is
untouched, so baseline vs 1-strike vs 2-strike can be compared on identical entries (books.json).

Logs: logs/YYYY-MM-DD/exits.jsonl (every check), logs/YYYY-MM-DD/exit_book_trades.jsonl (every book close),
state/exit_books.json (book state), books.json at the data root (per-book summary + baseline on the same entries).
"""
from __future__ import annotations
import asyncio, contextlib, glob, json, logging, math, os, re, time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, Field

from .config import ROOT
from .risk import _atomic_write
from .exit_versions import (GEMINI_EXIT_PROMPT_VERSION, GEMINI_MECH_EXIT_VERSION,
                            gemini_exit_calls_enabled, gemini_close_tag)
from .uniform_exit import uniform_exits_enabled, UNIFORM_BANNER, arm_uniform, UNIFORM_EXIT_VERSION, close_tag
from .hard_stop import (book_has_stop, beyond as stop_beyond, hard_stop_level, hard_stop_reason, hard_stop_usd,
                        shadow_books)

log = logging.getLogger("signals.exit")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
STATE_VERSION = 1

BOOK_1 = "1-strike"
BOOK_2 = "2-strike"
BOOK_NOSTOP = "nostop"   # shadow: identical entries, Gemini CLOSE only (1-strike rule), never stopped
EXIT_FIELD_ORDER = ("structure", "reason", "decision", "confidence")
STRUCTURES = ("expanding", "healthy_pullback", "wall_touch_harvest", "giveback_limit",
              "closed_bar_invalidated", "spike_hold_wait", "session_decay")
# Prompt placeholders that must always render to a non-empty value (never literal braces).
PROMPT_KEYS = (
    "symbol", "side", "entry", "price", "minutes_into_bar", "pnl_atr", "pnl_usd", "mfe_atr",
    "giveback_pct", "commission_usd", "commission_atr", "net_pnl_usd", "net_pnl_atr",
    "prev_giveback_pct", "giveback_2_ago", "giveback_change_2checks",
    "minutes", "session", "next_wall_label", "next_wall_price", "dist_to_wall_atr",
    "invalid_level_label", "invalid_level_price", "vwap", "ema20", "ema50",
    "last_o", "last_h", "last_l", "last_c", "rvol_last", "form_o", "form_h", "form_l",
    "poc", "vah", "val", "cvd_label", "cvd_slope", "gex_summary",
)
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
GEX_UNAVAILABLE = "unavailable (outside NY cash hours or no coverage)"


# ---------------------------------------------------------------------------------------------- schema / prompt
class ExitDecision(BaseModel):
    """Field order = Gemini propertyOrdering: analysis (structure, reason) BEFORE the decision.

    structure is a free string so an unknown label does not fail the call (error => HOLD only
    applies to missing/invalid decision or unparseable JSON). Known values are listed in STRUCTURES.
    """
    structure: str = Field(description="expanding | healthy_pullback | wall_touch_harvest | giveback_limit | "
                                       "closed_bar_invalidated | spike_hold_wait | session_decay (or other)")
    reason: str = Field(description="must start with [pnl=...ATR, mfe=...ATR, giveback=...%] then the rule "
                                    "trigger (max 22 words total)")
    decision: Literal["HOLD", "CLOSE"]
    confidence: float = Field(description="0..1")


def gemini_only(cfg: dict) -> bool:
    """USER RULE: 100% Gemini - no hard stop, no max hold, no 16:45 flat, breaker log-only."""
    return str((cfg.get("lucid") or {}).get("exit_mode", "")) == "gemini_only"


def em_cfg(cfg: dict) -> dict:
    d = {"enabled": False, "check_seconds": 18, "runner_mfe_atr": 0.8, "wall_reject_atr": 0.10,
         "giveback_pct": 35.0, "giveback_min_mfe_atr": 1.0, "early_minutes": 12, "max_hold_minutes": 90,
         "max_daily_loss_pct": 3.5, "start_balance": 100000.0, "gemini_timeout_seconds": 12,
         "max_feed_age_minutes": 20, "books": [BOOK_1],
         "gemini_exit_calls_enabled": True,
         "mech_target_atr": 1.0, "mech_time_cap_minutes": 90, "mech_nostop_stop_atr": 1.0}
    return {**d, **(cfg.get("exit_manager") or {})}


def _r(x, nd):
    return None if x is None else round(float(x), nd)


def _side_word(side: int) -> str:
    return "LONG" if side > 0 else "SHORT"


def position_metrics(side: int, entry: float, price: float, mfe_pts: float, atr: float,
                     usd_per_point: float, commission_rt: float = 0.0,
                     giveback_hist: list | None = None) -> dict:
    """Open P&L / MFE / giveback. mfe_pts = best favourable excursion in price points seen so far (>= 0).
    giveback_pct = share of the peak open profit handed back (0 when there was never any profit).
    Net P&L is gross minus full round-trip commission. giveback_hist is prior checks' giveback_pct
    (oldest first, last two kept by the caller) so the speed rule can see the ~36s change."""
    pnl_pts = side * (price - entry)
    mfe = max(float(mfe_pts or 0.0), pnl_pts, 0.0)
    give = (mfe - pnl_pts) / mfe * 100.0 if mfe > 0 else 0.0
    a = float(atr) if atr and atr > 0 else float("nan")
    unit = (a * float(usd_per_point)) if a == a else None
    pnl_usd = round(pnl_pts * usd_per_point, 2)
    comm = round(float(commission_rt or 0.0), 2)
    net_usd = round(pnl_usd - comm, 2)
    hist = [float(x) for x in (giveback_hist or []) if x is not None]
    prev = hist[-1] if len(hist) >= 1 else None
    ago2 = hist[-2] if len(hist) >= 2 else None
    gb = round(max(0.0, give), 1)
    baseline = ago2 if ago2 is not None else prev
    change = round(gb - baseline, 1) if baseline is not None else None
    return {"pnl_pts": pnl_pts, "mfe_pts": mfe,
            "pnl_atr": round(pnl_pts / a, 3) if a == a else None,
            "mfe_atr": round(mfe / a, 3) if a == a else None,
            "giveback_pct": gb,
            "pnl_usd": pnl_usd,
            "commission_usd": comm,
            "commission_atr": round(comm / unit, 3) if unit else None,
            "net_pnl_usd": net_usd,
            "net_pnl_atr": round(net_usd / unit, 3) if unit else None,
            "prev_giveback_pct": prev,
            "giveback_2_ago": ago2,
            "giveback_change_2checks": change}


def round_trip_commission(cfg: dict, pos: dict) -> float:
    """Full entry+exit commission in USD for this contract/qty (or pos['commission_rt'] if set)."""
    if pos.get("commission_rt") is not None:
        return round(float(pos["commission_rt"]), 2)
    c = ((cfg.get("lucid") or {}).get("contracts") or {}).get(pos.get("contract") or "")
    if not c:
        return 0.0
    return round(2 * float(c.get("commission") or 0) * int(pos.get("qty") or 0), 2)


def usd_per_point_of(cfg: dict, pos: dict) -> float | None:
    c = ((cfg.get("lucid") or {}).get("contracts") or {}).get(pos.get("contract") or "")
    if not c:
        return None
    return float(c["point_value"]) * int(pos.get("qty") or 0)


def giveback_limit_pct(mfe_atr: float | None) -> float | None:
    """Giveback CLOSE threshold from peak MFE alone. None if MFE has not reached 0.25 ATR."""
    if mfe_atr is None or float(mfe_atr) < 0.25:
        return None
    if float(mfe_atr) < 0.45:
        return 50.0
    if float(mfe_atr) < 0.85:
        return 40.0
    return 30.0


def profit_lock_flags(*, mfe_atr, net_pnl_atr, giveback_pct,
                      prev_giveback_pct=None, giveback_2_ago=None) -> list[str]:
    """Deterministic offline CLOSE flags for profit-lock / giveback / speed rules.

    This is a rule replay helper only — it does not close live trades (Gemini still decides).
    """
    flags: list[str] = []
    if mfe_atr is None or float(mfe_atr) < 0.25:
        return flags
    if net_pnl_atr is not None and float(net_pnl_atr) <= 0.03 + 1e-12:
        flags.append("profit_lock")
    lim = giveback_limit_pct(mfe_atr)
    if lim is not None and giveback_pct is not None and float(giveback_pct) >= lim - 1e-12:
        flags.append("giveback_limit")
    baseline = giveback_2_ago if giveback_2_ago is not None else prev_giveback_pct
    if (baseline is not None and giveback_pct is not None
            and (float(giveback_pct) - float(baseline)) >= 40.0 - 1e-12):
        flags.append("speed_rule")
    return flags


def enrich_exit_metrics(m: dict, cfg: dict, pos: dict) -> dict:
    """Fill commission / net P&L / previous-giveback on an existing metrics dict (mutates m)."""
    comm = round_trip_commission(cfg, pos)
    upp = usd_per_point_of(cfg, pos)
    a = float(pos.get("atr") or 0)
    unit = (a * upp) if (a > 0 and upp) else None
    if unit is None and m.get("pnl_atr") not in (None, 0) and m.get("pnl_usd") not in (None, 0):
        unit = m["pnl_usd"] / m["pnl_atr"]
    m["commission_usd"] = comm
    m["commission_atr"] = round(comm / unit, 3) if unit else None
    net_usd = round(float(m.get("pnl_usd") or 0.0) - comm, 2)
    m["net_pnl_usd"] = net_usd
    m["net_pnl_atr"] = round(net_usd / unit, 3) if unit else None
    hist = [float(x) for x in (pos.get("giveback_hist") or []) if x is not None]
    m["prev_giveback_pct"] = hist[-1] if len(hist) >= 1 else None
    m["giveback_2_ago"] = hist[-2] if len(hist) >= 2 else None
    baseline = m["giveback_2_ago"] if m["giveback_2_ago"] is not None else m["prev_giveback_pct"]
    gb = m.get("giveback_pct")
    m["giveback_change_2checks"] = (round(gb - baseline, 1)
                                    if baseline is not None and gb is not None else None)
    return m


PIN_WALLS = ("Call Wall #1", "Put Wall #1")


def gex_candidates(gex: dict | None) -> list[tuple[str, float]]:
    """FirmTape walls / flip / NY opening range (already on the chart's price scale) as wall candidates."""
    if not gex:
        return []
    out = []
    for side, name in (("call_walls", "Call Wall"), ("put_walls", "Put Wall")):
        for w in gex.get(side) or []:
            if w.get("price") is not None:
                out.append((f"{name} #{w.get('rank', 1)}", float(w["price"])))
    if gex.get("gamma_flip") is not None:
        out.append(("Gamma Flip", float(gex["gamma_flip"])))
    orr = gex.get("ny_opening_range") or {}
    for k, lbl in (("high", "NY OR High"), ("low", "NY OR Low")):
        if orr.get(k) is not None:
            out.append((lbl, float(orr[k])))
    return out


def find_walls(ctx: dict, side: int, price: float, entry: float, atr: float, candle: dict | None = None) -> dict:
    """Nearest target wall IN the trade direction (beyond the live price) and the nearest invalidating level
    AGAINST the trade (beyond the worse of entry / live price). Candidates: M15 S/R + Session/Prev/24h H/L + VP
    POC/VAH/VAL. Fallbacks: the +/-2 ATR band for the wall, the signal-candle extreme for invalidation.
    A session extreme just printed by the current move (last closed / forming bar) is not used as the target."""
    cands = [(str(lv.get("label") or lv.get("type")), float(lv["price"])) for lv in (ctx.get("levels") or [])
             if lv.get("price") is not None]
    for k in ("poc", "vah", "val"):
        v = ctx.get(k)
        if v:
            cands.append((k.upper(), float(v)))
    cands += gex_candidates(ctx.get("gex"))
    # A Session High (long) / Session Low (short) printed by the last closed or the forming M15 bar is the
    # current move's own price discovery, not a wall: skip it as a TARGET (the next wall beyond is used).
    sc = ctx.get("signal_candle") or {}
    fresh_ext = [x for x in ((sc.get("high"), ctx.get("forming_m15_high")) if side > 0 else
                             (sc.get("low"), ctx.get("forming_m15_low"))) if x is not None]
    tol = 0.02 * atr if atr else 0.0
    own = "Session High" if side > 0 else "Session Low"
    tgt_cands = [c for c in cands if not (c[0] == own and any(abs(c[1] - x) <= tol for x in fresh_ext))]
    if side > 0:
        ahead = [c for c in tgt_cands if c[1] > price]
        band = ctx.get("atr_upper")
        wall = min(ahead, key=lambda c: c[1]) if ahead else (
            ("+2 ATR band", band) if band is not None and band > price else ("none (price discovery)", None))
        ref = min(entry, price)
        behind = [c for c in cands if c[1] < ref]
        inval = max(behind, key=lambda c: c[1]) if behind else (
            "Signal-candle low", (candle or {}).get("low"))
    else:
        ahead = [c for c in tgt_cands if c[1] < price]
        band = ctx.get("atr_lower")
        wall = max(ahead, key=lambda c: c[1]) if ahead else (
            ("-2 ATR band", band) if band is not None and band < price else ("none (price discovery)", None))
        ref = max(entry, price)
        behind = [c for c in cands if c[1] > ref]
        inval = min(behind, key=lambda c: c[1]) if behind else (
            "Signal-candle high", (candle or {}).get("high"))
    dist = (abs(float(wall[1]) - price) / atr) if (wall[1] is not None and atr) else None
    return {"next_wall_label": wall[0], "next_wall_price": wall[1],
            "dist_to_wall_atr": None if dist is None else round(dist, 2),
            "invalid_level_label": inval[0], "invalid_level_price": inval[1]}


def _filled(value, fallback="n/a") -> str:
    """Never return empty or None — placeholders must always show a real token."""
    if value is None:
        return fallback
    s = str(value).strip()
    return s if s else fallback


def _fmt_px(x, digits: int) -> str:
    return "n/a" if x is None else f"{float(x):.{digits}f}"


def _gex_fmt(gex: dict | None, fmt) -> dict:
    if not gex:
        return {}
    w = lambda ws: ", ".join(f"#{x.get('rank')} {fmt(x.get('price'))}"
                             + (f" (str {x['strength']:.2f})" if isinstance(x.get("strength"), (int, float)) else "")
                             + (" [OI]" if x.get("oi_aligned") else "") for x in ws or []) or "n/a"
    orr = gex.get("ny_opening_range")
    return {"flip": fmt(gex.get("gamma_flip")), "calls": w(gex.get("call_walls")), "puts": w(gex.get("put_walls")),
            "orr": f"{fmt(orr['high'])} / {fmt(orr['low'])}" if orr else None}


def gex_summary_text(gex: dict | None, fmt) -> str:
    """One-line GEX context for the prompt. 'unavailable' outside NY hours / no coverage."""
    if not gex:
        return GEX_UNAVAILABLE
    bits = _gex_fmt(gex, fmt)
    parts = []
    if gex.get("regime"):
        parts.append(str(gex["regime"]))
    if gex.get("gamma_flip") is not None:
        vs = gex.get("price_vs_flip")
        parts.append(f"Gamma Flip {bits['flip']}" + (f" (price {vs})" if vs else ""))
    if bits.get("calls") and bits["calls"] != "n/a":
        parts.append(f"Call Walls {bits['calls']}")
    if bits.get("puts") and bits["puts"] != "n/a":
        parts.append(f"Put Walls {bits['puts']}")
    if bits.get("orr"):
        parts.append(f"NY OR {bits['orr']}")
    return " | ".join(parts) if parts else GEX_UNAVAILABLE


def exit_prompt_values(cfg: dict, pos: dict, ctx: dict, m: dict, walls: dict, now: datetime) -> dict:
    """Every PROMPT_KEYS entry as a non-empty string, ready to substitute into the template."""
    d = int(ctx.get("digits") or 5)
    price = ctx.get("live_price") if ctx.get("live_price") is not None else ctx.get("last_close")
    sc = ctx.get("signal_candle") or {}
    minutes = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 60.0
    mib = ctx.get("minutes_into_bar")
    enrich_exit_metrics(m, cfg, pos)
    vals = {
        "symbol": _filled(pos.get("symbol")),
        "side": _side_word(pos["side"]),
        "entry": _fmt_px(pos.get("entry"), d),
        "price": _fmt_px(price, d),
        "minutes_into_bar": _filled(f"{float(mib):.1f}" if mib is not None else None),
        "pnl_atr": f"{m['pnl_atr']:+.2f}" if m.get("pnl_atr") is not None else "n/a",
        "pnl_usd": f"{m['pnl_usd']:+,.2f}" if m.get("pnl_usd") is not None else "n/a",
        "mfe_atr": f"{m['mfe_atr']:.2f}" if m.get("mfe_atr") is not None else "n/a",
        "giveback_pct": f"{m['giveback_pct']:.0f}" if m.get("giveback_pct") is not None else "n/a",
        "commission_usd": f"{m['commission_usd']:,.2f}" if m.get("commission_usd") is not None else "n/a",
        "commission_atr": f"{m['commission_atr']:.3f}" if m.get("commission_atr") is not None else "n/a",
        "net_pnl_usd": f"{m['net_pnl_usd']:+,.2f}" if m.get("net_pnl_usd") is not None else "n/a",
        "net_pnl_atr": f"{m['net_pnl_atr']:+.3f}" if m.get("net_pnl_atr") is not None else "n/a",
        "prev_giveback_pct": (f"{m['prev_giveback_pct']:.1f}"
                              if m.get("prev_giveback_pct") is not None else "n/a"),
        "giveback_2_ago": (f"{m['giveback_2_ago']:.1f}"
                           if m.get("giveback_2_ago") is not None else "n/a"),
        "giveback_change_2checks": (f"{m['giveback_change_2checks']:+.1f}"
                                    if m.get("giveback_change_2checks") is not None else "n/a"),
        "minutes": f"{minutes:.1f}",
        "session": _filled(ctx.get("session")),
        "next_wall_label": _filled(walls.get("next_wall_label")),
        "next_wall_price": _fmt_px(walls.get("next_wall_price"), d),
        "dist_to_wall_atr": (f"{walls['dist_to_wall_atr']:.2f}"
                             if walls.get("dist_to_wall_atr") is not None else "n/a"),
        "invalid_level_label": _filled(walls.get("invalid_level_label")),
        "invalid_level_price": _fmt_px(walls.get("invalid_level_price"), d),
        "vwap": _fmt_px(ctx.get("vwap"), d),
        "ema20": _fmt_px(ctx.get("ema20_baseline"), d),
        "ema50": _fmt_px(ctx.get("ema50"), d),
        "last_o": _fmt_px(sc.get("open"), d),
        "last_h": _fmt_px(sc.get("high"), d),
        "last_l": _fmt_px(sc.get("low"), d),
        "last_c": _fmt_px(sc.get("close"), d),
        "rvol_last": _filled(ctx.get("rvol_last")),
        "form_o": _fmt_px(ctx.get("forming_m15_open"), d),
        "form_h": _fmt_px(ctx.get("forming_m15_high"), d),
        "form_l": _fmt_px(ctx.get("forming_m15_low"), d),
        "poc": _fmt_px(ctx.get("poc"), d),
        "vah": _fmt_px(ctx.get("vah"), d),
        "val": _fmt_px(ctx.get("val"), d),
        "cvd_label": _filled(ctx.get("cvd_label")),
        "cvd_slope": _filled(ctx.get("cvd_slope")),
        "gex_summary": gex_summary_text(ctx.get("gex"), lambda x: _fmt_px(x, d)),
    }
    for k in PROMPT_KEYS:
        vals[k] = _filled(vals.get(k))
    return vals


def render_exit_prompt(values: dict) -> str:
    """Substitute {name} placeholders. Raises if any key is missing, empty, or left as literal braces."""
    tmpl = (ROOT / "templates" / "exit_prompt.j2").read_text()
    missing = [k for k in PROMPT_KEYS if k not in values]
    if missing:
        raise KeyError(f"unwired exit-prompt placeholders: {missing}")
    def repl(m):
        k = m.group(1)
        if k not in values:
            raise KeyError(f"unwired exit-prompt placeholder {{{k}}}")
        v = values[k]
        if v is None or str(v).strip() == "":
            raise ValueError(f"empty exit-prompt placeholder {{{k}}}")
        return str(v)
    out = _PLACEHOLDER_RE.sub(repl, tmpl)
    leftover = _PLACEHOLDER_RE.findall(out)
    if leftover:
        raise ValueError(f"unsubstituted exit-prompt placeholders: {leftover}")
    return out


def build_exit_prompt(cfg: dict, pos: dict, ctx: dict, m: dict, walls: dict, now: datetime) -> str:
    """Render templates/exit_prompt.j2 with live telemetry in every placeholder."""
    return render_exit_prompt(exit_prompt_values(cfg, pos, ctx, m, walls, now))


def exit_user_text(pos: dict) -> str:
    return (f"Chart: {pos['symbol']} M15 (entry-model chart + ENTRY line) for the open {_side_word(pos['side'])} "
            f"position #{pos['id']}. Return the JSON exit decision now.")


def parse_decision(raw: str | None) -> tuple[dict | None, str | None]:
    if not raw:
        return None, "empty response"
    txt = raw.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        txt = txt[txt.find("{"):]
    try:
        obj = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        d = ExitDecision.model_validate(obj).model_dump()
        d["confidence"] = max(0.0, min(1.0, float(d["confidence"])))
        return d, None
    except Exception as ex:
        return None, f"{type(ex).__name__}: {str(ex)[:200]}"


def mock_decision(m: dict) -> dict:
    """Deterministic stand-in for --dry-run / tests."""
    if (m.get("pnl_atr") or 0) <= -0.5:
        return {"structure": "support_broken", "reason": "mock: underwater beyond 0.5 ATR", "decision": "CLOSE",
                "confidence": 0.7}
    if (m.get("mfe_atr") or 0) >= 1.0 and (m.get("giveback_pct") or 0) >= 35:
        return {"structure": "chopping", "reason": "mock: giveback >= 35% after 1 ATR", "decision": "CLOSE",
                "confidence": 0.7}
    return {"structure": "healthy_pullback", "reason": "mock: hold", "decision": "HOLD", "confidence": 0.6}


# ---------------------------------------------------------------------------------------------- strike logic
def required_strikes(book: str) -> int:
    return 2 if book == BOOK_2 else 1


def apply_strike(book: str, strikes: int, decision: str, mfe_atr: float | None, runner_mfe_atr: float) -> dict:
    """Pure strike rule. HOLD (incl. errors, which are HOLD) resets the streak.
    1-strike: any CLOSE exits. 2-strike: CLOSE exits at once when MFE >= runner_mfe_atr, else on the 2nd
    consecutive CLOSE."""
    if decision != "CLOSE":
        return {"strikes_before": strikes, "strikes_after": 0, "close": False, "rule": "hold"}
    after = strikes + 1
    if required_strikes(book) == 1:
        return {"strikes_before": strikes, "strikes_after": after, "close": True, "rule": "1-strike"}
    if mfe_atr is not None and mfe_atr >= runner_mfe_atr:
        return {"strikes_before": strikes, "strikes_after": after, "close": True, "rule": "runner_single_close"}
    return {"strikes_before": strikes, "strikes_after": after, "close": after >= 2,
            "rule": "2nd_consecutive_close" if after >= 2 else "strike_1_of_2"}


# ---------------------------------------------------------------------------------------------- calendar
def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def lucid_in_session(t: datetime, cfg: dict) -> bool:
    """Same calendar as LucidSim.in_session: 18:00 ET open -> 16:45 ET flat, Mon-Fri trading days."""
    L = cfg.get("lucid") or {}
    open_t, flat_t = _hm(L.get("session_open_et", "18:00")), _hm(L.get("flat_by_et", "16:45"))
    et = t.astimezone(ET)
    d = (et + timedelta(days=1)).date() if et.time() >= open_t else et.date()
    if d.weekday() >= 5:
        return False
    return not (et.date() == d and et.time() >= flat_t)


# ---------------------------------------------------------------------------------------------- books
class Books:
    """Pure accounting for the Gemini-managed paper books (no I/O besides the caller's save/log hooks)."""

    def __init__(self, cfg: dict, state: dict | None = None):
        self.cfg = cfg
        self.e = em_cfg(cfg)
        self.L = cfg["lucid"]
        self.tz = ZoneInfo(cfg["general"]["session_tz"])
        self.s = state or self.fresh()
        self.retired_now: list[str] = []
        # USER RULE (2026-11-12 ~17:45 Dubai, t210u): books removed from [exit_manager].books are RETIRED, not
        # deleted: moved (balance, stats, open positions as they were) into s["retired_books"] and never checked
        # again, so they cost no Gemini exit calls. Their logs stay in logs/<day>/exit_book_trades.jsonl.
        for b in [k for k in list(self.s.get("books", {})) if k not in self.e["books"]]:
            rb = self.s["books"].pop(b)
            rb["retired_at"] = datetime.now(UTC).isoformat()
            rb["retired_open_positions"] = [{k: p.get(k) for k in ("id", "symbol", "side", "qty", "entry", "entry_time")}
                                            for p in rb.get("positions") or []]
            self.s.setdefault("retired_books", {})[b] = rb
            self.retired_now.append(b)
            log.info("exit books: retired book %s (state archived, no more exit checks)", b)
        for b in self.e["books"]:
            if b in self.s["books"]:
                continue
            if b in shadow_books(cfg) and state is not None and BOOK_1 in self.s["books"]:
                # Added mid-run (hard-stop deploy): every earlier trade was already no-stop / Gemini-only, so the
                # shadow continues the 1-strike book (balance, stats, open positions) and simply never stops.
                self.s["books"][b] = self._clone_shadow(self.s["books"][BOOK_1], b)
            else:
                self.s["books"][b] = self._new_book()
        self.closed: list[dict] = []       # closes produced since the caller last drained them

    def _new_book(self) -> dict:
        sb = float(self.e["start_balance"])
        return {"balance": sb, "positions": [], "day": {"id": None, "start_balance": sb, "realized": 0.0,
                                                        "halted": None},
                "stats": {"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0,
                          "net_pnl": 0.0, "commissions": 0.0, "hold_seconds": 0, "exit_reasons": {}}}

    @staticmethod
    def _clone_shadow(src: dict, name: str) -> dict:
        import copy
        b = copy.deepcopy(src)
        for p in b["positions"]:
            p["book"] = name
            for k in ("hard_stop_px", "hard_stop_armed_at", "hard_stop_armed_late"):
                p.pop(k, None)
        b["cloned_from"] = {"book": BOOK_1, "at": datetime.now(UTC).isoformat(), "balance": src.get("balance"),
                            "trades": (src.get("stats") or {}).get("trades"),
                            "open_ids": [p["id"] for p in src.get("positions") or []]}
        return b

    def has_stop(self, name: str) -> bool:
        return book_has_stop(self.cfg, name)

    def arm_mech_exits(self, name: str, p: dict, now: datetime, *, late: bool = False) -> None:
        """Uniform 1 ATR stop + 1 ATR target on every book (or the older nostop-only stop). Late-arm from now."""
        atr = float(p.get("atr") or 0)
        if atr <= 0:
            return
        tick = self.tick(p)
        if uniform_exits_enabled(self.cfg):
            if late and p.get("uniform_exit") == UNIFORM_EXIT_VERSION and p.get("stop") is not None and p.get("target") is not None:
                return
            arm_uniform(p, now, late=late, tick=tick)
            return
        tgt_mult = float(self.e.get("mech_target_atr", 1.0))
        if p.get("target") is None:
            p["target"] = self._round_tick(p["entry"] + p["side"] * tgt_mult * atr, tick)
            p["mech_target_atr"] = tgt_mult
            if late:
                p["mech_armed_at"] = now.isoformat()
        if (not self.has_stop(name)) and p.get("stop") is None:
            sm = float(self.e.get("mech_nostop_stop_atr", 1.0))
            p["stop"] = self._round_tick(p["entry"] - p["side"] * sm * atr, tick)
            p["stop_note"] = "mech_1atr"
            if late:
                p["mech_stop_armed_at"] = now.isoformat()

    def arm_hard_stop(self, name: str, p: dict, now: datetime) -> float | None:
        """Attach the -$N NET hard stop to a book position (no-op for shadow books / stop off / uniform 1 ATR)."""
        if uniform_exits_enabled(self.cfg) or not self.has_stop(name) or p.get("stop") is not None:
            return None
        if p.get("hard_stop_px") is None:
            p["hard_stop_px"] = hard_stop_level(self.cfg, p)
        if p.get("hard_stop_armed_at") is None:
            p["hard_stop_armed_at"] = now.isoformat()
            p["hard_stop_armed_late"] = True
        return p["hard_stop_px"]

    def fresh(self) -> dict:
        return {"version": STATE_VERSION, "created_at": datetime.now(UTC).isoformat(), "seen_ids": [],
                "last_price": {}, "books": {}}

    # -- helpers
    def spec(self, c: str) -> dict:
        return self.L["contracts"][c]

    def usd_per_point(self, p: dict) -> float:
        return self.spec(p["contract"])["point_value"] * p["qty"]

    def tick(self, p: dict) -> float:
        return self.spec(p["contract"])["tick"]

    def _round_tick(self, x: float, tick: float) -> float:
        return round(round(x / tick) * tick, 10)

    def slip_exit(self, p: dict, price: float) -> float:
        n = self.L.get("slippage_ticks", 0)
        return self._round_tick(price - p["side"] * n * self.tick(p), self.tick(p))

    def open_ids(self) -> set[int]:
        return {p["id"] for b in self.s["books"].values() for p in b["positions"]}

    def positions_for(self, pid: int) -> dict[str, dict]:
        return {name: p for name, b in self.s["books"].items() for p in b["positions"] if p["id"] == pid}

    def roll_day(self, t: datetime) -> None:
        did = t.astimezone(self.tz).date().isoformat()
        for b in self.s["books"].values():
            if b["day"]["id"] != did:
                b["day"] = {"id": did, "start_balance": b["balance"], "realized": 0.0, "halted": None}

    # -- intake
    def sync_feed(self, feed: list[dict], now: datetime, adopt_ids: set | None = None) -> list[dict]:
        """Open every not-yet-seen Lucid entry in each (non-halted) book. Entries older than max_feed_age_minutes
        when first seen are marked seen but not opened (e.g. the first tick after a deploy)."""
        self.roll_day(now)
        seen = set(self.s["seen_ids"])
        opened = []
        for f in feed or []:
            if f["id"] in seen:
                continue
            seen.add(f["id"])
            age = (now - datetime.fromisoformat(f["entry_time"])).total_seconds() / 60.0
            if age > self.e["max_feed_age_minutes"] and f["id"] not in (adopt_ids or set()):
                log.info("exit books: entry #%s is %.0f min old -> not copied", f["id"], age)
                continue
            for name, b in self.s["books"].items():
                if b["day"]["halted"]:
                    continue
                p = {**f, "book": name, "strikes": 0, "mfe": 0.0, "mae": 0.0, "scan_from": f["entry_time"],
                     "checks": 0, "opened_at": now.isoformat()}
                if self.has_stop(name):
                    if p.get("stop") is None:
                        p["hard_stop_px"] = f.get("hard_stop_px") or hard_stop_level(self.cfg, p)
                        p["hard_stop_armed_at"] = f.get("hard_stop_armed_at") or f["entry_time"]
                else:
                    for k in ("hard_stop_px", "hard_stop_armed_at"):
                        p.pop(k, None)
                    # Shadow re-entry: main was stopped and re-entered while the shadow still holds this symbol.
                    # The pre-change sim could not have taken it (one per symbol) -> tagged, summarised apart.
                    if any(q["symbol"] == f["symbol"] for q in b["positions"]):
                        p["overlap"] = True
                if not gemini_exit_calls_enabled(self.cfg):
                    self.arm_mech_exits(name, p, now, late=False)
                comm = round(self.spec(p["contract"])["commission"] * p["qty"], 2)
                b["balance"] = round(b["balance"] - comm, 2)
                b["stats"]["commissions"] = round(b["stats"]["commissions"] + comm, 2)
                b["positions"].append(p)
                opened.append({"book": name, "id": f["id"], "symbol": f["symbol"]})
        self.s["seen_ids"] = sorted(seen)[-500:]
        return opened

    # -- price path
    def scan_m1(self, p: dict, bars: pd.DataFrame | None) -> tuple | None:
        """Update MFE/MAE from M1 bars since the last scan and detect a stop or (mech) target hit.
        Returns (fill_price, bar_time, reason) on a hit (gap fills at the bar open), else None."""
        if bars is None or not len(bars):
            return None
        start = pd.Timestamp(datetime.fromisoformat(p["scan_from"])).floor("min")
        sd = p["side"]
        level = p.get("stop") if p.get("stop") is not None else p.get("hard_stop_px")
        armed = None
        if p.get("stop") is None and p.get("hard_stop_armed_at"):
            armed = pd.Timestamp(datetime.fromisoformat(p["hard_stop_armed_at"])).ceil("min")
        elif p.get("mech_stop_armed_at"):
            armed = pd.Timestamp(datetime.fromisoformat(p["mech_stop_armed_at"])).ceil("min")
        mech_arm = None
        if p.get("mech_armed_at"):
            mech_arm = pd.Timestamp(datetime.fromisoformat(p["mech_armed_at"])).ceil("min")
        # Only scan the 1 ATR mechanical target when Gemini exit calls are paused.
        # Positions may carry a leftover Lucid `target` in Gemini mode; do not stop those out on M1.
        target = p.get("target") if not gemini_exit_calls_enabled(self.cfg) else None
        last_ts = None
        # A live-quote fill lands inside its minute (entry_time hh:mm:ss): that bar's range is mostly pre-entry,
        # so it never feeds MFE/MAE or the stop (the 18 s live-price checks cover the rest of the minute).
        entered = pd.Timestamp(datetime.fromisoformat(p["entry_time"])) if p.get("entry_time") else None
        for ts, row in bars[bars.index >= start].iterrows():
            if entered is not None and ts < entered:
                last_ts = ts
                continue
            o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
            fav, adv = (h, l) if sd > 0 else (l, h)
            p["mfe"] = max(float(p.get("mfe") or 0.0), max(0.0, sd * (fav - p["entry"])))
            p["mae"] = max(float(p.get("mae") or 0.0), max(0.0, -sd * (adv - p["entry"])))
            last_ts = ts
            if level is not None and (armed is None or ts >= armed):
                hit = (l <= level) if sd > 0 else (h >= level)
                if hit:
                    gap = (o <= level) if sd > 0 else (o >= level)
                    why = "stop_1atr" if p.get("stop_note") in ("mech_1atr", "uniform_1atr") else (
                        "hard_stop" if p.get("stop") is not None else "hs")
                    return self.slip_exit(p, o if gap else level), ts.to_pydatetime(), why
            if target is not None and (mech_arm is None or ts >= mech_arm):
                tgt_hit = (h >= target) if sd > 0 else (l <= target)
                if tgt_hit:
                    gap = (o >= target) if sd > 0 else (o <= target)
                    return self.slip_exit(p, o if gap else target), ts.to_pydatetime(), "target"
        if last_ts is not None:
            p["scan_from"] = last_ts.isoformat()   # re-scan the (possibly forming) last minute next time
        return None

    # -- exits
    def close(self, name: str, p: dict, price: float, t: datetime, reason: str, extra: dict | None = None) -> dict:
        b = self.s["books"][name]
        comm = round(self.spec(p["contract"])["commission"] * p["qty"], 2)
        gross = round(p["side"] * (price - p["entry"]) * self.usd_per_point(p), 2)
        net = round(gross - 2 * comm, 2)
        b["balance"] = round(b["balance"] + gross - comm, 2)
        st = b["stats"]
        st["trades"] += 1
        st["commissions"] = round(st["commissions"] + comm, 2)
        st["net_pnl"] = round(st["net_pnl"] + net, 2)
        if net > 0:
            st["wins"] += 1; st["gross_win"] = round(st["gross_win"] + net, 2)
        else:
            st["losses"] += 1; st["gross_loss"] = round(st["gross_loss"] + net, 2)
        hold = int((t - datetime.fromisoformat(p["entry_time"])).total_seconds())
        st["hold_seconds"] += max(0, hold)
        st["exit_reasons"][reason] = st["exit_reasons"].get(reason, 0) + 1
        if p.get("overlap"):
            st["overlap_trades"] = st.get("overlap_trades", 0) + 1
            st["overlap_net"] = round(st.get("overlap_net", 0.0) + net, 2)
        b["day"]["realized"] = round(b["day"]["realized"] + net, 2)
        b["positions"] = [q for q in b["positions"] if q["id"] != p["id"]]
        atr = p.get("atr") or 0
        rec = {"t_exit": t.astimezone(UTC).isoformat(timespec="microseconds"),
               "t_exit_dubai": t.astimezone(self.tz).isoformat(timespec="microseconds"),
               "book": name, "id": p["id"], "symbol": p["symbol"], "contract": p["contract"], "qty": p["qty"],
               "side": _side_word(p["side"]), "entry_time": p["entry_time"], "entry": p["entry"], "stop": p["stop"],
               "exit": round(price, 6), "reason": reason, "gross": gross, "commission": round(2 * comm, 2),
               "net": net, "hold_seconds": hold, "balance_after": b["balance"], "risk_usd": p.get("risk_usd"),
               "mfe_atr": round(p.get("mfe", 0) / atr, 3) if atr else None,
               "mae_atr": round(p.get("mae", 0) / atr, 3) if atr else None,
               "mfe_usd": round(p.get("mfe", 0) * self.usd_per_point(p), 2),
               "checks": p.get("checks", 0), "strikes_at_exit": p.get("strikes", 0),
               "hard_stop_px": p.get("hard_stop_px"), "overlap": bool(p.get("overlap")),
               "exit_prompt_version": gemini_close_tag(self.cfg),
               "exit_mode": ("gemini" if gemini_exit_calls_enabled(self.cfg) else "mechanical"),
               **(extra or {})}
        rec["event"] = "close"
        self.closed.append(rec)
        self.queue_post_exit(rec)
        return rec

    # -- post-exit tracking (5 min after every close: books AND the baseline sim)
    def queue_post_exit(self, rec: dict, minutes: float = 5.0) -> None:
        t = datetime.fromisoformat(rec["t_exit"])
        self.s.setdefault("post_exit", []).append(
            {"book": rec["book"], "id": rec["id"], "symbol": rec["symbol"], "contract": rec["contract"],
             "qty": rec["qty"], "side": rec["side"], "exit": rec["exit"], "t_exit": rec["t_exit"],
             "reason": rec.get("reason"), "net": rec.get("net"),
             "due": (t + timedelta(minutes=minutes)).isoformat()})

    def breaker(self, t: datetime) -> list[dict]:
        """3.5% daily loss per book (realized + open at last prices vs start-of-day balance) -> flatten + halt."""
        out = []
        lim = float(self.e["max_daily_loss_pct"]) / 100.0
        for name, b in self.s["books"].items():
            if b["day"]["halted"]:
                continue
            eq = b["balance"] + sum(p["side"] * (self.s["last_price"].get(p["symbol"], p["entry"]) - p["entry"])
                                    * self.usd_per_point(p) for p in b["positions"])
            if eq <= b["day"]["start_balance"] * (1 - lim) + 1e-9 and gemini_only(self.cfg):
                if not b["day"].get("breach_logged"):
                    b["day"]["breach_logged"] = {"t": t.isoformat(), "equity": round(eq, 2),
                                                 "note": f"{self.e['max_daily_loss_pct']}% breaker breached (log-only)"}
                    out.append({"event": "breaker_breach_logged", "book": name, **b["day"]["breach_logged"]})
                continue
            if eq <= b["day"]["start_balance"] * (1 - lim) + 1e-9:
                for p in list(b["positions"]):
                    px = self.slip_exit(p, self.s["last_price"].get(p["symbol"], p["entry"]))
                    out.append(self.close(name, p, px, t, "circuit_breaker_3.5pct"))
                b["day"]["halted"] = f"daily loss >= {self.e['max_daily_loss_pct']}% -> flat, no new entries today"
        return out

    def summary(self) -> dict:
        out = {}
        for name, b in self.s["books"].items():
            st = b["stats"]
            n = st["trades"]
            out[name] = {"balance": b["balance"], "net_pnl": st["net_pnl"], "trades": n, "wins": st["wins"],
                         "losses": st["losses"], "win_rate": round(st["wins"] / n, 3) if n else None,
                         "avg_win": round(st["gross_win"] / st["wins"], 2) if st["wins"] else None,
                         "avg_loss": round(st["gross_loss"] / st["losses"], 2) if st["losses"] else None,
                         "commissions": st["commissions"],
                         "avg_hold_min": round(st["hold_seconds"] / n / 60, 1) if n else None,
                         "exit_reasons": st["exit_reasons"], "open_positions": len(b["positions"]),
                         "open_pnl": round(sum(p["side"] * (self.s["last_price"].get(p["symbol"], p["entry"])
                                                            - p["entry"]) * self.usd_per_point(p)
                                               for p in b["positions"]), 2),
                         "hard_stop_net_usd": hard_stop_usd(self.cfg) if self.has_stop(name) else None,
                         "overlap_trades": st.get("overlap_trades", 0), "overlap_net": st.get("overlap_net", 0.0),
                         "net_pnl_excl_overlap": round(st["net_pnl"] - st.get("overlap_net", 0.0), 2),
                         "cloned_from": b.get("cloned_from"), "day": b["day"]}
        return out


def post_exit_metrics(item: dict, bars: pd.DataFrame, usd_per_point: float) -> dict | None:
    """Price 5 min after the exit + the best/worst move inside that window, signed in the CLOSED trade's direction
    (positive move_* = price kept going the trade's way = money left on the table; negative = reversal avoided).
    None until the M1 feed covers the whole window."""
    t_exit, due = datetime.fromisoformat(item["t_exit"]), datetime.fromisoformat(item["due"])
    if bars is None or not len(bars):
        return None
    if bars.index[-1].to_pydatetime() + timedelta(minutes=1) < due:
        return None
    w = bars[(bars.index >= pd.Timestamp(t_exit).floor("min")) & (bars.index < pd.Timestamp(due))]
    if not len(w):
        return None
    sd = 1 if item["side"] == "LONG" else -1
    ex = float(item["exit"])
    p5 = float(w["close"].iloc[-1])
    fav = (float(w["high"].max()) - ex) if sd > 0 else (ex - float(w["low"].min()))
    adv = (ex - float(w["low"].min())) if sd > 0 else (float(w["high"].max()) - ex)
    mv = sd * (p5 - ex)
    return {"price_after_5m": round(p5, 6), "move_after_5m_pts": round(mv, 6),
            "move_after_5m_usd": round(mv * usd_per_point, 2),
            "max_fav_5m_pts": round(max(0.0, fav), 6), "max_fav_5m_usd": round(max(0.0, fav) * usd_per_point, 2),
            "max_adv_5m_pts": round(max(0.0, adv), 6), "max_adv_5m_usd": round(max(0.0, adv) * usd_per_point, 2),
            "window_bars": int(len(w))}


def baseline_summary(logs_dir: str | Path, ids: set[int], since: str | None = None) -> dict:
    """Baseline Lucid sim results restricted to the same entries the books copied (fair comparison)."""
    trades = []
    for f in sorted(glob.glob(str(Path(logs_dir) / "*" / "lucid_trades.jsonl"))):
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("id") in ids and not (since and r.get("entry_time") and str(r["entry_time"]) < since):
                    trades.append(r)
    n = len(trades)
    wins = [t for t in trades if t["net"] > 0]
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {"net_pnl": round(sum(t["net"] for t in trades), 2), "trades": n, "wins": len(wins),
            "losses": n - len(wins), "win_rate": round(len(wins) / n, 3) if n else None,
            "avg_hold_min": round(sum(t["hold_seconds"] for t in trades) / n / 60, 1) if n else None,
            "exit_reasons": reasons}


# ---------------------------------------------------------------------------------------------- Gemini
async def call_exit_gemini(cfg: dict, system_text: str, image: bytes, user_text: str,
                           model: str | None = None) -> dict:
    """One exit decision. Tries the primary model, then fallbacks, inside one overall time budget. Never raises."""
    from google import genai
    from google.genai import types
    from .gemini_client import _image_mime, _is_retryable
    e, g = em_cfg(cfg), cfg["gemini"]
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return {"model": None, "raw_text": None, "error": "GEMINI_API_KEY not set", "attempts": []}
    budget = float(e["gemini_timeout_seconds"])
    t0 = time.monotonic()
    from .config import exit_model as _exit_model
    primary = _exit_model(cfg, model)
    # Strict: do not fall back to the entry model or [gemini].fallback_models on 404.
    models = [primary] + [m for m in (g.get("exit_fallback_models") or []) if m != primary]
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(budget * 1000)))
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_text),
                                                  types.Part.from_bytes(data=image, mime_type=_image_mime(image))])]
    attempts = []
    for m in models:
        left = budget - (time.monotonic() - t0)
        if left < 2:
            break
        from .market_hours import guard as _mh_guard; _mh_guard("Gemini exit")
        kw = dict(system_instruction=system_text, response_mime_type="application/json",
                  response_schema=ExitDecision)
        if m.startswith("gemini-3") and g.get("thinking_level"):
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=g["thinking_level"])
        try:
            resp = await asyncio.wait_for(client.aio.models.generate_content(
                model=m, contents=contents, config=types.GenerateContentConfig(**kw)), left)
            if not resp.text:
                raise RuntimeError("empty response")
            usage = resp.usage_metadata.model_dump(exclude_none=True) if resp.usage_metadata else None
            attempts.append({"model": m, "ok": True})
            return {"model": m, "model_version": getattr(resp, "model_version", None) or m,
                    "raw_text": resp.text, "usage": usage, "attempts": attempts, "error": None}
        except Exception as ex:
            err = f"{type(ex).__name__}: {str(ex)[:200]}"
            retry = _is_retryable(ex) if not isinstance(ex, RuntimeError) else True
            attempts.append({"model": m, "ok": False, "error": err, "retryable": retry})
            log.warning("exit gemini %s failed: %s", m, err)
            if not retry and "404" not in err:
                break
    return {"model": None, "raw_text": None, "error": attempts[-1]["error"] if attempts else "no time budget",
            "attempts": attempts}


# ---------------------------------------------------------------------------------------------- chart payload
def decorate_exit_payload(payload: dict, pos: dict, m: dict) -> dict:
    """The entry-model M15 payload + ENTRY line + a position line in the header, on its own warm page."""
    d = int((payload.get("context") or {}).get("digits") or payload.get("digits") or 5)
    payload["page_key"] = f"exit-{pos['symbol']}"
    payload["entry_line"] = {"price": pos["entry"], "color": "#FFFFFF",
                             "title": f"ENTRY {_side_word(pos['side'])}"}
    pa = m.get("pnl_atr")
    payload["position_header"] = (
        f"POSITION #{pos['id']} {_side_word(pos['side'])} {pos['qty']} {pos['contract']} @ {pos['entry']:.{d}f}"
        f"  |  P&L {pa:+.2f} ATR ({m['pnl_usd']:+,.0f} USD)" if pa is not None else
        f"POSITION #{pos['id']} {_side_word(pos['side'])} @ {pos['entry']:.{d}f}") + (
        f"  |  MFE {m['mfe_atr']:.2f} ATR  giveback {m['giveback_pct']:.0f}%" if m.get("mfe_atr") is not None else "")
    return payload


async def market_snapshot(sym: str, cfg: dict, now: datetime) -> tuple[dict, pd.DataFrame]:
    """(entry-model payload with live fields, M1 frame). Raises on data failure."""
    from .data_providers import fetch_symbol
    from .indicators import build_payload
    from .live_price import enrich_live_prices
    df, src, ticker = await fetch_symbol(sym, cfg)
    payload = build_payload(sym, df, cfg, src, ticker)
    # same live path as the entry cycle: Dukascopy ticks, US100 FirmTape NQ + basis in RTH, M1 fallback
    info = await enrich_live_prices([payload], cfg, now)
    m1 = (info.get("m1") or {}).get(sym)
    if m1 is None:
        raise RuntimeError(f"{sym}: live M1 unavailable")
    from .firmtape import enrich_firmtape   # GEX levels only; reuses the shared FirmTape response
    await enrich_firmtape([payload], cfg, now)
    return payload, m1


# ---------------------------------------------------------------------------------------------- manager
class ExitManager:
    def __init__(self, cfg: dict, dry_run: bool = False, model: str | None = None,
                 io_lock: asyncio.Lock | None = None, state_dir: str | Path | None = None,
                 logs_dir: str | Path | None = None, site_dir: str | Path | None = None,
                 snapshot=None, render=None, decide=None, m1_fetch=None):
        self.cfg, self.dry_run, self.model = cfg, dry_run, model
        self.e = em_cfg(cfg)
        self.io_lock = io_lock or asyncio.Lock()
        self.state_dir = Path(state_dir or cfg["general"]["state_dir"])
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or cfg.get("site", {}).get("dir") or self.state_dir.parent)
        self.path = self.state_dir / "exit_books.json"
        self.tz = ZoneInfo(cfg["general"]["session_tz"])
        st = json.loads(self.path.read_text()) if self.path.exists() else None
        tok = (cfg.get("lucid") or {}).get("reset_token")
        reset_books = False
        if st is not None and tok and st.get("reset_token") != tok:
            # Lucid account reset: position ids restart at 1, so the books start fresh too (old state archived).
            day = datetime.now(UTC).astimezone(ZoneInfo(cfg["general"]["session_tz"])).strftime("%Y-%m-%d")
            arch = (self.cfg.get("lucid") or {}).get("exit_books_archive") or "exit_books_pre_{token}.json"
            _atomic_write(self.state_dir / arch.format(day=day, token=tok), st)
            log.info("exit books: reset for Lucid reset_token %s (archived)", tok)
            st = None
            reset_books = True
        self.books = Books(cfg, st)
        for b in self.books.retired_now:
            _atomic_write(self.state_dir / f"exit_books_retired_{b}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json",
                          self.books.s["retired_books"][b])
            reset_books = True                  # rewrite exit_books.json / books.json without the retired book
        if tok:
            self.books.s["reset_token"] = tok
        # Persist immediately: with no open trades, tick() is idle and would not write, leaving the old
        # balances (and a stale site books.json / baseline) on disk.
        if reset_books or self._books_summary_stale():
            self._write_books_files()
        # injectable for tests / dry runs
        self._snapshot = snapshot or market_snapshot
        self._render = render or self._render_default
        self._decide = decide
        self._m1_fetch = m1_fetch
        self.busy = False
        self.lucid_close_requests: list[dict] = []   # Gemini CLOSEs for the Lucid account (written in _persist)

    @property
    def period(self) -> float:
        return float(self.e["check_seconds"])

    async def _render_default(self, payload: dict) -> bytes:
        from .renderer import render_all
        out = await render_all([payload])
        return next(iter(out.values()))

    def _account(self) -> dict:
        try:
            return json.loads((self.state_dir / "lucid" / "account.json").read_text())
        except Exception:
            return {}

    def _feed(self) -> list[dict]:
        return self._account().get("book_feed") or []

    def _sync_account(self, now: datetime) -> list[dict]:
        """Copy new Lucid entries into the books, keyed to the Lucid account's own reset_token: the config token
        changes at deploy, but the account only resets at its next minute replay and its ids restart at 1, so
        (a) no entries are copied while the account still carries an older token, and (b) the first time the
        account shows the new token the books start fresh and adopt the account's open positions (any age)."""
        acct = self._account()
        cfg_tok = (self.cfg.get("lucid") or {}).get("reset_token")
        acct_tok = acct.get("reset_token")
        if cfg_tok and acct_tok and acct_tok != cfg_tok:
            return []                                   # account not reset yet: old ids, do not copy
        adopt = set()
        if acct and acct_tok and self.books.s.get("account_token") != acct_tok:
            if self.books.s.get("account_token") is not None or self.books.s.get("seen_ids"):
                _atomic_write(self.state_dir / f"exit_books_pre_acct_{acct_tok}.json", self.books.s)
                self.books = Books(self.cfg, None)
                self.books.s["reset_token"] = cfg_tok
                log.info("exit books: fresh start for Lucid account token %s", acct_tok)
            self.books.s["account_token"] = acct_tok
            adopt = {p["id"] for p in acct.get("positions") or []}
            self._write_books_files()
        return self.books.sync_feed(acct.get("book_feed") or [], now, adopt_ids=adopt)

    def _jsonl(self, name: str, t: datetime, obj: dict) -> None:
        d = self.logs_dir / t.astimezone(self.tz).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a") as f:
            f.write(json.dumps(obj, default=str, separators=(",", ":")) + "\n")

    def _write_books_files(self) -> None:
        """Write state/exit_books.json and the site books.json (1-strike / 2-strike / baseline)."""
        _atomic_write(self.path, self.books.s)
        try:
            _atomic_write(self.site_dir / "books.json", self.summary())
        except Exception:
            log.exception("books.json write failed")

    def _books_summary_stale(self) -> bool:
        """True when site books.json is missing or does not match the in-memory books / empty baseline."""
        p = self.site_dir / "books.json"
        if not p.exists():
            return True
        try:
            cur = json.loads(p.read_text())
        except Exception:
            return True
        want = self.books.summary()
        for name, b in want.items():
            c = (cur.get("books") or {}).get(name) or {}
            if c.get("balance") != b.get("balance") or c.get("trades") != b.get("trades"):
                return True
        if not self.books.s.get("seen_ids"):
            if (cur.get("baseline") or {}).get("trades"):
                return True
        return False

    def summary(self) -> dict:
        ids = set(self.books.s["seen_ids"])

        mech = not gemini_exit_calls_enabled(self.cfg)
        uni = uniform_exits_enabled(self.cfg)
        hs = hard_stop_usd(self.cfg)
        if uni:
            note = (UNIFORM_BANNER + " Same Lucid entries. Every book (main, 1-strike, nostop, bracket, "
                    "CB-BRACKET, CB-RUNNER, Claude, DS-FLASH) closes only on a 1 ATR stop / 1 ATR target / "
                    "90-min cap. No model exit calls. Open positions armed from now. "
                    f"New closes tagged {UNIFORM_EXIT_VERSION}.")
        elif mech:
            note = ("Gemini exit calls paused — mechanical exits. Same Lucid entries. "
                    f"Main + 1-strike: {f'-${hs:g} net hard stop' if hs else 'no $ hard stop'}, "
                    f"{self.e.get('mech_target_atr', 1.0):g} ATR (M15 at entry) target, "
                    f"{int(self.e.get('mech_time_cap_minutes', 90))} min time cap. "
                    f"No-stop shadow: {self.e.get('mech_nostop_stop_atr', 1.0):g} ATR stop / "
                    f"{self.e.get('mech_target_atr', 1.0):g} ATR target / "
                    f"{int(self.e.get('mech_time_cap_minutes', 90))} min cap. "
                    "Bracket and CB-BRACKET unchanged. CB-RUNNER keeps its mechanical stop/lock/trail/"
                    "2.5 ATR target/150-min cap (model exit checks dropped). "
                    f"New closes tagged {GEMINI_MECH_EXIT_VERSION}.")
        else:
            note = ((f"GEMINI EXITS + HARD STOP -${hs:g} NET: same Lucid entries; books "
                     f"1-strike / 2-strike exit on Gemini CLOSE or the -${hs:g} net hard stop; "
                     f"shadow book nostop = same entries, Gemini CLOSE only (never stopped). No target, no max "
                     f"hold, no 16:45 flat; 3.5% breaker log-only. ")
                    if gemini_only(self.cfg) and hs else
                    "GEMINI ONLY: same Lucid entries; books A (1-strike) / B (2-strike) exit on Gemini CLOSE only - no "
                    "stop, no target, no max hold, no 16:45 flat; 3.5% breaker log-only. " if gemini_only(self.cfg)
                    else "Same Lucid entries in all three; books exit via Gemini (hard stop, 16:45 ET flat, "
                    f"3.5% breaker, {self.e['max_hold_minutes']} min max hold). ")
            note += ("BASELINE = the main Lucid account "
                     f"(exit_mode={(self.cfg.get('lucid') or {}).get('exit_mode', 'server')}: gemini = follows the "
                     f"{(self.cfg.get('lucid') or {}).get('gemini_exit_book', BOOK_1)} book's CLOSE) on the same entry ids.")
        return {"updated_at_utc": datetime.now(UTC).isoformat(), "paper_only": True,
                "note": note,
                "gemini_exit_calls_enabled": not mech,
                "uniform_1atr_exits": uni,
                "exit_calls_note": (UNIFORM_BANNER if uni else
                                    ("Gemini exit calls paused — mechanical exits" if mech else None)),
                "exit_rules": ({k: "1 ATR stop · 1 ATR target · 90-min cap"
                                for k in ("main", "1-strike", "nostop", "bracket",
                                          "CB-BRACKET", "CB-RUNNER", "C-OPUS", "C-SONNET", "C-OPUS-B", "DS-FLASH")}
                               if uni else ({
                    "main": (f"−${hs:g} net hard stop · 1 ATR target · 90-min cap" if hs else
                             "1 ATR target · 90-min cap"),
                    "1-strike": (f"−${hs:g} net hard stop · 1 ATR target · 90-min cap" if hs else
                                 "1 ATR target · 90-min cap"),
                    "nostop": "1 ATR stop · 1 ATR target · 90-min cap",
                    "bracket": "unchanged (1 ATR stop / 1 ATR target / 90 min)",
                    "CB-BRACKET": "unchanged (1 ATR stop / 1 ATR target / 90 min)",
                    "CB-RUNNER": "mechanical stop/lock/trail / 2.5 ATR target / 150-min cap (model exits paused)",
                } if mech else None)),
                "check_seconds": self.e["check_seconds"], "books": self.books.summary(),
                "baseline": baseline_summary(self.logs_dir, ids, self.books.s.get("created_at"))}

    async def tick(self, now: datetime | None = None) -> dict:
        """One exit check over every open book position. Never raises (errors -> HOLD, logged)."""
        if self.busy:
            return {"skipped": "previous tick still running"}
        self.busy = True
        try:
            now = now or datetime.now(UTC)
            opened = self._sync_account(now)
            self._queue_baseline_closes(now)
            post = await self._process_post_exit(now)
            ids = sorted(self.books.open_ids())
            if not ids:
                if opened or post:
                    await self._persist(now, [], post)
                return {"idle": True, "opened": opened, "post_exit": post}
            recs = await asyncio.gather(*[self._check(pid, now) for pid in ids], return_exceptions=True)
            recs = [r if not isinstance(r, Exception) else {"error": f"{type(r).__name__}: {r}"} for r in recs]
            self.books.breaker(now)
            await self._persist(now, recs, post)
            return {"checked": ids, "opened": opened, "records": recs, "post_exit": post}
        finally:
            self.busy = False

    def _queue_baseline_closes(self, now: datetime) -> None:
        """Queue the baseline Lucid sim's closes of the copied entries for the same 5-min post-exit tracking."""
        seen = set(self.books.s["seen_ids"])
        done = set(self.books.s.setdefault("baseline_tracked", []))
        if not (seen - done):
            return
        days = {(now - timedelta(days=k)).astimezone(self.tz).strftime("%Y-%m-%d") for k in (0, 1)}
        for day in sorted(days):
            f = self.logs_dir / day / "lucid_trades.jsonl"
            if not f.exists():
                continue
            for line in f.read_text().splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                # Lucid ids restart at 1 after an account reset: only trades entered after these books were created
                if r.get("entry_time") and str(r["entry_time"]) < str(self.books.s.get("created_at") or ""):
                    continue
                if r.get("id") in seen and r["id"] not in done:
                    done.add(r["id"])
                    self.books.queue_post_exit({**r, "book": "baseline"})
        self.books.s["baseline_tracked"] = sorted(done)[-500:]

    async def _process_post_exit(self, now: datetime) -> list[dict]:
        q = self.books.s.get("post_exit") or []
        due = [x for x in q if now >= datetime.fromisoformat(x["due"])]
        if not due:
            return []
        from .live_price import fetch_symbol_m1
        fetch = self._m1_fetch or (lambda sym: fetch_symbol_m1(sym, self.cfg, now))
        bars = {}
        for sym in {x["symbol"] for x in due}:
            try:
                bars[sym] = (await fetch(sym))[0]
            except Exception as ex:
                log.warning("post-exit m1 %s failed: %s", sym, ex)
        out = []
        for x in due:
            upp = self.books.spec(x["contract"])["point_value"] * x["qty"]
            m = post_exit_metrics(x, bars.get(x["symbol"]), upp)
            late = now - datetime.fromisoformat(x["due"]) > timedelta(minutes=20)
            if m is None and not late:
                continue   # feed has not covered the window yet: retry next tick
            q.remove(x)
            out.append({"event": "post_exit_5m", "t_utc": now.isoformat(timespec="microseconds"),
                        "t_dubai": now.astimezone(self.tz).isoformat(timespec="microseconds"),
                        **{k: x[k] for k in ("book", "id", "symbol", "side", "exit", "t_exit", "reason", "net")},
                        **(m or {"status": "no_m1_data_for_window"})})
        self.books.s["post_exit"] = q
        return out

    async def _persist(self, now: datetime, recs: list[dict], post: list[dict] | None = None) -> None:
        async with self.io_lock:
            for x in post or []:
                self._jsonl("exit_book_trades.jsonl", now, x)
            for r in recs:
                if r and "t_utc" in r:
                    self._jsonl("exits.jsonl", now, r)
            for c in self.books.closed:
                self._jsonl("exit_book_trades.jsonl", now, c)
                log.info("exit book %s close #%s %s %s net=%.2f", c["book"], c["id"], c["symbol"], c["reason"],
                         c["net"])
            self.books.closed = []
            if self.lucid_close_requests:
                # The Lucid account (exit_mode = "gemini") exits on these at its next minute replay.
                f = self.state_dir / "lucid" / "close_requests.json"
                try:
                    rows = json.loads(f.read_text()) if f.exists() else []
                except Exception:
                    rows = []
                rows = (rows + self.lucid_close_requests)[-200:]
                f.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(f, rows)
                for r in self.lucid_close_requests:
                    log.info("exit -> lucid close request #%s %s @ %s (%s)", r["id"], r["symbol"], r["price"], r["rule"])
                self.lucid_close_requests = []
            self._write_books_files()

    def _follow_requests(self, pid: int, sym: str, hits: list[dict], rec: dict) -> None:
        """Mirror 1-strike (gemini_exit_book) mechanical closes onto the main Lucid account."""
        if not hits:
            return
        follow = (self.cfg.get("lucid") or {}).get("gemini_exit_book", BOOK_1)
        h = next((x for x in hits if x["book"] == follow), None)
        if h is None:
            return
        reason = h.get("reason") or "mech_close"
        rec.setdefault("mech_follow", {"book": h["book"], "reason": reason, "price": h["price"]})
        self.lucid_close_requests.append(
            {"id": pid, "symbol": sym, "t": h["t"].isoformat() if hasattr(h["t"], "isoformat") else str(h["t"]),
             "price": h["price"], "book": h["book"], "rule": reason, "reason": reason,
             "reason_text": h.get("reason_text") or reason,
             "account_token": (self.cfg.get("lucid") or {}).get("reset_token")})

    def _stop_request(self, pid: int, sym: str, hits: list[dict], rec: dict) -> None:
        """Mirror a book hard stop onto the main Lucid account (same id / entry / qty -> same level). Prefers the
        book the account follows (gemini_exit_book). The account applies it at its next minute replay."""
        if not hits:
            return
        follow = (self.cfg.get("lucid") or {}).get("gemini_exit_book", BOOK_1)
        h = next((x for x in hits if x["book"] == follow), hits[0])
        reason = hard_stop_reason(self.cfg)
        rec["hard_stop"] = {"books": [x["book"] for x in hits], "trigger": h["trigger"], "price": h["price"],
                            "net": h["net"], "t": h["t"].isoformat()}
        self.lucid_close_requests.append(
            {"id": pid, "symbol": sym, "t": h["t"].isoformat(), "price": h["price"], "book": h["book"],
             "rule": reason, "reason": reason, "trigger": h["trigger"],
             "reason_text": f"hard stop -${hard_stop_usd(self.cfg):g} net ({h['trigger']})",
             "account_token": (self.cfg.get("lucid") or {}).get("reset_token")})

    async def _check(self, pid: int, now: datetime) -> dict:
        t_mono = time.monotonic()
        held = self.books.positions_for(pid)
        ref = next(iter(held.values()))
        sym = ref["symbol"]
        rec = {"t_utc": now.isoformat(timespec="microseconds"),
               "t_dubai": now.astimezone(self.tz).isoformat(timespec="microseconds"),
               "id": pid, "symbol": sym, "side": _side_word(ref["side"]), "contract": ref["contract"],
               "qty": ref["qty"], "entry": ref["entry"], "stop": ref["stop"], "entry_time": ref["entry_time"],
               "atr": ref.get("atr"), "books_open": sorted(held)}
        try:
            payload, m1 = await self._snapshot(sym, self.cfg, now)
        except Exception as ex:
            rec.update({"skipped_reason": "market_data_failed", "error": f"{type(ex).__name__}: {str(ex)[:200]}",
                        "decision": "HOLD", "books": {b: {"action": "none"} for b in held}})
            return rec
        ctx = payload["context"]
        price = ctx.get("live_price")
        rec.update({"live_price": price, "live_price_ts_utc": ctx.get("live_price_ts_utc"),
                    "live_price_source": ctx.get("live_price_source"), "live_price_stale": ctx.get("live_price_stale"),
                    "forming_m15": {k: ctx.get(f"forming_m15_{k}") for k in ("open", "high", "low")},
                    "minutes_into_bar": ctx.get("minutes_into_bar")})
        if price is not None:
            self.books.s["last_price"][sym] = price
        book_out: dict[str, dict] = {}
        mech = not gemini_exit_calls_enabled(self.cfg)
        rec["gemini_exit_calls_enabled"] = not mech
        rec["exit_mode"] = "mechanical" if mech else "gemini"
        # 1. hard stop (+MFE) on the M1 path since the last check, per book. Shadow books: MFE only
        #    (plus, in mechanical mode, a 1 ATR stop on nostop and a 1 ATR target on every book).
        stop_hits: list[dict] = []
        follow_hits: list[dict] = []  # closes that the main Lucid account should mirror
        hs_reason = hard_stop_reason(self.cfg)
        for name, p in list(held.items()):
            self.books.arm_hard_stop(name, p, now)
            if mech:
                self.books.arm_mech_exits(name, p, now, late=bool(
                    p.get("target") is None or (not self.books.has_stop(name) and p.get("stop") is None)))
            hit = self.books.scan_m1(p, m1)
            if hit:
                px, t_hit = hit[0], hit[1]
                kind = hit[2] if len(hit) > 2 else None
                if kind == "target":
                    reason = "target"
                elif kind == "stop_1atr":
                    reason = "stop_1atr"
                elif p.get("stop") is not None and p.get("stop_note") == "mech_1atr":
                    reason = "stop_1atr"
                elif p.get("stop") is not None:
                    reason = "hard_stop"
                else:
                    reason = hs_reason
                c = self.books.close(name, p, px, t_hit, reason,
                                     extra={"stop_trigger": "m1_path", "hard_stop_armed_at": p.get("hard_stop_armed_at")})
                book_out[name] = {"action": reason, "exit": px, "net": c["net"], "trigger": "m1_path"}
                row = {"book": name, "price": px, "t": t_hit, "trigger": "m1_path", "net": c["net"], "reason": reason}
                if reason.startswith("hard_stop") or reason == "hard_stop":
                    stop_hits.append(row)
                else:
                    follow_hits.append(row)
                held.pop(name)
        minutes = (now - datetime.fromisoformat(ref["entry_time"])).total_seconds() / 60.0
        rec["minutes_in_trade"] = round(minutes, 2)
        # 2. server-side forced exits (no Gemini call needed)
        forced = None
        cap_min = float(self.e.get("mech_time_cap_minutes", self.e["max_hold_minutes"]) if mech
                        else self.e["max_hold_minutes"])
        if gemini_only(self.cfg) and not mech:
            pass                                # no server-forced exits at all
        elif held and (not mech) and not lucid_in_session(now, self.cfg):
            forced = "flat_by_16:45_ET"
        elif held and minutes >= cap_min:
            forced = f"max_hold_{int(cap_min)}m"
        if held and (price is None or ctx.get("live_price_stale")):
            rec.update({"skipped_reason": "live_price_stale", "decision": "HOLD"})
            for name in held:
                book_out[name] = {"action": "none", "strikes_before": held[name]["strikes"],
                                  "strikes_after": held[name]["strikes"]}
            rec["books"] = book_out
            self._stop_request(pid, sym, stop_hits, rec)
            return rec
        # 1b. hard stop on the LIVE price (NET of RT commission <= -$N). Fill model = a resting stop order: the
        #     level + slippage when the stop was already working at the previous observation (<= gap_s ago);
        #     the live price + slippage when it was not (armed at deploy while already beyond, or a data /
        #     session gap), i.e. the market gapped through. The live-price fill is logged either way.
        gap_s = float(self.e.get("hard_stop_gap_seconds", 180))
        for name, p in list(held.items()):
            lvl = p.get("hard_stop_px") if p.get("stop") is None and self.books.has_stop(name) else None
            if lvl is None:
                continue
            if not stop_beyond(p["side"], lvl, float(price)):
                p["hard_stop_ok_t"] = now.isoformat()
                continue
            prev = p.get("hard_stop_ok_t") or (None if p.get("hard_stop_armed_late") else p.get("hard_stop_armed_at"))
            resting = bool(prev) and (now - datetime.fromisoformat(prev)).total_seconds() <= gap_s
            live_px = self.books.slip_exit(p, float(price))
            px = self.books.slip_exit(p, lvl) if resting else live_px
            trig = "live_price" if resting else "live_price_gap"
            live_net = round(p["side"] * (live_px - p["entry"]) * self.books.usd_per_point(p)
                             - 2 * self.books.spec(p["contract"])["commission"] * p["qty"], 2)
            c = self.books.close(name, p, px, now, hs_reason,
                                 extra={"stop_trigger": trig, "live_price": price,
                                        "fill_model": "resting_stop_at_level" if resting else "live_price",
                                        "net_if_live_fill": live_net, "last_ok_t": prev,
                                        "hard_stop_armed_at": p.get("hard_stop_armed_at")})
            book_out[name] = {"action": hs_reason, "exit": px, "net": c["net"], "trigger": trig,
                              "net_if_live_fill": live_net}
            stop_hits.append({"book": name, "price": px, "t": now, "trigger": trig, "net": c["net"]})
            held.pop(name)
        # Live 1 ATR stop on shadow (nostop) books in mechanical mode
        if mech and held and price is not None and not ctx.get("live_price_stale"):
            for name, p in list(held.items()):
                lvl = p.get("stop") if p.get("stop_note") == "mech_1atr" else None
                if lvl is None or not stop_beyond(p["side"], float(lvl), float(price)):
                    continue
                px = self.books.slip_exit(p, float(lvl))
                c = self.books.close(name, p, px, now, "stop_1atr", extra={"stop_trigger": "live_price"})
                book_out[name] = {"action": "stop_1atr", "exit": px, "net": c["net"], "trigger": "live_price"}
                follow_hits.append({"book": name, "price": px, "t": now, "trigger": "live_price",
                                    "net": c["net"], "reason": "stop_1atr"})
                held.pop(name)
        self._stop_request(pid, sym, stop_hits, rec)
        # Live 1 ATR target (mechanical mode): fill at the target when it was already working, else live price.
        if mech and held and price is not None and not ctx.get("live_price_stale"):
            for name, p in list(held.items()):
                tgt = p.get("target")
                if tgt is None:
                    continue
                if not ((p["side"] > 0 and float(price) >= float(tgt) - 1e-12) or
                        (p["side"] < 0 and float(price) <= float(tgt) + 1e-12)):
                    continue
                # late-armed this tick: still close from now at the live/target fill (not a historical bar)
                px = self.books.slip_exit(p, float(tgt))
                c = self.books.close(name, p, px, now, "target", extra={"stop_trigger": "live_price"})
                book_out[name] = {"action": "target", "exit": px, "net": c["net"], "trigger": "live_price"}
                follow_hits.append({"book": name, "price": px, "t": now, "trigger": "live_price",
                                    "net": c["net"], "reason": "target"})
                held.pop(name)
        if forced:
            for name, p in list(held.items()):
                px = self.books.slip_exit(p, price)
                c = self.books.close(name, p, px, now, forced)
                book_out[name] = {"action": forced, "exit": px, "net": c["net"]}
                follow_hits.append({"book": name, "price": px, "t": now, "trigger": "time_cap",
                                    "net": c["net"], "reason": forced})
            held = {}
        self._follow_requests(pid, sym, follow_hits, rec)
        if not held:
            rec.update({"decision": None, "skipped_reason": rec.get("skipped_reason") or "no_open_book_position",
                        "books": book_out})
            return rec
        rec["books_after_stop"] = sorted(held)   # e.g. ["nostop"] once main/1-strike/2-strike are stopped out
        if mech:
            rec.update({"decision": "HOLD", "decision_source": "mechanical",
                        "skipped_reason": "gemini_exit_calls_paused", "books": book_out})
            for name in held:
                book_out[name] = {"action": "none", "rule": "mechanical"}
            rec["books"] = book_out
            return rec
        # 3. Gemini decision (one call, shared by every book still holding this position)
        p0 = next(iter(held.values()))
        m = position_metrics(p0["side"], p0["entry"], price, p0.get("mfe", 0.0), p0.get("atr"),
                             self.books.usd_per_point(p0),
                             round_trip_commission(self.cfg, p0), p0.get("giveback_hist"))
        walls = find_walls(ctx, p0["side"], price, p0["entry"], p0.get("atr") or 0, p0.get("candle"))
        sc = ctx.get("signal_candle") or {}
        rec.update({k: m[k] for k in ("pnl_atr", "pnl_usd", "mfe_atr", "giveback_pct",
                                      "commission_usd", "commission_atr", "net_pnl_usd", "net_pnl_atr",
                                      "prev_giveback_pct", "giveback_2_ago", "giveback_change_2checks")},
                   pnl_pts=round(m["pnl_pts"], 8), walls=walls,
                   cvd_label=ctx.get("cvd_label"), cvd_slope=ctx.get("cvd_slope"), session=ctx.get("session"),
                   vwap=ctx.get("vwap"), ema20=ctx.get("ema20_baseline"), ema50=ctx.get("ema50"),
                   last_o=sc.get("open"), last_h=sc.get("high"), last_l=sc.get("low"), last_c=sc.get("close"),
                   rvol_last=ctx.get("rvol_last"), poc=ctx.get("poc"), vah=ctx.get("vah"), val=ctx.get("val"),
                   gex_summary=gex_summary_text(ctx.get("gex"),
                                                lambda x: _fmt_px(x, int(ctx.get("digits") or 5))))
        prompt = build_exit_prompt(self.cfg, p0, ctx, m, walls, now)
        resp = {"model": None, "raw_text": None, "error": None}
        try:
            decorate_exit_payload(payload, p0, m)
            png = await self._render(payload)
            if self._decide is not None:
                resp = await self._decide(prompt, png, p0, m)
            elif self.dry_run:
                resp = {"model": "mock-dry-run", "raw_text": json.dumps(mock_decision(m)), "error": None}
            else:
                resp = await call_exit_gemini(self.cfg, prompt, png, exit_user_text(p0), self.model)
        except Exception as ex:
            resp = {"model": None, "raw_text": None, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
        dec, perr = parse_decision(resp.get("raw_text")) if resp.get("raw_text") else (None, resp.get("error"))
        decision = dec["decision"] if dec else "HOLD"
        rec.update({"decision": decision, "structure": (dec or {}).get("structure"),
                    "reason": (dec or {}).get("reason"), "confidence": (dec or {}).get("confidence"),
                    "decision_source": "gemini" if dec else "error_default_hold",
                    "gemini_raw_text": resp.get("raw_text"), "model": resp.get("model"),
                    "model_version": resp.get("model_version"), "gemini_usage": resp.get("usage"),
                    "ai_error": resp.get("error"), "parse_error": perr if resp.get("raw_text") else None,
                    "latency_ms": round((time.monotonic() - t_mono) * 1000)})
        # 4. apply to each book
        for name, p in held.items():
            p["checks"] = p.get("checks", 0) + 1
            if m.get("giveback_pct") is not None:
                hist = list(p.get("giveback_hist") or [])
                hist.append(float(m["giveback_pct"]))
                p["giveback_hist"] = hist[-2:]
            st = apply_strike(name, int(p.get("strikes", 0)), decision, m["mfe_atr"], float(self.e["runner_mfe_atr"]))
            p["strikes"] = st["strikes_after"]
            out = {"action": "hold", **{k: st[k] for k in ("strikes_before", "strikes_after", "rule")}}
            if st["close"]:
                px = self.books.slip_exit(p, price)
                c = self.books.close(name, p, px, now, "gemini_close",
                                     extra={"exit_rule": st["rule"], "exit_reason_text": rec.get("reason"),
                                            "exit_structure": rec.get("structure"),
                                            "exit_confidence": rec.get("confidence")})
                out.update({"action": "close", "exit": px, "net": c["net"]})
                if name == (self.cfg.get("lucid") or {}).get("gemini_exit_book", BOOK_1):
                    self.lucid_close_requests.append(
                        {"id": pid, "symbol": sym, "t": now.isoformat(), "price": px, "book": name,
                         "rule": st["rule"], "reason_text": rec.get("reason"),
                         "account_token": (self.cfg.get("lucid") or {}).get("reset_token")})
            book_out[name] = out
        rec["books"] = book_out
        rec["prompt_chars"] = len(prompt)
        return rec


async def run_exit_loop(mgr: ExitManager, stop: asyncio.Event | None = None,
                        pre_tick=None) -> None:
    """Fixed-cadence exit checks (default 18 s). A slow tick never stacks: missed slots are skipped.
    Runs as its own asyncio task, so it never delays the 2-minute entry cycle.
    Optional pre_tick (e.g. Lucid pending fill / CLOSE replay) runs immediately before each tick
    so a just-filled position is picked up in the same 18-second slot."""
    nxt = time.monotonic()
    while not (stop and stop.is_set()):
        try:
            if pre_tick is not None:
                await pre_tick()
            await mgr.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("exit tick failed (paper books only; signals unaffected)")
        nxt += mgr.period
        now = time.monotonic()
        if nxt < now:
            missed = math.ceil((now - nxt) / mgr.period)
            log.info("exit tick overran by %.1fs; skipping %d slot(s)", now - nxt, missed)
            nxt += missed * mgr.period
        await asyncio.sleep(nxt - now)


async def render_sample(cfg: dict, symbol: str, side: int, out_dir: str | Path, minutes_ago: float = 6.0,
                        entry_offset_atr: float = 0.3) -> dict:
    """Dry run for review: real market data, a FAKE open position, the exit chart PNG + the exact prompt text.
    No Gemini call, no state written."""
    from .renderer import get_pool
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    payload, m1 = await market_snapshot(symbol, cfg, now)
    ctx = payload["context"]
    atr = float(ctx["atr"])
    price = float(ctx["live_price"])
    contract = cfg["lucid"]["symbols"][symbol]["contracts"][-1]
    tick = cfg["lucid"]["contracts"][contract]["tick"]
    entry = round(round((price - side * entry_offset_atr * atr) / tick) * tick, 10)  # fake entry in profit
    pos = {"id": 0, "symbol": symbol, "side": side, "contract": contract, "qty": 5, "entry": entry,
           "stop": round(entry - side * 0.8 * atr, 10), "atr": atr,
           "entry_time": (now - timedelta(minutes=minutes_ago)).isoformat(),
           "candle": ctx.get("signal_candle"), "mfe": 0.0, "scan_from": (now - timedelta(minutes=minutes_ago)).isoformat()}
    Books(cfg).scan_m1(pos, m1)
    upp = cfg["lucid"]["contracts"][contract]["point_value"] * pos["qty"]
    m = position_metrics(side, entry, price, pos["mfe"], atr, upp)
    walls = find_walls(ctx, side, price, entry, atr, pos["candle"])
    prompt = build_exit_prompt(cfg, pos, ctx, m, walls, now)
    decorate_exit_payload(payload, pos, m)
    try:
        png = await ExitManager._render_default(None, payload)
    finally:
        await get_pool().close()
    ext = "jpg" if png[:3] == b"\xff\xd8\xff" else "png"
    (out / f"sample_exit_chart.{ext}").write_bytes(png)
    (out / "exit_prompt_sample.txt").write_text(
        "##### system_instruction #####\n" + prompt + "\n\n##### user message (text part; the chart image follows) #####\n"
        + exit_user_text(pos) + "\n")
    meta = {"position": pos, "metrics": m, "walls": walls, "mock_decision": mock_decision(m),
            "live_price": price, "live_price_source": ctx.get("live_price_source"), "rendered_at_utc": now.isoformat()}
    (out / "exit_sample_meta.json").write_text(json.dumps(meta, indent=2, default=str))
    return {"chart": str(out / f"sample_exit_chart.{ext}"), **meta}
