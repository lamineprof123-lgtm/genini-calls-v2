"""Long-running signal loop for ONE GitHub Actions job (SIGNALS ONLY - never places orders).

One workflow run owns the loop (see gha_lock.py): warm Chromium pool + in-process bar cache, a cycle every
`interval` minutes aligned to the clock (+align_offset_seconds), and after every cycle:
  site JSON (status/latest_signal/history) -> Pages deploy (in-job, tools/deploy-pages.mjs)
  Lucid paper-sim step (M1 replay since last_processed, so missed time is always caught up)
  commit + push state/ + logs/ + site JSON to the data branch (a killed run loses at most one cycle)
Exits after max_runtime (default 5h30m, under the 6h job cap), handing the loop to a queued successor run
(started by cron or by this loop's own workflow_dispatch), or when a STOP file appears on the data branch.
"""
from __future__ import annotations
import asyncio, contextlib, json, logging, os, shutil, subprocess, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import gha_lock as L
from .config import ROOT
from .exit_manager import ExitManager, em_cfg, run_exit_loop
from .lucid import lucid_replay, lucid_step
from .pipeline import next_boundary, run_once
from .site import write_status

log = logging.getLogger("signals.gha")
SITE_JSON = ("status.json", "latest_signal.json", "history.json", "lucid.json", "claude.json", "bracket.json",
             "carbon.json", "deepseek.json", "signal_scores.json", "opus_b.json", "m15_scan.json", "other_venue.json",
             "panel.json")
DATA_JSON = ("books.json",)  # committed to the data branch only (not deployed to Pages)
CYCLE_LOG_KEEP_DAYS = 14


def _git(data: Path, *args, check=True, timeout=120):
    return subprocess.run(["git", *args], cwd=data, capture_output=True, text=True, check=check, timeout=timeout)


class GhaLoop:
    def __init__(self, cfg: dict, data: Path, run_id: str, role: str, dry_run: bool = False,
                 max_runtime_min: float = 330, hard_cap_min: float = 350, deploy: bool = True,
                 max_cycles: int | None = None, model: str | None = None):
        self.cfg, self.data, self.run_id, self.role = cfg, data, run_id, role
        self.dry_run, self.deploy_enabled, self.max_cycles, self.model = dry_run, deploy, max_cycles, model
        self.interval = int(cfg["scheduler"]["interval_minutes"])
        self.scan_interval = self.interval  # published to status.json (2 active / 15 blocked)
        self.offset = int(cfg["scheduler"]["align_offset_seconds"])
        self.t_start = datetime.now(timezone.utc)
        self.ends_at = self.t_start + timedelta(minutes=max_runtime_min)
        self.hard_cap = self.t_start + timedelta(minutes=hard_cap_min)
        self.main_dir = ROOT.parent  # repo root of the main-branch checkout (index.html, logos)
        self.cycles = 0
        self.dispatched = False
        self.deploy_skip = 0         # adaptive back-off after failed deploys (cycles to skip)
        self.deploy_fail_streak = 0
        self.stats = {"deploys_ok": 0, "deploys_failed": 0, "deploy_ms": [], "cycle_s": [], "late_s": []}
        self.last = {}
        # Serialises data-dir writes from the exit-manager task with git add/commit/push (never held long).
        self.io_lock = asyncio.Lock()
        self.exit_task: asyncio.Task | None = None
        self.claude = None                   # Claude parallel paper sims (signals/claude_sim.py)
        self.claude_exit_task: asyncio.Task | None = None
        self.claude_scan_task: asyncio.Task | None = None
        self.bracket = None                  # 'bracket' Gemini shadow book (signals/bracket.py)
        self.bracket_task: asyncio.Task | None = None
        self.bracket_cycle_task: asyncio.Task | None = None
        self.carbon = None                   # CB-BRACKET / CB-RUNNER Carbon side books (signals/carbon.py)
        self.carbon_tasks: list[asyncio.Task] = []
        self.deepseek = None                 # DS-FLASH DeepSeek entry-only book (signals/deepseek.py)
        self.deepseek_exit_task: asyncio.Task | None = None
        self.scorer = None
        self.scorer_task: asyncio.Task | None = None
        self.opus_b = None
        self.opus_b_exit_task: asyncio.Task | None = None
        self.m15 = None
        self.m15_task: asyncio.Task | None = None
        self.other_venue = None
        self.other_venue_task: asyncio.Task | None = None
        for sub in ("state", "logs"):
            (data / sub).mkdir(parents=True, exist_ok=True)

    # ---------- lock ----------
    def heartbeat(self, **extra) -> None:
        h = L.read(self.data, L.HOLDER) or {}
        h.update({"run_id": self.run_id, "heartbeat_at": L.iso(L.now()), "ends_at": L.iso(self.ends_at),
                  "interval_minutes": self.interval, "released": False, "handed_to": None,
                  "cycles": self.cycles, "run_url": os.environ.get("RUN_URL"), **extra})
        L.write(self.data, L.HOLDER, h)

    def acquire(self) -> bool:
        if self.role == "successor":
            return L.wait_for_handoff(self.data, self.run_id, (self.ends_at - L.now()).total_seconds() / 60,
                                      self.interval)
        for _ in range(5):
            L.sync_hard(self.data)
            if L.stop_requested(self.data):
                log.info("STOP present -> not starting"); return False
            h = L.read(self.data, L.HOLDER)
            if h and h.get("run_id") == self.run_id and not h.get("released"):
                return True
            if L.holder_alive(h) and (h or {}).get("handed_to") != self.run_id:
                log.info("another loop is alive (%s) -> exit", h.get("run_id")); return False
            if L.try_acquire(self.data, self.run_id, self.ends_at, self.interval):
                return True
        return False

    # ---------- persistence ----------
    def commit_push(self, msg: str) -> bool:
        d = self.data
        _git(d, "add", "-A", "--", "state", "logs", *[f for f in SITE_JSON + DATA_JSON if (d / f).exists()])
        if _git(d, "diff", "--cached", "--quiet", check=False).returncode != 0:
            _git(d, "commit", "-q", "-m", msg)
        for attempt in range(1, 6):
            if _git(d, "push", "-q", "origin", f"HEAD:{L.BRANCH}", check=False).returncode == 0:
                return True
            r = _git(d, "pull", "-q", "--rebase", "origin", L.BRANCH, check=False)
            if r.returncode != 0:
                _git(d, "rebase", "--abort", check=False)
                log.error("rebase conflict on data branch: %s", r.stderr.strip()[-300:])
                _git(d, "fetch", "-q", "origin", L.BRANCH, check=False)
                remote = _git(d, "show", f"origin/{L.BRANCH}:{L.HOLDER}", check=False)
                try:
                    owner = json.loads(remote.stdout).get("run_id") if remote.returncode == 0 else None
                except Exception:
                    owner = None
                if owner and owner != self.run_id:
                    raise LostLock(f"lock now held by {owner}")
            time.sleep(2 * attempt)
        log.error("push failed after retries (will retry next cycle)")
        return False

    def trim_logs(self) -> None:
        cutoff = (L.now() - timedelta(days=CYCLE_LOG_KEEP_DAYS)).strftime("%Y-%m-%d")
        root = Path(self.cfg["general"]["logs_dir"])
        for day in root.glob("20??-??-??"):
            if day.name < cutoff:
                for sub in day.iterdir():  # keep lucid_*.jsonl (trade/telemetry history) forever
                    if sub.is_dir():
                        shutil.rmtree(sub, ignore_errors=True)
                    elif sub.name == "signals.jsonl":
                        sub.unlink(missing_ok=True)

    # ---------- Pages ----------
    def stage_site(self) -> Path:
        st = Path(tempfile.mkdtemp(prefix="site-"))
        for f in self.main_dir.iterdir():
            if f.is_file() and (not f.name.startswith(".") or f.name == ".nojekyll") \
                    and f.suffix.lower() not in (".py", ".md", ".yml", ".yaml"):
                shutil.copy2(f, st / f.name)
        for f in SITE_JSON:
            if (self.data / f).exists():
                shutil.copy2(self.data / f, st / f)
        return st

    async def deploy(self) -> dict | None:
        if not self.deploy_enabled:
            return None
        if self.deploy_skip > 0:
            self.deploy_skip -= 1
            log.info("pages: skipping this cycle (back-off, %d more)", self.deploy_skip)
            return None
        st = self.stage_site()
        name = f"pages-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}-{self.cycles}-{int(time.time())}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", str(ROOT / "tools" / "deploy-pages.mjs"), str(st), name, cwd=str(ROOT),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 170)
            line = (out.decode().strip().splitlines() or ["{}"])[-1]
            try:
                res = json.loads(line)
            except Exception:
                res = {"ok": False, "error": out.decode()[-300:]}
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            shutil.rmtree(st, ignore_errors=True)
        if res.get("ok"):
            self.stats["deploys_ok"] += 1; self.stats["deploy_ms"].append(res.get("ms"))
            self.deploy_fail_streak = 0
            log.info("pages deployed in %sms (artifact %s, deployment %s)", res.get("ms"), res.get("artifact_id"),
                     res.get("deployment_id"))
        else:
            self.stats["deploys_failed"] += 1
            self.deploy_fail_streak += 1
            self.deploy_skip = min(4, 2 ** (self.deploy_fail_streak - 1))
            log.warning("pages deploy failed (%s); skipping next %d cycle(s)", res, self.deploy_skip)
        return res

    def dispatch_successor(self) -> None:
        if self.dispatched or not os.environ.get("GITHUB_ACTIONS"):
            return
        r = subprocess.run(["gh", "workflow", "run", os.environ.get("SIGNALS_WORKFLOW", "pipeline.yml"),
                            "--ref", os.environ.get("GITHUB_REF_NAME", "main"), "-f", "action=start",
                            "-f", f"interval_minutes={self.interval}"],
                           capture_output=True, text=True, cwd=self.main_dir)
        self.dispatched = r.returncode == 0
        log.info("dispatched successor run: %s %s", "ok" if self.dispatched else "FAILED", r.stderr.strip()[-200:])

    def status(self, state: str, nxt: datetime | None, interval: int | None = None) -> None:
        write_status(self.cfg, state, interval if interval is not None else self.scan_interval,
                     next_run_at=nxt.isoformat() if nxt else None,
                     runner="github-actions", run_id=self.run_id, **self.last)

    # ---------- main loop ----------
    async def run(self) -> str:
        if not self.acquire():
            return "not-acquired"
        log.info("loop owner %s: every %d min (+%ds), ends %s, hard cap %s", self.run_id, self.interval,
                 self.offset, self.ends_at.isoformat(), self.hard_cap.isoformat())
        # data checkout may have been reset during acquire: make sure we start from the newest state
        reason = "max-runtime"
        await self._verify_models_once()
        self.write_reset_marker()
        self.start_other_venue()
        self.start_exit_manager()
        self.start_claude()
        self.start_bracket()
        self.start_carbon()
        self.start_deepseek()
        self.start_opus_b()
        self.start_scorer()
        self.start_m15_scan()
        self.start_panel()
        while True:
            # The job still wakes every `self.interval` (2 min). The site countdown uses
            # scan_interval (2 active / 15 during ASIAN / DEADZONE) so it matches the next Gemini call.
            wake = next_boundary(datetime.now(timezone.utc), self.interval, self.offset)
            nxt = next_boundary(datetime.now(timezone.utc), self.scan_interval, self.offset)
            self.status("running", nxt)
            await asyncio.sleep(max(0.0, (wake - datetime.now(timezone.utc)).total_seconds()))
            started = datetime.now(timezone.utc)
            self.stats["late_s"].append(round((started - wake).total_seconds(), 1))
            t0 = time.monotonic()
            res = {}
            try:
                # Fill already-queued pendings / apply CLOSE requests BEFORE the entry scan so a
                # 10–16s Gemini call cannot delay the first exit check on a new position.
                if not self.dry_run:
                    try:
                        rpl = await lucid_replay(self.cfg)
                        if (rpl or {}).get("filled"):
                            log.info("lucid pre-scan fill: %s", rpl)
                    except Exception:
                        log.exception("lucid replay before entry scan failed")
                res = await run_once(self.cfg, dry_run=self.dry_run, force=True, model=self.model)
            except Exception:
                log.exception("cycle failed")
            self.cycles += 1
            if (res or {}).get("scan_interval_minutes"):
                self.scan_interval = int(res["scan_interval_minutes"])
            if not (res or {}).get("skipped"):
                self.last = {"last_cycle_started_at": started.isoformat(),
                             "last_cycle_finished_at": datetime.now(timezone.utc).isoformat()}
            nxt2 = next_boundary(datetime.now(timezone.utc), self.scan_interval, self.offset)
            self.status("running", nxt2)
            self.schedule_claude(res)
            self.schedule_bracket(res)
            deploy_task = asyncio.create_task(self.deploy())
            if not self.dry_run:
                lres = await lucid_step(self.cfg, (res or {}).get("record"))
                log.info("lucid: %s", json.dumps(lres, default=str)[:300])
                try:
                    from .other_venue import get_hub, MAIN_COPIES
                    hub = get_hub(self.cfg)
                    if hub is not None:
                        hub.intake_from_record("MAIN", (res or {}).get("record"),
                                               datetime.now(timezone.utc), copy_to=MAIN_COPIES)
                        hub.persist()
                except Exception:
                    log.exception("other-venue MAIN intake failed")
            if self.cycles % 30 == 1:
                self.trim_logs()
            self.heartbeat(last_cycle_at=started.isoformat())
            async with self.io_lock:
                ok = await asyncio.to_thread(self.commit_push, f"cycle {started.strftime('%H:%M')}Z run {self.run_id}")
            dres = await deploy_task
            cyc = round(time.monotonic() - t0, 1)
            self.stats["cycle_s"].append(cyc)
            log.info("cycle %d done in %.1fs (push %s, deploy %s)", self.cycles, cyc, ok,
                     None if dres is None else dres.get("ok"))
            # ---- exit / handoff decisions ----
            if L.stop_requested(self.data):
                reason = "stop-file"; break
            if self.max_cycles and self.cycles >= self.max_cycles:
                reason = "max-cycles"; break
            t = L.now()
            succ = L.read(self.data, L.SUCC)
            fresh = L.succ_fresh(succ, t)
            if t >= self.ends_at - timedelta(minutes=20) and not fresh:
                self.dispatch_successor()
            if t >= self.ends_at and fresh:
                reason = "handoff"; break
            if t >= self.hard_cap:
                reason = "hard-cap"; break
        await self.shutdown(reason)
        return reason

    async def _verify_models_once(self) -> None:
        """Probe configured entry/exit IDs with the repo key. Log the result; never swap a rejected ID."""
        from .config import entry_model, exit_model
        from .model_verify import verify_models, format_report
        log.info("configured models: entry=%s exit=%s (override entry=%s)",
                 entry_model(self.cfg, self.model), exit_model(self.cfg), self.model)
        try:
            res = await asyncio.to_thread(verify_models, self.cfg, [entry_model(self.cfg, self.model), exit_model(self.cfg)])
        except Exception:
            log.exception("model verification failed")
            return
        report = format_report(res)
        for line in report.splitlines():
            log.info("model-verify: %s", line)
        try:
            day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
            d = self.data / "logs" / day
            d.mkdir(parents=True, exist_ok=True)
            (d / "model_verify.json").write_text(json.dumps(res, indent=2, default=str))
        except Exception:
            log.exception("model_verify.json write failed")

    async def _lucid_before_exit_tick(self) -> None:
        """Fill due pendings / apply CLOSE requests, then the exit tick can watch a just-opened position."""
        try:
            rpl = await lucid_replay(self.cfg, only_if_needed=True)
            if (rpl or {}).get("filled"):
                log.info("lucid pre-tick fill: %s", rpl)
        except Exception:
            log.exception("lucid replay before exit tick failed")

    def start_exit_manager(self) -> None:
        """Gemini exit checks every [exit_manager].check_seconds (18 s) while a book position is open.
        Separate asyncio task: never blocks the clock-aligned 2-min entry cycle; idle ticks are a file read.
        Each tick first runs a cheap Lucid replay when a pending fill or CLOSE request is waiting, so
        checks start within one period of the fill instead of after the next entry scan."""
        if not em_cfg(self.cfg).get("enabled") or not (self.cfg.get("lucid") or {}).get("enabled"):
            return
        mgr = ExitManager(self.cfg, dry_run=self.dry_run, model=None, io_lock=self.io_lock)
        pre = None if self.dry_run else self._lucid_before_exit_tick
        self.exit_task = asyncio.create_task(run_exit_loop(mgr, pre_tick=pre), name="exit-manager")
        log.info("exit manager on: check every %ss while a book position is open (lucid replay first)", mgr.period)

    async def stop_exit_manager(self) -> None:
        t, self.exit_task = self.exit_task, None
        if t is None:
            return
        t.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(t, 30)

    def start_claude(self) -> None:
        """Claude parallel paper sims: own entry task per M15 close + own 18 s exit loop. Fully isolated: any
        error is logged and swallowed, a missing ANTHROPIC_API_KEY only shows 'waiting for key' on the panel."""
        if self.dry_run or not (self.cfg.get("claude") or {}).get("enabled", False):
            return
        try:
            from .claude_sim import ClaudeEngine, run_claude_exit_loop, run_claude_scan_loop
            self.claude = ClaudeEngine(self.cfg, io_lock=self.io_lock)
            self.claude_exit_task = asyncio.create_task(run_claude_exit_loop(self.claude), name="claude-exit")
            self.claude_scan_task = asyncio.create_task(run_claude_scan_loop(self.claude), name="claude-scan")
            log.info("claude sims on: %s, exits %s, scan M15+20s, key %s", list(self.claude.sims),
                     self.claude.cc["exit_model"], "present" if self.claude._key() else "MISSING (waiting)")
        except Exception:
            log.exception("claude sims failed to start (Gemini loop unaffected)")
            self.claude = None

    def schedule_claude(self, res: dict | None) -> None:
        """t251u: Claude entries fire only on the shared M15 close +20s scan, never the 2-min loop."""
        return

    async def stop_claude(self) -> None:
        for t in (self.claude_exit_task, self.claude_scan_task, getattr(self.claude, "entry_task", None)):
            if t is None:
                continue
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 30)
        self.claude_exit_task = None
        self.claude_scan_task = None

    def start_bracket(self) -> None:
        """'bracket' shadow book: consumes the same validated Gemini signals after each cycle (no extra Gemini
        call) + its own 18 s bracket checks. Isolated: errors are logged and never touch main / exit books."""
        from .bracket import bracket_cfg
        if self.dry_run or not bracket_cfg(self.cfg).get("enabled", False):
            return
        try:
            from .bracket import BracketBook, run_bracket_loop
            self.bracket = BracketBook(self.cfg, io_lock=self.io_lock)
            self.bracket_task = asyncio.create_task(run_bracket_loop(self.bracket), name="bracket")
            log.info("bracket shadow book on: %s", self.bracket.site_view()["rules"])
        except Exception:
            log.exception("bracket book failed to start (main loop unaffected)")
            self.bracket = None

    def schedule_bracket(self, res: dict | None) -> None:
        if self.bracket is None:
            return
        rec = (res or {}).get("record")
        if self.bracket_cycle_task is not None and not self.bracket_cycle_task.done():
            log.info("bracket: previous intake still running; this cycle's signals queued after it")
        prev = self.bracket_cycle_task

        async def _run():
            if prev is not None:
                with contextlib.suppress(BaseException):
                    await prev
            r = await self.bracket.on_cycle(rec)
            log.info("bracket: %s", json.dumps(r, default=str)[:300])
        self.bracket_cycle_task = asyncio.create_task(_run(), name="bracket-cycle")

    async def stop_bracket(self) -> None:
        for t in (self.bracket_cycle_task, self.bracket_task):
            if t is None:
                continue
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 30)
        self.bracket_task = self.bracket_cycle_task = None

    def start_carbon(self) -> None:
        """Carbon side books: own M15-close+20 s entry scan, own triggered checks, own 18 s server exits, each an
        independent task (never awaited by the 2-min cycle). Errors are logged; no other book is touched."""
        from .carbon import carbon_cfg
        if self.dry_run or not carbon_cfg(self.cfg).get("enabled", False):
            return
        try:
            from .carbon import CarbonEngine, run_carbon_exit_loop, run_carbon_scan_loop, run_carbon_trigger_loop
            self.carbon = CarbonEngine(self.cfg, io_lock=self.io_lock)
            self.carbon_tasks = [asyncio.create_task(run_carbon_scan_loop(self.carbon), name="carbon-scan"),
                                 asyncio.create_task(run_carbon_trigger_loop(self.carbon), name="carbon-trigger"),
                                 asyncio.create_task(run_carbon_exit_loop(self.carbon), name="carbon-exit")]
            log.info("carbon side books on: %s model=%s thinking=%s key %s", list(self.carbon.books),
                     self.carbon.cc["model"], self.carbon.cc["entry_thinking"],
                     "present" if os.environ.get("GEMINI_API_KEY") else "MISSING")
        except Exception:
            log.exception("carbon side books failed to start (main loop unaffected)")
            self.carbon = None

    async def stop_carbon(self) -> None:
        for t in self.carbon_tasks:
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 30)
        self.carbon_tasks = []

    def start_deepseek(self) -> None:
        """DS-FLASH entry-only book: same 2-min cycle as Gemini (kicked from run_once, never awaited),
        own 18 s mechanical-exit loop. Isolated: errors are logged; no other book is touched."""
        from .deepseek import deepseek_cfg
        if self.dry_run or not deepseek_cfg(self.cfg).get("enabled", False):
            return
        try:
            from .deepseek import DeepSeekEngine, run_deepseek_exit_loop, set_engine
            self.deepseek = DeepSeekEngine(self.cfg, io_lock=self.io_lock)
            set_engine(self.deepseek)
            self.deepseek_exit_task = asyncio.create_task(run_deepseek_exit_loop(self.deepseek),
                                                          name="ds-flash-exit")
            log.info("deepseek DS-FLASH on: model=%s effort=%s key %s",
                     self.deepseek.cc["model"], self.deepseek.cc["effort"],
                     "present" if self.deepseek._key() else "MISSING (waiting)")
        except Exception:
            log.exception("deepseek DS-FLASH failed to start (main loop unaffected)")
            self.deepseek = None
            try:
                from .deepseek import set_engine
                set_engine(None)
            except Exception:
                pass

    async def stop_deepseek(self) -> None:
        t, self.deepseek_exit_task = self.deepseek_exit_task, None
        entry = getattr(self.deepseek, "entry_task", None) if self.deepseek else None
        for task in (t, entry):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 30)
        try:
            from .deepseek import set_engine
            set_engine(None)
        except Exception:
            pass
        self.deepseek = None

    def start_opus_b(self) -> None:
        """C-OPUS-B: own $100k Lucid account, shared bracket-v1 package, 18 s mechanical exits."""
        from .opus_b import opus_b_cfg
        if self.dry_run or not opus_b_cfg(self.cfg).get("enabled", True):
            return
        if not (self.cfg.get("claude") or {}).get("enabled", False):
            # still allow C-OPUS-B if claude section is on; if the whole claude block is off, skip
            pass
        try:
            from .opus_b import OpusBEngine, run_opus_b_exit_loop
            self.opus_b = OpusBEngine(self.cfg, io_lock=self.io_lock)
            self.opus_b_exit_task = asyncio.create_task(run_opus_b_exit_loop(self.opus_b), name="opus-b-exit")
            log.info("C-OPUS-B on: model=%s effort=%s key %s",
                     self.opus_b.cc["model"], self.opus_b.cc["effort"],
                     "present" if self.opus_b._key() else "MISSING (waiting)")
        except Exception:
            log.exception("C-OPUS-B failed to start (other books unaffected)")
            self.opus_b = None

    async def stop_opus_b(self) -> None:
        t, self.opus_b_exit_task = self.opus_b_exit_task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 30)
        self.opus_b = None

    def start_m15_scan(self) -> None:
        """Shared M15 close +20s coordinator: one snapshot, every close-bar model."""
        if self.dry_run or (self.cfg.get("m15_scan") or {}).get("enabled", True) is False:
            return
        try:
            from .m15_scan import M15Coordinator, run_m15_scan_loop
            self.m15 = M15Coordinator(
                self.cfg, io_lock=self.io_lock,
                claude=self.claude, carbon=self.carbon,
                deepseek=self.deepseek, opus_b=self.opus_b, scorer=self.scorer,
                state_dir=self.data / "state", logs_dir=self.data / "logs", site_dir=self.data,
            )
            self.m15_task = asyncio.create_task(run_m15_scan_loop(self.m15), name="m15-scan")
            log.info("m15 shared scan on: offset=%ss symbols=%s claude=%s carbon=%s ds=%s opus_b=%s",
                     self.m15.offset(), self.m15.symbols(),
                     self.claude is not None, self.carbon is not None,
                     self.deepseek is not None, self.opus_b is not None)
        except Exception:
            log.exception("m15 shared scan failed to start (2-min Gemini loop unaffected)")
            self.m15 = None

    async def stop_m15_scan(self) -> None:
        t, self.m15_task = self.m15_task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 30)
        self.m15 = None

    def start_other_venue(self) -> None:
        """Parallel $100k OTHER-VENUE CFD books (BTCUSD) on every model book. 18 s uniform exits."""
        if self.dry_run or (self.cfg.get("other_venue") or {}).get("enabled", True) is False:
            return
        try:
            from .other_venue import OtherVenueHub, set_hub, run_other_venue_loop
            self.other_venue = OtherVenueHub(
                self.cfg, state_dir=self.data / "state", logs_dir=self.data / "logs", site_dir=self.data,
            )
            set_hub(self.other_venue)
            self.other_venue.persist()
            self.other_venue_task = asyncio.create_task(run_other_venue_loop(self.other_venue), name="other-venue")
            log.info("other-venue on: symbols=%s books=%s",
                     self.other_venue.cc.get("symbols"), list(self.other_venue.cc.get("books") or []))
        except Exception:
            log.exception("other-venue failed to start (other books unaffected)")
            self.other_venue = None

    async def stop_other_venue(self) -> None:
        t, self.other_venue_task = self.other_venue_task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 30)
        try:
            from .other_venue import set_hub
            set_hub(None)
        except Exception:
            pass
        self.other_venue = None

    def start_scorer(self) -> None:
        """Universal signal scorer (t248u): backfill logs, then score ready rows on a 60 s loop. No model calls."""
        if self.dry_run:
            return
        try:
            from .signal_score import SignalScorer

            async def _loop(sc: SignalScorer):
                try:
                    n = sc.ingest_logs()
                    log.info("signal_score backfill enqueued %s new rows (%s pending)", n, len(sc.s["queue"]))
                    sc._write_summary()
                    sc.save()
                except Exception:
                    log.exception("signal_score backfill failed")
                while True:
                    try:
                        sc.ingest_logs()
                        r = await sc.tick()
                        if r.get("scored"):
                            log.info("signal_score tick %s", r)
                    except Exception:
                        log.exception("signal_score tick failed")
                    await asyncio.sleep(60)

            self.scorer = SignalScorer(self.cfg, state_dir=self.data / "state",
                                       logs_dir=self.data / "logs", site_dir=self.data)
            self.scorer_task = asyncio.create_task(_loop(self.scorer), name="signal-score")
        except Exception:
            log.exception("signal scorer failed to start (main loop unaffected)")
            self.scorer = None

    def write_reset_marker(self) -> None:
        """Shared reset (t255u): stamp state/reset.json with the token + exact time before any book loads."""
        if self.dry_run:
            return
        try:
            from .panel import write_reset_marker
            m = write_reset_marker(self.cfg, self.data / "state")
            if m:
                log.info("shared reset marker: token=%s at=%s", m.get("token"), m.get("at"))
        except Exception:
            log.exception("reset marker write failed")

    def start_panel(self) -> None:
        """Clean panel ledger (t252u): per-book performance + every position since the reset, live MFE/MAE $."""
        if self.dry_run or (self.cfg.get("panel") or {}).get("enabled", True) is False:
            return
        try:
            from .panel import PanelLedger, run_panel_loop
            self.panel = PanelLedger(self.cfg, state_dir=self.data / "state", logs_dir=self.data / "logs",
                                     site_dir=self.data)
            self.panel_task = asyncio.create_task(run_panel_loop(self.panel), name="panel")
        except Exception:
            log.exception("panel ledger failed to start (books unaffected)")
            self.panel = None

    async def stop_panel(self) -> None:
        t, self.panel_task = getattr(self, "panel_task", None), None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 30)

    async def stop_scorer(self) -> None:
        t, self.scorer_task = self.scorer_task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 30)
        self.scorer = None

    async def shutdown(self, reason: str) -> None:
        await self.stop_exit_manager()
        await self.stop_claude()
        await self.stop_bracket()
        await self.stop_carbon()
        await self.stop_m15_scan()
        await self.stop_deepseek()
        await self.stop_opus_b()
        await self.stop_other_venue()
        await self.stop_scorer()
        await self.stop_panel()
        succ = L.read(self.data, L.SUCC)
        handed = succ.get("run_id") if (reason == "handoff" and succ) else None
        if reason == "stop-file":
            self.status("paused", None)
        h = L.read(self.data, L.HOLDER) or {}
        h.update({"released": True, "released_at": L.iso(L.now()), "handed_to": handed, "exit_reason": reason,
                  "cycles": self.cycles, "heartbeat_at": L.iso(L.now())})
        L.write(self.data, L.HOLDER, h)
        await asyncio.to_thread(self.commit_push, f"loop: {self.run_id} released ({reason})"
                                + (f" -> {handed}" if handed else ""))
        if reason == "stop-file":
            self.deploy_skip = 0
            await self.deploy()
        s = self.stats
        summary = {"run_id": self.run_id, "reason": reason, "cycles": self.cycles,
                   "deploys_ok": s["deploys_ok"], "deploys_failed": s["deploys_failed"],
                   "deploy_ms_avg": round(sum(x or 0 for x in s["deploy_ms"]) / len(s["deploy_ms"])) if s["deploy_ms"] else None,
                   "cycle_s_avg": round(sum(s["cycle_s"]) / len(s["cycle_s"]), 1) if s["cycle_s"] else None,
                   "cycle_s_max": max(s["cycle_s"]) if s["cycle_s"] else None,
                   "start_late_s_max": max(s["late_s"]) if s["late_s"] else None}
        log.info("loop summary %s", json.dumps(summary))
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write("### Signal loop\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")


class LostLock(RuntimeError):
    pass


async def run_gha_loop(cfg: dict, data: Path, run_id: str, role: str, **kw) -> str:
    from .renderer import get_pool
    loop = GhaLoop(cfg, data, run_id, role, **kw)
    try:
        return await loop.run()
    except LostLock as e:
        await loop.stop_exit_manager()
        await loop.stop_claude()
        await loop.stop_bracket()
        await loop.stop_carbon()
        await loop.stop_m15_scan()
        await loop.stop_deepseek()
        await loop.stop_opus_b()
        await loop.stop_other_venue()
        await loop.stop_scorer()
        await loop.stop_panel()
        log.error("lost the loop lock: %s -> exiting without further writes", e)
        return "lost-lock"
    finally:
        try:
            await get_pool().close()
        except Exception:
            pass
