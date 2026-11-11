"""Single-loop lock + handoff for the GitHub Actions signal loop. Stdlib only (the gate job runs it before
any dependency install).

The lock lives on the data branch (default pipeline-data) as two files, each written by only one party so
pushes never conflict:
  state/loop_holder.json     written by the run that owns the loop (heartbeat every cycle)
  state/loop_successor.json  written by the run queued to take over near the holder's planned end
A file named STOP at the data-branch root stops everything (holder exits after its cycle, new runs exit).
Pushes are fast-forward only, so a rejected push means someone else moved first: re-read and decide again.

  python signals/gha_lock.py gate --data DIR --run-id ID [--action start|stop]   -> prints role=holder|successor|exit
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRANCH = os.environ.get("SIGNALS_DATA_BRANCH", "pipeline-data")
HOLDER = "state/loop_holder.json"
SUCC = "state/loop_successor.json"
SUCC_STALE_S = 300
SUCC_WINDOW_MIN = float(os.environ.get("SIGNALS_SUCCESSOR_WINDOW_MIN", "25"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat()


def parse(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def holder_stale_s(interval_min: float | None = None) -> float:
    iv = interval_min or float(os.environ.get("SIGNALS_INTERVAL_MINUTES", "2"))
    return max(480.0, 3 * iv * 60)


def git(data: Path, *args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=data, capture_output=True, text=True, check=check, timeout=120)


def read(data: Path, rel: str) -> dict | None:
    p = data / rel
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def write(data: Path, rel: str, obj: dict | None) -> None:
    p = data / rel
    if obj is None:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def sync_hard(data: Path) -> None:
    """Gate/lock moments only (no local work to keep): match the remote branch exactly."""
    git(data, "fetch", "-q", "origin", BRANCH)
    git(data, "reset", "-q", "--hard", f"origin/{BRANCH}")


def push_paths(data: Path, msg: str, paths: list[str]) -> bool:
    git(data, "add", "-A", "--", *paths)
    if git(data, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(data, "commit", "-q", "-m", msg)
    r = git(data, "push", "-q", "origin", f"HEAD:{BRANCH}", check=False)
    return r.returncode == 0


def stop_requested(data: Path) -> bool:
    return (data / "STOP").exists()


def holder_alive(h: dict | None, t: datetime | None = None) -> bool:
    if not h or h.get("released"):
        return False
    hb = parse(h.get("heartbeat_at"))
    return hb is not None and ((t or now()) - hb).total_seconds() < holder_stale_s(h.get("interval_minutes"))


def succ_fresh(s: dict | None, t: datetime | None = None) -> bool:
    hb = parse((s or {}).get("heartbeat_at"))
    return hb is not None and ((t or now()) - hb).total_seconds() < SUCC_STALE_S


def holder_record(run_id: str, ends_at: datetime, interval_min: float, **extra) -> dict:
    t = now()
    return {"run_id": run_id, "acquired_at": iso(t), "heartbeat_at": iso(t), "ends_at": iso(ends_at),
            "interval_minutes": interval_min, "released": False, "handed_to": None,
            "run_url": os.environ.get("RUN_URL"), **extra}


def try_acquire(data: Path, run_id: str, ends_at: datetime, interval_min: float) -> bool:
    write(data, HOLDER, holder_record(run_id, ends_at, interval_min))
    s = read(data, SUCC)
    paths = [HOLDER]
    if s and s.get("run_id") == run_id:
        write(data, SUCC, None)
        paths.append(SUCC)
    return push_paths(data, f"loop: {run_id} acquired", paths)


def gate(data: Path, run_id: str, action: str = "start", runtime_min: float = 330,
         interval_min: float = 2) -> str:
    """Decide this run's role. holder = lock acquired now; successor = registered to take over; exit."""
    for _ in range(6):
        sync_hard(data)
        if action == "stop":
            if not stop_requested(data):
                (data / "STOP").write_text(f"stop requested by run {run_id} at {iso(now())}\n")
                if not push_paths(data, f"loop: STOP requested by {run_id}", ["STOP"]):
                    continue
            return "exit"
        if action == "start" and stop_requested(data):
            (data / "STOP").unlink()
            if not push_paths(data, f"loop: STOP cleared by {run_id}", ["STOP"]):
                continue
        if stop_requested(data):
            print("STOP file present on data branch -> exit", file=sys.stderr)
            return "exit"
        t = now()
        h = read(data, HOLDER)
        if h and h.get("run_id") == run_id and not h.get("released"):
            return "holder"
        if not holder_alive(h, t) or (h or {}).get("handed_to") == run_id:
            if try_acquire(data, run_id, t + timedelta(minutes=runtime_min), interval_min):
                return "holder"
            continue
        ends = parse(h.get("ends_at")) or t
        if (ends - t).total_seconds() > SUCC_WINDOW_MIN * 60:
            print(f"loop alive ({h.get('run_id')}, ends {h.get('ends_at')}) -> exit", file=sys.stderr)
            return "exit"
        s = read(data, SUCC)
        if succ_fresh(s, t) and s.get("run_id") != run_id:
            print(f"successor already queued ({s.get('run_id')}) -> exit", file=sys.stderr)
            return "exit"
        write(data, SUCC, {"run_id": run_id, "registered_at": iso(t), "heartbeat_at": iso(t),
                           "run_url": os.environ.get("RUN_URL")})
        if push_paths(data, f"loop: {run_id} queued as successor", [SUCC]):
            return "successor"
    return "exit"


def wait_for_handoff(data: Path, run_id: str, runtime_min: float, interval_min: float,
                     timeout_min: float = 55) -> bool:
    """Successor: keep the registration fresh until the holder hands over (or dies), then acquire."""
    deadline = time.monotonic() + timeout_min * 60
    last_hb = 0.0
    while time.monotonic() < deadline:
        try:
            sync_hard(data)
        except Exception as e:
            print(f"sync failed: {e}", file=sys.stderr); time.sleep(10); continue
        if stop_requested(data):
            write(data, SUCC, None); push_paths(data, f"loop: {run_id} leaves queue (STOP)", [SUCC])
            return False
        t = now()
        h, s = read(data, HOLDER), read(data, SUCC)
        if s and s.get("run_id") != run_id and succ_fresh(s, t):
            print(f"replaced as successor by {s.get('run_id')} -> exit", file=sys.stderr)
            return False
        if (h or {}).get("handed_to") == run_id or not holder_alive(h, t) or (h or {}).get("run_id") == run_id:
            if try_acquire(data, run_id, t + timedelta(minutes=runtime_min), interval_min):
                print(f"handoff complete: {run_id} now holds the loop (from {(h or {}).get('run_id')})", file=sys.stderr)
                return True
            continue
        if time.monotonic() - last_hb > 60:
            write(data, SUCC, {**(s or {}), "run_id": run_id, "heartbeat_at": iso(t),
                               "run_url": os.environ.get("RUN_URL")})
            if push_paths(data, f"loop: {run_id} successor heartbeat", [SUCC]):
                last_hb = time.monotonic()
        time.sleep(5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["gate", "show"])
    ap.add_argument("--data", required=True)
    ap.add_argument("--run-id", default=os.environ.get("LOOP_RUN_ID", "local"))
    ap.add_argument("--action", default="start")
    ap.add_argument("--runtime-min", type=float, default=float(os.environ.get("SIGNALS_MAX_RUNTIME_MIN", "330")))
    ap.add_argument("--interval", type=float, default=float(os.environ.get("SIGNALS_INTERVAL_MINUTES", "2")))
    a = ap.parse_args()
    data = Path(a.data)
    for k, v in (("user.name", "github-actions[bot]"),
                 ("user.email", "41898282+github-actions[bot]@users.noreply.github.com")):
        if git(data, "config", "--get", k, check=False).returncode != 0:
            git(data, "config", k, v)
    if a.cmd == "show":
        print(json.dumps({"holder": read(data, HOLDER), "successor": read(data, SUCC),
                          "stop": stop_requested(data)}, indent=2))
        return
    role = gate(data, a.run_id, a.action, a.runtime_min, a.interval)
    print(f"role={role}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"role={role}\n")


if __name__ == "__main__":
    main()
