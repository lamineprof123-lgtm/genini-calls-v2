"""Shared M15-close +20s snapshot + fan-out (t251u). PAPER ONLY.

Every M15-close model call (Claude C-OPUS / C-SONNET, C-OPUS-B, Carbon, DS-FLASH)
fires on this scheduler, not the 2-min Gemini flash loop. Charts and facts are
rendered ONCE; the same chart bytes (and a snapshot_id / sha256) are sent to every
model. Mid-bar Carbon triggered checks are out of scope and keep their own loop.
"""
from __future__ import annotations
import asyncio, hashlib, json, logging, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .pipeline import next_boundary
from .risk import _atomic_write
from . import bracket_v1 as B

log = logging.getLogger("signals.m15_scan")
UTC = timezone.utc
DUBAI = ZoneInfo("Asia/Dubai")

# Union of symbols any M15-close model may need. Individual books slice this.
ALL_SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "US100", "XAGUSD", "BTCUSD"]
CLAUDE_V2_SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "US100", "XAGUSD", "BTCUSD"]
OFFSET_SECONDS = 20


def bar_close_ts(now: datetime) -> datetime:
    """The M15 close that has just occurred at `now` (left-aligned bars: close == floor)."""
    step = 900
    ts = int(now.timestamp()) // step * step
    return datetime.fromtimestamp(ts, UTC)


def iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat()


def snapshot_hash(payloads: list[dict], charts: dict[str, bytes], facts: dict) -> tuple[str, str, dict]:
    """Hash of chart bytes + facts + last_closed_bar, independent of prompt wrapping."""
    acc = hashlib.sha256()
    acc.update(b"m15-snapshot-v1\n")
    chart_digests = {}
    for p in payloads:
        sym = p["symbol"]
        ctx = p.get("context") or {}
        acc.update(f"SYM:{sym}:{ctx.get('last_closed_bar_utc')}\n".encode())
        blob = charts.get(sym)
        if blob:
            d = hashlib.sha256(blob).hexdigest()
            chart_digests[sym] = d
            acc.update(f"IMG:{sym}:{d}:{len(blob)}\n".encode())
        f = facts.get(sym) or {}
        # drop any leftover bytes; facts are json-safe
        acc.update(json.dumps(f, sort_keys=True, default=str).encode())
        acc.update(b"\n")
    digest = acc.hexdigest()
    bar = ""
    if payloads:
        bar = str((payloads[0].get("context") or {}).get("last_closed_bar_utc") or "")[:19]
        bar = bar.replace(":", "").replace("-", "").replace("T", "")
    sid = f"m15-{bar}-{digest[:12]}" if bar else f"m15-{digest[:16]}"
    return digest, sid, chart_digests


class M15Snapshot:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def slice(self, symbols: list[str]) -> list[dict]:
        want = set(symbols)
        return [p for p in self.payloads if p.get("symbol") in want]

    def charts_for(self, symbols: list[str]) -> dict[str, bytes]:
        return {s: self.charts[s] for s in symbols if s in self.charts}


class M15Coordinator:
    """Owned by GhaLoop. Renders once per M15 close +20s and fans out to every close-bar model."""

    def __init__(self, cfg: dict, *, io_lock: asyncio.Lock | None = None,
                 claude=None, carbon=None, deepseek=None, opus_b=None, scorer=None, glow80=None, eval80=None,
                 snapshot=None, render=None, h1=None,
                 state_dir=None, logs_dir=None, site_dir=None):
        self.cfg = cfg
        self.cc = (cfg.get("m15_scan") or {})
        self.io_lock = io_lock or asyncio.Lock()
        self.claude = claude
        self.carbon = carbon
        self.deepseek = deepseek
        self.opus_b = opus_b
        self.glow80 = glow80
        self.eval80 = eval80
        self.scorer = scorer
        self._snapshot = snapshot
        self._render = render
        self._h1 = h1
        root = Path(state_dir or cfg["general"]["state_dir"])
        self.state_dir = Path(root) / "m15_scan"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or (cfg.get("site") or {}).get("dir") or root)
        self.last: dict = {}
        self.task: asyncio.Task | None = None
        # When set, Carbon / Claude own scan loops skip their independent fetch+render.
        if self.claude is not None:
            self.claude.shared_m15 = True
        if self.carbon is not None:
            self.carbon.shared_m15 = True
        if self.deepseek is not None:
            self.deepseek.shared_m15 = True

    def offset(self) -> int:
        return int(self.cc.get("offset_seconds", OFFSET_SECONDS))

    def symbols(self) -> list[str]:
        return list(self.cc.get("symbols") or ALL_SYMBOLS)

    async def _fetch_payloads(self, now: datetime) -> list[dict]:
        from .exit_manager import market_snapshot
        snap_fn = self._snapshot or (lambda s, cfg, t: market_snapshot(s, cfg, t))
        syms = self.symbols()
        got = await asyncio.gather(*[snap_fn(s, self.cfg, now) for s in syms], return_exceptions=True)
        out = []
        for s, r in zip(syms, got):
            if isinstance(r, BaseException):
                log.warning("m15 snapshot %s failed: %s", s, r)
                continue
            # market_snapshot returns (payload, m1) or similar
            p = r[0] if isinstance(r, (tuple, list)) and r else r
            if p:
                out.append(p)
        return out

    async def _render_charts(self, payloads: list[dict]) -> dict[str, bytes]:
        render = self._render
        if render is None:
            from .renderer import render_all
            render = render_all
        tagged = [{**p, "page_key": f"m15-{p['symbol']}"} for p in payloads]
        return await render(tagged) or {}

    async def capture(self, now: datetime | None = None) -> M15Snapshot:
        now = now or datetime.now(UTC)
        close = bar_close_ts(now)
        t0 = time.monotonic()
        payloads = await self._fetch_payloads(now)
        snap_ts = datetime.now(UTC)
        charts = await self._render_charts(payloads) if payloads else {}
        render_ms = round((time.monotonic() - t0) * 1000)
        facts = {p["symbol"]: B.compute_facts(p) for p in payloads}
        digest, sid, chart_digests = snapshot_hash(payloads, charts, facts)
        # H1 context once, shared (used by Claude v2 and the bracket package).
        htf = {}
        if self.claude is not None and hasattr(self.claude, "_htf"):
            try:
                syms = [p["symbol"] for p in payloads]
                vals = await asyncio.gather(*[self.claude._htf(s, now) for s in syms],
                                            return_exceptions=True)
                for s, v in zip(syms, vals):
                    if not isinstance(v, BaseException):
                        htf[s] = v
            except Exception:
                log.exception("m15 h1 context failed")
        pkg = B.build_package(
            [p for p in payloads if p["symbol"] in B.SYMBOLS],
            charts, htf=htf, bar_close_ts=iso(close), snapshot_ts=iso(snap_ts),
        )
        # Re-key the bracket package hash onto the shared snapshot so both logs agree.
        # The package already has its own hash of the exact bytes+text the two books see.
        snap = M15Snapshot(
            snapshot_id=sid, snapshot_hash=digest, package_id=pkg["snapshot_id"],
            package_hash=pkg["snapshot_hash"],
            bar_close_ts=close, snapshot_ts=snap_ts, now=now,
            payloads=payloads, charts=charts, facts=facts, htf=htf,
            chart_sha256=chart_digests, package=pkg, render_ms=render_ms,
            start_lag_s=round((snap_ts - close).total_seconds(), 3),
        )
        self._log_snapshot(snap)
        self.last = {"snapshot_id": sid, "snapshot_hash": digest, "package_id": pkg["snapshot_id"],
                     "bar_close_ts": iso(close), "snapshot_ts": iso(snap_ts),
                     "start_lag_s": snap.start_lag_s, "render_ms": render_ms,
                     "symbols": [p["symbol"] for p in payloads],
                     "chart_sha256": chart_digests}
        log.info("m15 snapshot %s lag=%.3fs render=%dms symbols=%s hash=%s",
                 sid, snap.start_lag_s, render_ms, [p["symbol"] for p in payloads], digest[:16])
        return snap

    def _log_snapshot(self, snap: M15Snapshot) -> None:
        day = Path(self.logs_dir) / snap.snapshot_ts.astimezone(DUBAI).strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        rec = {"t": iso(snap.snapshot_ts), "snapshot_id": snap.snapshot_id,
               "snapshot_hash": snap.snapshot_hash, "package_id": snap.package_id,
               "package_hash": snap.package_hash, "bar_close_ts": iso(snap.bar_close_ts),
               "snapshot_ts": iso(snap.snapshot_ts), "start_lag_s": snap.start_lag_s,
               "render_ms": snap.render_ms, "symbols": [p["symbol"] for p in snap.payloads],
               "chart_sha256": snap.chart_sha256,
               "facts": {s: {k: v for k, v in f.items() if k != "level_events"} | {
                   "level_events": f.get("level_events")} for s, f in snap.facts.items()}}
        with open(day / "m15_snapshots.jsonl", "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def log_call(self, *, book: str, model: str, snap: M15Snapshot, call_start: datetime,
                 answer_ts: datetime | None, fill_ts: datetime | None, ok: bool,
                 latency_s=None, cost_usd=None, thinking_tokens=None, error=None,
                 extra: dict | None = None) -> None:
        day = Path(self.logs_dir) / call_start.astimezone(DUBAI).strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        rec = {"book": book, "model": model, "snapshot_id": snap.snapshot_id,
               "snapshot_hash": snap.snapshot_hash, "package_id": getattr(snap, "package_id", None),
               "bar_close_ts": iso(snap.bar_close_ts), "snapshot_ts": iso(snap.snapshot_ts),
               "call_start": iso(call_start), "answer_ts": iso(answer_ts), "fill_ts": iso(fill_ts),
               "start_lag_s": round((call_start - snap.bar_close_ts).total_seconds(), 3),
               "ok": ok, "latency_s": latency_s, "cost_usd": cost_usd,
               "thinking_tokens": thinking_tokens, "error": error, **(extra or {})}
        with open(day / "m15_calls.jsonl", "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def _log_claude_calls(self, r: dict | None, snap: M15Snapshot, call_start: datetime) -> None:
        """One m15_calls.jsonl row per Claude v2 book (C-OPUS / C-SONNET) with timings. Never raises."""
        try:
            r = r or {}
            if r.get("skipped"):
                self.log_call(book="C-OPUS/C-SONNET", model="claude-v2", snap=snap, call_start=call_start,
                              answer_ts=datetime.now(UTC), fill_ts=None, ok=False,
                              error=None, extra={"skipped": r.get("skipped")})
                return
            for sid, v in (r.get("sims") or {}).items():
                lat = v.get("latency_ms")
                self.log_call(book=sid, model=v.get("model") or "claude", snap=snap, call_start=call_start,
                              answer_ts=_dt(v.get("answer_ts")) or datetime.now(UTC), fill_ts=None,
                              ok=bool(v.get("ok")), latency_s=round(lat / 1000, 3) if lat else None,
                              cost_usd=v.get("cost_usd"), thinking_tokens=v.get("thinking_tokens"),
                              error=v.get("error"),
                              extra={"stop_reason": v.get("stop_reason"),
                                     "symbols": [x.get("symbol") for x in v.get("signals") or []]})
        except Exception:
            log.exception("m15_calls claude log failed")

    def _log_carbon_call(self, r: dict | None, snap: M15Snapshot, call_start: datetime) -> None:
        """One m15_calls.jsonl row for the Carbon M15 entry scan (gemini-4-carbon). Never raises."""
        try:
            r = r or {}
            model = (getattr(self.carbon, "cc", None) or {}).get("model") or "gemini-4-carbon"
            tok = r.get("tokens") or {}
            self.log_call(book="CARBON", model=model, snap=snap, call_start=call_start,
                          answer_ts=_dt(r.get("answer_ts")) or datetime.now(UTC), fill_ts=None,
                          ok=bool(r.get("ok")), latency_s=r.get("latency_s"), cost_usd=r.get("cost_usd"),
                          thinking_tokens=tok.get("thoughts"), error=r.get("error"),
                          extra={"skipped": r.get("skipped"), "finish_reason": r.get("finish_reason"),
                                 "thinking_level": r.get("thinking_level"),
                                 "output_tokens": tok.get("output"), "symbols": r.get("sent")})
        except Exception:
            log.exception("m15_calls carbon log failed")

    def enqueue_signals(self, signals: list[dict]) -> None:
        sc = self.scorer
        if sc is None:
            return
        try:
            for s in signals or []:
                sc.enqueue(s)
        except Exception:
            log.exception("m15 enqueue to signal scorer failed")

    async def dispatch(self, snap: M15Snapshot) -> dict:
        """Fan out to every M15-close model in parallel. Never raises out."""
        tasks = {}
        if self.claude is not None:
            tasks["claude"] = asyncio.create_task(self._run_claude(snap), name="m15-claude")
        if self.carbon is not None:
            tasks["carbon"] = asyncio.create_task(self._run_carbon(snap), name="m15-carbon")
        if self.opus_b is not None or self.deepseek is not None:
            tasks["bracket"] = asyncio.create_task(self._run_bracket(snap), name="m15-bracket")
        if self.glow80 is not None:
            tasks["glow80"] = asyncio.create_task(self._run_glow80(snap), name="m15-glow80")
        out = {}
        if tasks:
            done = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for k, r in zip(tasks, done):
                if isinstance(r, BaseException):
                    log.exception("m15 dispatch %s failed", k, exc_info=r)
                    out[k] = {"error": f"{type(r).__name__}: {r}"}
                else:
                    out[k] = r
        return out

    async def _run_claude(self, snap: M15Snapshot) -> dict:
        """C-OPUS / C-SONNET: same shared charts, their own v2 prompt and 4-symbol set."""
        try:
            payloads = snap.slice(CLAUDE_V2_SYMBOLS)
            if not payloads:
                return {"skipped": "no_payloads"}
            pngs = snap.charts_for([p["symbol"] for p in payloads])
            fn = getattr(self.claude, "on_shared_snapshot", None)
            if fn is None:
                return {"skipped": "no on_shared_snapshot"}
            call_start = datetime.now(UTC)
            r = await fn(payloads, pngs, snap, now=snap.now)
            self._log_claude_calls(r, snap, call_start)
            return r
        except Exception as e:
            log.exception("m15 claude fan-out failed (other models unaffected)")
            return {"error": f"{type(e).__name__}: {e}"}

    async def _run_carbon(self, snap: M15Snapshot) -> dict:
        try:
            if not getattr(self.carbon, "entries_allowed", lambda _n: True)(snap.now):
                return {"skipped": "outside entry window / session"}
            syms = list((getattr(self.carbon, "cc", None) or {}).get("symbols") or [])
            payloads = snap.slice(syms or list(snap.charts))
            pngs = snap.charts_for([p["symbol"] for p in payloads])
            snaps = {p["symbol"]: p for p in payloads}
            call_start = datetime.now(UTC)
            r = await self.carbon.scan(snap.now, kind="entry", symbols=list(snaps),
                                       snaps=snaps, pngs=pngs, snapshot_meta={
                                           "snapshot_id": snap.snapshot_id,
                                           "snapshot_hash": snap.snapshot_hash,
                                           "bar_close_ts": iso(snap.bar_close_ts),
                                           "snapshot_ts": iso(snap.snapshot_ts)})
            self._log_carbon_call(r, snap, call_start)
            return r
        except Exception as e:
            log.exception("m15 carbon fan-out failed (other models unaffected)")
            return {"error": f"{type(e).__name__}: {e}"}

    async def _run_glow80(self, snap: M15Snapshot) -> dict:
        """G-LOW-80 (t272u): its own LOW-thinking Gemini call on the same shared snapshot."""
        call_start = datetime.now(UTC)
        try:
            r = await self.glow80.on_snapshot(snap, now=snap.now)
        except Exception as e:
            log.exception("m15 G-LOW-80 fan-out failed (other models unaffected)")
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            r = r or {}
            self.log_call(book="G-LOW-80", model=r.get("model") or "gemini-3.8-flash", snap=snap,
                          call_start=call_start, answer_ts=_dt(r.get("answer_ts")) or datetime.now(UTC),
                          fill_ts=None, ok=bool(r.get("ok")), latency_s=r.get("latency_s"),
                          cost_usd=r.get("cost_usd"), thinking_tokens=r.get("thinking_tokens"),
                          error=r.get("error"),
                          extra={"skipped": r.get("skipped"), "thinking_level": r.get("thinking_level"),
                                 "model_version": r.get("model_version"), "usage": r.get("usage"),
                                 "symbols": r.get("symbols"), "queued": r.get("queued"),
                                 "signals": r.get("signals")})
        except Exception:
            log.exception("m15_calls G-LOW-80 log failed")
        self.enqueue_signals(r.get("score_signals") or [])
        if self.eval80 is not None:
            r = {**r, "eval80": await self._run_eval80(snap, r)}
        return r

    async def _run_eval80(self, snap: M15Snapshot, g: dict) -> dict:
        """EVAL-80 (t284u): no model call; evaluates G-LOW-80's response for the same snapshot."""
        call_start = datetime.now(UTC)
        try:
            e = await self.eval80.on_shared(g, snap, now=snap.now)
        except Exception as ex:
            log.exception("m15 EVAL-80 failed (other models unaffected)")
            e = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        try:
            e = e or {}
            self.log_call(book="EVAL-80", model=e.get("model") or "gemini-3.8-flash", snap=snap,
                          call_start=call_start, answer_ts=datetime.now(UTC), fill_ts=None,
                          ok=bool(e.get("ok")), latency_s=0.0, cost_usd=0.0, thinking_tokens=None,
                          error=e.get("error"),
                          extra={"shared_from": "G-LOW-80", "skipped": e.get("skipped"),
                                 "thinking_level": e.get("thinking_level"), "queued": e.get("queued"),
                                 "signals": e.get("signals"), "eval_status": e.get("eval_status")})
        except Exception:
            log.exception("m15_calls EVAL-80 log failed")
        self.enqueue_signals(e.get("score_signals") or [])
        return {k: e.get(k) for k in ("ok", "skipped", "error", "queued", "eval_status")}

    async def _run_bracket(self, snap: M15Snapshot) -> dict:
        """C-OPUS-B and DS-FLASH get the identical bracket-v1 package (same bytes)."""
        pkg = snap.package
        out = {}
        coros = []
        names = []
        if self.opus_b is not None:
            names.append("C-OPUS-B")
            coros.append(self._call_book(self.opus_b, "C-OPUS-B", pkg, snap))
        if self.deepseek is not None:
            names.append("DS-FLASH")
            coros.append(self._call_book(self.deepseek, "DS-FLASH", pkg, snap))
        if not coros:
            return out
        got = await asyncio.gather(*coros, return_exceptions=True)
        for n, r in zip(names, got):
            out[n] = {"error": f"{type(r).__name__}: {r}"} if isinstance(r, BaseException) else r
        return out

    async def _call_book(self, engine, book: str, pkg: dict, snap: M15Snapshot) -> dict:
        fn = getattr(engine, "on_bracket_package", None)
        if fn is None:
            return {"skipped": "no on_bracket_package"}
        call_start = datetime.now(UTC)
        r = await fn(pkg, now=snap.now, snap_meta={
            "snapshot_id": snap.snapshot_id, "snapshot_hash": snap.snapshot_hash,
            "package_id": pkg["snapshot_id"], "bar_close_ts": iso(snap.bar_close_ts),
            "snapshot_ts": iso(snap.snapshot_ts), "call_start": iso(call_start)})
        answer_ts = _dt((r or {}).get("answer_ts")) or datetime.now(UTC)
        fill_ts = _dt((r or {}).get("fill_ts"))
        model = (r or {}).get("model") or book
        self.log_call(book=book, model=model, snap=snap, call_start=call_start,
                      answer_ts=answer_ts, fill_ts=fill_ts, ok=bool((r or {}).get("ok")),
                      latency_s=(r or {}).get("latency_s"), cost_usd=(r or {}).get("cost_usd"),
                      thinking_tokens=(r or {}).get("thinking_tokens"),
                      error=(r or {}).get("error"),
                      extra={"prompt_version": B.PROMPT_VERSION,
                             "package_hash": pkg["snapshot_hash"],
                             "symbols": pkg.get("symbols")})
        self.enqueue_signals((r or {}).get("score_signals") or [])
        return r

    async def run_once(self, now: datetime | None = None) -> dict:
        snap = await self.capture(now)
        dispatched = await self.dispatch(snap)
        rec = {**self.last, "dispatched": {k: {kk: vv for kk, vv in (v or {}).items()
                                               if kk in ("ok", "skipped", "error", "latency_s", "cost_usd",
                                                         "thinking_tokens", "start_lag_s", "thinking_level", "eval80")}
                                           if isinstance(v, dict) else v
                                           for k, v in dispatched.items()}}
        self.last = rec
        try:
            async with self.io_lock:
                _atomic_write(self.site_dir / "m15_scan.json", {**rec, "updated_at": iso(datetime.now(UTC))})
        except Exception:
            log.exception("m15_scan.json write failed")
        return rec


def _dt(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


async def run_m15_scan_loop(coord: M15Coordinator, stop: asyncio.Event | None = None) -> None:
    """Own scheduler: every M15 close + offset seconds, odd or even minute alike."""
    offset = coord.offset()
    while not (stop and stop.is_set()):
        wake = next_boundary(datetime.now(UTC), 15, offset)
        await asyncio.sleep(max(0.0, (wake - datetime.now(UTC)).total_seconds()))
        if stop and stop.is_set():
            return
        try:
            now = datetime.now(UTC)
            lag = (now - bar_close_ts(now)).total_seconds()
            log.info("m15 scan wake lag=%.3fs (target %ss) at %s", lag, offset, now.isoformat())
            rec = await coord.run_once(now)
            log.info("m15 scan done: %s", json.dumps({k: rec.get(k) for k in
                     ("snapshot_id", "start_lag_s", "render_ms", "symbols")}, default=str))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("m15 scan loop iteration failed (2-min Gemini loop unaffected)")
