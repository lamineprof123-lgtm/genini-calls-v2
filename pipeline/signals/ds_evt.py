"""ds-evt-v1: DeepSeek event-triggered paper books DS-IMG-E (chart image + text) and DS-TXT-E (CSV table + text).
PAPER ONLY. Spec: /workspace/specs/ds_evt_v1.md (user, 13 Nov 2026), sections 1-11. No existing book is changed.

  Feed      the shared EventFeed (signals/event_feed.py): every M1 close +10 s, the shared numeric-context /
            live-price builder for XAUUSD / EURUSD / US100.
  Triggers  T1-T8 (DsTriggerDetector) on the live M1 feed, at most once per M15 bar per trigger per symbol.
  Guards    per book and symbol: 3 min between calls, 1 M15 bar between the same trigger ID, 1 bar between the same
            T1/T2 level (label + price), 2 bars after a HOLD for that trigger ID; 08:00-20:30 UTC only; never when
            session_blocks_automation / ASIAN_SESSION_CHOP / LOW_LIQUIDITY_DEADZONE / live_price_stale / held /
            re-entry lock. Per book: 4 calls per rolling hour and $3 per UTC day, checked before each call.
  Call      one batched deepseek-flash call per book per fire (all symbols that fired on the same M1 step),
            json_object, temperature 0, max 3,000 output tokens, 60 s timeout, no retry; returned model must
            contain "V4.1-Flash" else the cycle is skipped.
  Server    gate BUY/SELL and score >= 5 (confidence logged only); EventBook rules (signals/event_feed.py).
  What-if   every BUY/SELL (any score) on the same bracket + a coin-flip twin at the same timestamp.
  Dry run   [ds_evt].live = false: triggers, guards, would-be calls and caps are all logged; NO API call, NO fill.
Logs (logs/<Dubai day>/): ds_evt_triggers / ds_evt_suppressed / ds_evt_calls / ds_evt_signals / ds_evt_whatif.jsonl,
ds_img_e_* / ds_txt_e_* trades + events. State: state/ds_evt/engine.json, state/ds_img_e/, state/ds_txt_e/.
Site: ds_evt.json (cards + per-trigger table), ds_img_e.json, ds_txt_e.json.
"""
from __future__ import annotations
import asyncio, base64, copy, hashlib, json, logging, random, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import event_feed as EF
from .bracket import _dt, _iso
from .config import ROOT
from .risk import _atomic_write

log = logging.getLogger("signals.ds_evt")
UTC = timezone.utc
DUBAI = ZoneInfo("Asia/Dubai")
TAG = "ds-evt-v1"
TRIGGERS = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"]
PRIORITY = ["T2", "T1", "T6", "T8", "T3", "T4", "T5", "T7"]      # primary trigger when several fire together
LEVEL_TRIGGERS = ("T1", "T2", "T6", "T8")
BLOCKED_SESSIONS = ("ASIAN_SESSION_CHOP", "LOW_LIQUIDITY_DEADZONE")
SUPPRESS_REASONS = ("debounced", "cooldown_after_hold", "per_hour_cap", "per_day_cap", "session_blocked",
                    "stale_quote", "locked_reentry", "already_held")
FIELDS = ("symbol", "level_event", "rationale", "setup_type", "score", "action", "confidence", "stop_loss",
          "take_profit")

DEFAULTS = {
    "enabled": True, "live": False, "tag": TAG,
    "model": "deepseek-flash", "model_assert": "V4.1-Flash", "base_url": "https://api.deepseek.com",
    "timeout_seconds": 60, "max_output_tokens": 3000, "temperature": 0,
    "window_utc": ["08:00", "20:30"], "symbols": list(EF.EVENT_SYMBOLS),
    "calls_per_hour": 4, "usd_per_day": 3.0, "dry_run_cost_estimate_usd": 0.004,
    "min_call_gap_s": 180, "trigger_gap_bars": 1, "level_gap_bars": 1, "hold_cooldown_bars": 2,
    "t1_band_atr": 0.1, "t3_rvol": 2.0, "t7_hold_closes": 2, "min_score": 5, "csv_bars": 100,
    "reset_token": TAG,
}
BOOK_DEFAULTS = {**EF.EVENT_BOOK_DEFAULTS, "reset_token": TAG, "enabled": True, "min_confidence": 0.0}


def ds_evt_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **{k: v for k, v in (cfg.get("ds_evt") or {}).items() if not isinstance(v, dict)}}


def system_prompt(mode: str, symbols: list[str]) -> str:
    d = ROOT / "templates" / "ds_evt"
    base = (d / "system_v1.txt").read_text()
    inputs = (d / f"inputs_{mode}_v1.txt").read_text().strip()
    return base.replace("{INPUTS_SECTION}", inputs).replace("{symbol_order}", ", ".join(symbols)).strip()


def _sign(x: float | None) -> int:
    if x is None:
        return 0
    return 1 if x > 0 else -1 if x < 0 else 0


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------- detection
class DsTriggerDetector:
    """T1-T8 on the live M1 feed. Pure state machine (state dict persisted by the engine). Detection is shared by
    both books; the per-book guards are applied afterwards."""

    def __init__(self, cc: dict, state: dict | None = None):
        self.cc = cc
        self.st = state if state is not None else {}

    def _sym(self, sym: str, bar_open: str) -> dict:
        s = self.st.setdefault(sym, {})
        if s.get("bar") != bar_open:
            s.update({"bar": bar_open, "fired": [], "touches": {}, "t2_done": [], "prev_rvol": None})
        return s

    def detect(self, sym: str, payload: dict, now: datetime) -> list[dict]:
        ctx = payload.get("context") or {}
        live, atr = _f(ctx.get("live_price")), _f(ctx.get("atr"))
        bo = ctx.get("forming_m15_open_utc") or _iso(EF.m15_open(now))
        s = self._sym(sym, bo)
        if live is None or not atr:
            return []
        d = int(ctx.get("digits") or 5)
        out = []
        prev = _f(s.get("prev_live"))
        mib = ctx.get("minutes_into_bar")
        base = {"trigger_price": round(live, d), "trigger_utc": ctx.get("live_price_ts_utc") or _iso(now),
                "bar_open_utc": bo, "minutes_into_bar": mib}

        def fire(t, label=None, price=None, resolution=None, **extra):
            if t in s["fired"]:
                return
            s["fired"].append(t)
            out.append({"type": t, "level_label": label, "level_price": None if price is None else round(price, d),
                        **base, "event_resolution": resolution, **extra})

        band = float(self.cc["t1_band_atr"]) * atr
        fo, fh, fl = _f(ctx.get("forming_m15_open")), _f(ctx.get("forming_m15_high")), _f(ctx.get("forming_m15_low"))
        levels = [(str(lv.get("label") or lv.get("type") or "level"), float(lv["price"]))
                  for lv in ctx.get("levels") or [] if isinstance(lv, dict) and lv.get("price") is not None]
        # T1: live price enters the 0.1 ATR band around a levels[] entry (touches are remembered for T2)
        for lab, px in levels:
            inside = abs(live - px) <= band
            was = prev is not None and abs(prev - px) <= band
            key = f"{lab}@{round(px, d)}"
            if inside and prev is not None and not was:
                if key not in s["touches"]:
                    orig = _sign((fo if fo is not None else prev) - px) or _sign(prev - px)
                    s["touches"][key] = {"label": lab, "price": px, "orig": orig}
                fire("T1", lab, px, level_key=key)
        # T2: after a T1 touch, the same bar resolves closed_through / swept_closed_back
        for key, tch in s["touches"].items():
            if key in s["t2_done"] or "T2" in s["fired"] or not tch.get("orig"):
                continue
            px, o = tch["price"], tch["orig"]
            res = None
            if _sign(live - px) == -o:
                res = "closed_through"
            elif _sign(live - px) == o and ((o > 0 and fl is not None and fl < px) or (o < 0 and fh is not None and fh > px)):
                res = "swept_closed_back"
            if res:
                s["t2_done"].append(key)
                fire("T2", tch["label"], px, res, level_key=key)
        # T3: forming-bar RVOL crosses 2.0 (not US100)
        rv = _f(ctx.get("forming_rvol"))
        if sym != "US100" and rv is not None:
            pr = _f(s.get("prev_rvol"))
            if pr is not None and pr < float(self.cc["t3_rvol"]) <= rv:
                fire("T3", forming_rvol=rv)
            s["prev_rvol"] = rv
        last_close = _f(ctx.get("last_close"))
        # T4 / T5: sign(live - VWAP / EMA50) flips vs the last closed bar
        for t, key in (("T4", "vwap"), ("T5", "ema50")):
            ref = _f(ctx.get(key))
            if ref is not None and last_close is not None:
                a, b = _sign(live - ref), _sign(last_close - ref)
                if a and b and a != b:
                    fire(t, **{f"{key}": ref})
        # T6: live price enters atr_upper / atr_lower
        up, lo = _f(ctx.get("atr_upper")), _f(ctx.get("atr_lower"))
        if prev is not None:
            if up is not None and live >= up > prev:
                fire("T6", "ATR Upper", up)
            elif lo is not None and live <= lo < prev:
                fire("T6", "ATR Lower", lo)
        # T7: sign(cvd_slope) flips and holds 2 M1 closes
        cs = _sign(_f(ctx.get("cvd_slope")))
        if cs:
            stable = s.get("cvd_stable")
            if stable is None:
                s["cvd_stable"], s["cvd_n"] = cs, 0
            elif cs != stable:
                s["cvd_n"] = int(s.get("cvd_n") or 0) + 1
                if s["cvd_n"] >= int(self.cc["t7_hold_closes"]):
                    s["cvd_stable"], s["cvd_n"] = cs, 0
                    fire("T7", cvd_slope=ctx.get("cvd_slope"))
            else:
                s["cvd_n"] = 0
        # T8: US100 / XAUUSD, NY cash only: sign(live - gamma_flip) flips vs the last closed bar
        gf = _f((ctx.get("gex") or {}).get("gamma_flip"))
        if sym in ("US100", "XAUUSD") and gf is not None and last_close is not None and EF.ny_cash(now):
            a, b = _sign(live - gf), _sign(last_close - gf)
            if a and b and a != b:
                fire("T8", "Gamma Flip", gf)
        s["prev_live"] = live
        return out


# ---------------------------------------------------------------------------------------------- books
class DsEvtBook(EF.EventBook):
    MODE = "img"
    STATE_SUBDIR = "ds_img_e"
    CORR_FILES = ["deepseek/account.json", "carbon/CB-BRACKET.json", "carbon/CB-RUNNER.json",
                  "ds_img_e/account.json", "ds_txt_e/account.json"]

    def load_cfg(self, cfg: dict) -> dict:
        sec = (cfg.get("ds_evt") or {}).get(self.STATE_SUBDIR) or {}
        return {**BOOK_DEFAULTS, **{k: v for k, v in (cfg.get("ds_evt") or {}).items()
                                    if k in BOOK_DEFAULTS and not isinstance(v, dict)}, **sec}

    def guards(self) -> dict:
        return self.s.setdefault("guards", {"sym": {}, "calls": []})

    def g_sym(self, sym: str) -> dict:
        return self.guards()["sym"].setdefault(sym, {"last_call_t": None, "by_type": {}, "by_level": {},
                                                     "hold_until": {}, "last_call_bar": None})


class DsImgBook(DsEvtBook):
    BOOK_NAME = "DS-IMG-E"
    BOOK_LABEL = "DeepSeek · chart · event"
    MODE = "img"
    STATE_SUBDIR = "ds_img_e"
    TRADES_LOG = "ds_img_e_trades.jsonl"
    EVENTS_LOG = "ds_img_e_events.jsonl"
    SITE_FILE = "ds_img_e.json"


class DsTxtBook(DsEvtBook):
    BOOK_NAME = "DS-TXT-E"
    BOOK_LABEL = "DeepSeek · text · event"
    MODE = "txt"
    STATE_SUBDIR = "ds_txt_e"
    TRADES_LOG = "ds_txt_e_trades.jsonl"
    EVENTS_LOG = "ds_txt_e_events.jsonl"
    SITE_FILE = "ds_txt_e.json"


# ---------------------------------------------------------------------------------------------- engine
class DsEvtEngine:
    NAME = "ds_evt"

    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *, state_dir=None, logs_dir=None,
                 site_dir=None, call_fn=None, render_fn=None, quotes=None, clock=None):
        self.cfg = cfg
        self.cc = ds_evt_cfg(cfg)
        self.io_lock = io_lock or asyncio.Lock()
        root = Path(state_dir or cfg["general"]["state_dir"])
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or (cfg.get("site") or {}).get("dir") or root.parent)
        self.dir = root / "ds_evt"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "engine.json"
        try:
            self.e = json.loads(self.path.read_text())
        except Exception:
            self.e = {}
        if self.e.get("tag") != self.cc["tag"]:
            self.e = {"tag": self.cc["tag"], "created_at": _iso(datetime.now(UTC)), "detector": {},
                      "whatif": [], "stats": {}, "dry_run_started_at": None}
        kw = dict(state_dir=root, logs_dir=self.logs_dir, site_dir=self.site_dir, quotes=quotes)
        self.books = [DsImgBook(cfg, self.io_lock, **kw), DsTxtBook(cfg, self.io_lock, **kw)]
        self.detector = DsTriggerDetector(self.cc, self.e.setdefault("detector", {}))
        self._call = call_fn
        self._render = render_fn
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def live(self) -> bool:
        return bool(self.cc.get("live"))

    # ---------------- logging
    def _jsonl(self, name: str, t: datetime, obj: dict) -> None:
        d = self.logs_dir / t.astimezone(DUBAI).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a") as f:
            f.write(json.dumps({"tag": TAG, **obj}, default=str, separators=(",", ":")) + "\n")

    def _stat(self, book: str, trig: str, key: str, n: int = 1) -> None:
        st = self.e.setdefault("stats", {}).setdefault(book, {}).setdefault(trig, {})
        st[key] = st.get(key, 0) + n

    def _suppress(self, now, book, sym, trig, reason):
        self._stat(book.BOOK_NAME, trig["type"], f"suppressed_{reason}")
        self._jsonl("ds_evt_suppressed.jsonl", now, {
            "ts_utc": _iso(now), "book": book.BOOK_NAME, "symbol": sym, "trigger_type": trig["type"],
            "level_label": trig.get("level_label"), "trigger_price": trig.get("trigger_price"),
            "suppressed_reason": reason, **({"event": "skipped_locked"} if reason == "locked_reentry" else {})})

    # ---------------- guards
    def symbol_reason(self, book: DsEvtBook, sym: str, ctx: dict, now: datetime) -> str | None:
        if ctx.get("session_blocks_automation") or ctx.get("session") in BLOCKED_SESSIONS:
            return "session_blocked"
        if ctx.get("live_price_stale"):
            return "stale_quote"
        if book.held(sym):
            return "already_held"
        if book.symbol_locked(sym, now):
            return "locked_reentry"
        return None

    def trigger_reason(self, book: DsEvtBook, sym: str, trig: dict, now: datetime) -> str | None:
        g = book.g_sym(sym)
        bar = EF.bar_index(now)
        if g.get("last_call_t") and (now - _dt(g["last_call_t"])).total_seconds() < float(self.cc["min_call_gap_s"]):
            return "debounced"
        lb = g["by_type"].get(trig["type"])
        if lb is not None and bar - int(lb) <= int(self.cc["trigger_gap_bars"]):
            return "debounced"
        if trig["type"] in ("T1", "T2") and trig.get("level_key"):
            ll = g["by_level"].get(trig["level_key"])
            if ll is not None and bar - int(ll) <= int(self.cc["level_gap_bars"]):
                return "debounced"
        hu = g["hold_until"].get(trig["type"])
        if hu is not None and bar <= int(hu):
            return "cooldown_after_hold"
        return None

    def spend_today(self, book: DsEvtBook, now: datetime) -> float:
        d = now.astimezone(UTC).strftime("%Y-%m-%d")
        return round(sum(float(c.get("cost") or 0) for c in book.guards()["calls"] if c["t"][:10] == d), 6)

    def calls_last_hour(self, book: DsEvtBook, now: datetime) -> int:
        cut = now - timedelta(hours=1)
        return sum(1 for c in book.guards()["calls"] if _dt(c["t"]) > cut)

    def cap_reason(self, book: DsEvtBook, now: datetime) -> str | None:
        if self.calls_last_hour(book, now) >= int(self.cc["calls_per_hour"]):
            return "per_hour_cap"
        if self.spend_today(book, now) >= float(self.cc["usd_per_day"]) - 1e-9:
            return "per_day_cap"
        return None

    # ---------------- per M1 step
    async def on_feed(self, now: datetime, snaps: dict, store: EF.M1Store) -> dict:
        if self.e.get("dry_run_started_at") is None and not self.live:
            self.e["dry_run_started_at"] = _iso(now)
        out = {"fired": {}, "calls": {}}
        win = EF.in_window(now, tuple(self.cc["window_utc"]))
        fired = {}
        for sym in self.cc["symbols"]:
            p = snaps.get(sym)
            if p is None:
                continue
            trigs = self.detector.detect(sym, p, now)
            if trigs and win:
                fired[sym] = sorted(trigs, key=lambda t: PRIORITY.index(t["type"]))
        out["fired"] = {s: [t["type"] for t in v] for s, v in fired.items()}
        for sym, trigs in fired.items():
            for t in trigs:
                self._jsonl("ds_evt_triggers.jsonl", now, {"ts_utc": _iso(now), "symbol": sym, **t,
                                                           "live": self.live})
                for b in self.books:
                    self._stat(b.BOOK_NAME, t["type"], "fired")
        for book in self.books:
            try:
                out["calls"][book.BOOK_NAME] = await self._book_step(book, now, snaps, fired, store)
            except Exception as ex:
                log.exception("%s step failed", book.BOOK_NAME)
                out["calls"][book.BOOK_NAME] = {"error": f"{type(ex).__name__}: {ex}"}
        self.resolve_whatifs(now, store)
        await self.persist(now)
        return out

    async def _book_step(self, book: DsEvtBook, now, snaps, fired, store) -> dict:
        batch = []
        for sym in self.cc["symbols"]:
            trigs = fired.get(sym)
            if not trigs:
                continue
            why = self.symbol_reason(book, sym, snaps[sym].get("context") or {}, now)
            ok = []
            for t in trigs:
                r = why or self.trigger_reason(book, sym, t, now)
                if r:
                    self._suppress(now, book, sym, t, r)
                else:
                    ok.append(t)
            if ok:
                batch.append((sym, ok))
        if not batch:
            return {"called": False}
        cap = self.cap_reason(book, now)
        if cap:
            for sym, ok in batch:
                for t in ok:
                    self._suppress(now, book, sym, t, cap)
            return {"called": False, "cap": cap}
        return await self._call_batch(book, now, snaps, batch, store)

    def _mark_called(self, book: DsEvtBook, now: datetime, batch, cost: float) -> None:
        bar = EF.bar_index(now)
        for sym, ok in batch:
            g = book.g_sym(sym)
            g["last_call_t"] = _iso(now)
            g["last_call_bar"] = bar
            for t in ok:
                g["by_type"][t["type"]] = bar
                if t.get("level_key"):
                    g["by_level"][t["level_key"]] = bar
                self._stat(book.BOOK_NAME, t["type"], "calls")
        gc = book.guards()["calls"]
        gc.append({"t": _iso(now), "cost": round(cost, 6), "symbols": [s for s, _ in batch]})
        cut = (now - timedelta(days=2)).isoformat()
        book.guards()["calls"] = [c for c in gc if c["t"] >= cut]

    def trigger_obj(self, book: DsEvtBook, sym: str, t: dict, now: datetime) -> dict:
        g = book.g_sym(sym)
        lb = g.get("last_call_bar")
        return {"type": t["type"], "level_label": t.get("level_label"), "level_price": t.get("level_price"),
                "trigger_price": t.get("trigger_price"), "trigger_utc": t.get("trigger_utc"),
                "bar_open_utc": t.get("bar_open_utc"), "minutes_into_bar": t.get("minutes_into_bar"),
                "bars_since_last_event_call": None if lb is None else EF.bar_index(now) - int(lb),
                "event_resolution": t.get("event_resolution") if t["type"] == "T2" else None}

    def build_content(self, book: DsEvtBook, now: datetime, items: list[tuple[str, dict, dict, dict]],
                      charts: dict[str, bytes]) -> list[dict]:
        """items: (sym, payload, numeric context with trigger, facts). Per symbol: Chart i/N header, numeric context
        JSON, facts JSON, then image (DS-IMG-E) or CSV (DS-TXT-E). Final: Return the JSON for all symbols now."""
        n = len(items)
        parts = []
        for i, (sym, p, ctx, facts) in enumerate(items, 1):
            parts.append({"type": "text", "text": (f"Chart {i}/{n}: {sym} (M15)\n"
                                                   f"Numeric context JSON: {json.dumps(ctx, default=str)}\n"
                                                   f"Facts JSON: {json.dumps(facts, default=str)}")})
            if book.MODE == "img":
                blob = charts.get(sym)
                if blob:
                    parts.append({"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(blob).decode(), "detail": "original"}})
            else:
                parts.append({"type": "text", "text": EF.csv_table(p, int(self.cc["csv_bars"]))})
        parts.append({"type": "text", "text": "Return the JSON for all symbols now."})
        return parts

    async def _call_batch(self, book: DsEvtBook, now, snaps, batch, store) -> dict:
        syms = [s for s, _ in batch]
        types = sorted({t["type"] for _, ok in batch for t in ok}, key=TRIGGERS.index)
        items = []
        for sym, ok in batch:
            p = copy.deepcopy(snaps[sym])
            ctx = EF.numeric_context(p)
            ctx["trigger"] = self.trigger_obj(book, sym, ok[0], now)
            items.append((sym, p, ctx, EF.facts_at(p, now)))
        base = {"ts_utc": _iso(now), "book": book.BOOK_NAME, "cadence": "event", "trigger_types": types,
                "symbols": syms, "primary": {s: ok[0]["type"] for s, ok in batch}}
        if not self.live:
            # DRY RUN: everything up to the request, no API call, no fill. Counts toward the caps (estimated cost).
            est = float(self.cc["dry_run_cost_estimate_usd"])
            self._mark_called(book, now, batch, est)
            self._jsonl("ds_evt_calls.jsonl", now, {**base, "dry_run": True, "model_returned": None,
                                                    "cost_usd": None, "cost_estimate_usd": est,
                                                    "calls_last_hour": self.calls_last_hour(book, now),
                                                    "triggers": {s: ctx["trigger"] for s, _, ctx, _ in items}})
            return {"called": False, "dry_run": True, "symbols": syms}
        # ---- live
        charts = {}
        if book.MODE == "img":
            render = self._render or EF.render
            charts = await render([p for _, p, _, _ in items], "dsevt")
        system = system_prompt(book.MODE, syms)
        content = self.build_content(book, now, items, charts)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        t_call = time.monotonic()
        call = self._call or self._default_call
        resp = await call(messages)
        latency_ms = round((time.monotonic() - t_call) * 1000)
        from .deepseek import call_cost, usage_tokens, price_band
        usage = resp.get("usage")
        tok = usage_tokens(usage) if usage else {}
        cost = call_cost(usage, now) if usage else 0.0
        self._mark_called(book, now, batch, cost)
        model_ret = resp.get("model")
        parse_ok, perr, obj = False, resp.get("error"), None
        if resp.get("ok"):
            if self.cc["model_assert"] not in str(model_ret or ""):
                perr = f"model_assert: returned model {model_ret!r} does not contain {self.cc['model_assert']}"
            else:
                try:
                    obj = json.loads(resp.get("raw_text") or "")
                    parse_ok = isinstance(obj, dict) and isinstance(obj.get("signals"), list)
                    if not parse_ok:
                        perr = "no signals list"
                except Exception as ex:
                    perr = f"{type(ex).__name__}: {str(ex)[:160]}"
        reasoning = ((usage or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
        self._jsonl("ds_evt_calls.jsonl", now, {
            **base, "dry_run": False, "model_returned": model_ret, "latency_ms": latency_ms,
            "prompt_tokens": tok.get("prompt"), "completion_tokens": tok.get("completion"),
            "reasoning_tokens": reasoning, "cache_hit_tokens": tok.get("cache_hit"),
            "cache_miss_tokens": tok.get("cache_miss"), "cost_usd": cost, "rate_window": price_band(now),
            "raw_response": resp.get("raw_text"), "parse_ok": parse_ok, "parse_error": perr})
        t_resp = self._clock()
        if not parse_ok:
            return {"called": True, "parse_ok": False, "error": perr}
        by_sym = {str(s.get("symbol") or "").upper(): s for s in obj.get("signals") or [] if isinstance(s, dict)}
        queued = []
        for (sym, ok), (_, p, ctx, facts) in zip(batch, items):
            s = by_sym.get(sym) or {"symbol": sym, "action": "HOLD", "setup_type": "none", "score": 0}
            out = {k: s.get(k) for k in FIELDS}
            act = str(out.get("action") or "HOLD").upper()
            try:
                score = int(out.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            trig = ctx["trigger"]
            live_at_call = _f(ctx.get("live_price"))
            atr = _f(ctx.get("atr"))
            gate, skip = "hold", None
            if act in ("BUY", "SELL"):
                side = 1 if act == "BUY" else -1
                self.add_whatif(book, sym, side, live_at_call, atr, now, trig)
                if score < int(self.cc["min_score"]):
                    gate, skip = "rejected", f"score {score} < {self.cc['min_score']}"
                else:
                    skip = self.queue(book, sym, side, atr, live_at_call, trig, s, now)
                    gate = "passed" if skip is None else "rejected"
                    if skip is None:
                        queued.append(sym)
                        self._stat(book.BOOK_NAME, trig["type"], "gate_passed")
            else:
                g = book.g_sym(sym)
                for t in ok:
                    g["hold_until"][t["type"]] = EF.bar_index(now) + int(self.cc["hold_cooldown_bars"])
            self._jsonl("ds_evt_signals.jsonl", now, {
                "ts_utc": _iso(now), "book": book.BOOK_NAME, "symbol": sym, "output": out,
                "numeric_context": ctx, "facts": facts, "trigger": trig, "gate_decision": gate, "skip_reason": skip,
                "trigger_to_response_ms": _ms(_dt(trig.get("trigger_utc")), t_resp),
                "live_price_at_call": live_at_call, "trigger_price": trig.get("trigger_price")})
        res = {"called": True, "parse_ok": True, "queued": queued}
        if queued:
            r = await book.tick(self._clock())
            res["tick"] = r
            for p in book.s["positions"]:
                if p["symbol"] in queued and p.get("signal_at") == _iso(now):
                    self._jsonl("ds_evt_signals.jsonl", now, {
                        "ts_utc": _iso(now), "book": book.BOOK_NAME, "symbol": p["symbol"], "event": "fill",
                        "fill_price": p["entry"], "response_to_fill_ms": _ms(t_resp, _dt(p["entry_time"])),
                        "trigger_to_fill_ms": _ms(_dt(p.get("trigger_utc")), _dt(p["entry_time"]))})
        return res

    def queue(self, book: DsEvtBook, sym, side, atr, live_at_call, trig, sig, now) -> str | None:
        if not atr or live_at_call is None:
            return "no ATR / live price"
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
        book.s["pending"].append({
            "symbol": sym, "side": side, "contract": c, "qty": q, "atr": atr, "confidence": _f(sig.get("confidence")),
            "signal_entry": live_at_call, "trigger_price": trig.get("trigger_price"), "trigger_type": trig["type"],
            "trigger_utc": trig.get("trigger_utc"), "signal_at": _iso(now), "queued_at": _iso(now),
            "level_event": sig.get("level_event"), "score": sig.get("score"), "setup_type": sig.get("setup_type"),
            "rationale": str(sig.get("rationale") or "")[:300], "tag": TAG})
        book._event(now, "queued", symbol=sym, side=side, contract=c, qty=q, trigger=trig["type"])
        return None

    async def _default_call(self, messages: list[dict]) -> dict:
        from .deepseek import call_deepseek
        cc = {"model": self.cc["model"], "base_url": self.cc["base_url"],
              "timeout_seconds": self.cc["timeout_seconds"], "max_output_tokens": self.cc["max_output_tokens"]}
        return await call_deepseek(cc, messages=messages, drop_keys=("reasoning_effort", "thinking"),
                                   body_overrides={"temperature": self.cc["temperature"],
                                                   "max_tokens": int(self.cc["max_output_tokens"]),
                                                   "response_format": {"type": "json_object"}})

    # ---------------- what-if (every BUY/SELL any score + coin-flip twin)
    def add_whatif(self, book, sym, side, entry, atr, now, trig) -> None:
        seed = int(hashlib.sha256(f"{book.BOOK_NAME}|{sym}|{trig.get('trigger_utc')}".encode()).hexdigest()[:8], 16)
        twin = 1 if random.Random(seed).random() < 0.5 else -1
        for kind, sd in (("signal", side), ("coin_flip_twin", twin)):
            self.e.setdefault("whatif", []).append({"book": book.BOOK_NAME, "kind": kind, "symbol": sym, "side": sd,
                                                    "entry": entry, "atr": atr, "t0": _iso(now),
                                                    "trigger_type": trig["type"]})

    def resolve_whatifs(self, now: datetime, store: EF.M1Store) -> None:
        keep = []
        for w in self.e.get("whatif") or []:
            r = EF.whatif_path(w["side"], w["entry"], w["atr"], _dt(w["t0"]), store.frame(w["symbol"]), now=now)
            if r is None and now - _dt(w["t0"]) < timedelta(hours=4):
                keep.append(w)
                continue
            r = r or {"resolved": True, "R": None, "exit_reason": "no_data"}
            self._jsonl("ds_evt_whatif.jsonl", now, {**w, **r})
            if r.get("R") is not None:
                k = "wi_signal" if w["kind"] == "signal" else "wi_twin"
                self._stat(w["book"], w["trigger_type"], f"{k}_n")
                self._stat(w["book"], w["trigger_type"], f"{k}_wins", 1 if r["R"] > 0 else 0)
                st = self.e["stats"][w["book"]][w["trigger_type"]]
                st[f"{k}_R"] = round(st.get(f"{k}_R", 0.0) + float(r["R"]), 3)
        self.e["whatif"] = keep

    # ---------------- persistence / site
    def site_view(self, now: datetime) -> dict:
        cards = []
        for b in self.books:
            a = b.site_view()["account"]
            st = b.s["stats"]
            trades = self._trade_rows(b)
            rs = [float(t["R"]) for t in trades if t.get("R") is not None]
            gw = sum(t["net"] for t in trades if t["net"] > 0)
            gl = -sum(t["net"] for t in trades if t["net"] < 0)
            sig_n = sum((self.e.get("stats", {}).get(b.BOOK_NAME, {}).get(t, {}) or {}).get("wi_signal_n", 0)
                        for t in TRIGGERS)
            d = now.astimezone(UTC).strftime("%Y-%m-%d")
            cards.append({"book": b.BOOK_NAME, "label": b.BOOK_LABEL, "balance": a["balance"], "trades": a["trades"],
                          "R": round(sum(rs), 3), "R_per_signal": round(sum(rs) / sig_n, 3) if sig_n else None,
                          "win_rate": a["win_rate"], "profit_factor": round(gw / gl, 2) if gl else None,
                          "api_cost_today": round(self.spend_today(b, now), 4) if self.live else 0.0,
                          "calls_today": sum(1 for c in b.guards()["calls"] if c["t"][:10] == d),
                          "calls_this_hour": self.calls_last_hour(b, now), "open_positions": a["open_positions"]})
        table = []
        for t in TRIGGERS:
            row = {"trigger": t}
            for b in self.books:
                st = (self.e.get("stats", {}).get(b.BOOK_NAME, {}) or {}).get(t, {})
                n = st.get("wi_signal_n", 0)
                row[b.BOOK_NAME] = {"fired": st.get("fired", 0), "calls": st.get("calls", 0),
                                    "gate_passed": st.get("gate_passed", 0),
                                    "R_per_signal": round(st.get("wi_signal_R", 0) / n, 3) if n else None,
                                    "win_rate": round(st.get("wi_signal_wins", 0) / n, 3) if n else None}
            table.append(row)
        return {"updated_at": _iso(now), "tag": TAG, "paper_only": True, "live": self.live,
                "mode": "live" if self.live else "dry_run (triggers logged, no API calls, no fills)",
                "dry_run_started_at": self.e.get("dry_run_started_at"), "cards": cards, "per_trigger": table}

    def _trade_rows(self, b) -> list[dict]:
        rows = []
        for f in sorted(b.logs_dir.glob(f"*/{b.TRADES_LOG}")):
            for line in f.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        return rows

    async def persist(self, now: datetime) -> None:
        for b in self.books:
            await b.persist()
        view = self.site_view(now)
        async with self.io_lock:
            _atomic_write(self.path, self.e)
            _atomic_write(self.site_dir / "ds_evt.json", view)


def _ms(a: datetime | None, b: datetime | None):
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() * 1000)

