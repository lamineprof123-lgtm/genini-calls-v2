"""US100 live price from FirmTape's 1-second NQ quote (SIGNALS ONLY, never places orders).

Live-price subset of the exit-manager FirmTape module: no GEX levels, no prompt or chart lines.
GET {url}?sym=NQ  (Authorization: Bearer $FIRMTAPE_API_KEY; the key only ever comes from the environment).

  * errors arrive as HTTP 200: top-level {"error": ...} or levels[i].error
  * gate: NY regular hours (09:30-16:00 ET, Mon-Fri), session == "open", age_s <= max_age_s, no error,
    and the NQ price itself (price_as_of) no older than live_max_age_s
  * scale: US100 is the Dukascopy CFD. live = NQ + basis, basis = rolling median of the last
    `basis_window` aligned (Dukascopy tick ts vs price_as_of within basis_align_s) Dukascopy-minus-NQ samples.
    No aligned sample -> no override (the Dukascopy tick price stays).
"""
from __future__ import annotations
import logging, os, statistics
from collections import deque
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("signals.firmtape_live")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
SOURCE = "firmtape:NQ"

DEFAULTS = {"enabled": True, "url": "https://firmtape.com/api/v1/levels", "timeout_seconds": 4,
            "symbols": {"US100": "NQ"}, "rth_et": ["09:30", "16:00"], "max_age_s": 300,
            "live_max_age_s": 10, "basis_align_s": 20, "basis_window": 7, "basis_max_age_minutes": 30}


def ft_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **(((cfg.get("live_price") or {}).get("firmtape")) or {})}


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_ny_rth(t: datetime, cfg: dict) -> bool:
    a, b = ft_cfg(cfg)["rth_et"]
    et = t.astimezone(ET)
    return et.weekday() < 5 and _hm(a) <= et.time() < _hm(b)


def _ts(s) -> datetime | None:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except Exception:
        return None


async def fetch_levels(cfg: dict, fetch=None) -> tuple[dict | None, str | None]:
    """(response json, error). Never raises. `fetch` is a test hook: async (syms) -> json."""
    f = ft_cfg(cfg)
    syms = ",".join(dict.fromkeys(f["symbols"].values()))
    try:
        if fetch is not None:
            data = await fetch(syms)
        else:
            key = os.environ.get("FIRMTAPE_API_KEY", "").strip()
            if not key:
                return None, "no_api_key"
            from .data_providers import _get_httpx
            client = await _get_httpx()
            r = await client.get(f["url"], params={"sym": syms}, timeout=f["timeout_seconds"],
                                 headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"api error: {str(data.get('error'))[:160]}")
        return data, None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:160]}"
        log.warning("firmtape request failed: %s", err)
        return None, err


def entry_for(data: dict | None, ft_sym: str) -> dict | None:
    for lv in (data or {}).get("levels") or []:
        if isinstance(lv, dict) and lv.get("sym") == ft_sym:
            return lv
    return None


def gate(entry: dict | None, now: datetime, cfg: dict) -> str:
    """'ok' or why the FirmTape price must not be used."""
    f = ft_cfg(cfg)
    if not in_ny_rth(now, cfg):
        return "outside_ny_rth"
    if entry is None:
        return "no_data"
    if entry.get("error"):
        return f"symbol_error: {str(entry.get('error'))[:120]}"
    if entry.get("session") != "open":
        return f"session_{entry.get('session')}"
    age = entry.get("age_s")
    if age is None or float(age) > f["max_age_s"]:
        return f"stale_age_s_{age}"
    if not entry.get("price"):
        return "no_price"
    pts = _ts(entry.get("price_as_of"))
    if pts is None:
        return "no_price_as_of"
    if (now - pts).total_seconds() > f["live_max_age_s"]:
        return f"price_stale_{round((now - pts).total_seconds(), 1)}s"
    return "ok"


class BasisTracker:
    """Rolling Dukascopy-minus-FirmTape basis per symbol from aligned samples (in-process)."""

    def __init__(self):
        self.samples: dict[str, deque] = {}

    def add(self, sym: str, duka_price, duka_ts, ft_price, ft_ts, cfg: dict) -> bool:
        f = ft_cfg(cfg)
        a, b = _ts(duka_ts), _ts(ft_ts)
        if duka_price is None or ft_price is None or a is None or b is None:
            return False
        if abs((a - b).total_seconds()) > f["basis_align_s"]:
            return False
        dq = self.samples.setdefault(sym, deque(maxlen=int(f["basis_window"])))
        if dq and dq[-1][0] == b:
            return False   # same FirmTape quote already used
        dq.append((b, float(duka_price) - float(ft_price)))
        return True

    def basis(self, sym: str, now: datetime, cfg: dict) -> tuple[float | None, int]:
        f = ft_cfg(cfg)
        cut = now - timedelta(minutes=f["basis_max_age_minutes"])
        vals = [v for t, v in self.samples.get(sym, ()) if t >= cut]
        return (round(statistics.median(vals), 4), len(vals)) if vals else (None, 0)


_BASIS = BasisTracker()


def apply_firmtape_live(payload: dict, data: dict | None, err: str | None, now: datetime, cfg: dict,
                        tracker: BasisTracker | None = None) -> dict:
    """Override the payload's live price with NQ + basis when the gate passes. Returns a log dict."""
    f = ft_cfg(cfg)
    tracker = tracker or _BASIS
    sym = payload["symbol"]
    ft_sym = f["symbols"].get(sym)
    if not ft_sym:
        return {"symbol": sym, "firmtape": "n/a"}
    ctx = payload.setdefault("context", {})
    status = f"request_failed: {err}" if err else gate(entry_for(data, ft_sym), now, cfg)
    ctx["firmtape_status"] = status
    out = {"symbol": sym, "firmtape": status}
    if status != "ok":
        return out
    entry = entry_for(data, ft_sym)
    ft_price, ft_ts = float(entry["price"]), _ts(entry["price_as_of"])
    duka_ok = not ctx.get("live_price_stale") and ctx.get("live_price") is not None
    if duka_ok:
        tracker.add(sym, ctx["live_price"], ctx.get("live_price_ts_utc"), ft_price, ft_ts, cfg)
    basis, n = tracker.basis(sym, now, cfg)
    ctx.update({"live_basis": basis, "live_basis_n": n})
    out.update({"basis": basis, "basis_n": n})
    if basis is None:
        ctx["firmtape_status"] = out["firmtape"] = "no_aligned_basis"
        return out
    d = int(ctx.get("digits") or 2)
    live = round(ft_price + basis, d)
    ts = ft_ts.astimezone(UTC)
    ctx.update({"live_price_dukascopy": ctx.get("live_price"),
                "live_price_dukascopy_ts_utc": ctx.get("live_price_ts_utc"),
                "live_price_dukascopy_source": ctx.get("live_price_source"),
                "live_price": live, "last_price": live, "live_price_ts_utc": ts.isoformat(),
                "last_price_ts_utc": ts.isoformat(), "live_price_source": SOURCE, "live_price_stale": False})
    if ctx.get("forming_m15_high") is not None:
        ctx["forming_m15_high"] = round(max(ctx["forming_m15_high"], live), d)
        ctx["forming_m15_low"] = round(min(ctx["forming_m15_low"], live), d)
    fc = payload.get("forming_candle")
    if fc:
        fc.update({"high": max(fc["high"], live), "low": min(fc["low"], live), "close": live})
    out["live_price"] = live
    return out


def wanted(payloads: list[dict], cfg: dict, now: datetime) -> bool:
    f = ft_cfg(cfg)
    return bool(f["enabled"]) and in_ny_rth(now, cfg) and any(p["symbol"] in f["symbols"] for p in payloads)
