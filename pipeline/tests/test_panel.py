"""Clean panel ledger (t252u) + shared reset (t255u). No network."""
import asyncio, json
from datetime import datetime, timezone
from pathlib import Path

from signals.config import load_config
from signals.panel import PanelLedger, write_reset_marker, status_of
from signals.other_venue import OtherVenueHub

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)


def _cfg(tok="tok-1"):
    cfg = load_config()
    cfg["reset"] = {"token": tok}
    cfg.setdefault("other_venue", {})["reset_token"] = tok
    return cfg


def test_status_mapping():
    assert status_of("stop_1atr") == "CLOSED-stop" and status_of("hard_stop_500") == "CLOSED-stop"
    assert status_of("target_1atr") == "CLOSED-target"
    assert status_of("max_hold_90m") == "CLOSED-time" and status_of("time_90min") == "CLOSED-time"


def test_ledger_tracks_open_mfe_mae_and_close(tmp_path):
    cfg = _cfg()
    st, lg = tmp_path / "state", tmp_path / "logs"
    (st / "lucid").mkdir(parents=True)
    write_reset_marker(cfg, st, T0)
    pos = {"id": 1, "symbol": "US100", "contract": "NQ", "qty": 1, "side": -1, "entry": 20000.0,
           "entry_time": "2026-11-12T18:05:00+00:00", "atr": 40.0}
    (st / "lucid" / "account.json").write_text(json.dumps({"account": {"balance": 100000.0}, "positions": [pos]}))
    px = {"v": 19990.0}

    async def q(syms):
        return {"US100": {"price": px["v"]}}
    led = PanelLedger(cfg, state_dir=st, logs_dir=lg, site_dir=tmp_path, quote_fn=q, history_fn=False)
    t1 = datetime(2026, 11, 12, 18, 6, tzinfo=UTC)
    asyncio.run(led.tick(t1))
    px["v"] = 20015.0
    asyncio.run(led.tick(t1))
    row = next(iter(led.s["rows"].values()))
    assert row["status"] == "OPEN" and row["max_pnl_usd"] == 200.0 and row["min_pnl_usd"] == -300.0
    # first print was +$200 gross / +$196.50 net (NQ RT fees $3.50) — went positive first
    assert row["went_positive_first"] == "yes" and row["first_cross"] == "pos"
    assert row["first_cross_basis"] == "live_net_tick"
    # close lands in the trade log, position leaves the book
    (st / "lucid" / "account.json").write_text(json.dumps({"account": {"balance": 99196.5}, "positions": []}))
    d = lg / "2026-11-12"; d.mkdir(parents=True)
    (d / "lucid_trades.jsonl").write_text(json.dumps({
        "t_exit": "2026-11-12T18:20:00+00:00", "id": 1, "symbol": "US100", "contract": "NQ", "qty": 1,
        "side": "SHORT", "entry_time": "2026-11-12T18:05:00+00:00", "entry": 20000.0, "exit": 20040.0,
        "reason": "stop_1atr", "gross": -800.0, "net": -803.5}) + "\n")
    asyncio.run(led.tick(T0))
    site = json.loads((tmp_path / "panel.json").read_text())
    r = site["positions"][0]
    assert r["status"] == "CLOSED-stop" and r["net_at_close_usd"] == -803.5 and r["min_pnl_usd"] == -800.0
    main = next(p for p in site["performance"] if p["book"] == "MAIN" and p["venue"] == "LUCID")
    assert main["trades"] == 1 and main["losses"] == 1 and main["net_pnl_usd"] == -803.5
    assert r["went_positive_first"] == "yes" and r["went_positive_first_label"] == "Yes"
    assert main["went_positive_first"] == "100% (1/1)" and main["went_positive_first_yes"] == 1
    ep = main["eval_pass"]
    assert ep["passing_label"] is None and ep["passing_pct"] is None
    assert ep["current_profit"] == -803.5 and ep["current_progress_pct"] == -13.4
    assert ep["mll_buffer"] == 2196.5 and ep["mll_floor"] == 97000.0 and ep["lucid_trades"] == 1
    ov = next(p for p in site["performance"] if p["book"] == "MAIN" and p["venue"] != "LUCID")
    assert "eval_pass" not in ov
    assert "Eval passing" in site["eval_pass_note"]
    assert site["reset"]["token"] == "tok-1" and site["reset"]["at"].startswith("2026-11-12T18:00")
    assert {p["book"] for p in site["performance"]} >= {"MAIN", "C-OPUS-B", "DS-FLASH", "CB-BRACKET", "C-SONNET"}


def test_pre_reset_positions_are_ignored(tmp_path):
    cfg = _cfg()
    st = tmp_path / "state"; (st / "lucid").mkdir(parents=True)
    write_reset_marker(cfg, st, T0)
    old = {"id": 9, "symbol": "US100", "contract": "NQ", "qty": 1, "side": 1, "entry": 1.0,
           "entry_time": "2026-11-12T17:00:00+00:00"}
    (st / "lucid" / "account.json").write_text(json.dumps({"positions": [old]}))

    async def q(syms):
        return {}
    led = PanelLedger(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path, quote_fn=q, history_fn=False)
    asyncio.run(led.tick(T0))
    assert led.s["rows"] == {}


def test_other_venue_reset_archives_and_starts_fresh(tmp_path):
    cfg = _cfg("tok-a")
    hub = OtherVenueHub(cfg, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    hub.book("MAIN")["account"]["balance"] = 95000.0
    hub.book("MAIN")["positions"].append({"symbol": "BTCUSD"})
    hub.persist(T0)
    cfg2 = _cfg("tok-b")
    hub2 = OtherVenueHub(cfg2, state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", site_dir=tmp_path)
    assert hub2.book("MAIN")["account"]["balance"] == 100000.0 and hub2.book("MAIN")["positions"] == []
    assert hub2.s["reset_token"] == "tok-b"
    arch = json.loads((tmp_path / "state" / "other_venue" / "books_pre_reset_tok-b.json").read_text())
    assert arch["books"]["MAIN"]["account"]["balance"] == 95000.0
