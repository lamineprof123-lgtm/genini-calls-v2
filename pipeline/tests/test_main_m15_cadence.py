"""t296u: MAIN calls Gemini once per M15 close (+20 s) on the shared M15 snapshot; 2-min cycles keep running
(prices / positions / Lucid / panel) without a Gemini call."""
from __future__ import annotations
import asyncio, copy, json, sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import pipeline as P
from signals.config import load_config
from signals.m15_scan import M15Coordinator, M15Snapshot

UTC = timezone.utc
SYMS = ["XAUUSD", "EURUSD", "GBPUSD", "US100", "XAGUSD", "BTCUSD"]


def _cfg(tmp_path: Path, cadence: str = "m15") -> dict:
    cfg = copy.deepcopy(load_config())
    cfg["general"].update(state_dir=str(tmp_path / "state"), logs_dir=str(tmp_path / "logs"), log_mode="compact")
    cfg["equity"]["paper_file"] = str(tmp_path / "state" / "paper_equity.json")
    cfg["site"]["dir"] = str(tmp_path)
    cfg.setdefault("publish", {})["enabled"] = False
    cfg["gemini"]["main_entry_cadence"] = cadence
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return cfg


def _payload(sym: str, bar: str = "2026-11-13T13:15:00+00:00") -> dict:
    return {"symbol": sym, "source": "test", "ticker": sym,
            "context": {"last_closed_bar_utc": bar, "last_close": 1.0, "session_blocks_automation": False}}


@pytest.fixture
def patched(monkeypatch):
    calls = {"gemini": [], "fetch": 0, "render": 0}

    async def fetch_symbol(sym, cfg):
        calls["fetch"] += 1
        return None, "test", sym

    async def live(payloads, cfg, now):
        return {"ms": 0.0}

    async def firm(payloads, cfg, now):
        return None

    async def render_all(payloads):
        calls["render"] += 1
        return {p["symbol"]: b"own-" + p["symbol"].encode() for p in payloads}

    async def call_gemini(cfg, payloads, pngs, model=None):
        calls["gemini"].append({"symbols": [p["symbol"] for p in payloads], "pngs": dict(pngs)})
        return {"model": "gemini-3.8-flash", "raw_text": json.dumps({"signals": [], "market_summary": "t"}),
                "usage": {"prompt_token_count": 100, "candidates_token_count": 10, "thoughts_token_count": 50}}

    monkeypatch.setattr(P, "fetch_symbol", fetch_symbol)
    monkeypatch.setattr(P, "build_payload", lambda sym, df, cfg, src, ticker: _payload(sym))
    monkeypatch.setattr(P, "enrich_live_prices", live)
    monkeypatch.setattr(P, "enrich_firmtape", firm)
    monkeypatch.setattr(P, "render_all", render_all)
    monkeypatch.setattr(P, "call_gemini", call_gemini)
    monkeypatch.setattr(P, "validate_batch", lambda obj, ctx, cfg, eq, h, r: [])
    return calls


def _snap(close: datetime) -> M15Snapshot:
    bar = close.isoformat()
    return M15Snapshot(snapshot_id=f"m15-{close:%H%M}", snapshot_hash="h", package_id="p", package_hash="ph",
                       bar_close_ts=close, snapshot_ts=close, now=close,
                       payloads=[_payload(s, bar) for s in SYMS],
                       charts={s: b"shared-" + s.encode() for s in SYMS}, facts={}, htf={},
                       chart_sha256={}, package={}, render_ms=0, start_lag_s=20.0)


def test_cadence_flag_and_scan_interval(tmp_path):
    assert P.main_m15_only(_cfg(tmp_path)) is True
    assert P.main_m15_only(_cfg(tmp_path, "cycle")) is False
    assert P.main_m15_only({"gemini": {}}) is False          # default stays legacy
    assert P.scan_interval_minutes(False, _cfg(tmp_path)) == 15
    assert P.scan_interval_minutes(False, _cfg(tmp_path, "cycle")) == 2
    assert load_config()["gemini"]["main_entry_cadence"] == "m15"   # live config switched (t296u)


def test_main_call_allowed_once_per_close(tmp_path):
    cfg = _cfg(tmp_path)
    c = datetime(2026, 11, 13, 13, 15, tzinfo=UTC)
    assert P.main_call_allowed(cfg, None) is False
    assert P.main_call_allowed(cfg, c) is True
    (tmp_path / "state" / P.MAIN_M15_STATE).write_text(json.dumps({"bar_close_ts": c.isoformat()}))
    assert P.main_call_allowed(cfg, c) is False
    assert P.main_call_allowed(cfg, datetime(2026, 11, 13, 13, 30, tzinfo=UTC)) is True
    assert P.main_call_allowed(_cfg(tmp_path, "cycle"), None) is True


def test_mid_bar_cycle_refreshes_without_gemini(tmp_path, patched):
    cfg = _cfg(tmp_path)
    for _ in range(3):
        res = asyncio.run(P.run_once(cfg, force=True))
        assert res["skipped"] is True and res["skipped_reason"] == P.MAIN_M15_SKIP
        assert res["scan_interval_minutes"] == 15
        assert res["record"]["skipped_reason"] == P.MAIN_M15_SKIP
        assert Path(res["record"]["run_dir"]).exists()          # cycle still logged for the panel / Lucid
    assert patched["gemini"] == []
    assert patched["fetch"] == 3 * len(cfg["general"]["symbols"])   # prices still refreshed every cycle


def test_m15_close_uses_shared_snapshot_once(tmp_path, patched):
    cfg = _cfg(tmp_path)
    c1 = datetime(2026, 11, 13, 13, 15, tzinfo=UTC)
    snap = _snap(c1)
    res = asyncio.run(P.run_once(cfg, force=True, m15_close=c1, snapshot=snap))
    assert res["skipped"] is False
    assert len(patched["gemini"]) == 1
    sent = patched["gemini"][0]
    assert all(v == snap.charts[k] for k, v in sent["pngs"].items())   # same chart bytes as the other books
    assert patched["fetch"] == 0 and patched["render"] == 0
    assert res["record"]["snapshot_id"] == snap.snapshot_id
    assert res["record"]["m15_close"] == c1.isoformat()
    assert snap.payloads[0]["context"].get("in_position") is None     # shared rows not mutated
    # same close again (e.g. a restart): no second call
    res2 = asyncio.run(P.run_once(cfg, force=True, m15_close=c1, snapshot=snap))
    assert res2["skipped_reason"] == P.MAIN_M15_SKIP and len(patched["gemini"]) == 1
    # mid-bar: none
    asyncio.run(P.run_once(cfg, force=True))
    assert len(patched["gemini"]) == 1
    # next close: exactly one more
    c2 = datetime(2026, 11, 13, 13, 30, tzinfo=UTC)
    asyncio.run(P.run_once(cfg, force=True, m15_close=c2, snapshot=_snap(c2)))
    assert len(patched["gemini"]) == 2


def test_m15_close_without_snapshot_falls_back_to_own_fetch(tmp_path, patched):
    cfg = _cfg(tmp_path)
    c = datetime(2026, 11, 13, 13, 15, tzinfo=UTC)
    res = asyncio.run(P.run_once(cfg, force=True, m15_close=c, snapshot=None))
    assert res["skipped"] is False and len(patched["gemini"]) == 1
    assert patched["render"] == 1 and patched["fetch"] > 0


def test_legacy_cycle_mode_calls_every_cycle(tmp_path, patched):
    cfg = _cfg(tmp_path, "cycle")
    for _ in range(2):
        asyncio.run(P.run_once(cfg, force=True))
    assert len(patched["gemini"]) == 2


def _loop(tmp_path, cadence="m15"):
    from signals.gha_loop import GhaLoop
    cfg = _cfg(tmp_path, cadence)
    return GhaLoop(cfg, tmp_path / "data", "test-run", "start", dry_run=True, deploy=False), cfg


def test_next_wake_schedule(tmp_path):
    lp, cfg = _loop(tmp_path)
    t = lambda h, m, s=0: datetime(2026, 11, 13, h, m, s, tzinfo=UTC)
    # mid-bar on an odd quarter: 2-min boundaries are even minutes, the M15 wake is 13:15:20
    assert lp.next_wake(t(13, 14, 30)) == (t(13, 15, 20), t(13, 15))
    # :30 / :00 coincide with a 2-min boundary -> still the M15 wake
    assert lp.next_wake(t(13, 28, 30)) == (t(13, 30, 20), t(13, 30))
    # after the close was served: back to 2-min price cycles with no Gemini
    lp.m15_served = t(13, 15)
    assert lp.next_wake(t(13, 15, 50)) == (t(13, 16, 20), None)
    # a long cycle ran past 13:45:20 without serving it -> serve the close immediately (<= 5 min late)
    now = t(13, 46, 0)
    assert lp.next_wake(now) == (now, t(13, 45))
    # too late (> 5 min): wait for the next close, never a mid-bar call
    assert lp.next_wake(t(13, 51, 0)) == (t(13, 52, 20), None)
    # served state on disk (e.g. after a restart) is honoured
    (Path(cfg["general"]["state_dir"]) / P.MAIN_M15_STATE).write_text(json.dumps({"bar_close_ts": t(13, 45).isoformat()}))
    lp.m15_served = None
    assert lp.next_wake(now) == (t(13, 46, 20), None)
    assert lp.scan_interval == 15


def test_next_wake_legacy(tmp_path):
    lp, _ = _loop(tmp_path, "cycle")
    t = datetime(2026, 11, 13, 13, 14, 30, tzinfo=UTC)
    assert lp.next_wake(t) == (datetime(2026, 11, 13, 13, 16, 20, tzinfo=UTC), None)


def test_coordinator_hands_snapshot_to_main(tmp_path):
    cfg = _cfg(tmp_path)
    co = M15Coordinator(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    c = datetime(2026, 11, 13, 13, 15, tzinfo=UTC)

    async def go():
        waiter = asyncio.create_task(co.wait_snapshot(c, timeout=5))
        await asyncio.sleep(0.05)
        await co.publish_snapshot(_snap(c))
        got = await waiter
        miss = await co.wait_snapshot(datetime(2026, 11, 13, 13, 30, tzinfo=UTC), timeout=0.1)
        return got, miss

    got, miss = asyncio.run(go())
    assert got is not None and got.bar_close_ts == c
    assert miss is None


def test_bracket_ignores_main_m15_skip_records():
    import inspect
    from signals import bracket
    assert "main_m15_cadence" in inspect.getsource(bracket.BracketBook.intake)
