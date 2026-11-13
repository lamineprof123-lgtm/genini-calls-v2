"""LucidPro 100K eval-passing replay (display only). No network."""
from datetime import datetime, timezone

from signals.eval_pass import eval_pass_stats, is_lucid_tradable, START, PROFIT_TARGET, DLL, MLL

UTC = timezone.utc


def T(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC) if "T" in s else s


def trade(net, t_exit, symbol="XAUUSD"):
    return {"net": net, "t_exit": t_exit, "symbol": symbol}


# Session helpers (November 2026 is EST, UTC-5):
# Thu 12 Nov 18:00 UTC = 13:00 ET → Lucid day 2026-11-12
# Thu 12 Nov 21:45 UTC = 16:45 ET → EOD of 2026-11-12
# Thu 12 Nov 23:00 UTC = 18:00 ET → Lucid day 2026-11-13
DAY1 = "2026-11-12T18:00:00+00:00"          # Thu 13:00 ET, day 12
DAY1_LATE = "2026-11-12T20:00:00+00:00"     # Thu 15:00 ET, still day 12
EOD1 = datetime(2026, 11, 12, 21, 45, tzinfo=UTC)
DAY2 = "2026-11-13T15:00:00+00:00"          # Fri 10:00 ET, day 13
DAY2_LATE = "2026-11-13T20:00:00+00:00"     # Fri 15:00 ET, day 13
AFTER_D2 = datetime(2026, 11, 13, 22, 0, tzinfo=UTC)  # Fri 17:00 ET, past EOD 13
DAY3 = "2026-11-16T15:00:00+00:00"          # Mon 10:00 ET, day 16


def test_btc_and_blank_are_not_lucid_tradable():
    assert is_lucid_tradable("XAUUSD") and is_lucid_tradable("US100") and is_lucid_tradable("EURUSD")
    assert is_lucid_tradable("GBPUSD") and is_lucid_tradable("XAGUSD")
    assert not is_lucid_tradable("BTCUSD")
    assert not is_lucid_tradable("BTC/USD")
    assert not is_lucid_tradable("btcusdt")
    assert not is_lucid_tradable("XBTUSD")
    assert not is_lucid_tradable("") and not is_lucid_tradable(None)


def test_no_trades_is_emdash_and_zero_progress():
    s = eval_pass_stats([])
    assert s["passing_pct"] is None and s["passing_label"] is None
    assert s["attempts_finished"] == 0 and s["passes"] == 0 and s["fails"] == 0
    assert s["current_profit"] == 0 and s["current_progress_pct"] == 0
    assert s["mll_buffer"] == MLL and s["mll_floor"] == START - MLL and not s["mll_locked"]
    assert s["lucid_trades"] == 0


def test_open_pnl_only_on_unfinished_first_attempt():
    s = eval_pass_stats([], open_pnl=300.0)
    assert s["current_profit"] == 300.0 and s["current_progress_pct"] == 5.0
    assert s["mll_buffer"] == 3300.0 and s["passing_label"] is None


def test_btc_trades_are_excluded():
    s = eval_pass_stats([
        trade(-500, DAY1, "BTCUSD"),
        trade(200, DAY1, "XAUUSD"),
    ])
    assert s["lucid_trades"] == 1 and s["current_profit"] == 200 and s["passing_label"] is None


def test_dll_fail_then_restart():
    # -1800 on day 1 fails; next trade starts a new $100k attempt.
    s = eval_pass_stats([
        trade(-1000, DAY1),
        trade(-800, DAY1_LATE),
        trade(250, DAY2),
    ])
    assert s["fails"] == 1 and s["passes"] == 0 and s["passing_label"] == "0/1"
    assert s["passing_pct"] == 0.0
    assert s["current_profit"] == 250 and s["current_progress_pct"] == round(100 * 250 / 6000, 1)
    assert s["mll_buffer"] == 3250.0


def test_eod_mll_fail_across_two_days():
    # -$1,500 each of two days never trips the DLL; EOD of day 2 is exactly at the $97,000 floor.
    s = eval_pass_stats([
        trade(-1500, DAY1),
        trade(-1500, DAY2),
    ], now=AFTER_D2)
    assert s["fails"] == 1 and s["passes"] == 0 and s["passing_label"] == "0/1"
    assert s["current_profit"] == 0 and s["mll_buffer"] == MLL  # restarted


def test_eod_mll_fail_triggered_by_next_days_first_trade():
    # Same two losing days, but the EOD is applied when Monday's trade arrives; that trade starts the next attempt.
    s = eval_pass_stats([
        trade(-1500, DAY1),
        trade(-1500, DAY2),
        trade(400, DAY3),
    ])
    assert s["fails"] == 1 and s["passing_label"] == "0/1"
    assert s["current_profit"] == 400


def test_mll_not_failed_intraday_before_eod():
    # -$3,000 in one day would also trip DLL first. Spread just under DLL, still below the floor mid-day:
    # day 1 -1500 (floor stays 97k), day 2 -1600 → balance 96,900, but EOD has not run yet.
    s = eval_pass_stats([
        trade(-1500, DAY1),
        trade(-1600, DAY2),
    ], now=datetime(2026, 11, 13, 20, 0, tzinfo=UTC))  # 15:00 ET, before 16:45
    assert s["attempts_finished"] == 0
    assert s["current_profit"] == -3100
    assert s["mll_buffer"] == (START - 3100) - (START - MLL)  # -100, still unfinished


def test_pass_at_6000_then_restart():
    s = eval_pass_stats([
        trade(2500, DAY1),
        trade(3500, DAY1_LATE),
        trade(100, DAY2),
    ])
    assert s["passes"] == 1 and s["fails"] == 0 and s["passing_label"] == "1/1"
    assert s["passing_pct"] == 100.0
    assert s["current_profit"] == 100 and s["mll_floor"] == START - MLL


def test_trailing_floor_then_eod_fail():
    # Day 1 closes +2,000 → highest EOD 102,000, floor trails to 99,000.
    # Day 2 loses 3,000 (but only 1,700 realized that day to stay under DLL) wait:
    # Day 2 can lose at most 1,799 without DLL. 102,000 - 1,799 = 100,201, still above 99,000.
    # Need more days: day 2 -1700 → 100,300, EOD floor still 99,000 (highest is 102k).
    # Day 3 -1700 → 98,600 <= 99,000 at EOD → fail. Day-3 realized is only -1700, no DLL.
    s = eval_pass_stats([
        trade(2000, DAY1),
        trade(-1700, DAY2),
        trade(-1700, DAY3),
    ], now=datetime(2026, 11, 16, 22, 0, tzinfo=UTC))
    assert s["fails"] == 1 and s["passes"] == 0 and s["current_profit"] == 0
    assert s["mll_floor"] == START - MLL  # fresh attempt


def test_floor_locks_at_100100_after_eod_above_103100():
    s = eval_pass_stats([
        trade(3200, DAY1),   # 103,200 EOD > 103,100 → lock at 100,100
    ], now=EOD1)
    assert s["attempts_finished"] == 0
    assert s["current_profit"] == 3200
    assert s["mll_locked"] is True and s["mll_floor"] == 100100.0
    assert s["mll_buffer"] == 3100.0  # 103200 - 100100


def test_multiple_attempts_and_open_pnl():
    s = eval_pass_stats([
        trade(6000, DAY1),             # pass 1
        trade(-1800, DAY2),            # fail 1
        trade(500, DAY3),
    ], open_pnl=-50.0)
    assert s["passing_label"] == "1/2" and s["passing_pct"] == 50.0
    assert s["current_profit"] == 450 and s["current_progress_pct"] == 7.5
    assert s["dll_used"] == 0.0  # day net +50 after open? 500 - 50 = +450, dll_used 0


def test_negative_current_progress():
    s = eval_pass_stats([trade(-600, DAY1)])
    assert s["passing_label"] is None
    assert s["current_profit"] == -600
    assert s["current_progress_pct"] == -10.0
    assert s["mll_buffer"] == 2400.0
    assert s["dll_used"] == 600.0


def test_open_pnl_ignored_after_just_finished_pass():
    s = eval_pass_stats([trade(6000, DAY1)], open_pnl=200.0)
    assert s["passing_label"] == "1/1"
    assert s["current_profit"] == 0 and s["current_equity"] == START
    assert s["mll_buffer"] == MLL
