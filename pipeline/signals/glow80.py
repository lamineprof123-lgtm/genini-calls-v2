"""G-LOW-80 Gemini side book (t272u). PAPER ONLY - nothing here places an order anywhere.

User (t272u, 13 Nov 2026): "go ahead but add it as a separate book, pick the highest pnl brackets". The
confidence-bracket backtest (all logged BUY/SELL since 11 Nov, uniform 1 ATR / 1 ATR / 90-min, $900 per ATR)
put almost all of the edge in Gemini 3.8 Flash on LOW thinking (before the 21:24 Dubai 12 Nov switch to high),
best in the 0.8-0.9 (+$204 avg) and 0.9-1.0 (+$844) brackets. This book re-runs exactly that model config:

  Call     its OWN Gemini entry call at every M15 close +20 s on the shared M15 snapshot (signals/m15_scan.py:
           same charts / payloads as every close-bar model), with the MAIN prompt (templates/system_prompt.j2),
           SignalBatch schema, server scalp levels + validator, and thinking_level LOW: the exact pre-b23b41bb
           config (gemini-3.8-flash, ThinkingConfig(thinking_level="low"), 45 s timeout, 2 attempts / 60 s budget,
           no fallback). MAIN Gemini is untouched (stays on its own 2-min loop, thinking high).
  Trades   only BUY/SELL with confidence >= 0.70. Lucid symbols (XAUUSD / EURUSD / GBPUSD / US100 / XAGUSD) go
           to its own fresh $100k Lucid-style account (this module, a BracketBook subclass); BTCUSD goes to its
           own $100k OTHER-VENUE account (signals/other_venue.py, book "G-LOW-80").
  Exits    uniform-1atr-v1: 1 ATR stop / 1 ATR target / 90-min cap, no model exits (18 s server checks).
  Size     ~$900 risk: qty = floor(900 / (1 ATR x point value)) >= 1 full-size contract, skip if 1 contract risks
           > $1,500, max 6 full-size open, one position per symbol.
  Fill     live quote + 1 tick; skipped if it is more than 0.3 ATR worse than the live price sent to the model
           (0.3 ATR fill check). Daily loss stop: no new entries after -$1,800 net on the Lucid trading day.
  Logs     logs/<day>/glow80_signals.jsonl (every call, every signal), glow80_trades.jsonl, glow80_events.jsonl,
           m15_calls.jsonl (book G-LOW-80, thinking_level low) and the universal signal scorer.
State: state/glow80/account.json. Site: glow80.json at the data root; panel.json lists it as its own book.
"""
from __future__ import annotations
import asyncio, copy, json, logging, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .bracket import BracketBook, DEFAULTS as BRACKET_DEFAULTS, _side_word, skip_key

log = logging.getLogger("signals.glow80")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
BOOK = "G-LOW-80"
LABEL = "Gemini flash · G-LOW-80 (low thinking, min 0.70)"
MODEL = "gemini-3.8-flash"

DEFAULTS = {
    **BRACKET_DEFAULTS,
    "enabled": True,
    "book": BOOK,
    "model": MODEL,
    "thinking_level": "low",
    "timeout_seconds": 45,
    "max_attempts_per_model": 2,
    "retry_budget_seconds": 60,
    "symbols": ["XAUUSD", "EURUSD", "GBPUSD", "US100", "XAGUSD"],   # Lucid books of this account
    "scan_symbols": ["XAUUSD", "EURUSD", "GBPUSD", "US100", "XAGUSD", "BTCUSD"],
    "other_venue_symbols": ["BTCUSD"],
    "min_confidence": 0.70,
    "max_chase_atr": 0.3,              # the 0.3 ATR fill check (vs the live price sent to the model)
    "daily_loss_usd": 1800.0,
    "reset_token": "glow80-v1",
}


def glow80_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("glow80") or {})}


def gemini_low_cfg(cfg: dict, g80: dict) -> dict:
    """A copy of cfg whose [gemini] block is the pre-21:24 (12 Nov) LOW-thinking entry config."""
    c = copy.deepcopy(cfg)
    g = c.setdefault("gemini", {})
    g["model"] = g80["model"]
    g["entry_thinking_level"] = g80["thinking_level"]
    g["thinking_level"] = g80["thinking_level"]
    g["fallback_models"] = []
    g["timeout_seconds"] = g80["timeout_seconds"]
    g["max_attempts_per_model"] = g80["max_attempts_per_model"]
    g["retry_budget_seconds"] = g80["retry_budget_seconds"]
    return c


def lucid_day(t: datetime) -> str:
    """Lucid trading day: rolls at 18:00 New York."""
    et = t.astimezone(ET)
    d = (et + timedelta(days=1)).date() if et.hour >= 18 else et.date()
    return d.isoformat()


class GLow80Book(BracketBook):
    BOOK_NAME = BOOK
    BOOK_LABEL = LABEL
    STATE_SUBDIR = "glow80"
    TRADES_LOG = "glow80_trades.jsonl"
    EVENTS_LOG = "glow80_events.jsonl"
    SITE_FILE = "glow80.json"

    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *, call=None, ov_hub=None, **kw):
        super().__init__(cfg, io_lock, **kw)
        self._call = call          # injectable for tests: async (cfg, payloads, pngs) -> gemini resp dict
        self._ov_hub = ov_hub      # injectable; default = other_venue.get_hub(cfg)
        self.s.setdefault("day", {"day": None, "net": 0.0})
        self.s.setdefault("calls", {"ok": 0, "failed": 0, "skipped": 0, "tokens": {}})

    def load_cfg(self, cfg: dict) -> dict:
        return glow80_cfg(cfg)

    def fresh(self) -> dict:
        s = super().fresh()
        s.update({"book": BOOK, "day": {"day": None, "net": 0.0},
                  "calls": {"ok": 0, "failed": 0, "skipped": 0, "tokens": {}}})
        return s

    # ------------------------------------------------------------------ risk
    def day_net(self, now: datetime) -> float:
        d = self.s.setdefault("day", {"day": None, "net": 0.0})
        if d.get("day") != lucid_day(now):
            return 0.0
        return float(d.get("net") or 0.0)

    def eligible(self, sig: dict) -> str | None:
        why = super().eligible(sig)
        if why is None and self.day_net(datetime.now(UTC)) <= -float(self.b["daily_loss_usd"]) + 1e-9:
            return f"daily loss stop: {self.day_net(datetime.now(UTC)):,.0f} <= -{self.b['daily_loss_usd']:g}"
        return why

    def close(self, p, price, t, reason, extra=None):
        rec = super().close(p, price, t, reason, extra)
        d = self.s.setdefault("day", {"day": None, "net": 0.0})
        day = lucid_day(t)
        if d.get("day") != day:
            d.update({"day": day, "net": 0.0})
        d["net"] = round(float(d["net"]) + float(rec["net"]), 2)
        return rec

    def held_any(self, sym: str) -> bool:
        if sym in self.b["other_venue_symbols"]:
            hub = self.hub()
            return bool(hub is not None and hub.held(BOOK, sym))
        return self.held(sym)

    def hub(self):
        if self._ov_hub is not None:
            return self._ov_hub
        try:
            from .other_venue import get_hub
            return get_hub(self.cfg)
        except Exception:
            return None

    # ------------------------------------------------------------------ the M15 entry call
    def scan_symbols(self, snap_symbols: list[str], now: datetime) -> tuple[list[str], dict]:
        """Symbols sent to Gemini this close: the MAIN scan set minus symbols this book already holds
        (MAIN does the same for its own positions) and minus Lucid symbols outside the Lucid session."""
        out, why = [], {}
        lucid_ok = self.entries_allowed(now)
        for s in self.b["scan_symbols"]:
            if s not in snap_symbols:
                why[s] = "not in snapshot"
            elif self.held_any(s):
                why[s] = "held"
            elif s not in self.b["other_venue_symbols"] and not lucid_ok:
                why[s] = "outside Lucid session"
            else:
                out.append(s)
        return out, why

    async def gemini(self, payloads: list[dict], pngs: dict) -> dict:
        lcfg = gemini_low_cfg(self.cfg, self.b)
        if self._call is not None:
            return await self._call(lcfg, payloads, pngs)
        from .gemini_client import call_gemini
        return await call_gemini(lcfg, payloads, pngs, self.b["model"])

    def validate(self, resp: dict, payloads: list[dict], ai_error: str | None) -> tuple[list[dict], dict | None, str | None]:
        from .validator import parse_raw, validate_batch
        from .scalp_levels import apply_server_levels
        raw = (resp or {}).get("raw_text") or ""
        obj, perr = parse_raw(raw) if raw else (None, ai_error or "empty response")
        contexts = {p["symbol"]: p["context"] for p in payloads}
        if obj and (self.cfg.get("scalp") or {}).get("enabled", False):
            for s in obj.get("signals") or []:
                if not isinstance(s, dict) or not s.get("symbol"):
                    continue
                sym = str(s["symbol"]).upper().replace("/", "").strip()
                ctx = contexts.get(sym)
                if not ctx:
                    continue
                if not ctx.get("live_price_stale") and ctx.get("live_price") is not None:
                    s["entry"] = ctx["live_price"]
                elif s.get("entry") is None:
                    s["entry"] = ctx.get("last_close")
                apply_server_levels(s, ctx, self.cfg, sym)
        validated = validate_batch(obj, contexts, self.cfg, float(self.s["balance"]), False, None)
        for v in validated:
            ctx = contexts.get(v["symbol"]) or {}
            for k in ("session", "cvd_label", "last_closed_bar_utc", "live_price", "live_price_ts_utc",
                      "live_price_stale", "live_price_source", "atr"):
                if v.get(k) is None and ctx.get(k) is not None:
                    v[k] = ctx[k]
            v["thinking_level"] = self.b["thinking_level"]
            v["model"] = self.b["model"]
            v["book"] = BOOK
        return validated, obj, perr

    async def on_snapshot(self, snap, now: datetime | None = None) -> dict:
        """One LOW-thinking Gemini call on the shared snapshot, then intake. Never raises."""
        now = now or getattr(snap, "now", None) or datetime.now(UTC)
        t_call = datetime.now(UTC)
        out = {"book": BOOK, "model": self.b["model"], "thinking_level": self.b["thinking_level"], "ok": False}
        try:
            snap_syms = [p["symbol"] for p in snap.payloads]
            syms, why = self.scan_symbols(snap_syms, now)
            out["excluded"] = why
            if not syms:
                out["skipped"] = "no symbols to scan (" + ", ".join(f"{k}: {v}" for k, v in why.items()) + ")"
                self.s["calls"]["skipped"] += 1
                await self.persist()
                return out
            payloads = snap.slice(syms)
            pngs = snap.charts_for(syms)
            payloads = [p for p in payloads if p["symbol"] in pngs]
            if not payloads:
                out["skipped"] = "no charts"
                return out
            ai_error, resp = None, {}
            t0 = time.monotonic()
            try:
                resp = await self.gemini(payloads, pngs)
            except Exception as e:
                ai_error = f"{type(e).__name__}: {str(e)[:300]}"
                resp = {"raw_text": "", "attempts": getattr(e, "attempts", None)}
            out["latency_s"] = resp.get("latency_s") or round(time.monotonic() - t0, 3)
            out["answer_ts"] = datetime.now(UTC).isoformat()
            usage = resp.get("usage") or {}
            out["thinking_tokens"] = usage.get("thoughts_token_count")
            out["usage"] = {k: usage.get(k) for k in ("prompt_token_count", "candidates_token_count",
                                                      "thoughts_token_count", "total_token_count")}
            out["model_version"] = resp.get("model_version")
            out["attempts"] = resp.get("attempts")
            validated, obj, perr = self.validate(resp, payloads, ai_error)
            ok = ai_error is None and obj is not None and perr is None
            out["ok"] = ok
            out["error"] = ai_error or perr
            out["symbols"] = [p["symbol"] for p in payloads]
            c = self.s["calls"]
            c["ok" if ok else "failed"] += 1
            for k, v in out["usage"].items():
                if isinstance(v, (int, float)):
                    c["tokens"][k] = int(c["tokens"].get(k, 0) + v)
            sig_at = t_call.isoformat()
            # ---- intake: Lucid symbols -> this account, BTC -> own OTHER-VENUE account
            lucid_rec = {"run_at_utc": sig_at, "signals": [v for v in validated
                                                             if v["symbol"] not in self.b["other_venue_symbols"]],
                         "in_position": []}
            queued = self.intake(lucid_rec, now) if ok else []
            ov_res = []
            if ok:
                ov_res = await self._intake_other_venue(
                    [v for v in validated if v["symbol"] in self.b["other_venue_symbols"]], now)
            traded = {q["symbol"] for q in queued} | {r.get("symbol") for r in ov_res if r and not r.get("skipped")}
            out["queued"] = [q["symbol"] for q in queued]
            out["other_venue"] = ov_res
            score = []
            for v in validated:
                score.append({**v, "signal_at": sig_at, "kind": "entry", "traded": v["symbol"] in traded,
                              "prompt_version": "system_prompt.j2", "book": BOOK,
                              "model_version": resp.get("model_version")})
            out["score_signals"] = score if ok else []
            out["validated"] = validated        # in-memory only: shared with EVAL-80 (no extra Gemini call)
            out["signal_at"] = sig_at
            out["signals"] = [{k: v.get(k) for k in ("symbol", "action", "confidence", "session", "atr",
                                                    "entry", "live_price")} for v in validated]
            self._jsonl("glow80_signals.jsonl", now, {
                "t": sig_at, "book": BOOK, "model": self.b["model"], "model_version": resp.get("model_version"),
                "thinking_level": self.b["thinking_level"], "snapshot_id": getattr(snap, "snapshot_id", None),
                "snapshot_hash": getattr(snap, "snapshot_hash", None), "ok": ok, "error": out["error"],
                "latency_s": out["latency_s"], "usage": out["usage"], "attempts": resp.get("attempts"),
                "market_summary": (obj or {}).get("market_summary"), "excluded": why,
                "signals": [{**v, "traded": v["symbol"] in traded} for v in validated],
                "gemini_raw_text": resp.get("raw_text")})
            if queued:
                await self.tick(now)
            else:
                await self.persist()
            return out
        except Exception as e:
            log.exception("G-LOW-80 on_snapshot failed (other books unaffected)")
            out["error"] = f"{type(e).__name__}: {e}"
            return out

    async def _intake_other_venue(self, sigs: list[dict], now: datetime) -> list[dict]:
        hub = self.hub()
        out = []
        if hub is None:
            return out
        for v in sigs:
            if v.get("action") not in ("BUY", "SELL"):
                continue
            why = None
            try:
                conf = float(v.get("confidence"))
            except (TypeError, ValueError):
                conf = -1
            if conf < float(self.b["min_confidence"]) - 1e-9:
                why = f"confidence {conf:.2f} < {self.b['min_confidence']:g}"
            elif not v.get("atr"):
                why = "no ATR"
            elif self.day_net(now) <= -float(self.b["daily_loss_usd"]) + 1e-9:
                why = "daily loss stop"
            if why:
                self._skip(skip_key(why))
                self._event(now, "skip", venue="OTHER-VENUE", symbol=v["symbol"], action=v.get("action"),
                            confidence=v.get("confidence"), reason=why)
                out.append({"skipped": why, "symbol": v["symbol"]})
                continue
            side = 1 if v["action"] == "BUY" else -1
            ref = v.get("live_price") or v.get("entry")
            px = None
            try:
                q, _m1 = await self._quotes([v["symbol"]], now)
                qq = (q or {}).get(v["symbol"])
                if qq and qq.get("price") is not None:
                    px = float(qq["price"])
            except Exception as e:
                log.warning("G-LOW-80 BTC fill quote failed: %s", e)
            px = px if px is not None else (float(ref) if ref is not None else None)
            if px is not None and ref is not None and side * (px - float(ref)) > float(self.b["max_chase_atr"]) * float(v["atr"]):
                why = "chase > max_chase_atr"
                self._skip(why)
                self._event(now, "chase_skip", venue="OTHER-VENUE", symbol=v["symbol"], fill=px, signal_entry=ref)
                out.append({"skipped": why, "symbol": v["symbol"]})
                continue
            self.s["stats"]["signals_seen"] += 1
            r = hub.intake_signal(BOOK, {**v, "model": self.b["model"]}, now, quote=px)
            try:
                hub.persist()
            except Exception:
                log.exception("other-venue persist failed")
            if r and not r.get("skipped"):
                self.s["stats"]["signals_taken"] += 1
                self._event(now, "open", venue="OTHER-VENUE", symbol=v["symbol"], side=_side_word(side),
                            confidence=v.get("confidence"), price=px)
            out.append({k: r.get(k) for k in ("id", "symbol", "side", "qty", "entry", "skipped")} if r else {})
        return out

    def site_view(self) -> dict:
        v = super().site_view()
        b = self.b
        v.update({"model": b["model"], "thinking_level": b["thinking_level"], "calls": self.s.get("calls"),
                  "day": self.s.get("day"),
                  "rules": (f"Own Gemini {b['model']} entry call at every M15 close +20 s on the shared snapshot, "
                            f"thinking {b['thinking_level']} (pre-21:24 12 Nov config), MAIN prompt and charts. "
                            f"BUY/SELL with confidence >= {b['min_confidence']:g} only. Lucid: "
                            f"{', '.join(b['symbols'])}; BTCUSD on its own OTHER-VENUE $100k account. "
                            f"Uniform 1 ATR stop / 1 ATR target / {b['max_hold_minutes']} min cap, no model exits. "
                            f"~${b['risk_usd']:g} risk, full-size, max {b['max_minis']} open, one per symbol, "
                            f"0.3 ATR fill check, daily loss stop ${b['daily_loss_usd']:g}."),
                  "limitation": None})
        return v
