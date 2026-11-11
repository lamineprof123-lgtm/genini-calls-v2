"""FirmTape options-gamma levels (GEX) + 1-second NQ price. SIGNALS ONLY.

GET {url}?sym=NQ,GLD  (Authorization: Bearer $FIRMTAPE_API_KEY). Gotchas handled here:
  * errors arrive as HTTP 200: top-level {"error": ...} or levels[i].error
  * per-symbol units differ -> only the SIGN of net_gamma is used (regime)
  * levels are only meaningful in NY RTH (09:30-16:00 ET) with session == "open" and age_s <= max_age_s
  * next_update_s is 3 (open) / 60 (closed): the shared cache TTL follows it (clamped)

Normalisation (raw FirmTape prices never reach the chart or Gemini):
  US100 <- NQ : additive basis.  adjusted = raw + basis, basis = Dukascopy price - NQ price.
               basis = rolling median of the last `basis_window` ALIGNED samples (Dukascopy live ts and FirmTape
               price_as_of within basis_align_s), else the instantaneous difference (marked "instant").
  XAUUSD <- GLD: ratio.  adjusted = raw * (Dukascopy price / GLD price). Opening range omitted.

US100 live price: during RTH with a fresh NQ quote (now - price_as_of <= live_max_age_s) and at least one aligned
basis sample, live_price = NQ price + rolling basis (live_price_source = "firmtape_nq+basis"); the forming M15
bar's high/low/close are extended with it. Otherwise the Dukascopy M1 value stays (fallback).
"""
from __future__ import annotations
import asyncio, logging, os, statistics, time
from collections import deque
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("signals.firmtape")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")

GEX_COLORS = {"call": "#FFB300", "put": "#00BFA5", "flip": "#E5E7EB"}
# Context keys that are logged in the cycle record but not sent to Gemini (see gemini_client._GEMINI_CTX_DROP)
LOG_ONLY_KEYS = ("gex_status", "live_basis", "live_basis_n", "live_basis_quality", "live_price_dukascopy",
                 "live_price_dukascopy_ts_utc")


def ft_cfg(cfg: dict) -> dict:
    d = {"enabled": False, "url": "https://firmtape.com/api/v1/levels", "timeout_seconds": 4,
         "symbols": {"US100": "NQ", "XAUUSD": "GLD"}, "rth_et": ["09:30", "16:00"], "max_age_s": 300,
         "min_cache_seconds": 3, "max_cache_seconds": 60, "error_cache_seconds": 10, "walls_per_side": 2,
         "chart_margin_pct": 15, "live_price_override": True, "live_max_age_s": 5, "basis_align_s": 20,
         "basis_window": 7, "basis_max_age_minutes": 30}
    return {**d, **(cfg.get("firmtape") or {})}


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_ny_rth(t: datetime, cfg: dict) -> bool:
    a, b = ft_cfg(cfg)["rth_et"]
    et = t.astimezone(ET)
    return et.weekday() < 5 and _hm(a) <= et.time() < _hm(b)


def _ts(s) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except Exception:
        return None


# ------------------------------------------------------------------------------------------ client + cache
class FirmTapeClient:
    """One HTTP request shared by every caller inside the cache TTL (entry cycle + exit checks)."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._data: dict | None = None
        self._err: str | None = None
        self._until = 0.0
        self._warned = False
        self.requests = 0

    def reset(self):
        self._data, self._err, self._until = None, None, 0.0

    async def get(self, cfg: dict, fetch=None) -> tuple[dict | None, str | None]:
        """(response json, error). Never raises."""
        f = ft_cfg(cfg)
        async with self._lock:
            if time.monotonic() < self._until:
                return self._data, self._err
            key = os.environ.get("FIRMTAPE_API_KEY", "").strip()
            if not key and fetch is None:
                if not self._warned:
                    log.warning("FIRMTAPE_API_KEY not set -> gex = null")
                    self._warned = True
                self._data, self._err, self._until = None, "no_api_key", time.monotonic() + 300
                return None, self._err
            syms = ",".join(dict.fromkeys(f["symbols"].values()))
            try:
                self.requests += 1
                if fetch is not None:
                    data = await fetch(syms)
                else:
                    from .data_providers import _get_httpx
                    client = await _get_httpx()
                    r = await client.get(f["url"], params={"sym": syms}, timeout=f["timeout_seconds"],
                                         headers={"Authorization": f"Bearer {key}"})
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    data = r.json()
                if isinstance(data, dict) and data.get("error"):
                    raise RuntimeError(f"api error: {str(data.get('error'))[:160]}")
                ttl = min(max(float(data.get("next_update_s") or f["min_cache_seconds"]), f["min_cache_seconds"]),
                          f["max_cache_seconds"])
                self._data, self._err, self._until = data, None, time.monotonic() + ttl
            except Exception as e:
                self._data, self._err = None, f"{type(e).__name__}: {str(e)[:160]}"
                self._until = time.monotonic() + f["error_cache_seconds"]
                log.warning("firmtape request failed: %s", self._err)
            return self._data, self._err


_CLIENT = FirmTapeClient()


def get_client() -> FirmTapeClient:
    return _CLIENT


def entry_for(data: dict | None, ft_sym: str) -> dict | None:
    for lv in (data or {}).get("levels") or []:
        if isinstance(lv, dict) and lv.get("sym") == ft_sym:
            return lv
    return None


def gate(entry: dict | None, now: datetime, cfg: dict) -> str:
    """'ok' or the reason gex must be null."""
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
    return "ok"


# ------------------------------------------------------------------------------------------ basis
class BasisTracker:
    """Rolling Dukascopy-minus-FirmTape basis per symbol from aligned samples (in-process)."""

    def __init__(self):
        self.samples: dict[str, deque] = {}

    def add(self, sym: str, duka_price, duka_ts, ft_price, ft_ts, cfg: dict) -> bool:
        f = ft_cfg(cfg)
        a, b = _ts(duka_ts) if not isinstance(duka_ts, datetime) else duka_ts, \
            _ts(ft_ts) if not isinstance(ft_ts, datetime) else ft_ts
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


def get_basis_tracker() -> BasisTracker:
    return _BASIS


def scaler(sym: str, live_price: float, ft_price: float, basis: float | None = None):
    """Map a raw FirmTape price onto the Dukascopy scale."""
    if sym == "US100":
        b = basis if basis is not None else (live_price - ft_price)
        return lambda x: None if x is None else round(float(x) + b, 2)
    r = live_price / ft_price
    return lambda x: None if x is None else round(float(x) * r, 2)


# ------------------------------------------------------------------------------------------ gex object
def _rank_no(rank: str) -> int:
    try:
        return int(str(rank)[1:])
    except Exception:
        return 99


def build_gex(sym: str, entry: dict, live_price: float, cfg: dict, basis: float | None = None) -> dict:
    """Clean, adjusted GEX levels for the numeric context (no raw prices / dollar totals / timestamps)."""
    f = ft_cfg(cfg)
    adj = scaler(sym, live_price, float(entry["price"]), basis)
    sign = str(((entry.get("net_gamma") or {}).get("sign")) or "").lower()
    flip = adj(entry.get("flip"))
    if sign in ("positive", "negative"):
        regime = "POSITIVE_DAMPENING" if sign == "positive" else "NEGATIVE_AMPLIFYING"
        basis_of_regime = "net_gamma_sign"
    else:   # no sign: fall back to price vs flip
        regime = "POSITIVE_DAMPENING" if (flip is not None and live_price >= flip) else "NEGATIVE_AMPLIFYING"
        basis_of_regime = "price_vs_flip"
    loi = ((entry.get("levels_of_interest") or {}).get("levels")) or []
    walls = {}
    for side, fallback in (("call", "call_wall"), ("put", "put_wall")):
        rows = sorted([x for x in loi if x.get("side") == side and x.get("strike") is not None],
                      key=lambda x: _rank_no(x.get("rank")))[: int(f["walls_per_side"])]
        out = [{"price": adj(x["strike"]), "rank": _rank_no(x.get("rank")),
                "strength": x.get("strength"), "oi_aligned": "open_interest" in (x.get("books") or [])}
               for x in rows]
        if not out and entry.get(fallback) is not None:
            oi_w = (entry.get("oi") or {}).get(fallback)
            out = [{"price": adj(entry[fallback]), "rank": 1, "strength": None,
                    "oi_aligned": oi_w is not None and abs(float(oi_w) - float(entry[fallback])) < 1e-9}]
        walls[side] = out
    g = {"regime": regime, "regime_from": basis_of_regime, "gamma_flip": flip,
         "price_vs_flip": None if flip is None else ("above" if live_price >= flip else "below"),
         "call_walls": walls["call"], "put_walls": walls["put"]}
    if sym == "US100":
        orr = ((entry.get("session_levels") or {}).get("opening_range")) or None
        if orr and orr.get("complete") and orr.get("high") is not None and orr.get("low") is not None:
            g["ny_opening_range"] = {"high": adj(orr["high"]), "low": adj(orr["low"])}
    return g


# ------------------------------------------------------------------------------------------ chart lines
def gex_chart_lines(payload: dict, gex: dict | None, cfg: dict) -> tuple[list[dict], list[dict]]:
    """(gex price lines, extra S/R levels for the chart). Only lines inside the visible candle range +/- margin."""
    if not gex:
        return [], []
    f = ft_cfg(cfg)
    vis = int(payload.get("visible") or 100)
    bars = list(payload.get("candles") or [])[-vis:] + ([payload["forming_candle"]] if payload.get("forming_candle") else [])
    if not bars:
        return [], []
    lo, hi = min(b["low"] for b in bars), max(b["high"] for b in bars)
    m = (hi - lo) * float(f["chart_margin_pct"]) / 100.0
    ok = lambda p: p is not None and lo - m <= p <= hi + m
    lines = []
    cw = (gex.get("call_walls") or [None])[0]
    pw = (gex.get("put_walls") or [None])[0]
    if cw and ok(cw["price"]):
        lines.append({"price": cw["price"], "color": GEX_COLORS["call"], "width": 1.5, "style": "dashdot",
                      "title": "Call Wall #1" + (" [OI]" if cw.get("oi_aligned") else "")})
    if pw and ok(pw["price"]):
        lines.append({"price": pw["price"], "color": GEX_COLORS["put"], "width": 1.5, "style": "dashdot",
                      "title": "Put Wall #1" + (" [OI]" if pw.get("oi_aligned") else "")})
    if ok(gex.get("gamma_flip")):
        lines.append({"price": gex["gamma_flip"], "color": GEX_COLORS["flip"], "width": 1, "style": "dotted",
                      "title": "Gamma Flip"})
    extra = []
    orr = gex.get("ny_opening_range")
    if orr:
        last = (bars[-1] or {}).get("close")
        for lbl, p in (("NY OR High", orr["high"]), ("NY OR Low", orr["low"])):
            if ok(p):
                extra.append({"type": "resistance" if (last is not None and p > last) else "support",
                              "label": lbl, "price": p, "touches": 1})
    return lines, extra


# ------------------------------------------------------------------------------------------ apply
def apply_firmtape(payload: dict, data: dict | None, err: str | None, now: datetime, cfg: dict,
                   tracker: BasisTracker | None = None) -> dict:
    """Mutates payload: context gex (or None), US100 live price override, chart lines. Returns a log dict."""
    f = ft_cfg(cfg)
    tracker = tracker or _BASIS
    sym = payload["symbol"]
    ctx = payload.setdefault("context", {})
    ctx["gex"] = None
    payload["gex_lines"] = []
    ft_sym = f["symbols"].get(sym)
    if not ft_sym:
        return {"symbol": sym, "gex": "n/a"}
    if err:
        ctx["gex_status"] = f"request_failed: {err}"
        return {"symbol": sym, "gex": ctx["gex_status"]}
    entry = entry_for(data, ft_sym)
    status = gate(entry, now, cfg)
    ctx["gex_status"] = status
    out = {"symbol": sym, "gex": status}
    if status != "ok":
        return out
    ft_price = float(entry["price"])
    ft_ts = _ts(entry.get("price_as_of"))
    duka = ctx.get("live_price") if not ctx.get("live_price_stale") else None
    basis = None
    if sym == "US100":
        tracker.add(sym, duka, ctx.get("live_price_ts_utc"), ft_price, ft_ts, cfg)
        basis, n = tracker.basis(sym, now, cfg)
        quality = "rolling_median" if basis is not None else "instant"
        if basis is None and duka is not None:
            basis = round(duka - ft_price, 4)
        ctx.update({"live_basis": basis, "live_basis_n": n, "live_basis_quality": quality})
        out.update({"basis": basis, "basis_n": n})
        fresh = ft_ts is not None and (now - ft_ts).total_seconds() <= f["live_max_age_s"]
        if f["live_price_override"] and fresh and n > 0:
            d = int(ctx.get("digits") or 2)
            live = round(ft_price + basis, d)
            ctx.update({"live_price_dukascopy": ctx.get("live_price"),
                        "live_price_dukascopy_ts_utc": ctx.get("live_price_ts_utc"),
                        "live_price": live, "last_price": live, "live_price_ts_utc": ft_ts.astimezone(UTC).isoformat(),
                        "last_price_ts_utc": ft_ts.astimezone(UTC).isoformat(),
                        "live_price_source": "firmtape_nq+basis", "live_price_stale": False})
            if ctx.get("forming_m15_high") is not None:
                ctx["forming_m15_high"] = max(ctx["forming_m15_high"], live)
                ctx["forming_m15_low"] = min(ctx["forming_m15_low"], live)
            fc = payload.get("forming_candle")
            if fc:
                fc.update({"high": max(fc["high"], live), "low": min(fc["low"], live), "close": live})
            out["live_price"] = live
    ref = ctx.get("live_price") if ctx.get("live_price") is not None else ctx.get("last_close")
    if ref is None:
        ctx["gex_status"] = out["gex"] = "no_reference_price"
        return out
    gex = build_gex(sym, entry, float(ref), cfg, basis if sym == "US100" else None)
    ctx["gex"] = gex
    lines, extra = gex_chart_lines(payload, gex, cfg)
    payload["gex_lines"] = lines
    if extra:   # chart-only copy: context["levels"] (scalp / Lucid structure) is left untouched
        payload["levels"] = list(payload.get("levels") or []) + extra
    return out


async def enrich_firmtape(payloads: list[dict], cfg: dict, now: datetime | None = None) -> dict:
    """Entry cycle / exit check hook. Never raises."""
    now = now or datetime.now(UTC)
    f = ft_cfg(cfg)
    if not f["enabled"]:
        for p in payloads:
            p.setdefault("context", {})["gex"] = None
        return {"enabled": False}
    need = any(p["symbol"] in f["symbols"] for p in payloads)
    data, err = (await _CLIENT.get(cfg)) if (need and in_ny_rth(now, cfg)) else (None, None)
    res = {}
    for p in payloads:
        try:
            res[p["symbol"]] = apply_firmtape(p, data, err, now, cfg)
        except Exception as e:
            p.setdefault("context", {})["gex"] = None
            res[p["symbol"]] = {"error": f"{type(e).__name__}: {e}"}
            log.exception("firmtape apply %s failed", p["symbol"])
    log.info("firmtape %s", res)
    return res
