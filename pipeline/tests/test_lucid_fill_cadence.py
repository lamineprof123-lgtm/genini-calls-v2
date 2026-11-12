"""Pending fills must happen as soon as fill_after is reached, not after the next 2-min entry scan."""
from __future__ import annotations
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.exit_manager import run_exit_loop
from signals.lucid import (
    LucidSim, apply_due_fills, due_fill_until, due_pendings, inject_live_bars, lucid_needs_replay,
)

UTC = timezone.utc
T_SIG = datetime(2026, 11, 12, 8, 18, 38, tzinfo=UTC)   # London, matches the live 08:18Z bug
T_FILL = datetime(2026, 11, 12, 8, 19, 0, tzinfo=UTC)
T_NOW = datetime(2026, 11, 12, 8, 19, 5, tzinfo=UTC)
PX = 28500.0


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["lucid"]["slippage_ticks"] = 0
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


@pytest.fixture
def sim(cfg, tmp_path):
    s = LucidSim(cfg, tmp_path / "state" / "lucid", tmp_path / "logs")
    s.s["last_processed"] = datetime(2026, 11, 12, 8, 18, 0, tzinfo=UTC).isoformat()
    s._roll_day(T_SIG)
    return s


def _rec(t=T_SIG, sym="US100", px=PX, atr=60.0, action="SELL"):
    return {"run_at_utc": t.isoformat(), "signals": [{
        "symbol": sym, "action": action, "model_action": action, "entry": px,
        "stop_loss": None, "take_profit": None, "confidence": 0.8, "rr_local": None,
        "last_close": px, "atr": atr, "model_risk_pct": None, "reasons": [],
        "session": "LONDON_SESSION",
        "signal_candle": {"high": px + 20, "low": px - 20}, "levels": []}]}


def test_due_fill_until_includes_current_minute(sim):
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    assert due_pendings(sim, T_SIG) == []          # fill_after is 08:19
    assert due_pendings(sim, T_NOW) and due_fill_until(sim, T_NOW) == datetime(2026, 11, 12, 8, 20, tzinfo=UTC)


def test_lagging_closed_m1_plus_live_price_fills_immediately(sim):
    """The live bug: latest closed M1 is 08:18, so process(until=08:19) never reaches the 08:19 fill minute.
    A live price must inject that minute and fill as soon as now >= fill_after."""
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    assert sim.s["pending"] and not sim.s["positions"]
    # Closed-bar feed only, same cap the live 08:20 cycle used (until = last closed + 1 min = 08:19)
    closed = {"US100": pd.DataFrame(
        [{"open": PX, "high": PX + 1, "low": PX - 1, "close": PX}],
        index=pd.DatetimeIndex([datetime(2026, 11, 12, 8, 18, tzinfo=UTC)]),
    )}
    sim.process(closed, datetime(2026, 11, 12, 8, 19, tzinfo=UTC))
    assert not sim.s["positions"] and sim.s["pending"]   # the old cap misses the fill

    filled = apply_due_fills(sim, closed, T_NOW, live_prices={"US100": PX})
    assert filled and sim.s["positions"] and not sim.s["pending"]
    p = sim.s["positions"][0]
    assert p["symbol"] == "US100" and p["entry_time"].startswith("2026-11-12T08:19:00")
    assert p["id"] in {f["id"] for f in sim.s["book_feed"]}


def test_inject_does_not_overwrite_existing_bar(sim):
    assert sim.on_record(_rec(), T_SIG)[0]["taken"]
    t19 = datetime(2026, 11, 12, 8, 19, tzinfo=UTC)
    bars = {"US100": pd.DataFrame(
        [{"open": 28090.0, "high": 28100.0, "low": 28080.0, "close": 28095.0}],
        index=pd.DatetimeIndex([t19]),
    )}
    out = inject_live_bars(bars, sim, T_NOW, {"US100": 99999.0})
    assert float(out["US100"].loc[t19]["open"]) == 28090.0


def test_lucid_needs_replay_for_pending_or_close_request(sim, tmp_path):
    assert not lucid_needs_replay(sim)
    sim.s["pending"] = [{"symbol": "US100", "fill_after": T_FILL.isoformat()}]
    assert lucid_needs_replay(sim)
    sim.s["pending"] = []
    sim.s["positions"] = [{"id": 3, "symbol": "US100"}]
    req = tmp_path / "state" / "lucid" / "close_requests.json"
    req.write_text(json.dumps([{"id": 3, "symbol": "US100", "price": 1.0}]))
    assert lucid_needs_replay(sim)
    sim.s["close_done"] = [3]
    assert not lucid_needs_replay(sim)


def test_run_exit_loop_pre_tick_runs_before_each_tick():
    order = []

    class Mgr:
        period = 0.04

        async def tick(self):
            order.append("tick")

    async def pre():
        order.append("pre")

    async def go():
        t = asyncio.create_task(run_exit_loop(Mgr(), pre_tick=pre))
        await asyncio.sleep(0.15)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert order[0] == "pre" and "tick" in order
    # pre always precedes the tick that follows it
    pairs = list(zip(order, order[1:]))
    assert ("pre", "tick") in pairs and ("tick", "pre") in pairs
    assert all(order[i] != "tick" or (i and order[i - 1] == "pre") for i in range(len(order)))
