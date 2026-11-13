"""EVAL-80 Lucid eval book (t284u). PAPER ONLY - nothing here places an order anywhere.

User (t279u-t284u, 13 Nov 2026): run the backtested "sweet spot" plan as its own paper book under the LucidPro 100K
eval rules: Gemini low thinking, 0.8+ signals, 0.8 ATR stop / 1 ATR target, ~$450 risk per trade. "push it live".

  Signals  NO Gemini call of its own: it reuses G-LOW-80's low-thinking gemini-3.8-flash response on the same
           M15 close +20 s snapshot (signals/m15_scan.py hands this book G-LOW-80's validated signals). Takes only
           BUY/SELL with confidence >= 0.80 on the Lucid symbols (XAUUSD / EURUSD / GBPUSD / US100 / XAGUSD).
           BTCUSD is never traded here (Lucid only). One position per symbol.
  Bracket  stop 0.8 ATR, target 1.0 ATR, 90-minute cap, no model exits, no trailing. 0.3 ATR fill check.
  Size     ~$450 risk at the 0.8 ATR stop: minis when floor(450 / mini risk) >= 1, else micros
           (GC/MGC, NQ/MNQ, SI/SIL, 6E/M6E, 6B/M6B). Max 2 minis (20 micro-equivalents) per position and 6 minis /
           60 micros total (1 mini = 10 micros). An overlapping trade downsizes into the remaining room (micros if
           the room is not a whole number of minis). FX is usually capped at 2 minis (under-sized, by design).
  Eval     fresh $100k account, LucidPro 100K eval rules ENFORCED:
           - DLL $1,800 (realized + open on the Lucid day): flatten, soft pause for the rest of the session.
           - MLL $3,000 EOD trailing from the highest EOD close (start floor $97,000); locks at $100,100 once an EOD
             close is above $103,100. Equity (balance + open) at or below the floor intraday = FAILED.
           - Balance >= $106,000 ($6,000 profit) = PASSED (open positions are flattened when equity reaches it).
           - PASSED / FAILED: no more trading, the record is kept, no auto-reset.
           - Flat 21:45 UTC (01:45 Dubai, Lucid 16:45 ET close), no new entries in the last 15 minutes (as G-LOW-80).
Limitation: G-LOW-80 leaves symbols it already holds out of its Gemini call, so EVAL-80 gets no signal for them that
close. Each such close (EVAL-80 flat on the symbol) is counted per symbol (missed_scans) and shown on the card.
State: state/eval80/account.json. Logs: logs/<day>/eval80_signals.jsonl, eval80_trades.jsonl, eval80_events.jsonl.
Site: eval80.json at the data root; panel.json lists it as its own book with an "eval" block.
"""
from __future__ import annotations
import asyncio, logging, math
from datetime import datetime, timezone

from .bracket import BracketBook, DEFAULTS as BRACKET_DEFAULTS, skip_key
from .glow80 import lucid_day

log = logging.getLogger("signals.eval80")
UTC = timezone.utc
BOOK = "EVAL-80"
LABEL = "Gemini flash · EVAL-80 (G-LOW-80's call, min 0.80, 0.8/1.0 ATR, Lucid eval rules)"
MODEL = "gemini-3.8-flash"
SOURCE_BOOK = "G-LOW-80"

# Micro specs (CME). Fees per side: Lucid list (MNQ $0.50, MGC $0.80, SIL $1.60); FX micros ~$0.50.
CONTRACTS = {
    "MGC": {"point_value": 10.0, "tick": 0.10, "commission": 0.80, "micro": True, "mini": "GC"},
    "MNQ": {"point_value": 2.0, "tick": 0.25, "commission": 0.50, "micro": True, "mini": "NQ"},
    "SIL": {"point_value": 1000.0, "tick": 0.005, "commission": 1.60, "micro": True, "mini": "SI"},
    "M6E": {"point_value": 12500.0, "tick": 0.0001, "commission": 0.50, "micro": True, "mini": "6E"},
    "M6B": {"point_value": 6250.0, "tick": 0.0001, "commission": 0.50, "micro": True, "mini": "6B"},
}
SYMBOL_CONTRACTS = {"XAUUSD": ("GC", "MGC"), "US100": ("NQ", "MNQ"), "XAGUSD": ("SI", "SIL"),
                    "EURUSD": ("6E", "M6E"), "GBPUSD": ("6B", "M6B")}

DEFAULTS = {
    **BRACKET_DEFAULTS,
    "enabled": True,
    "book": BOOK,
    "source_book": SOURCE_BOOK,
    "model": MODEL,
    "thinking_level": "low",
    "symbols": ["XAUUSD", "EURUSD", "GBPUSD", "US100", "XAGUSD"],
    "min_confidence": 0.80,
    "stop_atr": 0.8,
    "target_atr": 1.0,
    "max_hold_minutes": 90,
    "max_chase_atr": 0.3,
    "risk_usd": 450.0,
    "max_risk_usd": 1500.0,          # skip if ONE micro at the 0.8 ATR stop would risk more than this
    "max_minis_per_position": 2,
    "max_minis": 6,                  # total, = 60 micros
    "micros_per_mini": 10,
    "start_balance": 100000.0,
    "profit_target": 6000.0,
    "mll": 3000.0,
    "initial_trail_balance": 103100.0,
    "locked_mll_balance": 100100.0,
    "dll": 1800.0,
    "reset_token": "eval80-v1",
}


def eval80_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **{k: v for k, v in (cfg.get("eval80") or {}).items() if k != "contracts"}}


def eval80_contracts(cfg: dict) -> dict:
    """Micro specs (config [eval80.contracts] overrides the defaults above)."""
    over = (cfg.get("eval80") or {}).get("contracts") or {}
    out = {k: {**v, **(over.get(k) or {})} for k, v in CONTRACTS.items()}
    out.update({k: v for k, v in over.items() if k not in CONTRACTS})
    return out


class Eval80Book(BracketBook):
    BOOK_NAME = BOOK
    BOOK_LABEL = LABEL
    STATE_SUBDIR = "eval80"
    TRADES_LOG = "eval80_trades.jsonl"
    EVENTS_LOG = "eval80_events.jsonl"
    SITE_FILE = "eval80.json"

    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, **kw):
        self._micro = eval80_contracts(cfg)
        super().__init__(cfg, io_lock, **kw)
        self.s.setdefault("eval", self._fresh_eval())
        self.s.setdefault("day", {"day": None, "net": 0.0})
        self.s.setdefault("cycles", {"seen": 0, "with_signals": 0, "skipped": 0, "last": None})

    def load_cfg(self, cfg: dict) -> dict:
        return eval80_cfg(cfg)

    def _fresh_eval(self) -> dict:
        sb = float(self.b["start_balance"])
        return {"status": "ACTIVE", "reason": None, "at": None, "day": None, "highest_eod": sb,
                "mll_floor": round(sb - float(self.b["mll"]), 2), "mll_locked": False,
                "dll_paused_day": None, "eod_closes": [], "min_equity": sb}

    def fresh(self) -> dict:
        s = super().fresh()
        s.update({"book": BOOK, "eval": self._fresh_eval(), "day": {"day": None, "net": 0.0},
                  "cycles": {"seen": 0, "with_signals": 0, "skipped": 0, "last": None}})
        return s

    # ------------------------------------------------------------------ contracts / sizing
    def spec(self, c: str) -> dict:
        return self._micro.get(c) or self.L["contracts"][c]

    def is_micro(self, c: str) -> bool:
        return bool(self.spec(c).get("micro"))

    def micro_eq(self, p: dict) -> int:
        return int(p["qty"]) * (1 if self.is_micro(p["contract"]) else int(self.b["micros_per_mini"]))

    def micro_eq_used(self) -> int:
        return sum(self.micro_eq(p) for p in self.s["positions"] + self.s["pending"])

    def minis_used(self) -> float:
        return self.micro_eq_used() / int(self.b["micros_per_mini"])

    def size_for(self, sym: str, atr: float) -> tuple[str | None, int | None, str | None]:
        """(contract, qty, why). Minis when one fits the risk, else micros; per-position + total caps."""
        pair = SYMBOL_CONTRACTS.get(sym)
        if not pair:
            return None, None, "no contract"
        if not atr or atr <= 0:
            return None, None, "bad ATR"
        mini, micro = pair
        k = int(self.b["micros_per_mini"])
        risk = float(self.b["risk_usd"])
        stop = float(self.b["stop_atr"]) * atr
        per_mini = stop * float(self.spec(mini)["point_value"])
        per_micro = stop * float(self.spec(micro)["point_value"])
        pos_cap = int(self.b["max_minis_per_position"]) * k
        room = int(self.b["max_minis"]) * k - self.micro_eq_used()
        if room < 1:
            return None, None, f"max {self.b['max_minis']} minis / {int(self.b['max_minis']) * k} micros already open"
        q_mini = int(math.floor(risk / per_mini + 1e-9))
        if q_mini >= 1:
            want = min(q_mini * k, pos_cap)
        else:
            if per_micro > float(self.b["max_risk_usd"]):
                return None, None, (f"1 {micro} at a {self.b['stop_atr']:g} ATR stop risks ${per_micro:,.0f} > "
                                    f"${self.b['max_risk_usd']:g}")
            want = min(max(1, int(math.floor(risk / per_micro + 1e-9))), pos_cap)
        allowed = min(want, room)
        if q_mini >= 1 and allowed % k == 0:
            return mini, allowed // k, None
        return micro, allowed, None

    def sizing_table(self, atrs: dict) -> list[dict]:
        """Planned size per symbol on an empty book (report helper)."""
        saved = self.s["positions"], self.s["pending"]
        self.s["positions"], self.s["pending"] = [], []
        out = []
        try:
            for sym in self.b["symbols"]:
                atr = atrs.get(sym)
                if not atr:
                    continue
                c, q, why = self.size_for(sym, float(atr))
                row = {"symbol": sym, "atr": atr, "stop_dist": round(float(self.b["stop_atr"]) * atr, 6)}
                if c:
                    pv = float(self.spec(c)["point_value"])
                    row.update({"contract": c, "qty": q, "risk_usd": round(float(self.b["stop_atr"]) * atr * pv * q, 2),
                                "fees_rt": round(2 * float(self.spec(c)["commission"]) * q, 2)})
                else:
                    row["skip"] = why
                out.append(row)
        finally:
            self.s["positions"], self.s["pending"] = saved
        return out

    # ------------------------------------------------------------------ eval rules
    @property
    def ev(self) -> dict:
        return self.s.setdefault("eval", self._fresh_eval())

    def active(self) -> bool:
        return self.ev.get("status") == "ACTIVE"

    def day_net(self, now: datetime) -> float:
        d = self.s.setdefault("day", {"day": None, "net": 0.0})
        return float(d.get("net") or 0.0) if d.get("day") == lucid_day(now) else 0.0

    def dll_paused(self, now: datetime) -> bool:
        return self.ev.get("dll_paused_day") == lucid_day(now)

    def eligible(self, sig: dict) -> str | None:
        why = super().eligible(sig)
        if why is not None:
            return why
        now = datetime.now(UTC)
        if not self.active():
            return f"eval {self.ev['status']} (no more trading)"
        if self.dll_paused(now):
            return "daily loss limit hit (paused for the session)"
        return None

    def close(self, p, price, t, reason, extra=None):
        reason = {"stop_1atr": "stop_0.8atr"}.get(reason, reason)
        rec = super().close(p, price, t, reason, extra)
        d = self.s.setdefault("day", {"day": None, "net": 0.0})
        day = lucid_day(t)
        if d.get("day") != day:
            d.update({"day": day, "net": 0.0})
        d["net"] = round(float(d["net"]) + float(rec["net"]), 2)
        return rec

    def flatten(self, now: datetime, reason: str) -> list[dict]:
        out = []
        for p in list(self.s["positions"]):
            lp = self.s["last_price"].get(p["symbol"], p["entry"])
            out.append(self.close(p, self._slip(p, lp), now, reason, {"trigger": "eval_rule"}))
        if self.s["pending"]:
            self._event(now, "pending_dropped", reason=reason, symbols=[x["symbol"] for x in self.s["pending"]])
        self.s["pending"] = []
        return out

    def _end(self, now: datetime, status: str, reason: str) -> None:
        e = self.ev
        e.update({"status": status, "reason": reason, "at": now.astimezone(UTC).isoformat(),
                  "final_balance": self.s["balance"]})
        self._event(now, f"eval_{status.lower()}", reason=reason, balance=self.s["balance"], mll_floor=e["mll_floor"])
        log.warning("EVAL-80 %s: %s (balance %.2f)", status, reason, self.s["balance"])

    def roll_eod(self, now: datetime) -> bool:
        """At the first look in a new Lucid day: the previous day's close updates the trailing MLL."""
        e = self.ev
        day = lucid_day(now)
        if e.get("day") is None:
            e["day"] = day
            return True
        if e["day"] == day:
            return False
        bal = float(self.s["balance"])
        e["eod_closes"] = ((e.get("eod_closes") or []) + [{"day": e["day"], "balance": bal}])[-60:]
        e["highest_eod"] = max(float(e.get("highest_eod") or self.b["start_balance"]), bal)
        if not e.get("mll_locked"):
            if bal > float(self.b["initial_trail_balance"]) + 1e-9:
                e["mll_locked"] = True
                e["mll_floor"] = float(self.b["locked_mll_balance"])
            else:
                e["mll_floor"] = round(min(e["highest_eod"] - float(self.b["mll"]),
                                           float(self.b["locked_mll_balance"])), 2)
        self._event(now, "eod", day=e["day"], balance=bal, highest_eod=e["highest_eod"], mll_floor=e["mll_floor"],
                    mll_locked=e["mll_locked"])
        e["day"] = day
        return True

    def enforce(self, now: datetime) -> bool:
        """Apply the eval rules to the current balance / marks. Returns True when state changed."""
        changed = self.roll_eod(now)
        if not self.active():
            if self.s["positions"] or self.s["pending"]:
                self.flatten(now, f"eval_{self.ev['status'].lower()}")
                changed = True
            return changed
        e, b = self.ev, self.b
        op = self.open_pnl()
        eq = float(self.s["balance"]) + op
        e["min_equity"] = round(min(float(e.get("min_equity") or eq), eq), 2)
        if eq <= float(e["mll_floor"]) + 1e-9:
            self.flatten(now, "mll_breach")
            self._end(now, "FAILED", f"MLL: equity ${eq:,.2f} <= floor ${e['mll_floor']:,.2f}")
            return True
        if not self.dll_paused(now) and self.day_net(now) + op <= -float(b["dll"]) + 1e-9:
            self.flatten(now, "dll_1800")
            e["dll_paused_day"] = lucid_day(now)
            self._event(now, "dll_pause", day_net=self.day_net(now), day=e["dll_paused_day"])
            if float(self.s["balance"]) <= float(e["mll_floor"]) + 1e-9:
                self._end(now, "FAILED", f"MLL: balance ${self.s['balance']:,.2f} <= floor ${e['mll_floor']:,.2f}")
            return True
        target = float(self.s["start_balance"]) + float(b["profit_target"])
        if eq >= target - 1e-9:
            if self.s["positions"]:
                self.flatten(now, "eval_passed_flat")
            if float(self.s["balance"]) >= target - 1e-9:
                self._end(now, "PASSED", f"profit target: balance ${self.s['balance']:,.2f} >= ${target:,.0f}")
            return True
        return changed

    def eval_view(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        e, b = self.ev, self.b
        sb = float(self.s["start_balance"])
        op = self.open_pnl()
        profit = round(float(self.s["balance"]) - sb, 2)
        missed = dict(self.s["stats"].get("missed_scans") or {})
        return {"status": e.get("status"), "reason": e.get("reason"), "at": e.get("at"),
                "profit": profit, "profit_target": float(b["profit_target"]),
                "progress_pct": round(100 * max(0.0, profit) / float(b["profit_target"]), 1),
                "equity": round(float(self.s["balance"]) + op, 2),
                "mll_floor": e.get("mll_floor"), "mll_locked": bool(e.get("mll_locked")),
                "mll_room": round(float(self.s["balance"]) + op - float(e.get("mll_floor") or 0), 2),
                "highest_eod": e.get("highest_eod"), "dll_limit": float(b["dll"]),
                "dll_used": round(max(0.0, -(self.day_net(now) + op)), 2),
                "dll_paused": self.dll_paused(now), "lucid_day": lucid_day(now),
                "missed_scans": missed, "missed_total": int(sum(missed.values())),
                "eod_closes": (e.get("eod_closes") or [])[-10:]}

    # ------------------------------------------------------------------ intake (G-LOW-80's shared signals)
    def intake(self, record: dict | None, now: datetime) -> list[dict]:
        out = []
        if not record:
            return out
        key = record.get("run_at_utc")
        if key and key in self.s["seen_cycles"]:
            return out
        if key:
            self.s["seen_cycles"] = (self.s["seen_cycles"] + [key])[-200:]
        self._missed(record, now)
        for sig in record.get("signals") or []:
            if sig.get("action") not in ("BUY", "SELL"):
                continue
            self.s["stats"]["signals_seen"] += 1
            sym = str(sig.get("symbol") or "").upper()
            why = self.eligible(sig)
            if why is None and self.held(sym):
                why = "already in a position on this symbol (one per symbol)"
            if why is None and not self.entries_allowed(now):
                why = "outside session / last minutes before the 21:45 UTC flat"
            c = q = None
            if why is None:
                c, q, why = self.size_for(sym, float(sig["atr"]))
            ev = {"symbol": sym, "action": sig.get("action"), "confidence": sig.get("confidence"),
                  "atr": sig.get("atr"), "signal_entry": sig.get("entry"), "cycle": key, "session": sig.get("session")}
            if why is not None:
                self._skip(skip_key(why))
                self._event(now, "skip", reason=why, **ev)
                continue
            pe = {"symbol": sym, "side": 1 if sig["action"] == "BUY" else -1, "contract": c, "qty": q,
                  "micro": self.is_micro(c), "atr": float(sig["atr"]), "confidence": float(sig["confidence"]),
                  "signal_entry": sig.get("entry"), "signal_at": key or now.isoformat(),
                  "queued_at": now.isoformat(), "level_event": sig.get("level_event"),
                  "session": sig.get("session"), "rationale": (sig.get("rationale") or "")[:300]}
            self.s["pending"].append(pe)
            self._event(now, "queued", **ev, contract=c, qty=q)
            out.append(pe)
        return out

    async def on_shared(self, g: dict | None, snap=None, now: datetime | None = None) -> dict:
        """One evaluation cycle on G-LOW-80's response for this M15 close. Never raises."""
        now = now or getattr(snap, "now", None) or datetime.now(UTC)
        g = g or {}
        sig_at = g.get("signal_at") or now.astimezone(UTC).isoformat()
        out = {"book": BOOK, "source_book": SOURCE_BOOK, "model": self.b["model"],
               "thinking_level": self.b["thinking_level"], "ok": bool(g.get("ok")), "signal_at": sig_at,
               "eval_status": self.ev.get("status")}
        try:
            self.enforce(now)
            out["eval_status"] = self.ev.get("status")
            validated = [v for v in (g.get("validated") or []) if v.get("symbol") in self.b["symbols"]]
            c = self.s["cycles"]
            c["seen"] += 1
            if g.get("skipped") or not g.get("ok"):
                c["skipped"] += 1
                out["skipped"] = g.get("skipped") or f"G-LOW-80 call not ok: {g.get('error')}"
            held_by_source = [s for s, w in (g.get("excluded") or {}).items() if w == "held" and s in self.b["symbols"]]
            queued = []
            if g.get("ok"):
                if validated:
                    c["with_signals"] += 1
                queued = self.intake({"run_at_utc": sig_at, "signals": validated, "in_position": held_by_source}, now)
            elif held_by_source:
                self._missed({"in_position": held_by_source}, now)
            traded = {x["symbol"] for x in queued}
            out["queued"] = sorted(traded)
            out["missed_held_by_glow80"] = [s for s in held_by_source if not self.held(s)]
            out["signals"] = [{k: v.get(k) for k in ("symbol", "action", "confidence", "atr", "entry")} for v in validated]
            out["score_signals"] = [{**v, "signal_at": sig_at, "kind": "entry", "traded": v["symbol"] in traded,
                                     "book": BOOK, "model": self.b["model"], "thinking_level": self.b["thinking_level"],
                                     "prompt_version": "system_prompt.j2"}
                                    for v in validated] if g.get("ok") else []
            c["last"] = {"t": sig_at, "ok": bool(g.get("ok")), "signals": len(validated), "queued": out["queued"],
                         "eval_status": self.ev.get("status")}
            self._jsonl("eval80_signals.jsonl", now, {
                "t": sig_at, "book": BOOK, "source_book": SOURCE_BOOK, "model": self.b["model"],
                "thinking_level": self.b["thinking_level"], "snapshot_id": getattr(snap, "snapshot_id", None),
                "ok": bool(g.get("ok")), "skipped": out.get("skipped"), "error": g.get("error"),
                "missed_held_by_glow80": out["missed_held_by_glow80"], "eval": self.eval_view(now),
                "signals": [{**v, "traded": v["symbol"] in traded} for v in validated]})
            if queued:
                await self.tick(now)
            else:
                await self.persist()
            return out
        except Exception as e:
            log.exception("EVAL-80 on_shared failed (other books unaffected)")
            out["ok"] = False
            out["error"] = f"{type(e).__name__}: {e}"
            return out

    # ------------------------------------------------------------------ server loop
    async def tick(self, now: datetime | None = None) -> dict:
        if self.busy:
            return {"skipped": "busy"}
        now = now or datetime.now(UTC)
        if not (self.s["positions"] or self.s["pending"]):
            if self.roll_eod(now):
                await self.persist()
            return {"idle": True, "eval": self.ev.get("status")}
        res = await super().tick(now)
        self.busy = True
        try:
            if self.enforce(now):
                await self.persist()
        except Exception as e:
            log.exception("EVAL-80 enforce failed")
            res = {**(res or {}), "error": f"{type(e).__name__}: {e}"}
        finally:
            self.busy = False
        return res

    def site_view(self) -> dict:
        v = super().site_view()
        b = self.b
        st = self.s["stats"]
        v["account"]["stopped"] = st["exit_reasons"].get("stop_0.8atr", 0)
        for p, src in zip(v["open_positions"], self.s["positions"]):
            p["micro"] = self.is_micro(src["contract"])
        v["account"]["missed_scans_glow80_held"] = v["account"].pop("missed_scans_main_held", {})
        v.update({"model": b["model"], "thinking_level": b["thinking_level"], "source_book": SOURCE_BOOK,
                  "eval": self.eval_view(), "day": self.s.get("day"), "cycles": self.s.get("cycles"),
                  "rules": (f"No own model call: G-LOW-80's low-thinking {b['model']} response on the shared M15 "
                            f"snapshot. BUY/SELL with confidence >= {b['min_confidence']:g} on "
                            f"{', '.join(b['symbols'])} (no BTC). Stop {b['stop_atr']:g} ATR, target "
                            f"{b['target_atr']:g} ATR, {b['max_hold_minutes']} min cap, no model exits, no trailing, "
                            f"0.3 ATR fill check. ~${b['risk_usd']:g} risk: minis if one fits, else micros; max "
                            f"{b['max_minis_per_position']} minis per position, {b['max_minis']} minis / "
                            f"{int(b['max_minis']) * int(b['micros_per_mini'])} micros total. LucidPro 100K eval: "
                            f"DLL ${b['dll']:,.0f} (soft pause), ${b['mll']:,.0f} EOD trailing MLL (locks at "
                            f"${b['locked_mll_balance']:,.0f} after an EOD close above "
                            f"${b['initial_trail_balance']:,.0f}), fail on an intraday MLL touch, pass at "
                            f"+${b['profit_target']:,.0f}. Flat {b['flat_utc']} UTC."),
                  "limitation": ("G-LOW-80 does not re-send a symbol it already holds, so EVAL-80 gets no signal "
                                 "for that symbol that close (counted per symbol as missed scans).")})
        return v
