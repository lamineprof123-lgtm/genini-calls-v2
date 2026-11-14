"""t340u mechanical CME books on Lucid 50K: replicate known backtest trades from the reference script
(/workspace/orb_vote/orb_vote_optimize_blowok.py on the NinjaTrader CME 1-min bars), the live engine on Yahoo
CME bars, the Lucid 50K eval tracker, and the panel wiring. No network, no model calls."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import cme_mech as C
from signals.config import load_config

DATA = Path(__file__).resolve().parent / "data"
UTC = timezone.utc
REF = json.loads((DATA / "cme_mech_ref_cases.json").read_text())


def _bars(path, m):
    df = pd.read_csv(path)
    df = df[df.m == m].drop(columns=["m"])
    df["ts_ny"] = pd.to_datetime(df.ts_ny, utc=True).dt.tz_convert(C.ET)
    return C.prep_bars(df)


REF_BARS = {m: _bars(DATA / "cme_mech_ref_bars.csv.gz", m) for m in ("GC", "NQ", "ES")}


def _ref_trade(case):
    spec = C.BOOKS[case["book"]]; m = spec["m"]; leg = spec["legs"][0]
    df = REF_BARS[m]
    g = df[df.date.astype(str) == case["session"]].reset_index(drop=True)
    dl = C.daily_stats(REF["daily"][m])
    prev = C.prev_stats(dl, case["session"])
    info = C.votes_for(g, leg["O"], m, prev, C.ema5(df, leg["O"]))
    dec = C.decide(leg, info, m)
    D = C.hm_add(leg["O"], 30)
    path = g[(g.hm > D) & (g.hm <= C.CAP)]
    r = C.walk(path, dec["side"], dec["stop_pts"], dec["tgt_pts"], C.MK[m]["tick"], leg["n"], C.MK[m]["pv"], final=True)
    return info, dec, r


@pytest.mark.parametrize("case", REF["cases"], ids=lambda c: f"{c['book']}-{c['session']}-{c['kind']}")
def test_replicates_reference_backtest_trades(case):
    info, dec, r = _ref_trade(case)
    assert dec["side"] == case["side"]
    assert info["votes"] == case["votes"]
    assert info["gate"] == case["gate"]
    assert r["entry"] == pytest.approx(case["entry"], abs=1e-6)
    assert r["entry"] - dec["side"] * dec["stop_pts"] == pytest.approx(case["stop"], abs=1e-6)
    assert r["entry"] + dec["side"] * dec["tgt_pts"] == pytest.approx(case["target"], abs=1e-6)
    assert r["net"] == pytest.approx(case["net"], abs=0.01)
    if r["reason"] != "dll_1200":
        assert r["pts"] == pytest.approx(case["pts"], abs=1e-6)
        assert {"stop": "stop", "target": "target", "eod": "eod_1640"}[case["kind"]] == r["reason"]


def test_reference_cases_cover_every_single_setup_and_exit_kind():
    books = {c["book"] for c in REF["cases"]}
    assert books == {"GOLD-0820-TREND", "GOLD-0930-TREND", "GOLD-0820-VOTE60", "NQ-0930-GAP", "ES-0930-GAP"}
    assert {c["kind"] for c in REF["cases"]} == {"stop", "target", "eod"}


def test_setups_exactly_as_specified():
    B = C.BOOKS
    assert list(B) == ["GOLD-COMBO", "GOLD-0820-TREND", "GOLD-0930-TREND", "NQ-0930-GAP", "GOLD-0820-VOTE60", "ES-0930-GAP"]
    g1, g2 = B["GOLD-COMBO"]["legs"]
    assert (g1["O"], g1["sig"], g1["gate"], g1["stop"], g1["tp"], g1["n"]) == (820, "trend", True, ("pts", 15.0), 1.5, 3)
    assert (g2["O"], g2["sig"], g2["gate"], g2["stop"], g2["tp"], g2["n"]) == (930, "trend", False, ("atr", 0.1), 1.0, 4)
    l = lambda b: B[b]["legs"][0]
    assert (l("GOLD-0820-TREND")["stop"], l("GOLD-0820-TREND")["tp"], l("GOLD-0820-TREND")["n"], l("GOLD-0820-TREND")["gate"]) == (("pts", 15.0), 1.5, 4, True)
    assert (l("GOLD-0930-TREND")["stop"], l("GOLD-0930-TREND")["tp"], l("GOLD-0930-TREND")["n"]) == (("atr", 0.1), 1.0, 15)
    assert (l("NQ-0930-GAP")["stop"], l("NQ-0930-GAP")["tp"], l("NQ-0930-GAP")["n"], B["NQ-0930-GAP"]["m"]) == (("atr", 1.5), 2.0, 1, "NQ")
    assert (l("GOLD-0820-VOTE60")["sig"], l("GOLD-0820-VOTE60")["stop"], l("GOLD-0820-VOTE60")["tp"], l("GOLD-0820-VOTE60")["n"]) == ("vote60", ("pts", 15.0), 1.0, 6)
    assert (l("ES-0930-GAP")["stop"], l("ES-0930-GAP")["tp"], l("ES-0930-GAP")["n"], B["ES-0930-GAP"]["m"]) == (("pts", 10.0), 3.0, 8, "ES")
    assert C.COMM == 1.5 and C.DLL == 1200 and C.FLAT == 1640
    assert C.L50 == {"start": 50000.0, "profit_target": 3000.0, "mll": 2000.0, "dll": 1200.0, "lock_trigger": 52100.0,
                     "locked_floor": 50100.0, "max_micros": 40, "flat_et": "16:45", "fee": 120.40}
    assert B["GOLD-COMBO"]["backtest"]["net_per_eval"] == 6572 and B["ES-0930-GAP"]["backtest"]["win_pct"] == 36.6


def test_dll_caps_a_trade_at_minus_1200():
    ts = pd.date_range("2026-11-16 08:51", periods=6, freq="1min", tz=C.ET)
    path = pd.DataFrame({"ts_ny": ts, "open": [4000.0] * 6, "high": [4000.5] * 6,
                         "low": [3999.0, 3998.0, 3990.0, 3990.0, 3990.0, 3990.0], "close": [3999.5] * 6, "volume": 1})
    path["hm"] = path.ts_ny.dt.hour * 100 + path.ts_ny.dt.minute
    # 15 MGC, 20 pt stop: 15 x 10 x ~10.1 + 22.5 >= 1,200 before the stop
    r = C.walk(path, 1, 20.0, 20.0, 0.1, 15, 10.0, final=False)
    assert r["status"] == "closed" and r["reason"] == "dll_1200" and r["net"] == -1200.0


def test_combined_day_cap():
    assert C.day_net_mae([{"net": -700.0, "mae_usd": -700.0}, {"net": -600.0, "mae_usd": -650.0}]) == (-1200.0, -1200.0)
    assert C.day_net_mae([{"net": 500.0, "mae_usd": -100.0}, {"net": -300.0, "mae_usd": -300.0}]) == (200.0, -400.0)


def test_lucid50_eval_matches_reference_run_path_rules():
    st = {"eval": C.fresh_eval(), "passes": 0, "blows": 0, "attempts": []}
    assert C.eval_day(st, "d1", 1500, -200) is None and st["eval"]["peak"] == 51500
    assert C.eval_floor(st["eval"]) == 49500
    assert C.eval_day(st, "d2", 700, 0) is None and st["eval"]["locked"] and C.eval_floor(st["eval"]) == 50100
    assert C.eval_day(st, "d3", 800, -100) == "pass" and st["passes"] == 1 and st["eval"]["balance"] == 50000
    # blow on the intraday excursion (worst MAE touches the trailing floor), then a fresh eval
    assert C.eval_day(st, "d4", -1000, -1100) is None
    assert C.eval_day(st, "d5", 100, -1000) == "blow" and st["blows"] == 1 and st["eval"]["balance"] == 50000
    snap = C.eval_snapshot(st, open_pnl=300.0)
    assert snap["passing_pct"] == 50.0 and snap["passing_label"] == "1/2" and snap["blows"] == 1
    assert snap["current_progress_pct"] == 10.0 and snap["profit_target"] == 3000.0 and snap["title"] == "LucidPro 50K"


def _yahoo():
    fr = {}
    for m in ("GC", "NQ", "ES"):
        df = pd.read_csv(DATA / "cme_mech_yahoo_1112_1113.csv.gz")
        df = df[df.m == m].drop(columns=["m"])
        df["ts_ny"] = pd.to_datetime(df.ts_ny, utc=True).dt.tz_convert(C.ET)
        fr[m] = df.reset_index(drop=True)
    return fr


def test_live_engine_replays_two_yahoo_sessions(tmp_path):
    """Step the engine through 12-13 Nov 2026 on Yahoo CME bars (no look-ahead: fetch returns only bars before
    now). Incremental results must equal a one-shot walk of each full session; spot-check known trades."""
    raw = _yahoo()
    clock = {"t": datetime(2026, 11, 11, 20, tzinfo=UTC)}

    def fetch(m):
        d = raw[m]
        return d[d.ts_ny < pd.Timestamp(clock["t"])].copy()

    eng = C.CmeMechEngine({}, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path,
                          fetch=fetch, now_fn=lambda: clock["t"])
    t = datetime(2026, 11, 12, 13, 0, tzinfo=UTC)
    opened = set()
    while t < datetime(2026, 11, 13, 22, 30, tzinfo=UTC):
        clock["t"] = t
        asyncio.run(eng.tick())
        for b, s in eng.books.items():
            for p in s["positions"]:
                opened.add((b, p["session"], p["leg"]))
                assert p["mark"] is not None and p["symbol"] in ("GC", "NQ", "ES")
        t += timedelta(minutes=4)
    recs = [json.loads(l) for f in sorted((tmp_path / "logs").glob("*/*_trades.jsonl")) for l in open(f)]
    got = {(r["book"], r["session"], r["leg"]): r for r in recs}
    # every trade was seen OPEN on the way (live marks) and closed once
    assert len(recs) == len(got) and opened <= set(got)
    es = got[("ES-0930-GAP", "2026-11-12", "09:30")]
    assert (es["side"], es["entry"], es["stop"], es["target"], es["reason"], es["net"]) == ("SHORT", 7611.5, 7621.5, 7581.5, "stop", -422.0)
    g = got[("GOLD-0930-TREND", "2026-11-13", "09:30")]
    assert (g["side"], g["entry"], g["reason"], g["contract"], g["qty"]) == ("SHORT", 3894.0, "target", "MGC", 15)
    assert got[("GOLD-COMBO", "2026-11-13", "09:30")]["qty"] == 4
    # one-shot walk of the full session equals what the incremental engine booked
    for (b, ds, legname), r in got.items():
        spec = C.BOOKS[b]; m = spec["m"]; leg = next(x for x in spec["legs"] if x["leg"] == legname)
        df = C.prep_bars(raw[m].copy())
        g2 = df[df.date.astype(str) == ds].reset_index(drop=True)
        dl = C.daily_stats(eng.eng["daily"][m])
        info = C.votes_for(g2, leg["O"], m, C.prev_stats(dl, ds), C.ema5(df, leg["O"]))
        dec = C.decide(leg, info, m)
        D = C.hm_add(leg["O"], 30)
        w = C.walk(g2[(g2.hm > D) & (g2.hm <= C.CAP)], dec["side"], dec["stop_pts"], dec["tgt_pts"],
                   C.MK[m]["tick"], leg["n"], C.MK[m]["pv"], final=True)
        assert (w["reason"], w["net"]) == (r["reason"], r["net"]), (b, ds)
    # books: own $50k accounts, balance = 50k + realized, evals rolled for both sessions
    for b, s in eng.books.items():
        mine = sum(r["net"] for r in recs if r["book"] == b)
        assert s["balance"] == pytest.approx(50000 + mine, abs=0.01)
        assert s["final_through"] == "2026-11-13" and not s["positions"]
    site = json.loads((tmp_path / C.SITE_FILE).read_text())
    assert [c["book"] for c in site["books"]] == list(C.BOOKS) and site["model_calls"] is False
    assert site["books"][0]["backtest"]["label"] == "backtest (in-sample)"


def test_book_start_blocks_earlier_sessions(tmp_path):
    raw = _yahoo()
    clock = {"t": datetime(2026, 11, 13, 22, 30, tzinfo=UTC)}
    eng = C.CmeMechEngine({}, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path,
                          fetch=lambda m: raw[m].copy(), now_fn=lambda: clock["t"])
    asyncio.run(eng.tick())
    assert not list((tmp_path / "logs").glob("*/*_trades.jsonl"))
    for s in eng.books.values():
        assert s["final_through"] == "2026-11-13" and s["lucid50"]["eval"]["days"] == 0


def test_panel_cards_and_rows(tmp_path):
    from signals.panel import PanelLedger, BOOKS, LUCID_ONLY, OWN_RESET
    raw = _yahoo()
    clock = {"t": datetime(2026, 11, 11, 20, tzinfo=UTC)}
    eng = C.CmeMechEngine({}, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path,
                          fetch=lambda m: raw[m][raw[m].ts_ny < pd.Timestamp(clock["t"])].copy(), now_fn=lambda: clock["t"])
    for t in (datetime(2026, 11, 13, 15, 10, tzinfo=UTC), datetime(2026, 11, 13, 22, 30, tzinfo=UTC)):
        clock["t"] = t
        asyncio.run(eng.tick())
    cfg = copy.deepcopy(load_config())
    cfg["general"]["state_dir"] = str(tmp_path / "state"); cfg["general"]["logs_dir"] = str(tmp_path / "logs")
    cfg["site"]["dir"] = str(tmp_path)
    calls = []

    async def q(syms):
        calls.append(list(syms)); return {}
    led = PanelLedger(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path,
                      quote_fn=q, history_fn=False)
    led.reset = {"token": "t", "at": "2026-11-01T00:00:00+00:00"}
    led._reset_marker = lambda: {"token": "t", "at": "2026-11-01T00:00:00+00:00"}
    asyncio.run(led.tick(now=clock["t"]))
    site = json.loads((tmp_path / "panel.json").read_text())
    perf = {p["book"]: p for p in site["performance"] if p["book"] in C.BOOKS}
    assert set(perf) == set(C.BOOKS) and all(b in LUCID_ONLY and b in OWN_RESET for b in C.BOOKS)
    for b, p in perf.items():
        assert p["model"] == "mechanical" and p["start_balance"] == 50000.0 and p["venue"] == "LUCID"
        assert p["eval_pass"]["title"] == "LucidPro 50K" and p["eval_pass"]["profit_target"] == 3000.0
        assert p["backtest"]["label"] == "backtest (in-sample)"
        assert "went_positive_first_pct" in p
    rows = [r for r in site["positions"] if r["book"] in C.BOOKS]
    assert rows and all(r["status"].startswith("CLOSED") and r["net_at_close_usd"] is not None for r in rows)
    assert all(r["went_positive_first"] in ("yes", "no", "?") for r in rows)
    assert all(not ({"GC", "NQ", "ES"} & set(c)) for c in calls)     # CME symbols never hit the Dukascopy quote path
    es = next(r for r in rows if r["book"] == "ES-0930-GAP" and r["entry_time"].startswith("2026-11-13"))
    assert es["contract"] == "MES" and es["qty"] == 8 and es["fees_usd"] == 12.0


def test_no_model_call_and_market_gate_respected():
    import inspect
    src = inspect.getsource(C)
    for bad in ("gemini_client", "claude_client", "deepseek", "anthropic", "genai"):
        assert bad not in src
    assert "markets_closed" in inspect.getsource(C.run_cme_mech_loop)
    sat = datetime(2026, 11, 14, 15, tzinfo=UTC)
    assert not C.trading_window(sat)
    assert C.trading_window(datetime(2026, 11, 16, 13, 51, tzinfo=UTC))   # Mon 08:51 New York
