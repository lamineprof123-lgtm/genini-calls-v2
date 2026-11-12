"""Lucid Trading LucidPro 100K PAPER account simulator. PAPER ONLY: never sends an order anywhere.

Feeds on each pipeline cycle's validated Gemini record (direction + entry), derives a low-R:R prop stop/target
sized in Lucid futures contracts, and replays 1-minute proxy prices (Dukascopy spot/CFD) between cycles to
fill entries and detect stop / target / MLL / DLL / 16:45 ET force-flat events. Account lifecycle:
eval -> pass (+$6,000) -> funded -> payouts (40% consistency, buffer, caps, 90/10) ; MLL breach -> blown ->
pay reset / new-eval fee -> fresh account. All state in state/lucid/, trade log logs/YYYY-MM-DD/lucid_trades.jsonl.
"""
from __future__ import annotations
import asyncio, glob, json, logging, math, shutil, tempfile
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .risk import _atomic_write
from .validator import validate_signal
from .scalp_levels import (compute_scalp_levels, size_for_target, fixed_target_distance, plan_target_and_size,
                           structure_distances)
from .indicators import market_session, session_blocks_automation
from .telemetry import build_entry_snapshot, enrich_close_metrics
from .hard_stop import hard_stop_level, hard_stop_reason, hard_stop_usd

log = logging.getLogger("signals.lucid")
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
STATE_VERSION = 1


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def floor_min(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def ceil_min(t: datetime) -> datetime:
    f = floor_min(t)
    return f if f == t else f + timedelta(minutes=1)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def format_held(seconds: float | int | None) -> str | None:
    """Human hold time for the panel: '45s', '2m 14s', or '1h 05m'."""
    if seconds is None:
        return None
    s = int(round(float(seconds)))
    if s < 0:
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def trade_hold_seconds(row: dict) -> int | None:
    """Prefer the close-time hold_seconds; otherwise entry_time → t_exit."""
    if row.get("hold_seconds") is not None:
        try:
            return int(row["hold_seconds"])
        except (TypeError, ValueError):
            pass
    if row.get("t_exit") and row.get("entry_time"):
        a, b = _dt(row["entry_time"]), _dt(row["t_exit"])
        if a and b:
            return int((b - a).total_seconds())
    return None


class LucidSim:
    def __init__(self, cfg: dict, state_dir: str | Path | None = None, logs_dir: str | Path | None = None):
        self.cfg = cfg
        self.L = cfg["lucid"]
        self.state_dir = Path(state_dir or Path(cfg["general"]["state_dir"]) / "lucid")
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.tz_local = ZoneInfo(cfg["general"]["session_tz"])
        self.flat_t = _hm(self.L.get("flat_by_et", "16:45"))
        self.open_t = _hm(self.L.get("session_open_et", "18:00"))
        self.path = self.state_dir / "account.json"
        self.s = self._load()
        self._close_requests = {}

    # ------------------------------------------------------------------ state
    def _new_account(self, phase: str, now: datetime, no: int) -> dict:
        sb = float(self.L["start_balance"])
        return {"no": no, "phase": phase, "started_at": _iso(now), "start_balance": sb, "balance": sb,
                "highest_close": sb, "mll_floor": sb - self.L["mll"], "mll_locked": False,
                "dll": float(self.L["dll_fixed"]), "lucidscale": False, "peak_eod_profit": 0.0,
                "cycle_start_balance": sb, "cycle_day_pnl": {}, "payouts": [], "trading_days": 0,
                "realized_pnl": 0.0, "trades": 0}

    def _fresh_state(self, now: datetime) -> dict:
        st = {"version": STATE_VERSION, "created_at": _iso(now), "last_processed": _iso(floor_min(now)),
              "account": self._new_account("eval", now, 1), "positions": [], "pending": [], "next_pos_id": 1,
              "day": {"id": None, "start_balance": None, "realized": 0.0, "locked": None, "eod_done": None,
                      "traded": False},
              "last_price": {}, "history": [],
              "ledger": {"fees_total": 0.0, "fees": [], "evals_bought": 0, "resets": 0, "passes": 0,
                         "funded_accounts": 0, "blown_evals": 0, "blown_funded": 0, "payouts_gross": 0.0,
                         "payouts_received": 0.0, "payout_count": 0, "trades": 0, "wins": 0, "losses": 0,
                         "gross_win": 0.0, "gross_loss": 0.0, "commissions": 0.0, "net_trade_pnl": 0.0,
                         "skipped_signals": 0, "accepted_signals": 0}}
        if self.L.get("reset_token"):
            st["reset_token"] = self.L["reset_token"]
        self.s = st
        self._fee("eval", self.L["eval_fee"], now, "first LucidPro 100K evaluation")
        st["ledger"]["evals_bought"] = 1
        return st

    def _load(self) -> dict:
        token = self.L.get("reset_token")
        if self.path.exists():
            st = json.loads(self.path.read_text())
            if not token or st.get("reset_token") == token:
                return st
            return self._reset(st, token, datetime.now(UTC))
        st = self._fresh_state(datetime.now(UTC))
        self.save()
        return st

    def _reset(self, old: dict, token: str, now: datetime) -> dict:
        """One-shot account reset requested by [lucid].reset_token (applied by whichever loop first loads the new
        config, so a running old loop can never overwrite it). Archives the old state; trade/event logs are kept."""
        day = now.astimezone(self.tz_local).strftime("%Y-%m-%d")
        arch = self.state_dir / (self.L.get("reset_archive") or "account_pre_reset_{day}.json").format(day=day)
        n = 2
        while arch.exists():
            arch = self.state_dir / f"account_pre_reset_{day}_{n}.json"; n += 1
        _atomic_write(arch, old)
        self._fresh_state(now)
        self.s["reset_token"] = token
        self.s["reset_at"] = _iso(now)
        self.s["reset_reason"] = self.L.get("reset_reason") or token
        oa = old.get("account") or {}
        self._event(now, "reset", reason=self.L.get("reset_reason") or token, token=token, archived=arch.name,
                    previous={"no": oa.get("no"), "balance": oa.get("balance"), "realized_pnl": oa.get("realized_pnl"),
                              "trades": oa.get("trades"), "positions": len(old.get("positions") or []),
                              "pending": len(old.get("pending") or [])},
                    start_balance=self.s["account"]["balance"])
        self.save()
        return self.s

    def save(self) -> None:
        _atomic_write(self.path, self.s)

    # ------------------------------------------------------------------ logging
    def _jsonl(self, name: str, t: datetime, obj: dict) -> None:
        d = self.logs_dir / t.astimezone(self.tz_local).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a") as f:
            f.write(json.dumps(obj, default=str) + "\n")

    def _event(self, t: datetime, event: str, **kw) -> None:
        ev = {"t": _iso(t), "event": event, "account_no": self.s["account"]["no"],
              "phase": self.s["account"]["phase"], **kw}
        self._jsonl("lucid_events.jsonl", t, ev)
        log.info("lucid %s %s", event, {k: v for k, v in kw.items() if k != "detail"})

    def _fee(self, kind: str, amount: float, t: datetime, note: str) -> None:
        lg = self.s["ledger"]
        lg["fees_total"] = round(lg["fees_total"] + amount, 2)
        lg["fees"].append({"t": _iso(t), "kind": kind, "amount": amount, "note": note})
        self._event(t, "fee", kind=kind, amount=amount, note=note)

    # ------------------------------------------------------------------ calendar
    def day_id(self, t: datetime) -> date:
        """Lucid trading day: 18:00 ET (previous day) -> 16:45 ET. 18:00 ET onward belongs to the next date."""
        et = t.astimezone(ET)
        return (et + timedelta(days=1)).date() if et.time() >= self.open_t else et.date()

    def in_session(self, t: datetime) -> bool:
        et = t.astimezone(ET)
        d = self.day_id(t)
        if d.weekday() >= 5:
            return False
        return not (et.date() == d and et.time() >= self.flat_t)

    def entries_allowed(self, t: datetime) -> bool:
        if not self.in_session(t + timedelta(minutes=self.L.get("no_new_entries_last_minutes", 15))):
            return False
        return self.in_session(t)

    # ------------------------------------------------------------------ helpers
    def spec(self, c: str) -> dict:
        return self.L["contracts"][c]

    def mini_eq(self, c: str, qty: int) -> float:
        return float(qty)                                   # full-size contracts only (user rule)

    def pnl(self, p: dict, price: float) -> float:
        return p["side"] * (price - p["entry"]) * p["qty"] * self.spec(p["contract"])["point_value"]

    def _round_tick(self, x: float, tick: float) -> float:
        return round(round(x / tick) * tick, 10)

    def equity(self) -> float:
        a = self.s["account"]
        return a["balance"] + sum(self.pnl(p, self.s["last_price"].get(p["symbol"], p["entry"]))
                                  for p in self.s["positions"])

    def open_risk(self) -> float:
        return sum(max(0.0, -self.pnl(p, p["stop"])) for p in self.s["positions"] if p.get("stop") is not None)

    @property
    def gemini_exit(self) -> bool:
        """exit_mode = "gemini": Gemini's entry opens, the exit manager's Gemini CLOSE exits. No server target,
        no 2-bar time cut, no VWAP/EMA50 cross exits. Safety rails kept: hard stop, DLL/MLL, 16:45 ET flat,
        max hold. Sizing by stop distance only (no $381 NET-target floor: there is no target)."""
        return str(self.L.get("exit_mode", "server")) in ("gemini", "gemini_only")

    @property
    def gemini_only(self) -> bool:
        """USER RULE (2026-11-11 23:00 Dubai): 100% Gemini. No stop loss, no take profit, no max hold, no 16:45 flat,
        no server-forced exit of any kind. DLL / MLL / 3.5% breaker are LOG-ONLY (breach events, panel, report)."""
        return str(self.L.get("exit_mode", "server")) == "gemini_only"

    @property
    def hard_stop_usd(self) -> float | None:
        """USER RULE (2026-11-12 ~15:55 Dubai): hard stop at -$N NET of RT commission on the main account (and the
        1-strike / 2-strike books). Gemini still makes every other close. None = no stop."""
        return hard_stop_usd(self.cfg)

    def _arm_hard_stops(self, at: datetime) -> None:
        """Positions opened before the stop existed get it now (armed at `at`, never applied to earlier bars)."""
        if not self.hard_stop_usd:
            return
        for p in self.s["positions"]:
            if p.get("stop") is not None:
                continue
            if p.get("hard_stop_px") is None:
                p["hard_stop_px"] = hard_stop_level(self.cfg, p)
            if p.get("hard_stop_armed_at") is None:
                p["hard_stop_armed_at"] = _iso(at)
                p["hard_stop_armed_late"] = True
                self._event(at, "hard_stop_armed", symbol=p["symbol"], id=p["id"], level=p["hard_stop_px"],
                            usd=self.hard_stop_usd, note="open at deploy: armed from now (not retroactive)")

    def _hard_stop_m1(self, p: dict, b, t: datetime) -> bool:
        """M1-path hard stop for the main account (fallback to the exit manager's 18 s live-price check, which
        mirrors its stop onto this account via close_requests.json). Fill at the level, or the bar open if it
        gapped through, minus slippage."""
        lvl = p.get("hard_stop_px")
        if lvl is None or b is None or p.get("stop") is not None or not p.get("hard_stop_armed_at"):
            return False
        if t < ceil_min(_dt(p["hard_stop_armed_at"])):
            return False
        o, h, l, c = b
        sd = p["side"]
        if not ((l <= lvl) if sd > 0 else (h >= lvl)):
            return False
        gap = (o <= lvl) if sd > 0 else (o >= lvl)
        tick = self.spec(p["contract"])["tick"]
        px = self._round_tick(self._slip(p, sd, o if gap else lvl), tick)
        reason = hard_stop_reason(self.cfg)
        self._close(p, px, t, reason)
        self.s.setdefault("close_done", []).append(p["id"])
        self._event(t, "hard_stop", symbol=p["symbol"], id=p["id"], level=lvl, price=px, trigger="m1_path",
                    usd=self.hard_stop_usd)
        return True

    def _log_breach(self, t: datetime, kind: str, level: float, equity: float, note: str = "") -> None:
        day = self.s["day"].get("id")
        seen = self.s.setdefault("breaches", [])
        if any(b["kind"] == kind and b["day"] == day for b in seen):
            return
        b = {"t": _iso(t), "day": day, "kind": kind, "level": round(level, 2), "equity": round(equity, 2),
             "note": note or "log-only (gemini_only): no block, no forced close"}
        seen.append(b)
        del seen[:-100]
        self._event(t, "breach_logged", **{k: v for k, v in b.items() if k != "t"}, breach_t=b["t"])
    def size_by_stop(self, symbol: str, sdist: float) -> dict | None:
        """Full-size contracts only. qty = most contracts whose hard-stop risk fits min($max_risk, DLL room)
        and the remaining max_minis. Skip (None + reason) only when 1 contract does not fit."""
        from .scalp_levels import ceil_tick, full_size_contracts
        L = self.L
        cs = full_size_contracts([(c, self.spec(c)) for c in L["symbols"][symbol]["contracts"]])
        if not cs or not sdist or sdist <= 0:
            self.last_size_why = "no full-size contract / stop distance"; return None
        name, sp = cs[0]
        s = ceil_tick(float(sdist), sp["tick"])
        unit = s * sp["point_value"]
        budget = self.risk_budget()
        used = sum(self.mini_eq(p["contract"], p["qty"]) for p in self.s["positions"])
        room = int(math.floor(L["max_minis"] - used + 1e-9))
        if room < 1:
            self.last_size_why = f"max_minis={L['max_minis']:g} already used by open positions"; return None
        if unit > L["max_risk_usd"] + 1e-9:
            self.last_size_why = (f"risk cap: 1 {name} with a {s:g} stop risks ${unit:,.0f} > max_risk_usd "
                                  f"${L['max_risk_usd']:g} (no micros; skip)")
            return None
        if unit > budget + 1e-9:
            self.last_size_why = (f"DLL room: risk budget ${budget:.0f} < 1 {name} stop risk ${unit:,.0f} "
                                  f"(open risk ${self.open_risk():.0f} + pending ${self.pending_reserved_risk():.0f};"
                                  f" no new entries until it resets)")
            return None
        q = max(1, min(room, int(math.floor(min(L["max_risk_usd"], budget) / unit + 1e-9))))
        comm = 2 * sp["commission"] * q
        return {"contract": name, "qty": q, "sdist": s, "tdist": None, "risk_usd": round(unit * q, 2),
                "target_usd": None, "target_usd_gross": None, "commission_rt": round(comm, 2),
                "target_source": "none (Gemini exit)",
                "sizing": (f"stop {s:g} x {sp['point_value']:g}/pt = ${unit:,.0f} per {name}; {q} {name} = "
                           f"${unit * q:,.0f} risk <= min(${L['max_risk_usd']:g} cap, ${budget:,.0f} DLL room); "
                           f"no target / no $381 floor (exit = Gemini CLOSE)")}

    def size_notional(self, symbol: str, atr: float) -> dict | None:
        """gemini_only sizing: full-size, 1..max_minis, qty = floor($max_risk / (0.8 ATR x point value)), at least 1.
        0.8 ATR is a NOTIONAL reference only - there is no stop and it never triggers an exit. Never blocked by
        DLL/MLL (log-only); only the LucidPro 100K aggregate cap (max_minis, 6) applies."""
        from .scalp_levels import ceil_tick, full_size_contracts
        L = self.L
        cs = full_size_contracts([(c, self.spec(c)) for c in L["symbols"][symbol]["contracts"]])
        if not cs or not atr or atr <= 0:
            self.last_size_why = "no full-size contract / ATR"; return None
        name, sp = cs[0]
        mult = float((self.cfg.get("scalp") or {}).get("atr_stop_mult", 0.8))
        ref = ceil_tick(mult * atr, sp["tick"])
        unit = ref * sp["point_value"]
        used = sum(self.mini_eq(p["contract"], p["qty"]) for p in self.s["positions"])
        room = int(math.floor(L["max_minis"] - used + 1e-9))
        if room < 1:
            self.last_size_why = f"max_minis={L['max_minis']:g} already used by open positions"; return None
        q = max(1, min(room, int(math.floor(L["max_risk_usd"] / unit + 1e-9))))
        return {"contract": name, "qty": q, "sdist": None, "tdist": None, "risk_usd": None,
                "notional_risk_usd": round(unit * q, 2), "notional_ref_dist": ref,
                "target_usd": None, "target_usd_gross": None, "commission_rt": round(2 * sp["commission"] * q, 2),
                "target_source": "none (gemini_only)",
                "sizing": (f"gemini_only: notional {mult:g} ATR = {ref:g} x {sp['point_value']:g}/pt = ${unit:,.0f} per "
                           f"{name}; {q} {name} ~ ${unit * q:,.0f} notional <= ${L['max_risk_usd']:g} "
                           f"(reference only; NO stop, NO target, exit = Gemini CLOSE)")}

    def pending_reserved_risk(self) -> float:
        """Risk reserved by queued (not yet filled) entries; counts against DLL room for any other entry."""
        return sum(float(pe.get("reserved_risk") or 0.0) for pe in self.s["pending"])

    def risk_budget(self) -> float:
        """$ a NEW entry may risk: max_risk_usd capped by DLL room after open risk + pending reservations."""
        L = self.L
        # Worst case = every open position stopped out: balance + sum(P&L at each stop). Open (unrealized) profit
        # is NOT room - it can vanish before the stop (equity - open_risk used to count it, letting a 2nd entry
        # stack past the DLL while the first was in profit).
        at_stops = self.s["account"]["balance"] + sum(self.pnl(p, p["stop"]) for p in self.s["positions"]
                                                      if p.get("stop") is not None)
        room = at_stops - self.dll_level() - self.pending_reserved_risk()
        budget = min(L["max_risk_usd"], room - L.get("dll_safety_usd", 0))
        if L.get("respect_mll_room"):
            budget = min(budget, at_stops - self.s["account"]["mll_floor"] - self.pending_reserved_risk() - 50)
        return budget

    def dll_level(self) -> float:
        d = self.s["day"]
        sb = d["start_balance"] if d["start_balance"] is not None else self.s["account"]["balance"]
        return sb - self.s["account"]["dll"]

    # ------------------------------------------------------------------ day roll / EOD
    def _roll_day(self, t: datetime) -> None:
        did = self.day_id(t).isoformat()
        d = self.s["day"]
        if d["id"] == did:
            return
        self.s["day"] = {"id": did, "start_balance": self.s["account"]["balance"], "realized": 0.0,
                         "locked": None, "eod_done": None, "traded": False}

    def _eod(self, t: datetime) -> None:
        """16:45 ET close: trail update, LucidScale DLL, payouts, pass check."""
        a, d = self.s["account"], self.s["day"]
        d["eod_done"] = d["id"]
        bal = a["balance"]
        if d["traded"]:
            a["trading_days"] += 1
        # day P&L for the funded consistency rule
        if a["phase"] == "funded":
            day_pnl = round(bal - d["start_balance"], 2)
            if d["traded"]:
                a["cycle_day_pnl"][d["id"]] = round(a["cycle_day_pnl"].get(d["id"], 0.0) + day_pnl, 2)
        # EOD trailing MLL (non-intraday mode: closing balance at/under floor blows the account)
        if bal <= a["mll_floor"] + 1e-9:
            self._blow(t, f"EOD balance {bal:.2f} <= MLL floor {a['mll_floor']:.2f}")
            return
        if not a["mll_locked"]:
            a["highest_close"] = max(a["highest_close"], bal)
            if a["highest_close"] > self.L["initial_trail_balance"]:
                a["mll_locked"] = True
                a["mll_floor"] = float(self.L["locked_mll_balance"])
            else:
                a["mll_floor"] = min(a["highest_close"] - self.L["mll"], self.L["locked_mll_balance"])
        if a["phase"] == "funded":
            if bal > self.L["initial_trail_balance"]:
                a["lucidscale"] = True
            a["peak_eod_profit"] = max(a["peak_eod_profit"], bal - a["start_balance"])
            if a["lucidscale"]:
                a["dll"] = max(a["dll"], round(self.L["lucidscale_pct"] * a["peak_eod_profit"], 2))
            self._try_payout(t)
        self._event(t, "eod", balance=round(bal, 2), mll_floor=a["mll_floor"], mll_locked=a["mll_locked"],
                    dll=a["dll"], day=d["id"])

    def payout_status(self) -> dict:
        a = self.s["account"]
        if a["phase"] != "funded":
            return {"eligible": False, "why": "not funded"}
        cp = round(a["balance"] - a["cycle_start_balance"], 2)
        days = [v for v in a["cycle_day_pnl"].values()]
        largest = max([0.0] + days)
        cap = self.L["max_payout_first"] if not a["payouts"] else self.L["max_payout_later"]
        amt = round(min(cap, a["balance"] - self.L["payout_buffer"]), 2)
        why = []
        if cp < self.L["min_cycle_profit"]:
            why.append(f"cycle profit {cp:.0f} < {self.L['min_cycle_profit']:.0f}")
        if cp > 0 and largest > self.L["funded_consistency"] * cp + 1e-9:
            why.append(f"consistency: best day {largest:.0f} > {self.L['funded_consistency']:.0%} of {cp:.0f}")
        if amt < self.L["min_payout"]:
            why.append(f"withdrawable {max(amt, 0):.0f} < min {self.L['min_payout']:.0f} (buffer {self.L['payout_buffer']:.0f})")
        return {"eligible": not why, "amount": max(amt, 0.0), "cycle_profit": cp, "best_day": largest,
                "why": "; ".join(why) or "ok"}

    def _try_payout(self, t: datetime) -> None:
        a, lg = self.s["account"], self.s["ledger"]
        ps = self.payout_status()
        if not ps["eligible"]:
            return
        amt = ps["amount"]
        recv = round(amt * self.L["trader_split"], 2)
        a["balance"] = round(a["balance"] - amt, 2)
        a["payouts"].append({"t": _iso(t), "gross": amt, "received": recv, "cycle_profit": ps["cycle_profit"]})
        a["cycle_start_balance"] = a["balance"]
        a["cycle_day_pnl"] = {}
        lg["payouts_gross"] = round(lg["payouts_gross"] + amt, 2)
        lg["payouts_received"] = round(lg["payouts_received"] + recv, 2)
        lg["payout_count"] += 1
        self._event(t, "payout", gross=amt, received=recv, n=len(a["payouts"]), balance_after=a["balance"])

    # ------------------------------------------------------------------ account lifecycle
    def _archive_and_start(self, t: datetime, result: str, phase: str) -> None:
        a = self.s["account"]
        self.s["history"].append({**{k: a[k] for k in ("no", "phase", "started_at", "balance", "trading_days",
                                                        "realized_pnl", "trades")},
                                  "payouts": len(a["payouts"]),
                                  "payouts_received": round(sum(p["received"] for p in a["payouts"]), 2),
                                  "ended_at": _iso(t), "result": result})
        self.s["account"] = self._new_account(phase, t, a["no"] + 1)
        # no pause: the fresh account keeps trading immediately (user instruction); its DLL day starts now
        self.s["day"]["start_balance"] = self.s["account"]["balance"]
        self.s["day"]["realized"] = 0.0
        self.s["day"]["locked"] = None

    def _blow(self, t: datetime, why: str) -> None:
        a, lg = self.s["account"], self.s["ledger"]
        if self.gemini_only:
            self._log_breach(t, "MLL", a["mll_floor"], self.equity(), f"would blow: {why} (log-only)")
            return
        self._event(t, "blown", reason=why, balance=round(a["balance"], 2))
        if a["phase"] == "eval":
            lg["blown_evals"] += 1; lg["resets"] += 1
            self._archive_and_start(t, "blown", "eval")
            self._fee("reset", self.L["reset_fee"], t, "eval reset after MLL breach")
            self.s["history"][-1]["fee_charged"] = {"kind": "reset", "amount": self.L["reset_fee"]}
        else:
            lg["blown_funded"] += 1; lg["evals_bought"] += 1
            self._archive_and_start(t, "blown", "eval")
            self._fee("eval", self.L["eval_fee"], t, "new evaluation after funded account breach")
            self.s["history"][-1]["fee_charged"] = {"kind": "new eval", "amount": self.L["eval_fee"]}

    def _check_pass(self, t: datetime) -> None:
        a = self.s["account"]
        if a["phase"] == "eval" and not self.s["positions"] and \
                a["balance"] >= a["start_balance"] + self.L["eval_profit_target"] - 1e-9:
            self.s["ledger"]["passes"] += 1
            self.s["ledger"]["funded_accounts"] += 1
            self._event(t, "passed", balance=round(a["balance"], 2))
            self._archive_and_start(t, "passed", "funded")

    # ------------------------------------------------------------------ positions
    def _close(self, p: dict, price: float, t: datetime, reason: str) -> None:
        sp = self.spec(p["contract"])
        gross = round(self.pnl(p, price), 2)
        comm = round(sp["commission"] * p["qty"], 2)  # exit side (entry side was charged at fill)
        a, lg, d = self.s["account"], self.s["ledger"], self.s["day"]
        a["balance"] = round(a["balance"] + gross - comm, 2)
        net = round(gross - 2 * comm, 2)
        a["realized_pnl"] = round(a["realized_pnl"] + net, 2); a["trades"] += 1
        d["realized"] = round(d["realized"] + net, 2)
        lg["trades"] += 1; lg["commissions"] = round(lg["commissions"] + comm, 2)
        lg["net_trade_pnl"] = round(lg["net_trade_pnl"] + net, 2)
        if net > 0:
            lg["wins"] += 1; lg["gross_win"] = round(lg["gross_win"] + net, 2)
        else:
            lg["losses"] += 1; lg["gross_loss"] = round(lg["gross_loss"] + net, 2)
        self.s["positions"] = [q for q in self.s["positions"] if q["id"] != p["id"]]
        hold = (t - _dt(p["entry_time"])).total_seconds()
        pv_total = self.spec(p["contract"])["point_value"] * p["qty"]
        telem = enrich_close_metrics(
            p.get("telemetry"),
            mfe_pts=float(p.get("mfe") or 0.0),
            mae_pts=float(p.get("mae") or 0.0),
            atr=p.get("atr"),
            point_value_total=pv_total,
            hold_seconds=hold,
        )
        rec = {"t_exit": _iso(t), "account_no": a["no"], "phase": a["phase"], "id": p["id"], "symbol": p["symbol"],
               "contract": p["contract"], "qty": p["qty"], "side": "LONG" if p["side"] > 0 else "SHORT",
               "entry_time": p["entry_time"], "entry": p["entry"], "stop": p["stop"], "target": p["target"],
               "exit": round(price, 6), "reason": reason, "gross": gross, "commission": round(2 * comm, 2),
               "net": net, "hold_seconds": int(hold), "balance_after": a["balance"],
               "hard_stop_px": p.get("hard_stop_px"), "stopped": str(reason).startswith("hard_stop"),
               "risk_usd": p["risk_usd"], "target_usd": p["target_usd"],  # planned NET of RT commission
               "target_usd_is_net": True,
               "max_minis": self.L["max_minis"],
               "max_minis_note": "LucidPro 100K cap: 6 full-size minis (NQ, GC, 6E, 6B); no micros",
               "gemini": p.get("gemini"),
               "telemetry": telem,
               "mfe_usd": telem.get("mfe_usd"), "mae_usd": telem.get("mae_usd"),
               "mfe_atr": telem.get("mfe_atr"), "mae_atr": telem.get("mae_atr"),
               "bars_to_exit_m1": telem.get("bars_to_exit_m1"),
               "price_source": "proxy spot/CFD M1 (not CME futures)"}
        self._jsonl("lucid_trades.jsonl", t, rec)
        log.info("lucid close %s %s x%d %s @%s net=%.2f bal=%.2f", p["symbol"], p["contract"], p["qty"],
                 reason, price, net, a["balance"])

    def _slip(self, p_or_contract, side: int, price: float, adverse: bool = True) -> float:
        c = p_or_contract if isinstance(p_or_contract, str) else p_or_contract["contract"]
        tick = self.spec(c)["tick"]
        n = self.L.get("slippage_ticks", 0)
        return price - side * n * tick if adverse else price  # adverse for an exit of `side` position

    def size_trade(self, symbol: str, side: int, atr: float,
                   tdist: float | None = None, sdist: float | None = None, *,
                   entry: float | None = None, scalp: dict | None = None) -> dict | None:
        """Pick (contract, qty, target distance) so NET of RT commission is in [target_usd_min, target_usd_max].

        Stop distance = server scalp stop (structure / 0.8 ATR). Target: the scalp target if it clears the NET
        floor at an allowed size; else the next structure level beyond it that does; else extended to ~$aim NET
        with the fewest contracts that fit the risk cap (see scalp_levels.plan_target_and_size). Size respects
        max_risk_usd, the remaining DLL budget and max_minis (LucidPro 100K cap). Returns None (SKIP) when no
        size fits; the reason is in self.last_size_why. Result carries target_source + sizing rationale.
        """
        L = self.L
        self.last_size_why = None
        if self.gemini_only:
            return self.size_notional(symbol, float(atr or 0))
        if self.gemini_exit:
            return self.size_by_stop(symbol, sdist if sdist is not None else
                                     float((self.cfg.get("scalp") or {}).get("atr_stop_mult", 0.8)) * float(atr or 0))
        if not atr or atr <= 0:
            self.last_size_why = "no ATR"
            return None
        if tdist is None or sdist is None:
            tdist = tdist or fixed_target_distance(symbol, self.cfg) or (L.get("target_atr_mult", 0.6) * atr)
            sdist = sdist or (float((self.cfg.get("scalp") or {}).get("atr_stop_mult", 0.8)) * atr)
        if tdist <= 0 or sdist <= 0:
            self.last_size_why = "non-positive stop/target distance"
            return None
        budget = self.risk_budget()
        if budget < L.get("min_risk_usd", 0):
            self.last_size_why = (f"DLL room: risk budget ${budget:.0f} < min_risk_usd ${L.get('min_risk_usd', 0):g} "
                                  f"(open risk ${self.open_risk():.0f} + pending ${self.pending_reserved_risk():.0f}; "
                                  f"daily loss limit nearly used; no new entries until it resets)")
            return None
        used = sum(self.mini_eq(p["contract"], p["qty"]) for p in self.s["positions"])
        room_minis = L["max_minis"] - used
        if room_minis <= 0:
            self.last_size_why = f"max_minis={L['max_minis']:g} already used by open positions"
            return None
        contracts = [(c, self.spec(c)) for c in L["symbols"][symbol]["contracts"]]
        sc = self.cfg.get("scalp") or {}
        struct = []
        if scalp and entry is not None and (scalp.get("levels") or scalp.get("vp")):
            before = float(sc.get("tp_before_level_ticks", 1)) * contracts[0][1]["tick"]
            struct = structure_distances(side, entry, scalp.get("levels"), scalp.get("vp"), before)
        plan, why = plan_target_and_size(
            tdist0=float(tdist), sdist=float(sdist), atr=float(atr), contracts=contracts, struct_dists=struct,
            net_min=float(L["target_usd_min"]), net_aim=float(L["target_usd_aim"]), net_max=float(L["target_usd_max"]),
            max_risk_usd=float(L["max_risk_usd"]), budget=float(budget), room_minis=float(room_minis),
            min_target_atr=float(L.get("min_target_atr", 0)),
            max_target_atr=float(L.get("max_target_atr", 1e9)),
            extend_max_target_atr=float(L.get("extend_max_target_atr", 1.0)),
            target_source0=str((scalp or {}).get("target_source") or "scalp"))
        if not plan:
            self.last_size_why = why
        return plan

    # ------------------------------------------------------------------ signal intake
    def _build_snapshot(self, v: dict, ctx: dict, record: dict, side, scalp=None, size=None) -> dict:
        """Confluence telemetry from the validated signal + chart context (same numbers as the header)."""
        run_dir = record.get("run_dir")
        chart = f"{run_dir}/{v['symbol']}.png" if run_dir else None
        return build_entry_snapshot(
            symbol=v["symbol"], side=side, signal=v, ctx=ctx, scalp=scalp, size=size,
            record=record, chart_path=chart, run_at_utc=record.get("run_at_utc"),
        )

    def _skip_entry(self, t, sym: str, why: str, snapshot: dict | None = None, **extra) -> None:
        """Log an entry_skipped event with full telemetry (session_blocked / sizing / chase / ...)."""
        self.s["ledger"]["skipped_signals"] = self.s["ledger"].get("skipped_signals", 0) + 1
        payload = {"symbol": sym, "why": why}
        if snapshot is not None:
            payload["telemetry"] = snapshot
        payload.update(extra)
        self._event(t, "entry_skipped", **payload)

    def sim_candidate(self, v: dict) -> tuple[dict | None, str]:
        """Gemini direction for the sim. Site-approved signals pass straight through; HOLDs that were only
        downgraded for the R:R minimum are re-validated with min_rr=0 (direction/side/entry/confidence checks kept)."""
        if v["action"] in ("BUY", "SELL"):
            return v, "site signal"
        if v.get("model_action") not in ("BUY", "SELL"):
            return None, "HOLD"
        sig = {"action": v["model_action"], "entry": v["entry"], "stop_loss": v["stop_loss"],
               "take_profit": v["take_profit"], "confidence": v["confidence"], "risk_pct": v.get("model_risk_pct")}
        ctx = {"last_close": v["last_close"], "atr": v["atr"]}
        r = validate_signal(sig, v["symbol"], ctx, self.cfg, 100000.0, min_rr=0.0,
                            min_confidence=self.L["min_confidence"])
        if r["action"] in ("BUY", "SELL"):
            return r, "raw direction (R:R minimum skipped for sim)"
        return None, "; ".join(r["reasons"]) or "rejected"

    def on_record(self, record: dict, now: datetime | None = None) -> list[dict]:
        """Queue pending entries from one cycle's validated record. Returns per-symbol decisions.

        Every accepted pending carries a full ``telemetry`` snapshot (Volume Profile, CVD, session,
        VWAP/EMA50, RVOL, ATR band, S/R, Gemini, scalp plan). Every skip that is not a plain HOLD
        writes an ``entry_skipped`` event with the same snapshot.
        """
        now = now or datetime.now(UTC)
        self._roll_day(now)
        out = []
        run_at = _dt(record.get("run_at_utc")) or now
        if (now - run_at) > timedelta(minutes=self.L.get("fresh_signal_max_age_minutes", 10)):
            return [{"why": "stale record"}]
        data_by = record.get("data") or {}
        for v in record.get("signals", []):
            sym = v["symbol"]
            # Merge chart context (candle / VP / S/R) onto the signal for scalp levels
            ctx = data_by.get(sym) or {}
            for k in ("signal_candle", "last_candle", "levels", "poc", "vah", "val", "digits",
                      "vwap", "ema50", "ema20_baseline", "atr_upper", "atr_lower",
                      "rvol_last", "rvol_bars_since_spike", "price_vs_value_area", "close_vs_vwap",
                      "cvd", "cvd_label", "cvd_slope", "session"):
                if v.get(k) is None and ctx.get(k) is not None:
                    v[k] = ctx[k]
            if sym not in self.L["symbols"]:
                if v.get("model_action") in ("BUY", "SELL"):
                    out.append({"symbol": sym, "taken": False, "why": "not a Lucid product (excluded)"})
                continue
            cand, why = self.sim_candidate(v)
            if not cand:
                # HOLD with no tradeable direction: ignore. Directional rejects (confidence etc.): snapshot + skip.
                if v.get("model_action") in ("BUY", "SELL"):
                    side = 1 if v["model_action"] == "BUY" else -1
                    snap = self._build_snapshot(v, ctx, record, side, scalp=v.get("scalp"))
                    self._skip_entry(now, sym, why, snapshot=snap)
                    out.append({"symbol": sym, "taken": False, "why": why, "telemetry": True})
                continue
            side = 1 if cand["action"] == "BUY" else -1
            # Prefer signal-bar session (replay-correct) over wall clock
            bar_ts = (v.get("last_closed_bar_utc") or v.get("last_price_ts_utc")
                      or ctx.get("last_closed_bar_utc") or ctx.get("last_price_ts_utc"))
            sess = v.get("session") or ctx.get("session") or self._market_session_for(now, bar_ts)
            scalp = cand.get("scalp") or v.get("scalp")
            if not scalp:
                candle = (v.get("signal_candle") or v.get("last_candle")
                          or {"high": cand["entry"], "low": cand["entry"]})
                vp = {k: v[k] for k in ("poc", "vah", "val") if v.get(k) is not None}
                scalp = compute_scalp_levels(
                    cand["action"], cand["entry"], candle, cand["atr"], sym, self.cfg,
                    levels=v.get("levels"), vp=vp or None,
                    tick=(self.spec(self.L["symbols"][sym]["contracts"][0])["tick"]
                          if sym in self.L["symbols"] else None),
                    digits=self.cfg.get("symbols", {}).get(sym, {}).get("digits", 5),
                )
            snap = self._build_snapshot(v, ctx, record, side, scalp=scalp)
            reason = self._block_reason(sym, side, now, session=sess, bar_ts=bar_ts)
            if reason:
                self._skip_entry(now, sym, reason, snapshot=snap, session=sess)
                out.append({"symbol": sym, "taken": False, "why": reason, "session": sess, "telemetry": True})
                continue
            # DLL room at QUEUE time too (open risk + other pending reservations); the same check runs again at fill.
            plan = self.size_trade(sym, side, cand["atr"], tdist=(scalp or {}).get("tdist"),
                                   sdist=(scalp or {}).get("sdist"), entry=cand["entry"], scalp=scalp)
            if plan is None and str(self.last_size_why or "").startswith("DLL room"):
                why_dll = f"DLL room at queue: {self.last_size_why}"
                self._skip_entry(now, sym, why_dll, snapshot=snap, session=sess)
                out.append({"symbol": sym, "taken": False, "why": why_dll, "session": sess, "telemetry": True})
                continue
            pend = {"symbol": sym, "side": side, "created": _iso(now), "fill_after": _iso(ceil_min(now)),
                    "reserved_risk": (float(plan.get("risk_usd") or 0.0) if plan else
                                      (0.0 if self.gemini_only else float(self.L.get("min_risk_usd", 0)))),
                    "planned": ({k: plan.get(k) for k in ("contract", "qty", "risk_usd", "tdist", "target_source")}
                                if plan else None),
                    "expires": _iso(ceil_min(now) + timedelta(minutes=self.L["pending_expiry_minutes"])),
                    "atr": cand["atr"], "scalp": scalp, "session": sess, "telemetry": snap,
                    "gemini": {"action": cand["action"], "entry": cand["entry"],
                                                    "stop_loss": cand["stop_loss"], "take_profit": cand["take_profit"],
                                                    "confidence": cand["confidence"], "rr": cand.get("rr_local"),
                                                    "shown_on_site": v["action"] in ("BUY", "SELL"), "basis": why,
                                                    "run_at_utc": record.get("run_at_utc"), "session": sess,
                                                    "rationale": v.get("rationale") or v.get("reason"),
                                                    "model": record.get("model")}}
            self.s["pending"].append(pend)
            self.s["ledger"]["accepted_signals"] += 1
            out.append({"symbol": sym, "taken": True, "why": why, "session": sess})
        for o in out:
            # Keep the compact "signal" decision log; skips also have a dedicated entry_skipped event.
            self._event(now, "signal", **{k: v for k, v in o.items() if k != "telemetry"})
        return out

    def _market_session_for(self, t: datetime, bar_ts=None) -> str:
        """Liquidity window from SIGNAL BAR time when available (replay-correct), else wall time."""
        sc = (self.cfg.get("chart") or {}).get("session") or {}
        windows = {
            "LONDON_SESSION": tuple(sc.get("london", ["03:00", "08:00"])),
            "LONDON_NY_OVERLAP": tuple(sc.get("overlap", ["08:00", "11:00"])),
            "NEW_YORK_SESSION": tuple(sc.get("new_york", ["11:00", "16:00"])),
        }
        asian = tuple(sc.get("asian", ["18:00", "02:00"]))
        return market_session(bar_ts if bar_ts is not None else t, windows=windows, asian=asian)

    def _block_reason(self, sym: str, side: int, t: datetime, *, session: str | None = None,
                      bar_ts=None) -> str | None:
        if self.s["day"].get("locked"):
            return f"day locked: {self.s['day']['locked']}"
        if not self.entries_allowed(t):
            return "outside Lucid session / last minutes before 16:45 ET flat"
        sess = session or self._market_session_for(t, bar_ts)
        sc = (self.cfg.get("chart") or {}).get("session") or {}
        blocked = sc.get("blocked_for_automation") or [
            "ASIAN_SESSION_CHOP", "LOW_LIQUIDITY_DEADZONE"]
        if session_blocks_automation(sess, blocked):
            return "session_blocked"
        if any(p["symbol"] == sym for p in self.s["positions"]) or any(p["symbol"] == sym for p in self.s["pending"]):
            return "already in a position / pending on this symbol (one per symbol)"
        my = set(self.L["symbols"][sym]["contracts"])
        for grp in self.L.get("correlated_groups", []):
            if my & set(grp):
                for p in self.s["positions"] + self.s["pending"]:
                    if set(self.L["symbols"][p["symbol"]]["contracts"]) & set(grp) and p["side"] != side:
                        return "opposite position in a correlated contract (anti-hedge)"
        return None

    # ------------------------------------------------------------------ minute replay
    def process(self, bars: dict[str, pd.DataFrame], until: datetime,
                m15: dict[str, pd.DataFrame] | None = None) -> None:
        """Replay closed 1-minute bars (index = bar open UTC) for each signal symbol from last_processed up to
        `until` (exclusive). Symbols without a bar in a minute are simply not checked that minute.

        Optional ``m15`` maps symbol -> DataFrame of 15-minute bars with columns close/vwap/ema50
        (index = bar open UTC). Used for VWAP/EMA50 cross exits at 15m closes.
        """
        t = floor_min(_dt(self.s["last_processed"]))
        until = floor_min(until)
        idx = {s: {int(ts.timestamp()): row for ts, row in zip(df.index, df[["open", "high", "low", "close"]].itertuples(index=False))}
               for s, df in bars.items() if df is not None and len(df)}
        m15_idx = {}
        for s, df in (m15 or {}).items():
            if df is None or not len(df):
                continue
            cols = [c for c in ("close", "vwap", "ema50") if c in df.columns]
            m15_idx[s] = {int(ts.timestamp()): {c: float(row[c]) for c in cols if pd.notna(row[c])}
                          for ts, row in df[cols].iterrows()}
        self._m15_idx = m15_idx
        self._close_requests = self._load_close_requests()
        self._arm_hard_stops(max(until, datetime.now(UTC)))
        while t < until:
            self._minute(t, {s: m.get(int(t.timestamp())) for s, m in idx.items()})
            t += timedelta(minutes=1)
        self.s["last_processed"] = _iso(t)
        self._roll_day(t - timedelta(minutes=1))

    def _load_close_requests(self) -> dict:
        """Gemini CLOSE requests written by the exit manager (state/lucid/close_requests.json), keyed by position id."""
        f = self.state_dir / "close_requests.json"
        done = set(self.s.get("close_done") or [])
        try:
            rows = json.loads(f.read_text()) if f.exists() else []
        except Exception:
            rows = []
        open_ids = {p["id"] for p in self.s["positions"]}
        return {r["id"]: r for r in rows if r.get("id") in open_ids and r["id"] not in done
                and r.get("account_token") in (None, self.s.get("reset_token"))}

    def _minute_gemini_only(self, t: datetime, bar: dict) -> None:
        """Entry = Gemini signal (pending fills in session); exit = Gemini CLOSE requests only (any time)."""
        d = self.s["day"]
        sess = self.in_session(t)
        et = t.astimezone(ET)
        if not sess:
            self.s["pending"] = []
            if (d["eod_done"] != d["id"] and date.fromisoformat(d["id"]).weekday() < 5
                    and et.date().isoformat() == d["id"] and et.time() >= self.flat_t):
                self._eod(t)
        else:
            for pe in list(self.s["pending"]):
                if t > _dt(pe["expires"]):
                    self.s["pending"].remove(pe); self._event(t, "pending_expired", symbol=pe["symbol"]); continue
                if t < _dt(pe["fill_after"]) or bar.get(pe["symbol"]) is None:
                    continue
                self._try_fill(pe, bar[pe["symbol"]], t)
        for p in list(self.s["positions"]):
            b = bar.get(p["symbol"])
            if b is not None:
                o, h, l, c = b
                fav, adv = (h, l) if p["side"] > 0 else (l, h)
                p["mfe"] = max(float(p.get("mfe") or 0.0), max(0.0, p["side"] * (fav - p["entry"])))
                p["mae"] = max(float(p.get("mae") or 0.0), max(0.0, -p["side"] * (adv - p["entry"])))
            req = self._close_requests.get(p["id"])
            if req and floor_min(_dt(req["t"])) <= t:
                why = req.get("reason") or "gemini_close"
                self._close(p, float(req["price"]), t, why)
                self.s.setdefault("close_done", []).append(p["id"])
                self._event(t, "hard_stop" if why.startswith("hard_stop") else "gemini_close", symbol=p["symbol"],
                            id=p["id"], price=req["price"], request_t=req["t"], book=req.get("book"),
                            rule=req.get("rule"), trigger=req.get("trigger"), reason_text=req.get("reason_text"))
                continue
            self._hard_stop_m1(p, b, t)
        for s, b in bar.items():
            if b is not None:
                self.s["last_price"][s] = float(b[3])
        self._check_pass(t)
        eq, a = self.equity(), self.s["account"]
        if eq <= self.dll_level() + 1e-9:
            self._log_breach(t, "DLL", self.dll_level(), eq)
        if eq <= a["mll_floor"] + 1e-9:
            self._log_breach(t, "MLL", a["mll_floor"], eq)

    def _minute(self, t: datetime, bar: dict) -> None:
        self._roll_day(t)
        if self.gemini_only:
            return self._minute_gemini_only(t, bar)
        d, a = self.s["day"], self.s["account"]
        sess = self.in_session(t)
        et = t.astimezone(ET)
        # 1. forced flat at 16:45 ET (at this bar's open, adverse slippage)
        if not sess and self.s["positions"]:
            for p in list(self.s["positions"]):
                b = bar.get(p["symbol"])
                px = b[0] if b is not None else self.s["last_price"].get(p["symbol"], p["entry"])
                self._close(p, self._slip(p, p["side"], px), t, "flat_by_16:45_ET")
            self._check_pass(t)
        if not sess:
            self.s["pending"] = []
        if (not sess and d["eod_done"] != d["id"] and date.fromisoformat(d["id"]).weekday() < 5
                and et.date().isoformat() == d["id"] and et.time() >= self.flat_t):
            self._eod(t)
        if not sess:
            return
        # 2. pending entries
        for pe in list(self.s["pending"]):
            if t > _dt(pe["expires"]):
                self.s["pending"].remove(pe); self._event(t, "pending_expired", symbol=pe["symbol"]); continue
            if t < _dt(pe["fill_after"]) or bar.get(pe["symbol"]) is None:
                continue
            self._try_fill(pe, bar[pe["symbol"]], t)
        # 3. exits: stop has priority when both levels are inside one bar; target never on the entry bar.
        #    Also: MFE tracking, time/momentum cut, 15m VWAP/EMA50 cross exits (scalp framework).
        sc = self.cfg.get("scalp") or {}
        for p in list(self.s["positions"]):
            b = bar.get(p["symbol"])
            if b is None:
                continue
            o, h, l, c = b
            sd = p["side"]
            # Favourable / adverse extremes this minute (MFE / MAE in price points)
            fav = h if sd > 0 else l
            adv = l if sd > 0 else h
            mfe = max(float(p.get("mfe") or 0.0), max(0.0, sd * (fav - p["entry"])))
            mae = max(float(p.get("mae") or 0.0), max(0.0, -sd * (adv - p["entry"])))
            p["mfe"] = mfe
            p["mae"] = mae
            stop_hit = (l <= p["stop"]) if sd > 0 else (h >= p["stop"])
            tgt_hit = (p.get("target") is not None and ((h >= p["target"]) if sd > 0 else (l <= p["target"]))
                       and p["entry_time"] != _iso(t))
            if stop_hit:
                gap = (o <= p["stop"]) if sd > 0 else (o >= p["stop"])
                self._close(p, self._slip(p, sd, o if gap else p["stop"]), t, "stop")
                continue
            if tgt_hit:
                self._close(p, p["target"], t, "target")
                continue
            if p.get("exit_mode") == "gemini":
                req = self._close_requests.get(p["id"])
                if req and floor_min(_dt(req["t"])) <= t:
                    self._close(p, float(req["price"]), t, "gemini_close")
                    self.s.setdefault("close_done", []).append(p["id"])
                    self._event(t, "gemini_close", symbol=p["symbol"], id=p["id"], price=req["price"],
                                request_t=req["t"], book=req.get("book"), rule=req.get("rule"),
                                reason_text=req.get("reason_text"))
                    continue
                mh = float(self.L.get("max_hold_minutes", 90))
                if t >= _dt(p["entry_time"]) + timedelta(minutes=mh):
                    self._close(p, self._slip(p, sd, c), t, f"max_hold_{int(mh)}m")
                    continue
                continue   # no time cut / VWAP-EMA50 cross exits: Gemini owns the exit
            # Time / momentum cut: no meaningful MFE within N x 15m candles
            deadline = _dt(p.get("time_cut_deadline"))
            if deadline and t >= deadline and mfe < float(p.get("mfe_min") or 0.0):
                self._close(p, self._slip(p, sd, c), t, "time_cut_no_mfe")
                continue
            # 15m close across VWAP / EMA50 against the position
            # A 15m bar that opened at T closes when we reach T+15 (t.minute % 15 == 0 and t > entry).
            if t.minute % 15 == 0 and t > _dt(p["entry_time"]):
                opened = int((t - timedelta(minutes=15)).timestamp())
                row = (getattr(self, "_m15_idx", {}) or {}).get(p["symbol"], {}).get(opened)
                if row and "close" in row:
                    px = row["close"]
                    against = False
                    reason = None
                    if sc.get("vwap_cross_exit", True) and "vwap" in row:
                        if (sd > 0 and px < row["vwap"]) or (sd < 0 and px > row["vwap"]):
                            against, reason = True, "vwap_cross_15m"
                    if (not against) and sc.get("ema50_cross_exit", True) and "ema50" in row:
                        if (sd > 0 and px < row["ema50"]) or (sd < 0 and px > row["ema50"]):
                            against, reason = True, "ema50_cross_15m"
                    if against:
                        self._close(p, self._slip(p, sd, c), t, reason)
        for s, b in bar.items():
            if b is not None:
                self.s["last_price"][s] = float(b[3])
        self._check_pass(t)
        # 4. intraday MLL / DLL on worst-case equity inside the bar
        self._breach_check(t, bar)

    def _try_fill(self, pe: dict, b, t: datetime) -> None:
        self.s["pending"].remove(pe)
        sym, side = pe["symbol"], pe["side"]
        snap = pe.get("telemetry")
        why = self._block_reason(sym, side, t)
        if why and "pending" not in why:
            # Don't double-count if on_record already skipped; this is a late block at fill time.
            self._skip_entry(t, sym, why, snapshot=snap); return
        o, h, l, c = b
        g = pe["gemini"]
        if self.L.get("entry_mode") == "limit":
            touched = (l <= g["entry"]) if side > 0 else (h >= g["entry"])
            if not touched:
                self.s["pending"].append(pe); return
            px = min(o, g["entry"]) if side > 0 else max(o, g["entry"])
        else:
            px = o
        scalp = pe.get("scalp") or {}
        tdist = scalp.get("tdist")
        sdist = scalp.get("sdist")
        # Re-anchor stop/target distances to the actual fill when we have a candle package
        if scalp.get("candle") and pe.get("atr"):
            fresh = compute_scalp_levels(
                "BUY" if side > 0 else "SELL", px, scalp["candle"], pe["atr"], sym, self.cfg,
                levels=scalp.get("levels"), vp=scalp.get("vp"),
                tick=self.spec(self.L["symbols"][sym]["contracts"][0])["tick"],
                digits=self.cfg.get("symbols", {}).get(sym, {}).get("digits", 5),
            )
            if fresh:
                tdist, sdist = fresh["tdist"], fresh["sdist"]
                scalp = {**scalp, **fresh}
        # pe was removed above, so its own reservation is not counted; open risk + OTHER pending are.
        size = self.size_trade(sym, side, pe["atr"], tdist=tdist, sdist=sdist, entry=px, scalp=scalp)
        if not size and str(self.last_size_why or "").startswith("DLL room"):
            why = f"pending cancelled at fill: {self.last_size_why}"
            self._event(t, "pending_cancelled", symbol=sym, why=why, reserved_risk=pe.get("reserved_risk"),
                        open_risk=round(self.open_risk(), 2), realized_today=self.s["day"].get("realized"))
            self._skip_entry(t, sym, why, snapshot=snap, tdist=tdist, sdist=sdist)
            return
        if not size:
            why = (f"cannot reach ${self.L['target_usd_min']:g} NET within max_minis={self.L['max_minis']:g} "
                   f"at tdist={tdist} / risk budget (SKIP): {self.last_size_why}")
            if snap is not None:
                snap = {**snap, "scalp": {**(snap.get("scalp") or {}), "tdist": tdist, "sdist": sdist,
                                          "stop_basis": scalp.get("stop_source"),
                                          "target_basis": scalp.get("target_source")},
                        "planned": {**(snap.get("planned") or {}), "tdist": tdist, "sdist": sdist}}
            self._skip_entry(t, sym, why, snapshot=snap, tdist=tdist, sdist=sdist)
            return
        tick = self.spec(size["contract"])["tick"]
        px = self._round_tick(px + side * self.L.get("slippage_ticks", 0) * tick, tick) if self.L.get("entry_mode") != "limit" \
            else self._round_tick(px, tick)
        if self.L.get("entry_mode") != "limit" and side * (px - g["entry"]) > self.L["max_chase_atr"] * pe["atr"]:
            why = f"price ran {abs(px - g['entry']):.5g} past Gemini entry (chase)"
            self._skip_entry(t, sym, why, snapshot=snap)
            return
        sp = self.spec(size["contract"])
        sc = self.cfg.get("scalp") or {}
        if self.gemini_only:
            cut_bars, mfe_min = 0, 0.0
            cut_rule = ((f"none: exit = Gemini CLOSE ({self.L.get('gemini_exit_book', '1-strike')}) or hard stop "
                         f"-${self.hard_stop_usd:g} NET; no target, no max hold, no 16:45 flat; DLL/MLL/breaker log-only")
                        if self.hard_stop_usd else
                        (f"none: exit = Gemini CLOSE only ({self.L.get('gemini_exit_book', '1-strike')}); no stop, no "
                         "target, no max hold, no 16:45 flat; DLL/MLL/breaker log-only"))
        elif self.gemini_exit:
            cut_bars, mfe_min = 0, 0.0
            cut_rule = (f"none: exit = Gemini CLOSE ({self.L.get('gemini_exit_book', '1-strike')}); safety = hard stop, "
                        f"DLL/MLL, 16:45 ET flat, {self.L.get('max_hold_minutes', 90):g} min max hold")
        else:
            cut_bars, mfe_min, cut_rule = time_cut_rule(sc, float(pe["atr"] or 0), float(size["tdist"]),
                                                        size.get("target_source"))
        # Stamp final plan into the carried snapshot
        if snap is not None:
            snap = {**snap,
                    "scalp": {**(snap.get("scalp") or {}),
                              "tdist": size["tdist"], "sdist": size["sdist"],
                              "stop_basis": scalp.get("stop_source") or (snap.get("scalp") or {}).get("stop_basis"),
                              "target_basis": scalp.get("target_source") or (snap.get("scalp") or {}).get("target_basis"),
                              "structure_label": scalp.get("structure_label"),
                              "target_source": size.get("target_source")},
                    "planned": {"tdist": size["tdist"], "sdist": size["sdist"], "qty": size["qty"],
                                "contract": size["contract"], "target_usd_net": size["target_usd"],
                                "risk_usd": size["risk_usd"], "target_source": size.get("target_source"),
                                "sizing": size.get("sizing")}}
        p = {"id": self.s["next_pos_id"], "symbol": sym, "contract": size["contract"], "qty": size["qty"], "side": side,
             "entry": px, "stop": (None if size.get("sdist") is None else self._round_tick(px - side * size["sdist"], tick)),
             "notional_risk_usd": size.get("notional_risk_usd"),
             "target": (None if size.get("tdist") is None else self._round_tick(px + side * size["tdist"], tick)),
             "entry_time": _iso(t), "exit_mode": ("gemini_only" if self.gemini_only else "gemini" if self.gemini_exit else "server"),
             "risk_usd": size["risk_usd"], "target_usd": size["target_usd"],  # NET of RT commission
             "target_source": size.get("target_source"), "sizing": size.get("sizing"),
             "target_usd_gross": size.get("target_usd_gross", size["target_usd"]),
             "commission_rt": size.get("commission_rt", 0.0),
             "atr": pe["atr"], "gemini": g, "scalp": scalp, "telemetry": snap,
             "mfe": 0.0, "mae": 0.0, "mfe_min": mfe_min,
             "time_cut_deadline": _iso(t + timedelta(minutes=15 * cut_bars)) if cut_bars else None,
             "time_cut_candles": cut_bars, "time_cut_rule": cut_rule}
        if self.hard_stop_usd and p["stop"] is None:
            p["hard_stop_px"] = hard_stop_level(self.cfg, p)
            p["hard_stop_armed_at"] = p["entry_time"]
            p["hard_stop_usd"] = self.hard_stop_usd
        self.s["next_pos_id"] += 1
        comm = round(sp["commission"] * p["qty"], 2)
        self.s["account"]["balance"] = round(self.s["account"]["balance"] - comm, 2)
        self.s["ledger"]["commissions"] = round(self.s["ledger"]["commissions"] + comm, 2)
        self.s["positions"].append(p)
        # Feed for the Gemini exit-manager paper books (signals/exit_manager.py). Additive only: the
        # baseline sim never reads it. Books copy entry/qty/contract/stop from here (same entries).
        feed = self.s.setdefault("book_feed", [])
        feed.append({k: p[k] for k in ("id", "symbol", "contract", "qty", "side", "entry", "stop", "target",
                                       "entry_time", "risk_usd", "atr")}
                    | {"candle": (scalp or {}).get("candle"), "gemini_run_at_utc": g.get("run_at_utc"),
                       "gemini_confidence": g.get("confidence"), "hard_stop_px": p.get("hard_stop_px"),
                       "hard_stop_armed_at": p.get("hard_stop_armed_at")})
        del feed[:-50]
        self.s["day"]["traded"] = True
        self.s["last_price"][sym] = c
        self._event(t, "entry", symbol=sym, contract=p["contract"], qty=p["qty"], side="LONG" if side > 0 else "SHORT",
                    entry=px, stop=p["stop"], target=p["target"], risk_usd=p["risk_usd"],
                    hard_stop_px=p.get("hard_stop_px"), hard_stop_usd=p.get("hard_stop_usd"),
                    target_usd_net=p["target_usd"], target_usd_gross=p["target_usd_gross"],
                    commission_rt=p["commission_rt"],
                    target_source=size.get("target_source"), sizing=size.get("sizing"),
                    time_cut_rule=cut_rule, time_cut_candles=cut_bars, mfe_min=round(mfe_min, 6),
                    max_minis=self.L["max_minis"],
                    max_minis_note="LucidPro 100K cap: 6 full-size minis (NQ, GC, 6E, 6B); no micros",
                    telemetry_keys=sorted((snap or {}).keys()))

    def _breach_check(self, t: datetime, bar: dict) -> None:
        a = self.s["account"]
        pos = [p for p in self.s["positions"] if bar.get(p["symbol"]) is not None]
        if not self.s["positions"]:  # realized-only checks (e.g. a stop filled through a gap)
            if self.L.get("mll_intraday_binding", True) and a["balance"] <= a["mll_floor"] + 1e-9:
                self._blow(t, f"balance {a['balance']:.2f} at/below MLL floor {a['mll_floor']:.2f} after exit")
            elif a["balance"] <= self.dll_level() + 1e-9 and not self.s["day"].get("locked"):
                self.s["day"]["locked"] = f"DLL {a['dll']:.0f} hit (soft breach) - no trading until next session"
                self.s["pending"] = []
                self._event(t, "dll_hit", dll=a["dll"], balance=a["balance"])
            return
        ref = {p["id"]: (bar[p["symbol"]][0] if p in pos else self.s["last_price"].get(p["symbol"], p["entry"]))
               for p in self.s["positions"]}
        ext = {p["id"]: ((bar[p["symbol"]][2] if p["side"] > 0 else bar[p["symbol"]][1]) if p in pos else ref[p["id"]])
               for p in self.s["positions"]}
        eq = lambda f: a["balance"] + sum(self.pnl(p, ref[p["id"]] + f * (ext[p["id"]] - ref[p["id"]]))
                                          for p in self.s["positions"])
        e0, e1 = eq(0.0), eq(1.0)
        mll_lvl = a["mll_floor"] if self.L.get("mll_intraday_binding", True) else -math.inf
        dll_lvl = self.dll_level()
        lvl = max(mll_lvl, dll_lvl)
        if e1 > lvl:
            return
        f = 0.0 if e0 <= lvl else (e0 - lvl) / (e0 - e1)
        kind = "MLL" if mll_lvl >= dll_lvl else "DLL"
        for p in list(self.s["positions"]):
            px = ref[p["id"]] + f * (ext[p["id"]] - ref[p["id"]])
            self._close(p, px, t, f"{kind}_liquidation")
        if kind == "MLL":
            self._blow(t, f"intraday equity hit MLL floor {a['mll_floor']:.2f}")
        else:
            self.s["day"]["locked"] = f"DLL {a['dll']:.0f} hit (soft breach) - no trading until next session"
            self.s["pending"] = []
            self._event(t, "dll_hit", dll=a["dll"], balance=self.s["account"]["balance"])

    # ------------------------------------------------------------------ status
    def reset_at(self) -> str | None:
        """When the current account history starts (last one-shot reset; else state creation)."""
        return self.s.get("reset_at") or self.s.get("created_at")

    def post_reset_trades(self) -> list[dict]:
        from .lucid_report import read_records
        since = _dt(self.reset_at()) if self.reset_at() else None
        rows, _ = read_records(self.logs_dir, "lucid_trades.jsonl")
        out = [r for r in rows if since is None or (r.get("t_exit") and _dt(r["t_exit"]) >= since)]
        out.sort(key=lambda r: r.get("t_exit") or "")
        return out

    def site_payload(self, recent: int = 10) -> dict:
        """Public site view of the paper account (site/lucid.json). Post-reset only; no secrets, no telemetry."""
        a = self.s["account"]
        eq = self.equity()
        trades = self.post_reset_trades()
        lp = self.s.get("last_price") or {}
        pos = []
        for p in self.s["positions"]:
            px = lp.get(p["symbol"], p["entry"])
            pos.append({"symbol": p["symbol"], "contract": p["contract"], "qty": p["qty"],
                        "side": "LONG" if p["side"] > 0 else "SHORT", "entry_time": p.get("entry_time"),
                        "entry": p["entry"], "stop": p["stop"], "target": p["target"], "last": px,
                        "unrealized": round(self.pnl(p, px), 2), "risk_usd": p.get("risk_usd"),
                        "hard_stop_px": p.get("hard_stop_px"),
                        "target_usd_net": p.get("target_usd"), "target_source": p.get("target_source")})
        keep = ("t_exit", "symbol", "contract", "qty", "side", "entry_time", "entry", "exit", "reason", "net",
                "balance_after", "hold_seconds", "held", "stopped")
        recent_rows = []
        for trow in trades[-recent:][::-1]:
            row = {k: trow.get(k) for k in keep}
            hs = trade_hold_seconds(trow)
            row["hold_seconds"] = hs
            row["held"] = format_held(hs)
            row["stopped"] = str(trow.get("reason") or "").startswith("hard_stop")
            recent_rows.append(row)
        return {
            "updated_at": datetime.now(UTC).isoformat(), "paper_only": True, "plan": self.L.get("plan"),
            "exit_mode": ("gemini_only" if self.gemini_only else "gemini" if self.gemini_exit else "server"),
            "card": ((f"Hard stop −${self.hard_stop_usd:,.0f} net · TP: none · exit: Gemini" if self.hard_stop_usd
                      else "SL: none · TP: none · exit: Gemini") if self.gemini_only else None),
            "hard_stop_net_usd": self.hard_stop_usd,
            "comparison": self.books_comparison(eq, trades),
            "breaches": [b for b in (self.s.get("breaches") or [])][-20:],
            "breach_note": "DLL / MLL / 3.5% breaker are log-only (no block, no forced close)" if self.gemini_only else None,
            "sizing_rule": (f"full-size futures only (NQ, GC, 6E, 6B), 1-{self.L['max_minis']:g} contracts sized as "
                            f"if risk were {(self.cfg.get('scalp') or {}).get('atr_stop_mult', 0.8):g} ATR at "
                            f"${self.L['max_risk_usd']:g} (notional reference only); "
                            + (f"hard stop −${self.hard_stop_usd:,.0f} NET of commission, no target, exit = Gemini "
                               f"CLOSE or the stop" if self.hard_stop_usd else
                               "no stop, no target, exit = Gemini CLOSE only")) if self.gemini_only else
                           (f"full-size futures only (NQ, GC, 6E, 6B), 1-{self.L['max_minis']:g} contracts sized by "
                            f"the hard stop, max risk ${self.L['max_risk_usd']:g}; exit = Gemini CLOSE (no target), "
                            f"{self.L.get('max_hold_minutes', 90):g} min max hold") if self.gemini_exit else
                           (f"full-size futures only (NQ, GC, 6E, 6B), 1-{self.L['max_minis']:g} contracts; "
                            f"${self.L['target_usd_min']:g}-${self.L['target_usd_max']:g} NET target "
                            f"(aim ${self.L['target_usd_aim']:g}), max risk ${self.L['max_risk_usd']:g}"),
            "reset": {"at": self.reset_at(), "reason": self.s.get("reset_reason") or self.L.get("reset_reason"),
                      "token": self.s.get("reset_token")},
            "account": {"no": a["no"], "phase": a["phase"], "start_balance": a["start_balance"],
                        "balance": round(a["balance"], 2), "equity": round(eq, 2),
                        "realized_pnl_since_reset": round(sum(float(t.get("net") or 0) for t in trades), 2) + 0.0,
                        "open_pnl": round(eq - a["balance"], 2),
                        "commission_on_open": round(a["start_balance"] + a.get("realized_pnl", 0) - a["balance"], 2),
                        "trades_since_reset": len(trades),
                        "wins": sum(1 for t in trades if float(t.get("net") or 0) > 0),
                        "losses": sum(1 for t in trades if float(t.get("net") or 0) < 0),
                        "mll_floor": a["mll_floor"], "mll_room": round(eq - a["mll_floor"], 2),
                        "dll": a["dll"], "day_realized": round(float((self.s.get("day") or {}).get("realized") or 0), 2),
                        "day_locked": (self.s.get("day") or {}).get("locked"),
                        "to_target": round(a["start_balance"] + self.L["eval_profit_target"] - a["balance"], 2)
                        if a["phase"] == "eval" else None},
            "open_positions": pos, "pending": len(self.s["pending"]),
            "recent_trades": recent_rows,
            "last_processed": self.s.get("last_processed"),
        }

    def books_comparison(self, eq: float | None = None, trades: list | None = None) -> list[dict]:
        """Main account vs the exit books (state/exit_books.json) on the same entries, for the panel."""
        a = self.s["account"]
        eq = self.equity() if eq is None else eq
        trades = self.post_reset_trades() if trades is None else trades
        rows = [{"book": "main", "label": "Main (stop)" if self.hard_stop_usd else "Main",
                 "hard_stop_net_usd": self.hard_stop_usd, "balance": round(a["balance"], 2),
                 "equity": round(eq, 2), "net_pnl": round(sum(float(t.get("net") or 0) for t in trades), 2),
                 "open_pnl": round(eq - a["balance"], 2), "trades": len(trades),
                 "wins": sum(1 for t in trades if float(t.get("net") or 0) > 0),
                 "losses": sum(1 for t in trades if float(t.get("net") or 0) <= 0),
                 "stopped": sum(1 for t in trades if str(t.get("reason") or "").startswith("hard_stop")),
                 "open_positions": len(self.s["positions"])}]
        try:
            bs = json.loads((self.state_dir.parent / "exit_books.json").read_text())
        except Exception:
            return rows
        lp = {**(self.s.get("last_price") or {}), **(bs.get("last_price") or {})}
        labels = {"1-strike": "1-strike", "2-strike": "2-strike", "nostop": "No-stop shadow"}
        for name, b in (bs.get("books") or {}).items():
            st = b.get("stats") or {}
            op = 0.0
            for p in b.get("positions") or []:
                sp = self.L["contracts"].get(p.get("contract")) or {}
                op += p["side"] * (lp.get(p["symbol"], p["entry"]) - p["entry"]) * sp.get("point_value", 0) * p["qty"]
            stop = self.hard_stop_usd if (self.hard_stop_usd and name not in
                                          ((self.cfg.get("exit_manager") or {}).get("shadow_books") or ["nostop"])) else None
            rows.append({"book": name, "label": labels.get(name, name), "hard_stop_net_usd": stop,
                         "balance": round(float(b.get("balance") or 0), 2),
                         "equity": round(float(b.get("balance") or 0) + op, 2),
                         "net_pnl": round(float(st.get("net_pnl") or 0), 2), "open_pnl": round(op, 2),
                         "trades": st.get("trades", 0), "wins": st.get("wins", 0), "losses": st.get("losses", 0),
                         "stopped": sum(v for k, v in (st.get("exit_reasons") or {}).items() if k.startswith("hard_stop")),
                         "open_positions": len(b.get("positions") or []),
                         "overlap_trades": st.get("overlap_trades", 0)})
        return rows

    def write_site(self, site_dir) -> Path:
        p = Path(site_dir) / "lucid.json"
        _atomic_write(p, self.site_payload())
        return p

    def summary(self) -> dict:
        a, lg = self.s["account"], self.s["ledger"]
        n = lg["trades"]
        net_strategy = round(lg["payouts_received"] - lg["fees_total"], 2)
        return {
            "account": {"no": a["no"], "phase": a["phase"], "balance": a["balance"], "equity": round(self.equity(), 2),
                        "profit": round(a["balance"] - a["start_balance"], 2),
                        "to_target": round(a["start_balance"] + self.L["eval_profit_target"] - a["balance"], 2)
                        if a["phase"] == "eval" else None,
                        "mll_floor": a["mll_floor"], "mll_locked": a["mll_locked"],
                        "room_to_mll": round(self.equity() - a["mll_floor"], 2), "dll": a["dll"],
                        "trading_days": a["trading_days"], "payouts": len(a["payouts"])},
            "day": self.s["day"], "positions": self.s["positions"], "pending": self.s["pending"],
            "payout": self.payout_status(),
            "ledger": {**{k: v for k, v in lg.items() if k != "fees"},
                       "win_rate": round(lg["wins"] / n, 3) if n else None,
                       "avg_win": round(lg["gross_win"] / lg["wins"], 2) if lg["wins"] else None,
                       "avg_loss": round(lg["gross_loss"] / lg["losses"], 2) if lg["losses"] else None,
                       "net_strategy_cash": net_strategy,
                       "net_incl_open_account_profit": round(net_strategy + max(0.0, a["balance"] - a["start_balance"])
                                                             * (self.L["trader_split"] if a["phase"] == "funded" else 0), 2)},
            "history": self.s["history"], "last_processed": self.s["last_processed"],
        }


# ---------------------------------------------------------------------- live data + loop glue

def build_m15_indicators(df_m1: pd.DataFrame, anchor: str = "utc_day") -> pd.DataFrame:
    """Aggregate M1 -> M15 and compute session VWAP + EMA50 the same way indicators.py does."""
    from .indicators import vwap
    if df_m1 is None or len(df_m1) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap", "ema50"])
    d = df_m1.copy()
    if d.index.tz is None:
        d.index = d.index.tz_localize("UTC")
    agg = dict(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    if "volume" in d.columns:
        agg["volume"] = ("volume", "sum")
    ohlc = d.resample("15min", label="left", closed="left").agg(**agg).dropna(subset=["close"])
    if "volume" not in ohlc.columns:
        ohlc["volume"] = 1.0
    ohlc["volume"] = ohlc["volume"].fillna(0).clip(lower=0)
    ohlc["vwap"] = vwap(ohlc, anchor)
    ohlc["ema50"] = ohlc["close"].ewm(span=50, adjust=False).mean()
    return ohlc


async def fetch_m1(ticker: str, since: datetime, now: datetime, timeout: float = 60,
                   include_forming: bool = False) -> pd.DataFrame:
    """Dukascopy 1-minute bid bars from `since` to now. Closed bars only unless include_forming."""
    from .data_providers import DukascopyProvider, closed_only
    dp = DukascopyProvider()
    out = tempfile.mkdtemp(prefix="duka_m1_")
    try:
        args = dp.cmd + ["-i", ticker, "-from", (since - timedelta(minutes=5)).strftime("%Y-%m-%d"),
                         "-to", (now + timedelta(days=1)).strftime("%Y-%m-%d"), "-t", "m1", "-f", "json",
                         "-v", "true", "-dir", out, "-r", "2"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
        files = glob.glob(f"{out}/*.json")
        if not files:
            raise RuntimeError(f"dukascopy m1 {ticker}: no file ({stdout.decode()[-200:].strip()})")
        rows = json.load(open(files[0]))
    finally:
        shutil.rmtree(out, ignore_errors=True)
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    if include_forming:
        df = df[df.index <= pd.Timestamp(now)]
    else:
        df = closed_only(df, 1, now)
    return df[df.index >= pd.Timestamp(since)]


_LOCK = asyncio.Lock()


def time_cut_rule(sc: dict, atr: float, tdist: float, target_source: str | None) -> tuple[int, float, str]:
    """2-bar time cut, made consistent with the planned target.

    Scalp targets (fixed / nearest structure): unchanged user rule, time_cut_candles (2) x 15m and
    "moved in favour" = MFE >= mfe_min_atr x ATR.
    Targets stretched past the scalp level (target_source extended:* / structure:next:*): time_cut_candles_extended
    (4) x 15m, and MFE >= min(mfe_min_atr x ATR, mfe_min_target_frac x planned target distance), i.e. a third of the
    way to the target counts, so a 38-pt target is not judged by a threshold set for a 5-pt scalp.
    Hard stop and the risk cap are unchanged."""
    base = int(sc.get("time_cut_candles", 2))
    by_atr = float(sc.get("mfe_min_atr", 0.25)) * atr
    ext = str(target_source or "").startswith(("extended", "structure:next"))
    if not ext or tdist <= 0:
        return base, by_atr, f"{base}x15m (scalp target); mfe_min={sc.get('mfe_min_atr', 0.25):g}xATR={by_atr:.5g}"
    bars = int(sc.get("time_cut_candles_extended", 4))
    frac = float(sc.get("mfe_min_target_frac", 1 / 3))
    by_tgt = frac * tdist
    mfe_min = min(by_atr, by_tgt)
    return bars, mfe_min, (f"{bars}x15m (extended target); mfe_min=min({sc.get('mfe_min_atr', 0.25):g}xATR={by_atr:.5g}, "
                           f"{frac:.4g}xtarget {tdist:g}={by_tgt:.5g})={mfe_min:.5g}")


def due_pendings(sim: LucidSim, now: datetime) -> list[dict]:
    """Pendings whose fill_after has been reached (they can fill as soon as a bar exists)."""
    out = []
    for pe in sim.s.get("pending") or []:
        t = _dt(pe.get("fill_after"))
        if t is not None and t <= now:
            out.append(pe)
    return out


def due_fill_until(sim: LucidSim, now: datetime) -> datetime:
    """When a pending is due, process the current minute (until = next minute, exclusive)
    so the fill does not wait for that M1 bar to close."""
    until = floor_min(now)
    if due_pendings(sim, now):
        until = max(until, floor_min(now) + timedelta(minutes=1))
    return until


def _bar_index_utc(df: pd.DataFrame) -> pd.DatetimeIndex:
    if df is None or not len(df):
        return pd.DatetimeIndex([], tz="UTC")
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.tz_convert("UTC")


def inject_live_bars(bars: dict[str, pd.DataFrame], sim: LucidSim, now: datetime,
                     live_prices: dict[str, float] | None) -> dict[str, pd.DataFrame]:
    """Fill missing minutes up to now with a synthetic OHLC bar from a live price, so a due
    pending can fill when the closed M1 feed is still one minute behind."""
    if not live_prices:
        return bars
    out = dict(bars)
    end = floor_min(now)
    for pe in due_pendings(sim, now):
        sym, px = pe["symbol"], live_prices.get(pe["symbol"])
        if px is None:
            continue
        t = floor_min(_dt(pe["fill_after"]))
        missing = []
        while t <= end:
            df = out.get(sym)
            have = _bar_index_utc(df) if df is not None else pd.DatetimeIndex([], tz="UTC")
            ts = pd.Timestamp(t)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            if ts not in have:
                missing.append(ts)
            t += timedelta(minutes=1)
        if not missing:
            continue
        add = pd.DataFrame({"open": px, "high": px, "low": px, "close": px},
                           index=pd.DatetimeIndex(missing, tz="UTC"))
        df = out.get(sym)
        out[sym] = pd.concat([df, add]).sort_index() if df is not None and len(df) else add
    return out


def lucid_needs_replay(sim: LucidSim) -> bool:
    """True when a short replay should run between 2-min entry cycles: due/queued fills or unapplied CLOSEs."""
    if sim.s.get("pending"):
        return True
    f = Path(sim.state_dir) / "close_requests.json"
    if not f.exists():
        return False
    try:
        rows = json.loads(f.read_text())
    except Exception:
        return False
    done = set(sim.s.get("close_done") or [])
    open_ids = {p["id"] for p in sim.s.get("positions") or []}
    return any(r.get("id") in open_ids and r.get("id") not in done for r in (rows or []))


def apply_due_fills(sim: LucidSim, bars: dict[str, pd.DataFrame], now: datetime,
                    live_prices: dict[str, float] | None = None,
                    m15: dict[str, pd.DataFrame] | None = None) -> list[int]:
    """Process far enough to fill any pending whose fill_after has passed. Returns new position ids."""
    before = {p["id"] for p in sim.s.get("positions") or []}
    until = due_fill_until(sim, now)
    if until <= floor_min(_dt(sim.s["last_processed"])):
        return []
    bars = inject_live_bars(bars, sim, now, live_prices)
    sim.process(bars, until, m15=m15)
    return [p["id"] for p in sim.s.get("positions") or [] if p["id"] not in before]


async def _default_live_prices(cfg: dict, symbols: set[str], now: datetime) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        from .live_price import fetch_symbol_m1
    except Exception:
        return {}
    res = await asyncio.gather(*[fetch_symbol_m1(s, cfg, now) for s in symbols], return_exceptions=True)
    out: dict[str, float] = {}
    for s, r in zip(symbols, res):
        if isinstance(r, Exception) or not r or r[0] is None or not len(r[0]):
            continue
        try:
            out[s] = float(r[0]["close"].iloc[-1])
        except Exception:
            continue
    return out


async def _fetch_and_replay(sim: LucidSim, cfg: dict, now: datetime,
                            live_prices: dict[str, float] | None = None) -> tuple[dict, datetime, list[int]]:
    """Fetch M1 (forming bars included when a fill is due) and process. Returns (bars, until, new_ids)."""
    since = _dt(sim.s["last_processed"])
    need = {p["symbol"] for p in sim.s["positions"]} | {p["symbol"] for p in sim.s["pending"]}
    due = due_pendings(sim, now)
    bars, m15, until = {}, {}, floor_min(now)
    before = {p["id"] for p in sim.s.get("positions") or []}
    if need:
        lookback = since - timedelta(hours=36)
        include_forming = bool(due)
        res = await asyncio.gather(
            *[fetch_m1(cfg["lucid"]["symbols"][s]["m1"], lookback, now, include_forming=include_forming)
              for s in need],
            return_exceptions=True)
        lasts = []
        for s, r in zip(need, res):
            if isinstance(r, Exception):
                log.warning("lucid m1 %s failed: %s", s, r); continue
            bars[s] = r[r.index >= pd.Timestamp(since)] if len(r) else r
            anchor = cfg.get("symbols", {}).get(s, {}).get(
                "vwap_anchor", cfg.get("chart", {}).get("vwap", {}).get("default_anchor", "utc_day"))
            try:
                m15[s] = build_m15_indicators(r, anchor)
            except Exception:
                log.exception("lucid m15 indicators %s failed", s)
            if len(r):
                lasts.append(r.index[-1].to_pydatetime() + timedelta(minutes=1))
        fresh = [x for x in lasts if now - x <= timedelta(minutes=20)]
        if fresh:
            until = min(until, min(fresh))
        elif sim.in_session(now):
            until = min(until, max(lasts)) if lasts else since
        until = max(until, since)
    # A lagging closed-bar feed used to cap `until` before the fill minute, so the pending
    # sat until the next 2-min cycle. Extend to the current minute and inject live prices.
    if due:
        until = max(until, due_fill_until(sim, now))
        missing = {pe["symbol"] for pe in due}
        if live_prices is None:
            live_prices = await _default_live_prices(cfg, missing, now)
        bars = inject_live_bars(bars, sim, now, live_prices)
    sim.process(bars, until, m15=m15)
    new_ids = [p["id"] for p in sim.s.get("positions") or [] if p["id"] not in before]
    return bars, until, new_ids


async def _persist_sim(sim: LucidSim, cfg: dict, write_report: bool = True) -> None:
    sim.save()
    if write_report:
        from .lucid_report import write_report as _wr
        try:
            await asyncio.to_thread(_wr, sim.logs_dir)
        except Exception:
            log.exception("lucid report refresh failed (sim state already saved)")
    try:
        await asyncio.to_thread(sim.write_site, cfg["site"]["dir"])
    except Exception:
        log.exception("lucid site json failed (sim state already saved)")


async def lucid_replay(cfg: dict, now: datetime | None = None, *, only_if_needed: bool = False,
                       live_prices: dict[str, float] | None = None) -> dict:
    """Replay pending fills and Gemini CLOSE requests without waiting for the 2-min entry cycle.

    When only_if_needed is set, this is a no-op (no feed fetch) unless there is a queued
    pending or an unapplied close request, so the 18-second exit loop can call it cheaply."""
    if not cfg.get("lucid", {}).get("enabled"):
        return {}
    async with _LOCK:
        try:
            now = now or datetime.now(UTC)
            sim = await asyncio.to_thread(LucidSim, cfg)
            if only_if_needed and not lucid_needs_replay(sim):
                return {"skipped": "not_needed", "processed_until": sim.s["last_processed"]}
            _, until, new_ids = await _fetch_and_replay(sim, cfg, now, live_prices)
            await _persist_sim(sim, cfg, write_report=bool(new_ids))
            if new_ids:
                log.info("lucid replay filled ids %s (processed until %s)", new_ids, sim.s["last_processed"])
            return {"processed_until": sim.s["last_processed"], "until": _iso(until), "filled": new_ids}
        except Exception:
            log.exception("lucid replay failed (paper sim only; signals unaffected)")
            return {}


async def lucid_step(cfg: dict, record: dict | None = None, now: datetime | None = None,
                     live_prices: dict[str, float] | None = None) -> dict:
    """Replay M1, queue this cycle's signals, then replay again so a due pending can fill
    in the same step (does not wait for the next 2-min cycle). Never raises."""
    if not cfg.get("lucid", {}).get("enabled"):
        return {}
    async with _LOCK:
        try:
            now = now or datetime.now(UTC)
            sim = await asyncio.to_thread(LucidSim, cfg)
            bars, _, new_ids = await _fetch_and_replay(sim, cfg, now, live_prices)
            dec = sim.on_record(record, now) if record else []
            # Same-step fill: on_record queues with fill_after = next minute; if that
            # minute is already due (or live prices cover it), fill before we return.
            more = apply_due_fills(sim, bars, now, live_prices)
            new_ids = new_ids + more
            await _persist_sim(sim, cfg)
            return {"decisions": dec, "processed_until": sim.s["last_processed"], "filled": new_ids}
        except Exception:
            log.exception("lucid step failed (paper sim only; signals unaffected)")
            return {}


def format_status(sm: dict, cfg: dict) -> str:
    a, lg, d, po = sm["account"], sm["ledger"], sm["day"], sm["payout"]
    tz = ZoneInfo(cfg["general"]["session_tz"])
    lp = _dt(sm["last_processed"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    L = []
    L.append(f"LUCID PAPER SIM - {cfg['lucid']['plan']}  (paper only, proxy spot/CFD prices)")
    L.append(f"  processed through {lp} Dubai")
    mm = cfg["lucid"]["max_minis"]
    L.append(f"  SIZE CAP: max {mm:g} full-size contracts (NQ/GC/6E/6B, no micros; LucidPro 100K cap)")
    L.append(f"  TARGETS: ${cfg['lucid']['target_usd_min']:g}-${cfg['lucid']['target_usd_max']:g} "
             f"NET of RT commission (aim ${cfg['lucid']['target_usd_aim']:g} net)")
    if (cfg.get("scalp") or {}).get("enabled", False):
        L.append("  SL/TP: server-side scalp (structure or 0.8xATR stop; fixed/structural target; "
                 "time-cut + VWAP/EMA50 15m cross exits). Gemini = direction+confidence only.")
    L.append(f"Account #{a['no']}  phase={a['phase'].upper()}  balance=${a['balance']:,.2f}  equity=${a['equity']:,.2f}  "
             f"P&L=${a['profit']:+,.2f}")
    if a["phase"] == "eval":
        L.append(f"  to pass: ${a['to_target']:,.2f} more (target +${cfg['lucid']['eval_profit_target']:,.0f})")
    L.append(f"  MLL floor ${a['mll_floor']:,.2f} ({'locked' if a['mll_locked'] else 'trailing'})  room ${a['room_to_mll']:,.2f}"
             f"  | DLL ${a['dll']:,.0f}  day {d['id']} realized ${d['realized']:+,.2f}"
             + (f"  LOCKED: {d['locked']}" if d.get("locked") else ""))
    L.append(f"  trading days {a['trading_days']}  payouts on this account {a['payouts']}")
    if a["phase"] == "funded":
        L.append(f"  payout: {'ELIGIBLE $%.0f' % po['amount'] if po['eligible'] else 'not yet - ' + po['why']}")
    L.append(f"Open positions: {len(sm['positions'])}  pending: {len(sm['pending'])}")
    for p in sm["positions"]:
        L.append(f"  {p['symbol']}->{p['contract']} x{p['qty']} {'LONG' if p['side'] > 0 else 'SHORT'} @ {p['entry']} "
                 + (f"stop {p['stop']} (-${p['risk_usd']:,.0f}) " if p.get('stop') is not None else (f"hard stop {p['hard_stop_px']} (-${p.get('hard_stop_usd') or 500:,.0f} net) " if p.get('hard_stop_px') is not None else "SL: none "))
                 + (f"target {p['target']} (+${p['target_usd']:,.0f} net)" if p.get('target') is not None
                    else "TP: none, exit: Gemini"))
    L.append("Strategy ledger (all accounts):")
    wr = f"{lg['win_rate']:.0%}" if lg["win_rate"] is not None else "n/a"
    L.append(f"  trades {lg['trades']}  wins {lg['wins']}  losses {lg['losses']}  win rate {wr}  "
             f"avg win {lg['avg_win']}  avg loss {lg['avg_loss']}  commissions ${lg['commissions']:,.2f}")
    L.append(f"  net trade P&L ${lg['net_trade_pnl']:+,.2f}  signals taken {lg['accepted_signals']}  skipped {lg['skipped_signals']}")
    L.append(f"  accounts used {a['no']}  evals bought {lg['evals_bought']}  resets {lg['resets']}  passes {lg['passes']}  "
             f"blown evals {lg['blown_evals']}  blown funded {lg['blown_funded']}")
    L.append(f"  fees paid ${lg['fees_total']:,.2f}  payouts {lg['payout_count']} gross ${lg['payouts_gross']:,.2f} "
             f"-> received (90%) ${lg['payouts_received']:,.2f}")
    L.append(f"  NET CASH (payouts received - fees) ${lg['net_strategy_cash']:+,.2f}")
    for h in sm["history"][-5:]:
        L.append(f"  past #{h['no']} {h['phase']} {h['result']} bal ${h['balance']:,.2f} trades {h['trades']} "
                 f"payouts {h['payouts']} (${h['payouts_received']:,.2f})")
    return "\n".join(L)
