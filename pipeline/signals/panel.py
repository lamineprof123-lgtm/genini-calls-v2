"""Clean panel ledger (t252u). PAPER ONLY.

One file for the site (panel.json): per-book performance since the shared reset, and one table of every
position opened since the reset across all books (Lucid and OTHER-VENUE), with the best and worst $ P&L
reached, status (OPEN / CLOSED-stop / CLOSED-target / CLOSED-time / CLOSED-<other>) and net P&L at close.

Reads only the books' own state files and trade logs (no model calls, never touches a book). Live MFE/MAE
in $ is sampled for open positions every `poll_seconds` from the same live-quote path the exit checks use,
and merged with any excursion the book itself recorded (mfe/mae price units) and the close price.
State: state/panel/ledger.json. Site: panel.json at the data root.
"""
from __future__ import annotations
import asyncio, json, logging
from datetime import datetime, timezone
from pathlib import Path

from .risk import _atomic_write
from . import first_cross as fc

log = logging.getLogger("signals.panel")
UTC = timezone.utc
START = 100000.0

# book -> (model, label, lucid-state reader kind)
BOOKS = [
    ("MAIN", "gemini-3.8-flash", "Gemini flash · main"),
    ("1-strike", "gemini-3.8-flash", "Gemini flash · 1-strike"),
    ("nostop", "gemini-3.8-flash", "Gemini flash · nostop"),
    ("bracket", "gemini-3.8-flash", "Gemini flash · bracket (conf≥0.75)"),
    ("CB-BRACKET", "gemini-4-carbon", "Carbon · CB-BRACKET"),
    ("CB-RUNNER", "gemini-4-carbon", "Carbon · CB-RUNNER"),
    ("C-OPUS", "claude-opus-5-5", "Claude Opus · C-OPUS"),
    ("C-OPUS-B", "claude-opus-5-5", "Claude Opus · C-OPUS-B (bracket-v1)"),
    ("C-SONNET", "claude-sonnet-5-5", "Claude Sonnet · C-SONNET"),
    ("DS-FLASH", "deepseek-flash", "DeepSeek · DS-FLASH"),
]
MODEL_OF = {b: m for b, m, _ in BOOKS}
LABEL_OF = {b: l for b, _, l in BOOKS}
LUCID, OV = "LUCID", "OTHER-VENUE"


def _dt(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def _side(v) -> int:
    if isinstance(v, (int, float)):
        return 1 if v > 0 else -1
    s = str(v or "").upper()
    return 1 if s in ("LONG", "BUY", "1", "+1") else -1


def status_of(reason: str | None) -> str:
    r = str(reason or "").lower()
    if "stop" in r:
        return "CLOSED-stop"
    if "target" in r or r in ("tp", "take_profit"):
        return "CLOSED-target"
    if "time" in r or "hold" in r or "90" in r or "deadline" in r:
        return "CLOSED-time"
    return f"CLOSED-{r or 'other'}"


def _key(book: str, venue: str, sym: str, entry_time) -> str:
    t = _dt(entry_time)
    return f"{book}|{venue}|{sym}|{t.replace(microsecond=0).isoformat() if t else entry_time}"


def _read(p: Path):
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


class PanelLedger:
    def __init__(self, cfg: dict, *, state_dir=None, logs_dir=None, site_dir=None, quote_fn=None,
                 history_fn=None):
        self.cfg = cfg
        self.state = Path(state_dir or cfg["general"]["state_dir"])
        self.logs = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site = Path(site_dir or (cfg.get("site") or {}).get("dir") or self.state.parent)
        self.pc = dict(cfg.get("panel") or {})
        self.path = self.state / "panel" / "ledger.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.quote_fn = quote_fn
        # None = default Dukascopy/Binance fetch; False = skip (tests); callable = injected.
        self.history_fn = history_fn
        self.reset = self._reset_marker()
        st = _read(self.path)
        if not st or st.get("reset_token") != self.reset.get("token"):
            if st:
                _atomic_write(self.path.parent / f"ledger_pre_{self.reset.get('token')}.json", st)
            st = {"reset_token": self.reset.get("token"), "reset_at": self.reset.get("at"), "rows": {}}
        self.s = st
        self.contracts = (cfg.get("lucid") or {}).get("contracts") or {}

    # ---------------- reset marker
    def _reset_marker(self) -> dict:
        m = _read(self.state / "reset.json") or {}
        tok = (self.cfg.get("reset") or {}).get("token")
        if tok and m.get("token") == tok:
            return m
        return {"token": tok or m.get("token"), "at": m.get("at")}

    def since(self) -> datetime | None:
        return _dt(self.reset.get("at"))

    # ---------------- specs
    def pv(self, venue: str, p: dict) -> float:
        if venue == OV:
            from .other_venue import spec_for
            return float(spec_for(self.cfg, p.get("symbol"))["point_value"])
        c = self.contracts.get(p.get("contract")) or {}
        return float(c.get("point_value") or 1.0)

    def comm_rt(self, venue: str, p: dict) -> float:
        if venue == OV:
            return float(p.get("entry_fee") or 0) * 2
        c = self.contracts.get(p.get("contract")) or {}
        return 2 * float(c.get("commission") or 0) * float(p.get("qty") or 1)

    def fees_of(self, venue: str, p: dict, row: dict | None = None) -> float:
        """Round-trip fees in USD. Prefer a recorded total, then entry_fee*2 / contract commission."""
        src = {**(row or {}), **{k: v for k, v in (p or {}).items() if v is not None}}
        if isinstance(src.get("fees"), (int, float)):
            return float(src["fees"])
        if isinstance(src.get("fees_usd"), (int, float)) and (venue != OV or src.get("fees_usd")):
            # Use a previously stored value only as a fallback after recorded fees.
            stored = float(src["fees_usd"])
        else:
            stored = None
        if venue == OV:
            if isinstance(src.get("entry_fee"), (int, float)):
                return float(src["entry_fee"]) * 2
            try:
                from .other_venue import fee_usd
                entry = float(src.get("entry") or 0)
                qty = float(src.get("qty") or 1)
                if entry and qty:
                    return fee_usd(self.cfg, qty, entry) * 2
            except Exception:
                pass
            return stored or 0.0
        contract = src.get("contract") or (row or {}).get("contract")
        qty = src.get("qty") or (row or {}).get("qty") or 1
        return self.comm_rt(venue, {**src, "contract": contract, "qty": qty})

    # ---------------- adapters: open positions
    def open_positions(self) -> list[tuple[str, str, dict, dict]]:
        """[(book, venue, position, account_summary)]"""
        out = []
        S = self.state
        lu = _read(S / "lucid" / "account.json") or {}
        for p in lu.get("positions") or []:
            out.append(("MAIN", LUCID, p))
        eb = (_read(S / "exit_books.json") or {}).get("books") or {}
        for b in ("1-strike", "nostop"):
            for p in (eb.get(b) or {}).get("positions") or []:
                out.append((b, LUCID, p))
        for p in (_read(S / "bracket" / "account.json") or {}).get("positions") or []:
            out.append(("bracket", LUCID, p))
        for b in ("CB-BRACKET", "CB-RUNNER"):
            for p in (_read(S / "carbon" / f"{b}.json") or {}).get("positions") or []:
                out.append((b, LUCID, p))
        for b in ("C-OPUS", "C-SONNET"):
            for p in (_read(S / "claude" / "sims" / f"{b}.json") or {}).get("positions") or []:
                out.append((b, LUCID, p))
        for b, d in (("C-OPUS-B", "opus_b"), ("DS-FLASH", "deepseek")):
            for p in (_read(S / d / "account.json") or {}).get("positions") or []:
                out.append((b, LUCID, p))
        for b, v in ((_read(S / "other_venue" / "books.json") or {}).get("books") or {}).items():
            for p in v.get("positions") or []:
                out.append((b, OV, p))
        return out

    def accounts(self) -> dict[tuple[str, str], float | None]:
        """Realized balance per (book, venue) from each book's own state."""
        S = self.state
        out = {}
        lu = _read(S / "lucid" / "account.json") or {}
        out[("MAIN", LUCID)] = (lu.get("account") or {}).get("balance")
        eb = (_read(S / "exit_books.json") or {}).get("books") or {}
        for b in ("1-strike", "nostop"):
            out[(b, LUCID)] = (eb.get(b) or {}).get("balance")
        out[("bracket", LUCID)] = (_read(S / "bracket" / "account.json") or {}).get("balance")
        for b in ("CB-BRACKET", "CB-RUNNER"):
            out[(b, LUCID)] = (_read(S / "carbon" / f"{b}.json") or {}).get("balance")
        for b in ("C-OPUS", "C-SONNET"):
            out[(b, LUCID)] = (_read(S / "claude" / "sims" / f"{b}.json") or {}).get("balance")
        for b, d in (("C-OPUS-B", "opus_b"), ("DS-FLASH", "deepseek")):
            out[(b, LUCID)] = ((_read(S / d / "account.json") or {}).get("account") or {}).get("balance")
        for b, v in ((_read(S / "other_venue" / "books.json") or {}).get("books") or {}).items():
            out[(b, OV)] = (v.get("account") or {}).get("balance")
        return out

    # ---------------- adapters: closes (trade logs)
    def _log_rows(self, name: str):
        since = self.since()
        lo = _prev_day(since.date().isoformat()) if since else "0000"   # day folders may be UTC or Dubai dates
        for d in sorted(self.logs.glob("20??-??-??")):
            if d.name < lo:
                continue
            f = d / name
            if not f.exists():
                continue
            with open(f) as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue

    def closes(self) -> list[tuple[str, str, dict]]:
        out = []
        for r in self._log_rows("lucid_trades.jsonl"):
            out.append(("MAIN", LUCID, r))
        for r in self._log_rows("exit_book_trades.jsonl"):
            if r.get("event") == "close" and r.get("book") in ("1-strike", "nostop"):
                out.append((r["book"], LUCID, r))
        for r in self._log_rows("bracket_trades.jsonl"):
            if r.get("event", "close") == "close":
                out.append(("bracket", LUCID, r))
        for r in self._log_rows("carbon_trades.jsonl"):
            if r.get("event", "close") == "close" and r.get("book") in ("CB-BRACKET", "CB-RUNNER"):
                out.append((r["book"], LUCID, r))
        for r in self._log_rows("claude_trades.jsonl"):
            if r.get("book") == "real" and r.get("sim") in ("C-OPUS", "C-SONNET"):
                out.append((r["sim"], LUCID, r))
        for r in self._log_rows("opus_b_trades.jsonl"):
            out.append(("C-OPUS-B", LUCID, r))
        for r in self._log_rows("deepseek_trades.jsonl"):
            out.append(("DS-FLASH", LUCID, r))
        for r in self._log_rows("other_venue_trades.jsonl"):
            if r.get("book") and r.get("t_exit"):
                out.append((r["book"], OV, {**r, "entry_time": r.get("entry_time") or r.get("t_entry")}))
        return out

    # ---------------- update
    def _row(self, book, venue, p) -> dict | None:
        since = self.since()
        et = _dt(p.get("entry_time"))
        if since is not None and (et is None or et < since):
            return None
        k = _key(book, venue, p.get("symbol"), p.get("entry_time"))
        rows = self.s["rows"]
        if k not in rows:
            rows[k] = {"key": k, "book": book, "venue": venue, "model": MODEL_OF.get(book) or p.get("model"),
                       "symbol": p.get("symbol"), "contract": p.get("contract"),
                       "side": "LONG" if _side(p.get("side")) > 0 else "SHORT", "qty": p.get("qty"),
                       "entry_time": _iso(et), "entry": p.get("entry"), "stop": p.get("stop"),
                       "target": p.get("target"), "atr": p.get("atr"), "status": "OPEN",
                       "max_pnl_usd": 0.0, "min_pnl_usd": 0.0, "last_pnl_usd": 0.0, "last_price": None,
                       "close_reason": None, "exit": None, "t_exit": None, "net_at_close_usd": None,
                       "fees_usd": None, "went_positive_first": None, "first_cross": None,
                       "first_cross_at": None, "first_cross_basis": None, "id": p.get("id")}
        row = rows[k]
        fees = self.fees_of(venue, p, row)
        if fees and not row.get("fees_usd"):
            row["fees_usd"] = round(fees, 4)
        return row

    def _merge_ext(self, row, venue, p, price=None) -> None:
        side = _side(p.get("side"))
        qty = float(p.get("qty") or row.get("qty") or 1)
        pv = self.pv(venue, {**p, "contract": p.get("contract") or row.get("contract"), "symbol": row["symbol"]})
        entry = float(row["entry"] or p.get("entry") or 0)
        cands = []
        for k in ("mfe", "mfe_pts"):
            if isinstance(p.get(k), (int, float)):
                cands.append(("max", float(p[k]) * qty * pv))
        for k in ("mae", "mae_pts"):
            if isinstance(p.get(k), (int, float)):
                cands.append(("min", -abs(float(p[k])) * qty * pv))
        atr = p.get("atr") or row.get("atr")
        if atr:
            if isinstance(p.get("mfe_atr"), (int, float)):
                cands.append(("max", float(p["mfe_atr"]) * float(atr) * qty * pv))
            if isinstance(p.get("mae_atr"), (int, float)):
                cands.append(("min", -abs(float(p["mae_atr"])) * float(atr) * qty * pv))
        if isinstance(p.get("mfe_usd"), (int, float)):
            cands.append(("max", float(p["mfe_usd"])))
        if price is not None and entry:
            g = side * (float(price) - entry) * qty * pv
            cands += [("max", g), ("min", g)]
        for kind, v in cands:
            if kind == "max":
                row["max_pnl_usd"] = round(max(row["max_pnl_usd"], v), 2)
            else:
                row["min_pnl_usd"] = round(min(row["min_pnl_usd"], v), 2)

    async def _quotes(self, syms: list[str]) -> dict[str, float]:
        if not syms:
            return {}
        try:
            if self.quote_fn is not None:
                q = await self.quote_fn(syms)
            else:
                from .live_price import fetch_fill_quotes
                q = await fetch_fill_quotes(syms, self.cfg, datetime.now(UTC))
        except Exception as e:
            log.warning("panel quotes failed: %s", e)
            return {}
        out = {}
        for s, v in (q or {}).items():
            px = v.get("price") if isinstance(v, dict) else v
            if px is not None:
                out[s] = float(px)
        return out

    async def tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        self.reset = self._reset_marker()
        opens = self.open_positions()
        quotes = await self._quotes(sorted({p.get("symbol") for _, _, p in opens if p.get("symbol")}))
        live_keys = set()
        for book, venue, p in opens:
            row = self._row(book, venue, p)
            if row is None:
                continue
            live_keys.add(row["key"])
            px = quotes.get(p.get("symbol"))
            if px is None:
                px = p.get("mark") or p.get("last")
            row["status"] = "OPEN"
            if px is not None:
                side = _side(p.get("side"))
                qty = float(p.get("qty") or row.get("qty") or 1)
                pv = self.pv(venue, p)
                g = side * (float(px) - float(row["entry"])) * qty * pv
                row["last_pnl_usd"] = round(g, 2)
                row["last_price"] = float(px)
                row["last_at"] = _iso(now)
                fees = self.fees_of(venue, p, row)
                row["fees_usd"] = round(fees, 4)
                fc.observe_print(row, float(px), now, side=side, entry=float(row["entry"]),
                                 qty=qty, pv=pv, fees=fees, basis="live_net_tick")
            self._merge_ext(row, venue, p, px)
        for book, venue, r in self.closes():
            row = self._row(book, venue, r)
            if row is None:
                continue
            if row["key"] in live_keys and row["status"] == "OPEN" and not r.get("t_exit"):
                continue
            row["status"] = status_of(r.get("reason"))
            row["close_reason"] = r.get("reason")
            row["exit"] = r.get("exit")
            row["t_exit"] = _iso(_dt(r.get("t_exit")))
            row["net_at_close_usd"] = r.get("net")
            row["last_pnl_usd"] = r.get("gross") if r.get("gross") is not None else r.get("net")
            if r.get("id") is not None:
                row["id"] = r.get("id")
            fees = self.fees_of(venue, r, row)
            row["fees_usd"] = round(fees, 4)
            self._merge_ext(row, venue, {**r, "contract": r.get("contract") or row.get("contract")}, r.get("exit"))
        await self._backfill(now)
        # Exit print is a last-resort crossing only if the live path and the backfill left it unresolved.
        for book, venue, r in self.closes():
            row = self._row(book, venue, r)
            if row is None or fc.resolved(row) or r.get("exit") is None:
                continue
            fc.observe_print(row, float(r["exit"]), _dt(r.get("t_exit")) or now,
                             side=_side(r.get("side") or row.get("side")),
                             entry=float(row["entry"]), qty=float(r.get("qty") or row.get("qty") or 1),
                             pv=self.pv(venue, {**r, "contract": r.get("contract") or row.get("contract"),
                                                "symbol": row["symbol"]}),
                             fees=float(row.get("fees_usd") or self.fees_of(venue, r, row)),
                             basis="exit_print")
        # A row that vanished from the book without a close record yet: keep OPEN until its log row lands.
        self.s["updated_at"] = _iso(now)
        _atomic_write(self.path, self.s)
        site = self.site_payload(now)
        _atomic_write(self.site / "panel.json", site)
        return {"open": len(live_keys), "rows": len(self.s["rows"])}

    async def _backfill(self, now: datetime) -> None:
        """Fill went-positive-first from M1 (and ticks if needed) for rows not yet resolved."""
        if self.history_fn is False:
            return
        need = [r for r in self.s["rows"].values()
                if not fc.resolved(r) and not r.get("first_cross_backfill")]
        if not need:
            return
        by_sym: dict[str, list[dict]] = {}
        for r in need:
            if r.get("symbol") and r.get("entry_time"):
                by_sym.setdefault(r["symbol"], []).append(r)
        for sym, rows in by_sym.items():
            starts = [fc._dt(r["entry_time"]) for r in rows]
            ends = [fc._dt(r.get("t_exit")) or now for r in rows]
            start, end = min(starts), max(ends)
            m1 = ticks = None
            try:
                if callable(self.history_fn):
                    path = await self.history_fn(sym, start, end)
                    path = path or {}
                    m1, ticks = path.get("m1"), path.get("ticks")
                else:
                    m1, _src = await fc.fetch_m1_range(sym, start, end, self.cfg)
                    try:
                        ticks, _ts = await fc.fetch_tick_range(sym, start, end, self.cfg)
                    except Exception as e:
                        log.warning("first-cross tick backfill %s failed: %s", sym, e)
                        ticks = None
            except Exception as e:
                log.warning("first-cross backfill %s failed: %s", sym, e)
                continue
            # First pass without ticks (or with whatever the injected fn already gave us).
            rerun = []
            for r in rows:
                res = fc.first_cross_from_history(
                    m1, side=1 if r.get("side") == "LONG" else -1,
                    entry=float(r["entry"]), qty=float(r.get("qty") or 1),
                    pv=self.pv(r["venue"], r), fees=float(r.get("fees_usd") or self.fees_of(r["venue"], r, r) or 0),
                    entry_time=r.get("entry_time"), exit_time=r.get("t_exit"),
                    ticks=ticks, exit_price=r.get("exit") or r.get("last_price"))
                if res.get("first_cross") == "ambiguous" or res.get("first_cross") is None:
                    rerun.append(r)
                else:
                    fc.apply_result(r, res)
                    r["first_cross_backfill"] = "done"
            if rerun and ticks is None and self.history_fn is not False:
                try:
                    if callable(self.history_fn):
                        path = await self.history_fn(sym, start, end, want_ticks=True) if                             _accepts_want_ticks(self.history_fn) else None
                        ticks = (path or {}).get("ticks") if path else None
                    else:
                        ticks, _src = await fc.fetch_tick_range(sym, start, end, self.cfg)
                except TypeError:
                    ticks = None
                except Exception as e:
                    log.warning("first-cross tick backfill %s failed: %s", sym, e)
                    ticks = None
            for r in rerun:
                res = fc.first_cross_from_history(
                    m1, side=1 if r.get("side") == "LONG" else -1,
                    entry=float(r["entry"]), qty=float(r.get("qty") or 1),
                    pv=self.pv(r["venue"], r), fees=float(r.get("fees_usd") or self.fees_of(r["venue"], r, r) or 0),
                    entry_time=r.get("entry_time"), exit_time=r.get("t_exit"),
                    ticks=ticks, exit_price=r.get("exit") or r.get("last_price"))
                fc.apply_result(r, res)
                # Ambiguous without ticks stays "?". A full window with neither side is "—" and done.
                r["first_cross_backfill"] = "done"

    # ---------------- site
    def site_payload(self, now: datetime) -> dict:
        rows = sorted(self.s["rows"].values(), key=lambda r: r.get("entry_time") or "", reverse=True)
        bal = self.accounts()
        perf = []
        for book, model, label in BOOKS:
            for venue in (LUCID, OV):
                rs = [r for r in rows if r["book"] == book and r["venue"] == venue]
                closed = [r for r in rs if r["status"] != "OPEN" and r.get("net_at_close_usd") is not None]
                opn = [r for r in rs if r["status"] == "OPEN"]
                wins = sum(1 for r in closed if float(r["net_at_close_usd"]) > 0)
                losses = sum(1 for r in closed if float(r["net_at_close_usd"]) <= 0)
                net = round(sum(float(r["net_at_close_usd"]) for r in closed), 2)
                op = round(sum(float(r.get("last_pnl_usd") or 0) for r in opn), 2)
                b = bal.get((book, venue))
                wpf = fc.book_share(rs)
                for r in rs:
                    r["went_positive_first_label"] = fc.display_of(r)
                perf.append({"book": book, "venue": venue, "model": model, "label": label,
                             "balance": round(float(b), 2) if b is not None else round(START + net, 2),
                             "start_balance": START, "net_pnl_usd": net, "open_pnl_usd": op,
                             "trades": len(closed), "wins": wins, "losses": losses,
                             "win_rate": round(wins / len(closed), 3) if closed else None,
                             "open_positions": len(opn), **wpf})
        return {"updated_at": _iso(now), "paper_only": True,
                "reset": {"token": self.reset.get("token"), "at": self.reset.get("at")},
                "exit_rule": "uniform-1atr-v1: 1 ATR stop · 1 ATR target · 90-min cap (all books, no model exits)",
                "pnl_note": ("Max / lowest $ P&L: gross mark-to-market reached while open (live quote every "
                             f"{int(self.pc.get('poll_seconds', 15))} s, plus the book's own recorded excursion and "
                             "the exit price). P&L at close is net of commissions/fees. "
                             "Went positive first: net unrealized P&L (gross minus round-trip fees) rose above $0 "
                             "before gross fell below $0. The fee cushion (gross between $0 and fees) is neither "
                             "side; the entry fill is not a crossing. Open positions that have not yet done either "
                             "show —. If both sides occur in the same M1 bar and tick order is unavailable, the "
                             "cell is ?. Going forward this uses the 15s live-quote poll (same path as the exit "
                             "checks). Already-open / already-closed positions are backfilled from the same 15s "
                             "last-print path reconstructed from Dukascopy or Binance ticks when available, "
                             "otherwise from M1 bars; a bar that hits both sides with no 15s path is ?."),
                "wpf_basis": ("net_usd = gross - round-trip fees; live_net_tick / tick_15s = 15s last-print path; "
                              "m1_ohlc = M1 fallback"),
                "performance": perf, "positions": rows}


def _accepts_want_ticks(fn) -> bool:
    try:
        import inspect
        return "want_ticks" in inspect.signature(fn).parameters
    except Exception:
        return False


def _prev_day(day: str) -> str:
    try:
        from datetime import date, timedelta
        return (date.fromisoformat(day[:10]) - timedelta(days=1)).isoformat()
    except Exception:
        return "0000"


def write_reset_marker(cfg: dict, state_dir: Path, now: datetime | None = None) -> dict | None:
    """Stamp state/reset.json the first time a new [reset].token is seen (the shared reset time)."""
    tok = (cfg.get("reset") or {}).get("token")
    if not tok:
        return None
    p = Path(state_dir) / "reset.json"
    cur = _read(p) or {}
    if cur.get("token") == tok:
        return cur
    now = now or datetime.now(UTC)
    m = {"token": tok, "at": _iso(now), "reason": (cfg.get("reset") or {}).get("reason"),
         "previous": cur or None, "books": [b for b, _, _ in BOOKS], "venues": [LUCID, OV]}
    _atomic_write(p, m)
    return m


async def run_panel_loop(led: PanelLedger, stop: asyncio.Event | None = None, io_lock=None) -> None:
    period = float(led.pc.get("poll_seconds", 15))
    while stop is None or not stop.is_set():
        try:
            await led.tick()
        except Exception:
            log.exception("panel tick failed")
        await asyncio.sleep(period)
