"""'bracket' Gemini shadow book: same Gemini signals as main, conf >= 0.75, XAUUSD/EURUSD/US100, fixed 1 ATR stop /
1 ATR target / 90 min / 21:45 UTC flat, Gemini CLOSEs ignored, half-Lucid sizing, independent of main."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals.bracket import BracketBook, bracket_cfg, BOOK, LABEL
from signals.pipeline import lucid_busy_symbols
from signals import gha_loop

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 14, 0, 20, tzinfo=UTC)   # Thu 09:00 New York, in session


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c


def q(sym, price, t=T0, source="dukascopy:tick", age=2.0, stale=False):
    return {"symbol": sym, "price": price, "ts": t.isoformat(), "fetched_at": t.isoformat(), "age_s": age,
            "stale": stale, "source": source}


class Feed:
    """Injectable quotes: a list of (quotes, m1) per call; the last one repeats."""
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), 0

    async def __call__(self, symbols, now):
        i = min(self.calls, len(self.steps) - 1)
        self.calls += 1
        return self.steps[i]


def book(cfg, feed=None):
    bk = BracketBook(cfg, quotes=feed or Feed(({}, {})))
    # recent_trades() drops closes earlier than created_at[:19]. created_at is wall-clock now,
    # so once the clock passes T0 (2026-11-12 14:00 UTC) those test closes vanish from the list.
    bk.s["created_at"] = (T0 - timedelta(hours=1)).isoformat()
    return bk


def sig(sym="XAUUSD", action="SELL", conf=0.8, atr=5.0, entry=3945.0):
    return {"symbol": sym, "action": action, "confidence": conf, "atr": atr, "entry": entry,
            "level_event": "rejection", "session": "NEW_YORK_SESSION"}


def rec(*sigs, t=T0, in_position=()):
    return {"run_at_utc": t.isoformat(), "dry_run": False, "signals": list(sigs), "in_position": list(in_position)}


def m1(rows, start):
    idx = pd.date_range(pd.Timestamp(start).floor("min"), periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def test_config_defaults(cfg):
    b = bracket_cfg(cfg)
    assert b["enabled"] and b["symbols"] == ["XAUUSD", "EURUSD", "US100"]
    assert b["min_confidence"] == 0.75 and b["stop_atr"] == 1.0 and b["target_atr"] == 1.0
    assert b["max_hold_minutes"] == 90 and b["flat_utc"] == "21:45" and b["max_minis"] == 6
    assert b["slippage_ticks"] == 1
    assert "bracket.json" in gha_loop.SITE_JSON
    # main account rules untouched
    assert cfg["lucid"]["exit_mode"] == "gemini_only" and cfg["lucid"]["hard_stop_net_usd"] == 500.0
    assert cfg["lucid"]["max_risk_usd"] == 1500.0


def test_filters(cfg):
    bk = book(cfg)
    assert bk.eligible(sig(conf=0.75)) is None
    assert "confidence" in bk.eligible(sig(conf=0.74))
    assert "symbol" in bk.eligible(sig(sym="GBPUSD"))
    assert "symbol" in bk.eligible(sig(sym="BTCUSD"))
    assert bk.eligible({**sig(), "action": "HOLD"}) == "not BUY/SELL"
    out = bk.intake(rec(sig(conf=0.7), sig(sym="GBPUSD", conf=0.9), sig(sym="BTCUSD", conf=0.9),
                        {**sig(sym="EURUSD"), "action": "HOLD"}, sig(sym="US100", action="BUY", conf=0.76, atr=40.0,
                                                                       entry=28000.0)), T0)
    assert [p["symbol"] for p in out] == ["US100"]
    assert bk.s["stats"]["signals_seen"] == 4      # HOLD is not a signal
    assert bk.s["stats"]["skips"] == {"confidence<min": 1, "symbol": 2}


def test_sizing_half_lucid(cfg):
    bk = book(cfg)
    assert bk.size("GC", 5.0) == (1, None)           # 1 GC x 5 pts x $100 = $500 (2 would be $1,000 > $900)
    assert bk.size("GC", 4.0) == (2, None)           # $400 each -> 2 = $800
    assert bk.size("NQ", 40.0) == (1, None)          # $800
    assert bk.size("NQ", 20.0) == (2, None)          # $400 each -> 2
    assert bk.size("6E", 0.0008) == (6, None)        # $100 each -> 9, capped at 6 full-size
    q_, why = bk.size("NQ", 80.0)                    # 1 NQ = $1,600 > $1,500 -> skip
    assert q_ is None and "risks" in why
    bk.s["positions"].append({"symbol": "EURUSD", "qty": 5, "contract": "6E"})
    assert bk.size("GC", 4.0) == (1, None)           # only 1 left of 6
    bk.s["positions"][0]["qty"] = 6
    assert bk.size("GC", 4.0)[0] is None


def test_fill_live_quote_plus_tick_and_target(cfg):
    feed = Feed(({"XAUUSD": q("XAUUSD", 3945.03)}, {}),
                ({"XAUUSD": q("XAUUSD", 3940.0, T0 + timedelta(minutes=5))}, {}))
    bk = book(cfg, feed)
    bk.intake(rec(sig()), T0)
    r = asyncio.run(bk.tick(T0))
    p = bk.s["positions"][0]
    assert r["opened"] == [1]
    assert p["entry"] == pytest.approx(3944.9)       # SELL: 3945.03 - 1 tick (0.10) = 3944.93 -> rounded DOWN 3944.9
    assert p["stop"] == pytest.approx(3949.9) and p["target"] == pytest.approx(3939.9)
    assert p["qty"] == 1 and p["contract"] == "GC"
    assert bk.s["balance"] == pytest.approx(100000 - 2.30)
    # 5 min later price 3940.0: not yet at the 3939.9 target
    asyncio.run(bk.tick(T0 + timedelta(minutes=5)))
    assert bk.s["positions"]
    feed.steps.append(({"XAUUSD": q("XAUUSD", 3939.8, T0 + timedelta(minutes=6))}, {}))
    asyncio.run(bk.tick(T0 + timedelta(minutes=6)))
    assert not bk.s["positions"]
    t = bk.recent_trades()[0]
    assert t["reason"] == "target_1atr" and t["exit"] == pytest.approx(3940.0)   # level 3939.9 + 1 adverse tick
    assert t["net"] == pytest.approx((3944.9 - 3940.0) * 100 - 4.60)
    assert bk.s["balance"] == pytest.approx(100000 + t["net"])
    lines = (Path(cfg["general"]["logs_dir"]) / "2026-11-12" / "bracket_trades.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["book"] == BOOK


def test_stop_on_m1_path_and_same_bar_takes_stop(cfg):
    t1 = T0 + timedelta(minutes=3)
    bars = m1([[3945, 3946, 3944, 3945], [3945, 3946, 3944, 3945], [3946, 3951, 3938, 3940]], T0 + timedelta(minutes=1))
    feed = Feed(({"XAUUSD": q("XAUUSD", 3945.0)}, {}), ({"XAUUSD": q("XAUUSD", 3940.0, t1)}, {"XAUUSD": bars}))
    bk = book(cfg, feed)
    bk.intake(rec(sig()), T0)
    asyncio.run(bk.tick(T0))
    asyncio.run(bk.tick(t1))
    t = bk.recent_trades()[0]
    assert t["reason"] == "stop_1atr" and t["stopped"]   # bar touched stop 3949.9 and target 3939.9 -> loss
    assert t["entry"] == pytest.approx(3944.9) and t["stop"] == pytest.approx(3949.9)
    assert t["exit"] == pytest.approx(3950.0)            # stop level 3949.9 + 1 adverse tick
    assert t["net"] == pytest.approx(-(t["exit"] - t["entry"]) * 100 - 4.60)


def test_live_stop_resting_vs_gap(cfg):
    feed = Feed(({"US100": q("US100", 28000.0)}, {}),
                ({"US100": q("US100", 28060.0, T0 + timedelta(seconds=18))}, {}))
    bk = book(cfg, feed)
    bk.intake(rec(sig(sym="US100", action="SELL", atr=40.0, entry=28000.0)), T0)
    asyncio.run(bk.tick(T0))
    p = bk.s["positions"][0]
    assert p["entry"] == 27999.75 and p["stop"] == 28039.75 and p["target"] == 27959.75
    asyncio.run(bk.tick(T0 + timedelta(seconds=18)))
    t = bk.recent_trades()[0]
    assert t["reason"] == "stop_1atr" and t["exit"] == 28040.0     # resting stop: level + 1 tick, not 28060
    assert t["net"] == pytest.approx(-(28040.0 - 27999.75) * 20 - 3.50)


def test_time_exit_and_flat(cfg):
    feed = Feed(({"EURUSD": q("EURUSD", 1.15)}, {}))
    bk = book(cfg, feed)
    bk.intake(rec(sig(sym="EURUSD", action="BUY", atr=0.0008, entry=1.15)), T0)
    asyncio.run(bk.tick(T0))
    p = bk.s["positions"][0]
    assert p["qty"] == 6 and p["entry"] == pytest.approx(1.15005)
    late = T0 + timedelta(minutes=90, seconds=1)
    feed.steps = [({"EURUSD": q("EURUSD", 1.1502, late)}, {})]
    asyncio.run(bk.tick(late))
    t = bk.recent_trades()[0]
    assert t["reason"] == "time_90min" and t["exit"] == pytest.approx(1.15015)
    # flat at 21:45 UTC
    t2 = datetime(2026, 11, 12, 21, 0, tzinfo=UTC)
    feed.steps = [({"EURUSD": q("EURUSD", 1.15, t2)}, {})]
    bk.intake(rec(sig(sym="EURUSD", action="BUY", atr=0.0008, entry=1.15), t=t2), t2)
    asyncio.run(bk.tick(t2))
    assert bk.s["positions"]
    t3 = datetime(2026, 11, 12, 21, 45, 5, tzinfo=UTC)
    feed.steps = [({"EURUSD": q("EURUSD", 1.15, t3)}, {})]
    asyncio.run(bk.tick(t3))
    assert bk.recent_trades()[0]["reason"] == "flat_2145utc"
    # no new entries in the last 15 minutes before the flat
    t4 = datetime(2026, 11, 12, 21, 31, tzinfo=UTC)
    assert bk.intake(rec(sig(sym="EURUSD", action="BUY", atr=0.0008, entry=1.15), t=t4), t4) == []


def test_one_per_symbol_and_independent_of_main(cfg, tmp_path):
    st = Path(cfg["general"]["state_dir"])
    (st / "lucid").mkdir(parents=True, exist_ok=True)
    acct = {"positions": [{"symbol": "XAUUSD", "id": 7}], "pending": []}
    (st / "lucid" / "account.json").write_text(json.dumps(acct))
    before = (st / "lucid" / "account.json").read_text()
    feed = Feed(({"XAUUSD": q("XAUUSD", 3945.0), "US100": q("US100", 28000.0)}, {}))
    bk = book(cfg, feed)
    # main holds XAUUSD: the bracket book still takes an XAUUSD signal (independent positions)
    asyncio.run(bk.on_cycle(rec(sig(), sig(sym="US100", action="BUY", atr=40.0, entry=28000.0)), T0))
    asyncio.run(bk.tick(T0))
    assert sorted(p["symbol"] for p in bk.s["positions"]) == ["US100", "XAUUSD"]
    # a repeat signal while the bracket book holds the symbol is skipped
    t1 = T0 + timedelta(minutes=2)
    assert bk.intake(rec(sig(), t=t1), t1) == []
    assert bk.s["stats"]["skips"]["held_in_bracket"] == 1
    # bracket positions never reach main's entry-scan exclusion (only main's account is read)
    assert lucid_busy_symbols(cfg, st) == {"XAUUSD"}
    assert (st / "lucid" / "account.json").read_text() == before
    # missed scans: main excluded EURUSD while the bracket book was flat on it
    t2 = T0 + timedelta(minutes=4)
    bk.intake(rec(t=t2, in_position=["EURUSD", "XAUUSD", "GBPUSD"]), t2)
    assert bk.s["stats"]["missed_scans"] == {"EURUSD": 1}
    site = json.loads((tmp_path / "bracket.json").read_text())
    assert site["label"] == LABEL and site["account"]["open_positions"] == 2


def test_gemini_close_requests_ignored(cfg):
    st = Path(cfg["general"]["state_dir"])
    (st / "lucid").mkdir(parents=True, exist_ok=True)
    feed = Feed(({"XAUUSD": q("XAUUSD", 3945.0)}, {}))
    bk = book(cfg, feed)
    bk.intake(rec(sig()), T0)
    asyncio.run(bk.tick(T0))
    (st / "lucid" / "close_requests.json").write_text(json.dumps([{"id": 1, "symbol": "XAUUSD"}]))
    feed.steps = [({"XAUUSD": q("XAUUSD", 3945.0, T0 + timedelta(minutes=1))}, {})]
    asyncio.run(bk.tick(T0 + timedelta(minutes=1)))
    assert len(bk.s["positions"]) == 1


def test_stale_quote_waits_then_expires_and_chase(cfg):
    feed = Feed(({"XAUUSD": q("XAUUSD", 3945.0, source="dukascopy:m1")}, {}))
    bk = book(cfg, feed)
    bk.intake(rec(sig()), T0)
    asyncio.run(bk.tick(T0 + timedelta(seconds=18)))
    assert bk.s["pending"] and not bk.s["positions"]
    asyncio.run(bk.tick(T0 + timedelta(seconds=200)))
    assert not bk.s["pending"] and not bk.s["positions"]
    # chase: SELL signal at 3945, fill 3940.9 is 4.1 pts (> 0.5 ATR = 2.5) worse for a short -> skipped
    feed.steps = [({"XAUUSD": q("XAUUSD", 3941.0)}, {})]
    bk.intake(rec(sig(), t=T0 + timedelta(minutes=5)), T0 + timedelta(minutes=5))
    asyncio.run(bk.tick(T0 + timedelta(minutes=5)))
    assert not bk.s["positions"] and bk.s["stats"]["skips"]["chase > max_chase_atr"] == 1


def test_state_persists_and_reloads(cfg):
    feed = Feed(({"XAUUSD": q("XAUUSD", 3945.0)}, {}))
    bk = book(cfg, feed)
    asyncio.run(bk.on_cycle(rec(sig()), T0))
    bk2 = book(cfg, feed)
    assert len(bk2.s["positions"]) == 1 and bk2.s["positions"][0]["book"] == BOOK
    # the same cycle record is never taken twice
    assert bk2.intake(rec(sig()), T0) == []


def test_slip_rounding_is_adverse(cfg):
    from signals.bracket import round_adverse
    # US100 short at a 27937.97 quote: 27937.72 must round DOWN to 27937.50, never up to 27937.75
    assert round_adverse(27937.97 - 0.25, 0.25, -1, True) == 27937.5
    assert round_adverse(27937.97 + 0.25, 0.25, 1, True) == 27938.25      # long entry rounds up
    assert round_adverse(27937.97 + 0.25, 0.25, -1, False) == 27938.25    # short exit (buy) rounds up
    assert round_adverse(27937.97 - 0.25, 0.25, 1, False) == 27937.5      # long exit (sell) rounds down
    assert round_adverse(3944.9, 0.1, -1, True) == pytest.approx(3944.9)  # on-tick prices unchanged

def test_close_tags_exit_prompt_version(cfg):
    from signals.exit_versions import GEMINI_EXIT_PROMPT_VERSION
    bk = book(cfg)
    p = {"id": 1, "symbol": "XAUUSD", "contract": "GC", "qty": 1, "side": -1,
         "entry": 3944.9, "stop": 3949.9, "target": 3939.9, "entry_time": T0.isoformat(),
         "risk_usd": 500.0, "atr": 5.0, "mfe": 0, "mae": 0, "checks": 0}
    bk.s["positions"] = [p]
    rec = bk.close(p, 3950.0, T0 + timedelta(minutes=3), "stop_1atr")
    assert rec["exit_prompt_version"] == GEMINI_EXIT_PROMPT_VERSION
    assert rec["reason"] == "stop_1atr" and rec["event"] == "close"

