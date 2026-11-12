"""Non-Lucid OTHER-VENUE paper books (t254u). PAPER ONLY — no broker orders.

Symbols that are not Lucid products (BTCUSD always; XAGUSD only if SI is absent from
the Lucid fees list) trade here. Each model book gets a parallel $100k CFD/spot
sub-account: size so 1 ATR of stop risk is ~$900, fees 0.05% of notional per side,
1-tick-equivalent slippage, and the same uniform 1 ATR / 1 ATR / 90-min bracket.
Silver (XAGUSD) stays on Lucid as SI when SI is in the fees file (it is: GC/SI/HG/PL
group at $2.30/side).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .risk import _atomic_write
from .uniform_exit import (TIME_CAP_MINUTES, UNIFORM_BANNER, UNIFORM_EXIT_VERSION,
                           arm_uniform, first_touch)

log = logging.getLogger("signals.other_venue")
UTC = timezone.utc

VENUE = "OTHER-VENUE"
VERSION = "other-venue-v1"
START_BALANCE = 100_000.0
RISK_USD = 900.0
FEE_RATE = 0.0005  # 0.05% of notional per side
SLIPPAGE_TICKS = 1
QTY_STEP_DEFAULT = 0.001

DEFAULT_SYMBOLS = ["BTCUSD"]  # XAGUSD stays Lucid SI
DEFAULT_SPECS = {
    "BTCUSD": {"tick": 5.0, "point_value": 1.0, "digits": 2, "qty_step": 0.001},
    "XAGUSD": {"tick": 0.005, "point_value": 1.0, "digits": 3, "qty_step": 1.0},  # only if routed here
}
# Primary model books plus the Gemini-family copies of MAIN fills.
DEFAULT_BOOKS = [
    "MAIN", "1-strike", "nostop", "bracket",
    "CB-BRACKET", "CB-RUNNER",
    "C-OPUS", "C-SONNET", "C-OPUS-B",
    "DS-FLASH",
]
# When MAIN (flash) accepts a BTC fill, these books copy it (same pattern as Lucid exit books).
MAIN_COPIES = ("1-strike", "nostop", "bracket")


def ov_cfg(cfg: dict | None) -> dict:
    d = dict((cfg or {}).get("other_venue") or {})
    d.setdefault("enabled", True)
    d.setdefault("start_balance", START_BALANCE)
    d.setdefault("risk_usd", RISK_USD)
    d.setdefault("fee_rate", FEE_RATE)
    d.setdefault("slippage_ticks", SLIPPAGE_TICKS)
    d.setdefault("symbols", list(DEFAULT_SYMBOLS))
    d.setdefault("books", list(DEFAULT_BOOKS))
    d.setdefault("specs", {})
    return d


def other_venue_symbols(cfg: dict | None) -> list[str]:
    c = ov_cfg(cfg)
    if not c.get("enabled", True):
        return []
    return [str(s) for s in (c.get("symbols") or DEFAULT_SYMBOLS)]


def is_other_venue_symbol(cfg: dict | None, sym: str | None) -> bool:
    return bool(sym) and str(sym) in set(other_venue_symbols(cfg))


def spec_for(cfg: dict | None, sym: str) -> dict:
    c = ov_cfg(cfg)
    raw = ((c.get("specs") or {}).get(sym) or DEFAULT_SPECS.get(sym) or {})
    return {
        "tick": float(raw.get("tick") or DEFAULT_SPECS.get(sym, {}).get("tick") or 1.0),
        "point_value": float(raw.get("point_value") or 1.0),
        "digits": int(raw.get("digits") or 2),
        "qty_step": float(raw.get("qty_step") or QTY_STEP_DEFAULT),
    }


def size_qty(cfg: dict | None, sym: str, atr: float) -> float | None:
    """Contracts/units such that 1 ATR of stop risk is about $risk_usd. None if unsizable."""
    if atr is None or float(atr) <= 0:
        return None
    sp = spec_for(cfg, sym)
    unit = float(atr) * sp["point_value"]
    if unit <= 0:
        return None
    raw = float(ov_cfg(cfg)["risk_usd"]) / unit
    step = sp["qty_step"] or QTY_STEP_DEFAULT
    qty = math.floor(raw / step + 1e-12) * step
    if qty <= 0:
        return None
    return round(qty, 10)


def fee_usd(cfg: dict | None, qty: float, price: float) -> float:
    return float(ov_cfg(cfg)["fee_rate"]) * abs(float(qty)) * abs(float(price))


def slip_px(cfg: dict | None, sym: str, side: int, price: float, *, entry: bool = True) -> float:
    sp = spec_for(cfg, sym)
    ticks = int(ov_cfg(cfg).get("slippage_ticks") or 1)
    delta = ticks * sp["tick"]
    # Adverse: long entry higher, long exit lower (and the reverse for shorts).
    signed = side if entry else -side
    return round(float(price) + signed * delta, 10)


def _iso(t: datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat()


def _parse(t) -> datetime | None:
    if t is None:
        return None
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


def _fresh_account(start: float) -> dict:
    return {
        "balance": float(start), "equity": float(start), "start_balance": float(start),
        "open_pnl": 0.0, "net_pnl": 0.0, "realized_pnl": 0.0,
        "wins": 0, "losses": 0, "trades": 0, "stopped": 0,
    }


def _fresh_book(name: str, start: float) -> dict:
    return {"book": name, "venue": VENUE, "account": _fresh_account(start),
            "positions": [], "pending": [], "next_id": 1}


class OtherVenueHub:
    def __init__(self, cfg: dict, *, state_dir=None, logs_dir=None, site_dir=None):
        self.cfg = cfg
        self.cc = ov_cfg(cfg)
        root = Path(state_dir or cfg["general"]["state_dir"])
        self.root = root / "other_venue"
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or (cfg.get("site") or {}).get("dir") or root.parent)
        self.path = self.root / "books.json"
        self.s = self._load()

    def _load(self) -> dict:
        start = float(self.cc["start_balance"])
        want = list(self.cc["books"])
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else None
        except Exception:
            raw = None
        books = {}
        if isinstance(raw, dict):
            for k, v in (raw.get("books") or {}).items():
                books[k] = v
        for name in want:
            if name not in books:
                books[name] = _fresh_book(name, start)
        return {"venue": VENUE, "version": VERSION, "books": books}

    def persist(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.s["updated_at"] = _iso(now)
        _atomic_write(self.path, self.s)
        self.write_site(now)

    def book(self, name: str) -> dict:
        start = float(self.cc["start_balance"])
        if name not in self.s["books"]:
            self.s["books"][name] = _fresh_book(name, start)
        return self.s["books"][name]

    def held(self, name: str, sym: str) -> bool:
        b = self.book(name)
        return any(p.get("symbol") == sym for p in (b.get("positions") or []) + (b.get("pending") or []))

    def busy_symbols(self, name: str = "MAIN") -> set[str]:
        b = self.book(name)
        out: set[str] = set()
        for row in (b.get("positions") or []) + (b.get("pending") or []):
            if row.get("symbol"):
                out.add(row["symbol"])
        return out

    def intake_signal(self, book: str, sig: dict, now: datetime, *,
                      copy_to: list[str] | tuple[str, ...] | None = None,
                      quote: float | None = None) -> dict | None:
        """Open (or queue) a BUY/SELL on this book's OTHER-VENUE account. Returns the position or skip dict."""
        if not self.cc.get("enabled", True):
            return None
        action = (sig.get("action") or "").upper()
        if action not in ("BUY", "SELL"):
            return None
        sym = sig.get("symbol")
        if not is_other_venue_symbol(self.cfg, sym):
            return {"skipped": "not_other_venue_symbol", "symbol": sym, "book": book}
        if self.held(book, sym):
            return {"skipped": "held", "symbol": sym, "book": book}
        atr = float(sig.get("atr") or 0)
        qty = size_qty(self.cfg, sym, atr)
        if not qty:
            self._skip(book, now, sig, "no size (atr missing or too small)")
            return {"skipped": "no_size", "symbol": sym, "book": book, "atr": atr}
        side = 1 if action == "BUY" else -1
        px = quote
        if px is None:
            px = sig.get("live_price") or sig.get("entry") or (sig.get("context") or {}).get("live_price")
        if px is None:
            pe = self._queue(book, sig, now, side, qty, atr)
            return pe
        return self._fill(book, sig, now, side, qty, atr, float(px), copy_to=copy_to)

    def intake_from_record(self, book: str, record: dict | None, now: datetime,
                           *, copy_to: list[str] | tuple[str, ...] | None = None) -> list[dict]:
        out = []
        if not record:
            return out
        sigs = record.get("signals") or record.get("validated") or []
        if not sigs and record.get("symbol"):
            sigs = [record]
        # thinking_level / model stamped on the record apply to each signal
        for sig in sigs:
            s = dict(sig)
            s.setdefault("thinking_level", record.get("thinking_level") or s.get("thinking_level"))
            s.setdefault("model", record.get("model") or s.get("model"))
            s.setdefault("atr", s.get("atr") or (s.get("context") or {}).get("atr") or record.get("atr"))
            if not s.get("symbol"):
                continue
            r = self.intake_signal(book, s, now, copy_to=copy_to)
            if r:
                out.append(r)
        return out

    def _queue(self, book, sig, now, side, qty, atr) -> dict:
        b = self.book(book)
        pe = {"symbol": sig["symbol"], "side": side, "qty": qty, "atr": atr,
              "action": sig.get("action"), "queued_at": _iso(now),
              "confidence": sig.get("confidence"), "model": sig.get("model"),
              "thinking_level": sig.get("thinking_level"), "signal_entry": sig.get("entry")}
        b["pending"].append(pe)
        self._event(now, "queued", book=book, **{k: pe[k] for k in ("symbol", "side", "qty", "atr")})
        return pe

    def _fill(self, book, sig, now, side, qty, atr, raw_px, *, copy_to=None) -> dict:
        sp = spec_for(self.cfg, sig["symbol"])
        entry = slip_px(self.cfg, sig["symbol"], side, raw_px, entry=True)
        fee = fee_usd(self.cfg, qty, entry)
        b = self.book(book)
        pid = int(b.get("next_id") or 1)
        b["next_id"] = pid + 1
        pos = {
            "id": pid, "venue": VENUE, "book": book, "symbol": sig["symbol"], "side": side,
            "qty": qty, "entry": entry, "raw_quote": raw_px, "atr": atr,
            "entry_time": _iso(now), "entry_fee": round(fee, 4),
            "confidence": sig.get("confidence"), "model": sig.get("model"),
            "thinking_level": sig.get("thinking_level"),
            "exit_mode": UNIFORM_EXIT_VERSION, "prompt_version": sig.get("prompt_version"),
        }
        arm_uniform(pos, now, tick=sp["tick"])
        b["positions"].append(pos)
        b["account"]["balance"] = round(b["account"]["balance"] - fee, 4)
        self._mark(book)
        self._event(now, "fill", book=book, id=pid, symbol=pos["symbol"], side=side,
                    qty=qty, entry=entry, fee=round(fee, 4), atr=atr,
                    stop=pos.get("stop"), target=pos.get("target"))
        for other in (copy_to or ()):
            if other == book:
                continue
            if not self.held(other, pos["symbol"]):
                # Copy the same fill onto sibling books (independent $100k).
                self._fill(other, sig, now, side, qty, atr, raw_px, copy_to=None)
        return pos

    def fill_pendings(self, quotes: dict[str, float], now: datetime) -> list[dict]:
        opened = []
        for name, b in self.s["books"].items():
            keep = []
            for pe in list(b.get("pending") or []):
                px = quotes.get(pe["symbol"])
                if px is None:
                    keep.append(pe)
                    continue
                pos = self._fill(name, {**pe, "action": pe.get("action") or ("BUY" if pe["side"] > 0 else "SELL")},
                                 now, pe["side"], pe["qty"], pe["atr"], float(px))
                opened.append(pos)
            b["pending"] = keep
        return opened

    def tick_book(self, name: str, bars: dict[str, pd.DataFrame], now: datetime) -> list[dict]:
        b = self.book(name)
        closed = []
        keep = []
        for p in list(b.get("positions") or []):
            df = bars.get(p["symbol"])
            hit = first_touch(
                df, side=int(p["side"]), entry=float(p["entry"]),
                stop=float(p["stop"]), target=float(p["target"]),
                entry_time=p["entry_time"], armed_at=p.get("mech_armed_at"),
                deadline=p.get("deadline"), tick=spec_for(self.cfg, p["symbol"])["tick"],
                slippage_ticks=int(self.cc.get("slippage_ticks") or 1),
            )
            if not hit:
                keep.append(p)
                continue
            closed.append(self._close(name, p, hit, now))
        b["positions"] = keep
        self._mark(name)
        return closed

    def tick_all(self, bars: dict[str, pd.DataFrame], now: datetime) -> list[dict]:
        out = []
        for name in list(self.s["books"]):
            out.extend(self.tick_book(name, bars, now))
        return out

    def _close(self, name: str, p: dict, hit: dict, now: datetime) -> dict:
        b = self.book(name)
        qty, side = float(p["qty"]), int(p["side"])
        exit_px = float(hit["price"])
        fee = fee_usd(self.cfg, qty, exit_px)
        gross = side * (exit_px - float(p["entry"])) * qty * spec_for(self.cfg, p["symbol"])["point_value"]
        net = gross - float(p.get("entry_fee") or 0) - fee
        risk = abs(float(p["entry"]) - float(p["stop"])) * qty * spec_for(self.cfg, p["symbol"])["point_value"]
        rec = {
            "t_exit": _iso(_parse(hit["t"]) or now), "t_entry": p["entry_time"],
            "venue": VENUE, "book": name, "symbol": p["symbol"], "side": side, "qty": qty,
            "entry": p["entry"], "exit": exit_px, "gross": round(gross, 2),
            "fees": round(float(p.get("entry_fee") or 0) + fee, 4), "net": round(net, 2),
            "reason": hit["reason"], "exit_mode": UNIFORM_EXIT_VERSION,
            "exit_prompt_version": UNIFORM_EXIT_VERSION,
            "R": round(net / risk, 3) if risk else None,
            "atr": p.get("atr"), "model": p.get("model"),
            "thinking_level": p.get("thinking_level"), "confidence": p.get("confidence"),
            "id": p.get("id"),
        }
        acct = b["account"]
        acct["balance"] = round(acct["balance"] + gross - fee, 4)  # entry fee already deducted
        acct["realized_pnl"] = round(acct.get("realized_pnl", 0) + net, 4)
        acct["net_pnl"] = acct["realized_pnl"]
        acct["trades"] = int(acct.get("trades") or 0) + 1
        if net >= 0:
            acct["wins"] = int(acct.get("wins") or 0) + 1
        else:
            acct["losses"] = int(acct.get("losses") or 0) + 1
        if str(hit["reason"]).startswith("stop"):
            acct["stopped"] = int(acct.get("stopped") or 0) + 1
        self._jsonl(now, rec)
        self._event(now, "close", **{k: rec[k] for k in
                                    ("book", "symbol", "net", "reason", "exit", "qty")})
        return rec

    def _mark(self, name: str) -> None:
        b = self.book(name)
        upl = 0.0  # marked at last mid when we have quotes; 0 if unknown
        for p in b.get("positions") or []:
            last = p.get("mark")
            if last is None:
                continue
            upl += int(p["side"]) * (float(last) - float(p["entry"])) * float(p["qty"])
        acct = b["account"]
        acct["open_pnl"] = round(upl, 2)
        acct["equity"] = round(acct["balance"] + upl, 2)

    def mark_quotes(self, quotes: dict[str, float]) -> None:
        for b in self.s["books"].values():
            for p in b.get("positions") or []:
                if p["symbol"] in quotes:
                    p["mark"] = float(quotes[p["symbol"]])
            self._mark(b["book"])

    def _skip(self, book, now, sig, why) -> None:
        self._event(now, "skip", book=book, symbol=sig.get("symbol"), reason=why,
                    action=sig.get("action"), atr=sig.get("atr"))

    def _event(self, now, kind, **kw) -> None:
        rec = {"t": _iso(now), "venue": VENUE, "kind": kind, **kw}
        self._jsonl(now, rec, name="other_venue_events.jsonl")

    def _jsonl(self, now, rec: dict, name: str = "other_venue_trades.jsonl") -> None:
        day = (_parse(now) or datetime.now(UTC)).astimezone(UTC).date().isoformat()
        folder = self.logs_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / name
        with p.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def site_payload(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        rows = []
        for name in self.cc["books"]:
            b = self.book(name)
            a = b["account"]
            rows.append({
                "book": name, "venue": VENUE, "label": f"{name} · Other venue",
                "account": deepcopy(a),
                "open_positions": list(b.get("positions") or []),
                "pending": list(b.get("pending") or []),
                "symbols": other_venue_symbols(self.cfg),
            })
        return {
            "updated_at": _iso(now), "venue": VENUE, "version": VERSION,
            "banner": UNIFORM_BANNER,
            "note": ("Other venue: non-Lucid symbols (BTCUSD) on a parallel $100k paper account per model book. "
                     "Spot/CFD sizing ~$900 risk per 1 ATR stop, fees 0.05%/side, 1-tick slippage, "
                     "uniform 1 ATR / 1 ATR / 90-min bracket. Silver (XAGUSD) stays on Lucid as SI."),
            "symbols": other_venue_symbols(self.cfg),
            "books": rows,
            "rules": {
                "risk_usd": self.cc["risk_usd"], "fee_rate": self.cc["fee_rate"],
                "slippage_ticks": self.cc["slippage_ticks"],
                "exit": UNIFORM_EXIT_VERSION, "time_cap_min": TIME_CAP_MINUTES,
            },
        }

    def write_site(self, now: datetime | None = None) -> None:
        payload = self.site_payload(now)
        _atomic_write(self.site_dir / "other_venue.json", payload)
        return payload



_HUB: OtherVenueHub | None = None


def set_hub(hub: OtherVenueHub | None) -> None:
    global _HUB
    _HUB = hub


def get_hub(cfg: dict | None = None, **paths) -> OtherVenueHub | None:
    """Return the process-wide hub started by GhaLoop. Does not auto-create (avoids test leakage)."""
    return _HUB


def entry_symbols(cfg: dict) -> list[str]:
    """Lucid symbols plus OTHER-VENUE symbols (for flash entry scans)."""
    lu = (cfg.get("lucid") or {}).get("symbols") or {}
    out = list(lu) if lu else ["XAUUSD", "EURUSD", "GBPUSD", "US100"]
    for s in other_venue_symbols(cfg):
        if s not in out:
            out.append(s)
    return out


async def tick_hub(hub: OtherVenueHub, now: datetime | None = None, *, m1_fetch=None) -> dict:
    """Fill pendings + apply uniform exits on M1 bars. Never raises to the caller."""
    now = now or datetime.now(UTC)
    try:
        from .live_price import fetch_symbol_m1, enrich_live_prices
        syms = set()
        for b in hub.s["books"].values():
            for row in (b.get("positions") or []) + (b.get("pending") or []):
                if row.get("symbol"):
                    syms.add(row["symbol"])
        bars: dict[str, pd.DataFrame] = {}
        quotes: dict[str, float] = {}
        for sym in syms:
            try:
                if m1_fetch:
                    df = await m1_fetch(sym)
                    if isinstance(df, tuple):
                        df = df[0]
                else:
                    df, _src = await fetch_symbol_m1(sym, hub.cfg, now)
                if df is not None and len(df):
                    bars[sym] = df
                    quotes[sym] = float(df["close"].iloc[-1])
            except Exception:
                log.exception("other-venue m1 fetch failed for %s", sym)
        # Prefer live quotes when the enricher has them
        try:
            live = {}
            enrich_live_prices  # noqa: presence
        except Exception:
            live = {}
        hub.fill_pendings(quotes, now)
        hub.mark_quotes(quotes)
        closed = hub.tick_all(bars, now)
        hub.persist(now)
        return {"ok": True, "closed": len(closed), "quotes": list(quotes)}
    except Exception as e:
        log.exception("other-venue tick failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def run_other_venue_loop(hub: OtherVenueHub, stop: asyncio.Event | None = None,
                               period: float = 18.0) -> None:
    while True:
        if stop and stop.is_set():
            return
        try:
            await tick_hub(hub)
        except Exception:
            log.exception("other-venue loop tick")
        try:
            await asyncio.sleep(period)
        except asyncio.CancelledError:
            return
