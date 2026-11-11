"""gha_lock restart action: queue as successor, restart STOP, holder release, acquire + STOP cleanup."""
from __future__ import annotations
import subprocess, sys, threading, time
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import gha_lock as L


def _sh(cwd, *a):
    subprocess.run(list(a), cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    _sh(tmp_path, "git", "init", "-q", "--bare", str(bare))
    seed = tmp_path / "seed"
    _sh(tmp_path, "git", "clone", "-q", str(bare), str(seed))
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        _sh(seed, "git", "config", k, v)
    (seed / "README.md").write_text("data\n")
    _sh(seed, "git", "checkout", "-q", "-b", L.BRANCH)
    _sh(seed, "git", "add", "-A"); _sh(seed, "git", "commit", "-q", "-m", "seed")
    _sh(seed, "git", "push", "-q", "origin", L.BRANCH)
    return bare


def _clone(remote, path):
    _sh(path.parent, "git", "clone", "-q", "--branch", L.BRANCH, str(remote), str(path))
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        _sh(path, "git", "config", k, v)
    return path


def _release(data, reason="stop-file"):
    L.sync_hard(data)
    h = L.read(data, L.HOLDER)
    h.update({"released": True, "exit_reason": reason, "handed_to": None})
    L.write(data, L.HOLDER, h)
    assert L.push_paths(data, "release", [L.HOLDER])


def test_restart_handover_without_waiting_for_ends_at(remote, tmp_path):
    old = _clone(remote, tmp_path / "old")
    new = _clone(remote, tmp_path / "new")
    assert L.gate(old, "old-1", "start") == "holder"
    # a cron run with the holder far from its end exits; a restart run queues as successor anyway
    assert L.gate(_clone(remote, tmp_path / "cron"), "cron-1", "cron") == "exit"
    assert L.gate(new, "new-1", "restart") == "successor"
    assert L.read(new, L.SUCC)["restart"] is True

    result = {}
    th = threading.Thread(target=lambda: result.setdefault("ok", L.wait_for_handoff(new, "new-1", 330, 2, timeout_min=1)))
    th.start()
    # the successor writes the restart STOP; the old holder sees it after its cycle and releases
    for _ in range(100):
        L.sync_hard(old)
        if L.stop_requested(old):
            break
        time.sleep(0.1)
    assert L.restart_stop_owner(old) == "new-1"
    # cron gates meanwhile must not clear a live restart STOP nor grab the lock
    assert L.gate(_clone(remote, tmp_path / "cron2"), "cron-2", "cron") == "exit"
    _release(old)
    th.join(30)
    assert result.get("ok") is True
    L.sync_hard(old)
    h = L.read(old, L.HOLDER)
    assert h["run_id"] == "new-1" and not h["released"]
    assert not L.stop_requested(old)                  # restart STOP removed on acquire
    assert L.read(old, L.SUCC) is None


def test_cron_keeps_user_stop_but_clears_orphaned_restart_stop(remote, tmp_path):
    d = _clone(remote, tmp_path / "d")
    (d / "STOP").write_text("stop requested by run x\n")
    assert L.push_paths(d, "stop", ["STOP"])
    assert L.gate(d, "cron-1", "cron") == "exit"
    assert L.stop_requested(d)
    # restart STOP from a successor that died (no fresh successor record) -> cleared by the next cron gate
    L.write_restart_stop(d, "dead-9")
    assert L.push_paths(d, "restart stop", ["STOP"])
    assert L.restart_stop_orphaned(d)
    assert L.gate(d, "cron-2", "cron") == "holder"
    assert not L.stop_requested(d)


def test_start_still_clears_user_stop(remote, tmp_path):
    d = _clone(remote, tmp_path / "d")
    (d / "STOP").write_text("stop requested by run x\n")
    assert L.push_paths(d, "stop", ["STOP"])
    assert L.gate(d, "start-1", "start") == "holder"
