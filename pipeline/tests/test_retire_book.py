"""2-strike book killed (USER RULE 2026-11-12, t210u): a book dropped from [exit_manager].books is retired -
archived, never checked again (no Gemini exit calls), gone from books.json / the panel comparison."""
import asyncio, copy, json
from datetime import datetime, timedelta, timezone

from signals.config import load_config
from signals.exit_manager import BOOK_1, BOOK_2, BOOK_NOSTOP, Books, ExitManager

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 13, 0, tzinfo=UTC)


def test_live_config_has_no_2strike():
    c = load_config()
    assert BOOK_2 not in c["exit_manager"]["books"] and c["exit_manager"]["books"] == [BOOK_1, BOOK_NOSTOP]


def test_retired_book_archived_and_never_checked(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["lucid"]["reset_token"] = None
    old_cfg = copy.deepcopy(cfg); old_cfg["exit_manager"]["books"] = [BOOK_1, BOOK_2, BOOK_NOSTOP]
    old = Books(old_cfg)
    feed = [{"id": 1, "symbol": "XAUUSD", "contract": "GC", "qty": 1, "side": -1, "entry": 3945.1, "stop": None,
             "entry_time": T0.isoformat(), "atr": 7.0, "candle": None}]
    old.sync_feed(feed, T0)
    assert set(old.positions_for(1)) == {BOOK_1, BOOK_2, BOOK_NOSTOP}
    st = tmp_path / "state"; (st / "lucid").mkdir(parents=True)
    (st / "exit_books.json").write_text(json.dumps(old.s))
    calls = {"n": 0}

    async def decide(prompt, png, p0, m):
        calls["n"] += 1
        return {"model": "t", "raw_text": json.dumps({"structure": "expanding", "reason": "x", "decision": "HOLD",
                                                       "confidence": 0.5}), "error": None}
    mgr = ExitManager(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path, decide=decide)
    assert BOOK_2 not in mgr.books.s["books"] and BOOK_2 in mgr.books.s["retired_books"]
    assert mgr.books.s["retired_books"][BOOK_2]["retired_open_positions"][0]["id"] == 1
    assert list(st.glob("exit_books_retired_2-strike_*.json"))
    on_disk = json.loads((st / "exit_books.json").read_text())
    assert BOOK_2 not in on_disk["books"] and BOOK_2 in on_disk["retired_books"]
    assert BOOK_2 not in json.loads((tmp_path / "books.json").read_text())["books"]
    assert set(mgr.books.positions_for(1)) == {BOOK_1, BOOK_NOSTOP}
    # once the other books are flat nothing is checked: the retired position costs no Gemini call
    for b in mgr.books.s["books"].values():
        b["positions"] = []
    assert not mgr.books.open_ids()
    # reloading again does not re-retire / duplicate
    mgr2 = ExitManager(cfg, state_dir=st, logs_dir=tmp_path / "logs", site_dir=tmp_path, decide=decide)
    assert mgr2.books.retired_now == [] and BOOK_2 in mgr2.books.s["retired_books"]
