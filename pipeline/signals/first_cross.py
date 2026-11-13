"""Went-positive-first: did net unrealized P&L rise above $0 before gross fell below $0?

Basis (documented on the panel and in site_payload["pnl_note"]):
  unrealized_net = side * (price - entry) * qty * point_value - round_trip_fees

  "Yes"  — net rose above $0 (price covered round-trip fees) before gross fell below $0.
  "No"   — gross fell below $0 (price moved against the position) before net rose above $0.
  "—"    — neither has happened yet (still inside the fee cushion, or no path yet).
  "?"    — both sides occur in the same M1 bar and tick order is unavailable.

The entry print itself is not a crossing: at the fill, net equals -fees, which is the
edge of the cushion, not a loss. Going forward the 15s live-quote poll (same path as
the exit checks) is the source of truth. Already-open / already-closed positions are
backfilled from the same 15s last-print path reconstructed from tick history when
available; otherwise from M1 bars (Dukascopy or Binance). Ticks are not walked one
by one (a 10 ms bid flicker would otherwise dominate). If an M1 bar hits both sides
and 15s prints are unavailable, the result is "?".
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd

log = logging.getLogger("signals.first_cross")
UTC = timezone.utc
EPS = 1e-9
YES, NO, UNKNOWN = "yes", "no", "?"

# first_cross: "pos" | "neg" | "ambiguous" | None
# went_positive_first: "yes" | "no" | "?" | None


def _dt(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    if isinstance(s, pd.Timestamp):
        d = s.to_pydatetime()
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


def _iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    if isinstance(t, pd.Timestamp):
        t = t.to_pydatetime()
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat()


def gross_usd(side: int, entry: float, price: float, qty: float, pv: float) -> float:
    return float(side) * (float(price) - float(entry)) * float(qty) * float(pv)


def net_usd(side: int, entry: float, price: float, qty: float, pv: float, fees: float) -> float:
    return gross_usd(side, entry, price, qty, pv) - float(fees or 0)


def classify(net: float, fees: float, *, eps: float = EPS) -> str | None:
    """'pos' if net > 0, 'neg' if gross < 0 (net < -fees), else None (fee cushion)."""
    fees = max(0.0, float(fees or 0))
    if net > eps:
        return "pos"
    if net < -fees - eps:
        return "neg"
    return None


def result(cross: str | None, at=None, basis: str | None = None) -> dict:
    if cross == "pos":
        wpf = YES
    elif cross == "neg":
        wpf = NO
    elif cross == "ambiguous":
        wpf = UNKNOWN
    else:
        wpf = None
    return {
        "went_positive_first": wpf,
        "first_cross": cross,
        "first_cross_at": _iso(_dt(at)) if at is not None else None,
        "first_cross_basis": basis if cross else basis,
    }


def pending(basis: str | None = None) -> dict:
    return result(None, None, basis)


def resolved(row: dict) -> bool:
    return row.get("went_positive_first") in (YES, NO, UNKNOWN) or row.get("first_cross") in (
        "pos", "neg", "ambiguous")


def display_of(row: dict) -> str:
    v = row.get("went_positive_first")
    if v == YES:
        return "Yes"
    if v == NO:
        return "No"
    if v == UNKNOWN or row.get("first_cross") == "ambiguous":
        return "?"
    return "—"


def observe_print(row: dict, price: float, ts, *, side: int, entry: float, qty: float,
                  pv: float, fees: float, basis: str = "live_net_tick") -> bool:
    """Apply one time-ordered mark. Returns True if this print resolved the row."""
    if resolved(row):
        return False
    et = _dt(row.get("entry_time"))
    t = _dt(ts)
    if t is not None and et is not None and t <= et:
        return False  # entry instant is not a crossing
    s = classify(net_usd(side, entry, price, qty, pv, fees), fees)
    if not s:
        return False
    row.update(result(s, t, basis))
    return True


def first_cross_from_prints(prints: Iterable, *, side: int, entry: float, qty: float,
                            pv: float, fees: float, entry_time=None, exit_time=None,
                            basis: str = "tick_path") -> dict:
    """Walk (ts, price) prints in time order. Skips the entry instant and anything after exit."""
    et, xt = _dt(entry_time), _dt(exit_time)
    for ts, price in prints:
        t = _dt(ts)
        if t is None or price is None:
            continue
        if et is not None and t <= et:
            continue
        if xt is not None and t > xt:
            break
        s = classify(net_usd(side, entry, float(price), qty, pv, fees), fees)
        if s:
            return result(s, t, basis)
    return pending(basis)


def last_prints_every(ticks: pd.DataFrame | None, entry_time, exit_time=None,
                      period_s: float = 15) -> list[tuple[datetime, float]]:
    """Last print in each `period_s` window after entry, matching the 15s panel poll.

    Windows are (entry, entry+15s], (entry+15s, entry+30s], ... up to exit.
    A brief flicker that does not last until the window's last print is ignored.
    """
    et, xt = _dt(entry_time), _dt(exit_time)
    if ticks is None or len(ticks) == 0 or et is None:
        return []
    t = ticks.sort_index()
    if t.index.tz is None:
        t.index = t.index.tz_localize("UTC")
    end = pd.Timestamp(xt) if xt is not None else t.index[-1]
    sl = t[(t.index > pd.Timestamp(et)) & (t.index <= end)]
    if sl.empty:
        return []
    col = "bid" if "bid" in sl.columns else ("close" if "close" in sl.columns else sl.columns[0])
    out = []
    period = pd.Timedelta(seconds=period_s)
    # bucket = number of full periods since entry; keep the last print in each bucket
    elapsed = (sl.index - pd.Timestamp(et)).total_seconds()
    buckets = (elapsed // period_s).astype(int)
    for b, part in sl.groupby(buckets):
        ts = part.index[-1].to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        out.append((ts, float(part[col].iloc[-1])))
    return out


def first_cross_from_history(bars: pd.DataFrame | None, *, side: int, entry: float, qty: float,
                             pv: float, fees: float, entry_time, exit_time=None,
                             ticks: pd.DataFrame | None = None, exit_price: float | None = None,
                             period_s: float = 15) -> dict:
    """Prefer a 15s last-print path from ticks (same basis as the live poll); else M1 OHLC."""
    et, xt = _dt(entry_time), _dt(exit_time)
    prints = last_prints_every(ticks, et, xt, period_s=period_s)
    if prints:
        r = first_cross_from_prints(prints, side=side, entry=entry, qty=qty, pv=pv, fees=fees,
                                    entry_time=et, exit_time=xt, basis="tick_15s")
        if r["first_cross"] or not (bars is not None and len(bars)):
            # If 15s path ran across the window and never left the cushion, keep that
            # (pending) rather than letting a coarser M1 bar invent a crossing.
            if r["first_cross"]:
                return r
            if xt is not None or (prints and et is not None):
                # full window walked
                if exit_price is not None and xt is not None:
                    r2 = first_cross_from_prints(prints + [(xt, exit_price)], side=side, entry=entry,
                                                 qty=qty, pv=pv, fees=fees, entry_time=et,
                                                 exit_time=xt, basis="tick_15s")
                    return r2
                return r
    return first_cross_from_m1(bars, side=side, entry=entry, qty=qty, pv=pv, fees=fees,
                               entry_time=entry_time, exit_time=exit_time, ticks=ticks,
                               exit_price=exit_price)


def _bar_prints_ohlc(row, side: int) -> tuple[float, float, float, float]:
    o, h, lo, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    return o, h, lo, c


def _ticks_in(ticks: pd.DataFrame | None, start: datetime, end: datetime):
    if ticks is None or len(ticks) == 0:
        return []
    t = ticks.sort_index()
    if t.index.tz is None:
        t.index = t.index.tz_localize("UTC")
    sl = t[(t.index > pd.Timestamp(start)) & (t.index <= pd.Timestamp(end))]
    col = "bid" if "bid" in sl.columns else ("close" if "close" in sl.columns else sl.columns[0])
    return [(idx, float(px)) for idx, px in sl[col].items()]


def first_cross_from_m1(bars: pd.DataFrame, *, side: int, entry: float, qty: float, pv: float,
                        fees: float, entry_time, exit_time=None, ticks: pd.DataFrame | None = None,
                        exit_price: float | None = None) -> dict:
    """Walk M1 OHLC after entry. Use ticks for the entry/exit bar and for bars that hit both sides."""
    et, xt = _dt(entry_time), _dt(exit_time)
    if et is None:
        return pending("m1_ohlc")
    if bars is None or len(bars) == 0:
        if exit_price is not None and xt is not None:
            return first_cross_from_prints([(xt, exit_price)], side=side, entry=entry, qty=qty,
                                           pv=pv, fees=fees, entry_time=et, exit_time=xt,
                                           basis="exit_print")
        return pending("m1_ohlc")

    df = bars.sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    need_ticks_windows: list[tuple[datetime, datetime]] = []

    for ts, row in df.iterrows():
        bar_open = ts.to_pydatetime()
        if bar_open.tzinfo is None:
            bar_open = bar_open.replace(tzinfo=UTC)
        bar_end = bar_open + timedelta(minutes=1)
        if bar_end <= et:
            continue
        if xt is not None and bar_open >= xt:
            break

        in_entry = bar_open <= et < bar_end
        in_exit = xt is not None and bar_open < xt <= bar_end
        window_start = et if in_entry else bar_open
        window_end = xt if in_exit else bar_end

        # Entry/exit bars: high/low may include prices outside the position's life.
        if in_entry or in_exit:
            prints = _ticks_in(ticks, window_start, window_end)
            if prints:
                r = first_cross_from_prints(prints, side=side, entry=entry, qty=qty, pv=pv,
                                            fees=fees, entry_time=et, exit_time=xt, basis="tick_path")
                if r["first_cross"]:
                    return r
            else:
                need_ticks_windows.append((window_start, window_end))
            # Without ticks: only trust the close if it falls inside the position window,
            # and the open if the bar opened after entry (exit bar). Never trust H/L.
            trusted = []
            if (not in_entry) and (et is None or bar_open > et):
                trusted.append((bar_open, float(row["open"])))
            close_at = bar_end
            if (xt is None or close_at <= xt) and (et is None or close_at > et) and not in_exit:
                trusted.append((close_at, float(row["close"])))
            if in_exit and exit_price is not None:
                trusted.append((xt, float(exit_price)))
            r = first_cross_from_prints(trusted, side=side, entry=entry, qty=qty, pv=pv,
                                        fees=fees, entry_time=et, exit_time=xt, basis="m1_close")
            if r["first_cross"]:
                # If H/L of this bar would have hit the OTHER side, we cannot be sure without ticks.
                other = "neg" if r["first_cross"] == "pos" else "pos"
                fav = float(row["high"] if side > 0 else row["low"])
                adv = float(row["low"] if side > 0 else row["high"])
                fav_s = classify(net_usd(side, entry, fav, qty, pv, fees), fees)
                adv_s = classify(net_usd(side, entry, adv, qty, pv, fees), fees)
                # Only the portion after entry / before exit is unknown; if both extremes
                # classify, tick order is required. If we already needed ticks for this bar,
                # return ambiguous rather than a possibly-wrong close/open answer.
                if fav_s and adv_s:
                    return result("ambiguous", window_start, "m1_ambiguous")
                return r
            # If H/L would have resolved at least one side, we needed ticks we don't have.
            fav = float(row["high"] if side > 0 else row["low"])
            adv = float(row["low"] if side > 0 else row["high"])
            if classify(net_usd(side, entry, fav, qty, pv, fees), fees) or classify(
                    net_usd(side, entry, adv, qty, pv, fees), fees):
                if not prints:
                    # May still be resolved by later bars; remember, don't return ? yet unless
                    # this is the only chance (handled after the loop if still pending and flagged).
                    need_ticks_windows.append((window_start, window_end))
            continue

        # Full bar wholly inside the position.
        o, h, lo, c = _bar_prints_ohlc(row, side)
        o_s = classify(net_usd(side, entry, o, qty, pv, fees), fees)
        if o_s:
            return result(o_s, bar_open, "m1_open")
        fav = h if side > 0 else lo
        adv = lo if side > 0 else h
        fav_s = classify(net_usd(side, entry, fav, qty, pv, fees), fees)
        adv_s = classify(net_usd(side, entry, adv, qty, pv, fees), fees)
        if fav_s == "pos" and adv_s == "neg":
            prints = _ticks_in(ticks, bar_open, bar_end if xt is None else min(bar_end, xt))
            if prints:
                r = first_cross_from_prints(prints, side=side, entry=entry, qty=qty, pv=pv,
                                            fees=fees, entry_time=et, exit_time=xt, basis="tick_path")
                if r["first_cross"]:
                    return r
            return result("ambiguous", bar_open, "m1_ambiguous")
        if fav_s == "pos":
            return result("pos", bar_open, "m1_ohlc")
        if adv_s == "neg":
            return result("neg", bar_open, "m1_ohlc")

    if exit_price is not None and xt is not None:
        r = first_cross_from_prints([(xt, exit_price)], side=side, entry=entry, qty=qty,
                                    pv=pv, fees=fees, entry_time=et, exit_time=xt, basis="exit_print")
        if r["first_cross"]:
            return r
    return pending("m1_ohlc")


def apply_result(row: dict, res: dict) -> None:
    if resolved(row):
        return
    if not res:
        return
    if res.get("first_cross") or res.get("went_positive_first"):
        row["went_positive_first"] = res.get("went_positive_first")
        row["first_cross"] = res.get("first_cross")
        row["first_cross_at"] = res.get("first_cross_at")
        row["first_cross_basis"] = res.get("first_cross_basis")


def book_share(rows: list[dict]) -> dict:
    """Share of positions with Yes, out of those resolved (yes or no; excludes — and ?)."""
    decided = [r for r in rows if r.get("went_positive_first") in (YES, NO)]
    yes = sum(1 for r in decided if r["went_positive_first"] == YES)
    n = len(decided)
    if n == 0:
        return {"went_positive_first": None, "went_positive_first_yes": 0,
                "went_positive_first_n": 0, "went_positive_first_pct": None}
    pct = int(round(100 * yes / n))
    return {"went_positive_first": f"{pct}% ({yes}/{n})",
            "went_positive_first_yes": yes, "went_positive_first_n": n,
            "went_positive_first_pct": pct}


# ---------------- historical fetch (used by the panel backfill) ----------------

async def fetch_m1_range(symbol: str, start: datetime, end: datetime, cfg: dict) -> tuple[pd.DataFrame, str]:
    """M1 bars covering [start, end], via the symbol's usual provider chain."""
    from .live_price import m1_ticker, _fetch_duka_m1, _M1_TIMEOUT
    from .data_providers import BinanceProvider, COLS, _get_httpx

    start, end = _dt(start), _dt(end)
    errors = []
    # Prefer the same order as live_price.fetch_symbol_m1, but with an explicit window.
    duka = m1_ticker(symbol, cfg, "dukascopy")
    # BTC prefers Binance in config.chain
    chain = ((cfg.get("symbols") or {}).get(symbol) or {}).get("chain") or ["dukascopy", "binance"]
    if "binance" in chain and chain.index("binance") < (chain.index("dukascopy") if "dukascopy" in chain else 99):
        order = ("binance", "dukascopy")
    else:
        order = ("dukascopy", "binance")

    for prov in order:
        try:
            if prov == "dukascopy" and duka:
                df = await _fetch_duka_m1(duka, start - timedelta(minutes=2), end + timedelta(minutes=2),
                                          max(_M1_TIMEOUT, 40.0))
                df = df[(df.index >= pd.Timestamp(start) - pd.Timedelta(minutes=1))
                        & (df.index <= pd.Timestamp(end) + pd.Timedelta(minutes=1))]
                if len(df):
                    return df, f"dukascopy:{duka}:m1"
                errors.append(f"dukascopy:{duka}: empty")
            elif prov == "binance":
                ticker = m1_ticker(symbol, cfg, "binance")
                if not ticker:
                    continue
                client = await _get_httpx()
                params = {"symbol": ticker, "interval": "1m",
                          "startTime": int(start.timestamp() * 1000) - 120_000,
                          "endTime": int(end.timestamp() * 1000) + 120_000,
                          "limit": 1000}
                r = await client.get(BinanceProvider.URL, params=params)
                r.raise_for_status()
                rows = r.json()
                df = pd.DataFrame([[x[0], *x[1:6]] for x in rows], columns=["t", *COLS])
                df.index = pd.to_datetime(df.pop("t"), unit="ms", utc=True)
                df = df.astype(float)
                if len(df):
                    return df, f"binance:{ticker}:m1"
                errors.append(f"binance:{ticker}: empty")
        except Exception as e:
            errors.append(f"{prov}: {e}")
            log.warning("first-cross M1 %s %s failed: %s", symbol, prov, e)
    raise RuntimeError(f"{symbol}: M1 range failed: {errors or 'no ticker'}")


async def fetch_tick_range(symbol: str, start: datetime, end: datetime, cfg: dict) -> tuple[pd.DataFrame, str]:
    """Tick / trade prints covering (start, end], Dukascopy ticks or Binance aggTrades."""
    from .live_price import m1_ticker
    from .data_providers import DukascopyProvider, _get_httpx
    import asyncio, glob, json, shutil, tempfile

    start, end = _dt(start), _dt(end)
    chain = ((cfg.get("symbols") or {}).get(symbol) or {}).get("chain") or ["dukascopy", "binance"]
    errors = []

    async def _duka():
        ticker = m1_ticker(symbol, cfg, "dukascopy")
        if not ticker:
            raise RuntimeError("no dukascopy ticker")
        dp = DukascopyProvider()
        out = tempfile.mkdtemp(prefix="duka_fc_")
        try:
            args = dp.cmd + ["-i", ticker,
                             "-from", start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                             "-to", end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                             "-t", "tick", "-f", "json", "-dir", out, "-r", "1", "-s"]
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), 45.0)
            except asyncio.TimeoutError:
                proc.kill()
                raise
            files = glob.glob(f"{out}/*.json")
            if not files:
                raise RuntimeError(f"no file ({stdout.decode()[-200:].strip()})")
            rows = json.load(open(files[0]))
        finally:
            shutil.rmtree(out, ignore_errors=True)
        if not rows:
            raise RuntimeError("empty")
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
        return df.rename(columns={"bidPrice": "bid", "askPrice": "ask"})[["bid", "ask"]].astype(float), f"dukascopy:{ticker}:tick"

    async def _binance_agg():
        ticker = m1_ticker(symbol, cfg, "binance")
        if not ticker:
            raise RuntimeError("no binance ticker")
        client = await _get_httpx()
        # aggTrades, 1000 at a time
        frames = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        while cursor < end_ms and len(frames) < 20:
            r = await client.get("https://api.binance.com/api/v3/aggTrades",
                                 params={"symbol": ticker, "startTime": cursor, "endTime": end_ms, "limit": 1000})
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            part = pd.DataFrame(rows)
            part.index = pd.to_datetime(part["T"], unit="ms", utc=True)
            part = part.rename(columns={"p": "bid"})[["bid"]].astype(float)
            frames.append(part)
            last = int(rows[-1]["T"])
            if last <= cursor or len(rows) < 1000:
                break
            cursor = last + 1
        if not frames:
            raise RuntimeError("empty")
        return pd.concat(frames), f"binance:{ticker}:aggTrades"

    # Same preference as M1: BTC -> binance first
    order = ("binance", "dukascopy") if chain and chain[0] == "binance" else ("dukascopy", "binance")
    fn = {"dukascopy": _duka, "binance": _binance_agg}
    for prov in order:
        try:
            return await fn[prov]()
        except Exception as e:
            errors.append(f"{prov}: {e}")
            log.warning("first-cross ticks %s %s failed: %s", symbol, prov, e)
    raise RuntimeError(f"{symbol}: tick range failed: {errors or 'no ticker'}")
