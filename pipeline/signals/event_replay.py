"""Replay recorded M1 through the SAME builders the live event feed uses (indicators.build_payload on the M15 bars
closed at `now`, live_price.live_from_m1 / apply_live_fields for the live price and the forming bar). No GEX
(FirmTape is not recorded). Used by the trigger unit tests and the trigger-count reports (tools/event_counts.py)."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import event_feed as EF

UTC = timezone.utc


def load_m1(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.astype(float).sort_index()


def replay_snapshot(sym: str, m1: pd.DataFrame, cfg: dict, now: datetime, history_bars: int = 200):
    """(payload, m1 up to now) as the live feed would have built them at `now`."""
    from .indicators import build_payload
    from .live_price import apply_live_fields, live_from_m1
    upto = m1[m1.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
    m15 = upto.resample("15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    m15 = m15[m15.index + pd.Timedelta(minutes=15) <= pd.Timestamp(now)].tail(history_bars)
    payload = build_payload(sym, m15, cfg, "dukascopy", sym.lower())
    d = int((payload.get("context") or {}).get("digits") or 5)
    apply_live_fields(payload, live_from_m1(upto, now=now, digits=d, source="replay:m1"))
    ctx = payload["context"]
    ctx.setdefault("session_blocks_automation", False)
    ctx["forming_m15_volume"] = EF.forming_volume(upto, now)
    ctx["forming_rvol"] = EF.forming_rvol(payload, upto, now)
    return payload, upto


def minutes(start: datetime, end: datetime, offset_s: int = 10):
    t = start.replace(second=0, microsecond=0) + timedelta(seconds=offset_s)
    while t < end:
        yield t
        t += timedelta(minutes=1)
