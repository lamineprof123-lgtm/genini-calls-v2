"""ev-v1: event-driven Claude entry stream. PAPER ONLY. Spec: /workspace/specs/claude_ev_v1.md. Runs next to the
timed books and replaces nothing.

  Books     EV-FABLE (claude-fable-5-2, effort xhigh, 32k max tokens, 90 s) and EV-OPUS (claude-opus-5-5, effort high,
            16k, 60 s), each its own $100k EventBook; EV-CONT / EV-REV / EV-TWIN code-only what-if controls.
  Feed      the shared EventFeed (signals/event_feed.py, M1 close +10 s); this engine acts on each CLOSED M5 bar
            (built from the shared M1 store) - i.e. 10 s after each M5 close. 08:00-20:30 UTC. ATR = M15 ATR(14).
  Triggers  LVL-SWEEP, LVL-BREAK, VOL-BURST, RVOL-SPIKE (not US100), VWAP-STRETCH (EvTriggerDetector, frozen).
  Merge     one event per symbol per M5 bar, triggers ordered as above; the primary sets event_direction.
  Guards    per AI book: 1 call / symbol / 10 min, same trigger type + level 1 call / 30 min, skip held symbol,
            50 calls / day, $20 (FABLE) / $8 (OPUS) per day. Skip (logged) when live_price_stale.
  Call      both books in parallel, same snapshot, structured output (schema 3.5), adaptive thinking; no retry after
            a timeout, one retry after a fast 429/5xx only if it can finish within 90 s of the M5 close.
  Gate      action BUY/SELL + validation (action must match play x event_direction) + answer <= 90 s after the M5
            close + server room >= 1.0 ATR; fill live + 1 tick, skip if > 0.3 ATR worse than the event bar close.
  Controls  every logged trigger (called or not) after the same room filter, entry at the bar close + the median
            observed AI fill delay, same bracket (also scored on 0.8 / 1.0), no account limits.
  Q3        each EV-OPUS / EV-FABLE BUY/SELL vs C-OPUS timed signals on the same symbol + side within 20 min after.
  Dry run   [ev_evt].live = false: triggers, skip reasons, would-be calls (estimated cost toward caps) and the controls
            are logged; NO API call, NO fill.
Logs (logs/<Dubai day>/): event_triggers / event_calls / event_signals / event_trades.jsonl.
State: state/ev_evt/engine.json, state/ev_fable/, state/ev_opus/. Site: ev_evt.json, ev_fable.json, ev_opus.json.
"""
from __future__ import annotations
import asyncio, copy, hashlib, json, logging, random, statistics, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import event_feed as EF
from .bracket import _dt, _iso
from .risk import _atomic_write

log = logging.getLogger("signals.ev_evt")
UTC = timezone.utc
DUBAI = ZoneInfo("Asia/Dubai")
TAG = "ev-v1"
ORDER = ["LVL-SWEEP", "LVL-BREAK", "VOL-BURST", "RVOL-SPIKE", "VWAP-STRETCH"]
LEVEL_LABELS = ("Session High", "Session Low", "Prev High", "Prev Low", "24h High", "24h Low", "VAH", "VAL", "POC",
                "VWAP", "NY OR High", "NY OR Low", "Call Wall #1", "Put Wall #1", "Gamma Flip")
CONTROLS = ("EV-CONT", "EV-REV", "EV-TWIN")

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["read", "checks", "play", "action", "confidence", "invalidation"],
          "properties": {"read": {"type": "string"}, "checks": {"type": "string"},
                         "play": {"type": "string", "enum": ["continue", "reverse", "none"]},
                         "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                         "confidence": {"type": "number"}, "invalidation": {"type": ["number", "null"]}}}

SYSTEM_PROMPT = """You are judging one market event that code has just detected on a closed 5-minute bar. Decide
whether it is a trade, and in which direction. Answer in JSON only.

What happens after you answer: if you choose BUY or SELL and the server's gate passes, the server
enters at the live price with a FIXED bracket: stop 1 ATR and target 1 ATR from the fill, 90-minute
maximum hold. ATR means the M15 ATR(14) given in the numbers. Nobody manages the trade after entry.
So only trade when you expect price to travel 1 ATR in your direction before it travels 1 ATR
against you.

<inputs>
- "event": the code-detected trigger(s) on the last closed M5 bar, the event level, the event bar,
  event_direction, and the room (in ATR) to the next labelled level above and below.
- The numeric context JSON and a "facts" JSON (computed in code). Numbers are authoritative: never
  read a price off the image when the JSON has it.
- An M5 chart (last 6 hours): cyan = up bar, magenta = down bar, the right-most solid bar is the
  event bar (marked with a white triangle), the solid white line is the event level. Orange = session
  VWAP; dashed dark grey = M15 EMA20; dashed light grey = M15 EMA50; dashed pink = resistance; dashed
  purple = support; dash-dot gold / teal = Call / Put Wall; dotted white = Gamma Flip.
- Data caveats: prices are spot/CFD proxies of the futures; volume is broker tick volume; CVD is an
  estimate. Treat volume and CVD as supporting hints, never as the reason for a trade.
</inputs>

<the_decision>
Every event has two possible plays:
- CONTINUE: trade in event_direction (the sweep reversal holds, the break follows through, the
  burst extends, the stretch keeps running).
- REVERSE: trade against event_direction (the sweep was a stop run before the real break, the
  break fails and snaps back, the burst exhausts, the stretch reverts to VWAP).
Or NONE: the event is noise, or the 1-ATR bracket does not fit the situation.

Must-haves for any trade:
  M1. Room: at least 1.0 ATR to the next labelled level in the trade direction (room_up_atr for
      BUY, room_down_atr for SELL). The target must be reachable.
  M2. The event bar's CLOSE supports the play: a sweep closed back inside the level (CONTINUE of a
      sweep), a break closed beyond it (CONTINUE of a break), or the bar's close already shows
      rejection of the move (REVERSE). Wicks alone are not enough.
Supporting points (1 each):
  S1. CVD label or cvd_slope agrees with the trade direction.
  S2. Session is LONDON_NY_OVERLAP or NEW_YORK_SESSION.
  S3. h1_trend agrees with the trade direction (for REVERSE of a VWAP-STRETCH: h1_trend is flat or
      agrees with the move back toward VWAP).
  S4. The event bar's volume is above its 20-bar average (US100: skip this point and count S5 twice).
  S5. VWAP side: CONTINUE trades keep price on the same side of VWAP as the trade; REVERSE trades
      point back toward VWAP.
Trade only if both must-haves hold AND at least 3 supporting points hold. Otherwise NONE / HOLD.
Never REVERSE a break when gex.regime is NEGATIVE_AMPLIFYING; never CONTINUE a VWAP-STRETCH when
price is already more than 3.5 ATR from VWAP.
</the_decision>

<output>
Fields in this order:
- read: max 40 words, what the event bar and the last hour show (closed bars only).
- checks: e.g. "M1 yes (room 1.7). M2 yes (closed back below 3925). S1 S2 S5. 3 of 5."
- play: "continue" | "reverse" | "none"
- action: "BUY" | "SELL" | "HOLD" (must match play and event_direction; "none" -> "HOLD")
- confidence: your probability (0-1) that price reaches +1 ATR in the trade direction before -1 ATR.
  Estimate it independently of your decision. For HOLD, the probability for the better of the two plays.
- invalidation: the price that would prove the idea wrong (logged for study; the server uses its own stop).
</output>"""

DEFAULTS = {
    "enabled": True, "live": False, "tag": TAG, "window_utc": ["08:00", "20:30"], "symbols": list(EF.EVENT_SYMBOLS),
    "sweep_atr": 0.05, "break_atr": 0.15, "burst_range_atr": 0.9, "burst_body_frac": 0.6, "rvol_mult": 2.5,
    "rvol_range_atr": 0.4, "rvol_bars": 20, "stretch_atr": 2.5, "rearm_atr": 1.5,
    "symbol_cooldown_min": 10, "trigger_cooldown_min": 30, "calls_per_day": 50, "late_seconds": 90,
    "room_min_atr": 1.0, "chart_bars": 72, "max_bar_lag_min": 3, "control_fill_delay_s": 15,
    "pair_window_min": 20, "path_hours": 4, "timed_sim": "C-OPUS", "reset_token": TAG,
}
AI_BOOKS = {
    "EV-FABLE": {"model": "claude-fable-5-2", "effort": "xhigh", "max_tokens": 32000, "timeout_s": 90,
                 "usd_per_day": 20.0, "est_cost_usd": 0.29, "prices": (8.0, 40.0, 10.0, 0.80)},
    "EV-OPUS": {"model": "claude-opus-5-5", "effort": "high", "max_tokens": 16000, "timeout_s": 60,
                "usd_per_day": 8.0, "est_cost_usd": 0.08, "prices": (4.0, 20.0, 5.0, 0.20)},
}
BOOK_DEFAULTS = {**EF.EVENT_BOOK_DEFAULTS, "reset_token": TAG, "enabled": True, "min_confidence": 0.0,
                 "max_trigger_adverse_atr": 0.3, "lock_minutes": 15}


def ev_cfg(cfg: dict) -> dict:
    sec = cfg.get("ev_evt") or {}
    cc = {**DEFAULTS, **{k: v for k, v in sec.items() if not isinstance(v, dict)}}
    cc["books"] = {b: {**d, **(sec.get(b.lower().replace("-", "_")) or {})} for b, d in AI_BOOKS.items()}
    return cc


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def labelled_levels(payload: dict) -> list[tuple[str, float]]:
    """The spec's labelled levels from the shared key_levels() builder (same values as the charts)."""
    from .bracket_v1 import key_levels
    return [(lab, px) for lab, px in key_levels(payload) if lab in LEVEL_LABELS]


def direction_word(d: int) -> str:
    return "up" if d > 0 else "down"


# ---------------------------------------------------------------------------------------------- detection
class EvTriggerDetector:
    """Frozen ev-v1 triggers on one closed M5 bar. State (VWAP-stretch arming, last bar) is a plain dict."""

    def __init__(self, cc: dict, state: dict | None = None):
        self.cc = cc
        self.st = state if state is not None else {}

    def detect(self, sym: str, payload: dict, m5: pd.DataFrame) -> dict | None:
        """m5: closed M5 bars, the last row is the event bar. Returns the merged event or None."""
        if m5 is None or len(m5) < 2:
            return None
        ctx = payload.get("context") or {}
        atr = _f(ctx.get("atr"))
        if not atr:
            return None
        d = int(ctx.get("digits") or 5)
        bar, prev = m5.iloc[-1], m5.iloc[-2]
        o, h, l, c, v = (float(bar[k]) for k in ("open", "high", "low", "close", "volume"))
        pc = float(prev["close"])
        rng = h - l
        trig = []
        levels = labelled_levels(payload)
        cc = self.cc
        for lab, L in levels:
            # LVL-SWEEP: level above (bar opened below) -> high > L + 0.05 ATR, close < L => down; mirror below
            if o < L and h > L + cc["sweep_atr"] * atr and c < L:
                trig.append({"type": "LVL-SWEEP", "level_label": lab, "level": round(L, d), "dir": -1,
                             "detail": f"high {round(h, d)} (+{(h - L) / atr:.2f} ATR above), closed {round(c, d)} (back below)"})
            elif o > L and l < L - cc["sweep_atr"] * atr and c > L:
                trig.append({"type": "LVL-SWEEP", "level_label": lab, "level": round(L, d), "dir": 1,
                             "detail": f"low {round(l, d)} (-{(L - l) / atr:.2f} ATR below), closed {round(c, d)} (back above)"})
            # LVL-BREAK: previous M5 closed on one side, this one closes beyond by >= 0.15 ATR
            if pc < L and c >= L + cc["break_atr"] * atr:
                trig.append({"type": "LVL-BREAK", "level_label": lab, "level": round(L, d), "dir": 1,
                             "detail": f"prev close {round(pc, d)} below, closed {round(c, d)} (+{(c - L) / atr:.2f} ATR above)"})
            elif pc > L and c <= L - cc["break_atr"] * atr:
                trig.append({"type": "LVL-BREAK", "level_label": lab, "level": round(L, d), "dir": -1,
                             "detail": f"prev close {round(pc, d)} above, closed {round(c, d)} (-{(L - c) / atr:.2f} ATR below)"})
        bdir = 1 if c > o else -1 if c < o else 0
        if rng >= cc["burst_range_atr"] * atr and abs(c - o) >= cc["burst_body_frac"] * rng and bdir:
            trig.append({"type": "VOL-BURST", "dir": bdir,
                         "detail": f"range {rng / atr:.2f} ATR, body {abs(c - o) / rng:.0%} of range"})
        n = int(cc["rvol_bars"])
        prior = m5["volume"].iloc[-n - 1:-1].astype(float)
        avg = float(prior.mean()) if len(prior) else 0.0
        if sym != "US100" and avg > 0 and v >= cc["rvol_mult"] * avg and rng >= cc["rvol_range_atr"] * atr and bdir:
            trig.append({"type": "RVOL-SPIKE", "dir": bdir, "detail": f"volume {v / avg:.1f}x the 20-bar average"})
        vwap = _f(ctx.get("vwap"))
        s = self.st.setdefault(sym, {"stretch_armed": True})
        if vwap is not None:
            dist = (c - vwap) / atr
            if abs(dist) <= cc["rearm_atr"]:
                s["stretch_armed"] = True
            elif abs(dist) >= cc["stretch_atr"] and s.get("stretch_armed", True):
                s["stretch_armed"] = False
                trig.append({"type": "VWAP-STRETCH", "dir": 1 if dist > 0 else -1, "level_label": "VWAP",
                             "level": round(vwap, d), "detail": f"close {dist:+.2f} ATR from session VWAP"})
        if not trig:
            return None
        trig.sort(key=lambda t: (ORDER.index(t["type"]), abs((t.get("level") or c) - c)))
        primary = trig[0]
        from .bracket_v1 import room_to_levels
        room = room_to_levels(c, atr, levels)
        bt = m5.index[-1].to_pydatetime()
        close_t = bt + timedelta(minutes=5)
        return {
            "id": f"{sym}|{_iso(close_t)}", "symbol": sym, "bar_close_utc": _iso(close_t),
            "triggers": [{k: t[k] for k in ("type", "level_label", "level", "detail") if k in t} for t in trig],
            "primary": primary["type"], "event_direction": direction_word(primary["dir"]),
            "event_level": primary.get("level"), "event_level_label": primary.get("level_label"),
            "event_bar": {"o": round(o, d), "h": round(h, d), "l": round(l, d), "c": round(c, d),
                          "range_atr": round(rng / atr, 2), "volume": v, "vol_avg20": round(avg, 2)},
            "room_up_atr": room["room_up_atr"], "room_down_atr": room["room_down_atr"],
            "room_up_level": room["room_up_level"], "room_down_level": room["room_down_level"],
            "atr": atr, "_bar_time": _iso(bt), "_keys": [f"{t['type']}|{t.get('level_label')}|{t.get('level')}"
                                                        for t in trig],
        }


def event_block(ev: dict) -> dict:
    return {k: ev[k] for k in ("symbol", "bar_close_utc", "triggers", "primary", "event_direction", "event_bar",
                               "room_up_atr", "room_down_atr")}


def room_ok(ev: dict, side: int, min_atr: float) -> bool:
    r = ev.get("room_up_atr") if side > 0 else ev.get("room_down_atr")
    return r is None or float(r) >= min_atr          # None = no labelled level beyond -> unlimited room


def validate(out: dict, event_direction: str) -> tuple[str, str | None, float]:
    """(action, mismatch reason, clamped confidence). action must equal play x event_direction."""
    play = str(out.get("play") or "none").lower()
    act = str(out.get("action") or "HOLD").upper()
    conf = max(0.0, min(1.0, _f(out.get("confidence")) or 0.0))
    ed = 1 if event_direction == "up" else -1
    want = "HOLD" if play == "none" else ("BUY" if (ed if play == "continue" else -ed) > 0 else "SELL")
    if act != want:
        return "HOLD", f"action {act} does not match play {play} x event_direction {event_direction} ({want})", conf
    return act, None, conf


# ---------------------------------------------------------------------------------------------- books
class EvBook(EF.EventBook):
    CORR_FILES: list[str] = []          # max one open per USD side PER BOOK (spec 4)

    def load_cfg(self, cfg: dict) -> dict:
        sec = cfg.get("ev_evt") or {}
        mine = sec.get(self.STATE_SUBDIR) or {}
        return {**BOOK_DEFAULTS, **{k: v for k, v in sec.items() if k in BOOK_DEFAULTS and not isinstance(v, dict)},
                **{k: v for k, v in mine.items() if k in BOOK_DEFAULTS}}

    def lock_until(self, t: datetime) -> datetime:
        return t + timedelta(minutes=float(self.b.get("lock_minutes", 15)))

    def guards(self) -> dict:
        return self.s.setdefault("guards", {"sym": {}, "keys": {}, "calls": []})

    def close(self, p, price, t, reason, extra=None):
        rec = super().close(p, price, t, reason, extra)
        try:
            row = {"tag": TAG, "kind": "real", **rec, "trigger_id": p.get("trigger_id"),
                   "fill_delay_s": p.get("fill_delay_s"), "slippage_atr": p.get("slippage_atr")}
            self._jsonl("event_trades.jsonl", t, row)
        except Exception:
            log.exception("event_trades write failed")
        return rec


class EvFableBook(EvBook):
    BOOK_NAME = "EV-FABLE"
    BOOK_LABEL = "Event · Claude Fable 5.2 (xhigh)"
    STATE_SUBDIR = "ev_fable"
    TRADES_LOG = "ev_fable_trades.jsonl"
    EVENTS_LOG = "ev_fable_events.jsonl"
    SITE_FILE = "ev_fable.json"


class EvOpusBook(EvBook):
    BOOK_NAME = "EV-OPUS"
    BOOK_LABEL = "Event · Claude Opus 5.5 (high)"
    STATE_SUBDIR = "ev_opus"
    TRADES_LOG = "ev_opus_trades.jsonl"
    EVENTS_LOG = "ev_opus_events.jsonl"
    SITE_FILE = "ev_opus.json"


# ---------------------------------------------------------------------------------------------- engine
class EvEngine:
    NAME = "ev_evt"

    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *, state_dir=None, logs_dir=None,
                 site_dir=None, call_fn=None, render_fn=None, quotes=None, clock=None):
        self.cfg = cfg
        self.cc = ev_cfg(cfg)
        self.io_lock = io_lock or asyncio.Lock()
        root = Path(state_dir or cfg["general"]["state_dir"])
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or (cfg.get("site") or {}).get("dir") or root.parent)
        self.dir = root / "ev_evt"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "engine.json"
        try:
            self.e = json.loads(self.path.read_text())
        except Exception:
            self.e = {}
        if self.e.get("tag") != self.cc["tag"]:
            self.e = {"tag": self.cc["tag"], "created_at": _iso(datetime.now(UTC)), "detector": {}, "last_bar": {},
                      "whatif": [], "stats": {}, "events": [], "ai_signals": [], "fill_delays": [],
                      "latency": {}, "dry_run_started_at": None, "pairs": []}
        kw = dict(state_dir=root, logs_dir=self.logs_dir, site_dir=self.site_dir, quotes=quotes)
        self.books = {"EV-FABLE": EvFableBook(cfg, self.io_lock, **kw), "EV-OPUS": EvOpusBook(cfg, self.io_lock, **kw)}
        self.detector = EvTriggerDetector(self.cc, self.e.setdefault("detector", {}))
        self._call = call_fn
        self._render = render_fn
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def live(self) -> bool:
        return bool(self.cc.get("live"))

    def _jsonl(self, name: str, t: datetime, obj: dict) -> None:
        d = self.logs_dir / t.astimezone(DUBAI).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a") as f:
            f.write(json.dumps({"tag": TAG, **obj}, default=str, separators=(",", ":")) + "\n")

    def _stat(self, scope: str, trig: str, key: str, n: float = 1) -> None:
        st = self.e.setdefault("stats", {}).setdefault(scope, {}).setdefault(trig, {})
        st[key] = round(st.get(key, 0) + n, 4)

    # ---------------- guards (per AI book)
    def calls_today(self, book: EvBook, now: datetime) -> list[dict]:
        d = now.astimezone(UTC).strftime("%Y-%m-%d")
        return [c for c in book.guards()["calls"] if c["t"][:10] == d]

    def spend_today(self, book: EvBook, now: datetime) -> float:
        return round(sum(float(c.get("cost") or 0) for c in self.calls_today(book, now)), 6)

    def skip_reason(self, name: str, ev: dict, now: datetime) -> str | None:
        book, bc = self.books[name], self.cc["books"][name]
        sym = ev["symbol"]
        if book.held(sym):
            return "position_held"
        g = book.guards()
        last = (g["sym"].get(sym) or {}).get("last_call_t")
        if last and now - _dt(last) < timedelta(minutes=float(self.cc["symbol_cooldown_min"])):
            return "symbol_cooldown_10m"
        k = ev["_keys"][0]
        lk = g["keys"].get(f"{sym}|{k}")
        if lk and now - _dt(lk) < timedelta(minutes=float(self.cc["trigger_cooldown_min"])):
            return "same_trigger_cooldown_30m"
        if len(self.calls_today(book, now)) >= int(self.cc["calls_per_day"]):
            return "daily_call_cap"
        if self.spend_today(book, now) >= float(bc["usd_per_day"]) - 1e-9:
            return "daily_usd_cap"
        return None

    def _mark_called(self, name: str, ev: dict, now: datetime, cost: float) -> None:
        g = self.books[name].guards()
        g["sym"].setdefault(ev["symbol"], {})["last_call_t"] = _iso(now)
        g["keys"][f"{ev['symbol']}|{ev['_keys'][0]}"] = _iso(now)
        g["calls"].append({"t": _iso(now), "cost": round(float(cost or 0), 6), "symbol": ev["symbol"]})
        cut = _iso(now - timedelta(days=2))
        g["calls"] = [c for c in g["calls"] if c["t"] >= cut]

    # ---------------- per M1 step (acts on new closed M5 bars only)
    async def on_feed(self, now: datetime, snaps: dict, store: EF.M1Store) -> dict:
        if self.e.get("dry_run_started_at") is None and not self.live:
            self.e["dry_run_started_at"] = _iso(now)
        out = {"events": []}
        win = EF.in_window(now, tuple(self.cc["window_utc"]))
        evs = []
        for sym in self.cc["symbols"]:
            p, m1 = snaps.get(sym), store.frame(sym)
            if p is None or m1 is None or not len(m1):
                continue
            m5 = EF.m5_bars(m1, now)
            if not len(m5):
                continue
            bt = m5.index[-1]
            if _iso(bt.to_pydatetime()) == self.e["last_bar"].get(sym):
                continue
            close_t = bt + pd.Timedelta(minutes=5)
            if (pd.Timestamp(now) - close_t) > pd.Timedelta(minutes=float(self.cc["max_bar_lag_min"])):
                self.e["last_bar"][sym] = _iso(bt.to_pydatetime())       # too old (restart / feed gap): skip it
                continue
            if (bt + pd.Timedelta(minutes=4)) not in m1.index:
                continue                                                # last M1 of the bar not in yet: next step
            self.e["last_bar"][sym] = _iso(bt.to_pydatetime())
            ev = self.detector.detect(sym, p, m5)
            if ev is None or not win:
                continue
            ctx = p.get("context") or {}
            ev["_payload_live"] = _f(ctx.get("live_price"))
            if ctx.get("live_price_stale"):
                ev["skip_all"] = "live_price_stale"
            evs.append((ev, p, m1))
        for ev, p, m1 in evs:
            out["events"].append(await self.handle_event(ev, p, m1, now, store))
        self.resolve_whatifs(now, store)
        await self.persist(now)
        return out

    async def handle_event(self, ev: dict, payload: dict, m1, now: datetime, store) -> dict:
        sym = ev["symbol"]
        reasons, callers = {}, []
        for name in self.books:
            r = ev.get("skip_all") or self.skip_reason(name, ev, now)
            if r:
                reasons[name] = r
            else:
                callers.append(name)
        for t in ev["triggers"]:
            self._stat("triggers", t["type"], f"n_{sym}")
            self._stat("triggers", t["type"], "n")
        self.e.setdefault("events", []).append({"t": ev["bar_close_utc"], "symbol": sym})
        self.e["events"] = [x for x in self.e["events"] if _dt(x["t"]) > now - timedelta(days=2)]
        self._jsonl("event_triggers.jsonl", now, {
            "ts_utc": _iso(now), "trigger_id": ev["id"], "symbol": sym, "bar_close_utc": ev["bar_close_utc"],
            "types": [t["type"] for t in ev["triggers"]], "triggers": ev["triggers"], "primary": ev["primary"],
            "event_direction": ev["event_direction"], "level": ev.get("event_level"),
            "level_label": ev.get("event_level_label"), "room_up_atr": ev["room_up_atr"],
            "room_down_atr": ev["room_down_atr"], "event_bar": ev["event_bar"], "atr": ev["atr"],
            "called_by": callers if self.live else [], "would_call": callers if not self.live else [],
            "skip_reasons": reasons, "dry_run": not self.live})
        self.add_controls(ev)
        if not callers or ev.get("skip_all"):
            return {"id": ev["id"], "called": [], "skips": reasons}
        if not self.live:
            for name in callers:
                est = float(self.cc["books"][name]["est_cost_usd"])
                self._mark_called(name, ev, now, est)
                self._jsonl("event_calls.jsonl", now, {"ts_utc": _iso(now), "book": name, "trigger_id": ev["id"],
                                                        "dry_run": True, "model": self.cc["books"][name]["model"],
                                                        "effort": self.cc["books"][name]["effort"],
                                                        "cost_estimate_usd": est})
            return {"id": ev["id"], "dry_run": True, "would_call": callers, "skips": reasons}
        # ---- live: same snapshot for both books, called in parallel
        content = await self.build_content(ev, payload, m1, now)
        res = await asyncio.gather(*[self.call_book(n, ev, payload, content, now) for n in callers],
                                   return_exceptions=True)
        out = {}
        for n, r in zip(callers, res):
            out[n] = r if not isinstance(r, BaseException) else {"error": f"{type(r).__name__}: {r}"}
        return {"id": ev["id"], "called": callers, "skips": reasons, "results": out}

    async def build_content(self, ev: dict, payload: dict, m1, now: datetime) -> list[dict]:
        from .claude_client import image_block
        p = copy.deepcopy(payload)
        ctx = EF.numeric_context(p)
        facts = EF.facts_at(p, now)
        chart = None
        try:
            mp = EF.m5_payload(p, m1, now, n=int(self.cc["chart_bars"]), event_level=ev.get("event_level"),
                               event_bar_time=_dt(ev["_bar_time"]), event_direction=ev["event_direction"])
            if mp is not None:
                render = self._render or EF.render
                charts = await render([mp], "ev")
                chart = charts.get(ev["symbol"]) or (next(iter(charts.values())) if charts else None)
        except Exception:
            log.exception("ev chart render failed")
        parts = [{"type": "text", "text": f"Event on {ev['symbol']} at {ev['bar_close_utc']}."},
                 {"type": "text", "text": "Event JSON: " + json.dumps({"event": event_block(ev)}, default=str)},
                 {"type": "text", "text": "Numeric context JSON: " + json.dumps(ctx, default=str)},
                 {"type": "text", "text": "Facts JSON: " + json.dumps(facts, default=str)}]
        if chart:
            parts.append(image_block(chart))
        parts.append({"type": "text", "text": "Return the JSON now."})
        return parts

    async def _default_call(self, bc: dict, content: list, timeout: float) -> dict:
        from .claude_client import messages_call
        return await messages_call(model=bc["model"], system=SYSTEM_PROMPT, content=content, schema=SCHEMA,
                                   max_tokens=int(bc["max_tokens"]), thinking={"type": "adaptive"},
                                   effort=bc["effort"], timeout=timeout, max_attempts=1,
                                   prices={bc["model"]: tuple(bc["prices"])})

    async def call_book(self, name: str, ev: dict, payload: dict, content: list, now: datetime) -> dict:
        bc = self.cc["books"][name]
        book = self.books[name]
        close_t = _dt(ev["bar_close_utc"])
        late_at = close_t + timedelta(seconds=float(self.cc["late_seconds"]))
        call = self._call or self._default_call
        t_start = self._clock()
        resp = await call(bc, content, float(bc["timeout_s"]))
        attempts = 1
        err = str(resp.get("error") or "")
        fast = (self._clock() - t_start).total_seconds() < 15
        if (not resp.get("ok") and fast and any(x in err for x in ("429", "RateLimit", "InternalServer", "500",
                                                                   "502", "503", "529", "Overloaded"))
                and "Timeout" not in err):
            left = (late_at - self._clock()).total_seconds()
            if left >= 20:
                resp = await call(bc, content, min(float(bc["timeout_s"]), left))
                attempts = 2
        t_ans = self._clock()
        usage = resp.get("usage") or {}
        cost = float(resp.get("cost_usd") or 0)
        self._mark_called(name, ev, now, cost)
        lat = (t_ans - close_t).total_seconds()
        self.e.setdefault("latency", {}).setdefault(name, []).append(round(lat, 2))
        self.e["latency"][name] = self.e["latency"][name][-500:]
        self._jsonl("event_calls.jsonl", now, {
            "ts_utc": _iso(now), "book": name, "trigger_id": ev["id"], "dry_run": False, "model": bc["model"],
            "model_returned": resp.get("model"), "effort": bc["effort"], "start_utc": _iso(t_start),
            "answer_utc": _iso(t_ans), "latency_ms": resp.get("latency_ms"), "since_bar_close_s": round(lat, 2),
            "attempts": attempts, "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "thinking_tokens": resp.get("thinking_tokens"), "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cost_usd": cost, "stop_reason": resp.get("stop_reason"), "error": resp.get("error")})
        if not resp.get("ok"):
            return {"ok": False, "error": resp.get("error")}
        out = resp.get("parsed") or {}
        act, mismatch, conf = validate(out, ev["event_direction"])
        late = t_ans > late_at
        ctx = payload.get("context") or {}
        live = _f(ctx.get("live_price"))
        side = 1 if act == "BUY" else -1 if act == "SELL" else 0
        gate, skip = "hold", mismatch
        if side:
            self.add_ai_whatif(name, ev, side, conf, t_ans)
            if late:
                gate, skip = "rejected", f"late: answer {lat:.0f} s after the M5 close (> {self.cc['late_seconds']} s)"
            elif not room_ok(ev, side, float(self.cc["room_min_atr"])):
                gate, skip = "rejected", "room < 1.0 ATR in the trade direction"
            else:
                skip = self.queue(book, ev, side, conf, out, now)
                gate = "passed" if skip is None else "rejected"
        self._jsonl("event_signals.jsonl", now, {
            "ts_utc": _iso(now), "book": name, "trigger_id": ev["id"], "symbol": ev["symbol"],
            **{k: out.get(k) for k in ("read", "checks", "play", "action", "invalidation")}, "confidence": conf,
            "validated_action": act, "validation": "ok" if mismatch is None else mismatch, "late": late,
            "gate_decision": gate, "skip_reason": skip, "answer_utc": _iso(t_ans), "live_price_at_answer": live,
            "event_direction": ev["event_direction"], "primary": ev["primary"],
            "path_from_utc": ev["bar_close_utc"], "path_to_utc": _iso(close_t + timedelta(hours=float(self.cc["path_hours"])))})
        res = {"ok": True, "action": act, "gate": gate, "skip": skip}
        if gate == "passed":
            r = await book.tick(self._clock())
            res["tick"] = r
            for p in book.s["positions"]:
                if p.get("trigger_id") == ev["id"] and p.get("fill_delay_s") is None:
                    p["fill_delay_s"] = round((_dt(p["entry_time"]) - close_t).total_seconds(), 2)
                    p["slippage_atr"] = round(p["side"] * (float(p["entry"]) - ev["event_bar"]["c"]) / ev["atr"], 3)
                    self.e.setdefault("fill_delays", []).append(p["fill_delay_s"])
                    self.e["fill_delays"] = self.e["fill_delays"][-200:]
                    self._jsonl("event_signals.jsonl", now, {"ts_utc": _iso(now), "book": name, "event": "fill",
                                                             "trigger_id": ev["id"], "fill_price": p["entry"],
                                                             "fill_delay_s": p["fill_delay_s"],
                                                             "slippage_atr": p["slippage_atr"]})
                    await book.persist()
        return res

    def queue(self, book: EvBook, ev: dict, side: int, conf: float, out: dict, now: datetime) -> str | None:
        sym, atr = ev["symbol"], ev["atr"]
        if book.held(sym):
            return "already_held"
        why = book.lock_reason(sym, side, now)
        if why:
            return why
        c = book.contract_for(sym)
        if c is None:
            return "no contract"
        q, why = book.size(c, atr)
        if why:
            return why
        new_risk = float(book.b["stop_atr"]) * atr * book.spec(c)["point_value"] * q
        why = book.risk_block(sym, side, new_risk, now)
        if why:
            return why
        bc = ev["event_bar"]["c"]
        book.s["pending"].append({
            "symbol": sym, "side": side, "contract": c, "qty": q, "atr": atr, "confidence": conf,
            "signal_entry": bc, "trigger_price": bc, "trigger_id": ev["id"], "trigger_type": ev["primary"],
            "signal_at": _iso(now), "queued_at": _iso(now), "play": out.get("play"),
            "invalidation": out.get("invalidation"), "tag": TAG})
        book._event(now, "queued", symbol=sym, side=side, contract=c, qty=q, trigger=ev["primary"])
        return None

    # ---------------- what-ifs: controls on every trigger + every AI BUY/SELL
    def control_delay_s(self) -> float:
        fd = self.e.get("fill_delays") or []
        return float(statistics.median(fd)) if fd else float(self.cc["control_fill_delay_s"])

    def add_controls(self, ev: dict) -> None:
        ed = 1 if ev["event_direction"] == "up" else -1
        seed = int(hashlib.sha256(ev["id"].encode()).hexdigest()[:8], 16)
        twin = 1 if random.Random(seed).random() < 0.5 else -1
        t_entry = _dt(ev["bar_close_utc"]) + timedelta(seconds=self.control_delay_s())
        for book, side in (("EV-CONT", ed), ("EV-REV", -ed), ("EV-TWIN", twin)):
            if not room_ok(ev, side, float(self.cc["room_min_atr"])):
                self._stat(book, ev["primary"], "room_skip")
                continue
            self.e.setdefault("whatif", []).append({"book": book, "trigger_id": ev["id"], "symbol": ev["symbol"],
                                                    "side": side, "entry": None, "t0": _iso(t_entry),
                                                    "atr": ev["atr"], "primary": ev["primary"],
                                                    "types": [t["type"] for t in ev["triggers"]]})

    def add_ai_whatif(self, name: str, ev: dict, side: int, conf: float, t_ans: datetime) -> None:
        self.e.setdefault("whatif", []).append({"book": name, "trigger_id": ev["id"], "symbol": ev["symbol"],
                                                "side": side, "entry": None, "t0": _iso(t_ans), "atr": ev["atr"],
                                                "primary": ev["primary"], "confidence": conf,
                                                "types": [t["type"] for t in ev["triggers"]]})

    @staticmethod
    def _entry_from_m1(bars, t0: datetime) -> float | None:
        """Entry at t0: open of the M1 bar that contains t0 (the first price after the fill delay)."""
        if bars is None or not len(bars):
            return None
        sel = bars[bars.index >= pd.Timestamp(t0).floor("min")]
        return None if sel.empty else float(sel["open"].iloc[0])

    def resolve_whatifs(self, now: datetime, store: EF.M1Store) -> None:
        keep = []
        for w in self.e.get("whatif") or []:
            bars = store.frame(w["symbol"])
            t0 = _dt(w["t0"])
            if w.get("entry") is None:
                w["entry"] = self._entry_from_m1(bars, t0)
                if w["entry"] is None:
                    if now - t0 < timedelta(hours=4):
                        keep.append(w)
                    continue
            r = EF.whatif_path(w["side"], w["entry"], w["atr"], t0, bars, now=now)
            if r is None and now - t0 < timedelta(hours=4):
                keep.append(w)
                continue
            r = r or {"resolved": True, "R": None, "exit_reason": "no_data"}
            r08 = EF.whatif_path(w["side"], w["entry"], w["atr"], t0, bars, stop_atr=0.8, target_atr=1.0,
                                 now=now + timedelta(hours=2))
            row = {"kind": "whatif", **w, **r, "R_0p8_1p0": (r08 or {}).get("R"),
                   "fill_delay_s": round((t0 - _dt(w["trigger_id"].split("|", 1)[1])).total_seconds(), 2)}
            self._jsonl("event_trades.jsonl", now, row)
            if r.get("R") is not None:
                for scope in (w["primary"], "ALL"):
                    self._stat(w["book"], scope, "n")
                    self._stat(w["book"], scope, "R", float(r["R"]))
                    self._stat(w["book"], scope, "wins", 1 if r["R"] > 0 else 0)
                if w["book"] in self.books:
                    self.e.setdefault("ai_signals", []).append({k: w.get(k) for k in (
                        "book", "trigger_id", "symbol", "side", "entry", "t0", "atr")} | {"R": r["R"]})
                    self.pair_q3(w, float(r["R"]), store, now)
        self.e["whatif"] = keep
        self.e["ai_signals"] = (self.e.get("ai_signals") or [])[-2000:]

    # ---------------- Q3 pairing vs the timed C-OPUS stream
    def timed_signals(self, t_from: datetime, t_to: datetime) -> list[dict]:
        rows = []
        days = {t_from.astimezone(DUBAI).strftime("%Y-%m-%d"), t_to.astimezone(DUBAI).strftime("%Y-%m-%d")}
        for d in days:
            f = self.logs_dir / d / "claude_signals.jsonl"
            if not f.exists():
                continue
            for line in f.read_text().splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("sim") != self.cc["timed_sim"] or str(r.get("action")).upper() not in ("BUY", "SELL"):
                    continue
                t = _dt(r.get("t"))
                if t is not None and t_from <= t <= t_to:
                    rows.append(r)
        return rows

    def pair_q3(self, w: dict, r_event: float, store, now: datetime) -> None:
        t0 = _dt(w["t0"])
        side_word = "BUY" if w["side"] > 0 else "SELL"
        cands = [r for r in self.timed_signals(t0, t0 + timedelta(minutes=float(self.cc["pair_window_min"])))
                 if str(r.get("symbol")).upper() == w["symbol"] and str(r.get("action")).upper() == side_word]
        row = {"book": w["book"], "trigger_id": w["trigger_id"], "symbol": w["symbol"], "side": side_word,
               "R_event": r_event, "vs": self.cc["timed_sim"]}
        if cands:
            c = sorted(cands, key=lambda r: r["t"])[0]
            te = _dt(c["t"])
            ent = _f(c.get("live_price")) or self._entry_from_m1(store.frame(w["symbol"]), te)
            rt = EF.whatif_path(w["side"], ent, w["atr"], te, store.frame(w["symbol"]), now=now)
            row.update({"paired": True, "timed_t": c["t"], "timed_entry": ent,
                        "R_timed": (rt or {}).get("R"),
                        "R_diff": None if not rt or rt.get("R") is None else round(r_event - rt["R"], 3),
                        "entry_improvement_atr": None if ent is None else round(w["side"] * (ent - w["entry"]) / w["atr"], 3)})
        else:
            row.update({"paired": False, "unique_catch": True})
        self.e.setdefault("pairs", []).append(row)
        self.e["pairs"] = self.e["pairs"][-2000:]
        self._jsonl("event_trades.jsonl", now, {"kind": "q3_pair", **row})

    def missed_by_triggers(self, now: datetime, hours: float = 24) -> int:
        evs = [(x["symbol"], _dt(x["t"])) for x in self.e.get("events") or []]
        n = 0
        for r in self.timed_signals(now - timedelta(hours=hours), now):
            t = _dt(r["t"])
            if not any(s == str(r.get("symbol")).upper() and t - timedelta(minutes=20) <= te <= t for s, te in evs):
                n += 1
        return n

    # ---------------- persistence / site
    def site_view(self, now: datetime) -> dict:
        d = now.astimezone(UTC).strftime("%Y-%m-%d")
        cards = []
        for name, b in self.books.items():
            a = b.site_view()["account"]
            st = (self.e.get("stats", {}).get(name) or {}).get("ALL", {})
            lat = sorted(self.e.get("latency", {}).get(name) or [])
            late = sum(1 for x in lat if x > float(self.cc["late_seconds"]))

            def pct(q):
                return None if not lat else lat[min(len(lat) - 1, int(round(q * (len(lat) - 1))))]
            cards.append({"book": name, "label": b.BOOK_LABEL, "model": self.cc["books"][name]["model"],
                          "effort": self.cc["books"][name]["effort"], "balance": a["balance"],
                          "trades": a["trades"], "signals": int(st.get("n", 0)), "R": round(st.get("R", 0.0), 3),
                          "R_per_signal": round(st["R"] / st["n"], 3) if st.get("n") else None,
                          "latency_p50_s": pct(0.5), "latency_p95_s": pct(0.95),
                          "late_share": round(late / len(lat), 3) if lat else None,
                          "cost_today": round(self.spend_today(b, now), 4) if self.live else 0.0,
                          "calls_today": len(self.calls_today(b, now)), "open_positions": a["open_positions"]})
        table = []
        trig = self.e.get("stats", {}).get("triggers", {})
        for t in ORDER:
            row = {"trigger": t, "n": int((trig.get(t) or {}).get("n", 0)),
                   "per_symbol": {s: int((trig.get(t) or {}).get(f"n_{s}", 0)) for s in self.cc["symbols"]}}
            for cb in CONTROLS:
                st = (self.e.get("stats", {}).get(cb) or {}).get(t, {})
                row[cb] = round(st["R"] / st["n"], 3) if st.get("n") else None
            table.append(row)
        pairs = [p for p in self.e.get("pairs") or [] if p.get("paired") and p.get("R_diff") is not None]
        return {"updated_at": _iso(now), "tag": TAG, "paper_only": True, "live": self.live,
                "mode": "live" if self.live else "dry_run (trigger-only, no API calls, no fills)",
                "dry_run_started_at": self.e.get("dry_run_started_at"), "cards": cards, "triggers": table,
                "controls": {cb: (self.e.get("stats", {}).get(cb) or {}).get("ALL", {}) for cb in CONTROLS},
                "q3": {"pairs": len(pairs),
                       "mean_R_diff": round(sum(p["R_diff"] for p in pairs) / len(pairs), 3) if pairs else None,
                       "unique_catches": sum(1 for p in self.e.get("pairs") or [] if p.get("unique_catch")),
                       "missed_by_triggers_24h": self.missed_by_triggers(now)}}

    async def persist(self, now: datetime) -> None:
        for b in self.books.values():
            await b.persist()
        view = self.site_view(now)
        async with self.io_lock:
            _atomic_write(self.path, self.e)
            _atomic_write(self.site_dir / "ev_evt.json", view)
