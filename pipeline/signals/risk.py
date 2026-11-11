"""Equity sources and the daily-drawdown circuit breaker (state persisted to disk)."""
from __future__ import annotations
import json, os, tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class PaperEquityFile:
    """Reads {"equity": <float>} from a local JSON file (edit it, or have a paper-trade tracker update it)."""

    def __init__(self, path: str, start_equity: float = 100000.0):
        self.path = Path(path)
        if not self.path.exists():
            _atomic_write(self.path, {"equity": start_equity, "currency": "USD",
                                      "note": "paper equity - update to reflect P&L"})

    def get_equity(self) -> float:
        return float(json.loads(self.path.read_text())["equity"])


class MT5Equity:
    """Account equity from a MetaTrader5 terminal (Windows / bridge only)."""

    def get_equity(self) -> float:
        import MetaTrader5 as mt5  # type: ignore
        if not mt5.initialize():
            raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
        return float(mt5.account_info().equity)


def make_equity_source(cfg: dict):
    e = cfg["equity"]
    if e["provider"] == "paper_file":
        return PaperEquityFile(e["paper_file"], e.get("paper_start_equity", 100000.0))
    if e["provider"] == "mt5":
        return MT5Equity()
    raise ValueError(f"unknown equity provider {e['provider']}")


@dataclass
class BreakerStatus:
    session_day: str
    start_equity: float
    equity: float
    loss_pct: float
    halted: bool
    halted_at: str | None
    reason: str | None


class DailyDrawdownBreaker:
    """Latches HALT for the rest of the session day once loss vs start-of-day equity exceeds the limit.
    The latch is persisted, so restarts do not clear it; it resets only when a new session day begins."""

    def __init__(self, state_path: str | Path, max_daily_loss_pct: float = 3.5, tz: str = "UTC"):
        self.path = Path(state_path)
        self.max_loss = float(max_daily_loss_pct)
        self.tz = ZoneInfo(tz)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def check(self, equity: float, now: datetime | None = None) -> BreakerStatus:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(self.tz).date().isoformat()
        st = self._load()
        if st.get("session_day") != day:  # new session: snapshot start-of-day equity, clear latch
            st = {"session_day": day, "start_equity": float(equity), "halted": False, "halted_at": None,
                  "reason": None}
        start = float(st["start_equity"])
        loss_pct = round((start - equity) / start * 100, 6) if start > 0 else 0.0  # round away float noise
        if not st["halted"] and loss_pct > self.max_loss:
            st.update(halted=True, halted_at=now.isoformat(),
                      reason=f"daily loss {loss_pct:.2f}% > {self.max_loss}% (start {start:.2f}, now {equity:.2f})")
        st["last_equity"] = float(equity); st["last_check"] = now.isoformat(); st["last_loss_pct"] = round(loss_pct, 4)
        _atomic_write(self.path, st)
        return BreakerStatus(day, start, float(equity), round(loss_pct, 4), st["halted"], st["halted_at"], st["reason"])
