"""Shared event-trigger infrastructure for the event-driven paper books (PAPER ONLY).

Used by ds-evt-v1 (signals/ds_evt.py: DS-IMG-E / DS-TXT-E) and ev-v1 (signals/ev_evt.py: EV-FABLE / EV-OPUS + code
controls). Nothing here changes an existing book.

Shared builders (the SAME code the timed books use, callable with a timestamp; nothing forked):
  snapshot_at(sym, cfg, now)   exit_manager.market_snapshot: indicators.build_payload on the M15 history + the live
                               price path (Dukascopy tick / FirmTape NQ) + FirmTape GEX -> (payload, live M1 frame)
  numeric_context(payload)     bracket_v1._slim_context (the numeric-context JSON the DeepSeek / C-OPUS-B books get)
  facts_at(payload, now)       bracket_v1.compute_facts (+ h1_trend from claude_rules.h1_context and the aliases the
                               event prompts name: dist_vwap_atr, last_bar_level_events)
  csv_table(payload)           last 100 closed M15 bars + one FORMING row (DS-TXT-E)
  m5_payload(...)              claude_sim.build_m5_exit_payload (M5 chart_v2 from live M1) + event level / marker
  render(payloads)             renderer.render_all (chart_v2 JPEG)

Also: M1Store (live M1 history per symbol, seeded once), EventFeed (one M1-close +10 s loop shared by every event
engine), whatif_path (fixed-bracket what-if on M1), usd_side + open_usd_sides (correlation across books), and
EventBook (a BracketBook with the event-book risk rules: per-contract caps, re-entry locks, daily stop, trailing
MLL guard, trigger-price fill guard).
"""
from __future__ import annotations
import asyncio, json, logging, math, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import bracket_v1 as B
from .bracket import BracketBook, _dt, _iso, round_adverse

log = logging.getLogger("signals.event_feed")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
DUBAI = ZoneInfo("Asia/Dubai")
EVENT_SYMBOLS = ["XAUUSD", "EURUSD", "US100"]


def hm(s: str) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def in_window(now: datetime, window=("08:00", "20:30")) -> bool:
    t = now.astimezone(UTC)
    m = t.hour * 60 + t.minute + t.second / 60.0
    return hm(window[0]) <= m < hm(window[1])


def m15_open(now: datetime) -> datetime:
    return datetime.fromtimestamp(int(now.timestamp()) // 900 * 900, UTC)


def bar_index(t: datetime) -> int:
    """Monotone M15 bar number (for 'N bars between' guards)."""
    return int(t.timestamp()) // 900


def ny_cash(now: datetime) -> bool:
    et = now.astimezone(ET)
    m = et.hour * 60 + et.minute
    return et.weekday() < 5 and 9 * 60 + 30 <= m < 16 * 60


# ---------------------------------------------------------------------------------------------- shared builders
async def snapshot_at(sym: str, cfg: dict, now: datetime):
    from .exit_manager import market_snapshot
    return await market_snapshot(sym, cfg, now)


def numeric_context(payload: dict) -> dict:
    return B._slim_context(payload)


def h1_from_payload(payload: dict) -> pd.DataFrame | None:
    cs = payload.get("candles") or []
    if not cs:
        return None
    df = pd.DataFrame(cs)
    df.index = pd.to_datetime(df.pop("time"), unit="s", utc=True)
    return df.resample("1h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def facts_at(payload: dict, now: datetime) -> dict:
    from .claude_rules import h1_context
    f = B.compute_facts(payload)
    try:
        anchor = (payload.get("context") or {}).get("vwap_anchor") or "utc_day"
        h1 = h1_context(h1_from_payload(payload), now, anchor)
    except Exception:
        h1 = {"h1_trend": None}
    gex = (payload.get("context") or {}).get("gex") or {}
    f.update({"dist_vwap_atr": f.get("vwap_distance_atr"),
              "last_bar_level_events": [e for e in f.get("level_events") or [] if e.get("event") != "untouched"],
              "h1_trend": h1.get("h1_trend"), "gex_regime": gex.get("regime")})
    return f


def _line_map(rows) -> dict:
    return {r["time"]: r["value"] for r in rows or [] if r.get("value") is not None}


def csv_table(payload: dict, n: int = 100) -> str:
    """t_utc,o,h,l,c,vol,vwap,ema20,ema50,rvol for the last n closed M15 bars (oldest first) + one FORMING row."""
    ctx = payload.get("context") or {}
    d = int(ctx.get("digits") or payload.get("digits") or 5)
    vw, e20, e50 = _line_map(payload.get("vwap")), _line_map(payload.get("mid")), _line_map(payload.get("ema50"))
    vol = {v["time"]: v for v in payload.get("volume") or []}
    f = lambda x: "" if x is None else f"{float(x):.{d}f}"
    rows = ["t_utc,o,h,l,c,vol,vwap,ema20,ema50,rvol"]
    cs = (payload.get("candles") or [])[-n:]
    for c in cs:
        t = datetime.fromtimestamp(int(c["time"]), UTC).strftime("%H:%M")
        v = vol.get(c["time"]) or {}
        rv = v.get("rvol")
        rows.append(",".join([t, f(c["open"]), f(c["high"]), f(c["low"]), f(c["close"]),
                              str(int(round(float(v.get("value") or 0)))), f(vw.get(c["time"])),
                              f(e20.get(c["time"])), f(e50.get(c["time"])), "" if rv is None else f"{rv:.2f}"]))
    fo = ctx.get("forming_m15_open_utc")
    if fo and ctx.get("forming_m15_open") is not None:
        t = pd.Timestamp(fo).tz_convert("UTC").strftime("%H:%M")
        live = ctx.get("live_price")
        fv = ctx.get("forming_m15_volume")
        last_e20 = ctx.get("ema20_baseline")
        last_e50 = ctx.get("ema50")
        e20f = last_e20 + 2 / 21 * (live - last_e20) if live is not None and last_e20 is not None else None
        e50f = last_e50 + 2 / 51 * (live - last_e50) if live is not None and last_e50 is not None else None
        rows.append(",".join(["FORMING " + t, f(ctx.get("forming_m15_open")), f(ctx.get("forming_m15_high")),
                              f(ctx.get("forming_m15_low")), f(live), "" if fv is None else str(int(round(fv))),
                              f(ctx.get("vwap")), f(e20f), f(e50f),
                              "" if ctx.get("forming_rvol") is None else f"{ctx['forming_rvol']:.2f}"]))
    return "\n".join(rows)


def forming_volume(m1: pd.DataFrame | None, now: datetime) -> float | None:
    if m1 is None or not len(m1) or "volume" not in m1.columns:
        return None
    t0 = pd.Timestamp(m15_open(now))
    return float(m1[(m1.index >= t0) & (m1.index <= pd.Timestamp(now))]["volume"].sum())


def forming_rvol(payload: dict, m1: pd.DataFrame | None, now: datetime, n: int = 20) -> float | None:
    """Forming M15 bar volume / mean volume of the last n closed M15 bars (same RVOL definition as the chart)."""
    fv = forming_volume(m1, now)
    vols = [float(v.get("value") or 0) for v in (payload.get("volume") or [])[-n:]]
    if fv is None or not vols:
        return None
    avg = sum(vols) / len(vols)
    return round(fv / avg, 3) if avg > 0 else None


def m5_bars(m1: pd.DataFrame | None, now: datetime) -> pd.DataFrame:
    """Closed M5 bars from M1 (left-labelled)."""
    if m1 is None or not len(m1):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = m1.sort_index()
    if "volume" not in df.columns:
        df = df.assign(volume=0.0)
    agg = df.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(
        subset=["open", "high", "low", "close"])
    return agg[agg.index + pd.Timedelta(minutes=5) <= pd.Timestamp(now)]


def m5_payload(payload: dict, m1: pd.DataFrame | None, now: datetime, *, n: int = 72,
               event_level: float | None = None, event_bar_time: datetime | None = None,
               event_direction: str | None = None) -> dict | None:
    """Claude's M5 chart builder (claude_sim.build_m5_exit_payload, live M1 -> M5, M15 VWAP/EMA lines, S/R + GEX)
    with the event level as a solid white line and the event bar marked with a small white triangle."""
    from .claude_sim import build_m5_exit_payload
    out = build_m5_exit_payload(payload, m1, now, n=n)
    if out is None:
        return None
    out["visible"] = n
    out["forming_candle"] = None          # the event bar (last closed M5) is the right-most solid bar
    if event_level is not None:
        out["entry_line"] = {"price": float(event_level), "color": "#FFFFFF", "title": "EVENT"}
    if event_bar_time is not None:
        down = event_direction == "down"
        out["event_marker"] = {"time": int(event_bar_time.timestamp()), "shape": "arrowDown" if down else "arrowUp",
                               "position": "aboveBar" if down else "belowBar", "color": "#FFFFFF"}
    return out


async def render(payloads: list[dict], key: str = "evt") -> dict[str, bytes]:
    from .renderer import render_all
    tagged = [{**p, "page_key": f"{key}-{p['symbol']}"} for p in payloads]
    return await render_all(tagged) or {}


# ---------------------------------------------------------------------------------------------- M1 history
class M1Store:
    """Live M1 per symbol: seeded once with `seed_hours` of Dukascopy M1, then merged from every feed snapshot."""

    def __init__(self, cfg: dict, seed_hours: float = 7.0, keep_hours: float = 12.0, fetch=None):
        self.cfg, self.seed_hours, self.keep_hours = cfg, seed_hours, keep_hours
        self.df: dict[str, pd.DataFrame] = {}
        self._seeded: set[str] = set()
        self._fetch = fetch

    async def seed(self, sym: str, now: datetime) -> None:
        if sym in self._seeded:
            return
        self._seeded.add(sym)
        try:
            if self._fetch is not None:
                df = await self._fetch(sym, now - timedelta(hours=self.seed_hours), now)
            else:
                from .live_price import _fetch_duka_m1, m1_ticker
                tk = m1_ticker(sym, self.cfg, "dukascopy")
                if not tk:
                    return
                df = await _fetch_duka_m1(tk, now - timedelta(hours=self.seed_hours), now, 60.0)
            self.merge(sym, df)
        except Exception as e:
            log.warning("event M1 seed %s failed: %s", sym, e)

    def merge(self, sym: str, df: pd.DataFrame | None) -> None:
        if df is None or not len(df):
            return
        old = self.df.get(sym)
        d = df if old is None else pd.concat([old, df])
        d = d[~d.index.duplicated(keep="last")].sort_index()
        cut = d.index[-1] - pd.Timedelta(hours=self.keep_hours)
        self.df[sym] = d[d.index >= cut]

    def frame(self, sym: str) -> pd.DataFrame | None:
        return self.df.get(sym)


# ---------------------------------------------------------------------------------------------- what-if
def whatif_path(side: int, entry: float, atr: float, t0: datetime, bars: pd.DataFrame | None, *,
                stop_atr: float = 1.0, target_atr: float = 1.0, hold_min: float = 90.0,
                now: datetime | None = None) -> dict | None:
    """Fixed bracket on M1 bars after t0 (stop wins a bar touching both). None while unresolved."""
    if not atr or atr <= 0 or entry is None:
        return {"resolved": True, "R": None, "exit_reason": "no_atr"}
    stop = entry - side * stop_atr * atr
    tgt = entry + side * target_atr * atr
    dl = t0 + timedelta(minutes=hold_min)
    mfe = mae = 0.0
    last_c, last_t = None, None
    if bars is not None and len(bars):
        sel = bars[(bars.index >= pd.Timestamp(t0).floor("min")) & (bars.index < pd.Timestamp(dl))]
        for ts, r in sel.iterrows():
            h, l, c = float(r["high"]), float(r["low"]), float(r["close"])
            fav, adv = (h, l) if side > 0 else (l, h)
            mfe = max(mfe, side * (fav - entry))
            mae = max(mae, -side * (adv - entry))
            if (l <= stop) if side > 0 else (h >= stop):
                return {"resolved": True, "R": -round(stop_atr / stop_atr, 3), "exit_reason": "stop",
                        "exit_t": _iso(ts.to_pydatetime()), "mfe_atr": round(mfe / atr, 3),
                        "mae_atr": round(mae / atr, 3)}
            if (h >= tgt) if side > 0 else (l <= tgt):
                return {"resolved": True, "R": round(target_atr / stop_atr, 3), "exit_reason": "target",
                        "exit_t": _iso(ts.to_pydatetime()), "mfe_atr": round(mfe / atr, 3),
                        "mae_atr": round(mae / atr, 3)}
            last_c, last_t = c, ts
    if (now or datetime.now(UTC)) >= dl + timedelta(minutes=1) and last_c is not None:
        return {"resolved": True, "R": round(side * (last_c - entry) / (stop_atr * atr), 3), "exit_reason": "time",
                "exit_t": _iso(last_t.to_pydatetime()), "mfe_atr": round(mfe / atr, 3), "mae_atr": round(mae / atr, 3)}
    return None


# ---------------------------------------------------------------------------------------------- correlation
def usd_side(sym: str, side: int) -> str | None:
    """XAUUSD / EURUSD BUY = short-USD, SELL = long-USD. US100 has no USD side."""
    if sym in ("XAUUSD", "EURUSD"):
        return "short_usd" if side > 0 else "long_usd"
    return None


def _side_of(p: dict) -> int | None:
    s = p.get("side")
    if isinstance(s, (int, float)):
        return 1 if s > 0 else -1
    s = str(s or p.get("action") or p.get("direction") or "").upper()
    return 1 if s in ("BUY", "LONG") else -1 if s in ("SELL", "SHORT") else None


def open_usd_sides(state_root: Path, files: list[str]) -> list[dict]:
    """Open/pending positions with a USD side across the listed state files (read-only)."""
    out = []
    for rel in files:
        try:
            st = json.loads((Path(state_root) / rel).read_text())
        except Exception:
            continue
        for p in (st.get("positions") or []) + (st.get("pending") or []):
            sym = str(p.get("symbol") or "").upper()
            sd = _side_of(p)
            if sd is None:
                continue
            u = usd_side(sym, sd)
            if u:
                out.append({"file": rel, "symbol": sym, "side": sd, "usd_side": u})
    return out


# ---------------------------------------------------------------------------------------------- event book
EVENT_BOOK_DEFAULTS = {
    "symbols": list(EVENT_SYMBOLS), "stop_atr": 1.0, "target_atr": 1.0, "max_hold_minutes": 90,
    "flat_utc": "21:45", "reopen_utc": "23:00", "no_new_entries_last_minutes": 15, "risk_usd": 900.0,
    "max_risk_usd": 1200.0, "max_minis": 6, "contract_caps": {"NQ": 1, "GC": 1, "6E": 4}, "slippage_ticks": 1,
    "start_balance": 100000.0, "check_seconds": 18, "pending_expiry_seconds": 120, "max_chase_atr": 0.3,
    "max_trigger_adverse_atr": 0.5, "gap_seconds": 180, "fill_quote_max_age_s": 60, "recent_trades": 20,
    "daily_stop_usd": 1500.0, "mll_usd": 3000.0, "locked_mll_balance": 100100.0, "mll_buffer_usd": 300.0,
    "after_loss_lock_min": 30,
}


def next_m15_close(t: datetime) -> datetime:
    return datetime.fromtimestamp((int(t.timestamp()) // 900 + 1) * 900, UTC)


def lucid_day(t: datetime) -> str:
    et = t.astimezone(ET)
    return str((et + timedelta(days=1)).date() if et.hour >= 18 else et.date())


class EventBook(BracketBook):
    """BracketBook + the event-book risk rules (shared by DS-IMG-E / DS-TXT-E / EV-FABLE / EV-OPUS).

      size     floor($900 / (1 ATR x point value)), min 1, per-contract caps (NQ 1, GC 1, 6E 4), skip if one
               contract risks > $1,200, 6 contracts aggregate, one position per symbol
      fill     live quote + 1 tick; skip if > 0.3 ATR worse than the live price at the call (max_chase_atr) or
               (DS) > 0.5 ATR worse than the trigger price
      locks    symbol locked until the next M15 close after any exit (EV: lock_minutes after exit); after a
               stop-out 30 min and no same-side re-entry this session
      daily    day P&L <= -$1,500 -> flatten, no entries until the next session; entry only if
               (room to the $3,000 trailing MLL - open stop risk - new risk) >= $300; flat 21:45 UTC
      usd side one open per USD side across `corr_files` (state files of the other books in the scope)
    """
    CORR_FILES: list[str] = []

    def fresh(self) -> dict:
        s = super().fresh()
        s.update({"locks": {}, "day": {"id": None, "realized": 0.0, "flattened": False},
                  "highest_close": s["start_balance"],
                  "mll_floor": s["start_balance"] - float(self.b.get("mll_usd", 3000))})
        return s

    def _ensure(self) -> None:
        self.s.setdefault("locks", {})
        self.s.setdefault("day", {"id": None, "realized": 0.0, "flattened": False})
        self.s.setdefault("highest_close", self.s["start_balance"])
        self.s.setdefault("mll_floor", self.s["start_balance"] - float(self.b.get("mll_usd", 3000)))

    def roll_day(self, now: datetime) -> None:
        self._ensure()
        d = lucid_day(now)
        if self.s["day"].get("id") != d:
            if self.s["day"].get("id") is not None:
                # end of session: trailing MLL from the highest end-of-day balance, locked at 100,100
                self.s["highest_close"] = max(float(self.s["highest_close"]), float(self.s["balance"]))
                fl = self.s["highest_close"] - float(self.b["mll_usd"])
                self.s["mll_floor"] = min(max(float(self.s["mll_floor"]), fl), float(self.b["locked_mll_balance"]))
            self.s["day"] = {"id": d, "realized": 0.0, "flattened": False}

    def size(self, contract: str, atr: float):
        q, why = super().size(contract, atr)
        if q is None:
            return q, why
        cap = int((self.b.get("contract_caps") or {}).get(contract, q))
        return min(q, cap), None

    def open_stop_risk(self) -> float:
        return round(sum(float(p.get("risk_usd") or 0) for p in self.s["positions"]), 2)

    def lock_reason(self, sym: str, side: int, now: datetime) -> str | None:
        self._ensure()
        lk = self.s["locks"].get(sym)
        if not lk:
            return None
        if now < _dt(lk["until"]):
            return f"locked_reentry until {lk['until']}"
        if lk.get("loss") and lk.get("side") == side and lk.get("day") == lucid_day(now):
            return "locked_reentry: no same-side re-entry after a stop-out (this session)"
        return None

    def symbol_locked(self, sym: str, now: datetime) -> bool:
        lk = (self.s.get("locks") or {}).get(sym)
        return bool(lk and now < _dt(lk["until"]))

    def lock_until(self, t: datetime) -> datetime:
        return next_m15_close(t)

    def close(self, p, price, t, reason, extra=None):
        rec = super().close(p, price, t, reason, extra)
        self._ensure()
        self.roll_day(t)
        self.s["day"]["realized"] = round(float(self.s["day"]["realized"]) + rec["net"], 2)
        stopped = str(reason).startswith("stop")
        until = self.lock_until(t)
        if stopped:
            until = max(until, t + timedelta(minutes=float(self.b["after_loss_lock_min"])))
        self.s["locks"][p["symbol"]] = {"until": _iso(until), "loss": stopped, "side": p["side"],
                                        "day": lucid_day(t), "exit_t": _iso(t)}
        return rec

    def risk_block(self, sym: str, side: int, new_risk: float, now: datetime) -> str | None:
        """Account-level reasons a new entry is not allowed (None = ok)."""
        self.roll_day(now)
        if self.s["day"].get("flattened"):
            return "daily_stop: flattened at -$1,500 (no entries until the next session)"
        if not self.entries_allowed(now):
            return "session: outside session / last minutes before the 21:45 UTC flat"
        room = float(self.s["balance"]) - float(self.s["mll_floor"])
        if room - self.open_stop_risk() - new_risk < float(self.b["mll_buffer_usd"]):
            return (f"mll_guard: room ${room:,.0f} - open risk ${self.open_stop_risk():,.0f} - new ${new_risk:,.0f} "
                    f"< ${self.b['mll_buffer_usd']:g}")
        u = usd_side(sym, side)
        if u:
            mine = [{"symbol": p["symbol"], "usd_side": usd_side(p["symbol"], p["side"])}
                    for p in self.s["positions"] + self.s["pending"]]
            others = open_usd_sides(self.state_dir.parent, self.CORR_FILES)
            if any(x["usd_side"] == u for x in mine + others):
                return f"correlation: a {u} position is already open (one per USD side)"
        return None

    def daily_stop_check(self, now: datetime) -> list[dict]:
        self.roll_day(now)
        if self.s["day"]["flattened"]:
            return []
        if float(self.s["day"]["realized"]) + self.open_pnl() > -float(self.b["daily_stop_usd"]) + 1e-9:
            return []
        self.s["day"]["flattened"] = True
        out = []
        for p in list(self.s["positions"]):
            lp = self.s["last_price"].get(p["symbol"], p["entry"])
            out.append(self.close(p, self._slip(p, lp), now, "daily_stop_1500", {"trigger": "daily_stop"}))
        self.s["pending"] = []
        self._event(now, "daily_stop", realized=self.s["day"]["realized"])
        return out

    def fill_pendings(self, quotes: dict, now: datetime) -> list[dict]:
        keep = []
        for pe in self.s["pending"]:
            q = quotes.get(pe["symbol"])
            tp = pe.get("trigger_price")
            if q is not None and tp is not None and self._quote_problem(q) is None:
                tick = float(self.spec(pe["contract"])["tick"])
                entry = round_adverse(float(q["price"]) + pe["side"] * int(self.b["slippage_ticks"]) * tick, tick,
                                      pe["side"], True)
                worse = -pe["side"] * (float(tp) - entry)
                if worse > float(self.b["max_trigger_adverse_atr"]) * pe["atr"]:
                    self._skip("fill > 0.5 ATR worse than trigger_price")
                    self._event(now, "trigger_chase_skip", symbol=pe["symbol"], fill=entry, trigger_price=tp)
                    continue
            keep.append(pe)
        self.s["pending"] = keep
        return super().fill_pendings(quotes, now)

    async def tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        r = await super().tick(now)
        try:
            if self.s["positions"]:
                closed = self.daily_stop_check(now)
                if closed:
                    await self.persist()
                    r = {**(r or {}), "daily_stop": [c["id"] for c in closed]}
        except Exception:
            log.exception("%s daily stop check failed", self.BOOK_NAME)
        return r


# ---------------------------------------------------------------------------------------------- feed
class EventFeed:
    """One shared M1-close +offset loop: snapshots (shared builder) for the event symbols, M1 history, fan-out to
    every event engine via `await engine.on_feed(now, snaps, store)`. Never raises; weekend = nothing at all."""

    def __init__(self, cfg: dict, engines: list, *, symbols=None, offset_s: float = 10.0,
                 active_utc=("07:45", "20:45"), snapshot=None, store: M1Store | None = None):
        self.cfg, self.engines = cfg, [e for e in engines if e is not None]
        self.symbols = list(symbols or EVENT_SYMBOLS)
        self.offset_s, self.active_utc = float(offset_s), active_utc
        self._snapshot = snapshot or snapshot_at
        self.store = store or M1Store(cfg)
        self.last: dict = {}

    def active(self, now: datetime) -> bool:
        from .market_hours import markets_closed
        return (not markets_closed(now, self.cfg)) and in_window(now, self.active_utc)

    async def capture(self, now: datetime) -> dict:
        t0 = time.monotonic()
        await asyncio.gather(*[self.store.seed(s, now) for s in self.symbols], return_exceptions=True)
        got = await asyncio.gather(*[self._snapshot(s, self.cfg, now) for s in self.symbols], return_exceptions=True)
        snaps = {}
        for s, r in zip(self.symbols, got):
            if isinstance(r, BaseException):
                log.warning("event feed %s snapshot failed: %s", s, r)
                continue
            payload, m1 = r if isinstance(r, (tuple, list)) else (r, None)
            self.store.merge(s, m1)
            fr = self.store.frame(s)
            ctx = payload.setdefault("context", {})
            ctx["forming_m15_volume"] = forming_volume(fr, now)
            ctx["forming_rvol"] = forming_rvol(payload, fr, now)
            snaps[s] = payload
        self.last = {"t": _iso(now), "symbols": sorted(snaps), "ms": round((time.monotonic() - t0) * 1000)}
        return snaps

    async def step(self, now: datetime) -> dict:
        if not self.active(now):
            return {"inactive": True}
        snaps = await self.capture(now)
        out = {}
        for e in self.engines:
            try:
                out[e.NAME] = await e.on_feed(now, snaps, self.store)
            except Exception as ex:
                log.exception("event engine %s failed (other books unaffected)", getattr(e, "NAME", e))
                out[getattr(e, "NAME", "?")] = {"error": f"{type(ex).__name__}: {ex}"}
        return out

    async def run(self, stop: asyncio.Event | None = None) -> None:
        while not (stop and stop.is_set()):
            now = datetime.now(UTC)
            wake = datetime.fromtimestamp((int(now.timestamp()) // 60 + 1) * 60, UTC) + timedelta(seconds=self.offset_s)
            await asyncio.sleep(max(0.0, (wake - datetime.now(UTC)).total_seconds()))
            try:
                await self.step(datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event feed step failed")
