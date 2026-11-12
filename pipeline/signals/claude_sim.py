"""Claude parallel paper sims (PAPER ONLY - nothing here places an order anywhere).

Two $100k Lucid-style paper accounts, identical except for the entry model (templates/claude/*):
  C-OPUS   entries claude-opus-5-5   (adaptive thinking, effort medium, json_schema)
  C-SONNET entries claude-sonnet-5-5 (adaptive thinking, effort medium, json_schema)
  both     exits   claude-haiku-5-5  (thinking disabled, effort low, 5 s timeout), every 18 s per open position.
Entries: one batched call per entry model per M15 close, same 4 charts + numbers for both (XAUUSD, EURUSD,
GBPUSD, US100), gated on score >= 5. Server rules (claude_rules.py) are BINDING here, unlike the Gemini sim.
Shadow books on the same data, never traded: "mech" (same fills, server rules only), "whatif" (every BUY/SELL
signal, server rules only, 1 contract) and "twin" (coin-flip side at each signal timestamp, same rules).
The Gemini sim is untouched; every public coroutine here catches its own errors.

State: state/claude/{engine.json, sims/<SIM>.json, report.json}. Logs: logs/<day>/claude_{calls,signals,
trades,exit_checks,calibration,events}.jsonl. Site: claude.json at the data root (deployed with the panel).
"""
from __future__ import annotations
import asyncio, copy, glob, json, logging, math, os, random, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .risk import _atomic_write
from . import claude_client as CC
from .claude_rules import (rules_cfg, clamp_stop, size_for, risk_per_contract, advance, trade_result, floor_for,
                           effective_stop, phase_of, m5_summary, h1_context, lucid_day, in_session, round_tick, pctl,
                           usd_group, us100_alignment, open_usd_groups, group_lock_until, set_group_lock,
                           m15_bars_closed_since_entry, phase_a_structure_reason)
from .exit_versions import CLAUDE_EXIT_PROMPT_VERSION, CLAUDE_EXIT_PROMPT_VERSION_PRE
from .uniform_exit import (uniform_exits_enabled, claude_exit_calls_enabled, arm_uniform,
                           close_tag, UNIFORM_BANNER, UNIFORM_EXIT_VERSION, TIME_CAP_MINUTES)

log = logging.getLogger("signals.claude")
UTC = timezone.utc

CLAUDE_DEFAULTS = {
    "enabled": True,
    "entry_models": ["claude-opus-5-5", "claude-sonnet-5-5"],
    "exit_model": "claude-haiku-5-5",
    "entry_effort": "medium", "exit_effort": "low",
    "timeout_seconds_entry": 60, "timeout_seconds_exit": 5, "max_attempts": 2,
    "entry_max_tokens": 8000, "exit_max_tokens": 200,
    "check_seconds": 18, "start_balance": 100000.0, "reset_token": "claude-v2-1",
    "skip_blocked_sessions": True, "m5_chart": True, "scan_offset_seconds": 20, "recent_trades": 15,
    "sims": {"claude-opus-5-5": ["C-OPUS", "Claude Opus"], "claude-sonnet-5-5": ["C-SONNET", "Claude Sonnet"]},
}
LOG_FILES = ("claude_calls.jsonl", "claude_signals.jsonl", "claude_trades.jsonl", "claude_exit_checks.jsonl",
             "claude_calibration.jsonl", "claude_events.jsonl")


def claude_cfg(cfg: dict) -> dict:
    cc = {**CLAUDE_DEFAULTS, **(cfg.get("claude") or {})}
    em = (os.environ.get("CLAUDE_ENTRY_MODELS") or "").strip()
    if em:
        cc["entry_models"] = [m.strip() for m in em.split(",") if m.strip()]
    xm = (os.environ.get("CLAUDE_EXIT_MODEL") or "").strip()
    if xm:
        cc["exit_model"] = xm
    pr = cc.get("prices") or {}
    cc["_prices"] = {k: tuple(v) for k, v in pr.items()} if pr else None
    return cc


def sim_id_for(cc: dict, model: str) -> tuple[str, str]:
    s = (cc.get("sims") or {}).get(model)
    if s:
        return s[0], s[1]
    tag = model.replace("claude-", "").split("-")[0].upper()
    return f"C-{tag}", f"Claude {tag.title()}"


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def next_m15_close(t: datetime) -> datetime:
    step = 900
    ts = int(t.timestamp())
    return datetime.fromtimestamp((ts // step + 1) * step, UTC)


def in_exit_grace(entry_time, now: datetime) -> bool:
    """True from entry until (not including) the first M15 bar close after entry.

    During this window the server ignores a Haiku CLOSE; only the hard stop and
    account risk limits can end the trade.
    """
    t0 = _dt(entry_time) if isinstance(entry_time, str) else entry_time
    return now < next_m15_close(t0)


def _side_word(side: int) -> str:
    return "LONG" if side > 0 else "SHORT"


def held_str(sec) -> str | None:
    if sec is None:
        return None
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, x = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {x:02d}s" if m else f"{x}s"


def _fmt(x, d: int) -> str:
    return "n/a" if x is None else f"{float(x):.{d}f}"


# ------------------------------------------------------------------ default I/O (injectable for tests)
async def _default_snapshot(sym: str, cfg: dict, now: datetime):
    from .exit_manager import market_snapshot
    return await market_snapshot(sym, cfg, now)


async def _default_m1(sym: str, cfg: dict, now: datetime):
    from .live_price import fetch_symbol_m1
    df, _src = await fetch_symbol_m1(sym, cfg, now)
    return df


async def _default_h1(sym: str, cfg: dict, now: datetime):
    from .data_providers import PROVIDERS
    scfg = (cfg.get("symbols") or {}).get(sym, {})
    errs = []
    for name in scfg.get("chain", cfg["data"]["default_chain"]):
        try:
            return await PROVIDERS[name]().fetch(scfg.get(name, sym), 60, 120)
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise RuntimeError(f"{sym} H1: {errs}")


async def _default_render(payloads: list[dict]) -> dict[str, bytes]:
    from .renderer import render_all
    return await render_all(payloads)


# ------------------------------------------------------------------ engine
class ClaudeEngine:
    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *, state_dir=None, logs_dir=None,
                 site_dir=None, snapshot=None, m1_fetch=None, h1_fetch=None, render=None, entry_api=None,
                 exit_api=None, rng: random.Random | None = None, key_fn=None, quote_fetch=None):
        self.cfg = cfg
        self.cc = claude_cfg(cfg)
        self.r = rules_cfg(self.cc)
        if uniform_exits_enabled(self.cfg):
            self.r["uniform_1atr"] = True
            self.r["time_stop_minutes"] = TIME_CAP_MINUTES
            self.r["time_stop_mfe_atr"] = 0.0
        self.L = cfg.get("lucid") or {}
        self.contracts = self.L.get("contracts") or {}
        self.symmap = self.L.get("symbols") or {}
        self.symbols = [s for s in cfg["general"]["symbols"] if s in self.symmap]
        self.tz = ZoneInfo(cfg["general"]["session_tz"])
        self.root = Path(state_dir or cfg["general"]["state_dir"]) / "claude"
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or (cfg.get("site") or {}).get("dir") or self.root.parent.parent)
        (self.root / "sims").mkdir(parents=True, exist_ok=True)
        self.io_lock = io_lock or asyncio.Lock()
        self.mu = asyncio.Lock()
        self._snapshot = snapshot or _default_snapshot
        self._quote_fetch = quote_fetch
        self._m1 = m1_fetch or _default_m1
        self._h1 = h1_fetch or _default_h1
        self._render = render or _default_render
        self._entry_api = entry_api or CC.entry_call
        self._exit_api = exit_api or CC.exit_call
        self._key = key_fn or CC.key_present
        self.rng = rng or random.Random()
        self.prompts = CC.load_prompts()
        self._h1_cache: dict[str, tuple[str, dict]] = {}
        self.flat_et = self.L.get("flat_by_et", "16:45")
        self.open_et = self.L.get("session_open_et", "18:00")
        self.eng = self._load_engine()
        self.sims: dict[str, dict] = {}
        for m in self.cc["entry_models"]:
            sid, label = sim_id_for(self.cc, m)
            self.sims[sid] = self._load_sim(sid, label, m)
        self.entry_task: asyncio.Task | None = None
        self.last_entry: dict = self.eng.get("last_entry") or {}
        self._backfill_exit_prompt_versions()

    # ---------------- state
    def _load_engine(self) -> dict:
        p = self.root / "engine.json"
        tok = self.cc["reset_token"]
        try:
            st = json.loads(p.read_text()) if p.exists() else None
        except Exception:
            st = None
        if st is None or st.get("reset_token") != tok:
            if st is not None:
                _atomic_write(self.root / f"engine_pre_{tok}.json", st)
            st = {"reset_token": tok, "created_at": _iso(datetime.now(UTC)), "last_entry_bar": None,
                  "shadow": [], "probes": [], "next_vid": 1, "last_entry": {}}
        return st

    def _new_sim(self, sid: str, label: str, model: str) -> dict:
        sb = float(self.cc["start_balance"])
        now = datetime.now(UTC)
        return {"sim": sid, "label": label, "entry_model": model, "exit_model": self.cc["exit_model"],
                "reset_token": self.cc["reset_token"], "created_at": _iso(now), "start_balance": sb,
                "balance": sb, "highest_close": sb, "mll_floor": sb - float(self.L.get("mll", 3000.0)),
                "blown": None, "positions": [], "next_id": 1,
                "day": {"id": str(lucid_day(now, self.open_et)), "realized": 0.0, "flattened": False},
                "locks": {}, "group_locks": {}, "last_price": {}, "trades": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0,
                "r_total": 0.0, "recent": []}

    def _load_sim(self, sid: str, label: str, model: str) -> dict:
        p = self.root / "sims" / f"{sid}.json"
        try:
            st = json.loads(p.read_text()) if p.exists() else None
        except Exception:
            st = None
        if st is None or st.get("reset_token") != self.cc["reset_token"] or st.get("entry_model") != model:
            if st is not None:
                _atomic_write(self.root / "sims" / f"{sid}_pre_{self.cc['reset_token']}.json", st)
            st = self._new_sim(sid, label, model)
        st["label"] = label
        st.setdefault("group_locks", {})
        return st

    def _save(self) -> None:
        for sid, s in self.sims.items():
            _atomic_write(self.root / "sims" / f"{sid}.json", s)
        self.eng["last_entry"] = self.last_entry
        _atomic_write(self.root / "engine.json", self.eng)

    def _jsonl(self, name: str, obj: dict, t: datetime | None = None) -> None:
        t = t or datetime.now(UTC)
        d = self.logs_dir / t.astimezone(self.tz).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a") as f:
            f.write(json.dumps(obj, default=str, separators=(",", ":")) + "\n")

    def _backfill_exit_prompt_versions(self) -> int:
        """Stamp pre-grace on already-closed Claude trades that have no version tag."""
        n = 0
        for sid, sim in self.sims.items():
            for row in sim.get("recent") or []:
                if not row.get("exit_prompt_version"):
                    row["exit_prompt_version"] = CLAUDE_EXIT_PROMPT_VERSION_PRE
                    n += 1
        for f in sorted(glob.glob(str(self.logs_dir / "*" / "claude_trades.jsonl"))):
            try:
                raw = Path(f).read_text()
            except Exception:
                continue
            lines, out, changed = raw.splitlines(), [], False
            for line in lines:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    out.append(line)
                    continue
                if not row.get("exit_prompt_version"):
                    row["exit_prompt_version"] = CLAUDE_EXIT_PROMPT_VERSION_PRE
                    changed = True
                    n += 1
                out.append(json.dumps(row, default=str, separators=(",", ":")))
            if changed:
                path = Path(f)
                fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
                with os.fdopen(fd, "w") as fh:
                    fh.write("\n".join(out) + "\n")
                os.replace(tmp, path)
        return n

    # ---------------- helpers
    def spec(self, contract: str) -> dict:
        return self.contracts[contract]

    def contract_of(self, sym: str) -> str:
        return (self.symmap.get(sym) or {}).get("contracts", ["?"])[0]

    def pnl(self, p: dict, price: float) -> float:
        return p["side"] * (price - p["entry"]) * int(p["qty"]) * float(self.spec(p["contract"])["point_value"])

    def comm_rt(self, p: dict) -> float:
        return 2 * float(self.spec(p["contract"]).get("commission") or 0) * int(p["qty"])

    def open_stop_risk(self, sim: dict) -> float:
        return sum(max(0.0, -self.pnl(p, effective_stop(p))) + self.comm_rt(p) for p in sim["positions"])

    def equity(self, sim: dict) -> float:
        lp = sim.get("last_price") or {}
        return sim["balance"] + sum(self.pnl(p, lp.get(p["symbol"], p["entry"])) for p in sim["positions"])

    def roll_day(self, sim: dict, now: datetime) -> None:
        d = str(lucid_day(now, self.open_et))
        if sim["day"]["id"] == d:
            return
        # EOD close: trailing floor follows the highest closing balance, locked at locked_mll_balance
        sim["highest_close"] = max(sim["highest_close"], sim["balance"])
        lock = float(self.L.get("locked_mll_balance", sim["start_balance"] + 100))
        sim["mll_floor"] = max(sim["mll_floor"], min(sim["highest_close"] - float(self.L.get("mll", 3000.0)), lock))
        self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": "day_roll", "day": d,
                                            "balance": sim["balance"], "mll_floor": sim["mll_floor"]}, now)
        sim["day"] = {"id": d, "realized": 0.0, "flattened": False}

    def entries_allowed(self, now: datetime) -> bool:
        mins = int(self.L.get("no_new_entries_last_minutes", 15))
        return in_session(now, self.flat_et, self.open_et) and \
            in_session(now + timedelta(minutes=mins), self.flat_et, self.open_et)

    def guard(self, sim: dict, new_risk: float, now: datetime) -> str | None:
        """None = allowed; else the blocking rule."""
        if sim.get("blown"):
            return "account blown (equity reached the trailing floor)"
        if sim["day"].get("flattened"):
            return "daily loss limit hit today (flattened at -$1,500)"
        if not self.entries_allowed(now):
            return "outside Lucid session / last 15 min before 16:45 ET"
        osr = self.open_stop_risk(sim)
        day_loss = max(0.0, -float(sim["day"]["realized"]))
        if day_loss + osr + new_risk > self.r["daily_loss_usd"]:
            return (f"daily guard: day loss ${day_loss:,.0f} + open stop risk ${osr:,.0f} + new ${new_risk:,.0f}"
                    f" > ${self.r['daily_loss_usd']:,.0f}")
        room = sim["balance"] - sim["mll_floor"]
        if room - osr - new_risk < self.r["trail_buffer_usd"]:
            return (f"trailing guard: room ${room:,.0f} - open stop risk ${osr:,.0f} - new ${new_risk:,.0f}"
                    f" < ${self.r['trail_buffer_usd']:,.0f}")
        return None

    def lock_reason(self, sim: dict, sym: str, side: int, now: datetime, payload: dict | None) -> str | None:
        lk = (sim.get("locks") or {}).get(sym)
        if not lk:
            return None
        if now < _dt(lk["until"]):
            return f"re-entry lock until {lk['until']}" + (" (after a loss)" if lk.get("loss") else "")
        if lk.get("loss") and side == lk.get("side") and not lk.get("retested"):
            lvl, ex_t = float(lk["level"]), _dt(lk["exit_t"])
            ctx = (payload or {}).get("context") or {}
            lows, highs = [], []
            for c in (payload or {}).get("candles") or []:
                if datetime.fromtimestamp(int(c["time"]), UTC) + timedelta(minutes=15) > ex_t:
                    lows.append(c["low"]); highs.append(c["high"])
            for k, arr in (("forming_m15_low", lows), ("forming_m15_high", highs)):
                if ctx.get(k) is not None:
                    arr.append(ctx[k])
            hit = any(l is not None and l <= lvl for l in lows) if side > 0 else \
                any(h is not None and h >= lvl for h in highs)
            if hit:
                lk["retested"] = True
                return None
            return f"same-side re-entry blocked until price retests the losing stop {lvl}"
        return None

    # ---------------- entry cycle
    def schedule_entry(self, payloads: list[dict] | None) -> None:
        """Called from the GHA loop after each Gemini cycle. Background task; never awaited by the loop."""
        if not payloads or not self.cc.get("enabled"):
            return
        if self.entry_task is not None and not self.entry_task.done():
            return
        self.entry_task = asyncio.create_task(self._safe_cycle(payloads), name="claude-entry")

    async def _safe_cycle(self, payloads) -> dict:
        try:
            return await self.on_cycle(payloads)
        except Exception:
            log.exception("claude entry cycle failed (Claude sims only; Gemini unaffected)")
            return {"error": True}

    async def fetch_entry_payloads(self, now: datetime) -> list[dict]:
        """Own M15+live snapshots for the +20s entry scan. Does not use the Gemini cycle payloads."""
        out = []
        rs = await asyncio.gather(*[self._snapshot(s, self.cfg, now) for s in self.symbols], return_exceptions=True)
        for s, r in zip(self.symbols, rs):
            if isinstance(r, Exception):
                log.warning("claude scan snapshot %s failed: %s", s, r)
                continue
            if r and r[0]:
                out.append(r[0])
        return out

    async def _htf(self, sym: str, now: datetime) -> dict:
        key = now.strftime("%Y%m%d%H")
        hit = self._h1_cache.get(sym)
        if hit and hit[0] == key:
            return hit[1]
        try:
            h1 = await self._h1(sym, self.cfg, now)
            anchor = ((self.cfg.get("symbols") or {}).get(sym) or {}).get("vwap_anchor", "utc_day")
            val = h1_context(h1, now, anchor)
        except Exception as e:
            log.warning("claude h1 context %s failed: %s", sym, e)
            val = {"h1_trend": None, "prior_day_high": None, "prior_day_low": None}
        self._h1_cache[sym] = (key, val)
        return val

    def entry_text(self, p: dict, i: int, n: int, htf: dict) -> str:
        from .gemini_client import slim_gemini_context
        ctx = {"symbol": p["symbol"], "source": f"{p['source']}:{p['ticker']}", "timeframe": p["timeframe"],
               **slim_gemini_context(p["context"])}
        d = int(p["context"].get("digits") or 5)
        for k in ("h1_trend", "prior_day_high", "prior_day_low"):
            v = htf.get(k)
            if v is not None:
                ctx[k] = round(v, d) if isinstance(v, float) else v
        return f"Chart {i}/{n} - {p['symbol']}\nNumeric context (authoritative): {json.dumps(ctx, default=str)}"

    async def on_cycle(self, payloads: list[dict], now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        lp = [p for p in payloads if p.get("symbol") in self.symmap]
        lp.sort(key=lambda p: self.symbols.index(p["symbol"]) if p["symbol"] in self.symbols else 99)
        if not lp:
            return {"skipped": "no_payloads"}
        bar = max(p["context"]["last_closed_bar_utc"] for p in lp)
        if self.eng.get("last_entry_bar") == bar:
            await self.publish(now, report=False)
            return {"skipped": "same_bar"}
        if not self._key():
            await self.publish(now, report=False)
            return {"skipped": "no_key"}
        self.eng["last_entry_bar"] = bar
        blocked = all(bool(p["context"].get("session_blocks_automation")) for p in lp)
        if blocked and self.cc.get("skip_blocked_sessions", True):
            self.last_entry = {"bar": bar, "t": _iso(now), "skipped": "all symbols in a blocked session "
                               "(ASIAN_SESSION_CHOP / LOW_LIQUIDITY_DEADZONE): the v2 prompt would HOLD every one"}
            self._jsonl("claude_events.jsonl", {"t": _iso(now), "event": "entry_skip_blocked_session", "bar": bar}, now)
            await self.publish(now)
            return {"skipped": "blocked_session"}
        syms = [p["symbol"] for p in lp]
        htf = dict(zip(syms, await asyncio.gather(*[self._htf(s, now) for s in syms])))
        rp = [{**p, "page_key": f"ce-{p['symbol']}"} for p in lp]
        t_r = time.monotonic()
        pngs = await self._render(rp)
        render_ms = round((time.monotonic() - t_r) * 1000)
        content = []
        for i, p in enumerate(lp, 1):
            content.append({"type": "text", "text": self.entry_text(p, i, len(lp), htf[p["symbol"]])})
            if pngs.get(p["symbol"]):
                content.append(CC.image_block(pngs[p["symbol"]]))
        content.append({"type": "text", "text": "Return the JSON signal batch for all symbols now."})
        system = CC.entry_system_prompt(syms, self.prompts)
        schema = CC.signal_batch_schema(syms)
        models = list(self.cc["entry_models"])
        res = await asyncio.gather(*[self._entry_api(self.cc, m, system, content, schema) for m in models],
                                   return_exceptions=True)
        await self._refresh_live(res, now)
        out = {"bar": bar, "t": _iso(now), "render_ms": render_ms, "sims": {}}
        async with self.mu:
            t_done = datetime.now(UTC)
            for m, r in zip(models, res):
                sid, _ = sim_id_for(self.cc, m)
                if isinstance(r, Exception):
                    r = {"ok": False, "model": m, "error": f"{type(r).__name__}: {str(r)[:200]}"}
                self._log_call(sid, "entry", m, r, t_done, bar=bar, n_symbols=len(syms))
                sigs = self._process_signals(sid, m, r, lp, htf, bar, t_done)
                out["sims"][sid] = {"ok": r.get("ok"), "latency_ms": r.get("latency_ms"), "cost_usd": r.get("cost_usd"),
                                    "error": r.get("error"),
                                    "signals": [{k: s.get(k) for k in ("symbol", "action", "setup_type", "score",
                                                                        "confidence", "decision")} for s in sigs]}
            self.last_entry = out
            await self._persist(now)
        await self.publish(now)
        return out

    async def _fill_quotes(self, syms: list[str], t: datetime) -> dict[str, dict]:
        """Fresh live quotes from the SAME path the 18 s exit checks use (enrich_live_prices: Dukascopy tick bid /
        FirmTape NQ + basis). With an injected snapshot (tests / dry runs) the quote is read from it."""
        from .live_price import fetch_fill_quotes, fill_quote_from_context
        if self._quote_fetch is not None:
            return await self._quote_fetch(syms, self.cfg, t)
        if self._snapshot is _default_snapshot:
            return await fetch_fill_quotes(syms, self.cfg, t)
        got = await asyncio.gather(*[self._snapshot(s, self.cfg, t) for s in syms], return_exceptions=True)
        return {s: fill_quote_from_context(s, r[0].get("context") or {}) for s, r in zip(syms, got)
                if not isinstance(r, BaseException) and r and r[0]}

    async def _refresh_live(self, res, now: datetime) -> None:
        """Fresh live quote for symbols with a tradable signal, taken when the answer arrives (entry calls take
        20-60 s), so the fill is at the market at fill time, not at the price the charts were drawn with. The
        last finished M1 close is NOT used: it is 1-2 minutes old (same bug as the Gemini sim, 12 Nov)."""
        want = set()
        for r in res:
            if isinstance(r, dict):
                for s in ((r.get("parsed") or {}).get("signals") or []):
                    if isinstance(s, dict) and s.get("action") in ("BUY", "SELL"):
                        want.add(str(s.get("symbol") or "").upper())
        want &= set(self.symmap)
        fq = self.eng.setdefault("fill_quotes", {})
        fq.clear()
        if not want:
            return
        t = datetime.now(UTC)
        try:
            quotes = await self._fill_quotes(sorted(want), t)
        except Exception as e:
            log.warning("claude fill quotes failed: %s", e)
            quotes = {}
        for s, q in (quotes or {}).items():
            if q and q.get("price") is not None:
                fq[s] = {**q, "at": q.get("fetched_at") or _iso(t)}

    def _log_call(self, sid: str, kind: str, model: str, r: dict, t: datetime, **extra) -> None:
        u = r.get("usage") or {}
        self._jsonl("claude_calls.jsonl", {
            "t": _iso(t), "sim": sid, "kind": kind, "model": model, "ok": bool(r.get("ok")),
            "latency_ms": r.get("latency_ms"), "total_ms": r.get("total_ms"), "attempts": r.get("attempts"),
            "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
            "thinking_tokens": r.get("thinking_tokens"), "cache_read": u.get("cache_read_input_tokens"),
            "cost_usd": r.get("cost_usd"), "stop_reason": r.get("stop_reason"), "error": r.get("error"),
            "request_id": r.get("request_id"), **extra}, t)

    def _vtrade(self, book: str, sid: str, sym: str, side: int, entry: float, stop: float, atr: float, t: datetime,
                qty: int = 1, **meta) -> dict:
        c = self.contract_of(sym)
        sp = self.spec(c)
        vid = self.eng.get("next_vid", 1)
        self.eng["next_vid"] = vid + 1
        return {"vid": vid, "book": book, "sim": sid, "symbol": sym, "contract": c, "qty": qty, "side": side,
                "entry": entry, "stop": stop, "init_stop": stop, "atr": atr, "entry_time": _iso(t),
                "scan_from": _iso(t), "tick": float(sp["tick"]),
                "comm_pts": 2 * float(sp.get("commission") or 0) / float(sp["point_value"]),
                "mfe_pts": 0.0, "mae_pts": 0.0, "floor": None, "floor_label": None, "reach_05": None, **meta}

    def _process_signals(self, sid: str, model: str, r: dict, lp: list[dict], htf: dict, bar: str,
                         now: datetime) -> list[dict]:
        sim = self.sims[sid]
        self.roll_day(sim, now)
        parsed = r.get("parsed") or {}
        by = {}
        for s in parsed.get("signals") or []:
            if isinstance(s, dict) and s.get("symbol"):
                by[str(s["symbol"]).upper()] = s
        ctxs = {p["symbol"]: p for p in lp}
        rows = []
        for p in lp:
            sym, ctx = p["symbol"], p["context"]
            raw = by.get(sym)
            v = CC.validate_entry_signal(raw or {"action": "HOLD"}, ctx, int(self.r["min_score"]))
            if raw is None:
                v["issues"].append("missing from model output" if r.get("ok") else f"call failed: {r.get('error')}")
            price = ctx.get("live_price") if ctx.get("live_price") is not None else ctx.get("last_close")
            v.update({"t": _iso(now), "sim": sid, "model": model, "bar": bar, "live_price": price,
                      "live_price_stale": ctx.get("live_price_stale"), "atr": ctx.get("atr"),
                      "session": ctx.get("session"), **{k: htf.get(sym, {}).get(k) for k in ("h1_trend",
                                                                                              "prior_day_high", "prior_day_low")}})
            v["decision"] = "HOLD"
            rows.append(v)
            if v["action"] in ("BUY", "SELL") and price is not None and ctx.get("atr"):
                side = 1 if v["action"] == "BUY" else -1
                atr = float(ctx["atr"])
                stop, dist, note = clamp_stop(side, float(price), v.get("stop_loss"), atr, self.r)
                if stop is None:
                    dist, note = 1.0 * atr, f"what-if fallback 1.0 ATR ({note})"
                    stop = float(price) - side * dist
                meta = {"setup_type": v["setup_type"], "score": v["score"], "confidence": v["confidence"],
                        "session": ctx.get("session"), "gated": v["gated"], "model": model, "bar": bar,
                        "stop_note": note}
                w = self._vtrade("whatif", sid, sym, side, float(price), stop, atr, now, **meta)
                tw_side = self.rng.choice((1, -1))
                tw = self._vtrade("twin", sid, sym, tw_side, float(price),
                                  float(price) - tw_side * dist, atr, now, **meta)
                if uniform_exits_enabled(self.cfg):
                    for row in (w, tw):
                        try:
                            tick = float(self.spec(row["contract"])["tick"])
                        except Exception:
                            tick = None
                        arm_uniform(row, now, late=False, tick=tick)
                self.eng["shadow"].append(w)
                self.eng["shadow"].append(tw)
        # real entries: one per cycle, highest score first
        cands = sorted([v for v in rows if v["gated"]], key=lambda v: (v["score"], v["confidence"]), reverse=True)
        opened = None
        for v in cands:
            if opened is not None:
                v["decision"] = "skip: one new entry per cycle"
                continue
            why = self._try_open(sim, v, ctxs[v["symbol"]], now)
            if why is None:
                v["decision"] = "OPENED"
                opened = v
            else:
                v["decision"] = f"skip: {why}"
        for v in rows:
            self._jsonl("claude_signals.jsonl", {k: v.get(k) for k in (
                "t", "sim", "model", "bar", "symbol", "action", "setup_type", "score", "confidence", "risk_pct",
                "entry", "stop_loss", "take_profit", "risk_reward", "level_event", "rationale", "trend_vs_vwap",
                "volatility", "gated", "issues", "decision", "live_price", "live_price_stale", "atr", "session",
                "h1_trend", "prior_day_high", "prior_day_low")}, now)
        return rows

    def _try_open(self, sim: dict, v: dict, payload: dict, now: datetime) -> str | None:
        sym, ctx = v["symbol"], payload["context"]
        if any(p["symbol"] == sym for p in sim["positions"]):
            return "already in a position on this symbol"
        if ctx.get("live_price_stale"):
            return "live price stale"
        side = 1 if v["action"] == "BUY" else -1
        atr = float(ctx["atr"])
        c = self.contract_of(sym)
        sp = self.spec(c)
        lr = self.lock_reason(sim, sym, side, now, payload)
        if lr:
            return lr
        grp = usd_group(sym, side)
        if grp:
            until = group_lock_until(sim, grp, now)
            if until is not None:
                why = f"USD group {grp} locked until {until.isoformat()} (after a stop-out)"
                self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": "skip_usd_group_lock",
                                                    "symbol": sym, "side": _side_word(side), "group": grp,
                                                    "until": _iso(until)}, now)
                return why
            held = next((p for p in sim["positions"] if usd_group(p["symbol"], p["side"]) == grp), None)
            if held is not None:
                why = (f"USD group {grp} already has an open position "
                       f"({held['symbol']} {_side_word(held['side'])} #{held['id']})")
                self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": "skip_usd_group",
                                                    "symbol": sym, "side": _side_word(side), "group": grp,
                                                    "open_id": held["id"], "open_symbol": held["symbol"],
                                                    "open_side": _side_word(held["side"])}, now)
                return why
        ref = float(ctx.get("live_price") or ctx.get("last_close"))
        # Fill at the fresh live quote taken when the answer arrived (same feed as the exit checks / stop).
        from .live_price import FILL_QUOTE_MAX_AGE_S, FILL_QUOTE_SOURCES, fill_quote_problem
        q = (self.eng.get("fill_quotes") or {}).get(sym)
        bad = fill_quote_problem(q, sources=tuple(self.L.get("fill_quote_sources") or FILL_QUOTE_SOURCES),
                                 max_age_s=float(self.L.get("fill_quote_max_age_s", FILL_QUOTE_MAX_AGE_S)))
        if not bad and (now - _dt(q["at"])).total_seconds() > 30:
            bad = f"live quote taken {(now - _dt(q['at'])).total_seconds():.0f}s before the fill"
        if bad:
            return f"no fresh live quote at fill time: {bad}"
        fill_ref, fill_t = float(q["price"]), _dt(q["at"])
        adverse = side * (fill_ref - ref)
        lim = float(self.r.get("fill_max_adverse_atr", 0.3)) * atr
        if adverse > lim:
            why = (f"live quote {adverse / atr:.2f} ATR worse than chart_live_price "
                   f"(> {self.r.get('fill_max_adverse_atr', 0.3)} ATR)")
            self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": "fill_skip_adverse",
                                                "symbol": sym, "side": _side_word(side), "quote": fill_ref,
                                                "chart_live_price": ref, "adverse_atr": round(adverse / atr, 4),
                                                "limit_atr": float(self.r.get("fill_max_adverse_atr", 0.3))}, now)
            return why
        if adverse > self.r["max_chase_atr"] * atr:
            return f"price ran {adverse / atr:.2f} ATR past the signal (> {self.r['max_chase_atr']})"
        tick = float(sp["tick"])
        fill = round_tick(fill_ref + side * int(self.r["slippage_ticks"]) * tick, tick)
        fill_source = f"live_quote:{q.get('source')}"
        fill_check = {"fill_t": _iso(fill_t), "quote_price": fill_ref, "quote_ts": q.get("ts"),
                      "quote_source": q.get("source"), "quote_age_s": q.get("age_s"), "booked": fill,
                      "slippage_ticks": int(self.r["slippage_ticks"]), "tick": tick,
                      "adverse_ticks_vs_quote": round(side * (fill - fill_ref) / tick, 3),
                      "tolerance_ticks": int(self.r["slippage_ticks"]) + 0.5,   # slippage + half-tick grid rounding
                      "within_slippage": abs(fill - fill_ref) <= tick * (int(self.r["slippage_ticks"]) + 0.5) + 1e-9,
                      "chart_live_price": ref}
        stop, dist, note = clamp_stop(side, fill, v.get("stop_loss"), atr, self.r)
        if stop is None:
            return note
        stop = round_tick(stop, float(sp["tick"]))
        dist = abs(fill - stop)
        qty, why = size_for(c, side, dist, sp, self.r, sim["positions"])
        if qty < 1:
            return why
        new_risk = qty * risk_per_contract(dist, sp)
        g = self.guard(sim, new_risk, now)
        if g:
            return g
        pid = sim["next_id"]
        sim["next_id"] = pid + 1
        pos = self._vtrade("real", sim["sim"], sym, side, fill, stop, atr, fill_t, qty=qty,
                           setup_type=v["setup_type"], score=v["score"], confidence=v["confidence"],
                           session=ctx.get("session"), model=v["model"], bar=v["bar"], stop_note=note,
                           fill_source=fill_source, fill_check=fill_check)
        pos.update({"id": pid, "model_stop": v.get("stop_loss"), "take_profit": v.get("take_profit"),
                    "rationale": (v.get("rationale") or "")[:300], "risk_usd": round(new_risk, 2),
                    "signal_price": ref, "exit_fail": 0, "checks": 0, "server_only": False})
        if uniform_exits_enabled(self.cfg):
            arm_uniform(pos, fill_t, late=False, tick=float(sp["tick"]))
        sim["positions"].append(pos)
        sim.setdefault("last_price", {})[sym] = fill
        mech = copy.deepcopy(pos)
        mech.update({"book": "mech", "vid": self.eng.get("next_vid", 1), "real_id": pid})
        self.eng["next_vid"] = mech["vid"] + 1
        self.eng["shadow"].append(mech)
        ev = {"t": _iso(now), "sim": sim["sim"], "event": "open", "id": pid,
              "symbol": sym, "side": _side_word(side), "qty": qty, "contract": c,
              "entry": fill, "stop": stop, "stop_note": note,
              "risk_usd": round(new_risk, 2), "score": v["score"],
              "setup_type": v["setup_type"], "fill_source": fill_source,
              "fill_check": fill_check, "usd_group": grp}
        if sym == "US100":
            al = us100_alignment(side, open_usd_groups([x for x in sim["positions"] if x["id"] != pid]))
            ev["us100_usd_alignment"] = al
            self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": "us100_usd_alignment",
                                                "id": pid, "side": _side_word(side), "alignment": al,
                                                "usd_groups": sorted(open_usd_groups(
                                                    [x for x in sim["positions"] if x["id"] != pid]))}, now)
        self._jsonl("claude_events.jsonl", ev, now)
        log.info("claude %s OPEN #%d %s %s %d %s @ %s stop %s (%s) risk $%.0f", sim["sim"], pid, sym,
                 _side_word(side), qty, c, fill, stop, note, new_risk)
        return None

    # ---------------- closes
    def _close_real(self, sim: dict, pos: dict, price: float, t: datetime, reason: str, extra: dict | None = None):
        res = trade_result(pos, price, self.spec(pos["contract"]))
        sim["positions"] = [p for p in sim["positions"] if p["id"] != pos["id"]]
        sim["balance"] = round(sim["balance"] + res["net"], 2)
        sim["realized_pnl"] = round(sim["realized_pnl"] + res["net"], 2)
        sim["day"]["realized"] = round(sim["day"]["realized"] + res["net"], 2)
        sim["trades"] += 1
        sim["wins" if res["net"] > 0 else "losses"] += 1 if res["net"] != 0 else 0
        sim["r_total"] = round(sim["r_total"] + (res["R"] or 0), 3)
        loss = res["net"] < 0
        until = next_m15_close(t)
        if loss:
            until = max(until, t + timedelta(minutes=int(self.r["loss_lock_minutes"])))
        sim.setdefault("locks", {})[pos["symbol"]] = {"until": _iso(until), "loss": loss, "side": pos["side"],
                                                      "level": pos["init_stop"], "retested": False, "exit_t": _iso(t)}
        if str(reason).startswith("hard_stop"):
            g = usd_group(pos["symbol"], pos["side"])
            if g:
                mins = int(self.r.get("group_lock_minutes", 30))
                set_group_lock(sim, g, t, mins)
                self._jsonl("claude_events.jsonl", {"t": _iso(t), "sim": sim["sim"], "event": "usd_group_lock",
                                                    "group": g, "symbol": pos["symbol"], "id": pos.get("id"),
                                                    "until": (sim.get("group_locks") or {}).get(g),
                                                    "reason": reason}, t)
        hold = (t - _dt(pos["entry_time"])).total_seconds()
        row = self._trade_row(pos, price, t, reason, res, sim=sim, extra=extra)
        row["balance_after"] = sim["balance"]
        self._jsonl("claude_trades.jsonl", row, t)
        sim["recent"] = ([{k: row.get(k) for k in ("t_exit", "entry_time", "symbol", "contract", "qty", "side",
                                                    "entry", "exit", "reason", "net", "R", "hold_seconds",
                                                    "setup_type", "score", "exit_prompt_version")}] + (sim.get("recent") or []))[:50]
        log.info("claude %s CLOSE #%s %s %s @ %s net %+.2f (%.2fR) held %s", sim["sim"], pos["id"],
                 pos["symbol"], reason, price, res["net"], res["R"] or 0, held_str(hold))
        return res

    def _trade_row(self, t: dict, price: float, te: datetime, reason: str, res: dict, sim=None, extra=None) -> dict:
        atr = float(t["atr"])
        hold = (te - _dt(t["entry_time"])).total_seconds()
        return {"t_exit": _iso(te), "book": t["book"], "sim": t["sim"], "id": t.get("id") or t.get("real_id"),
                "vid": t.get("vid"), "symbol": t["symbol"], "contract": t["contract"], "qty": t["qty"],
                "side": _side_word(t["side"]), "entry_time": t["entry_time"], "entry": t["entry"], "exit": price,
                "stop": t["init_stop"], "stop_note": t.get("stop_note"), "reason": reason, **res,
                "mfe_atr": round(float(t.get("mfe_pts") or 0) / atr, 3), "mae_atr": round(float(t.get("mae_pts") or 0) / atr, 3),
                "reach_05_before_stop": t.get("reach_05"), "setup_type": t.get("setup_type"), "score": t.get("score"),
                "confidence": t.get("confidence"), "session": t.get("session"), "gated": t.get("gated"),
                "model": t.get("model"), "bar": t.get("bar"), "hold_seconds": int(hold),
                "fill_source": t.get("fill_source"), "fill_check": t.get("fill_check"),
                "exit_prompt_version": close_tag(self.cfg) if uniform_exits_enabled(self.cfg) else CLAUDE_EXIT_PROMPT_VERSION,
                "exit_mode": close_tag(self.cfg) if uniform_exits_enabled(self.cfg) else "haiku", **(extra or {})}

    # ---------------- exit tick (18 s)
    def has_work(self) -> bool:
        return bool(self.eng.get("shadow") or self.eng.get("probes") or
                    any(s["positions"] for s in self.sims.values()))

    async def exit_tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        if not self.has_work():
            return {"idle": True}
        async with self.mu:
            return await self._exit_tick(now)

    async def _exit_tick(self, now: datetime) -> dict:
        real_syms = {p["symbol"] for s in self.sims.values() for p in s["positions"]}
        other = ({t["symbol"] for t in self.eng["shadow"]} | {p["symbol"] for p in self.eng["probes"]}) - real_syms
        snaps, m1s = {}, {}
        rs = await asyncio.gather(*[self._snapshot(s, self.cfg, now) for s in real_syms], return_exceptions=True)
        for s, r in zip(real_syms, rs):
            if isinstance(r, Exception):
                log.warning("claude exit snapshot %s failed: %s", s, r)
                continue
            snaps[s], m1s[s] = r
        ro = await asyncio.gather(*[self._m1(s, self.cfg, now) for s in other], return_exceptions=True)
        for s, r in zip(other, ro):
            if not isinstance(r, Exception) and r is not None:
                m1s[s] = r
        live = self.eng.setdefault("live", {})
        for s, df in m1s.items():
            ctx = (snaps.get(s) or {}).get("context")
            px = (ctx or {}).get("live_price")
            if px is None and df is not None and len(df):
                px = float(df["close"].iloc[-1])
            if px is not None:
                live[s] = {"price": float(px), "t": _iso(now)}
        out = {"shadow_closed": 0, "real_closed": 0, "checks": 0}
        if uniform_exits_enabled(self.cfg):
            for sim in self.sims.values():
                for p in sim["positions"]:
                    if p.get("uniform_exit") != UNIFORM_EXIT_VERSION:
                        try:
                            tick = float(self.spec(p["contract"])["tick"])
                        except Exception:
                            tick = None
                        arm_uniform(p, now, late=True, tick=tick)
            for trow in self.eng["shadow"]:
                if trow.get("uniform_exit") != UNIFORM_EXIT_VERSION:
                    try:
                        tick = float(self.spec(trow["contract"])["tick"])
                    except Exception:
                        tick = None
                    arm_uniform(trow, now, late=True, tick=tick)
        # shadow books
        keep = []
        for t in self.eng["shadow"]:
            ev = advance(t, m1s.get(t["symbol"]), self.r, flat_et=self.flat_et, open_et=self.open_et, now=now)
            if ev:
                res = trade_result(t, ev["price"], self.spec(t["contract"]))
                self._jsonl("claude_trades.jsonl", self._trade_row(t, ev["price"], _dt(ev["t"]), ev["reason"], res), now)
                out["shadow_closed"] += 1
            else:
                keep.append(t)
        self.eng["shadow"] = keep
        self._resolve_probes(m1s, now)
        # real positions: server rules first (binding), then the exit model
        calls = []
        for sim in self.sims.values():
            self.roll_day(sim, now)
            for p in list(sim["positions"]):
                ev = advance(p, m1s.get(p["symbol"]), self.r, flat_et=self.flat_et, open_et=self.open_et, now=now)
                if ev:
                    self._close_real(sim, p, ev["price"], _dt(ev["t"]), ev["reason"])
                    out["real_closed"] += 1
            for s, v in live.items():
                if any(p["symbol"] == s for p in sim["positions"]):
                    sim.setdefault("last_price", {})[s] = v["price"]
            self._account_limits(sim, now)
            if not uniform_exits_enabled(self.cfg):
                for p in list(sim["positions"]):
                    snap = snaps.get(p["symbol"])
                    if not snap:
                        continue
                    ctx = snap.get("context") or {}
                    mfe = max(float(p.get("mfe_pts") or 0), 0.0) / float(p["atr"])
                    reason = phase_a_structure_reason(p, ctx, now, phase_of(mfe, self.r))
                    if not reason:
                        continue
                    sc = ctx.get("signal_candle") or {}
                    close = float(sc["close"])
                    sp = self.spec(p["contract"])
                    px = round_tick(close - p["side"] * int(self.r["slippage_ticks"]) * float(sp["tick"]),
                                    float(sp["tick"]))
                    self._close_real(sim, p, px, now, reason)
                    out["real_closed"] += 1
            if claude_exit_calls_enabled(self.cfg) and not uniform_exits_enabled(self.cfg):
                for p in sim["positions"]:
                    if p["symbol"] in snaps:
                        calls.append(self._exit_check(sim, p, (snaps[p["symbol"]], m1s.get(p["symbol"])), now))
        if calls:
            res = await asyncio.gather(*calls, return_exceptions=True)
            for r in res:
                if isinstance(r, Exception):
                    log.warning("claude exit check failed: %s", r)
                else:
                    out["checks"] += 1
                    if r.get("closed"):
                        out["real_closed"] += 1
        await self._persist(now)
        if out["real_closed"] or out["shadow_closed"]:
            await self.publish(now)
        return out

    def _account_limits(self, sim: dict, now: datetime) -> None:
        if not sim["positions"]:
            return
        lp = sim.get("last_price") or {}
        unreal = sum(self.pnl(p, lp.get(p["symbol"], p["entry"])) for p in sim["positions"])
        reason = None
        if sim["day"]["realized"] + unreal <= -self.r["daily_loss_usd"]:
            reason = "daily_flatten_-1500"
            sim["day"]["flattened"] = True
        elif sim["balance"] + unreal <= sim["mll_floor"]:
            reason = "trailing_floor_breach"
            sim["blown"] = _iso(now)
        if reason:
            for p in list(sim["positions"]):
                sp = self.spec(p["contract"])
                px = lp.get(p["symbol"], p["entry"]) - p["side"] * int(self.r["slippage_ticks"]) * float(sp["tick"])
                self._close_real(sim, p, round_tick(px, float(sp["tick"])), now, reason)
            self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": reason,
                                                "balance": sim["balance"]}, now)

    def exit_values(self, p: dict, ctx: dict, m1, now: datetime) -> dict:
        from .exit_manager import find_walls, gex_summary_text, _fmt_px
        d = int(ctx.get("digits") or 5)
        price = float(ctx.get("live_price") if ctx.get("live_price") is not None else ctx.get("last_close"))
        atr = float(p["atr"])
        pnl_atr = p["side"] * (price - p["entry"]) / atr
        mfe = max(float(p.get("mfe_pts") or 0), p["side"] * (price - p["entry"]), 0.0) / atr
        mae = max(float(p.get("mae_pts") or 0), -p["side"] * (price - p["entry"]), 0.0) / atr
        walls = find_walls(ctx, p["side"], price, p["entry"], atr, None)
        sc = ctx.get("signal_candle") or {}
        m5 = m5_summary(m1, now)
        fl = p.get("floor")
        return {
            "symbol": p["symbol"], "side": _side_word(p["side"]), "phase": phase_of(mfe, self.r),
            "minutes": f"{(now - _dt(p['entry_time'])).total_seconds() / 60:.1f}", "session": ctx.get("session"),
            "m15_bars_closed_since_entry": m15_bars_closed_since_entry(p["entry_time"], now),
            "entry": _fmt(p["entry"], d), "price": _fmt(price, d), "pnl_atr": f"{pnl_atr:+.2f}",
            "mfe_atr": f"{mfe:.2f}", "mae_atr": f"{mae:.2f}", "setup_type": p.get("setup_type"),
            "entry_rationale_short": (p.get("rationale") or "n/a")[:160],
            "entry_stop": _fmt(p["init_stop"], d), "entry_target": _fmt(p.get("take_profit"), d),
            "next_wall_label": walls.get("next_wall_label"), "next_wall_price": _fmt_px(walls.get("next_wall_price"), d),
            "dist_to_wall_atr": "n/a" if walls.get("dist_to_wall_atr") is None else f"{walls['dist_to_wall_atr']:.2f}",
            "invalid_level_label": walls.get("invalid_level_label"),
            "invalid_level_price": _fmt_px(walls.get("invalid_level_price"), d),
            "vwap": _fmt(ctx.get("vwap"), d), "ema20": _fmt(ctx.get("ema20_baseline"), d),
            "ema50": _fmt(ctx.get("ema50"), d), "poc": _fmt(ctx.get("poc"), d), "vah": _fmt(ctx.get("vah"), d),
            "val": _fmt(ctx.get("val"), d), "last_o": _fmt(sc.get("open"), d), "last_h": _fmt(sc.get("high"), d),
            "last_l": _fmt(sc.get("low"), d), "last_c": _fmt(sc.get("close"), d), "rvol_last": ctx.get("rvol_last"),
            "m5_c3": _fmt(m5["m5_c3"], d), "m5_c2": _fmt(m5["m5_c2"], d), "m5_c1": _fmt(m5["m5_c1"], d),
            "m5_o": _fmt(m5["m5_o"], d), "m5_h": _fmt(m5["m5_h"], d), "m5_l": _fmt(m5["m5_l"], d),
            "cvd_label": ctx.get("cvd_label"), "cvd_slope": ctx.get("cvd_slope"),
            "gex_summary": gex_summary_text(ctx.get("gex"), lambda x: _fmt_px(x, d)),
            "floor_price": _fmt(fl if fl is not None else p["stop"], d),
            "floor_label": p.get("floor_label") or "hard stop",
            "_price": price, "_pnl_atr": round(pnl_atr, 3), "_mfe_atr": round(mfe, 3), "_mae_atr": round(mae, 3),
        }

    async def _exit_check(self, sim: dict, p: dict, snap, now: datetime) -> dict:
        payload, m1 = snap
        ctx = payload.get("context") or {}
        rec = {"t": _iso(now), "sim": sim["sim"], "id": p["id"], "symbol": p["symbol"], "side": _side_word(p["side"]),
               "qty": p["qty"], "entry": p["entry"], "stop": p["stop"], "floor": p.get("floor"),
               "floor_label": p.get("floor_label")}
        age = ctx.get("live_price_age_s")
        if ctx.get("live_price") is None or ctx.get("live_price_stale") or \
                (age is not None and float(age) > self.r["stale_price_seconds"]):
            rec.update({"skipped": "live price stale (> 60 s): model answer ignored, server rules only"})
            self._jsonl("claude_exit_checks.jsonl", rec, now)
            return rec
        vals = self.exit_values(p, ctx, m1, now)
        price = vals.pop("_price")
        rec.update({"live_price": price, "phase": vals["phase"], "pnl_atr": vals.pop("_pnl_atr"),
                    "mfe_atr": vals.pop("_mfe_atr"), "mae_atr": vals.pop("_mae_atr"),
                    "minutes": float(vals["minutes"]),
                    "m15_bars_closed_since_entry": vals.get("m15_bars_closed_since_entry")})
        user = CC.render_exit_user(vals, self.prompts)
        m5_ok = False
        q = dict(payload)
        if self.cc.get("m5_chart", True):
            m5p = build_m5_exit_payload(payload, m1, now)
            if m5p is not None:
                q = m5p
                m5_ok = True
        if not m5_ok:
            user = "CHART NOTE: M5 render unavailable; the image is M15.\n" + user
        system = CC.exit_system_prompt(m5_ok, self.prompts)
        rec["m5_chart"] = m5_ok
        png = None
        try:
            q["page_key"] = f"cx-m5-{sim['sim']}-{p['symbol']}" if m5_ok else f"cx-{sim['sim']}-{p['symbol']}"
            q["entry_line"] = {"price": p["entry"], "color": "#FFFFFF", "title": f"ENTRY {_side_word(p['side'])}"}
            q["position_header"] = (f"POSITION {_side_word(p['side'])} @ {_fmt(p['entry'], int(ctx.get('digits') or 5))}"
                                    f"  |  now {rec['pnl_atr']:+.2f} ATR  |  best {rec['mfe_atr']:.2f} ATR")
            png = next(iter((await self._render([q])).values()), None)
        except Exception as e:
            rec["render_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        r = await self._exit_api(self.cc, system, user, png)
        self._log_call(sim["sim"], "exit", self.cc["exit_model"], r, now, id=p["id"], symbol=p["symbol"])
        p["checks"] = int(p.get("checks") or 0) + 1
        dec = r.get("parsed") if r.get("ok") else None
        rec.update({"ok": bool(dec), "latency_ms": r.get("latency_ms"), "cost_usd": r.get("cost_usd"),
                    "thinking_tokens": r.get("thinking_tokens"), "error": r.get("error"), "image": png is not None})
        if not dec or dec.get("decision") not in ("HOLD", "CLOSE"):
            p["exit_fail"] = int(p.get("exit_fail") or 0) + 1
            if p["exit_fail"] >= int(self.r["exit_fail_limit"]) and not p.get("server_only"):
                p["server_only"] = True
                self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": "exit_model_down",
                                                    "id": p["id"], "fails": p["exit_fail"]}, now)
            rec.update({"decision": None, "server_only": p.get("server_only")})
            self._jsonl("claude_exit_checks.jsonl", rec, now)
            return rec
        if p.get("server_only"):
            self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": "exit_model_recovered",
                                                "id": p["id"]}, now)
        p["exit_fail"], p["server_only"] = 0, False
        conf = dec.get("confidence")
        try:
            conf = min(1.0, max(0.0, float(conf)))
        except (TypeError, ValueError):
            conf = None
        rec.update({"decision": dec["decision"], "structure": dec.get("structure"), "reason": dec.get("reason"),
                    "confidence": conf})
        self.eng["probes"].append({"sim": sim["sim"], "id": p["id"], "symbol": p["symbol"], "side": p["side"],
                                   "price": price, "atr": p["atr"], "t": _iso(now), "confidence": conf,
                                   "decision": dec["decision"], "phase": rec["phase"]})
        if dec["decision"] == "CLOSE":
            if rec["phase"] == "A":
                rec["ignored"] = "close_ignored_phase_a"
                self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"],
                                                    "event": "close_ignored_phase_a",
                                                    "id": p["id"], "symbol": p["symbol"],
                                                    "entry_time": p["entry_time"],
                                                    "exit_reason_text": dec.get("reason"),
                                                    "exit_structure": dec.get("structure"),
                                                    "exit_confidence": conf, "exit_phase": rec["phase"]}, now)
            elif in_exit_grace(p["entry_time"], now):
                rec["ignored"] = "close_ignored_grace"
                rec["grace_until"] = _iso(next_m15_close(_dt(p["entry_time"])))
                self._jsonl("claude_events.jsonl", {"t": _iso(now), "sim": sim["sim"], "event": "close_ignored_grace",
                                                    "id": p["id"], "symbol": p["symbol"],
                                                    "entry_time": p["entry_time"], "grace_until": rec["grace_until"],
                                                    "exit_reason_text": dec.get("reason"),
                                                    "exit_structure": dec.get("structure"),
                                                    "exit_confidence": conf, "exit_phase": rec["phase"]}, now)
            else:
                sp = self.spec(p["contract"])
                px = round_tick(price - p["side"] * int(self.r["slippage_ticks"]) * float(sp["tick"]), float(sp["tick"]))
                self._close_real(sim, p, px, now, "claude_close",
                                 extra={"exit_reason_text": dec.get("reason"), "exit_structure": dec.get("structure"),
                                        "exit_confidence": conf, "exit_phase": rec["phase"]})
                rec["closed"] = True
        self._jsonl("claude_exit_checks.jsonl", rec, now)
        return rec

    def _resolve_probes(self, m1s: dict, now: datetime) -> None:
        """Exit calibration: after each answer, does price move 0.25 ATR FOR the trade before 0.25 AGAINST?"""
        keep = []
        for pr in self.eng.get("probes") or []:
            df = m1s.get(pr["symbol"])
            t0 = _dt(pr["t"])
            res = None
            if df is not None and len(df):
                a = 0.25 * float(pr["atr"])
                up, dn = pr["price"] + a, pr["price"] - a
                for ts, row in df[df.index >= pd.Timestamp(t0).floor("min")].iterrows():
                    hu, hd = float(row["high"]) >= up, float(row["low"]) <= dn
                    if hu or hd:
                        if hu and hd:
                            res = "ambiguous"
                        else:
                            res = "for" if (hu and pr["side"] > 0) or (hd and pr["side"] < 0) else "against"
                        break
            if res is None and (now - t0) > timedelta(minutes=90):
                res = "expired"
            if res:
                self._jsonl("claude_calibration.jsonl", {**pr, "kind": "exit", "outcome": res,
                                                         "resolved_at": _iso(now)}, now)
            else:
                keep.append(pr)
        self.eng["probes"] = keep[-2000:]

    async def _persist(self, now: datetime) -> None:
        async with self.io_lock:
            self._save()

    # ---------------- reporting / site
    def _read_logs(self, name: str) -> list[dict]:
        since = self.eng.get("created_at")
        rows = []
        for f in sorted(glob.glob(str(self.logs_dir / "*" / name))):
            try:
                with open(f) as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                rows.append(json.loads(line))
                            except Exception:
                                pass
            except Exception:
                pass
        key = "t_exit" if name == "claude_trades.jsonl" else "t"
        out = [r for r in rows if not since or (r.get(key) or "") >= since]
        if name == "claude_trades.jsonl":
            for r in out:
                r.setdefault("exit_prompt_version", CLAUDE_EXIT_PROMPT_VERSION_PRE)
        return out

    @staticmethod
    def _agg(rows: list[dict]) -> dict:
        n = len(rows)
        net = round(sum(float(r.get("net") or 0) for r in rows), 2)
        rr = [float(r["R"]) for r in rows if r.get("R") is not None]
        return {"n": n, "wins": sum(1 for r in rows if float(r.get("net") or 0) > 0),
                "losses": sum(1 for r in rows if float(r.get("net") or 0) < 0), "net": net,
                "r_total": round(sum(rr), 3), "r_avg": round(sum(rr) / len(rr), 3) if rr else None}

    def build_report(self) -> dict:
        trades = self._read_logs("claude_trades.jsonl")
        calls = self._read_logs("claude_calls.jsonl")
        calib = self._read_logs("claude_calibration.jsonl")
        signals = self._read_logs("claude_signals.jsonl")
        rep = {"generated_at": _iso(datetime.now(UTC)), "since": self.eng.get("created_at"), "sims": {}}
        for sid in self.sims:
            st = {}
            for book in ("real", "mech", "whatif", "twin"):
                rows = [r for r in trades if r.get("sim") == sid and r.get("book") == book]
                b = self._agg(rows)
                for dim in ("symbol", "side", "setup_type", "session", "reason"):
                    groups = {}
                    for r in rows:
                        groups.setdefault(str(r.get(dim)), []).append(r)
                    b[f"by_{dim}"] = {k: self._agg(v) for k, v in sorted(groups.items())}
                if book == "whatif":
                    gated = [r for r in rows if r.get("gated")]
                    b["gated_only"] = self._agg(gated)
                post = [r for r in rows if r.get("exit_prompt_version") == CLAUDE_EXIT_PROMPT_VERSION]
                b["since_fix"] = self._agg(post)
                st[book] = b
            sg = [s for s in signals if s.get("sim") == sid]
            st["signals"] = {"rows": len(sg), "buy_sell": sum(1 for s in sg if s.get("action") in ("BUY", "SELL")),
                             "gated": sum(1 for s in sg if s.get("gated")),
                             "opened": sum(1 for s in sg if s.get("decision") == "OPENED")}
            st["whatif_open"] = sum(1 for t in self.eng["shadow"] if t["sim"] == sid and t["book"] == "whatif")
            # entry calibration: confidence bucket vs reached +0.5 ATR before the stop (what-if book)
            buckets = {"0.5-0.6": [], "0.6-0.7": [], "0.7+": [], "<0.5": []}
            for r in (x for x in trades if x.get("sim") == sid and x.get("book") == "whatif"):
                c = r.get("confidence")
                if c is None:
                    continue
                k = "0.7+" if c >= 0.7 else "0.6-0.7" if c >= 0.6 else "0.5-0.6" if c >= 0.5 else "<0.5"
                buckets[k].append(1 if r.get("reach_05_before_stop") else 0)
            st["entry_calibration"] = {k: {"n": len(v), "hit_rate": round(sum(v) / len(v), 3) if v else None}
                                       for k, v in buckets.items()}
            eb = {}
            for c in (x for x in calib if x.get("sim") == sid and x.get("kind") == "exit"
                      and x.get("outcome") in ("for", "against") and x.get("confidence") is not None):
                k = f"{min(9, int(float(c['confidence']) * 10)) / 10:.1f}"
                eb.setdefault(k, []).append(1 if c["outcome"] == "for" else 0)
            st["exit_calibration"] = {k: {"n": len(v), "for_rate": round(sum(v) / len(v), 3)}
                                      for k, v in sorted(eb.items())}
            ops = {}
            for kind in ("entry", "exit"):
                cs = [c for c in calls if c.get("sim") == sid and c.get("kind") == kind]
                lat = [c.get("latency_ms") for c in cs if c.get("ok")]
                days = {}
                for c in cs:
                    dd = days.setdefault(str(c.get("t", ""))[:10], {"calls": 0, "cost_usd": 0.0, "input_tokens": 0,
                                                                     "output_tokens": 0, "thinking_tokens": 0})
                    dd["calls"] += 1
                    dd["cost_usd"] = round(dd["cost_usd"] + float(c.get("cost_usd") or 0), 4)
                    for k in ("input_tokens", "output_tokens", "thinking_tokens"):
                        dd[k] += int(c.get(k) or 0)
                ops[kind] = {"calls": len(cs), "failed": sum(1 for c in cs if not c.get("ok")),
                             "latency_p50_ms": pctl(lat, 0.5), "latency_p95_ms": pctl(lat, 0.95),
                             "cost_usd": round(sum(float(c.get("cost_usd") or 0) for c in cs), 4),
                             "by_utc_day": days}
            st["ops"] = ops
            rep["sims"][sid] = st
        return rep

    def site_payload(self, report: dict | None = None) -> dict:
        now = datetime.now(UTC)
        key = self._key()
        sims = []
        for sid, s in self.sims.items():
            lp = s.get("last_price") or {}
            eq = self.equity(s)
            pos = []
            for p in s["positions"]:
                px = lp.get(p["symbol"], p["entry"])
                mfe = float(p.get("mfe_pts") or 0) / float(p["atr"])
                pos.append({"symbol": p["symbol"], "contract": p["contract"], "qty": p["qty"],
                            "side": _side_word(p["side"]), "entry_time": p["entry_time"], "entry": p["entry"],
                            "stop": p["init_stop"], "floor": p.get("floor"), "floor_label": p.get("floor_label"),
                            "last": px, "unrealized": round(self.pnl(p, px), 2), "risk_usd": p.get("risk_usd"),
                            "phase": phase_of(mfe, self.r), "setup_type": p.get("setup_type"),
                            "score": p.get("score"), "server_only": p.get("server_only"),
                            "target": p.get("target") or p.get("take_profit"), "stop_live": p.get("stop")})
            recent = []
            for r in (s.get("recent") or [])[: int(self.cc.get("recent_trades", 15))]:
                recent.append({**r, "held": held_str(r.get("hold_seconds"))})
            rs = (report or {}).get("sims", {}).get(sid, {})
            books = {}
            for b in ("mech", "whatif", "twin"):
                x = rs.get(b) or {}
                books[b] = {k: x.get(k) for k in ("n", "wins", "losses", "net", "r_total", "r_avg")}
            books["whatif_gated"] = {k: (rs.get("whatif") or {}).get("gated_only", {}).get(k)
                                     for k in ("n", "r_total", "r_avg")}
            le = (self.last_entry.get("sims") or {}).get(sid) or {}
            sims.append({
                "id": sid, "label": s["label"], "entry_model": s["entry_model"], "exit_model": s["exit_model"],
                "account": {"start_balance": s["start_balance"], "balance": round(s["balance"], 2),
                            "equity": round(eq, 2), "realized_pnl": round(s["realized_pnl"], 2),
                            "open_pnl": round(eq - s["balance"], 2), "trades": s["trades"], "wins": s["wins"],
                            "losses": s["losses"], "r_total": s["r_total"],
                            "r_per_trade": round(s["r_total"] / s["trades"], 3) if s["trades"] else None,
                            "mll_floor": s["mll_floor"], "mll_room": round(eq - s["mll_floor"], 2),
                            "day_realized": s["day"]["realized"], "day_flattened": s["day"]["flattened"],
                            "blown": s.get("blown")},
                "open_positions": pos, "recent_trades": recent, "books": books,
                "signals": (rs.get("signals") or {}), "ops": rs.get("ops"),
                "since_fix": (rs.get("real") or {}).get("since_fix") or {"n": 0, "wins": 0, "losses": 0,
                                                                         "net": 0.0, "r_total": 0.0, "r_avg": None},
                "exit_prompt_version": CLAUDE_EXIT_PROMPT_VERSION,
                "last_entry_call": {"bar": self.last_entry.get("bar"), "t": self.last_entry.get("t"), **le}})
        return {"updated_at": _iso(now), "paper_only": True, "key_present": key,
                "status": "running" if key else "waiting for key",
                "exit_model": self.cc["exit_model"], "entry_models": self.cc["entry_models"],
                "claude_exit_calls_enabled": claude_exit_calls_enabled(self.cfg) and not uniform_exits_enabled(self.cfg),
                "uniform_1atr_exits": uniform_exits_enabled(self.cfg),
                "exit_calls_note": (UNIFORM_BANNER if uniform_exits_enabled(self.cfg)
                                    else ("Haiku exit calls paused — mechanical exits"
                                          if not claude_exit_calls_enabled(self.cfg) else None)),
                "exit_mode": close_tag(self.cfg) if uniform_exits_enabled(self.cfg) else None,
                "last_entry_bar": self.eng.get("last_entry_bar"),
                "last_entry_skipped": self.last_entry.get("skipped"),
                "rules": ("Binding server rules: hard stop = model stop clamped 0.6-1.5 ATR; $900 risk sizing "
                          "(NQ 1, GC 1, 6E/6B up to 4, 6 max); break-even at +0.5 ATR; profit lock 50% of best at "
                          "+1.0 ATR, 60% at +1.5 ATR; 45-min time stop; daily -$1,500; trailing $300 buffer; "
                          "re-entry lock; USD-group cap (one open per long-USD / short-USD group; 30-min lock "
                          "after a stop-out); skip fill if the live quote is >0.3 ATR worse than the chart; "
                          "one entry per M15 close +20s (score >= 5). Phase A (never +0.25 ATR): Haiku CLOSE is "
                          "ignored; server exits on a closed M15 after entry beyond invalidation or wrong-side "
                          "of both VWAP and EMA20. Phases B/C ignore a CLOSE until the first M15 close after "
                          "entry. Exit confidence is logged, never acted on. Exit model every 18 s."),
                "reset": {"at": self.eng.get("created_at"), "token": self.cc["reset_token"]},
                "sims": sims}

    async def publish(self, now: datetime | None = None, report: bool = True) -> None:
        try:
            rep = None
            if report:
                rep = await asyncio.to_thread(self.build_report)
                self._report_cache = rep
            else:
                rep = getattr(self, "_report_cache", None)
                if rep is None:
                    rep = await asyncio.to_thread(self.build_report)
                    self._report_cache = rep
            site = self.site_payload(rep)
            async with self.io_lock:
                _atomic_write(self.root / "report.json", rep)
                _atomic_write(self.site_dir / "claude.json", site)
        except Exception:
            log.exception("claude publish failed")




def _unix_ts(ts) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return int(t.timestamp())


def build_m5_exit_payload(payload_m15: dict, m1: pd.DataFrame | None, now: datetime, n: int = 80) -> dict | None:
    """Resample live M1 into an M5 chart (closed bars + paler forming bar), same style as the M15 entry chart.

    VWAP / EMA20 (mid) / EMA50 are drawn as M15 context levels (the prompt legend is "M15 EMA20 / EMA50").
    S/R and GEX levels are copied from the M15 payload. Returns None if M1 cannot form a chart.
    """
    if m1 is None or not len(m1):
        return None
    df = m1.sort_index()
    if "volume" not in df.columns:
        df = df.copy()
        df["volume"] = 0.0
    agg = df.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"])
    if not len(agg):
        return None
    nt = pd.Timestamp(now)
    if nt.tzinfo is None:
        nt = nt.tz_localize("UTC")
    closed = agg[agg.index + pd.Timedelta(minutes=5) <= nt]
    forming = agg[agg.index + pd.Timedelta(minutes=5) > nt]
    view = closed.tail(n)
    digits = int(payload_m15.get("digits") or (payload_m15.get("context") or {}).get("digits") or 2)

    def rnd(x):
        return None if pd.isna(x) else round(float(x), digits + 1)

    candles, volume = [], []
    for ts, row in view.iterrows():
        ti = _unix_ts(ts)
        o, h, l, c = rnd(row["open"]), rnd(row["high"]), rnd(row["low"]), rnd(row["close"])
        candles.append({"time": ti, "open": o, "high": h, "low": l, "close": c})
        volume.append({"time": ti, "value": float(row["volume"] or 0),
                       "up": bool(float(row["close"]) >= float(row["open"])), "rvol": None})
    forming_candle = None
    if len(forming):
        f = forming.iloc[-1]
        forming_candle = {"time": _unix_ts(forming.index[-1]), "open": rnd(f["open"]), "high": rnd(f["high"]),
                          "low": rnd(f["low"]), "close": rnd(f["close"])}
    if not candles and forming_candle is None:
        return None
    ctx = payload_m15.get("context") or {}
    times = [c["time"] for c in candles]
    if forming_candle is not None:
        times = times + [forming_candle["time"]]

    def const_line(val):
        if val is None or not times:
            return []
        v = float(val)
        return [{"time": t, "value": v} for t in times]

    out = dict(payload_m15)
    out["timeframe"] = "M5"
    out["candles"] = candles
    out["volume"] = volume
    out["forming_candle"] = forming_candle
    out["visible"] = min(int(payload_m15.get("visible") or 60), max(len(candles) + (1 if forming_candle else 0), 1))
    out["vwap"] = const_line(ctx.get("vwap"))
    out["mid"] = const_line(ctx.get("ema20_baseline") if ctx.get("ema20_baseline") is not None else ctx.get("ema20"))
    out["ema50"] = const_line(ctx.get("ema50"))
    out["upper"] = []
    out["lower"] = []
    return out


async def run_claude_exit_loop(eng: ClaudeEngine, stop: asyncio.Event | None = None) -> None:
    """18 s cadence like the Gemini exit loop; idle ticks are an in-memory check. Never raises."""
    period = float(eng.cc.get("check_seconds", 18))
    nxt = time.monotonic()
    while not (stop and stop.is_set()):
        try:
            await eng.exit_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("claude exit tick failed (Claude sims only; Gemini unaffected)")
        nxt += period
        now = time.monotonic()
        if nxt < now:
            nxt += math.ceil((now - nxt) / period) * period
        await asyncio.sleep(nxt - now)

async def run_claude_scan_loop(eng: ClaudeEngine, stop: asyncio.Event | None = None) -> None:
    """Own task: Claude entry calls at M15 close +20s. Isolated from the Gemini cycle so it cannot delay it.

    schedule_entry remains as a same-bar fallback if this scan is missed; on_cycle dedupes on last_entry_bar.
    """
    from .pipeline import next_boundary
    offset = int(eng.cc.get("scan_offset_seconds", 20))
    while not (stop and stop.is_set()):
        wake = next_boundary(datetime.now(UTC), 15, offset)
        await asyncio.sleep(max(0.0, (wake - datetime.now(UTC)).total_seconds()))
        if stop and stop.is_set():
            return
        try:
            now = datetime.now(UTC)
            payloads = await eng.fetch_entry_payloads(now)
            if payloads:
                r = await eng.on_cycle(payloads, now=now)
                log.info("claude scan: %s", {k: r.get(k) for k in ("bar", "t", "skipped", "error", "render_ms")
                                             if isinstance(r, dict) and k in r})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("claude scan loop iteration failed (Gemini loop unaffected)")

