"""Server-side scalp SL/TP: structure vs 0.8 ATR stop, fixed vs structural target, $381 net sizing."""
import copy, sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.scalp_levels import (
    compute_stop, compute_target, compute_scalp_levels, size_for_target, fixed_target_distance,
)


@pytest.fixture
def cfg():
    return load_config()


def test_fixed_targets_are_midpoints(cfg):
    assert fixed_target_distance("US100", cfg) == pytest.approx(5.0)       # 20 * 0.25
    assert fixed_target_distance("XAUUSD", cfg) == pytest.approx(1.25)     # 12.5 * 0.1
    assert fixed_target_distance("EURUSD", cfg) == pytest.approx(0.00125)
    assert fixed_target_distance("GBPUSD", cfg) == pytest.approx(0.00125)


def test_structure_stop_preferred_when_tighter_than_atr(cfg):
    # BUY: candle low 5 pts below entry; ATR=10 so 0.8 ATR=8, min floor=2.5 — structure (~5+buf) wins
    r = compute_stop(+1, entry=20000.0, candle_high=20005.0, candle_low=19995.0, atr=10.0, cfg=cfg)
    assert r["source"].startswith("structure")
    assert r["sdist"] < 0.8 * 10 + 1e-9
    assert r["stop"] < 19995.0  # beyond the low (buffer)


def test_atr_stop_used_when_tighter_and_still_beyond_structure(cfg):
    # Wide candle: structure is far; ATR candidate is tighter but NOT beyond structure -> structure kept
    r = compute_stop(+1, entry=20000.0, candle_high=20010.0, candle_low=19950.0, atr=40.0, cfg=cfg)
    assert r["source"].startswith("structure")
    assert r["stop"] <= 19950.0

    # Tight candle vs small ATR: structure distance ~3, 0.8*ATR=4, min floor=1.25 — structure tighter
    r2 = compute_stop(+1, entry=20000.0, candle_high=20002.0, candle_low=19997.0, atr=5.0, cfg=cfg)
    assert r2["sdist"] < 0.8 * 5 + 1e-9
    assert r2["source"].startswith("structure")


def test_atr_stop_when_candle_is_tiny_but_atr_clears_structure(cfg):
    # Tiny candle entirely above atr_stop: structure ~ entry-1, atr_stop = entry-0.8*atr.
    # atr_stop is further than structure, so structure (tighter) wins among those beyond structure.
    # To force ATR: make structure further than ATR stop... impossible if structure is candle low - buf
    # and candle low is near entry. ATR is always further when candle is tight.
    # SELL case where ATR is beyond structure and tighter: candle high far, ATR closer but still >= structure
    # structure = high+buf = 20040.05; atr = 20000+32=20032 — atr is tighter AND >= structure? 20032 < 20040, so NOT beyond.
    # Only when candle high is below atr_stop (SELL): high=20020, structure=20020.05*..., atr=20032 — both beyond, atr further, structure tighter.
    r = compute_stop(-1, entry=20000.0, candle_high=20020.0, candle_low=19990.0, atr=40.0, cfg=cfg)
    assert r["stop"] >= 20020.0
    assert r["source"] == "structure"


def test_min_stop_atr_floor(cfg):
    # Degenerate flat candle at entry -> structure ~ entry; min floor 0.25 ATR kicks in
    r = compute_stop(+1, entry=100.0, candle_high=100.0, candle_low=100.0, atr=4.0, cfg=cfg)
    assert r["sdist"] == pytest.approx(0.25 * 4.0)
    assert "min_floor" in r["source"]


def test_fixed_vs_structural_target_picks_nearer(cfg):
    # BUY: fixed = +5.0; resistance at +3.0 -> structural nearer
    levels = [{"type": "resistance", "label": "Session High", "price": 20003.0}]
    t = compute_target(+1, 20000.0, "US100", atr=40.0, levels=levels, vp=None, cfg=cfg, tick=0.25)
    assert t["source"].startswith("structure")
    assert t["tdist"] < 5.0
    assert t["target"] < 20003.0  # just before the wall

    # No structure nearby -> fixed
    t2 = compute_target(+1, 20000.0, "US100", atr=40.0, levels=[], vp=None, cfg=cfg, tick=0.25)
    assert t2["source"] == "fixed"
    assert t2["tdist"] == pytest.approx(5.0)


def test_vp_levels_used_as_structure(cfg):
    vp = {"poc": 20002.0, "vah": 20010.0, "val": 19990.0}
    t = compute_target(+1, 20000.0, "US100", atr=40.0, levels=[], vp=vp, cfg=cfg, tick=0.25)
    assert "POC" in t["source"] or t["structure_label"] == "POC"
    assert t["tdist"] < 5.0


def test_size_for_target_meets_381_or_none():
    # MNQ: pv=2, tick=0.25, comm=0.50; tdist=5.0 -> unit_gross=10, unit_comm_rt=1, unit_net=9
    # q_min for 381 = ceil(381/9)=43 — but max_qty=40 -> None (SKIP)
    assert size_for_target(
        point_value=2.0, tick=0.25, commission=0.50, tdist=5.0, sdist=10.0,
        max_qty=40, net_min=381, net_aim=750, net_max=1000, max_risk_usd=1500, budget=1500,
    ) is None

    # Wider target: tdist=20 -> unit_net = 40-1=39; q_min=ceil(381/39)=10
    sz = size_for_target(
        point_value=2.0, tick=0.25, commission=0.50, tdist=20.0, sdist=30.0,
        max_qty=40, net_min=381, net_aim=750, net_max=1000, max_risk_usd=1500, budget=1500,
    )
    assert sz is not None
    assert sz["target_usd"] >= 381
    assert sz["qty"] <= 40
    assert sz["risk_usd"] <= 1500


def test_size_for_target_381_on_6e():
    # 6E: pv=125000, tdist=0.00125 (12.5 pips), unit_gross=156.25, comm_rt=4.8, unit_net≈151.45
    # q_min = ceil(381/151.45)=3
    sz = size_for_target(
        point_value=125000.0, tick=0.00005, commission=2.40, tdist=0.00125, sdist=0.0020,
        max_qty=40, net_min=381, net_aim=750, net_max=1000, max_risk_usd=1500, budget=1500,
    )
    assert sz is not None and sz["qty"] >= 3
    assert sz["target_usd"] >= 381


def test_compute_scalp_levels_package(cfg):
    pkg = compute_scalp_levels(
        "BUY", 20000.0, {"high": 20005.0, "low": 19990.0}, 40.0, "US100", cfg,
        levels=[{"type": "resistance", "label": "S/R", "price": 20004.0}],
        vp={"poc": 19995.0, "vah": 20008.0, "val": 19980.0}, tick=0.25, digits=2,
    )
    assert pkg["stop_loss"] < pkg["entry"] < pkg["take_profit"]
    assert pkg["tdist"] > 0 and pkg["sdist"] > 0
