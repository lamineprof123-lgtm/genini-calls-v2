"""User 13 Nov 2026: MAIN (and the 1-strike / nostop / bracket copies of its call) calls Gemini and opens new
trades only 16:40-01:00 Dubai = 12:40-21:00 UTC. Outside: no Gemini call, no entries; exits keep running."""
from __future__ import annotations
import asyncio, inspect, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import pipeline as P
from signals.config import load_config
from test_main_m15_cadence import _cfg, _snap, patched  # noqa: F401  (fixture)

UTC = timezone.utc


def _at(h, m, s=0):
    return datetime(2026, 11, 13, h, m, s, tzinfo=UTC)


def test_config_window_is_1240_2100_utc():
    cfg = load_config()
    assert cfg["gemini"]["main_window_utc"] == ["12:40", "21:00"]
    assert cfg["gemini"]["main_entry_cadence"] == "m15"          # nothing else about MAIN changed
    assert (cfg["gemini"].get("prompt_template") or "system_prompt.j2") == "system_prompt.j2"


def test_window_bounds():
    cfg = {"gemini": {"main_window_utc": ["12:40", "21:00"]}}
    assert not P.main_window_open(cfg, _at(12, 30, 20))     # 16:30 Dubai close: no call
    assert not P.main_window_open(cfg, _at(12, 39, 59))
    assert P.main_window_open(cfg, _at(12, 40))
    assert P.main_window_open(cfg, _at(12, 45, 20))         # first MAIN call: 16:45 Dubai close
    assert P.main_window_open(cfg, _at(20, 45, 20))         # last MAIN call: 00:45 Dubai close
    assert not P.main_window_open(cfg, _at(21, 0, 0))
    assert not P.main_window_open(cfg, _at(21, 0, 20))
    assert not P.main_window_open(cfg, _at(3, 0))
    dubai = timezone(timedelta(hours=4))
    assert P.main_window_open(cfg, datetime(2026, 11, 14, 0, 59, tzinfo=dubai))
    assert not P.main_window_open(cfg, datetime(2026, 11, 14, 1, 0, tzinfo=dubai))
    assert P.main_window_open({"gemini": {}}, _at(3, 0))    # unset = always open
    wrap = {"gemini": {"main_window_utc": ["22:00", "02:00"]}}
    assert P.main_window_open(wrap, _at(23, 0)) and P.main_window_open(wrap, _at(1, 0))
    assert not P.main_window_open(wrap, _at(12, 0))


def test_no_main_call_outside_window(tmp_path, patched):
    cfg = _cfg(tmp_path)
    now = datetime.now(UTC)
    cfg["gemini"]["main_window_utc"] = [f"{(now + timedelta(hours=2)).hour:02d}:00",
                                        f"{(now + timedelta(hours=3)).hour:02d}:00"]
    c = _at(13, 15)
    for kw in ({}, {"m15_close": c, "snapshot": _snap(c)}):
        res = asyncio.run(P.run_once(cfg, force=True, **kw))
        assert res["skipped"] is True and res["skipped_reason"] == P.MAIN_WINDOW_SKIP
        assert res["record"]["signals"] == []                      # nothing for Lucid / exit books to enter
        assert Path(res["record"]["run_dir"]).exists()             # 2-min cycle still logged (exits / panel)
    assert patched["gemini"] == [] and patched["render"] == 0
    assert P.main_m15_last_close(cfg) is None                       # close not claimed


def test_close_before_window_not_called_even_if_served_late(tmp_path, patched):
    cfg = _cfg(tmp_path)
    cfg["gemini"]["main_window_utc"] = ["00:00", "23:59"]
    c = _at(12, 30)                                                # pre-window close served inside the window
    cfg["gemini"]["main_window_utc"] = ["12:40", "21:00"]
    res = asyncio.run(P.run_once(cfg, force=True, m15_close=c, snapshot=_snap(c)))
    assert res["skipped_reason"] == P.MAIN_WINDOW_SKIP and patched["gemini"] == []


def test_call_inside_window(tmp_path, patched):
    cfg = _cfg(tmp_path)
    now = datetime.now(UTC)
    cfg["gemini"]["main_window_utc"] = [f"{(now - timedelta(hours=1)).hour:02d}:00",
                                        f"{(now + timedelta(hours=2)).hour:02d}:00"]
    c = datetime.fromtimestamp(int(now.timestamp()) // 900 * 900, UTC)
    res = asyncio.run(P.run_once(cfg, force=True, m15_close=c, snapshot=_snap(c)))
    assert res["skipped"] is False and len(patched["gemini"]) == 1


def test_bracket_ignores_window_skip_records():
    from signals import bracket
    assert "main_outside_window" in inspect.getsource(bracket.BracketBook.intake)
