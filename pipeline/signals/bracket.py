"""'bracket' Gemini shadow book (PAPER ONLY - nothing here places an order anywhere).

Source: gemini-signals/analysis/edge_study.md ("most robust combo": confidence >= 0.75, no BTC, 1-ATR stop /
1-ATR target / 90-min cap, no Gemini exits; GBPUSD dropped because 6B costs ~0.45 ATR per round trip).

  Signals  the SAME validated Gemini entry signals the main Lucid account gets each cycle (no extra Gemini call).
           Taken only when action is BUY/SELL, confidence >= min_confidence (0.75) and symbol in XAUUSD / EURUSD /
           US100 (no GBPUSD, no BTCUSD). One position per symbol IN THIS BOOK, tracked independently of main: the
           bracket book takes a signal even when main already holds that symbol.
  Bracket  fixed at entry: stop = entry -/+ 1 M15 ATR (the signal's ATR14), target = entry +/- 1 ATR, 90-minute
           max hold, flat by 21:45 UTC. Gemini CLOSEs are ignored. Same-check stop+target -> stop (loss).
  Fills    entry at the live quote fetched at intake (same feed as the hard stop / 18 s exit checks:
           live_price.enrich_live_prices -> Dukascopy tick, US100 NQ+basis in NY RTH) + 1 adverse tick. Stop fills at
           the level + 1 tick (live price + 1 tick if it gapped through); target / time / flat exits 1 adverse tick.
           Real Lucid commission per side.
  Size     half of Lucid: qty = floor(risk_usd (900) / (1 ATR x point value)), at least 1 (skip if 1 contract
           risks more than max_risk_usd), at most max_minis (6) full-size contracts open across this book.

Scan limitation: pipeline.lucid_busy_symbols() drops symbols the MAIN account holds/pends from the batched entry
scan, so no signal exists for them that cycle; the bracket book cannot see it. Its own positions never feed that
set (the scan only reads state/lucid/account.json), so a symbol held only by this book is still scanned. Changing
the scan for main-held symbols would change main's Gemini prompt / call volume, so it is NOT done; every cycle in
which this book was flat on an eligible symbol that main excluded is counted (missed_scans) and logged.

State: state/bracket/account.json. Logs: logs/<day>/bracket_trades.jsonl (closes), bracket_events.jsonl.
Site: bracket.json at the data root (deployed with the panel).
"""
from __future__ import annotations
import asyncio, copy, json, logging, math, time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .risk import _atomic_write

log = logging.getLogger("signals.bracket")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
BOOK = "bracket"
LABEL = "Bracket shadow (conf>=0.75, 1 ATR / 1 ATR)"

DEFAULTS = {
    "enabled": True,
    "symbols": ["XAUUSD", "EURUSD", "US100"],
    "min_confidence": 0.75,
    "stop_atr": 1.0,
    "target_atr": 1.0,
    "max_hold_minutes": 90,
    "flat_utc": "21:45",
    "reopen_utc": "23:00",
    "no_new_entries_last_minutes": 15,
    "risk_usd": 900.0,
    "max_risk_usd": 1500.0,
    "max_minis": 6,
    "slippage_ticks": 1,
    "start_balance": 100000.0,
    "check_seconds": 18,
    "pending_expiry_seconds": 180,
    "max_chase_atr": 0.5,
    "gap_seconds": 180,
    "fill_quote_max_age_s": 60,
    "recent_trades": 20,
    "reset_token": "bracket-v1",
}


def bracket_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("bracket") or {})}


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _side_word(side: int) -> str:
    return "LONG" if side > 0 else "SHORT"


def round_away(x: float, entry: float, tick: float) -> float:
    """Round a bracket level to the tick, never closer to the entry than the exact distance."""
    n = x / tick
    n = math.ceil(n - 1e-9) if x >= entry else math.floor(n + 1e-9)
    return round(n * tick, 10)


def skip_key(why: str) -> str:
    """Short bucket for the skip counters (the full reason is in bracket_events.jsonl)."""
    for pre, k in (("symbol", "symbol"), ("confidence", "confidence<min"), ("already", "held_in_bracket"),
                   ("outside", "session"), ("1 ", "risk>max"), ("max ", "max_minis"), ("not BUY", "hold")):
        if why.startswith(pre):
            return k
    return why[:40]


def round_tick(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10)


class BracketBook:
    """Pure-ish accounting + I/O for the bracket shadow book. `quotes` is injectable for tests:
    async quotes(symbols, now) -> ({sym: quote dict}, {sym: M1 DataFrame})."""

    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *, state_dir=None, logs_dir=None,
                 site_dir=None, quotes=None):
        self.cfg = cfg
        self.b = bracket_cfg(cfg)
        self.L = cfg["lucid"]
        self.io_lock = io_lock or asyncio.Lock()
        self.state_dir = Path(state_dir or cfg["general"]["state_dir"]) / "bracket"
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or (cfg.get("site") or {}).get("dir") or self.state_dir.parent.parent)
        self.tz = ZoneInfo(cfg["general"]["session_tz"])
        self.path = self.state_dir / "account.json"
        self._quotes = quotes or self._default_quotes
        self.busy = False
        st = None
        if self.path.exists():
            try:
                st = json.loads(self.path.read_text())
            except Exception:
                st = None
        if st is not None and st.get("reset_token") != self.b["reset_token"]:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.state_dir / f"account_pre_{self.b['reset_token']}.json", st)
            st = None
        self.s = st or self.fresh()
        if st is None:
            self._write_state()

    # ------------------------------------------------------------------ state
    def fresh(self) -> dict:
        sb = float(self.b["start_balance"])
        return {"version": 1, "book": BOOK, "reset_token": self.b["reset_token"],
                "created_at": datetime.now(UTC).isoformat(), "start_balance": sb, "balance": sb,
                "peak_balance": sb, "max_drawdown": 0.0, "next_id": 1, "positions": [], "pending": [],
                "last_price": {}, "seen_cycles": [],
                "stats": {"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0, "net_pnl": 0.0,
                          "commissions": 0.0, "hold_seconds": 0, "exit_reasons": {}, "signals_seen": 0,
                          "signals_taken": 0, "skips": {}, "missed_scans": {}}}

    def spec(self, c: str) -> dict:
        return self.L["contracts"][c]

    def contract_for(self, sym: str) -> str | None:
        cs = ((self.L.get("symbols") or {}).get(sym) or {}).get("contracts") or []
        cs = [c for c in cs if c in self.L["contracts"] and not self.L["contracts"][c].get("micro")]
        return cs[0] if cs else None

    def held(self, sym: str) -> bool:
        return any(p["symbol"] == sym for p in self.s["positions"] + self.s["pending"])

    def minis_used(self) -> int:
        return sum(int(p["qty"]) for p in self.s["positions"] + self.s["pending"])

    def _skip(self, why: str) -> None:
        sk = self.s["stats"]["skips"]
        sk[why] = sk.get(why, 0) + 1

    # ------------------------------------------------------------------ calendar
    def in_session(self, t: datetime) -> bool:
        """Lucid week (Sun 18:00 ET -> Fri) and the daily 21:45 -> 23:00 UTC flat window."""
        et = t.astimezone(ET)
        d = (et + timedelta(days=1)).date() if et.time() >= _hm(self.L.get("session_open_et", "18:00")) else et.date()
        if d.weekday() >= 5:
            return False
        u = t.astimezone(UTC).time()
        flat, reopen = _hm(self.b["flat_utc"]), _hm(self.b["reopen_utc"])
        return not (flat <= u < reopen)

    def entries_allowed(self, t: datetime) -> bool:
        return self.in_session(t) and self.in_session(t + timedelta(minutes=float(self.b["no_new_entries_last_minutes"])))

    # ------------------------------------------------------------------ sizing / levels
    def size(self, contract: str, atr: float) -> tuple[int | None, str | None]:
        sp = self.spec(contract)
        per = float(self.b["stop_atr"]) * atr * sp["point_value"]
        if per <= 0:
            return None, "bad ATR"
        if per > float(self.b["max_risk_usd"]):
            return None, f"1 {contract} at a {self.b['stop_atr']:g} ATR stop risks ${per:,.0f} > ${self.b['max_risk_usd']:g}"
        room = int(self.b["max_minis"]) - self.minis_used()
        if room < 1:
            return None, f"max {self.b['max_minis']} full-size contracts already open in the bracket book"
        q = max(1, int(math.floor(float(self.b["risk_usd"]) / per + 1e-9)))
        return min(q, room), None

    def levels(self, side: int, entry: float, atr: float, tick: float) -> tuple[float, float]:
        stop = round_away(entry - side * float(self.b["stop_atr"]) * atr, entry, tick)
        target = round_away(entry + side * float(self.b["target_atr"]) * atr, entry, tick)
        return stop, target

    # ------------------------------------------------------------------ intake
    def eligible(self, sig: dict) -> str | None:
        """None when the validated signal qualifies for this book, else the skip reason."""
        if sig.get("action") not in ("BUY", "SELL"):
            return "not BUY/SELL"
        sym = str(sig.get("symbol") or "").upper()
        if sym not in self.b["symbols"]:
            return f"symbol {sym} not in bracket symbols"
        try:
            conf = float(sig.get("confidence"))
        except (TypeError, ValueError):
            return "no confidence"
        if conf < float(self.b["min_confidence"]) - 1e-9:
            return f"confidence {conf:.2f} < {self.b['min_confidence']:g}"
        if not sig.get("atr") or float(sig["atr"]) <= 0:
            return "no ATR"
        return None

    def intake(self, record: dict | None, now: datetime) -> list[dict]:
        """Queue pendings from one entry-cycle record. Returns queued rows. Never touches main's state."""
        out = []
        if not record or record.get("dry_run") or record.get("skipped_reason") in ("blocked_session_throttle",):
            if record and record.get("skipped_reason"):
                self._missed(record, now)
            return out
        key = record.get("run_at_utc")
        if key and key in self.s["seen_cycles"]:
            return out
        if key:
            self.s["seen_cycles"] = (self.s["seen_cycles"] + [key])[-200:]
        self._missed(record, now)
        for sig in record.get("signals") or []:
            if sig.get("action") not in ("BUY", "SELL"):
                continue
            self.s["stats"]["signals_seen"] += 1
            sym = str(sig.get("symbol") or "").upper()
            why = self.eligible(sig)
            if why is None and self.held(sym):
                why = "already in a bracket position on this symbol (one per symbol)"
            if why is None and not self.entries_allowed(now):
                why = "outside session / last minutes before 21:45 UTC flat"
            if why is None:
                c = self.contract_for(sym)
                if c is None:
                    why = "no full-size contract"
                else:
                    q, why = self.size(c, float(sig["atr"]))
            ev = {"symbol": sym, "action": sig.get("action"), "confidence": sig.get("confidence"),
                  "atr": sig.get("atr"), "signal_entry": sig.get("entry"), "cycle": key,
                  "level_event": sig.get("level_event"), "session": sig.get("session")}
            if why is not None:
                self._skip(skip_key(why))
                self._event(now, "skip", reason=why, **ev)
                continue
            pe = {"symbol": sym, "side": 1 if sig["action"] == "BUY" else -1, "contract": c, "qty": q,
                  "atr": float(sig["atr"]), "confidence": float(sig["confidence"]),
                  "signal_entry": sig.get("entry"), "signal_at": key or now.isoformat(),
                  "queued_at": now.isoformat(), "level_event": sig.get("level_event"),
                  "session": sig.get("session"), "rationale": (sig.get("rationale") or "")[:300]}
            self.s["pending"].append(pe)
            self._event(now, "queued", **ev, contract=c, qty=q)
            out.append(pe)
        return out

    def _missed(self, record: dict, now: datetime) -> None:
        """Eligible symbols the main account's scan excluded (main in position) while this book was flat on them."""
        for sym in record.get("in_position") or []:
            if sym in self.b["symbols"] and not self.held(sym):
                m = self.s["stats"]["missed_scans"]
                m[sym] = m.get(sym, 0) + 1

    # ------------------------------------------------------------------ fills
    def _quote_problem(self, q: dict | None) -> str | None:
        from .live_price import fill_quote_problem
        srcs = tuple(self.L.get("fill_quote_sources") or ("dukascopy:tick", "firmtape:NQ"))
        return fill_quote_problem(q, sources=srcs, max_age_s=float(self.b["fill_quote_max_age_s"]))

    def fill_pendings(self, quotes: dict, now: datetime) -> list[dict]:
        opened, keep = [], []
        for pe in self.s["pending"]:
            age = (now - _dt(pe["queued_at"])).total_seconds()
            q = quotes.get(pe["symbol"])
            prob = self._quote_problem(q)
            if prob is not None:
                if age > float(self.b["pending_expiry_seconds"]):
                    self._skip("pending expired (no usable live quote)")
                    self._event(now, "expired", symbol=pe["symbol"], reason=prob)
                else:
                    keep.append(pe)
                continue
            sp = self.spec(pe["contract"])
            tick = float(sp["tick"])
            n = int(self.b["slippage_ticks"])
            entry = round_tick(float(q["price"]) + pe["side"] * n * tick, tick)
            ref = pe.get("signal_entry")
            if ref is not None and -pe["side"] * (float(ref) - entry) > float(self.b["max_chase_atr"]) * pe["atr"]:
                self._skip("chase > max_chase_atr")
                self._event(now, "chase_skip", symbol=pe["symbol"], fill=entry, signal_entry=ref)
                continue
            stop, target = self.levels(pe["side"], entry, pe["atr"], tick)
            et = _dt(q.get("fetched_at")) or now
            comm = round(sp["commission"] * pe["qty"], 2)
            p = {**pe, "id": self.s["next_id"], "book": BOOK, "entry": entry, "entry_time": _iso(et),
                 "quote_price": float(q["price"]), "quote_source": q.get("source"), "quote_age_s": q.get("age_s"),
                 "stop": stop, "target": target,
                 "deadline": _iso(et + timedelta(minutes=float(self.b["max_hold_minutes"]))),
                 "risk_usd": round(abs(entry - stop) * sp["point_value"] * pe["qty"] + 2 * comm, 2),
                 "target_usd": round(abs(target - entry) * sp["point_value"] * pe["qty"] - 2 * comm, 2),
                 "mfe": 0.0, "mae": 0.0, "scan_from": _iso(et), "checks": 0}
            self.s["next_id"] += 1
            self.s["balance"] = round(self.s["balance"] - comm, 2)
            self.s["stats"]["commissions"] = round(self.s["stats"]["commissions"] + comm, 2)
            self.s["stats"]["signals_taken"] += 1
            self.s["positions"].append(p)
            self.s["last_price"][p["symbol"]] = float(q["price"])
            self._event(now, "open", **{k: p[k] for k in ("id", "symbol", "contract", "qty", "entry", "stop",
                                                          "target", "deadline", "entry_time", "confidence",
                                                          "atr", "risk_usd", "target_usd", "quote_source")},
                        side=_side_word(p["side"]))
            opened.append(p)
        self.s["pending"] = keep
        return opened

    # ------------------------------------------------------------------ exits
    def _slip(self, p: dict, price: float) -> float:
        tick = float(self.spec(p["contract"])["tick"])
        return round_tick(float(price) - p["side"] * int(self.b["slippage_ticks"]) * tick, tick)

    def scan_bars(self, p: dict, bars: pd.DataFrame | None) -> tuple[str, float, datetime] | None:
        """Walk M1 bars since the last scan (bars opening before the fill are skipped). Stop wins a bar that
        touches both levels. Returns (reason, fill_price, bar_time) or None."""
        if bars is None or not len(bars):
            return None
        sd = p["side"]
        start = pd.Timestamp(_dt(p["scan_from"])).floor("min")
        entered = pd.Timestamp(_dt(p["entry_time"]))
        last = None
        for ts, row in bars[bars.index >= start].iterrows():
            last = ts
            if ts < entered:
                continue
            o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
            fav, adv = (h, l) if sd > 0 else (l, h)
            p["mfe"] = max(float(p.get("mfe") or 0), sd * (fav - p["entry"]), 0.0)
            p["mae"] = max(float(p.get("mae") or 0), -sd * (adv - p["entry"]), 0.0)
            stop_hit = (l <= p["stop"]) if sd > 0 else (h >= p["stop"])
            tgt_hit = (h >= p["target"]) if sd > 0 else (l <= p["target"])
            if stop_hit:
                gap = (o <= p["stop"]) if sd > 0 else (o >= p["stop"])
                return "stop_1atr", self._slip(p, o if gap else p["stop"]), ts.to_pydatetime()
            if tgt_hit:
                gap = (o >= p["target"]) if sd > 0 else (o <= p["target"])
                return "target_1atr", self._slip(p, o if gap else p["target"]), ts.to_pydatetime()
        if last is not None:
            p["scan_from"] = last.isoformat()
        return None

    def check_price(self, p: dict, price: float, now: datetime) -> tuple[str, float] | None:
        sd = p["side"]
        p["mfe"] = max(float(p.get("mfe") or 0), sd * (price - p["entry"]), 0.0)
        p["mae"] = max(float(p.get("mae") or 0), -sd * (price - p["entry"]), 0.0)
        if (price <= p["stop"]) if sd > 0 else (price >= p["stop"]):
            # resting stop order (placed at the fill): the level + slip when the previous good look at the price
            # (last check, else the fill) is <= gap_seconds old; otherwise a gap -> live price + slip
            prev = _dt(p.get("last_check_at") or p["entry_time"])
            resting = (now - prev).total_seconds() <= float(self.b.get("gap_seconds", 180))
            return "stop_1atr", self._slip(p, p["stop"] if resting else price)
        if (price >= p["target"]) if sd > 0 else (price <= p["target"]):
            return "target_1atr", self._slip(p, p["target"])
        if now >= _dt(p["deadline"]):
            return "time_90min", self._slip(p, price)
        if not self.in_session(now):
            return "flat_2145utc", self._slip(p, price)
        return None

    def close(self, p: dict, price: float, t: datetime, reason: str, extra: dict | None = None) -> dict:
        sp = self.spec(p["contract"])
        comm = round(sp["commission"] * p["qty"], 2)
        upp = sp["point_value"] * p["qty"]
        gross = round(p["side"] * (price - p["entry"]) * upp, 2)
        net = round(gross - 2 * comm, 2)
        s, st = self.s, self.s["stats"]
        s["balance"] = round(s["balance"] + gross - comm, 2)
        st["trades"] += 1
        st["commissions"] = round(st["commissions"] + comm, 2)
        st["net_pnl"] = round(st["net_pnl"] + net, 2)
        if net > 0:
            st["wins"] += 1; st["gross_win"] = round(st["gross_win"] + net, 2)
        else:
            st["losses"] += 1; st["gross_loss"] = round(st["gross_loss"] + net, 2)
        hold = max(0, int((t - _dt(p["entry_time"])).total_seconds()))
        st["hold_seconds"] += hold
        st["exit_reasons"][reason] = st["exit_reasons"].get(reason, 0) + 1
        s["peak_balance"] = max(float(s.get("peak_balance") or s["start_balance"]), s["balance"])
        s["max_drawdown"] = round(min(float(s.get("max_drawdown") or 0), s["balance"] - s["peak_balance"]), 2)
        s["positions"] = [q for q in s["positions"] if q["id"] != p["id"]]
        atr = p.get("atr") or 0
        rec = {"event": "close", "book": BOOK, "t_exit": t.astimezone(UTC).isoformat(),
               "t_exit_dubai": t.astimezone(self.tz).isoformat(), "id": p["id"], "symbol": p["symbol"],
               "contract": p["contract"], "qty": p["qty"], "side": _side_word(p["side"]),
               "entry_time": p["entry_time"], "entry": p["entry"], "stop": p["stop"], "target": p["target"],
               "exit": round(price, 6), "reason": reason, "stopped": reason.startswith("stop"),
               "gross": gross, "commission": round(2 * comm, 2), "net": net, "hold_seconds": hold,
               "balance_after": s["balance"], "risk_usd": p.get("risk_usd"), "target_usd": p.get("target_usd"),
               "confidence": p.get("confidence"), "atr": atr, "signal_at": p.get("signal_at"),
               "R": round(net / p["risk_usd"], 3) if p.get("risk_usd") else None,
               "mfe_atr": round(p.get("mfe", 0) / atr, 3) if atr else None,
               "mae_atr": round(p.get("mae", 0) / atr, 3) if atr else None,
               "checks": p.get("checks", 0), **(extra or {})}
        self._jsonl("bracket_trades.jsonl", t, rec)
        return rec

    def manage(self, quotes: dict, m1: dict, now: datetime) -> list[dict]:
        out = []
        for p in list(self.s["positions"]):
            hit = self.scan_bars(p, (m1 or {}).get(p["symbol"]))
            if hit:
                reason, px, ts = hit
                out.append(self.close(p, px, max(ts, _dt(p["entry_time"])), reason, {"trigger": "m1_path"}))
                continue
            q = quotes.get(p["symbol"])
            if self._quote_problem(q) is not None:
                # no usable live quote: still enforce the clock exits at the last known price
                lp = self.s["last_price"].get(p["symbol"])
                if lp is not None and (now >= _dt(p["deadline"]) or not self.in_session(now)):
                    r = "time_90min" if now >= _dt(p["deadline"]) else "flat_2145utc"
                    out.append(self.close(p, self._slip(p, lp), now, r, {"trigger": "last_price (quote unusable)"}))
                continue
            price = float(q["price"])
            self.s["last_price"][p["symbol"]] = price
            p["checks"] = p.get("checks", 0) + 1
            r = self.check_price(p, price, now)
            p["last_check_at"] = now.isoformat()
            if r:
                out.append(self.close(p, r[1], now, r[0], {"trigger": "live_price", "live_price": price}))
        return out

    # ------------------------------------------------------------------ I/O
    def _jsonl(self, name: str, t: datetime, obj: dict) -> None:
        d = self.logs_dir / t.astimezone(self.tz).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a") as f:
            f.write(json.dumps(obj, default=str, separators=(",", ":")) + "\n")

    def _event(self, t: datetime, kind: str, **kw) -> None:
        self._jsonl("bracket_events.jsonl", t, {"t": t.astimezone(UTC).isoformat(), "event": kind, **kw})

    def _write_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, self.s)

    def recent_trades(self, n: int | None = None) -> list[dict]:
        n = n or int(self.b["recent_trades"])
        rows = []
        for f in sorted(self.logs_dir.glob("*/bracket_trades.jsonl")):
            for line in f.read_text().splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("t_exit", "") >= self.s["created_at"][:19]:
                    rows.append(r)
        rows.sort(key=lambda r: r["t_exit"])
        return rows[-n:][::-1]

    def open_pnl(self) -> float:
        tot = 0.0
        for p in self.s["positions"]:
            lp = self.s["last_price"].get(p["symbol"], p["entry"])
            tot += p["side"] * (lp - p["entry"]) * self.spec(p["contract"])["point_value"] * p["qty"]
        return round(tot, 2)

    def site_view(self) -> dict:
        s, st, b = self.s, self.s["stats"], self.b
        n = st["trades"]
        pos = []
        for p in s["positions"]:
            lp = s["last_price"].get(p["symbol"], p["entry"])
            upp = self.spec(p["contract"])["point_value"] * p["qty"]
            pos.append({k: p.get(k) for k in ("id", "symbol", "contract", "qty", "entry", "entry_time", "stop",
                                              "target", "deadline", "confidence", "risk_usd", "target_usd")}
                       | {"side": _side_word(p["side"]), "last_price": lp,
                          "unrealized": round(p["side"] * (lp - p["entry"]) * upp, 2)})
        op = self.open_pnl()
        return {
            "updated_at": datetime.now(UTC).isoformat(), "paper_only": True, "book": BOOK, "label": LABEL,
            "account": {"start_balance": s["start_balance"], "balance": s["balance"],
                        "equity": round(s["balance"] + op, 2), "net_pnl": st["net_pnl"], "open_pnl": op,
                        "trades": n, "wins": st["wins"], "losses": st["losses"],
                        "win_rate": round(st["wins"] / n, 3) if n else None,
                        "avg_win": round(st["gross_win"] / st["wins"], 2) if st["wins"] else None,
                        "avg_loss": round(st["gross_loss"] / st["losses"], 2) if st["losses"] else None,
                        "max_drawdown": s.get("max_drawdown"), "peak_balance": s.get("peak_balance"),
                        "commissions": st["commissions"], "exit_reasons": st["exit_reasons"],
                        "stopped": st["exit_reasons"].get("stop_1atr", 0), "open_positions": len(s["positions"]),
                        "pending": len(s["pending"]), "signals_seen": st["signals_seen"],
                        "signals_taken": st["signals_taken"], "skips": st["skips"],
                        "missed_scans_main_held": st["missed_scans"]},
            "open_positions": pos, "recent_trades": self.recent_trades(),
            "since": s["created_at"],
            "rules": (f"Same Gemini entry signals as main (no extra Gemini call); conf >= {b['min_confidence']:g}; "
                      f"{', '.join(b['symbols'])} only. Fixed bracket at entry: stop {b['stop_atr']:g} M15 ATR, "
                      f"target {b['target_atr']:g} ATR, {b['max_hold_minutes']} min max hold, flat by "
                      f"{b['flat_utc']} UTC; Gemini CLOSEs ignored. Fill = live quote + {b['slippage_ticks']} tick; "
                      f"exits {b['slippage_ticks']} tick slippage; real commission. Size = floor(${b['risk_usd']:g} / "
                      f"1-ATR risk per contract), max {b['max_minis']} full-size open, one per symbol."),
            "limitation": ("Main's entry scan skips symbols the MAIN account holds, so the bracket book gets no "
                           "signal for them while main is in; symbols held only by the bracket book are still "
                           "scanned. Missed scans are counted per symbol."),
        }

    async def persist(self) -> None:
        view = self.site_view()
        async with self.io_lock:
            self._write_state()
            try:
                _atomic_write(self.site_dir / "bracket.json", view)
            except Exception:
                log.exception("bracket.json write failed")

    async def _default_quotes(self, symbols, now: datetime):
        from .live_price import enrich_live_prices, fill_quote_from_context
        payloads = []
        for s in symbols:
            scfg = (self.cfg.get("symbols") or {}).get(s) or {}
            payloads.append({"symbol": s, "source": "dukascopy" if scfg.get("dukascopy") else None,
                             "context": {"digits": int(scfg.get("digits") or 5)}})
        info = await enrich_live_prices(payloads, self.cfg, now)
        q = {p["symbol"]: fill_quote_from_context(p["symbol"], p["context"]) for p in payloads}
        return q, (info or {}).get("m1") or {}

    # ------------------------------------------------------------------ async entry points (never raise)
    async def on_cycle(self, record: dict | None, now: datetime | None = None) -> dict:
        try:
            now = now or datetime.now(UTC)
            queued = self.intake(record, now)
            res = {"queued": [q["symbol"] for q in queued]}
            if queued:
                res.update(await self.tick(now))
            else:
                await self.persist()
            return res
        except Exception as e:
            log.exception("bracket on_cycle failed (main sim unaffected)")
            return {"error": f"{type(e).__name__}: {e}"}

    async def tick(self, now: datetime | None = None) -> dict:
        if self.busy:
            return {"skipped": "busy"}
        self.busy = True
        try:
            now = now or datetime.now(UTC)
            syms = sorted({p["symbol"] for p in self.s["positions"] + self.s["pending"]})
            if not syms:
                return {"idle": True}
            quotes, m1 = await self._quotes(syms, now)
            quotes = quotes or {}
            opened = self.fill_pendings(quotes, now)
            closed = self.manage(quotes, m1 or {}, now)
            await self.persist()
            return {"opened": [p["id"] for p in opened], "closed": [(c["id"], c["reason"], c["net"]) for c in closed]}
        except Exception as e:
            log.exception("bracket tick failed (main sim unaffected)")
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            self.busy = False


async def run_bracket_loop(book: BracketBook, stop: asyncio.Event | None = None) -> None:
    """18 s cadence while the book has a position or pending; idle ticks are an in-memory check."""
    period = float(book.b.get("check_seconds", 18))
    nxt = time.monotonic()
    while not (stop and stop.is_set()):
        try:
            await book.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bracket loop tick failed")
        nxt += period
        now = time.monotonic()
        if nxt < now:
            nxt += math.ceil((now - nxt) / period) * period
        await asyncio.sleep(nxt - now)
