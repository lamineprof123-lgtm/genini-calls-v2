"""DeepSeek entry-only paper book DS-FLASH (PAPER ONLY — nothing here places an order anywhere).

  Entries  deepseek-flash (DeepSeek-V4.1-Flash, text+image, effort low) on the same 2-min batched
           cycle, charts, system prompt and numeric context as Gemini flash. Timeout 60 s, ONE
           attempt, no retry / no fallback. Runs concurrently with the Gemini call and is never
           awaited by it. Symbols the DS book already holds (open or pending) are skipped.
           BTCUSD is site-only and is never traded. Mid-bar session throttle matches Gemini.
  Exits    no model exit calls. Mechanical rules match main while Gemini exits are paused:
           −$500 net hard stop, 1 ATR (M15 ATR at entry) target, 90-min cap. Same fill logic
           and full-size NQ/GC/6E/6B sizing as the main Lucid account (own fresh $100k book).
  Spend    official DeepSeek pricing (per 1M tokens, 2026-11): off-peak cache-hit $0.003 /
           cache-miss $0.15 / output $0.60; peak (Mon–Fri 01:00–04:00 and 06:00–10:00 UTC) is
           double. Chinese public holidays are treated as peak for the cap (safer). Soft-stop
           new entry calls when estimated Dubai-day cost exceeds $5.
  Logs     state/deepseek/{account,engine}.json, logs/<day>/deepseek_{signals,trades,events}.jsonl,
           site deepseek.json. Every signal is logged with confidence for later what-if scoring.
  Isolation
           own asyncio tasks and lock; errors are logged and never touch Gemini / Carbon / Claude
           / exit-manager books. Key: env DEEPSEEK_API_KEY only (repo secret).
"""
from __future__ import annotations
import asyncio, base64, contextlib, copy, json, logging, os, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .uniform_exit import close_tag, uniform_exits_enabled, UNIFORM_BANNER, UNIFORM_EXIT_VERSION
from .exit_versions import GEMINI_MECH_EXIT_VERSION
from .gemini_client import SignalBatch, _image_mime, chart_user_text, system_prompt
from . import bracket_v1 as B
from .lucid import (LucidSim, _fetch_and_replay, apply_due_fills, due_pendings,
                    lucid_needs_replay)
from .risk import _atomic_write
from .scalp_levels import apply_server_levels
from .validator import parse_raw, validate_batch

log = logging.getLogger("signals.deepseek")
UTC = timezone.utc
DUBAI = ZoneInfo("Asia/Dubai")
BOOK = "DS-FLASH"
MODEL = "deepseek-flash"

DEFAULTS = {
    "enabled": True,
    "model": MODEL,
    "base_url": "https://api.deepseek.com",
    "effort": "low",
    "timeout_seconds": 60,
    "max_output_tokens": 4000,
    "book": BOOK,
    "start_balance": 100000.0,
    "reset_token": "2026-11-12-ds-flash",
    "reset_reason": "DS-FLASH fresh $100k paper book",
    "soft_spend_usd": 5.0,
    "check_seconds": 18,
    "recent_trades": 20,
    "symbols": list(B.SYMBOLS),
    "prompt_version": B.PROMPT_VERSION,
    "fill_max_adverse_atr": B.FILL_MAX_ADVERSE_ATR,
    "scan": "m15_close_plus_20s",
}

# Official https://api-docs.deepseek.com/quick_start/pricing/ (USD per 1M tokens).
# Peak: Mon–Fri 01:00–04:00 and 06:00–10:00 UTC. Other hours, and weekends, are off-peak.
# Officially Chinese public holidays are also off-peak; we treat those weekday windows as
# peak anyway so the $5 soft cap cannot undershoot.
PRICES = {
    "peak": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
    "off_peak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60},
}

_ENGINE: "DeepSeekEngine | None" = None


def deepseek_cfg(cfg: dict) -> dict:
    out = {**DEFAULTS, **(cfg.get("deepseek") or {})}
    return out


def set_engine(engine: "DeepSeekEngine | None") -> None:
    global _ENGINE
    _ENGINE = engine


def kick(cfg: dict, payloads: list[dict] | None, pngs: dict[str, bytes] | None = None,
         skipped: str | None = None, now: datetime | None = None) -> asyncio.Task | None:
    """t251u: DS-FLASH no longer rides the 2-min Gemini flash loop. Kept as a no-op
    so a stale pipeline.py hook cannot fire a mid-bar entry call. Never raises."""
    log.info("deepseek kick ignored (DS-FLASH is on the shared M15 close +20s scan)")
    return None


def spend_day(t: datetime) -> str:
    return t.astimezone(DUBAI).strftime("%Y-%m-%d")


def is_peak(t: datetime) -> bool:
    u = t.astimezone(UTC)
    if u.weekday() >= 5:
        return False
    m = u.hour * 60 + u.minute
    return (60 <= m < 240) or (360 <= m < 600)


def price_band(t: datetime) -> str:
    return "peak" if is_peak(t) else "off_peak"


def usage_tokens(usage: dict | None) -> dict:
    u = usage or {}
    details = u.get("prompt_tokens_details") or {}
    hit = int(u.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0)
    miss = int(u.get("prompt_cache_miss_tokens") or 0)
    prompt = int(u.get("prompt_tokens") or 0)
    if not miss and not hit:
        miss = prompt
    elif hit and not miss:
        miss = max(0, prompt - hit)
    completion = int(u.get("completion_tokens") or 0)
    reasoning = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    return {"prompt": prompt, "cache_hit": hit, "cache_miss": miss,
            "completion": completion, "reasoning": reasoning,
            "total": int(u.get("total_tokens") or (prompt + completion))}


def call_cost(usage: dict | None, t: datetime) -> float:
    """USD for one call. completion_tokens already includes reasoning_tokens (do not double-count)."""
    p = PRICES[price_band(t)]
    tok = usage_tokens(usage)
    usd = (tok["cache_hit"] * p["cache_hit"]
           + tok["cache_miss"] * p["cache_miss"]
           + tok["completion"] * p["output"]) / 1e6
    return round(usd, 8)


def _ds_lucid_cfg(cfg: dict) -> dict:
    """Shallow copy of cfg with a DS-only Lucid reset token and no eval fee (fresh $100k)."""
    cc = deepseek_cfg(cfg)
    out = copy.copy(cfg)
    L = dict(cfg.get("lucid") or {})
    L["eval_fee"] = 0.0
    L["reset_token"] = cc["reset_token"]
    L["reset_reason"] = cc.get("reset_reason") or "DS-FLASH fresh $100k paper book"
    L["reset_archive"] = "account_pre_ds_flash_{day}.json"
    L["start_balance"] = float(cc.get("start_balance") or L.get("start_balance") or 100000.0)
    out["lucid"] = L
    return out


class DeepSeekLucid(LucidSim):
    """Main Lucid fill/sizing/mech-exit path, isolated state + logs, always mechanical exits."""

    def mech_exits(self) -> bool:
        return True

    def _fee(self, kind: str, amount: float, t: datetime, note: str) -> None:
        if not amount:
            return
        super()._fee(kind, amount, t, note)

    def _jsonl(self, name: str, t: datetime, obj: dict) -> None:
        if name == "lucid_events.jsonl":
            name = "deepseek_events.jsonl"
        elif name == "lucid_trades.jsonl":
            name = "deepseek_trades.jsonl"
            tag = close_tag(self.cfg) if hasattr(self, "cfg") else UNIFORM_EXIT_VERSION
            # DS-FLASH never makes model exit calls.
            if not (hasattr(self, "cfg") and uniform_exits_enabled(self.cfg)):
                tag_mode, tag_ver = "mech", GEMINI_MECH_EXIT_VERSION
            else:
                tag_mode, tag_ver = tag, tag
            obj = {**obj, "model": MODEL, "exit_mode": tag_mode,
                   "book": BOOK, "exit_prompt_version": tag_ver}
        super()._jsonl(name, t, obj)

    def post_reset_trades(self) -> list[dict]:
        from .lucid_report import read_records
        from .lucid import _dt
        since = _dt(self.reset_at()) if self.reset_at() else None
        rows, _ = read_records(self.logs_dir, "deepseek_trades.jsonl")
        out = [r for r in rows if since is None or (r.get("t_exit") and _dt(r["t_exit"]) >= since)]
        out.sort(key=lambda r: r.get("t_exit") or "")
        return out

    def write_site(self, site_dir) -> Path:
        # Engine writes deepseek.json; never touch lucid.json.
        return Path(site_dir) / "deepseek.json"

    def stamp_positions(self) -> None:
        for p in list(self.s.get("positions") or []) + list(self.s.get("pending") or []):
            p["model"] = MODEL
            p["exit_mode"] = UNIFORM_EXIT_VERSION
            p["book"] = BOOK


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def busy_symbols(sim: LucidSim) -> set[str]:
    out = set()
    for row in list(sim.s.get("positions") or []) + list(sim.s.get("pending") or []):
        sym = str(row.get("symbol") or "").upper().replace("/", "").strip()
        if sym and sym != "BTCUSD":
            out.add(sym)
    return out


def filter_ds_payloads(payloads: list[dict], busy: set[str], cfg: dict) -> list[dict]:
    from . import bracket_v1 as B
    allow = set((cfg.get("deepseek") or {}).get("symbols") or B.SYMBOLS)
    return [p for p in payloads if p.get("symbol") in allow and p.get("symbol") not in busy]


def _b64_image(blob: bytes) -> tuple[str, str]:
    mime = _image_mime(blob)
    return mime, base64.b64encode(blob).decode()


def signal_batch_schema_prompt() -> str:
    """JSON-mode closer: embed the SignalBatch schema so DeepSeek can match Gemini's response_schema."""
    schema = json.dumps(SignalBatch.model_json_schema(), indent=2)
    return (
        "Return a single JSON object now, matching this SignalBatch schema exactly "
        "(field names and types unchanged). No markdown, no commentary outside the JSON.\n"
        + schema
    )


def build_messages(cfg: dict, payloads: list[dict], pngs: dict[str, bytes],
                   htf: dict | None = None, bar_close_ts: str | None = None,
                   snapshot_ts: str | None = None) -> list[dict]:
    """OpenAI-compatible multimodal messages for the shared bracket-v1 package
    (same system prompt, facts block, and JPEG bytes as C-OPUS-B)."""
    from . import bracket_v1 as B
    pkg = B.build_package(payloads, pngs, htf=htf, bar_close_ts=bar_close_ts,
                          snapshot_ts=snapshot_ts)
    return pkg["openai_messages"]


async def call_deepseek(cc: dict, *, messages: list[dict]) -> dict:
    """ONE request, hard timeout, no retry. Never raises. Never logs the key."""
    t0 = time.monotonic()
    key = os.environ.get("DEEPSEEK_API_KEY") or ""
    if not key:
        return {"ok": False, "raw_text": "", "usage": None, "latency_s": 0.0,
                "error": "DEEPSEEK_API_KEY missing", "model": cc.get("model") or MODEL}
    url = str(cc.get("base_url") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    body = {
        "model": cc.get("model") or MODEL,
        "messages": messages,
        "reasoning_effort": cc.get("effort") or "low",
        "max_tokens": int(cc.get("max_output_tokens") or 4000),
        "response_format": {"type": "json_object"},
        "thinking": {"type": "enabled"},
    }
    timeout = float(cc.get("timeout_seconds") or 60)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=body, headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            })
        latency = round(time.monotonic() - t0, 3)
        if r.status_code != 200:
            err = f"HTTP {r.status_code}: {(r.text or '')[:240]}"
            log.warning("deepseek call failed: %s latency=%.3fs", err, latency)
            return {"ok": False, "raw_text": "", "usage": None, "latency_s": latency,
                    "error": err, "model": cc.get("model") or MODEL, "http_status": r.status_code}
        data = r.json()
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        raw = msg.get("content") or ""
        return {"ok": True, "raw_text": raw, "usage": data.get("usage"), "latency_s": latency,
                "model": data.get("model") or cc.get("model") or MODEL,
                "finish_reason": ((data.get("choices") or [{}])[0].get("finish_reason")),
                "reasoning_len": len(msg.get("reasoning_content") or ""),
                "http_status": r.status_code}
    except Exception as e:
        latency = round(time.monotonic() - t0, 3)
        err = f"{type(e).__name__}: {str(e)[:240]}"
        log.warning("deepseek call failed: %s latency=%.3fs", err, latency)
        return {"ok": False, "raw_text": "", "usage": None, "latency_s": latency,
                "error": err, "model": cc.get("model") or MODEL}


class DeepSeekEngine:
    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *,
                 state_dir=None, logs_dir=None, site_dir=None, call_fn=None, render_fn=None):
        self.cfg = cfg
        self.cc = deepseek_cfg(cfg)
        self.ds_cfg = _ds_lucid_cfg(cfg)
        root = Path(cfg["general"]["state_dir"])
        self.state_dir = Path(state_dir or root / "deepseek")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or cfg["site"]["dir"])
        self.io_lock = io_lock or asyncio.Lock()
        self.sim_lock = asyncio.Lock()
        self.call_fn = call_fn or call_deepseek
        self.render_fn = render_fn
        self.sim = DeepSeekLucid(self.ds_cfg, state_dir=self.state_dir, logs_dir=self.logs_dir)
        self.engine_path = self.state_dir / "engine.json"
        self.e = self._load_engine()
        self.entry_task: asyncio.Task | None = None
        self.last_signals: list[dict] = list(self.e.get("last_signals") or [])

    def _key(self) -> str:
        return os.environ.get("DEEPSEEK_API_KEY") or ""

    def _load_engine(self) -> dict:
        if self.engine_path.exists():
            try:
                return json.loads(self.engine_path.read_text())
            except Exception:
                log.exception("deepseek engine.json unreadable; starting fresh")
        st = {"reset_token": self.cc["reset_token"], "created_at": _iso(datetime.now(UTC)),
              "spend": {}, "last_call": None, "last_signals": [], "last_bars": {}}
        _atomic_write(self.engine_path, st)
        return st

    def _save_engine(self) -> None:
        self.e["last_signals"] = self.last_signals
        _atomic_write(self.engine_path, self.e)

    def spend_today(self, now: datetime) -> dict:
        d = spend_day(now)
        sp = self.e.setdefault("spend", {}).setdefault(
            d, {"usd": 0.0, "calls": 0, "failed": 0, "tokens": {
                "prompt": 0, "cache_hit": 0, "cache_miss": 0, "completion": 0, "reasoning": 0}})
        for k in sorted(self.e.get("spend") or {})[:-14]:
            self.e["spend"].pop(k, None)
        return sp

    def spend_blocked(self, now: datetime) -> str | None:
        usd = float(self.spend_today(now)["usd"])
        cap = float(self.cc["soft_spend_usd"])
        if usd > cap:
            return f"soft spend cap: ${usd:.2f} > ${cap:.2f} today (Dubai)"
        return None

    def record_usage(self, now: datetime, usage: dict | None, ok: bool, extra: dict) -> float:
        sp = self.spend_today(now)
        cost = call_cost(usage, now) if usage else 0.0
        sp["usd"] = round(float(sp["usd"]) + cost, 6)
        sp["calls"] = int(sp["calls"]) + 1
        if not ok:
            sp["failed"] = int(sp["failed"]) + 1
        tok = usage_tokens(usage)
        for k, v in tok.items():
            if k in sp["tokens"]:
                sp["tokens"][k] = int(sp["tokens"][k]) + int(v)
        self.e["last_call"] = {"t": _iso(now), "ok": ok, "cost_usd": cost,
                               "spend_today_usd": round(sp["usd"], 4),
                               "band": price_band(now), "tokens": tok, **extra}
        return cost

    def schedule_cycle(self, payloads: list[dict], pngs: dict[str, bytes] | None = None,
                       skipped: str | None = None, now: datetime | None = None) -> asyncio.Task | None:
        if self.entry_task is not None and not self.entry_task.done():
            log.info("deepseek: previous entry call still running; skipping this cycle")
            return None
        self.entry_task = asyncio.create_task(
            self.on_cycle(payloads, pngs=pngs, skipped=skipped, now=now), name="ds-flash-entry")
        return self.entry_task

    async def on_cycle(self, payloads: list[dict], pngs: dict[str, bytes] | None = None,
                       skipped: str | None = None, now: datetime | None = None) -> dict:
        """Entry cycle. Never raises. Never awaited by the Gemini path."""
        now = now or datetime.now(UTC)
        out = {"skipped": True}
        try:
            out = await self._on_cycle(payloads, pngs=pngs, skipped=skipped, now=now)
        except Exception:
            log.exception("deepseek cycle failed (Gemini loop unaffected)")
            out = {"skipped": True, "error": "exception"}
        return out

    async def _on_cycle(self, payloads: list[dict], pngs: dict[str, bytes] | None,
                        skipped: str | None, now: datetime) -> dict:
        from .pipeline import THROTTLE_SKIP, should_throttle_gemini

        if not self.cc.get("enabled", True):
            return {"skipped": True, "why": "disabled"}
        if skipped == THROTTLE_SKIP:
            log.info("deepseek: matching Gemini mid-bar session throttle, skip")
            return {"skipped": True, "why": THROTTLE_SKIP}
        # Same mid-bar throttle as Gemini even if the hook didn't pass the reason
        # (e.g. Gemini skipped for a different reason but the session is blocked).
        seen = self.e.get("last_bars") or {}
        if should_throttle_gemini(payloads, seen, self.cfg):
            log.info("deepseek: mid-bar session throttle, skip")
            return {"skipped": True, "why": THROTTLE_SKIP}
        if not self._key():
            await self._persist_site(now)
            return {"skipped": True, "why": "waiting for key"}
        blocked = self.spend_blocked(now)
        if blocked:
            log.info("deepseek: %s", blocked)
            self.e["last_call"] = {**(self.e.get("last_call") or {}), "t": _iso(now),
                                   "ok": False, "error": blocked, "skipped": True}
            await self._persist_site(now)
            return {"skipped": True, "why": blocked}

        busy = busy_symbols(self.sim)
        entry = filter_ds_payloads(payloads or [], busy, self.cfg)
        if busy:
            log.info("deepseek excluding in-position symbols: %s (entry=%s)",
                     sorted(busy), [p["symbol"] for p in entry])
        if not entry:
            return {"skipped": True, "why": "all_symbols_in_position", "busy": sorted(busy)}

        pngs = dict(pngs or {})
        missing = [p for p in entry if p["symbol"] not in pngs]
        if missing:
            try:
                render = self.render_fn
                if render is None:
                    from .renderer import render_all
                    render = render_all
                extra = await render(missing)
                pngs.update(extra or {})
            except Exception:
                log.exception("deepseek render of missing charts failed")
        ready = [p for p in entry if p["symbol"] in pngs]
        if not ready:
            return {"skipped": True, "why": "no_charts"}

        messages = build_messages(self.cfg, ready, pngs)
        resp = await self.call_fn(self.cc, messages=messages)
        cost = self.record_usage(now, resp.get("usage"), bool(resp.get("ok")),
                                 {"latency_s": resp.get("latency_s"), "error": resp.get("error"),
                                  "model": resp.get("model"), "symbols": [p["symbol"] for p in ready],
                                  "http_status": resp.get("http_status")})
        log.info("deepseek call ok=%s latency=%.3fs cost=$%.5f spend_today=$%.4f symbols=%s",
                 resp.get("ok"), resp.get("latency_s") or 0, cost,
                 self.spend_today(now)["usd"], [p["symbol"] for p in ready])

        obj, parse_err = parse_raw(resp.get("raw_text") or "") if resp.get("raw_text") else (
            None, resp.get("error") or "empty response")
        contexts = {p["symbol"]: p["context"] for p in ready}
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
        equity = self.sim.equity()
        validated = validate_batch(obj, contexts, self.cfg, equity, False, None)
        if obj and (self.cfg.get("scalp") or {}).get("enabled", False):
            by = {str(s.get("symbol", "")).upper().replace("/", "").strip(): s
                  for s in (obj.get("signals") or []) if isinstance(s, dict)}
            for v in validated:
                raw = by.get(v["symbol"]) or {}
                if raw.get("scalp"):
                    v["scalp"] = raw["scalp"]
                    v["levels_source"] = "server_scalp"
        for v in validated:
            ctx = contexts.get(v["symbol"]) or {}
            for k in ("session", "cvd", "cvd_label", "cvd_slope",
                      "last_closed_bar_utc", "last_price_ts_utc", "poc", "vah", "val",
                      "live_price", "live_price_ts_utc", "live_price_stale", "live_price_source",
                      "signal_candle", "levels", "atr", "digits"):
                if v.get(k) is None and ctx.get(k) is not None:
                    v[k] = ctx[k]
            v["model"] = MODEL
            v["book"] = BOOK

        self._log_signals(now, validated, resp, cost, parse_err)
        self.last_signals = [{"symbol": v.get("symbol"), "action": v.get("model_action") or v.get("action"),
                              "confidence": v.get("confidence"), "entry": v.get("entry"),
                              "rationale": (v.get("rationale") or "")[:240]}
                             for v in validated]
        last_bars = dict(self.e.get("last_bars") or {})
        for p in ready:
            bar = (p.get("context") or {}).get("last_closed_bar_utc")
            if bar:
                last_bars[p["symbol"]] = bar
        self.e["last_bars"] = last_bars

        record = {"run_at_utc": _iso(now), "signals": validated,
                  "data": {p["symbol"]: {**(p.get("context") or {}),
                                         "source": p.get("source"), "ticker": p.get("ticker")}
                           for p in (payloads or [])},
                  "model": MODEL, "book": BOOK}
        async with self.sim_lock:
            dec = self.sim.on_record(record, now)
            self.sim.stamp_positions()
            # Same-step fill when the live quote is already due.
            if due_pendings(self.sim, now):
                try:
                    from .lucid import _default_live_prices
                    live = await _default_live_prices(self.ds_cfg, {pe["symbol"] for pe in due_pendings(self.sim, now)}, now)
                    apply_due_fills(self.sim, {}, now, live)
                    self.sim.stamp_positions()
                except Exception:
                    log.exception("deepseek same-step fill failed (pending stays queued)")
            await self._persist(now)
        log.info("deepseek on_record: %s", json.dumps(dec, default=str)[:400])
        return {"skipped": False, "decisions": dec, "signals": self.last_signals,
                "cost_usd": cost, "latency_s": resp.get("latency_s")}

    def _log_signals(self, now: datetime, validated: list[dict], resp: dict, cost: float,
                     parse_err: str | None) -> None:
        day = Path(self.logs_dir) / now.astimezone(DUBAI).strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        rec = {"t": _iso(now), "model": MODEL, "book": BOOK, "ok": bool(resp.get("ok")),
               "latency_s": resp.get("latency_s"), "cost_usd": cost, "usage": resp.get("usage"),
               "parse_error": parse_err, "error": resp.get("error"),
               "spend_today_usd": self.spend_today(now)["usd"],
               "prompt_version": B.PROMPT_VERSION,
               "signals": [{"symbol": v.get("symbol"),
                            "action": v.get("model_action") or v.get("action"),
                            "confidence": v.get("confidence"),
                            "entry": v.get("entry"),
                            "stop_loss": v.get("stop_loss"),
                            "take_profit": v.get("take_profit"),
                            "rationale": v.get("rationale"),
                            "reasons": v.get("reasons"),
                            "atr": v.get("atr"),
                            "prompt_version": v.get("prompt_version") or B.PROMPT_VERSION,
                            "setup_type": v.get("setup_type"), "score": v.get("score"),
                            "gated": v.get("gated"), "decision": v.get("decision")} for v in validated]}
        with open(day / "deepseek_signals.jsonl", "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")


    async def on_bracket_package(self, pkg: dict, now: datetime | None = None,
                                 snap_meta: dict | None = None) -> dict:
        """Consume the shared bracket-v1 package (identical to C-OPUS-B). Never raises."""
        now = now or datetime.now(UTC)
        meta = snap_meta or {}
        try:
            return await self._on_bracket_package(pkg, now, meta)
        except Exception as e:
            log.exception("DS-FLASH bracket cycle failed (other books unaffected)")
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "model": MODEL, "book": BOOK}

    async def _on_bracket_package(self, pkg: dict, now: datetime, meta: dict) -> dict:
        from .validator import parse_raw
        if not self.cc.get("enabled", True):
            return {"ok": False, "skipped": "disabled", "model": MODEL, "book": BOOK}
        if not self._key():
            await self._persist_site(now)
            return {"ok": False, "skipped": "waiting for key", "model": MODEL, "book": BOOK}
        blocked = self.spend_blocked(now)
        if blocked:
            return {"ok": False, "skipped": blocked, "model": MODEL, "book": BOOK}
        busy = busy_symbols(self.sim)
        payloads = B.filter_symbols(pkg.get("payloads") or [], busy)
        if not payloads:
            return {"ok": True, "skipped": "all_symbols_in_position", "busy": sorted(busy),
                    "model": MODEL, "book": BOOK}
        call_start = datetime.now(UTC)
        resp = await self.call_fn(self.cc, messages=pkg["openai_messages"])
        answer_ts = datetime.now(UTC)
        cost = self.record_usage(now, resp.get("usage"), bool(resp.get("ok")),
                                 {"latency_s": resp.get("latency_s"), "error": resp.get("error"),
                                  "model": resp.get("model"), "symbols": [p["symbol"] for p in payloads],
                                  "http_status": resp.get("http_status"),
                                  "prompt_version": B.PROMPT_VERSION,
                                  "snapshot_id": meta.get("snapshot_id") or pkg.get("snapshot_id"),
                                  "bar_close_ts": meta.get("bar_close_ts"),
                                  "snapshot_ts": meta.get("snapshot_ts"),
                                  "call_start": meta.get("call_start") or _iso(call_start),
                                  "answer_ts": _iso(answer_ts)})
        obj, parse_err = parse_raw(resp.get("raw_text") or "") if resp.get("raw_text") else (
            None, resp.get("error") or "empty response")
        facts = pkg.get("facts") or {}
        ctxs = {p["symbol"]: p.get("context") or {} for p in payloads}
        by = {}
        for s in (obj or {}).get("signals") or []:
            if isinstance(s, dict) and s.get("symbol"):
                by[str(s["symbol"]).upper()] = s
        rows, score_signals, validated = [], [], []
        for p in payloads:
            sym = p["symbol"]
            v = B.apply_gates(by.get(sym) or {"action": "HOLD", "symbol": sym},
                              facts.get(sym) or B.compute_facts(p), ctxs[sym])
            v.update({"t": _iso(answer_ts), "model": MODEL, "book": BOOK,
                      "bar": ctxs[sym].get("last_closed_bar_utc"),
                      "prompt_version": B.PROMPT_VERSION, "effort": self.cc.get("effort") or "low",
                      "snapshot_id": pkg.get("snapshot_id"),
                      "live_price": ctxs[sym].get("live_price") or ctxs[sym].get("last_close"),
                      "atr": ctxs[sym].get("atr"), "session": ctxs[sym].get("session"),
                      "decision": "HOLD"})
            rows.append(v)
            # 0.3 ATR fill check vs the chart live price (same as C-OPUS-B).
            why = None
            if v.get("gated"):
                chart = ctxs[sym].get("live_price")
                if chart is None:
                    chart = ctxs[sym].get("last_close")
                px = v.get("live_price") or chart
                atr = ctxs[sym].get("atr")
                if chart is None or px is None or not atr:
                    why = "missing price/ATR for 0.3 ATR fill check"
                else:
                    side = 1 if v["action"] == "BUY" else -1
                    ok, ratio, why = B.fill_adverse(
                        side, float(px), float(chart), float(atr),
                        float(self.cc.get("fill_max_adverse_atr") or B.FILL_MAX_ADVERSE_ATR))
                    v["fill_adverse_atr"] = round(ratio, 4)
            if v.get("gated") and not why:
                v["decision"] = "OPENED"
            elif v.get("gated") and why:
                v["gated"] = False
                v["decision"] = f"skip: {why}"
            score_signals.append({
                "model": MODEL, "book": BOOK, "prompt_version": B.PROMPT_VERSION,
                "thinking_level": self.cc.get("effort") or "low",
                "symbol": sym, "action": v.get("action"), "confidence": v.get("confidence"),
                "entry": v.get("entry") or v.get("live_price"), "atr": v.get("atr"),
                "signal_at": _iso(answer_ts), "session": v.get("session"),
                "bar": v.get("bar"), "traded": v.get("decision") == "OPENED",
                "setup_type": v.get("setup_type"), "score": v.get("score"),
                "snapshot_id": pkg.get("snapshot_id"),
            })
            row = {"symbol": sym,
                   "action": v["action"] if v.get("decision") == "OPENED" else "HOLD",
                   "model_action": v["action"], "confidence": v.get("confidence"),
                   "entry": v.get("entry") or v.get("live_price"),
                   "stop_loss": v.get("stop_loss"), "take_profit": v.get("take_profit"),
                   "rationale": v.get("rationale"), "atr": v.get("atr"),
                   "prompt_version": B.PROMPT_VERSION, "setup_type": v.get("setup_type"),
                   "score": v.get("score"), "reasons": v.get("issues"),
                   "gated": v.get("gated"), "decision": v.get("decision")}
            for k in ("session", "live_price", "live_price_stale", "last_close", "digits"):
                if ctxs[sym].get(k) is not None:
                    row[k] = ctxs[sym][k]
            validated.append(row)
        # one new entry per cycle
        opened = None
        for v, row in zip(rows, validated):
            if v.get("decision") == "OPENED":
                if opened is None:
                    opened = v
                else:
                    v["decision"] = "skip: one new entry per cycle"
                    row["action"] = "HOLD"
                    row["decision"] = v["decision"]
        self._log_signals(now, validated, resp, cost, parse_err)
        self.last_signals = [{"symbol": v.get("symbol"), "action": v.get("action"),
                              "confidence": v.get("confidence"), "score": v.get("score"),
                              "setup_type": v.get("setup_type"), "gated": v.get("gated"),
                              "decision": v.get("decision"),
                              "rationale": (v.get("rationale") or "")[:240]} for v in rows]
        record = {"run_at_utc": _iso(now), "signals": validated,
                  "data": {p["symbol"]: {**(p.get("context") or {}), "source": p.get("source"),
                                         "ticker": p.get("ticker")} for p in (pkg.get("payloads") or [])},
                  "model": MODEL, "book": BOOK, "prompt_version": B.PROMPT_VERSION,
                  "snapshot_id": pkg.get("snapshot_id")}
        fill_ts = None
        async with self.sim_lock:
            dec = self.sim.on_record(record, now)
            self.sim.stamp_positions()
            if due_pendings(self.sim, now):
                try:
                    from .lucid import _default_live_prices
                    live = await _default_live_prices(self.ds_cfg, {pe["symbol"] for pe in due_pendings(self.sim, now)}, now)
                    apply_due_fills(self.sim, {}, now, live)
                    self.sim.stamp_positions()
                    fill_ts = datetime.now(UTC)
                except Exception:
                    log.exception("deepseek same-step fill failed (pending stays queued)")
            await self._persist(now)
        tok = usage_tokens(resp.get("usage"))
        thinking = tok.get("reasoning")
        return {"ok": bool(resp.get("ok")), "model": MODEL, "book": BOOK,
                "latency_s": resp.get("latency_s"), "cost_usd": cost,
                "thinking_tokens": thinking, "answer_ts": _iso(answer_ts),
                "fill_ts": _iso(fill_ts) if fill_ts else None, "error": resp.get("error") or parse_err,
                "signals": self.last_signals, "score_signals": score_signals,
                "decisions": dec, "prompt_version": B.PROMPT_VERSION,
                "snapshot_id": pkg.get("snapshot_id")}

    async def exit_tick(self, now: datetime | None = None) -> dict:
        """18 s mechanical-exit / pending-fill replay. Never raises."""
        now = now or datetime.now(UTC)
        try:
            async with self.sim_lock:
                if not (self.sim.s.get("positions") or self.sim.s.get("pending")
                        or lucid_needs_replay(self.sim)):
                    return {"skipped": "not_needed"}
                _, until, new_ids = await _fetch_and_replay(self.sim, self.ds_cfg, now)
                self.sim.stamp_positions()
                await self._persist(now)
                if new_ids:
                    log.info("deepseek replay filled ids %s (until %s)", new_ids, until)
                return {"processed_until": self.sim.s.get("last_processed"), "filled": new_ids}
        except Exception:
            log.exception("deepseek exit tick failed (other books unaffected)")
            return {"error": "exception"}

    async def _persist(self, now: datetime) -> None:
        self.sim.save()
        self._save_engine()
        await self._persist_site(now)

    async def _persist_site(self, now: datetime) -> None:
        try:
            view = self.site_view(now)
            async with self.io_lock:
                _atomic_write(self.site_dir / "deepseek.json", view)
        except Exception:
            log.exception("deepseek.json write failed")

    def site_view(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        payload = self.sim.site_payload(recent=int(self.cc.get("recent_trades") or 20))
        a = payload.get("account") or {}
        # Normalise names so the panel comparison rows work like Carbon.
        a.setdefault("net_pnl", a.get("realized_pnl_since_reset"))
        a.setdefault("trades", a.get("trades_since_reset"))
        a.setdefault("open_positions", len(payload.get("open_positions") or []))
        a.setdefault("stopped", sum(1 for t in (payload.get("recent_trades") or [])
                                    if t.get("stopped")))
        sp = self.spend_today(now)
        cap = float(self.cc["soft_spend_usd"])
        state = "soft_cap" if float(sp["usd"]) > cap else ("waiting_for_key" if not self._key() else "ok")
        hs = self.sim.hard_stop_usd
        if uniform_exits_enabled(self.ds_cfg if hasattr(self, "ds_cfg") else self.sim.cfg):
            rules = (f"deepseek-flash entries (effort low), shared bracket-v1 package with C-OPUS-B (same charts, facts, 3 symbols, M15 close +20s); "
                     f"no model exits; {UNIFORM_BANNER}; same fill and full-size sizing as main; "
                     f"spend soft-stop ${cap:g}/day Dubai")
            payload["card"] = UNIFORM_BANNER
            payload["exit_calls_note"] = UNIFORM_BANNER
            payload["stop_txt"] = "1 ATR stop · 1 ATR target · 90 min"
        else:
            rules = (f"deepseek-flash entries (effort low), shared bracket-v1 package with C-OPUS-B (same charts, facts, 3 symbols, M15 close +20s); "
                     f"no model exits; −${hs:,.0f} net hard stop · 1 ATR target · 90-min cap; "
                     f"same fill and full-size sizing as main; spend soft-stop ${cap:g}/day Dubai")
            payload["card"] = (f"Hard stop −${hs:,.0f} net · 1 ATR target · 90-min cap · no model exits"
                               if hs else "1 ATR target · 90-min cap · no model exits")
            payload["exit_calls_note"] = "No DeepSeek exit calls — mechanical exits"
        payload["exit_rules"] = None
        payload["comparison"] = None
        payload["gemini_exit_calls_enabled"] = False
        payload.update({
            "book": BOOK,
            "label": "DS-FLASH (deepseek-flash entries, mechanical exits)",
            "model": self.cc.get("model") or MODEL,
            "effort": self.cc.get("effort") or "low",
            "key_present": bool(self._key()),
            "rules": rules,
            "stop_txt": (f"−${hs:,.0f} net · 1 ATR tgt · 90 min" if hs else "1 ATR tgt · 90 min"),
            "spend": {"day_dubai": spend_day(now), "usd": round(float(sp["usd"]), 4),
                      "soft_usd": cap, "state": state, "calls": sp.get("calls") or 0,
                      "failed": sp.get("failed") or 0, "tokens": sp.get("tokens") or {},
                      "pricing": {"off_peak": PRICES["off_peak"], "peak": PRICES["peak"],
                                  "peak_windows_utc": ["01:00-04:00", "06:00-10:00"],
                                  "peak_days": "Mon-Fri",
                                  "source": "https://api-docs.deepseek.com/quick_start/pricing/"}},
            "last_call": self.e.get("last_call"),
            "last_signals": self.last_signals,
            "exit_mode": "mech",
        })
        return payload


async def run_deepseek_exit_loop(engine: DeepSeekEngine) -> None:
    period = float(engine.cc.get("check_seconds") or 18)
    while True:
        try:
            await engine.exit_tick()
        except Exception:
            log.exception("deepseek exit loop tick failed")
        await asyncio.sleep(period)


async def stop_task(task: asyncio.Task | None, timeout: float = 30) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, timeout)
