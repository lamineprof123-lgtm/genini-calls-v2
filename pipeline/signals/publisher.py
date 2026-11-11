"""Background publisher: copies site/status.json + site/latest_signal.json into the GitHub Pages repo and
commits + pushes them. One worker thread, requests are coalesced and serialized; failures never propagate.
Only those two JSON files are ever staged."""
from __future__ import annotations
import logging, os, shutil, subprocess, threading, time
from pathlib import Path

log = logging.getLogger("signals.publish")
FILES = ("status.json", "latest_signal.json")


class Publisher:
    def __init__(self, cfg: dict):
        p = cfg.get("publish", {})
        self.enabled = bool(p.get("enabled"))
        self.repo = Path(p.get("repo_dir", ""))
        self.branch = p.get("branch", "main")
        self.site = Path(cfg["site"]["dir"])
        self.retries = int(p.get("max_retries", 3))
        self.env = {**os.environ, "GIT_AUTHOR_NAME": p.get("author_name", ""), "GIT_COMMITTER_NAME": p.get("author_name", ""),
                    "GIT_AUTHOR_EMAIL": p.get("author_email", ""), "GIT_COMMITTER_EMAIL": p.get("author_email", ""),
                    "GIT_TERMINAL_PROMPT": "0"}
        self._event = threading.Event()
        self._idle = threading.Event(); self._idle.set()
        self._lock = threading.Lock()
        self._msg = "update"
        self.last_push_at: float | None = None
        if self.enabled:
            if not (self.repo / ".git").exists():
                log.error("publish disabled: %s is not a git repo", self.repo); self.enabled = False
            else:
                threading.Thread(target=self._worker, name="publisher", daemon=True).start()

    def publish(self, msg: str) -> None:
        """Non-blocking: schedule a publish of the current site files."""
        if not self.enabled:
            return
        with self._lock:
            self._msg = msg
            self._idle.clear()
            self._event.set()

    def flush(self, timeout: float = 90) -> bool:
        return self._idle.wait(timeout) if self.enabled else True

    def _git(self, *args, check=True, timeout=60):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True,
                              timeout=timeout, check=check)

    def _once(self, msg: str) -> None:
        for f in FILES:
            src = self.site / f
            if src.exists():
                dst = self.repo / f
                shutil.copyfile(src, dst); os.chmod(dst, 0o644)
        self._git("add", "--", *FILES)
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            log.info("publish: nothing changed"); return
        self._git("commit", "-q", "-m", msg, "--", *FILES)
        for attempt in range(1, self.retries + 2):
            r = self._git("push", "-q", "origin", f"HEAD:{self.branch}", check=False)
            if r.returncode == 0:
                self.last_push_at = time.time()
                log.info("published: %s", msg); return
            log.warning("push failed (attempt %d): %s", attempt, r.stderr.strip()[-300:])
            self._git("pull", "-q", "--rebase", "origin", self.branch, check=False)
            time.sleep(min(30, 3 * attempt))
        log.error("publish gave up after %d attempts (will retry on next event)", self.retries + 1)

    def _worker(self):
        while True:
            self._event.wait()
            with self._lock:
                self._event.clear(); msg = self._msg
            try:
                self._once(msg)
            except Exception as e:  # never crash the loop
                log.error("publish error: %s", e)
            with self._lock:
                if not self._event.is_set():
                    self._idle.set()
