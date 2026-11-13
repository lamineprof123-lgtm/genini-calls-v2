"""EVAL-R4 Lucid eval side book (t288u). PAPER ONLY - nothing here places an order anywhere.

User (t287u-t288u, 13 Nov 2026): "run as a side book whatever you think will pass eval". EVAL-R4 is EVAL-80 plus the
backtested conditional entry rule R4 ("strong H1 trend & not stretched") and the two anti-chasing conditions:

  Signals  NO Gemini call of its own: G-LOW-80's low-thinking gemini-3.8-flash response on the shared M15 close +20 s
           snapshot (same hand-off as EVAL-80). BUY/SELL with confidence >= 0.80 on the Lucid symbols, no BTC.
  Filter   an entry is taken ONLY when every condition holds, computed at signal time from CLOSED M15 bars
           (no lookahead), with the feature formulas of the backtest (/workspace/bb_feat/build.py `feats`):
             side = +1 BUY / -1 SELL, a = the signal's M15 ATR (the ATR the bracket uses), price = signal entry
             h1_ema20_vs_50  = side * (EMA20 - EMA50 of closed H1 closes) / a       >= 2.7   (strong H1 trend, with it)
             m15_px_vs_ema20 = side * (price - EMA20 of M15 closes) / a             <  3.0   (not stretched)
             last3_atr       = side * (last close - open of the 3rd-last bar) / a   <  2.2   (not chasing)
             vwap_dist_atr   = side * (price - UTC-day VWAP of closed bars) / a     <  3.5   (not far from VWAP)
           EMAs: pandas ewm(span, adjust=False) over the full bar history; H1 = last M15 close per clock hour, an hour
           is usable once it has closed; VWAP = typical price x tick volume (0 -> 1e-9), reset 00:00 UTC.
           History: ~1,500 Dukascopy M15 bars seeded at start (same feed as the backtest), then extended every cycle
           with the snapshot's closed bars (or a short Dukascopy refresh when the snapshot is not Dukascopy data).
           Too little history (< min_history_bars, or no closed H1 bar) = the signal fails with "no data".
  Bracket / size / eval rules: identical to EVAL-80 (0.8 ATR stop, 1 ATR target, 90-minute cap, 0.3 ATR fill check,
           ~$450 risk, minis preferred, 2 minis per position, 6 minis / 60 micros total, LucidPro 100K eval rules,
           fresh $100k, no auto-reset, flat 21:45 UTC).
  Logging  every 0.8+ BUY/SELL signal: values + pass/fail + reason per condition (evalr4_signals.jsonl "r4" field,
           and the skip / queued events). Missed signals while G-LOW-80 held the symbol are counted as in EVAL-80.
State: state/evalr4/account.json. Logs: logs/<day>/evalr4_signals.jsonl, evalr4_trades.jsonl, evalr4_events.jsonl.
Site: evalr4.json at the data root; panel.json lists it as its own Lucid-only book with an "eval" block.
"""
from __future__ import annotations
import asyncio, logging, time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .bracket import skip_key
from .eval80 import DEFAULTS as EVAL80_DEFAULTS, Eval80Book

log = logging.getLogger("signals.evalr4")
UTC = timezone.utc
BOOK = "EVAL-R4"
LABEL = "Gemini flash · EVAL-R4 (G-LOW-80's call, min 0.80, R4 trend filter, 0.8/1.0 ATR, Lucid eval rules)"
COND_KEYS = ("h1_trend", "m15_ema20", "chase", "vwap")

DEFAULTS = {
    **EVAL80_DEFAULTS,
    "book": BOOK,
    "reset_token": "evalr4-v1",
    "h1_trend_min_atr": 2.7,          # backtest rule R4 used 2.67 (shown rounded as 2.7); 2.7 per the brief
    "max_px_vs_ema20_atr": 3.0,
    "max_last3_atr": 2.2,
    "max_vwap_dist_atr": 3.5,
    "history_bars": 1500,             # ~15 trading days of M15 (EMA50 on H1 fully warmed up)
    "min_history_bars": 600,
    "seed_timeout_s": 180,
    "seed_retry_s": 600,
}


def evalr4_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **{k: v for k, v in (cfg.get("evalr4") or {}).items() if k != "contracts"}}


# ---------------------------------------------------------------------------------------------- features
def _frame(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Backtest indicator columns (build.py) on a closed-bar M15 frame indexed by bar-open UTC."""
    d = df.sort_index()
    d = d[~d.index.duplicated(keep="last")].copy()
    h, l, c = d.high, d.low, d.close
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    day = d.index.floor("D")
    tp = (h + l + c) / 3
    v = d.volume.replace(0, np.nan).fillna(1e-9)
    d["vwap"] = (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum()
    hc = c.resample("1h", label="left", closed="left").last().dropna()
    H1 = pd.DataFrame({"c": hc})
    H1["e20"] = hc.ewm(span=20, adjust=False).mean()
    H1["e50"] = hc.ewm(span=50, adjust=False).mean()
    H1.index = H1.index + pd.Timedelta(hours=1)   # available at the hour's close
    return d, H1


def compute_r4(df: pd.DataFrame | None, side: int, price: float, atr: float, t: datetime, b: dict) -> dict:
    """R4 filter values + pass/fail per condition. Only bars CLOSED at `t` are used (no lookahead)."""
    out = {"pass": False, "conds": {}, "reason": None}
    if df is None or not len(df):
        out["reason"] = "no data: no M15 history"
        return out
    ts = pd.Timestamp(t)
    df = df[df.index + pd.Timedelta(minutes=15) <= ts]
    if len(df) < max(60, int(b["min_history_bars"])):
        out["reason"] = f"no data: {len(df)} closed M15 bars < {max(60, int(b['min_history_bars']))}"
        out["bars"] = len(df)
        return out
    if not atr or atr <= 0 or not price:
        out["reason"] = "no data: no ATR / price"
        return out
    d, H1 = _frame(df)
    H1 = H1[H1.index <= ts]
    if H1.empty:
        out["reason"] = "no data: no closed H1 bar"
        return out
    bar, hh, a = d.iloc[-1], H1.iloc[-1], float(atr)
    vals = {
        "h1_ema20_vs_50": side * (float(hh.e20) - float(hh.e50)) / a,
        "m15_px_vs_ema20": side * (price - float(bar.ema20)) / a,
        "last3_atr": side * (float(bar.close) - float(d.open.iloc[-3])) / a,
        "vwap_dist_atr": side * (price - float(bar.vwap)) / a,
    }
    rules = {
        "h1_trend": ("h1_ema20_vs_50", ">=", float(b["h1_trend_min_atr"])),
        "m15_ema20": ("m15_px_vs_ema20", "<", float(b["max_px_vs_ema20_atr"])),
        "chase": ("last3_atr", "<", float(b["max_last3_atr"])),
        "vwap": ("vwap_dist_atr", "<", float(b["max_vwap_dist_atr"])),
    }
    labels = {"h1_trend": "H1 EMA20-EMA50 with the trade", "m15_ema20": "price vs M15 EMA20",
              "chase": "last 3 bars move", "vwap": "distance from VWAP"}
    fails = []
    for k, (f, op, th) in rules.items():
        x = vals[f]
        ok = x >= th - 1e-12 if op == ">=" else x < th
        why = None if ok else f"{labels[k]} {x:+.2f} ATR {'<' if op == '>=' else '>='} {th:g}"
        out["conds"][k] = {"feature": f, "value": round(x, 3), "op": op, "threshold": th, "pass": bool(ok),
                           "reason": why}
        if not ok:
            fails.append(why)
    out.update({"pass": not fails, "reason": "; ".join(fails) or None, "bars": len(d),
                "last_bar": d.index[-1].isoformat(), "h1_bar_close": H1.index[-1].isoformat(),
                "atr_used": round(a, 6), "atr_m15_hist": None if pd.isna(bar.atr) else round(float(bar.atr), 6),
                "ema20": round(float(bar.ema20), 6), "vwap": round(float(bar.vwap), 6),
                "h1_ema20": round(float(hh.e20), 6), "h1_ema50": round(float(hh.e50), 6), "price": price,
                "abs_vwap_atr": round(abs(price - float(bar.vwap)) / a, 3)})
    return out


# ---------------------------------------------------------------------------------------------- bar history
def payload_frame(p: dict) -> pd.DataFrame | None:
    """Closed M15 bars from an M15 snapshot payload (candles + tick volume)."""
    cs = p.get("candles") or []
    if not cs:
        return None
    vol = {v.get("time"): v.get("value") for v in (p.get("volume") or [])}
    df = pd.DataFrame([{"t": c["time"], "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"],
                        "volume": float(vol.get(c["time"]) or 0.0)} for c in cs])
    df.index = pd.to_datetime(df.pop("t"), unit="s", utc=True)
    return df.astype(float)


class BarHistory:
    """In-memory Dukascopy M15 history per symbol (seeded once, extended from each snapshot). Never raises."""

    def __init__(self, cfg: dict, symbols: list[str], b: dict, fetch=None):
        self.cfg, self.symbols, self.b = cfg, list(symbols), b
        self.n = int(b["history_bars"])
        self.df: dict[str, pd.DataFrame] = {}
        self.source: dict[str, str] = {}
        self._fetch = fetch or self._duka
        self._last_try: dict[str, float] = {}
        self.seed_task: asyncio.Task | None = None

    def ticker(self, sym: str) -> str:
        return ((self.cfg.get("symbols") or {}).get(sym) or {}).get("dukascopy") or sym.lower()

    async def _duka(self, sym: str, n: int, since: pd.Timestamp | None = None) -> pd.DataFrame:
        from .data_providers import DukascopyProvider
        return await DukascopyProvider(timeout=float(self.b["seed_timeout_s"])).fetch(self.ticker(sym), 15, n,
                                                                                       since=since)

    def _merge(self, sym: str, new: pd.DataFrame | None) -> None:
        if new is None or not len(new):
            return
        new = new[["open", "high", "low", "close", "volume"]].astype(float)
        old = self.df.get(sym)
        df = new if old is None else pd.concat([old, new])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        self.df[sym] = df.tail(self.n)

    async def seed(self, sym: str, force: bool = False) -> bool:
        if not force and time.monotonic() - self._last_try.get(sym, -1e9) < float(self.b["seed_retry_s"]):
            return False
        self._last_try[sym] = time.monotonic()
        try:
            df = await self._fetch(sym, self.n)
            self._merge(sym, df)
            self.source[sym] = "dukascopy-seed"
            log.info("EVAL-R4 history %s: %d bars (%s .. %s)", sym, len(self.df[sym]), self.df[sym].index[0],
                     self.df[sym].index[-1])
            return True
        except Exception as e:
            log.warning("EVAL-R4 history seed %s failed: %s", sym, e)
            return False

    async def seed_all(self) -> None:
        await asyncio.gather(*[self.seed(s, force=True) for s in self.symbols], return_exceptions=True)

    def start(self) -> None:
        if self.seed_task is None:
            self.seed_task = asyncio.create_task(self.seed_all(), name="evalr4-history")

    def enough(self, sym: str) -> bool:
        return sym in self.df and len(self.df[sym]) >= int(self.b["min_history_bars"])

    async def update(self, snap, now: datetime) -> dict:
        """Extend each symbol's history with the snapshot's closed bars. Returns {sym: note}."""
        notes = {}
        if self.seed_task is not None and not self.seed_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.seed_task), float(self.b["seed_timeout_s"]))
            except Exception:
                pass
        pays = {p.get("symbol"): p for p in (getattr(snap, "payloads", None) or [])}
        for sym in self.symbols:
            try:
                if not self.enough(sym):
                    await self.seed(sym)
                p = pays.get(sym) or {}
                pf = payload_frame(p) if p.get("source") == "dukascopy" else None
                old = self.df.get(sym)
                if pf is not None and len(pf):
                    pf = pf[pf.index + pd.Timedelta(minutes=15) <= pd.Timestamp(now)]
                    if old is not None and len(old) and pf.index[0] > old.index[-1] + pd.Timedelta(minutes=15):
                        # no overlap with the history: short Dukascopy refresh first
                        self._merge(sym, await self._fetch(sym, 64, old.index[-8]))
                    self._merge(sym, pf)
                    notes[sym] = "snapshot"
                elif old is not None and len(old):
                    self._merge(sym, await self._fetch(sym, 64, old.index[-8]))
                    notes[sym] = "dukascopy-refresh"
                else:
                    notes[sym] = "none"
            except Exception as e:
                notes[sym] = f"error: {type(e).__name__}: {e}"[:120]
                log.warning("EVAL-R4 history update %s failed: %s", sym, e)
        return notes


# ---------------------------------------------------------------------------------------------- book
class EvalR4Book(Eval80Book):
    BOOK_NAME = BOOK
    BOOK_LABEL = LABEL
    STATE_SUBDIR = "evalr4"
    TRADES_LOG = "evalr4_trades.jsonl"
    EVENTS_LOG = "evalr4_events.jsonl"
    SITE_FILE = "evalr4.json"
    SIGNALS_LOG = "evalr4_signals.jsonl"

    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *, bars_fetch=None, **kw):
        super().__init__(cfg, io_lock, **kw)
        self.hist = BarHistory(cfg, self.b["symbols"], self.b, fetch=bars_fetch)
        self.s.setdefault("r4", self._fresh_r4())
        self._filters: dict[str, dict] = {}
        self._now: datetime | None = None

    def load_cfg(self, cfg: dict) -> dict:
        return evalr4_cfg(cfg)

    @staticmethod
    def _fresh_r4() -> dict:
        return {"seen": 0, "passed": 0, "filtered_out": 0, "no_data": 0, "fail_by": {k: 0 for k in COND_KEYS},
                "last": {}}

    def fresh(self) -> dict:
        s = super().fresh()
        s["r4"] = self._fresh_r4()
        return s

    # ------------------------------------------------------------------ filter
    def r4_for(self, sig: dict, now: datetime) -> dict:
        sym = str(sig.get("symbol") or "").upper()
        side = 1 if sig.get("action") == "BUY" else -1
        price = sig.get("entry") or sig.get("live_price")
        try:
            r = compute_r4(self.hist.df.get(sym), side, float(price or 0), float(sig.get("atr") or 0), now, self.b)
        except Exception as e:
            log.exception("EVAL-R4 filter failed for %s", sym)
            r = {"pass": False, "conds": {}, "reason": f"no data: {type(e).__name__}: {e}"[:160]}
        r["history_source"] = self.hist.source.get(sym)
        return r

    def _count(self, sym: str, r: dict, now: datetime) -> None:
        st = self.s.setdefault("r4", self._fresh_r4())
        st["seen"] += 1
        if r["pass"]:
            st["passed"] += 1
        else:
            st["filtered_out"] += 1
            if not r.get("conds"):
                st["no_data"] += 1
            for k, c in (r.get("conds") or {}).items():
                if not c["pass"]:
                    st["fail_by"][k] = st["fail_by"].get(k, 0) + 1
        st["last"][sym] = {"t": now.astimezone(UTC).isoformat(), "pass": r["pass"], "reason": r.get("reason"),
                           **{k: (r.get("conds") or {}).get(k, {}).get("value") for k in COND_KEYS}}

    def _signal_extra(self, v: dict) -> dict:
        r = self._filters.get(str(v.get("symbol") or "").upper())
        return {"r4": r} if r is not None and v.get("action") in ("BUY", "SELL") else {}

    def intake(self, record: dict | None, now: datetime) -> list[dict]:
        """EVAL-80's intake + the R4 entry filter (computed and logged for every 0.8+ BUY/SELL signal)."""
        out = []
        if not record:
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
            r4 = None
            try:
                conf_ok = float(sig.get("confidence")) >= float(self.b["min_confidence"]) - 1e-9
            except (TypeError, ValueError):
                conf_ok = False
            if conf_ok and sym in self.b["symbols"]:
                r4 = self.r4_for(sig, now)
                self._filters[sym] = r4
                self._count(sym, r4, now)
            why = self.eligible(sig)
            if why is None and self.held(sym):
                why = "already in a position on this symbol (one per symbol)"
            if why is None and not self.entries_allowed(now):
                why = "outside session / last minutes before the 21:45 UTC flat"
            if why is None and r4 is not None and not r4["pass"]:
                why = f"R4 filter: {r4.get('reason')}"
            c = q = None
            if why is None:
                c, q, why = self.size_for(sym, float(sig["atr"]))
            ev = {"symbol": sym, "action": sig.get("action"), "confidence": sig.get("confidence"),
                  "atr": sig.get("atr"), "signal_entry": sig.get("entry"), "cycle": key, "session": sig.get("session")}
            if r4 is not None:
                ev["r4"] = {"pass": r4["pass"], "reason": r4.get("reason"),
                            **{k: (r4.get("conds") or {}).get(k, {}).get("value") for k in COND_KEYS}}
            if why is not None:
                self._skip("r4_filter" if why.startswith("R4 filter") else skip_key(why))
                self._event(now, "skip", reason=why, **ev)
                continue
            pe = {"symbol": sym, "side": 1 if sig["action"] == "BUY" else -1, "contract": c, "qty": q,
                  "micro": self.is_micro(c), "atr": float(sig["atr"]), "confidence": float(sig["confidence"]),
                  "signal_entry": sig.get("entry"), "signal_at": key or now.isoformat(),
                  "queued_at": now.isoformat(), "level_event": sig.get("level_event"),
                  "session": sig.get("session"), "rationale": (sig.get("rationale") or "")[:300],
                  "r4": ev.get("r4")}
            self.s["pending"].append(pe)
            self._event(now, "queued", **ev, contract=c, qty=q)
            out.append(pe)
        return out

    async def on_shared(self, g: dict | None, snap=None, now: datetime | None = None) -> dict:
        now = now or getattr(snap, "now", None) or datetime.now(UTC)
        self._filters = {}
        notes = {}
        try:
            notes = await self.hist.update(snap, now)
        except Exception:
            log.exception("EVAL-R4 history update failed")
        out = await super().on_shared(g, snap, now)
        out["history"] = {s: {"bars": len(self.hist.df[s]) if s in self.hist.df else 0, "update": notes.get(s)}
                          for s in self.b["symbols"]}
        out["r4"] = {s: {"pass": r["pass"], "reason": r.get("reason"),
                         **{k: (r.get("conds") or {}).get(k, {}).get("value") for k in COND_KEYS}}
                     for s, r in self._filters.items()}
        return out

    # ------------------------------------------------------------------ views
    def r4_view(self) -> dict:
        st = self.s.setdefault("r4", self._fresh_r4())
        b = self.b
        return {"seen": st["seen"], "passed": st["passed"], "filtered_out": st["filtered_out"],
                "no_data": st.get("no_data", 0), "fail_by": dict(st["fail_by"]), "last": st.get("last") or {},
                "rules": {"h1_trend_min_atr": b["h1_trend_min_atr"], "max_px_vs_ema20_atr": b["max_px_vs_ema20_atr"],
                          "max_last3_atr": b["max_last3_atr"], "max_vwap_dist_atr": b["max_vwap_dist_atr"]}}

    def eval_view(self, now: datetime | None = None) -> dict:
        v = super().eval_view(now)
        v["filter"] = self.r4_view()
        return v

    def site_view(self) -> dict:
        v = super().site_view()
        b = self.b
        v["r4"] = self.r4_view()
        v["rules"] = ("No own model call: G-LOW-80's low-thinking gemini-3.8-flash response on the shared M15 snapshot. "
                      f"BUY/SELL with confidence >= {b['min_confidence']:g} on {', '.join(b['symbols'])} (no BTC), "
                      f"entered ONLY when: H1 EMA20-EMA50 >= {b['h1_trend_min_atr']:g} ATR in the trade direction, "
                      f"price < {b['max_px_vs_ema20_atr']:g} ATR past the M15 EMA20, last 3 M15 bars moved < "
                      f"{b['max_last3_atr']:g} ATR with the trade, price < {b['max_vwap_dist_atr']:g} ATR past the "
                      f"UTC-day VWAP (closed bars only). Stop {b['stop_atr']:g} ATR, target {b['target_atr']:g} ATR, "
                      f"{b['max_hold_minutes']} min cap, 0.3 ATR fill check, ~${b['risk_usd']:g} risk (minis if one "
                      f"fits, else micros; {b['max_minis_per_position']} minis per position, {b['max_minis']} minis / "
                      f"{int(b['max_minis']) * int(b['micros_per_mini'])} micros total). LucidPro 100K eval: DLL "
                      f"${b['dll']:,.0f}, ${b['mll']:,.0f} EOD trailing MLL (locks at ${b['locked_mll_balance']:,.0f}), "
                      f"pass at +${b['profit_target']:,.0f}, no auto-reset. Flat {b['flat_utc']} UTC.")
        v["limitation"] = v["limitation"].replace("EVAL-80", BOOK)
        return v
