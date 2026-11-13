"""Went-positive-first: fee-aware first cross of $0, from ticks / M1 / the panel ledger."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from signals.config import load_config
from signals.first_cross import (
    YES, NO, UNKNOWN, classify, first_cross_from_history, first_cross_from_m1,
    first_cross_from_prints, last_prints_every, book_share, display_of, net_usd,
    observe_print,
)
from signals.panel import PanelLedger, write_reset_marker

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)


def _prints(pairs):
    return [(datetime(2026, 11, 12, 18, 0, tzinfo=UTC) + timedelta(seconds=s), px) for s, px in pairs]


# NQ: $20/pt, RT fees $3.50. 1 tick = 0.25 pts = $5. Fees are covered by the first tick in favor.
NQ = dict(side=1, entry=20000.0, qty=1, pv=20.0, fees=3.50)


def test_classify_fee_cushion():
    # net = gross - 3.50; pos only when gross > 3.50; neg only when gross < 0
    assert classify(-3.50, 3.50) is None          # entry fill
    assert classify(-0.01, 3.50) is None          # still in cushion
    assert classify(0.01, 3.50) == "pos"
    assert classify(-3.51, 3.50) == "neg"         # gross just below $0
    assert classify(100, 3.50) == "pos"
    assert net_usd(1, 20000, 20000, 1, 20, 3.50) == -3.50
    assert net_usd(1, 20000, 20000.25, 1, 20, 3.50) == 1.50   # +1 tick, covers fees


def test_prints_yes_when_price_covers_fees_first():
    # long: +1 tick then -2 ticks
    r = first_cross_from_prints(
        _prints([(10, 20000.25), (20, 19999.50)]),
        entry_time=T0, **NQ, basis="tick_path")
    assert r["went_positive_first"] == YES and r["first_cross"] == "pos"
    assert r["first_cross_basis"] == "tick_path"
    assert r["first_cross_at"].startswith("2026-11-12T18:00:10")


def test_prints_no_when_price_goes_against_first():
    r = first_cross_from_prints(
        _prints([(10, 19999.75), (20, 20010.0)]),
        entry_time=T0, **NQ)
    assert r["went_positive_first"] == NO and r["first_cross"] == "neg"


def test_prints_ignore_entry_instant_and_fee_cushion():
    # entry print at T0, then a 0.10 pt rise ($2) which does not clear $3.50 fees
    r = first_cross_from_prints(
        [(T0, 20000.0), (T0 + timedelta(seconds=5), 20000.10)],
        entry_time=T0, **NQ)
    assert r["went_positive_first"] is None and r["first_cross"] is None


def test_prints_skip_after_exit():
    # Adverse move is only after the exit: ignore it.
    r = first_cross_from_prints(
        _prints([(30, 19999.0)]),
        entry_time=T0, exit_time=T0 + timedelta(seconds=20), **NQ)
    assert r["went_positive_first"] is None
    # Same path, but the adverse print lands before exit.
    r2 = first_cross_from_prints(
        _prints([(10, 19999.0), (30, 20010.0)]),
        entry_time=T0, exit_time=T0 + timedelta(seconds=20), **NQ)
    assert r2["went_positive_first"] == NO


def test_m1_single_side_uses_ohlc():
    # bar wholly after entry: low never goes below entry, high clears fees
    idx = pd.to_datetime(["2026-11-12T18:06:00Z"])
    bars = pd.DataFrame({"open": [20000.05], "high": [20005.0], "low": [20000.02], "close": [20004.0]},
                        index=idx)
    r = first_cross_from_m1(bars, entry_time=T0 + timedelta(minutes=5), **NQ)
    assert r["went_positive_first"] == YES and r["first_cross_basis"] == "m1_ohlc"


def test_m1_both_sides_without_ticks_is_unknown():
    idx = pd.to_datetime(["2026-11-12T18:06:00Z"])
    bars = pd.DataFrame({"open": [20000.0], "high": [20005.0], "low": [19990.0], "close": [20001.0]},
                        index=idx)
    r = first_cross_from_m1(bars, entry_time=T0 + timedelta(minutes=5), **NQ)
    assert r["went_positive_first"] == UNKNOWN and r["first_cross"] == "ambiguous"


def test_m1_both_sides_resolved_by_ticks():
    idx = pd.to_datetime(["2026-11-12T18:06:00Z"])
    bars = pd.DataFrame({"open": [20000.0], "high": [20005.0], "low": [19990.0], "close": [19995.0]},
                        index=idx)
    ticks = pd.DataFrame(
        {"bid": [20000.1, 20004.0, 19990.0]},
        index=pd.to_datetime(["2026-11-12T18:06:01Z", "2026-11-12T18:06:08Z", "2026-11-12T18:06:20Z"]))
    r = first_cross_from_m1(bars, entry_time=T0 + timedelta(minutes=5), ticks=ticks, **NQ)
    assert r["went_positive_first"] == YES and r["first_cross_basis"] == "tick_path"


def test_entry_bar_ignores_pre_entry_adverse_spike():
    # Entry at 18:05:30. The 18:05 bar dipped to 19950 before entry (would be a huge MAE if trusted)
    # and closed at 20004 after entry. Without ticks we must not call this "No".
    idx = pd.to_datetime(["2026-11-12T18:05:00Z"])
    bars = pd.DataFrame({"open": [19950.0], "high": [20004.0], "low": [19950.0], "close": [20004.0]},
                        index=idx)
    et = datetime(2026, 11, 12, 18, 5, 30, tzinfo=UTC)
    r = first_cross_from_m1(bars, entry_time=et, **NQ)
    # Both H and L would classify, so without ticks this is ambiguous, not a false "No".
    assert r["went_positive_first"] in (UNKNOWN, YES, None)
    assert r["first_cross"] != "neg"


def test_observe_does_not_overwrite_and_skips_entry():
    row = {"entry_time": T0.isoformat(), "went_positive_first": None, "first_cross": None}
    assert observe_print(row, 20000.0, T0, **NQ) is False
    assert row["went_positive_first"] is None
    assert observe_print(row, 19999.0, T0 + timedelta(seconds=3), **NQ) is True
    assert row["went_positive_first"] == NO
    # later profit must not overwrite
    assert observe_print(row, 20020.0, T0 + timedelta(seconds=9), **NQ) is False
    assert row["went_positive_first"] == NO


def test_book_share_excludes_pending_and_unknown():
    rows = [
        {"went_positive_first": "yes"},
        {"went_positive_first": "yes"},
        {"went_positive_first": "no"},
        {"went_positive_first": None},
        {"went_positive_first": "?"},
    ]
    s = book_share(rows)
    assert s["went_positive_first"] == "67% (2/3)"
    assert s["went_positive_first_yes"] == 2 and s["went_positive_first_n"] == 3
    assert book_share([])["went_positive_first"] is None
    assert display_of({"went_positive_first": "yes"}) == "Yes"
    assert display_of({"went_positive_first": None}) == "—"
    assert display_of({"went_positive_first": "?"}) == "?"


def _cfg():
    cfg = load_config()
    cfg["reset"] = {"token": "tok-1"}
    return cfg


def test_ledger_live_tick_no_then_profit(tmp_path):
    cfg = _cfg()
    st, lg = tmp_path / "state", tmp_path / "logs"
    (st / "lucid").mkdir(parents=True)
    write_reset_marker(cfg, st, T0)
    pos = {"id": 1, "symbol": "US100", "contract": "NQ", "qty": 1, "side": 1, "entry": 20000.0,
           "entry_time": "2026-11-12T18:05:00+00:00", "atr": 40.0}
    (st / "lucid" / "account.json").write_text(json.dumps({"account": {"balance": 100000.0}, "positions": [pos]}))
    px = {"v": 19999.0}  # -$20 gross, already below $0

    async def q(syms):
        return {"US100": {"price": px["v"]}}

    led = PanelLedger(cfg, state_dir=st, logs_dir=lg, site_dir=tmp_path, quote_fn=q, history_fn=False)
    t1 = datetime(2026, 11, 12, 18, 6, tzinfo=UTC)
    asyncio.run(led.tick(t1))
    row = next(iter(led.s["rows"].values()))
    assert row["went_positive_first"] == "no"
    px["v"] = 20020.0
    asyncio.run(led.tick(t1 + timedelta(minutes=1)))
    row = next(iter(led.s["rows"].values()))
    assert row["went_positive_first"] == "no"  # locked in


def test_ledger_open_still_in_cushion_is_dash(tmp_path):
    cfg = _cfg()
    st, lg = tmp_path / "state", tmp_path / "logs"
    (st / "lucid").mkdir(parents=True)
    write_reset_marker(cfg, st, T0)
    pos = {"id": 1, "symbol": "US100", "contract": "NQ", "qty": 1, "side": 1, "entry": 20000.0,
           "entry_time": "2026-11-12T18:05:00+00:00"}
    (st / "lucid" / "account.json").write_text(json.dumps({"account": {"balance": 100000.0}, "positions": [pos]}))

    async def q(syms):
        return {"US100": {"price": 20000.10}}  # +$2 gross, inside $3.50 cushion

    led = PanelLedger(cfg, state_dir=st, logs_dir=lg, site_dir=tmp_path, quote_fn=q, history_fn=False)
    asyncio.run(led.tick(datetime(2026, 11, 12, 18, 6, tzinfo=UTC)))
    site = json.loads((tmp_path / "panel.json").read_text())
    r = site["positions"][0]
    assert r["went_positive_first"] is None and r["went_positive_first_label"] == "—"
    main = next(p for p in site["performance"] if p["book"] == "MAIN" and p["venue"] == "LUCID")
    assert main["went_positive_first"] is None and main["open_positions"] == 1


def test_ledger_backfill_from_m1(tmp_path):
    cfg = _cfg()
    st, lg = tmp_path / "state", tmp_path / "logs"
    (st / "lucid").mkdir(parents=True)
    write_reset_marker(cfg, st, T0)
    (st / "lucid" / "account.json").write_text(json.dumps({"account": {"balance": 99800.0}, "positions": []}))
    d = lg / "2026-11-12"; d.mkdir(parents=True)
    (d / "lucid_trades.jsonl").write_text(json.dumps({
        "t_exit": "2026-11-12T18:20:00+00:00", "id": 1, "symbol": "US100", "contract": "NQ", "qty": 1,
        "side": "LONG", "entry_time": "2026-11-12T18:05:00+00:00", "entry": 20000.0, "exit": 19980.0,
        "reason": "stop_1atr", "gross": -400.0, "net": -403.5}) + "\n")

    idx = pd.to_datetime(["2026-11-12T18:06:00Z", "2026-11-12T18:07:00Z"])
    # first full bar after entry: only favorable; later bar hits the stop
    m1 = pd.DataFrame({
        "open": [20000.05, 20004.0], "high": [20008.0, 20008.0],
        "low": [20000.02, 19980.0], "close": [20004.0, 19980.0],
    }, index=idx)

    async def hist(sym, start, end, want_ticks=False):
        return {"m1": m1, "ticks": None}

    async def q(syms):
        return {}

    led = PanelLedger(cfg, state_dir=st, logs_dir=lg, site_dir=tmp_path, quote_fn=q, history_fn=hist)
    asyncio.run(led.tick(datetime(2026, 11, 12, 18, 21, tzinfo=UTC)))
    site = json.loads((tmp_path / "panel.json").read_text())
    r = site["positions"][0]
    assert r["went_positive_first"] == "yes" and r["first_cross_basis"] == "m1_ohlc"
    main = next(p for p in site["performance"] if p["book"] == "MAIN" and p["venue"] == "LUCID")
    assert main["went_positive_first"] == "100% (1/1)"


def test_ledger_backfill_ambiguous_without_ticks(tmp_path):
    cfg = _cfg()
    st, lg = tmp_path / "state", tmp_path / "logs"
    (st / "lucid").mkdir(parents=True)
    write_reset_marker(cfg, st, T0)
    (st / "lucid" / "account.json").write_text(json.dumps({"account": {"balance": 100000.0}, "positions": []}))
    d = lg / "2026-11-12"; d.mkdir(parents=True)
    (d / "lucid_trades.jsonl").write_text(json.dumps({
        "t_exit": "2026-11-12T18:20:00+00:00", "id": 1, "symbol": "US100", "contract": "NQ", "qty": 1,
        "side": "LONG", "entry_time": "2026-11-12T18:05:00+00:00", "entry": 20000.0, "exit": 20000.0,
        "reason": "max_hold_90m", "gross": 0.0, "net": -3.5}) + "\n")
    idx = pd.to_datetime(["2026-11-12T18:06:00Z"])
    m1 = pd.DataFrame({"open": [20000.0], "high": [20010.0], "low": [19990.0], "close": [20000.0]}, index=idx)

    async def hist(sym, start, end, want_ticks=False):
        return {"m1": m1, "ticks": None}

    async def q(syms):
        return {}

    led = PanelLedger(cfg, state_dir=st, logs_dir=lg, site_dir=tmp_path, quote_fn=q, history_fn=hist)
    asyncio.run(led.tick(datetime(2026, 11, 12, 18, 21, tzinfo=UTC)))
    r = json.loads((tmp_path / "panel.json").read_text())["positions"][0]
    assert r["went_positive_first"] == "?" and r["went_positive_first_label"] == "?"


def test_15s_last_print_ignores_immediate_flicker():
    """A 10 ms adverse flicker that has reversed by the first 15s mark is not 'No'."""
    idx = pd.to_datetime([
        "2026-11-12T18:05:00.010Z",  # 10ms: flicker against
        "2026-11-12T18:05:01.000Z",
        "2026-11-12T18:05:14.800Z",  # last print of first 15s: in profit
        "2026-11-12T18:05:20.000Z",
    ])
    ticks = pd.DataFrame({"bid": [19999.0, 20000.2, 20005.0, 20006.0]}, index=idx)
    et = datetime(2026, 11, 12, 18, 5, 0, tzinfo=UTC)
    prints = last_prints_every(ticks, et, period_s=15)
    assert prints[0][1] == 20005.0
    r = first_cross_from_history(None, entry_time=et, ticks=ticks, **NQ)
    assert r["went_positive_first"] == YES and r["first_cross_basis"] == "tick_15s"


def test_15s_last_print_counts_a_move_that_lasts():
    idx = pd.to_datetime([
        "2026-11-12T18:05:00.010Z",
        "2026-11-12T18:05:14.800Z",  # still against at the first 15s mark
        "2026-11-12T18:05:30.000Z",  # later recovers
    ])
    ticks = pd.DataFrame({"bid": [19999.0, 19998.0, 20010.0]}, index=idx)
    et = datetime(2026, 11, 12, 18, 5, 0, tzinfo=UTC)
    r = first_cross_from_history(None, entry_time=et, ticks=ticks, **NQ)
    assert r["went_positive_first"] == NO and r["first_cross_basis"] == "tick_15s"
