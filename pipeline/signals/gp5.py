"""G-P5, G-P5-EVAL and G-P5-EVENT: Gemini's P5 spec as three NEW side books (user t299u-t301u + scope update,
13 Nov 2026). PAPER ONLY. No existing book (MAIN / 1-strike / nostop / bracket, G-LOW-80, EVAL-80, EVAL-R4, Carbon,
Claude, C-OPUS-B, DS-FLASH) is changed or reset by this module.

  G-P5        its OWN gemini-3.8-flash call at every M15 close +20 s on the shared M15 snapshot, thinking LOW,
              P5 prompt (templates/system_prompt_p5.j2) + the five P5 context fields (signals/p5.py), symbols
              XAUUSD / EURUSD / US100 / XAGUSD (no GBPUSD, no BTCUSD), all four sent every close. Takes BUY/SELL
              with confidence >= 0.72 that pass the P5 gates. 0.8 ATR stop / 1.0 ATR target / 90-min cap, 30-min
              dead-trade cut, ~$900 risk full-size (G-LOW-80 sizing), daily loss stop $1,800. Own fresh $100k.
  G-P5-EVAL   NO own call: G-P5's response for the same snapshot. Confidence >= 0.75, P5 gates, same bracket +
              dead cut, ~$450 risk minis-else-micros, LucidPro 100K eval rules ENFORCED (as EVAL-80). Own $100k.
  G-P5-EVENT  G-P5's rules, but Gemini is called at the M15 close ONLY for symbols whose just-closed bar fired a
              trigger (p5.event_trigger): price within 0.25 ATR of Session High/Low, VAH/VAL/POC or a FirmTape GEX
              wall, or RVOL >= 2.0, or atr_ratio_vs_50bar >= 1.30. No trigger -> no call. When the triggered set is
              all four symbols, the request is byte-identical to G-P5's (same snapshot, prompt, model, symbols) and
              G-P5's response is reused instead of paying twice. Every evaluation is logged to
              logs/<day>/gp5event_triggers.jsonl (every symbol, every close, pass/fail per condition).
State: state/gp5/, state/gp5eval/, state/gp5event/. Site: gp5.json, gp5eval.json, gp5event.json. Panel: own lines.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from .eval80 import DEFAULTS as EVAL80_DEFAULTS, Eval80Book
from .glow80 import DEFAULTS as GLOW80_DEFAULTS, GLow80Book
from .p5 import P5_SYMBOLS

log = logging.getLogger("signals.gp5")
UTC = timezone.utc
RESET_TOKEN = "p5-2026-11-13"

P5_COMMON = {"p5": True, "p5_gates": True, "send_held": True, "dead_cut_minutes": 30, "stop_atr": 0.8,
             "target_atr": 1.0, "max_hold_minutes": 90, "symbols": list(P5_SYMBOLS)}
GP5_DEFAULTS = {**GLOW80_DEFAULTS, **P5_COMMON, "book": "G-P5", "min_confidence": 0.72,
                "scan_symbols": list(P5_SYMBOLS), "other_venue_symbols": [], "reset_token": RESET_TOKEN}
EVENT_DEFAULTS = {**GP5_DEFAULTS, "book": "G-P5-EVENT"}
EVAL_DEFAULTS = {**EVAL80_DEFAULTS, **P5_COMMON, "book": "G-P5-EVAL", "source_book": "G-P5",
                 "min_confidence": 0.75, "reset_token": RESET_TOKEN}


def _merge(defaults: dict, cfg: dict, key: str) -> dict:
    return {**defaults, **{k: v for k, v in (cfg.get(key) or {}).items() if k != "contracts"}}


def gp5_cfg(cfg: dict) -> dict:
    return _merge(GP5_DEFAULTS, cfg, "gp5")


def gp5event_cfg(cfg: dict) -> dict:
    return _merge(EVENT_DEFAULTS, cfg, "gp5event")


def gp5eval_cfg(cfg: dict) -> dict:
    return _merge(EVAL_DEFAULTS, cfg, "gp5eval")


def _rules(b: dict, extra: str = "") -> str:
    return (f"{extra}P5 prompt + 5 P5 context fields, gemini-3.8-flash thinking {b['thinking_level']}, at the M15 close "
            f"+20 s on the shared snapshot. BUY/SELL with confidence >= {b['min_confidence']:g} that pass the P5 "
            f"gates (not stretched >= 2.4 ATR from EMA20 / 3.5 ATR from VWAP / 3-bar move >= 2.2 ATR; >= 0.85 ATR room "
            f"to the opposing wall unless ATR ratio >= 1.3 and RVOL >= 2; no chase of a >= 1.8 ATR signal candle "
            f"without a 25% pullback). {', '.join(b['symbols'])} (no GBPUSD / BTC). Stop {b['stop_atr']:g} ATR / "
            f"target {b['target_atr']:g} ATR / {b['max_hold_minutes']} min cap, 30-min dead-trade cut (MFE < 0.25 "
            f"ATR and P&L <= -0.25 ATR). ~${b['risk_usd']:g} risk full-size, max {b['max_minis']} open, one per "
            f"symbol, 0.3 ATR fill check, daily loss stop ${b['daily_loss_usd']:g}.")


class GP5Book(GLow80Book):
    BOOK_NAME = "G-P5"
    BOOK_LABEL = "Gemini flash · G-P5 (P5 prompt, low thinking, min 0.72, P5 gates, 0.8/1.0 ATR, 30-min dead cut)"
    STATE_SUBDIR = "gp5"
    TRADES_LOG = "gp5_trades.jsonl"
    EVENTS_LOG = "gp5_events.jsonl"
    SITE_FILE = "gp5.json"
    SIGNALS_LOG = "gp5_signals.jsonl"

    def __init__(self, cfg, io_lock=None, **kw):
        super().__init__(cfg, io_lock, **kw)
        self.last_call: dict | None = None      # {"snapshot_id", "symbols", "resp"} for G-P5-EVENT reuse

    def load_cfg(self, cfg: dict) -> dict:
        return gp5_cfg(cfg)

    async def gemini(self, payloads, pngs):
        resp = await super().gemini(payloads, pngs)
        self.last_call = {"snapshot_id": getattr(getattr(self, "_snap", None), "snapshot_id", None),
                          "symbols": [p["symbol"] for p in payloads], "resp": resp}
        return resp

    def site_view(self) -> dict:
        v = super().site_view()
        v.update({"prompt_version": "p5", "rules": _rules(self.b, "Own call. "), "limitation": None})
        return v


class GP5EventBook(GP5Book):
    BOOK_NAME = "G-P5-EVENT"
    BOOK_LABEL = ("Gemini flash · G-P5-EVENT (G-P5, Gemini called only when something happened: level touch ≤0.25 "
                  "ATR, RVOL ≥2, ATR ratio ≥1.3)")
    STATE_SUBDIR = "gp5event"
    TRADES_LOG = "gp5event_trades.jsonl"
    EVENTS_LOG = "gp5event_events.jsonl"
    SITE_FILE = "gp5event.json"
    SIGNALS_LOG = "gp5event_signals.jsonl"
    TRIGGERS_LOG = "gp5event_triggers.jsonl"

    def __init__(self, cfg, io_lock=None, *, shared_from: GP5Book | None = None, **kw):
        super().__init__(cfg, io_lock, **kw)
        self.shared_from = shared_from
        if shared_from is not None and shared_from.hist is not None:
            self.hist = shared_from.hist          # one M15 history (H1 field) for both books
        self.s.setdefault("triggers", self._fresh_trig())
        self._fired: list[str] = []

    def load_cfg(self, cfg: dict) -> dict:
        return gp5event_cfg(cfg)

    @staticmethod
    def _fresh_trig() -> dict:
        return {"closes": 0, "evaluated": 0, "fired": 0, "by": {"level": 0, "rvol": 0, "atr_ratio": 0},
                "calls": 0, "shared_calls": 0, "own_calls": 0, "symbols_sent": 0, "no_call_closes": 0, "last": {}}

    def fresh(self) -> dict:
        s = super().fresh()
        s["triggers"] = self._fresh_trig()
        return s

    def scan_symbols(self, snap_symbols, now):
        out, why = super().scan_symbols(snap_symbols, now)
        keep = []
        for sym in out:
            t = self._p5_triggers.get(sym)
            if t and t.get("fired"):
                keep.append(sym)
            else:
                why[sym] = "no event trigger"
        self._fired = list(keep)
        return keep, why

    async def gemini(self, payloads, pngs):
        syms = [p["symbol"] for p in payloads]
        src = self.shared_from
        sid = getattr(getattr(self, "_snap", None), "snapshot_id", None)
        lc = getattr(src, "last_call", None) if src is not None else None
        st = self.s["triggers"]
        if lc and lc.get("snapshot_id") == sid and lc.get("symbols") == syms and lc.get("resp"):
            st["shared_calls"] += 1
            self._shared = True
            return {**lc["resp"], "shared_from": src.BOOK_NAME}
        self._shared = False
        st["own_calls"] += 1
        return await GLow80Book.gemini(self, payloads, pngs)

    async def on_snapshot(self, snap, now: datetime | None = None) -> dict:
        now = now or getattr(snap, "now", None) or datetime.now(UTC)
        self._fired, self._shared = [], None
        out = await super().on_snapshot(snap, now)
        st = self.s.setdefault("triggers", self._fresh_trig())
        trig = {s: t for s, t in self._p5_triggers.items() if s in self.b["scan_symbols"]}
        if trig:
            st["closes"] += 1
            st["evaluated"] += len(trig)
            st["fired"] += sum(1 for t in trig.values() if t.get("fired"))
            for t in trig.values():
                for k in t.get("by") or []:
                    st["by"][k] = st["by"].get(k, 0) + 1
        called = bool(out.get("symbols"))
        if called:
            st["calls"] += 1
            st["symbols_sent"] += len(out["symbols"])
        else:
            st["no_call_closes"] += 1
        st["last"] = {"t": now.astimezone(UTC).isoformat(), "fired": self._fired, "called": called,
                      "shared": self._shared}
        out["fired"] = self._fired
        out["shared_call"] = self._shared
        if self._shared:
            out["cost_usd"] = 0.0
        self._jsonl(self.TRIGGERS_LOG, now, {
            "t": now.astimezone(UTC).isoformat(), "book": self.BOOK_NAME,
            "snapshot_id": getattr(snap, "snapshot_id", None), "bar_close_ts": str(getattr(snap, "bar_close_ts", "")),
            "fired": self._fired, "gemini_called": called, "shared_with_gp5": self._shared,
            "excluded": out.get("excluded"), "triggers": trig,
            "signals": out.get("signals")})
        try:
            await self.persist()
        except Exception:
            log.exception("G-P5-EVENT persist failed")
        return out

    def trigger_view(self) -> dict:
        st = self.s.setdefault("triggers", self._fresh_trig())
        return {k: st.get(k) for k in self._fresh_trig()}

    def site_view(self) -> dict:
        v = super().site_view()
        v["trigger"] = self.trigger_view()
        v["rules"] = _rules(self.b, "Event-driven: Gemini called at the M15 close only for symbols whose closed bar "
                                    "came within 0.25 ATR of Session High/Low, VAH/VAL/POC or a GEX wall, or printed "
                                    "RVOL >= 2.0, or ATR ratio vs 50 bars >= 1.30 (request shared with G-P5 when "
                                    "identical). ")
        return v


class GP5EvalBook(Eval80Book):
    BOOK_NAME = "G-P5-EVAL"
    BOOK_LABEL = "Gemini flash · G-P5-EVAL (G-P5's call, min 0.75, P5 gates, 0.8/1.0 ATR, ~$450, Lucid eval rules)"
    STATE_SUBDIR = "gp5eval"
    TRADES_LOG = "gp5eval_trades.jsonl"
    EVENTS_LOG = "gp5eval_events.jsonl"
    SITE_FILE = "gp5eval.json"
    SIGNALS_LOG = "gp5eval_signals.jsonl"
    SOURCE_BOOK = "G-P5"

    def load_cfg(self, cfg: dict) -> dict:
        return gp5eval_cfg(cfg)

    def site_view(self) -> dict:
        v = super().site_view()
        b = self.b
        v.update({"source_book": "G-P5", "prompt_version": "p5", "limitation": None,
                  "rules": (f"No own model call: G-P5's low-thinking P5 response on the same snapshot. BUY/SELL with "
                            f"confidence >= {b['min_confidence']:g} that pass the P5 gates, {', '.join(b['symbols'])}. "
                            f"Stop {b['stop_atr']:g} ATR / target {b['target_atr']:g} ATR / {b['max_hold_minutes']} min, "
                            f"30-min dead-trade cut, ~${b['risk_usd']:g} risk (minis if one fits, else micros; "
                            f"{b['max_minis_per_position']} minis per position, {b['max_minis']} total). LucidPro 100K "
                            f"eval: DLL ${b['dll']:,.0f}, ${b['mll']:,.0f} EOD trailing MLL (locks at "
                            f"${b['locked_mll_balance']:,.0f}), fail on touch, pass at +${b['profit_target']:,.0f}, no "
                            f"auto-reset. Flat {b['flat_utc']} UTC.")})
        return v
