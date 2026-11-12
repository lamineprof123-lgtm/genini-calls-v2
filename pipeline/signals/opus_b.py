"""C-OPUS-B paper book (t249u / t250u). PAPER ONLY — nothing here places an order.

Same model as C-OPUS (claude-opus-5-5) but a fresh $100k Lucid account, the shared
bracket-v1 input package (also used by DS-FLASH), high adaptive effort, XAUUSD /
EURUSD / US100 only, and the uniform 1 ATR / 1 ATR / 90-min exit (no Haiku / no
model exits). Cadence is the shared M15 close +20s snapshot, never the 2-min loop.
"""
from __future__ import annotations
import asyncio, contextlib, copy, json, logging, os, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import bracket_v1 as B
from . import claude_client as CC
from .lucid import (LucidSim, _fetch_and_replay, apply_due_fills, due_pendings,
                    lucid_needs_replay)
from .risk import _atomic_write
from .uniform_exit import close_tag, uniform_exits_enabled, UNIFORM_BANNER, UNIFORM_EXIT_VERSION

log = logging.getLogger("signals.opus_b")
UTC = timezone.utc
DUBAI = ZoneInfo("Asia/Dubai")
BOOK = "C-OPUS-B"
MODEL = "claude-opus-5-5"
PROMPT_VERSION = B.PROMPT_VERSION

DEFAULTS = {
    "enabled": True,
    "model": MODEL,
    "effort": "high",
    "timeout_seconds": 150,
    "max_tokens": 8000,
    "max_attempts": 2,
    "book": BOOK,
    "start_balance": 100000.0,
    "reset_token": "c-opus-b-v1",
    "reset_reason": "C-OPUS-B fresh $100k paper book (bracket-v1, high effort)",
    "fill_max_adverse_atr": B.FILL_MAX_ADVERSE_ATR,
    "check_seconds": 18,
    "recent_trades": 20,
    "symbols": list(B.SYMBOLS),
}


def opus_b_cfg(cfg: dict) -> dict:
    raw = (cfg.get("claude") or {}).get("books") or {}
    book = raw.get("C-OPUS-B") or raw.get("c-opus-b") or {}
    # also allow a top-level [opus_b] table
    out = {**DEFAULTS, **(cfg.get("opus_b") or {}), **book}
    return out


class OpusBLucid(LucidSim):
    def mech_exits(self) -> bool:
        return True

    def _jsonl(self, name: str, t: datetime, obj: dict) -> None:
        if name == "lucid_events.jsonl":
            name = "opus_b_events.jsonl"
        elif name == "lucid_trades.jsonl":
            name = "opus_b_trades.jsonl"
            obj = {**obj, "model": MODEL, "book": BOOK, "prompt_version": PROMPT_VERSION,
                   "exit_mode": close_tag(self.cfg) if hasattr(self, "cfg") else UNIFORM_EXIT_VERSION,
                   "exit_prompt_version": close_tag(self.cfg) if hasattr(self, "cfg") else UNIFORM_EXIT_VERSION}
        super()._jsonl(name, t, obj)

    def post_reset_trades(self) -> list[dict]:
        from .lucid_report import read_records
        from .lucid import _dt
        since = _dt(self.reset_at()) if self.reset_at() else None
        rows, _ = read_records(self.logs_dir, "opus_b_trades.jsonl")
        out = [r for r in rows if since is None or (r.get("t_exit") and _dt(r["t_exit"]) >= since)]
        out.sort(key=lambda r: r.get("t_exit") or "")
        return out

    def write_site(self, site_dir) -> Path:
        return Path(site_dir) / "opus_b.json"

    def stamp_positions(self) -> None:
        for p in list(self.s.get("positions") or []) + list(self.s.get("pending") or []):
            p["model"] = MODEL
            p["book"] = BOOK
            p["prompt_version"] = PROMPT_VERSION
            p["exit_mode"] = UNIFORM_EXIT_VERSION


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def busy_symbols(sim: LucidSim) -> set[str]:
    out = set()
    for row in list(sim.s.get("positions") or []) + list(sim.s.get("pending") or []):
        sym = str(row.get("symbol") or "").upper().replace("/", "").strip()
        if sym:
            out.add(sym)
    return out


def _lucid_cfg(cfg: dict, cc: dict) -> dict:
    out = copy.copy(cfg)
    L = dict(cfg.get("lucid") or {})
    L["eval_fee"] = 0.0
    L["reset_token"] = cc["reset_token"]
    L["reset_reason"] = cc.get("reset_reason") or DEFAULTS["reset_reason"]
    L["reset_archive"] = "account_pre_opus_b_{day}.json"
    L["start_balance"] = float(cc.get("start_balance") or 100000.0)
    # Keep the 0.3 ATR fill check even if main's max_chase is 0.5.
    L["fill_max_adverse_atr"] = float(cc.get("fill_max_adverse_atr") or B.FILL_MAX_ADVERSE_ATR)
    out["lucid"] = L
    return out


class OpusBEngine:
    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *,
                 state_dir=None, logs_dir=None, site_dir=None, call_fn=None):
        self.cfg = cfg
        self.cc = opus_b_cfg(cfg)
        self.ob_cfg = _lucid_cfg(cfg, self.cc)
        root = Path(cfg["general"]["state_dir"])
        self.state_dir = Path(state_dir or root / "opus_b")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or cfg["site"]["dir"])
        self.io_lock = io_lock or asyncio.Lock()
        self.sim_lock = asyncio.Lock()
        self.call_fn = call_fn
        self.sim = OpusBLucid(self.ob_cfg, state_dir=self.state_dir, logs_dir=self.logs_dir)
        self.engine_path = self.state_dir / "engine.json"
        self.e = self._load_engine()
        self.last_signals: list[dict] = list(self.e.get("last_signals") or [])

    def _key(self) -> bool:
        return CC.key_present()

    def _load_engine(self) -> dict:
        if self.engine_path.exists():
            try:
                return json.loads(self.engine_path.read_text())
            except Exception:
                log.exception("opus_b engine.json unreadable; starting fresh")
        st = {"reset_token": self.cc["reset_token"], "created_at": _iso(datetime.now(UTC)),
              "last_call": None, "last_signals": []}
        _atomic_write(self.engine_path, st)
        return st

    def _save_engine(self) -> None:
        self.e["last_signals"] = self.last_signals
        _atomic_write(self.engine_path, self.e)

    async def _api(self, pkg: dict) -> dict:
        if self.call_fn is not None:
            return await self.call_fn(self.cc, pkg)
        return await CC.messages_call(
            model=self.cc.get("model") or MODEL,
            system=pkg["system"],
            content=pkg["anthropic_content"],
            schema=pkg["schema"],
            max_tokens=int(self.cc.get("max_tokens") or 8000),
            thinking={"type": "adaptive"},
            effort=str(self.cc.get("effort") or "high"),
            timeout=float(self.cc.get("timeout_seconds") or 150),
            max_attempts=int(self.cc.get("max_attempts") or 2),
            prices=(claude_cfg_prices(self.cfg)),
        )


    async def on_bracket_package(self, pkg: dict, now: datetime | None = None,
                                 snap_meta: dict | None = None) -> dict:
        """Consume the shared bracket-v1 package. Never raises."""
        now = now or datetime.now(UTC)
        try:
            return await self._on_package(pkg, now, snap_meta or {})
        except Exception as e:
            log.exception("C-OPUS-B cycle failed (other books unaffected)")
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "model": MODEL, "book": BOOK}

    async def _on_package(self, pkg: dict, now: datetime, meta: dict) -> dict:
        if not self.cc.get("enabled", True):
            return {"ok": False, "skipped": "disabled", "model": MODEL, "book": BOOK}
        if not self._key():
            await self._persist_site(now)
            return {"ok": False, "skipped": "waiting for key", "model": MODEL, "book": BOOK}
        busy = busy_symbols(self.sim)
        payloads = B.filter_symbols(pkg.get("payloads") or [], busy)
        if not payloads:
            return {"ok": True, "skipped": "all_symbols_in_position", "busy": sorted(busy),
                    "model": MODEL, "book": BOOK}
        # Rebuild a book-local view of the package if some symbols are busy, but the
        # remaining chart bytes and facts stay identical to what DS-FLASH would see
        # for those same symbols (same snapshot_id / hash on the shared snap).
        call_start = datetime.now(UTC)
        resp = await self._api(pkg)
        answer_ts = datetime.now(UTC)
        usage = resp.get("usage") or {}
        thinking = resp.get("thinking_tokens") or ((usage or {}).get("output_tokens_details") or {}).get("thinking_tokens")
        cost = resp.get("cost_usd")
        self.e["last_call"] = {"t": _iso(answer_ts), "ok": bool(resp.get("ok")),
                               "latency_ms": resp.get("latency_ms") or resp.get("total_ms"),
                               "cost_usd": cost, "thinking_tokens": thinking,
                               "usage": usage, "error": resp.get("error"),
                               "effort": self.cc.get("effort") or "high",
                               "prompt_version": PROMPT_VERSION,
                               "snapshot_id": meta.get("snapshot_id") or pkg.get("snapshot_id"),
                               "snapshot_hash": meta.get("snapshot_hash") or pkg.get("snapshot_hash"),
                               "package_id": meta.get("package_id") or pkg.get("snapshot_id"),
                               "package_hash": pkg.get("snapshot_hash"),
                               "bar_close_ts": meta.get("bar_close_ts"),
                               "snapshot_ts": meta.get("snapshot_ts"),
                               "call_start": meta.get("call_start") or _iso(call_start),
                               "answer_ts": _iso(answer_ts), "model": resp.get("model") or MODEL}
        parsed = resp.get("parsed") or {}
        facts = pkg.get("facts") or {}
        ctxs = {p["symbol"]: p.get("context") or {} for p in payloads}
        by = {}
        for s in parsed.get("signals") or []:
            if isinstance(s, dict) and s.get("symbol"):
                by[str(s["symbol"]).upper()] = s
        rows, score_signals = [], []
        for p in payloads:
            sym = p["symbol"]
            v = B.apply_gates(by.get(sym) or {"action": "HOLD", "symbol": sym},
                              facts.get(sym) or B.compute_facts(p), ctxs[sym])
            v.update({"t": _iso(answer_ts), "sim": BOOK, "model": MODEL, "book": BOOK,
                      "bar": ctxs[sym].get("last_closed_bar_utc"),
                      "prompt_version": PROMPT_VERSION, "effort": self.cc.get("effort") or "high",
                      "thinking_tokens": thinking, "snapshot_id": pkg.get("snapshot_id"),
                      "live_price": ctxs[sym].get("live_price") or ctxs[sym].get("last_close"),
                      "atr": ctxs[sym].get("atr"), "session": ctxs[sym].get("session"),
                      "decision": "HOLD"})
            if not resp.get("ok") and not by.get(sym):
                v.setdefault("issues", []).append(f"call failed: {resp.get('error')}")
            rows.append(v)
            score_signals.append({
                "model": MODEL, "book": BOOK, "prompt_version": PROMPT_VERSION,
                "thinking_level": self.cc.get("effort") or "high",
                "symbol": sym, "action": v.get("action"), "confidence": v.get("confidence"),
                "entry": v.get("entry") or v.get("live_price"), "atr": v.get("atr"),
                "signal_at": _iso(answer_ts), "session": v.get("session"),
                "bar": v.get("bar"), "traded": False, "setup_type": v.get("setup_type"),
                "score": v.get("score"), "snapshot_id": pkg.get("snapshot_id"),
            })
        # One new entry per cycle, highest score first, then 0.3 ATR fill check.
        cands = sorted([v for v in rows if v.get("gated")], key=lambda v: (v.get("score") or 0,
                                                                          v.get("confidence") or 0), reverse=True)
        opened, fill_ts = None, None
        record_signals = []
        for v in rows:
            rec = {**v}
            record_signals.append(rec)
        # Build a record compatible with LucidSim.on_record
        for v in cands:
            if opened is not None:
                v["decision"] = "skip: one new entry per cycle"
                continue
            why = self._pre_fill_check(v, ctxs[v["symbol"]], now)
            if why:
                v["decision"] = f"skip: {why}"
                continue
            v["decision"] = "OPENED"
            opened = v
        self._log_signals(now, rows, resp, cost, thinking, pkg, meta)
        self.last_signals = [{"symbol": v.get("symbol"), "action": v.get("action"),
                              "confidence": v.get("confidence"), "score": v.get("score"),
                              "setup_type": v.get("setup_type"), "gated": v.get("gated"),
                              "decision": v.get("decision"), "rationale": (v.get("rationale") or "")[:240],
                              "gate_reasons": v.get("gate_reasons"), "issues": v.get("issues")}
                             for v in rows]
        # Feed Lucid only the opened (or empty) gated signal, plus HOLDs for logging.
        validated = []
        for v in rows:
            row = {"symbol": v["symbol"], "action": v["action"] if v.get("decision") == "OPENED" else "HOLD",
                   "model_action": v["action"], "confidence": v.get("confidence"),
                   "entry": v.get("entry") or v.get("live_price"),
                   "stop_loss": v.get("stop_loss"), "take_profit": v.get("take_profit"),
                   "rationale": v.get("rationale"), "atr": v.get("atr"),
                   "prompt_version": PROMPT_VERSION, "setup_type": v.get("setup_type"),
                   "score": v.get("score"), "reasons": v.get("issues")}
            # carry context fields Lucid may want
            ctx = ctxs.get(v["symbol"]) or {}
            for k in ("session", "live_price", "live_price_stale", "last_close", "digits"):
                if ctx.get(k) is not None:
                    row[k] = ctx[k]
            validated.append(row)
        record = {"run_at_utc": _iso(now), "signals": validated,
                  "data": {p["symbol"]: {**(p.get("context") or {}), "source": p.get("source"),
                                         "ticker": p.get("ticker")} for p in (pkg.get("payloads") or [])},
                  "model": MODEL, "book": BOOK, "prompt_version": PROMPT_VERSION,
                  "snapshot_id": pkg.get("snapshot_id")}
        async with self.sim_lock:
            from .other_venue import get_hub, is_other_venue_symbol
            ov, lucid = [], []
            for s in (record.get("signals") or []):
                (ov if is_other_venue_symbol(self.cfg, s.get("symbol")) else lucid).append(s)
            if ov:
                hub = get_hub(self.cfg)
                if hub is not None:
                    hub.intake_from_record(BOOK, {**record, "signals": ov}, now)
                    hub.persist(now)
            dec = self.sim.on_record({**record, "signals": lucid}, now)
            self.sim.stamp_positions()
            if due_pendings(self.sim, now):
                try:
                    from .lucid import _default_live_prices
                    live = await _default_live_prices(self.ob_cfg, {pe["symbol"] for pe in due_pendings(self.sim, now)}, now)
                    apply_due_fills(self.sim, {}, now, live)
                    self.sim.stamp_positions()
                    fill_ts = datetime.now(UTC)
                except Exception:
                    log.exception("C-OPUS-B same-step fill failed (pending stays queued)")
            await self._persist(now)
        if opened:
            for s in score_signals:
                if s["symbol"] == opened["symbol"]:
                    s["traded"] = True
        log.info("C-OPUS-B on_record: %s", json.dumps(dec, default=str)[:400])
        latency_s = None
        if resp.get("latency_ms") is not None:
            latency_s = round(float(resp["latency_ms"]) / 1000.0, 3)
        elif resp.get("total_ms") is not None:
            latency_s = round(float(resp["total_ms"]) / 1000.0, 3)
        return {"ok": bool(resp.get("ok")), "model": MODEL, "book": BOOK,
                "latency_s": latency_s, "cost_usd": cost, "thinking_tokens": thinking,
                "answer_ts": _iso(answer_ts), "fill_ts": _iso(fill_ts),
                "error": resp.get("error"), "signals": self.last_signals,
                "score_signals": score_signals, "decisions": dec,
                "prompt_version": PROMPT_VERSION, "snapshot_id": pkg.get("snapshot_id")}

    def _pre_fill_check(self, v: dict, ctx: dict, now: datetime) -> str | None:
        if ctx.get("live_price_stale"):
            return "live price stale"
        chart = ctx.get("live_price") if ctx.get("live_price") is not None else ctx.get("last_close")
        px = v.get("live_price") or chart
        atr = ctx.get("atr")
        if chart is None or px is None or not atr:
            return "missing price/ATR for 0.3 ATR fill check"
        side = 1 if v["action"] == "BUY" else -1
        ok, ratio, why = B.fill_adverse(side, float(px), float(chart), float(atr),
                                        float(self.cc.get("fill_max_adverse_atr") or B.FILL_MAX_ADVERSE_ATR))
        v["fill_adverse_atr"] = round(ratio, 4)
        return None if ok else why

    def _log_signals(self, now, rows, resp, cost, thinking, pkg, meta) -> None:
        day = Path(self.logs_dir) / now.astimezone(DUBAI).strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        rec = {"t": _iso(now), "model": MODEL, "book": BOOK, "prompt_version": PROMPT_VERSION,
               "effort": self.cc.get("effort") or "high", "ok": bool(resp.get("ok")),
               "latency_ms": resp.get("latency_ms") or resp.get("total_ms"),
               "cost_usd": cost, "usage": resp.get("usage"), "thinking_tokens": thinking,
               "error": resp.get("error"),
               "snapshot_id": meta.get("snapshot_id") or pkg.get("snapshot_id"),
               "snapshot_hash": meta.get("snapshot_hash") or pkg.get("snapshot_hash"),
               "package_id": pkg.get("snapshot_id"), "package_hash": pkg.get("snapshot_hash"),
               "bar_close_ts": meta.get("bar_close_ts"), "snapshot_ts": meta.get("snapshot_ts"),
               "call_start": meta.get("call_start"), "answer_ts": (self.e.get("last_call") or {}).get("answer_ts"),
               "signals": [{k: v.get(k) for k in (
                   "symbol", "action", "setup_type", "score", "confidence", "gated", "decision",
                   "entry", "stop_loss", "take_profit", "rationale", "level_event",
                   "issues", "gate_reasons", "vwap_distance_atr", "room_up_atr", "room_down_atr",
                   "atr", "session", "prompt_version")} for v in rows]}
        with open(day / "opus_b_signals.jsonl", "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        # Also append one row per signal to claude_signals.jsonl so the universal
        # scorer's existing Claude ingest picks C-OPUS-B up.
        with open(day / "claude_signals.jsonl", "a") as f:
            for v in rows:
                f.write(json.dumps({"t": _iso(now), "sim": BOOK, "model": MODEL, "book": BOOK,
                                    "prompt_version": PROMPT_VERSION, **{k: v.get(k) for k in (
                                        "symbol", "action", "setup_type", "score", "confidence",
                                        "entry", "atr", "session", "bar", "gated", "decision")}},
                                   default=str) + "\n")

    async def exit_tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        try:
            async with self.sim_lock:
                if not (self.sim.s.get("positions") or self.sim.s.get("pending")
                        or lucid_needs_replay(self.sim)):
                    return {"skipped": "not_needed"}
                _, until, new_ids = await _fetch_and_replay(self.sim, self.ob_cfg, now)
                self.sim.stamp_positions()
                await self._persist(now)
                if new_ids:
                    log.info("C-OPUS-B replay filled ids %s (until %s)", new_ids, until)
                return {"processed_until": self.sim.s.get("last_processed"), "filled": new_ids}
        except Exception:
            log.exception("C-OPUS-B exit tick failed (other books unaffected)")
            return {"error": "exception"}

    async def _persist(self, now: datetime) -> None:
        self.sim.save()
        self._save_engine()
        await self._persist_site(now)

    async def _persist_site(self, now: datetime) -> None:
        try:
            view = self.site_view(now)
            async with self.io_lock:
                _atomic_write(self.site_dir / "opus_b.json", view)
        except Exception:
            log.exception("opus_b.json write failed")

    def site_view(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        payload = self.sim.site_payload(recent=int(self.cc.get("recent_trades") or 20))
        a = payload.get("account") or {}
        a.setdefault("net_pnl", a.get("realized_pnl_since_reset"))
        a.setdefault("trades", a.get("trades_since_reset"))
        a.setdefault("open_positions", len(payload.get("open_positions") or []))
        a.setdefault("stopped", sum(1 for t in (payload.get("recent_trades") or []) if t.get("stopped")))
        rules = ("C-OPUS-B: claude-opus-5-5 entries, adaptive thinking, effort high, "
                 "shared bracket-v1 package with DS-FLASH (same charts, facts, 3 symbols, "
                 "M15 close +20s). Question: will price travel 1 ATR my way before 1 ATR "
                 "against? Code-computed facts + hold gates (no trend >2 ATR from VWAP; "
                 "≥1 ATR room; US100 volume-free S2). 0.3 ATR fill check. Uniform 1 ATR / "
                 "1 ATR / 90-min exits, no model exits. Fresh $100k Lucid account.")
        payload.update({
            "book": BOOK, "label": "C-OPUS-B (claude-opus-5-5, bracket-v1, effort high)",
            "model": self.cc.get("model") or MODEL, "effort": self.cc.get("effort") or "high",
            "prompt_version": PROMPT_VERSION, "prompt_reconstruction": B.PROMPT_RECONSTRUCTION,
            "key_present": self._key(), "rules": rules, "stop_txt": "1 ATR stop · 1 ATR target · 90 min",
            "card": UNIFORM_BANNER if uniform_exits_enabled(self.ob_cfg) else "1 ATR / 1 ATR / 90 min · no model exits",
            "exit_calls_note": UNIFORM_BANNER, "exit_mode": UNIFORM_EXIT_VERSION,
            "uniform_1atr_exits": True, "last_call": self.e.get("last_call"),
            "last_signals": self.last_signals,
        })
        return payload


def claude_cfg_prices(cfg: dict):
    cc = cfg.get("claude") or {}
    pr = cc.get("prices") or {}
    return {k: tuple(v) for k, v in pr.items()} if pr else None


async def run_opus_b_exit_loop(engine: OpusBEngine) -> None:
    period = float(engine.cc.get("check_seconds") or 18)
    while True:
        try:
            await engine.exit_tick()
        except Exception:
            log.exception("C-OPUS-B exit loop tick failed")
        await asyncio.sleep(period)


async def stop_task(task: asyncio.Task | None, timeout: float = 30) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, timeout)
