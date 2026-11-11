"""FirmTape GEX: gate, normalisation, gex object, chart lines, basis/live price, prompts, post-exit tracking."""
import asyncio, copy, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.config import load_config
from signals import firmtape as ft
from signals import firmtape_live as fl
from signals import exit_manager as em
from signals.gemini_client import slim_gemini_context, system_prompt

UTC = timezone.utc
OPEN_T = datetime(2026, 11, 11, 15, 0, 30, tzinfo=UTC)      # Wed 10:00:30 ET (RTH)
PRE_T = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)           # 09:00 ET (before cash open)


def nq(**kw):
    """Synthetic entry with the real response's shape (values invented)."""
    e = {"sym": "NQ", "session": "open", "price": 27800.0, "price_as_of": "2026-11-11T10:00:29-05:00",
         "flip": 27750.0, "call_wall": 27850.25, "put_wall": 27700.25,
         "net_gamma": {"value": -1.0e8, "sign": "negative", "unit": "usd_per_1pct"}, "age_s": 30, "grade": "B",
         "oi": {"call_wall": 29100.25, "put_wall": 27700.25},
         "levels_of_interest": {"levels": [
             {"rank": "C2", "side": "call", "strike": 27900.25, "books": ["volume"], "strength": 0.6},
             {"rank": "C1", "side": "call", "strike": 27850.25, "books": ["volume", "open_interest"], "strength": 1},
             {"rank": "C3", "side": "call", "strike": 27950.25, "books": ["volume"], "strength": 0.5},
             {"rank": "P1", "side": "put", "strike": 27700.25, "books": ["volume"], "strength": 0.9},
             {"rank": "P2", "side": "put", "strike": 27650.25, "books": ["open_interest"], "strength": 0.4}]},
         "session_levels": {"opening_range": {"high": 27820.0, "low": 27760.0, "complete": True}}}
    e.update(kw)
    return e


def gld(**kw):
    e = {"sym": "GLD", "session": "open", "price": 370.0, "price_as_of": "2026-11-11T10:00:29-05:00",
         "flip": 369.0, "net_gamma": {"value": 5e6, "sign": "positive", "unit": "usd_per_1pct"}, "age_s": 30,
         "levels_of_interest": {"levels": [{"rank": "C1", "side": "call", "strike": 375, "books": ["volume"],
                                            "strength": 0.8},
                                           {"rank": "P1", "side": "put", "strike": 365, "books": ["open_interest"],
                                            "strength": 1}]},
         "session_levels": {"opening_range": {"high": 371, "low": 368, "complete": True}}}
    e.update(kw)
    return e


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config())


@pytest.fixture(autouse=True)
def _fresh_globals():
    ft.get_basis_tracker().samples.clear()
    ft.get_client().reset()
    yield


def payload(sym="US100", live=27900.0, ts="2026-11-11T15:00:25+00:00", digits=2, lo=27700, hi=27950):
    candles = [{"time": 1_794_000_000 + i * 900, "open": lo + 10, "high": hi if i == 5 else lo + 50,
                "low": lo if i == 7 else lo + 20, "close": lo + 30} for i in range(100)]
    return {"symbol": sym, "visible": 100, "candles": candles, "levels": [{"type": "support", "label": "S/R",
                                                                         "price": lo + 5, "touches": 1}],
            "forming_candle": {"time": 0, "open": live, "high": live, "low": live, "close": live},
            "context": {"digits": digits, "live_price": live, "live_price_ts_utc": ts, "live_price_stale": False,
                        "forming_m15_open": live, "forming_m15_high": live, "forming_m15_low": live,
                        "levels": [{"type": "support", "label": "S/R", "price": lo + 5, "touches": 1}]}}


# ------------------------------------------------------------------ gate
def test_gate_rules(cfg):
    assert ft.gate(nq(), OPEN_T, cfg) == "ok"
    assert ft.gate(nq(), PRE_T, cfg) == "outside_ny_rth"
    assert ft.gate(nq(session="closed"), OPEN_T, cfg) == "session_closed"
    assert ft.gate(nq(age_s=301), OPEN_T, cfg).startswith("stale")
    assert ft.gate(nq(error={"code": "no_data"}), OPEN_T, cfg).startswith("symbol_error")
    assert ft.gate(None, OPEN_T, cfg) == "no_data"
    sat = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
    assert ft.gate(nq(), sat, cfg) == "outside_ny_rth"


def test_top_level_error_200_and_cache(cfg):
    calls = {"n": 0}

    async def bad(syms):
        calls["n"] += 1
        return {"error": {"message": "invalid key"}}

    c = ft.FirmTapeClient()
    d, err = asyncio.run(c.get(cfg, fetch=bad))
    assert d is None and "api error" in err
    asyncio.run(c.get(cfg, fetch=bad))
    assert calls["n"] == 1                      # error back-off cache

    async def good(syms):
        calls["n"] += 1
        assert syms == "NQ,GLD"
        return {"next_update_s": 3, "levels": [nq(), gld()]}
    c2 = ft.FirmTapeClient()
    c2.reset()                                  # drop the shared error back-off
    asyncio.run(c2.get(cfg, fetch=good)); asyncio.run(c2.get(cfg, fetch=good))
    assert calls["n"] == 2                      # 3 s TTL shared by entry cycle + exit checks


def test_one_shared_request_for_live_price_and_gex(cfg):
    calls = []

    async def good(syms):
        calls.append(syms)
        return {"next_update_s": 3, "levels": [nq(), gld()]}
    # live-price step (enrich_live_prices) asks first, for NQ + the GEX symbols ...
    d1, _ = asyncio.run(fl.shared_levels(cfg, max_age_s=2.0, fetch=good))
    # ... then the GEX step reuses that response
    d2, _ = asyncio.run(ft.FirmTapeClient().get(cfg))
    assert calls == ["NQ,GLD"] and d1 is d2


def test_no_key_means_null(cfg, monkeypatch):
    monkeypatch.delenv("FIRMTAPE_API_KEY", raising=False)
    d, err = asyncio.run(ft.FirmTapeClient().get(cfg))
    assert d is None and err == "no_api_key"


# ------------------------------------------------------------------ normalisation + gex object
def test_us100_additive_basis_and_gex(cfg):
    g = ft.build_gex("US100", nq(), 27900.0, cfg)          # instant basis = +100
    assert g["regime"] == "NEGATIVE_AMPLIFYING" and g["regime_from"] == "net_gamma_sign"
    assert g["gamma_flip"] == 27850.0 and g["price_vs_flip"] == "above"
    assert [w["rank"] for w in g["call_walls"]] == [1, 2] and g["call_walls"][0]["price"] == 27950.25
    assert g["call_walls"][0]["oi_aligned"] is True and g["call_walls"][1]["oi_aligned"] is False
    assert g["put_walls"][0]["price"] == 27800.25 and len(g["put_walls"]) == 2
    assert g["ny_opening_range"] == {"high": 27920.0, "low": 27860.0}
    raw = json.dumps(g)
    for leak in ("27850.25", "27700.25", "27800.0", "age_s", "price_as_of", "usd_per_1pct"):
        assert leak not in raw                   # never raw FirmTape prices / metadata


def test_xau_ratio_no_opening_range_and_sign_fallback(cfg):
    g = ft.build_gex("XAUUSD", gld(), 4070.0, cfg)          # ratio 11.0
    assert g["regime"] == "POSITIVE_DAMPENING"
    assert g["gamma_flip"] == 4059.0 and g["call_walls"][0]["price"] == 4125.0
    assert "ny_opening_range" not in g
    g2 = ft.build_gex("XAUUSD", gld(net_gamma={}, flip=371.0), 4070.0, cfg)   # no sign -> price vs flip
    assert g2["regime_from"] == "price_vs_flip" and g2["regime"] == "NEGATIVE_AMPLIFYING"


def test_apply_maps_symbols_and_nulls(cfg):
    data = {"levels": [nq(), gld()]}
    for sym in ("EURUSD", "GBPUSD", "BTCUSD"):
        p = payload(sym)
        ft.apply_firmtape(p, data, None, OPEN_T, cfg)
        assert p["context"]["gex"] is None and p["gex_lines"] == []
    p = payload("US100")
    ft.apply_firmtape(p, data, None, PRE_T, cfg)
    assert p["context"]["gex"] is None and p["context"]["gex_status"] == "outside_ny_rth"
    p = payload("US100")
    ft.apply_firmtape(p, data, "TimeoutError: x", OPEN_T, cfg)
    assert p["context"]["gex"] is None and p["gex_lines"] == []


# ------------------------------------------------------------------ chart lines
def test_chart_lines_margin_and_or_levels(cfg):
    p = payload("US100", live=27900.0)
    res = ft.apply_firmtape(p, {"levels": [nq()]}, None, OPEN_T, cfg)
    assert res["gex"] == "ok"
    titles = {l["title"]: l for l in p["gex_lines"]}
    # visible 27700..27950 (+/- 37.5): call wall 27950.25 in, put wall 27800.25 in, flip 27850 in
    assert titles["Call Wall #1 [OI]"]["style"] == "dashdot" and titles["Call Wall #1 [OI]"]["color"] == "#FFB300"
    assert titles["Put Wall #1"]["color"] == "#00BFA5" and titles["Gamma Flip"]["style"] == "dotted"
    labels = [l["label"] for l in p["levels"]]
    assert "NY OR High" in labels and "NY OR Low" in labels
    assert all(l["label"] != "NY OR High" for l in p["context"]["levels"])   # scalp/Lucid levels untouched
    far = payload("US100", live=27050.0, lo=27000, hi=27100)
    ft.apply_firmtape(far, {"levels": [nq(price=26950.0)]}, None, OPEN_T, cfg)   # adjusted walls ~ +100..+250
    assert far["gex_lines"] == [] and far["context"]["gex"] is not None     # context keeps gex; chart skips it


# ------------------------------------------------------------------ basis + live price
def test_rolling_basis_tracker(cfg):
    tr = ft.BasisTracker()
    t = OPEN_T
    assert not tr.add("US100", 27900, t, 27800, t + timedelta(seconds=60), cfg)   # not aligned
    for i, b in enumerate((100, 104, 98)):
        ts = t + timedelta(seconds=i * 18)
        assert tr.add("US100", 27800 + b, ts, 27800, ts + timedelta(seconds=1), cfg)
    assert tr.basis("US100", t + timedelta(minutes=1), cfg) == (100.0, 3)       # median
    assert tr.basis("US100", t + timedelta(minutes=45), cfg) == (None, 0)       # window expired


def test_gex_never_overrides_live_price_and_uses_firmtape_live_basis(cfg):
    # live step already set US100 = NQ + rolling basis (firmtape_live); GEX must keep it and reuse the basis
    p = payload("US100", live=27907.0, ts="2026-11-11T15:00:29+00:00")
    p["context"].update({"live_price_source": "firmtape:NQ", "live_basis": 100.0, "live_basis_n": 3,
                         "live_price_dukascopy": 27903.0})
    res = ft.apply_firmtape(p, {"levels": [nq(price=27807.0)]}, None, OPEN_T, cfg)
    c = p["context"]
    assert res["gex"] == "ok" and res["basis"] == 100.0 and c["live_basis_quality"] == "rolling_median"
    assert c["live_price"] == 27907.0 and c["live_price_source"] == "firmtape:NQ"
    assert c["gex"]["gamma_flip"] == 27850.0                                  # 27750 + 100
    assert p["forming_candle"]["close"] == 27907.0


def test_gex_instant_basis_without_firmtape_live(cfg):
    p = payload("US100", live=27901.0, ts="2026-11-11T15:00:00+00:00")
    p["context"]["live_price_source"] = "dukascopy:tick"
    ft.apply_firmtape(p, {"levels": [nq()]}, None, OPEN_T, cfg)
    c = p["context"]
    assert c["live_price"] == 27901.0 and c["live_basis_quality"] == "instant"
    assert c["gex"]["gamma_flip"] == 27851.0 and "live_price_dukascopy" not in c


def test_log_only_keys_not_sent_to_gemini(cfg):
    p = payload("US100", live=27901.0, ts="2026-11-11T15:00:29+00:00")
    p["context"].update({"live_basis": 100.0, "live_basis_n": 1, "live_price_dukascopy": 27901.0})
    ft.apply_firmtape(p, {"levels": [nq(price=27801.0)]}, None, OPEN_T, cfg)
    slim = slim_gemini_context(p["context"])
    assert "gex" in slim and not any(k in slim for k in ft.LOG_ONLY_KEYS)


# ------------------------------------------------------------------ prompts + walls
def test_entry_prompt_has_gex_block(cfg):
    txt = system_prompt(cfg, ["XAUUSD", "US100"])
    for s in ("Call Wall #1 (dealer resistance)", "POSITIVE_DAMPENING", "NEGATIVE_AMPLIFYING",
              "Never BUY directly into Call Wall #1", "cite the GEX regime"):
        assert s in txt


def test_exit_walls_use_gex_and_pin_rule(cfg):
    gex = ft.build_gex("US100", nq(), 27900.0, cfg)
    ctx = {"digits": 2, "live_price": 27940.0, "levels": [], "gex": gex, "atr_upper": 28100, "atr_lower": 27600,
           "signal_candle": {"high": 27945, "low": 27890}}
    w = em.find_walls(ctx, 1, 27940.0, 27905.0, 40.0)
    assert w["next_wall_label"] == "Call Wall #1" and w["dist_to_wall_atr"] == pytest.approx(0.26, abs=0.01)
    assert w["invalid_level_label"] == "NY OR Low"
    ws = em.find_walls(ctx, -1, 27855.0, 27880.0, 40.0)
    assert ws["next_wall_label"] == "Gamma Flip"
    pos = {"id": 1, "symbol": "US100", "side": 1, "entry": 27905.0, "atr": 40.0, "qty": 2, "contract": "NQ",
           "entry_time": OPEN_T.isoformat()}
    m = em.position_metrics(1, 27905.0, 27940.0, 40.0, 40.0, 40.0)
    txt = em.build_exit_prompt(cfg, pos, ctx, m, w, OPEN_T + timedelta(minutes=10))
    assert "DEALER GAMMA WALL PIN" in txt and txt.index("TARGET WALL REJECTION") < txt.index("DEALER GAMMA WALL PIN")
    assert "c) PROFIT PROTECTION" in txt and "d) CLIMAX EXHAUSTION" in txt
    assert "GEX: NEGATIVE_AMPLIFYING" in txt and "15-minute (M15)" in txt and "M1 EMA20" not in txt
    ctx2 = {**ctx, "gex": None}
    w2 = em.find_walls(ctx2, 1, 27940.0, 27905.0, 40.0)
    t2 = em.build_exit_prompt(cfg, pos, ctx2, m, w2, OPEN_T + timedelta(minutes=10))
    assert "DEALER GAMMA WALL PIN" not in t2 and "b) PROFIT PROTECTION" in t2 and "Options gamma" not in t2


# ------------------------------------------------------------------ post-exit tracking
def _m1(start, closes):
    idx = pd.date_range(start, periods=len(closes), freq="1min", tz="UTC")
    return pd.DataFrame({"open": closes, "high": [c + 2 for c in closes], "low": [c - 2 for c in closes],
                         "close": closes, "volume": 1.0}, index=idx)


def test_post_exit_metrics_short():
    item = {"side": "SHORT", "exit": 100.0, "t_exit": "2026-11-11T15:00:00+00:00",
            "due": "2026-11-11T15:05:00+00:00"}
    bars = _m1("2026-11-11T15:00:00+00:00", [100, 98, 96, 97, 95, 99])
    m = em.post_exit_metrics(item, bars, 20.0)
    assert m["price_after_5m"] == 95 and m["move_after_5m_pts"] == 5 and m["move_after_5m_usd"] == 100
    assert m["max_fav_5m_pts"] == 7 and m["max_adv_5m_pts"] == 2 and m["window_bars"] == 5
    assert em.post_exit_metrics(item, bars.iloc[:3], 20.0) is None        # feed not caught up yet


def test_post_exit_queue_books_and_baseline(cfg, tmp_path):
    st = tmp_path / "state"; (st / "lucid").mkdir(parents=True)
    t0 = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)
    feed = [{"id": 7, "symbol": "EURUSD", "contract": "6E", "qty": 5, "side": -1, "entry": 1.1145, "stop": 1.1160,
             "target": 1.1130, "entry_time": t0.isoformat(), "risk_usd": 900, "atr": 0.0005, "candle": None}]
    (st / "lucid" / "account.json").write_text(json.dumps({"book_feed": feed}))
    day = tmp_path / "logs" / "2026-11-11"; day.mkdir(parents=True)
    (day / "lucid_trades.jsonl").write_text(json.dumps(
        {"id": 7, "symbol": "EURUSD", "contract": "6E", "qty": 5, "side": "SHORT", "exit": 1.1140,
         "t_exit": (t0 + timedelta(minutes=2)).isoformat(), "reason": "stop", "net": -100.0, "hold_seconds": 120}) + "\n")

    async def m1_fetch(sym):
        return _m1(t0, [1.1145 - 0.0001 * i for i in range(30)]), "test"

    mgr = em.ExitManager(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path, m1_fetch=m1_fetch)
    mgr.books.sync_feed(feed, t0)
    p = mgr.books.positions_for(7)[em.BOOK_1]
    mgr.books.close(em.BOOK_1, p, 1.1141, t0 + timedelta(minutes=3), "gemini_close")
    r = asyncio.run(mgr.tick(t0 + timedelta(minutes=4)))            # not due yet; baseline queued
    assert not r["post_exit"] and {x["book"] for x in mgr.books.s["post_exit"]} == {"baseline", em.BOOK_1}
    mgr.books.s["books"][em.BOOK_2]["positions"] = []                 # nothing open -> idle tick still tracks
    r2 = asyncio.run(mgr.tick(t0 + timedelta(minutes=9)))
    got = {x["book"]: x for x in r2["post_exit"]}
    assert set(got) == {"baseline", em.BOOK_1} and not mgr.books.s["post_exit"]
    assert got[em.BOOK_1]["price_after_5m"] == pytest.approx(1.1145 - 0.0001 * 7)
    assert got[em.BOOK_1]["move_after_5m_pts"] > 0                    # short kept falling: left on the table
    lines = [json.loads(x) for x in (day / "exit_book_trades.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines if x["book"] == em.BOOK_1] == ["close", "post_exit_5m"] or \
        sorted(x["event"] for x in lines) == ["close", "post_exit_5m", "post_exit_5m"]
