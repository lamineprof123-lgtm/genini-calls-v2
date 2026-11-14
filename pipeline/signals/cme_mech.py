"""CME mechanical books (Lucid 50K, t340u). PAPER ONLY. No model call of any kind.

Six setups picked by Grok Bot's blow-OK backtest (/workspace/orb_vote/orb_vote_optimize_blowok.py, sheet 'best'
of orb_vote_optimized_blowok.xlsx) are traded here exactly as the reference code does, on CME 1-minute bars
(Yahoo Finance GC=F / NQ=F / ES=F front-month futures, the same contracts the NinjaTrader backtest bars came from;
traded as micros MGC / MNQ / MES). Times are New York.

Shared mechanics (orb_vote_mixed.market_days + orb_vote_optimize.day_arrays / outcome):
  session date = (ts_ny + 6 h).date (18:00 Globex open belongs to the next date); sessions < 300 bars are skipped.
  O = 08:20 or 09:30; D = O + 30 min; P = close of the D bar; entry = open of the first bar after D + 1 tick.
  15-min range = bars O..O+14; 30-min range = O..O+29 (needs >= 10 / >= 20 bars, else no trade).
  Daily table per completed session: close = last bar <= 13:29 (GC) / 15:59 (NQ, ES); RTH high/low 08:20-13:29
  (GC) / 09:30-15:59; SMA20 of closes; ATR14 = 14-session mean of true range (prior close). Today uses the
  PREVIOUS session's row (no look-ahead).
  trend = prior close vs SMA20; gap = O open vs prior close; ATR gate = 15-min range <= 0.25 x ATR14.
  vote >= 60%: 10 voters (orb15, orb30, retest, fade, ema, vwap, gap, trend, preopen, r0830), >= 5 active and
  >= 60 % on one side.
  Exits on every 1-min bar from the entry bar through 16:40: stop first if stop and target in one bar; stop fills
  stop - 1 tick, target at the limit; else flat at the close of the last bar <= 16:40 - 1 tick. Minimum stop
  2 ticks. Costs $1.50 round trip per micro. A trade whose intraday loss (incl. commission) reaches -$1,200 is
  flattened at exactly -$1,200 (Lucid DLL ends the day). GOLD-COMBO: both legs on one account, the -$1,200 cap
  applies to the day's summed P&L / worst excursion.

Lucid 50K eval tracker per book (reel_bt.backtest.run_path): start $50,000, pass at $53,000, max loss $2,000
trailing on end-of-day balance, floor locks at $50,100 once the balance reaches $52,100, DLL $1,200. A blow
(the day's worst excursion or close touching the floor) or a pass ends the attempt; the next session starts a
fresh $50,000 eval (blown evals allowed, counted). The book's own account is ONE continuous $50,000 account that
never resets (like the backtest's profit / month).
"""
from __future__ import annotations
import asyncio, json, logging, math, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

log = logging.getLogger("signals.cme_mech")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
MODEL = "mechanical"
COMM = 1.50            # $ per round trip per micro
DLL = 1200.0
FLAT = 1640
CAP = 1644
SEED_FILE = Path(__file__).resolve().parent / "data" / "cme_mech_seed.json"

# market -> micro specs + data
MK = {
    "GC": {"pv": 10.0, "tick": 0.10, "contract": "MGC", "yahoo": "GC=F", "close_hm": 1329, "rth": (820, 1329)},
    "NQ": {"pv": 2.0, "tick": 0.25, "contract": "MNQ", "yahoo": "NQ=F", "close_hm": 1559, "rth": (930, 1559)},
    "ES": {"pv": 5.0, "tick": 0.25, "contract": "MES", "yahoo": "ES=F", "close_hm": 1559, "rth": (930, 1559)},
}
CONTRACTS = {MK[m]["contract"]: {"point_value": MK[m]["pv"], "tick": MK[m]["tick"], "commission": COMM / 2,
                                 "micro": True, "mini": m} for m in MK}

# leg: O, signal, ATR gate, stop (kind, value), target R, micros
_G0820 = {"O": 820, "sig": "trend", "gate": True, "stop": ("pts", 15.0), "tp": 1.5}
_G0930 = {"O": 930, "sig": "trend", "gate": False, "stop": ("atr", 0.1), "tp": 1.0}
BOOKS = {
    "GOLD-COMBO": {"dir": "cme_gold_combo", "m": "GC", "legs": [{**_G0820, "leg": "08:20", "n": 3},
                                                              {**_G0930, "leg": "09:30", "n": 4}],
                   "label": "Mechanical · Gold 08:20 + 09:30 daily trend, one account (3 MGC 15 pt / 1.5R ATR-gated "
                            "+ 4 MGC 0.1x ATR14 / 1R), Lucid 50K",
                   "backtest": {"win_pct": 57.6, "per_month": 3607, "blow_pct": 4.5, "med_days": 16, "pass_pct": 87,
                                "payout_pct": 72, "net_per_eval": 6572}},
    "GOLD-0820-TREND": {"dir": "cme_gold_0820", "m": "GC", "legs": [{**_G0820, "leg": "08:20", "n": 4}],
                        "label": "Mechanical · Gold 08:20 daily trend, ATR gate · 15 pt stop · 1.5R · 4 MGC · Lucid 50K",
                        "backtest": {"win_pct": 57.9, "per_month": 3216, "blow_pct": 12, "med_days": 14, "pass_pct": 72,
                                     "payout_pct": 61, "net_per_eval": 4531}},
    "GOLD-0930-TREND": {"dir": "cme_gold_0930", "m": "GC", "legs": [{**_G0930, "leg": "09:30", "n": 15}],
                        "label": "Mechanical · Gold 09:30 daily trend · 0.1x ATR14 stop · 1R · 15 MGC · Lucid 50K",
                        "backtest": {"win_pct": 61.9, "per_month": 4399, "blow_pct": 50, "med_days": 8, "pass_pct": 47,
                                     "payout_pct": 41, "net_per_eval": 3825}},
    "NQ-0930-GAP": {"dir": "cme_nq_gap", "m": "NQ",
                    "legs": [{"O": 930, "sig": "gap", "gate": False, "stop": ("atr", 1.5), "tp": 2.0, "leg": "09:30", "n": 1}],
                    "label": "Mechanical · NQ 09:30 gap direction · 1.5x ATR14 stop · 2R · 1 MNQ · Lucid 50K",
                    "backtest": {"win_pct": 60.0, "per_month": 2550, "blow_pct": 0, "med_days": 25.5, "pass_pct": 92,
                                 "payout_pct": 71, "net_per_eval": 3655}},
    "GOLD-0820-VOTE60": {"dir": "cme_gold_vote60", "m": "GC",
                         "legs": [{"O": 820, "sig": "vote60", "gate": False, "stop": ("pts", 15.0), "tp": 1.0, "leg": "08:20", "n": 6}],
                         "label": "Mechanical · Gold 08:20 vote ≥60% (10 voters) · 15 pt stop · 1R · 6 MGC · Lucid 50K",
                         "backtest": {"win_pct": 61.4, "per_month": 3035, "blow_pct": 56, "med_days": 6, "pass_pct": 37,
                                      "payout_pct": 34, "net_per_eval": 2871}},
    "ES-0930-GAP": {"dir": "cme_es_gap", "m": "ES",
                    "legs": [{"O": 930, "sig": "gap", "gate": False, "stop": ("pts", 10.0), "tp": 3.0, "leg": "09:30", "n": 8}],
                    "label": "Mechanical · ES 09:30 gap direction · 10 pt stop · 3R · 8 MES · Lucid 50K",
                    "backtest": {"win_pct": 36.6, "per_month": 2551, "blow_pct": 33, "med_days": 14, "pass_pct": 65,
                                 "payout_pct": 59, "net_per_eval": 2687}},
}
BOOK_ORDER = list(BOOKS)
SITE_FILE = "cme_mech.json"

# LucidPro 50K (Lucid help centre "LucidPro Evaluation Account", checked 14 Nov 2026)
L50 = {"start": 50_000.0, "profit_target": 3_000.0, "mll": 2_000.0, "dll": 1_200.0, "lock_trigger": 52_100.0,
       "locked_floor": 50_100.0, "max_micros": 40, "flat_et": "16:45", "fee": 120.40}


def hm_add(hm: int, mins: int) -> int:
    t = (hm // 100) * 60 + hm % 100 + mins
    return (t // 60) * 100 + t % 60


# ---------------------------------------------------------------- bars
def prep_bars(df: pd.DataFrame) -> pd.DataFrame:
    """df: columns ts_ny (tz-aware NY), open/high/low/close/volume. Adds date (session) and hm."""
    df = df.dropna(subset=["ts_ny"]).drop_duplicates("ts_ny").sort_values("ts_ny").reset_index(drop=True)
    df["date"] = (df.ts_ny + pd.Timedelta(hours=6)).dt.date
    df["hm"] = df.ts_ny.dt.hour * 100 + df.ts_ny.dt.minute
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = df["volume"].fillna(0).astype(float)
    return df


def daily_row(g: pd.DataFrame, m: str) -> dict:
    """Completed-session row exactly as orb_vote_mixed.market_days builds `daily`."""
    c = g[g.hm <= MK[m]["close_hm"]]
    a, b = MK[m]["rth"]
    rth = g[(g.hm >= a) & (g.hm <= b)]
    if len(c) == 0 or len(rth) == 0:
        return {"close": None, "hi": None, "lo": None, "n": int(len(g))}
    return {"close": float(c.close.iloc[-1]), "hi": float(rth.high.max()), "lo": float(rth.low.min()), "n": int(len(g))}


def daily_stats(table: dict[str, dict]) -> pd.DataFrame:
    """table: session date -> {close, hi, lo, n}. Sessions with < 300 bars are dropped (as the reference)."""
    rows = [{"date": d, **v} for d, v in sorted(table.items()) if int(v.get("n") or 0) >= 300]
    dl = pd.DataFrame(rows, columns=["date", "close", "hi", "lo", "n"])
    for k in ("close", "hi", "lo"):
        dl[k] = pd.to_numeric(dl[k], errors="coerce").astype(float)
    dl["sma20"] = dl.close.rolling(20, min_periods=20).mean()
    pc = dl.close.shift(1)
    tr = pd.concat([dl.hi - dl.lo, (dl.hi - pc).abs(), (dl.lo - pc).abs()], axis=1).max(axis=1)
    dl["atr14"] = tr.rolling(14, min_periods=14).mean()
    return dl


def prev_stats(dl: pd.DataFrame, day: str) -> dict | None:
    """The previous qualifying session's close / SMA20 / ATR14 for session `day`."""
    p = dl[dl.date < day]
    if not len(p):
        return None
    r = p.iloc[-1]
    f = lambda x: None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
    return {"date": r.date, "close": f(r.close), "sma20": f(r.sma20), "atr14": f(r.atr14)}


def ema5(df: pd.DataFrame, O: int) -> pd.Series:
    s = df.set_index("ts_ny")
    o5 = s[["close"]].resample("5min", label="left", closed="left", offset=f"{(O % 100) % 5}min").agg(
        {"close": "last"}).dropna()
    return pd.DataFrame({"close": o5.close, "ema": o5.close.ewm(span=20, adjust=False).mean()})


def votes_for(g: pd.DataFrame, O: int, m: str, prev: dict | None, e5: pd.DataFrame | None) -> dict | None:
    """Port of orb_vote_mixed.market_days for one session. g = the session's bars (at least through D)."""
    if prev is None:
        return None
    D = hm_add(O, 30); E15 = hm_add(O, 15)
    bars = g.drop_duplicates("hm").set_index("hm")
    if O not in bars.index or D not in bars.index:
        return {"skip": "no O or D bar"}
    r15 = g[(g.hm >= O) & (g.hm < E15)]; r30 = g[(g.hm >= O) & (g.hm < D)]
    if len(r15) < 10 or len(r30) < 20:
        return {"skip": "thin opening range"}
    P = float(bars.loc[D, "close"]); o_open = float(bars.loc[O, "open"])
    h15, l15 = float(r15.high.max()), float(r15.low.min()); h30, l30 = float(r30.high.max()), float(r30.low.min())
    rng15 = h15 - l15
    v = {}
    v["orb15"] = 1 if P > h15 else (-1 if P < l15 else 0)
    v["orb30"] = 1 if P > h30 else (-1 if P < l30 else 0)
    post = g[(g.hm >= E15) & (g.hm <= D)]
    up = post.high.gt(h15).values; dn = post.low.lt(l15).values
    tol = 0.10 * rng15
    rt = 0
    if up.any() and not dn.any():
        k = int(np.argmax(up)); after = post.iloc[k + 1:]
        if len(after) and (after.low <= h15 + tol).any() and (after.close >= h15).all() and P > h15:
            rt = 1
    if dn.any() and not up.any():
        k = int(np.argmax(dn)); after = post.iloc[k + 1:]
        if len(after) and (after.high >= l15 - tol).any() and (after.close <= l15).all() and P < l15:
            rt = -1
    v["retest"] = rt
    v["fade"] = (-1 if (up.any() and not dn.any() and l15 <= P <= h15) else
                 (1 if (dn.any() and not up.any() and l15 <= P <= h15) else 0))
    d = g.date.iloc[0]
    t5 = pd.Timestamp(f"{d} {O // 100:02d}:{O % 100:02d}", tz="America/New_York")
    if e5 is not None and t5 in e5.index:
        c5 = e5.close.loc[t5]; ev = e5.ema.loc[t5]
        v["ema"] = 1 if c5 > ev else (-1 if c5 < ev else 0)
    else:
        v["ema"] = 0
    seg = g[(g.hm >= O) & (g.hm <= D)]
    tp = (seg.high + seg.low + seg.close) / 3
    w = seg.volume.where(seg.volume > 0, 0)
    vw = float((tp * w).sum() / w.sum()) if w.sum() > 0 else float(tp.mean())
    v["vwap"] = 1 if P > vw else (-1 if P < vw else 0)
    pc, sma, atr = prev.get("close"), prev.get("sma20"), prev.get("atr14")
    v["gap"] = 0 if pc is None else (1 if o_open > pc else (-1 if o_open < pc else 0))
    v["trend"] = 0 if (sma is None or pc is None) else (1 if pc > sma else -1)
    if m in ("NQ", "ES"):
        lo = g[g.hm == 300]
        ref = float(lo.open.iloc[0]) if len(lo) else float("nan")
    else:
        ref = float(g.open.iloc[0]) if g.hm.iloc[0] >= 1800 else float("nan")
    v["preopen"] = 0 if math.isnan(ref) else (1 if o_open > ref else (-1 if o_open < ref else 0))
    r8 = g[(g.hm >= 830) & (g.hm < 845)]
    if len(r8) >= 10 and D >= 845:
        v["r0830"] = 1 if P > r8.high.max() else (-1 if P < r8.low.min() else 0)
    else:
        v["r0830"] = 0
    gate = bool(atr is not None and rng15 <= 0.25 * atr)
    return {"votes": v, "gate": gate, "rng15": rng15, "atr": atr, "P": P, "o_open": o_open,
            "prev_close": pc, "sma20": sma}


def vote60(v: dict) -> int:
    a = [x for x in v.values() if x != 0]
    if len(a) < 5:
        return 0
    L = sum(1 for x in a if x == 1) / len(a)
    return 1 if L >= 0.6 else (-1 if (1 - L) >= 0.6 else 0)


def side_of(leg: dict, info: dict) -> int:
    v = info["votes"]
    return vote60(v) if leg["sig"] == "vote60" else int(v[leg["sig"]])


def stop_pts(leg: dict, info: dict, tick: float) -> float | None:
    kind, x = leg["stop"]
    s = x if kind == "pts" else (x * info["atr"] if info.get("atr") is not None else None)
    if not s or (isinstance(s, float) and math.isnan(s)) or s <= 0:
        return None
    return max(float(s), 2 * tick)


def decide(leg: dict, info: dict | None, m: str) -> dict:
    """{'side', 'stop_pts', 'tgt_pts'} or {'skip': reason}."""
    if info is None:
        return {"skip": "no prior-session stats"}
    if info.get("skip"):
        return {"skip": info["skip"]}
    s = side_of(leg, info)
    if not s:
        return {"skip": "no signal"}
    if leg["gate"] and not info["gate"]:
        return {"skip": "ATR gate (15-min range > 0.25 x ATR14)"}
    sp = stop_pts(leg, info, MK[m]["tick"])
    if sp is None:
        return {"skip": "no stop (ATR14 not available)"}
    return {"side": s, "stop_pts": sp, "tgt_pts": float(leg["tp"]) * sp}


def walk(path: pd.DataFrame, side: int, stop: float, tgt: float, tick: float, n: int, pv: float,
         final: bool) -> dict:
    """Port of orb_vote_optimize.day_arrays + outcome on bars hm > D (path), with the blow-OK DLL cap.
    Returns status 'open' (no exit yet) or 'closed' with pts (per contract, after slippage), mae (pts), reason,
    exit price and exit bar time. mae$ <= -$1,200 => net -$1,200 (dll_1200)."""
    p = path[path.hm <= FLAT]
    if not len(p):
        return {"status": "pending"}
    e = float(p.open.iloc[0]) + side * tick
    worst = 0.0
    for i, b in enumerate(p.itertuples()):
        fav = (b.high - e) if side == 1 else (e - b.low)
        adv = (b.low - e) if side == 1 else (e - b.high)
        if adv <= -stop:
            return _closed(e, side, -stop - tick, -stop, "stop", e - side * (stop + tick), b.ts_ny, n, pv, i)
        worst = min(worst, adv)
        if fav >= tgt:
            return _closed(e, side, tgt, worst, "target", e + side * tgt, b.ts_ny, n, pv, i)
        if n * (worst * pv) - n * COMM <= -DLL:
            return _closed(e, side, None, worst, "dll_1200", None, b.ts_ny, n, pv, i)
    last = p.iloc[-1]
    if final or int(last.hm) >= FLAT:
        px = float(last.close)
        return _closed(e, side, side * (px - e) - tick, worst, "eod_1640", px - side * tick, last.ts_ny, n, pv, len(p) - 1)
    return {"status": "open", "entry": e, "mae": worst, "mark": float(last.close), "mark_ts": last.ts_ny}


def _closed(e, side, pts, mae, reason, px, ts, n, pv, i) -> dict:
    mae_usd = n * (mae * pv) - n * COMM
    if mae_usd <= -DLL:
        net = -DLL
        reason = "dll_1200"
        px = e + side * ((-DLL / n + COMM) / pv)
    else:
        net = n * (pts * pv - COMM)
    return {"status": "closed", "entry": e, "pts": pts, "mae": mae, "reason": reason, "exit": px,
            "t_exit": ts, "net": round(net, 2), "gross": round(net + n * COMM, 2), "mae_usd": round(min(mae_usd, net, 0.0), 2),
            "bar_i": i}


def day_net_mae(results: list[dict]) -> tuple[float, float]:
    """Combined day (one account): summed net / worst excursion, capped at -$1,200 (blow-OK combo rule)."""
    pnl = sum(r["net"] for r in results); mae = sum(r["mae_usd"] for r in results)
    if mae <= -DLL:
        pnl, mae = -DLL, -DLL
    pnl = max(pnl, -DLL)
    return pnl, min(mae, pnl, 0.0)


# ---------------------------------------------------------------- Lucid 50K eval tracker (run_path, sequential)
def fresh_eval(start_day: str | None = None) -> dict:
    return {"balance": L50["start"], "peak": L50["start"], "locked": False, "days": 0, "start_day": start_day}


def eval_floor(ev: dict) -> float:
    return L50["locked_floor"] if ev["locked"] else ev["peak"] - L50["mll"]


def eval_day(st: dict, day: str, pnl: float, mae: float) -> str | None:
    """Apply one session (pnl, mae) to the current attempt. Returns 'pass' / 'blow' / None.
    st: {'eval': {...}, 'passes', 'blows', 'attempts': [...]}"""
    ev = st["eval"]
    ev["days"] += 1
    floor = eval_floor(ev)
    mae_eff = max(mae, -L50["dll"]); pnl_eff = max(pnl, -L50["dll"]) if pnl < 0 else pnl
    if ev["balance"] + mae_eff <= floor + 1e-9 or ev["balance"] + pnl_eff <= floor + 1e-9:
        st["blows"] += 1
        st["attempts"].append({"result": "blow", "day": day, "days": ev["days"], "balance": round(ev["balance"] + pnl_eff, 2),
                               "floor": round(floor, 2), "start_day": ev.get("start_day")})
        st["eval"] = fresh_eval()
        return "blow"
    ev["balance"] = round(ev["balance"] + pnl_eff, 2)
    if ev["balance"] >= L50["lock_trigger"]:
        ev["locked"] = True
    ev["peak"] = max(ev["peak"], ev["balance"])
    if ev["balance"] >= L50["start"] + L50["profit_target"] - 1e-9:
        st["passes"] += 1
        st["attempts"].append({"result": "pass", "day": day, "days": ev["days"], "balance": ev["balance"],
                               "start_day": ev.get("start_day")})
        st["eval"] = fresh_eval()
        return "pass"
    return None


def eval_snapshot(st: dict, open_pnl: float = 0.0, day_net: float = 0.0) -> dict:
    ev = st["eval"]
    equity = round(ev["balance"] + day_net + open_pnl, 2)
    profit = round(equity - L50["start"], 2)
    fin = st["passes"] + st["blows"]
    floor = eval_floor(ev)
    return {"title": "LucidPro 50K", "mode": "live sim (blown evals restart)",
            "passes": st["passes"], "fails": st["blows"], "blows": st["blows"], "attempts_finished": fin,
            "passing_pct": round(100.0 * st["passes"] / fin, 1) if fin else None,
            "passing_label": f"{st['passes']}/{fin}" if fin else None,
            "current_profit": profit, "current_progress_pct": round(100.0 * profit / L50["profit_target"], 1),
            "profit_target": L50["profit_target"], "mll_floor": round(floor, 2), "mll_buffer": round(equity - floor, 2),
            "mll_locked": bool(ev["locked"]), "dll_limit": L50["dll"], "dll": L50["dll"], "mll": L50["mll"],
            "start_balance": L50["start"], "current_equity": equity, "eval_days": ev["days"],
            "locked_mll_balance": L50["locked_floor"]}


# ---------------------------------------------------------------- data
def yahoo_1m(ticker: str, period: str = "7d") -> pd.DataFrame:
    """Yahoo 1-min futures bars (start-labelled) -> ts_ny/open/high/low/close/volume. Raises on empty."""
    import yfinance as yf
    h = yf.Ticker(ticker).history(period=period, interval="1m", prepost=True, auto_adjust=False, actions=False)
    if h is None or not len(h):
        raise RuntimeError(f"yahoo {ticker}: empty")
    h = h.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    h.index = h.index.tz_convert(ET)
    out = h.reset_index().rename(columns={h.index.name or "index": "ts_ny"})
    if "ts_ny" not in out.columns:
        out = out.rename(columns={out.columns[0]: "ts_ny"})
    return out


def load_seed() -> dict:
    try:
        return json.loads(SEED_FILE.read_text())
    except Exception:
        return {}


def _read(p: Path):
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def _iso(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, pd.Timestamp):
        t = t.to_pydatetime()
    return t.astimezone(UTC).isoformat()


def _atomic(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(p)


# ---------------------------------------------------------------- engine
class CmeMechEngine:
    """All six books. tick(): fetch closed 1-min bars, update the daily tables, decide / walk / close every
    session not yet final, roll the Lucid 50K evals, persist. Stateless per session (recomputed from bars), so a
    restart or a missed hour is caught up from Yahoo's 7-day 1-min history."""

    def __init__(self, cfg: dict, *, state_dir: Path, logs_dir: Path, site_dir: Path, fetch=None, io_lock=None,
                 now_fn=None):
        self.cfg = cfg
        self.c = {"enabled": True, "poll_seconds": 30, "idle_poll_seconds": 600, "min_bar_age_s": 20,
                  **(cfg.get("cme_mech") or {})}
        self.state_dir, self.logs_dir, self.site_dir = Path(state_dir), Path(logs_dir), Path(site_dir)
        self.fetch = fetch or (lambda m: yahoo_1m(MK[m]["yahoo"]))
        self.io_lock = io_lock
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.eng_path = self.state_dir / "cme_mech" / "engine.json"
        self.eng = _read(self.eng_path) or {}
        seed = load_seed()
        self.eng.setdefault("daily", {})
        for m in MK:
            tab = self.eng["daily"].setdefault(m, {})
            for d, v in ((seed.get("daily") or {}).get(m) or {}).items():
                tab.setdefault(d, v)
        self.eng.setdefault("data", {})
        self.books = {}
        now = self.now_fn()
        for b, spec in BOOKS.items():
            p = self.state_dir / spec["dir"] / "account.json"
            s = _read(p) or {}
            if not s.get("created_at"):
                s = {"book": b, "created_at": _iso(now), "balance": L50["start"], "start_balance": L50["start"],
                     "positions": [], "sessions": {}, "final_through": None, "trades": 0}
            s.setdefault("lucid50", {"eval": fresh_eval(), "passes": 0, "blows": 0, "attempts": []})
            self.books[b] = s

    # ---- persistence
    def _acct_path(self, b):
        return self.state_dir / BOOKS[b]["dir"] / "account.json"

    def persist(self) -> None:
        for b, s in self.books.items():
            _atomic(self._acct_path(b), s)
        _atomic(self.eng_path, self.eng)
        _atomic(self.site_dir / SITE_FILE, self.site_payload())

    def _log(self, b: str, rec: dict) -> None:
        t = rec.get("t_exit") or rec.get("entry_time") or _iso(self.now_fn())
        day = str(t)[:10]
        f = self.logs_dir / day / f"{BOOKS[b]['dir']}_trades.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def _event(self, b: str | None, kind: str, **kw) -> None:
        f = self.logs_dir / self.now_fn().strftime("%Y-%m-%d") / "cme_mech_events.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a") as fh:
            fh.write(json.dumps({"t": _iso(self.now_fn()), "book": b, "event": kind, **kw}, default=str) + "\n")

    # ---- data
    async def _bars(self, m: str) -> pd.DataFrame | None:
        try:
            df = await asyncio.to_thread(self.fetch, m)
        except Exception as e:
            log.warning("cme_mech %s fetch failed: %s", m, e)
            self.eng["data"][m] = {**(self.eng["data"].get(m) or {}), "error": str(e)[:200], "error_at": _iso(self.now_fn())}
            return None
        df = prep_bars(df)
        tk = MK[m]["tick"]
        for k in ("open", "high", "low", "close"):        # Yahoo floats (4111.600098) -> the tick grid
            df[k] = ((df[k] / tk).round() * tk).round(6)
        now = pd.Timestamp(self.now_fn()).tz_convert(ET)
        df = df[df.ts_ny + pd.Timedelta(seconds=60 + float(self.c["min_bar_age_s"])) <= now]   # closed bars only
        if len(df):
            self.eng["data"][m] = {"source": f"Yahoo Finance {MK[m]['yahoo']} 1-min (CME futures)",
                                   "last_bar": _iso(df.ts_ny.iloc[-1]), "bars": int(len(df)), "fetched_at": _iso(self.now_fn())}
        return df

    def _session_complete(self, day: str, now_et: datetime) -> bool:
        end = datetime.fromisoformat(day).replace(hour=17, minute=0, tzinfo=ET)
        return now_et >= end

    def _update_daily(self, m: str, df: pd.DataFrame, now_et: datetime) -> None:
        tab = self.eng["daily"][m]
        for d, g in df.groupby("date"):
            ds = d.isoformat()
            if not self._session_complete(ds, now_et):
                continue
            if int(g.hm.iloc[0]) < 1700 and ds in tab:      # window starts mid-session: keep the full row we have
                continue
            if int(g.hm.iloc[0]) < 1700 and len(g) < 300:
                continue
            tab[ds] = daily_row(g, m)

    # ---- per-session processing
    async def tick(self) -> dict:
        now = self.now_fn()
        now_et = now.astimezone(ET)
        needed = sorted({BOOKS[b]["m"] for b in BOOKS})
        out = {}
        frames = {}
        for m in needed:
            df = await self._bars(m)
            if df is None or not len(df):
                continue
            frames[m] = df
            self._update_daily(m, df, now_et)
        for b in BOOK_ORDER:
            m = BOOKS[b]["m"]
            if m not in frames:
                continue
            try:
                out[b] = self._process_book(b, frames[m], now_et)
            except Exception:
                log.exception("cme_mech %s process failed", b)
        self.eng["updated_at"] = _iso(now)
        if self.io_lock is not None:
            async with self.io_lock:
                self.persist()
        else:
            self.persist()
        return out

    def _process_book(self, b: str, df: pd.DataFrame, now_et: datetime) -> dict:
        spec, s = BOOKS[b], self.books[b]
        m = spec["m"]; mk = MK[m]
        created = datetime.fromisoformat(s["created_at"]).astimezone(ET)
        dl = daily_stats(self.eng["daily"][m])
        e5 = ema5(df, 820) if any(l["sig"] == "vote60" for l in spec["legs"]) else None
        res = {}
        for d, g in df.groupby("date"):
            ds = d.isoformat()
            if s.get("final_through") and ds <= s["final_through"]:
                continue
            sess = s["sessions"].setdefault(ds, {"legs": {}, "final": False})
            if sess.get("final"):
                continue
            g = g.reset_index(drop=True)
            complete = self._session_complete(ds, now_et)
            prev = prev_stats(dl, ds)
            for leg in spec["legs"]:
                L = sess["legs"].setdefault(leg["leg"], {})
                if L.get("done"):
                    continue
                D = hm_add(leg["O"], 30)
                t_dec = datetime.fromisoformat(ds).replace(hour=D // 100, minute=D % 100, tzinfo=ET)
                if t_dec < created:
                    L.update({"done": True, "skip": "before book start"}); continue
                if sess.get("halted"):
                    L.update({"done": True, "skip": "day ended by the $1,200 DLL"}); continue
                path = g[(g.hm > D) & (g.hm <= CAP)]
                if "side" not in L:
                    if not len(path) and not complete:
                        continue                      # entry bar not closed yet
                    if complete and len(g) < 300:
                        L.update({"done": True, "skip": "session < 300 bars"}); continue
                    info = votes_for(g, leg["O"], m, prev, e5)
                    dec = decide(leg, info, m)
                    L["decided_at"] = _iso(self.now_fn())
                    if info and info.get("votes"):
                        L["info"] = {k: info[k] for k in ("votes", "gate", "rng15", "atr", "P", "o_open", "prev_close", "sma20")}
                    if "skip" in dec:
                        L.update({"done": True, "skip": dec["skip"]})
                        self._event(b, "skip", session=ds, leg=leg["leg"], reason=dec["skip"])
                        continue
                    if len(path[path.hm <= FLAT]) < 5 and complete:
                        L.update({"done": True, "skip": "fewer than 5 bars after the decision"}); continue
                    L.update({"side": dec["side"], "stop_pts": dec["stop_pts"], "tgt_pts": dec["tgt_pts"], "n": leg["n"]})
                    self._event(b, "signal", session=ds, leg=leg["leg"], side=dec["side"], stop_pts=dec["stop_pts"],
                                tgt_pts=dec["tgt_pts"], votes=(info or {}).get("votes"))
                r = walk(path, L["side"], L["stop_pts"], L["tgt_pts"], mk["tick"], leg["n"], mk["pv"], final=complete)
                if r["status"] == "pending":
                    continue
                self._sync_position(b, ds, leg, L, r, path)
            self._combo_dll(b, ds, sess)
            if complete and all(x.get("done") for x in sess["legs"].values()) and len(sess["legs"]) == len(spec["legs"]):
                self._finalize(b, ds, sess)
            res[ds] = {k: {kk: vv for kk, vv in v.items() if kk != "info"} for k, v in sess["legs"].items()}
        s["positions"] = [p for p in s["positions"] if p.get("status") == "OPEN"]
        return res

    def _pos_id(self, b, ds, leg):
        return f"{b}|{ds}|{leg['leg']}"

    def _sync_position(self, b, ds, leg, L, r, path) -> None:
        s = self.books[b]; m = BOOKS[b]["m"]; mk = MK[m]
        side = L["side"]; e = r["entry"]
        pid = self._pos_id(b, ds, leg)
        et0 = path.ts_ny.iloc[0]
        pos = next((p for p in s["positions"] if p["id"] == pid), None)
        if pos is None:
            pos = {"id": pid, "book": b, "session": ds, "leg": leg["leg"], "symbol": m, "contract": mk["contract"],
                   "qty": leg["n"], "side": "LONG" if side > 0 else "SHORT", "entry": round(e, 4),
                   "entry_time": _iso(et0), "stop": round(e - side * L["stop_pts"], 4),
                   "target": round(e + side * L["tgt_pts"], 4), "stop_pts": L["stop_pts"], "tgt_pts": L["tgt_pts"],
                   "fees": round(COMM * leg["n"], 2), "status": "OPEN", "model": MODEL, "first_cross_backfill": "mech"}
            s["positions"].append(pos)
            L["entry"] = pos["entry"]; L["entry_time"] = pos["entry_time"]
        sub = path[(path.hm <= FLAT)]
        upto = sub if r["status"] == "open" else sub.iloc[: r["bar_i"] + 1]
        pos.update(self._excursions(pos, upto, side, e, leg["n"], mk["pv"]))
        if r["status"] == "open":
            pos.update({"mark": r["mark"], "mark_at": _iso(r["mark_ts"]), "mae_pts": round(-r["mae"], 4),
                        "open_pnl": round(side * (r["mark"] - e) * leg["n"] * mk["pv"], 2)})
            L["open"] = True
            return
        rec = {"event": "close", "book": b, "id": pid, "session": ds, "leg": leg["leg"], "symbol": m,
               "contract": mk["contract"], "qty": leg["n"], "side": pos["side"], "entry": pos["entry"],
               "entry_time": pos["entry_time"], "stop": pos["stop"], "target": pos["target"],
               "exit": round(float(r["exit"]), 4), "t_exit": _iso(r["t_exit"]), "reason": r["reason"],
               "gross": r["gross"], "net": r["net"], "fees": pos["fees"], "mae_usd": r["mae_usd"], "model": MODEL,
               **{k: pos.get(k) for k in ("mfe", "mae", "went_positive_first", "first_cross", "first_cross_at",
                                          "first_cross_basis")}}
        pos["status"] = "CLOSED"
        s["balance"] = round(float(s["balance"]) + r["net"], 2)
        s["trades"] = int(s.get("trades") or 0) + 1
        L.update({"done": True, "open": False, "result": {k: rec[k] for k in ("exit", "t_exit", "reason", "net", "mae_usd")}})
        self._log(b, rec)
        self._event(b, "close", session=ds, leg=leg["leg"], reason=r["reason"], net=r["net"])

    def _excursions(self, pos, bars, side, e, n, pv) -> dict:
        if not len(bars):
            return {}
        fav = float(((bars.high - e) if side > 0 else (e - bars.low)).max())
        adv = float(((bars.low - e) if side > 0 else (e - bars.high)).min())
        out = {"mfe": round(max(fav, 0.0), 4), "mae": round(max(-adv, 0.0), 4)}
        # Went positive first on the 1-min path: net (gross - $1.50 x qty) > 0 before gross < 0. Inside a bar the
        # prints are walked O -> L -> H -> C on an up bar (close >= open) and O -> H -> L -> C on a down bar; the
        # entry print (the entry bar's open) is not a crossing.
        from . import first_cross as fc
        fees = COMM * n
        if pos.get("went_positive_first") not in (fc.YES, fc.NO, fc.UNKNOWN):
            for j, b in enumerate(bars.itertuples()):
                seq = (b.open, b.low, b.high, b.close) if b.close >= b.open else (b.open, b.high, b.low, b.close)
                hit = None
                for k, px in enumerate(seq):
                    if j == 0 and k == 0:
                        continue
                    g = side * (px - e) * n * pv
                    if g - fees > 1e-9:
                        hit = "pos"; break
                    if g < -1e-9:
                        hit = "neg"; break
                if hit:
                    out.update(fc.result(hit, b.ts_ny, "m1_ohlc_path")); break
        return out

    def _combo_dll(self, b, ds, sess) -> None:
        """One account, several legs: the day's summed worst excursion / P&L capped at -$1,200 ends the day."""
        if len(BOOKS[b]["legs"]) < 2 or sess.get("halted"):
            return
        legs = sess["legs"].values()
        closed = [x["result"] for x in legs if x.get("result")]
        opens = [p for p in self.books[b]["positions"] if p.get("session") == ds and p.get("status") == "OPEN"]
        mae_open = sum(min(-(p.get("mae_pts") or 0) * p["qty"] * MK[BOOKS[b]["m"]]["pv"] - COMM * p["qty"], 0.0) for p in opens)
        tot = sum(r["mae_usd"] for r in closed) + mae_open
        if tot > -DLL or not opens:
            return
        sess["halted"] = True
        rest = -DLL - sum(r["net"] for r in closed)
        for i, p in enumerate(opens):
            net = round(rest / len(opens), 2)
            side = 1 if p["side"] == "LONG" else -1
            px = p["entry"] + side * ((net / p["qty"] + COMM) / MK[BOOKS[b]["m"]]["pv"])
            fake = {"status": "closed", "entry": p["entry"], "exit": px, "t_exit": datetime.fromisoformat(p.get("mark_at") or p["entry_time"]),
                    "reason": "dll_1200_day", "net": net, "gross": round(net + COMM * p["qty"], 2), "mae_usd": net, "bar_i": 0}
            leg = next(l for l in BOOKS[b]["legs"] if l["leg"] == p["leg"])
            self._close_direct(b, ds, leg, sess["legs"][p["leg"]], p, fake)

    def _close_direct(self, b, ds, leg, L, pos, r) -> None:
        s = self.books[b]
        rec = {"event": "close", "book": b, "id": pos["id"], "session": ds, "leg": leg["leg"], "symbol": pos["symbol"],
               "contract": pos["contract"], "qty": pos["qty"], "side": pos["side"], "entry": pos["entry"],
               "entry_time": pos["entry_time"], "stop": pos["stop"], "target": pos["target"], "exit": round(float(r["exit"]), 4),
               "t_exit": _iso(r["t_exit"]), "reason": r["reason"], "gross": r["gross"], "net": r["net"], "fees": pos["fees"],
               "mae_usd": r["mae_usd"], "model": MODEL}
        pos["status"] = "CLOSED"
        s["balance"] = round(float(s["balance"]) + r["net"], 2)
        s["trades"] = int(s.get("trades") or 0) + 1
        L.update({"done": True, "open": False, "result": {k: rec[k] for k in ("exit", "t_exit", "reason", "net", "mae_usd")}})
        self._log(b, rec)

    def _finalize(self, b, ds, sess) -> None:
        s = self.books[b]
        results = [x["result"] for x in sess["legs"].values() if x.get("result")]
        traded_any = any(x.get("side") for x in sess["legs"].values())
        if all(x.get("skip") == "before book start" for x in sess["legs"].values()):
            sess["final"] = True
            s["final_through"] = max(s.get("final_through") or "", ds)
            return
        pnl, mae = day_net_mae(results) if results else (0.0, 0.0)
        st = s["lucid50"]
        if st["eval"].get("start_day") is None:
            st["eval"]["start_day"] = ds
        outcome = eval_day(st, ds, pnl, mae)
        if outcome:
            self._event(b, f"eval_{outcome}", session=ds, passes=st["passes"], blows=st["blows"])
        sess.update({"final": True, "day_pnl": round(pnl, 2), "day_mae": round(mae, 2), "traded": traded_any,
                     "eval_outcome": outcome})
        s["final_through"] = max(s.get("final_through") or "", ds)
        # keep the session map small: drop finalized sessions older than 10 days
        keep = sorted(s["sessions"])[-10:]
        s["sessions"] = {k: s["sessions"][k] for k in keep}

    # ---- site
    def card(self, b: str) -> dict:
        s = self.books[b]
        opens = [p for p in s["positions"] if p.get("status") == "OPEN"]
        op = sum(float(p.get("open_pnl") or 0) for p in opens)
        st = s["lucid50"]
        today = max(s["sessions"]) if s["sessions"] else None
        day_net = 0.0
        if today and not s["sessions"][today].get("final"):
            day_net = sum(x["result"]["net"] for x in s["sessions"][today]["legs"].values() if x.get("result"))
        return {"book": b, "label": BOOKS[b]["label"], "model": MODEL, "market": BOOKS[b]["m"],
                "contract": MK[BOOKS[b]["m"]]["contract"], "balance": s["balance"], "start_balance": L50["start"],
                "created_at": s["created_at"], "open_pnl": round(op, 2),
                "eval": eval_snapshot(st, op, day_net), "attempts": st["attempts"][-20:],
                "backtest": {**BOOKS[b]["backtest"], "label": "backtest (in-sample)",
                             "window": "GC 27 May - 5 Nov 2026, NQ/ES 4 May - 5 Nov 2026"},
                "today": ({"session": today, "legs": {k: {kk: vv for kk, vv in v.items() if kk not in ("info",)}
                                                      for k, v in s["sessions"][today]["legs"].items()}} if today else None)}

    def site_payload(self) -> dict:
        return {"updated_at": self.eng.get("updated_at"), "paper_only": True, "model_calls": False,
                "lucid50": L50, "data": self.eng.get("data"), "books": [self.card(b) for b in BOOK_ORDER]}


def trading_window(now: datetime) -> bool:
    """Active polling 08:15-17:10 New York on weekdays (all decisions, exits and the 16:40 flat)."""
    et = now.astimezone(ET)
    hm = et.hour * 100 + et.minute
    return et.weekday() < 5 and 815 <= hm <= 1710


async def run_cme_mech_loop(eng: CmeMechEngine, stop: asyncio.Event | None = None) -> None:
    """Poll every `poll_seconds` (30 s) 08:15-17:10 New York on weekdays, else every `idle_poll_seconds` (10 min,
    daily-table / end-of-session roll). Nothing is fetched while the weekend / holiday market gate is closed
    (one tick at start-up so the panel has the books)."""
    from .market_hours import markets_closed
    last = None
    while not (stop and stop.is_set()):
        now = datetime.now(UTC)
        active = trading_window(now)
        due = last is None or (not markets_closed(now) and
                               (active or (time.monotonic() - last) >= float(eng.c["idle_poll_seconds"])))
        if due:
            try:
                await eng.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cme_mech tick failed")
            last = time.monotonic()
        await asyncio.sleep(float(eng.c["poll_seconds"]) if active else 30.0)
