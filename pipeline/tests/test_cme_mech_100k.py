"""t347u mechanical CME books on Lucid 100K: trade-for-trade vs the reference script
(/workspace/orb_vote/orb_vote_optimize_blowok_100k.py on the NinjaTrader CME 1-min bars),
the 100K eval tracker, and the panel. Existing Lucid 50K books are not modified. No network, no model calls."""
import asyncio, copy, json, sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import cme_mech as C
from signals.config import load_config

DATA = Path(__file__).resolve().parent / "data"
UTC = timezone.utc
REF = json.loads((DATA / "cme_mech_ref_cases_100k.json").read_text())
K100 = [b for b in C.BOOKS if b.startswith("100K-")]


def _bars(path, m):
    df = pd.read_csv(path)
    df = df[df.m == m].drop(columns=["m"])
    df["ts_ny"] = pd.to_datetime(df.ts_ny, utc=True).dt.tz_convert(C.ET)
    return C.prep_bars(df)


REF_BARS = {m: _bars(DATA / "cme_mech_ref_bars_100k.csv.gz", m) for m in ("GC", "NQ", "ES")}
# gold 08:20 cases also need the 50K bar file (shared sessions); merge if a market/day is missing
B50 = {m: _bars(DATA / "cme_mech_ref_bars.csv.gz", m) for m in ("GC", "NQ", "ES")}
for m in REF_BARS:
    if len(B50[m]):
        REF_BARS[m] = C.prep_bars(pd.concat([REF_BARS[m], B50[m]], ignore_index=True).drop_duplicates("ts_ny"))


def _leg(book, case):
    legs = C.BOOKS[book]["legs"]
    if book == "100K-GOLD-COMBO":
        return next(l for l in legs if l["leg"] == case.get("leg") or l["m"] == case.get("m"))
    return legs[0]


def _ref_trade(case):
    book = case["book"]
    spec = C.BOOKS[book]
    # standalone books: single leg
    leg = spec["legs"][0]
    m = C.leg_market(book, leg)
    df = REF_BARS[m]
    g = df[df.date.astype(str) == case["session"]].reset_index(drop=True)
    dl = C.daily_stats(REF["daily"][m])
    prev = C.prev_stats(dl, case["session"])
    info = C.votes_for(g, leg["O"], m, prev, C.ema5(df, leg["O"]))
    dec = C.decide(leg, info, m)
    D = C.hm_add(leg["O"], 30)
    path = g[(g.hm > D) & (g.hm <= C.CAP)]
    r = C.walk(path, dec["side"], dec["stop_pts"], dec["tgt_pts"], C.MK[m]["tick"], leg["n"], C.MK[m]["pv"],
               final=True, dll=1800.0)
    return info, dec, r


STANDALONE = [c for c in REF["cases"] if c["book"] != "100K-GOLD-COMBO"]


@pytest.mark.parametrize("case", STANDALONE, ids=lambda c: f"{c['book']}-{c['session']}-{c['kind']}")
def test_replicates_100k_reference_backtest_trades(case):
    info, dec, r = _ref_trade(case)
    assert dec["side"] == case["side"]
    assert info["votes"] == case["votes"]
    assert info["gate"] == case["gate"]
    assert r["entry"] == pytest.approx(case["entry"], abs=1e-6)
    assert r["entry"] - dec["side"] * dec["stop_pts"] == pytest.approx(case["stop"], abs=1e-6)
    assert r["entry"] + dec["side"] * dec["tgt_pts"] == pytest.approx(case["target"], abs=1e-6)
    assert r["net"] == pytest.approx(case["net"], abs=0.01)
    if r["reason"] != "dll_1800":
        assert r["pts"] == pytest.approx(case["pts"], abs=1e-6)
        assert {"stop": "stop", "target": "target", "eod": "eod_1640", "dll": "dll_1800"}[case["kind"]] == r["reason"]
    else:
        assert case["kind"] == "dll"


def test_100k_reference_cases_cover_every_standalone_setup():
    books = {c["book"] for c in STANDALONE}
    assert books == {"100K-GOLD-0820-TREND", "100K-GOLD-0930-TREND", "100K-GOLD-0820-VOTE60",
                     "100K-NQ-0930-GAP", "100K-ES-0930-GAP"}
    # every standalone book has a stop and a target; exits differ by setup
    for b in books:
        kinds = {c["kind"] for c in STANDALONE if c["book"] == b}
        assert "stop" in kinds and "target" in kinds, (b, kinds)


def test_100k_setups_exactly_as_specified():
    B = C.BOOKS
    assert K100 == ["100K-GOLD-COMBO", "100K-GOLD-0820-TREND", "100K-GOLD-0930-TREND",
                    "100K-NQ-0930-GAP", "100K-GOLD-0820-VOTE60", "100K-ES-0930-GAP"]
    # 50K books stay exactly as they were
    assert list(B)[:6] == ["GOLD-COMBO", "GOLD-0820-TREND", "GOLD-0930-TREND", "NQ-0930-GAP",
                           "GOLD-0820-VOTE60", "ES-0930-GAP"]
    assert (B["GOLD-COMBO"]["legs"][0]["n"], B["GOLD-COMBO"]["legs"][1]["n"]) == (3, 4)
    combo = B["100K-GOLD-COMBO"]["legs"]
    assert [(l["leg"], l["n"], l["m"], l["stop"], l["tp"], l["sig"], l.get("gate")) for l in combo] == [
        ("08:20", 4, "GC", ("pts", 15.0), 1.5, "trend", True),
        ("09:30", 2, "GC", ("atr", 0.1), 1.0, "trend", False),
        ("08:20-vote", 2, "GC", ("pts", 15.0), 1.0, "vote60", False),
        ("09:30-es", 4, "ES", ("rng", 1.5), 1.0, "gap", False),
    ]
    assert "NQ" not in C.markets_of("100K-GOLD-COMBO")
    l = lambda b: B[b]["legs"][0]
    assert (l("100K-GOLD-0820-TREND")["stop"], l("100K-GOLD-0820-TREND")["tp"], l("100K-GOLD-0820-TREND")["n"],
            l("100K-GOLD-0820-TREND")["gate"]) == (("pts", 15.0), 1.5, 5, True)
    assert (l("100K-GOLD-0930-TREND")["stop"], l("100K-GOLD-0930-TREND")["tp"], l("100K-GOLD-0930-TREND")["n"]) == (("atr", 0.1), 1.0, 22)
    assert (l("100K-NQ-0930-GAP")["stop"], l("100K-NQ-0930-GAP")["tp"], l("100K-NQ-0930-GAP")["n"],
            B["100K-NQ-0930-GAP"]["m"]) == (("atr", 0.5), 1.0, 2, "NQ")
    assert (l("100K-GOLD-0820-VOTE60")["sig"], l("100K-GOLD-0820-VOTE60")["stop"],
            l("100K-GOLD-0820-VOTE60")["tp"], l("100K-GOLD-0820-VOTE60")["n"]) == ("vote60", ("pts", 15.0), 1.0, 5)
    assert (l("100K-ES-0930-GAP")["stop"], l("100K-ES-0930-GAP")["tp"], l("100K-ES-0930-GAP")["n"],
            B["100K-ES-0930-GAP"]["m"]) == (("rng", 1.5), 1.0, 6, "ES")
    assert all(B[b].get("plan") == "L100" and C.plan_of(b) is C.L100 for b in K100)
    assert all(B[b].get("plan") is None and C.plan_of(b) is C.L50 for b in list(B)[:6])
    assert C.L100["start"] == 100_000 and C.L100["profit_target"] == 6_000 and C.L100["mll"] == 3_000
    assert C.L100["dll"] == 1_800 and C.L100["lock_trigger"] == 103_100 and C.L100["locked_floor"] == 100_100
    assert C.L100["fee"] == 190.40 and C.L100["max_micros"] == 60
    assert B["100K-GOLD-COMBO"]["backtest"]["win_pct"] == 62.7
    assert B["100K-GOLD-0820-TREND"]["backtest"]["blow_pct"] == 0


def test_rng_stop_and_dll_1800():
    info = {"atr": 10.0, "rng15": 8.0}
    assert C.stop_pts({"stop": ("rng", 1.5)}, info, 0.25) == pytest.approx(12.0)
    ts = pd.date_range("2026-11-16 10:01", periods=6, freq="1min", tz=C.ET)
    # 22 MGC, 20 pt stop: 22 x $10 x ~8.2 + 33 >= 1,800 before the stop
    path = pd.DataFrame({"ts_ny": ts, "open": [4000.0] * 6, "high": [4000.5] * 6,
                         "low": [3999.0, 3998.0, 3991.5, 3991.5, 3991.5, 3991.5], "close": [3999.5] * 6, "volume": 1})
    path["hm"] = path.ts_ny.dt.hour * 100 + path.ts_ny.dt.minute
    r = C.walk(path, 1, 20.0, 20.0, 0.1, 22, 10.0, final=False, dll=1800.0)
    assert r["status"] == "closed" and r["reason"] == "dll_1800" and r["net"] == -1800.0
    # 50K default DLL is unchanged
    r50 = C.walk(path, 1, 20.0, 20.0, 0.1, 15, 10.0, final=False)
    assert r50["reason"] == "dll_1200" and r50["net"] == -1200.0


def test_combined_day_cap_100k():
    assert C.day_net_mae([{"net": -1000.0, "mae_usd": -1000.0}, {"net": -900.0, "mae_usd": -950.0}], dll=1800.0) == (-1800.0, -1800.0)
    assert C.day_net_mae([{"net": 500.0, "mae_usd": -100.0}, {"net": -300.0, "mae_usd": -300.0}], dll=1800.0) == (200.0, -400.0)


def test_lucid100_eval_rules():
    st = {"eval": C.fresh_eval(rules=C.L100), "passes": 0, "blows": 0, "attempts": []}
    assert C.eval_day(st, "d1", 2000, -200, rules=C.L100) is None and st["eval"]["peak"] == 102000
    assert C.eval_floor(st["eval"], C.L100) == 99000
    # lock when balance reaches 103,100
    assert C.eval_day(st, "d2", 1500, 0, rules=C.L100) is None and st["eval"]["locked"] and C.eval_floor(st["eval"], C.L100) == 100100
    # pass at 106,000 (2000+1500+2500)
    assert C.eval_day(st, "d3", 2500, -100, rules=C.L100) == "pass" and st["passes"] == 1 and st["eval"]["balance"] == 100000
    # blow on excursion to the trailing floor, then a fresh 100k eval
    assert C.eval_day(st, "d4", -1500, -1600, rules=C.L100) is None
    assert C.eval_day(st, "d5", 100, -2000, rules=C.L100) == "blow" and st["blows"] == 1 and st["eval"]["balance"] == 100000
    snap = C.eval_snapshot(st, open_pnl=300.0, rules=C.L100)
    assert snap["passing_pct"] == 50.0 and snap["passing_label"] == "1/2" and snap["blows"] == 1
    assert snap["profit_target"] == 6000.0 and snap["title"] == "LucidPro 100K" and snap["dll"] == 1800.0
    assert snap["start_balance"] == 100000.0


def test_100k_combo_day_matches_reference():
    """May 27 2026: two combo legs, day P&L $476 / MAE -$599 as in the reference."""
    combo = REF["combo"]
    assert combo["session"] == "2026-05-27" and combo["day_pnl"] == 476.0
    spec = C.BOOKS["100K-GOLD-COMBO"]
    results = []
    for leg in spec["legs"]:
        m = C.leg_market("100K-GOLD-COMBO", leg)
        df = REF_BARS[m]
        g = df[df.date.astype(str) == combo["session"]].reset_index(drop=True)
        if not len(g):
            continue
        dl = C.daily_stats(REF["daily"][m])
        info = C.votes_for(g, leg["O"], m, C.prev_stats(dl, combo["session"]), C.ema5(df, leg["O"]))
        dec = C.decide(leg, info, m)
        if "skip" in dec:
            continue
        D = C.hm_add(leg["O"], 30)
        r = C.walk(g[(g.hm > D) & (g.hm <= C.CAP)], dec["side"], dec["stop_pts"], dec["tgt_pts"],
                   C.MK[m]["tick"], leg["n"], C.MK[m]["pv"], final=True, dll=1800.0)
        if r["status"] == "closed":
            results.append(r)
    pnl, mae = C.day_net_mae(results, dll=1800.0)
    assert len(results) == combo["n_legs"]
    assert pnl == pytest.approx(combo["day_pnl"], abs=0.01)
    assert mae == pytest.approx(combo["day_mae"], abs=0.01)


def test_100k_books_get_their_own_100k_accounts(tmp_path):
    raw = {}
    for m in ("GC", "NQ", "ES"):
        df = pd.read_csv(DATA / "cme_mech_yahoo_1112_1113.csv.gz")
        df = df[df.m == m].drop(columns=["m"])
        df["ts_ny"] = pd.to_datetime(df.ts_ny, utc=True).dt.tz_convert(C.ET)
        raw[m] = df.reset_index(drop=True)
    clock = {"t": datetime(2026, 11, 13, 22, 30, tzinfo=UTC)}
    eng = C.CmeMechEngine({}, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path,
                          fetch=lambda m: raw[m].copy(), now_fn=lambda: clock["t"])
    # created_at is "now", so 12-13 Nov sessions are before book start (same as 50K)
    asyncio.run(eng.tick())
    for b in K100:
        s = eng.books[b]
        assert s["start_balance"] == 100_000.0 and s["balance"] == 100_000.0
        assert s["lucid100"]["eval"]["balance"] == 100_000.0 and s["lucid100"]["eval"]["days"] == 0
        assert "lucid50" not in s
    for b in list(C.BOOKS)[:6]:
        assert eng.books[b]["start_balance"] == 50_000.0 and "lucid50" in eng.books[b]
    site = json.loads((tmp_path / C.SITE_FILE).read_text())
    assert [c["book"] for c in site["books"]] == list(C.BOOKS)
    by = {c["book"]: c for c in site["books"]}
    assert by["100K-GOLD-COMBO"]["start_balance"] == 100000.0
    assert by["100K-GOLD-COMBO"]["eval"]["title"] == "LucidPro 100K"
    assert by["GOLD-COMBO"]["start_balance"] == 50000.0
    assert site["lucid100"]["dll"] == 1800.0


def test_no_model_call_in_100k_module():
    import inspect
    src = inspect.getsource(C)
    for bad in ("gemini_client", "claude_client", "deepseek", "anthropic", "genai"):
        assert bad not in src
