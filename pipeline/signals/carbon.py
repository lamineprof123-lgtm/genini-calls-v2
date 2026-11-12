"""Gemini 4 Carbon side books CB-BRACKET and CB-RUNNER (PAPER ONLY - nothing here places an order anywhere).

Source: the "Blue Bird: Gemini 4 Carbon side books" playbook (user t221u/t222u, 12 Nov 2026).

  Entries  gemini-4-carbon, thinking_level "medium", the CURRENT live entry prompt (templates/system_prompt.j2) and
           SignalBatch schema unchanged; XAUUSD / EURUSD / US100 are tradeable, BTCUSD is scan + what-if only
           (no Lucid contract; CB books never open it). Called at every M15 close + 20 s inside
           08:00-20:30 UTC, plus triggered one-symbol checks while a bar forms (labelled level crossed by >= 0.1 ATR,
           forming range >= 1.5 ATR, forming RVOL > 2.0; max 1 per symbol per bar, 40 per day). 150 s timeout, ONE
           attempt, no fallback model. Entry/trigger thinking_level is "high". Symbols in ASIAN_SESSION_CHOP / LOW_LIQUIDITY_DEADZONE are not sent.
           Symbols held by BOTH books are not sent (a book skips a symbol it already holds).
  Filter   BUY/SELL and confidence >= 0.75. Every BUY/SELL (any confidence) is also scored through both exits as a
           what-if (server rules only, no account rules) -> carbon_whatif.jsonl.
  Books    each its own $100k account, binding risk rules (sizing caps, one per symbol, 6 aggregate, re-entry locks,
           -$1,500 daily flatten, trailing-loss guard, 21:45 UTC flat).
             CB-BRACKET stop 1 ATR / target 1 ATR / 90 min, no model exits.
             CB-RUNNER  stop 1 ATR; at MFE +1 ATR lock the stop at +0.5 ATR then trail 0.75 ATR behind the best price;
                        target 2.5 ATR; 150 min; Carbon (thinking "low") may CLOSE only at an M15 close once MFE >= 1 ATR.
  Spend    cost of every Carbon call from usage_metadata: prompt x input price + (candidates + thoughts) x output price,
           price by date. Running total per Dubai day: >= $15 soft (no triggered checks, no runner model exits),
           >= $19 hard (no Carbon calls at all). max_output_tokens caps per call.

Isolation: runs as its own asyncio tasks in the GHA loop (never awaited by the 2-min cycle); reads no other book's
state and writes only state/carbon/*, logs/<day>/carbon_*.jsonl and carbon.json. Every public coroutine catches its
own errors.
"""
from __future__ import annotations
import asyncio, copy, json, logging, math, os, time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .risk import _atomic_write
from .exit_versions import gemini_exit_calls_enabled, gemini_close_tag
from .uniform_exit import close_tag, uniform_exits_enabled, UNIFORM_BANNER, UNIFORM_EXIT_VERSION

log = logging.getLogger("signals.carbon")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
DUBAI = ZoneInfo("Asia/Dubai")
BOOKS = ("CB-BRACKET", "CB-RUNNER")
BOOK_LABELS = {"CB-BRACKET": "CB-BRACKET (Carbon entries, 1 ATR / 1 ATR)",
               "CB-RUNNER": "CB-RUNNER (Carbon entries, 1 ATR / 1 ATR)"}

DEFAULTS = {
    "enabled": True,
    "model": "gemini-4-carbon",
    "entry_thinking": "high",
    "exit_thinking": "low",
    "symbols": ["XAUUSD", "EURUSD", "US100", "BTCUSD"],
    "tradeable_symbols": ["XAUUSD", "EURUSD", "US100", "XAGUSD"],
    "whatif_units": {"BTCUSD": {"tick": 5.0, "point_value": 1.0, "commission": 0.0,
                                "unit": "1 BTC", "flag": "site-only/what-if"}},
    "entry_window_utc": ["08:00", "20:30"],
    "flat_utc": "21:45",
    "reopen_utc": "23:00",
    "scan": "m15_close_plus_20s",
    "scan_offset_seconds": 20,
    "timeout_seconds": 150,
    "triggered_checks": {"level_cross_atr": 0.1, "bar_range_atr": 1.5, "rvol_gt": 2.0,
                         "max_per_symbol_per_bar": 1, "max_per_day": 40, "poll_seconds": 30},
    "min_confidence": 0.75,
    "max_chase_atr": 0.5,
    "sizing": {"risk_usd": 900, "atr_mult": 1.0, "min_contracts": 1, "skip_if_one_contract_risk_gt": 1200,
               "caps": {"US100": 1, "XAUUSD": 1, "EURUSD": 4}, "aggregate_max": 6},
    "books": {
        "CB-BRACKET": {"stop_atr": 1.0, "target_atr": 1.0, "max_hold_min": 90, "model_exits": False},
        "CB-RUNNER": {"stop_atr": 1.0, "target_atr": 1.0, "max_hold_min": 90, "model_exits": False,
                      "lock_at_atr": 1.0, "lock_to_atr": 0.5, "trail_atr": 0.75},
    },
    "risk": {"daily_stop_usd": -1500, "mll_buffer_usd": 300, "one_per_symbol": True,
             "reentry_lock": "next_m15_close", "after_loss_lock_min": 30, "mll_usd": 3000.0,
             "locked_mll_balance": 100100.0},
    "spend_cap": {"soft_usd": 15, "hard_usd": 19,
                  "price_per_mtok": {"until_2026-11-30": [2, 10], "from_2026-12-01": [4, 20]},
                  "max_output_tokens": {"entry": 24000, "exit": 800}},
    "start_balance": 100000.0,
    "check_seconds": 18,
    "slippage_ticks": 1,
    "pending_expiry_seconds": 180,
    "fill_quote_max_age_s": 60,
    "gap_seconds": 180,
    "recent_trades": 20,
    "reset_token": "carbon-v1",
}


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def carbon_cfg(cfg: dict) -> dict:
    return _merge(DEFAULTS, cfg.get("carbon_books") or {})


SITE_ONLY_FLAG = "site-only/what-if"


def tradeable_symbols(cc: dict) -> list[str]:
    return list(cc.get("tradeable_symbols") or [s for s in cc["symbols"] if s != "BTCUSD"])


def is_tradeable(cc: dict, sym: str) -> bool:
    return sym in tradeable_symbols(cc)


def whatif_spec(cfg: dict, cc: dict, sym: str) -> dict | None:
    """Lucid full-size contract, or a site-only what-if unit (BTCUSD). None if the symbol cannot be scored."""
    L = cfg.get("lucid") or {}
    cs = ((L.get("symbols") or {}).get(sym) or {}).get("contracts") or []
    cs = [c for c in cs if c in (L.get("contracts") or {}) and not L["contracts"][c].get("micro")]
    if cs:
        sp = L["contracts"][cs[0]]
        return {"contract": cs[0], "tick": float(sp["tick"]), "point_value": float(sp["point_value"]),
                "commission": float(sp["commission"]), "site_only": False, "flag": None,
                "unit": f"1 {cs[0]}"}
    u = (cc.get("whatif_units") or {}).get(sym)
    if not u:
        return None
    return {"contract": sym, "tick": float(u["tick"]), "point_value": float(u["point_value"]),
            "commission": float(u.get("commission") or 0.0), "site_only": True,
            "flag": u.get("flag") or SITE_ONLY_FLAG, "unit": u.get("unit") or "1 unit"}


# ---------------------------------------------------------------------------------------------- small helpers
def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def _dt(s) -> datetime | None:
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
    n = x / tick
    n = math.ceil(n - 1e-9) if x >= entry else math.floor(n + 1e-9)
    return round(n * tick, 10)


def round_toward(x: float, entry: float, tick: float) -> float:
    """Round a profit-side stop to the tick, never further from price than computed (toward entry)."""
    n = x / tick
    n = math.floor(n + 1e-9) if x >= entry else math.ceil(n - 1e-9)
    return round(n * tick, 10)


def round_adverse(x: float, tick: float, side: int, entering: bool) -> float:
    buying = (side > 0) == entering
    n = x / tick
    n = math.ceil(n - 1e-9) if buying else math.floor(n + 1e-9)
    return round(n * tick, 10)


def next_m15_close(t: datetime) -> datetime:
    ts = int(t.timestamp())
    return datetime.fromtimestamp((ts // 900 + 1) * 900, UTC)


def lucid_day(t: datetime, open_et: str = "18:00") -> date:
    et = t.astimezone(ET)
    return (et + timedelta(days=1)).date() if et.time() >= _hm(open_et) else et.date()


def held_str(sec) -> str:
    s = max(0, int(sec or 0))
    h, rem = divmod(s, 3600)
    m, x = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {x:02d}s" if m else f"{x}s"


# ---------------------------------------------------------------------------------------------- spend / pricing
def price_for(cc: dict, d: date) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for date d from spend_cap.price_per_mtok
    {"until_YYYY-MM-DD": [in, out], "from_YYYY-MM-DD": [in, out]}."""
    pp = (cc.get("spend_cap") or {}).get("price_per_mtok") or {}
    untils, froms = [], []
    for k, v in pp.items():
        kind, _, ds = str(k).partition("_")
        try:
            dd = date.fromisoformat(ds)
        except ValueError:
            continue
        (untils if kind == "until" else froms).append((dd, (float(v[0]), float(v[1]))))
    for dd, p in sorted(untils):
        if d <= dd:
            return p
    best = None
    for dd, p in sorted(froms):
        if d >= dd:
            best = p
    if best:
        return best
    allp = [p for _, p in untils + froms]
    return max(allp) if allp else (4.0, 20.0)


def usage_tokens(usage: dict | None) -> dict:
    u = usage or {}
    return {"input": int(u.get("prompt_token_count") or 0), "output": int(u.get("candidates_token_count") or 0),
            "thoughts": int(u.get("thoughts_token_count") or 0), "total": int(u.get("total_token_count") or 0)}


def call_cost(usage: dict | None, price: tuple[float, float]) -> float:
    t = usage_tokens(usage)
    return round(t["input"] * price[0] / 1e6 + (t["output"] + t["thoughts"]) * price[1] / 1e6, 6)


def spend_day(t: datetime) -> str:
    return t.astimezone(DUBAI).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------------------------- Carbon API call
async def carbon_generate(cc: dict, *, system_text: str, contents, schema, thinking: str, max_output_tokens: int) -> dict:
    """ONE request to the Carbon model (no retry, no fallback), hard timeout (cc['timeout_seconds'], default 150 s). Never raises."""
    t0 = time.monotonic()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return {"ok": False, "error": "GEMINI_API_KEY not set", "latency_s": 0.0, "usage": None, "raw_text": None}
    try:
        from google import genai
        from google.genai import types
        timeout = float(cc["timeout_seconds"])
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(timeout * 1000)))
        conf = types.GenerateContentConfig(
            system_instruction=system_text, response_mime_type="application/json", response_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level=thinking), max_output_tokens=int(max_output_tokens))
        resp = await asyncio.wait_for(client.aio.models.generate_content(model=cc["model"], contents=contents,
                                                                         config=conf), timeout + 2)
        usage = resp.usage_metadata.model_dump(exclude_none=True, mode="json") if resp.usage_metadata else None
        fr = None
        try:
            fr = str(resp.candidates[0].finish_reason) if resp.candidates else None
        except Exception:
            pass
        text = resp.text
        out = {"ok": bool(text), "raw_text": text, "usage": usage, "latency_s": round(time.monotonic() - t0, 3),
               "model_version": getattr(resp, "model_version", None) or cc["model"], "finish_reason": fr}
        if not text:
            out["error"] = f"empty response (finish_reason={fr})"
        return out
    except Exception as e:
        return {"ok": False, "raw_text": None, "usage": None, "latency_s": round(time.monotonic() - t0, 3),
                "error": f"{type(e).__name__}: {str(e)[:300]}"}


# ---------------------------------------------------------------------------------------------- trade mechanics
def exit_rules(cc: dict, book: str) -> dict:
    return dict(cc["books"][book])


def open_trade(p: dict, rules: dict, entry: float, atr: float, tick: float, t: datetime) -> dict:
    sd = p["side"]
    p.update({"entry": entry, "atr": atr, "entry_time": _iso(t), "scan_from": _iso(t), "best": entry,
              "mfe": 0.0, "mae": 0.0, "locked": False, "checks": 0,
              "stop": round_away(entry - sd * float(rules["stop_atr"]) * atr, entry, tick),
              "target": round_away(entry + sd * float(rules["target_atr"]) * atr, entry, tick),
              "deadline": _iso(t + timedelta(minutes=float(rules["max_hold_min"])))})
    p["init_stop"] = p["stop"]
    return p


def runner_update(p: dict, rules: dict, tick: float) -> bool:
    """CB-RUNNER: once MFE >= lock_at_atr, stop = max(entry + lock_to_atr, best - trail_atr) (long; mirrored short).
    Only ever tightens. Returns True when the stop moved."""
    if "lock_at_atr" not in rules:
        return False
    sd, a = p["side"], float(p["atr"])
    if p["mfe"] + 1e-12 < float(rules["lock_at_atr"]) * a:
        return False
    p["locked"] = True
    lock_px = p["entry"] + sd * float(rules["lock_to_atr"]) * a
    trail_px = p["best"] - sd * float(rules["trail_atr"]) * a
    new = max(lock_px, trail_px) if sd > 0 else min(lock_px, trail_px)
    new = round_toward(new, p["entry"], tick)
    if sd * (new - p["stop"]) > 1e-12:
        p["stop"] = new
        return True
    return False


def _excursion(p: dict, fav: float, adv: float) -> None:
    sd = p["side"]
    if sd * (fav - p["best"]) > 0:
        p["best"] = fav
    p["mfe"] = max(float(p.get("mfe") or 0), sd * (fav - p["entry"]), 0.0)
    p["mae"] = max(float(p.get("mae") or 0), -sd * (adv - p["entry"]), 0.0)


def _stop_reason(p: dict) -> str:
    if p.get("locked"):
        return "trail_stop" if abs(p["stop"] - p["init_stop"]) > 0 else "stop_1atr"
    return "stop_1atr"


def step_bars(p: dict, bars: pd.DataFrame | None, rules: dict, tick: float):
    """Walk M1 bars since p['scan_from'] (bars before the fill skipped). Per bar: stop (current level) first,
    then target, then excursion + runner stop update (applies from the next bar). -> (reason, raw_px, t) | None."""
    if bars is None or not len(bars):
        return None
    sd = p["side"]
    start = pd.Timestamp(_dt(p["scan_from"])).floor("min")
    entered = pd.Timestamp(_dt(p["entry_time"])).floor("min")
    last = None
    for ts, row in bars[bars.index >= start].iterrows():
        last = ts
        if ts < entered:
            continue
        o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
        if ts == entered:          # the fill minute: only the part after the fill is known; use the close side
            o = p["entry"]
        stop_hit = (l <= p["stop"]) if sd > 0 else (h >= p["stop"])
        if stop_hit:
            gap = (o <= p["stop"]) if sd > 0 else (o >= p["stop"])
            return _stop_reason(p), (o if gap else p["stop"]), ts.to_pydatetime()
        tgt_hit = (h >= p["target"]) if sd > 0 else (l <= p["target"])
        if tgt_hit:
            gap = (o >= p["target"]) if sd > 0 else (o <= p["target"])
            return "target", (o if gap else p["target"]), ts.to_pydatetime()
        _excursion(p, h if sd > 0 else l, l if sd > 0 else h)
        runner_update(p, rules, tick)
    if last is not None:
        p["scan_from"] = last.isoformat()
    return None


def step_price(p: dict, price: float, now: datetime, rules: dict, tick: float, gap_s: float, in_sess: bool):
    """Live-price check. -> (reason, raw_px) | None. Resting stop fills at its level unless the previous good look
    at the price is older than gap_s (then the live price: gapped through)."""
    sd = p["side"]
    if (price <= p["stop"]) if sd > 0 else (price >= p["stop"]):
        prev = _dt(p.get("last_check_at") or p["entry_time"])
        resting = (now - prev).total_seconds() <= gap_s
        return _stop_reason(p), (p["stop"] if resting else price)
    if (price >= p["target"]) if sd > 0 else (price <= p["target"]):
        return "target", p["target"]
    _excursion(p, price, price)
    runner_update(p, rules, tick)
    if now >= _dt(p["deadline"]):
        return f"time_{int(float(rules['max_hold_min']))}min", price
    if not in_sess:
        return "flat_2145utc", price
    return None


def skip_key(why: str) -> str:
    for pre, k in (("confidence", "confidence<min"), ("no ATR", "no_atr"), ("held", "held_in_book"),
                   ("daily_stop", "daily_stop"), ("window", "window"), ("reentry_lock", "reentry_lock"),
                   ("no same-side", "same_side_after_loss"), ("risk>max", "risk>1200"), ("aggregate", "aggregate_6"),
                   ("trailing", "trailing_guard"), ("no full-size", "no_contract"),
                   ("site-only", "site_only_whatif")):
        if why.startswith(pre):
            return k
    return why[:30]


# ---------------------------------------------------------------------------------------------- one book
class CarbonBook:
    def __init__(self, eng: "CarbonEngine", name: str):
        self.eng, self.name = eng, name
        self.cc, self.L = eng.cc, eng.L
        self.rules = exit_rules(self.cc, name)
        self.path = eng.root / f"{name}.json"
        st = None
        if self.path.exists():
            try:
                st = json.loads(self.path.read_text())
            except Exception:
                st = None
        if st is not None and st.get("reset_token") != self.cc["reset_token"]:
            _atomic_write(eng.root / f"{name}_pre_{self.cc['reset_token']}.json", st)
            st = None
        self.s = st or self.fresh()

    def fresh(self) -> dict:
        sb = float(self.cc["start_balance"])
        rk = self.cc["risk"]
        now = datetime.now(UTC)
        return {"version": 1, "book": self.name, "label": BOOK_LABELS.get(self.name, self.name),
                "reset_token": self.cc["reset_token"], "created_at": _iso(now), "start_balance": sb, "balance": sb,
                "peak_balance": sb, "max_drawdown": 0.0, "highest_close": sb, "mll_floor": sb - float(rk["mll_usd"]),
                "day": {"id": str(lucid_day(now)), "realized": 0.0, "flattened": False},
                "next_id": 1, "positions": [], "pending": [], "last_price": {}, "locks": {},
                "stats": {"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0, "net_pnl": 0.0,
                          "commissions": 0.0, "hold_seconds": 0, "exit_reasons": {}, "signals_seen": 0,
                          "signals_taken": 0, "skips": {}, "r_total": 0.0, "model_exit_checks": 0}}

    # ---- accounting helpers
    def spec(self, c: str) -> dict:
        return self.L["contracts"][c]

    def held(self, sym: str) -> bool:
        return any(p["symbol"] == sym for p in self.s["positions"] + self.s["pending"])

    def used(self) -> int:
        return sum(int(p["qty"]) for p in self.s["positions"] + self.s["pending"])

    def pnl(self, p: dict, price: float) -> float:
        return p["side"] * (price - p["entry"]) * self.spec(p["contract"])["point_value"] * p["qty"]

    def comm_rt(self, p: dict) -> float:
        return 2 * self.spec(p["contract"])["commission"] * p["qty"]

    def open_pnl(self) -> float:
        lp = self.s["last_price"]
        return round(sum(self.pnl(p, lp.get(p["symbol"], p["entry"])) for p in self.s["positions"]), 2)

    def open_stop_risk(self) -> float:
        r = sum(max(0.0, -self.pnl(p, p["stop"])) + self.comm_rt(p) for p in self.s["positions"])
        r += sum(float(p.get("new_risk") or 0) for p in self.s["pending"])
        return r

    def roll_day(self, now: datetime) -> None:
        d = str(lucid_day(now))
        if self.s["day"]["id"] == d:
            return
        s, rk = self.s, self.cc["risk"]
        s["highest_close"] = max(s["highest_close"], s["balance"])
        s["mll_floor"] = max(s["mll_floor"], min(s["highest_close"] - float(rk["mll_usd"]),
                                                 float(rk["locked_mll_balance"])))
        self.eng.event(now, "day_roll", book=self.name, day=d, balance=s["balance"], mll_floor=s["mll_floor"])
        s["day"] = {"id": d, "realized": 0.0, "flattened": False}

    def _skip(self, key: str) -> None:
        sk = self.s["stats"]["skips"]
        sk[key] = sk.get(key, 0) + 1

    # ---- entry rules (binding)
    def size(self, sym: str, contract: str, atr: float) -> tuple[int | None, str | None, float]:
        sz = self.cc["sizing"]
        sp = self.spec(contract)
        per = float(sz.get("atr_mult", 1.0)) * float(self.rules["stop_atr"]) * atr * sp["point_value"]
        if per <= 0:
            return None, "bad ATR", 0.0
        if per > float(sz["skip_if_one_contract_risk_gt"]):
            return None, f"risk>max: 1 {contract} at 1 ATR risks ${per:,.0f} > ${sz['skip_if_one_contract_risk_gt']:g}", per
        q = max(int(sz.get("min_contracts", 1)), int(math.floor(float(sz["risk_usd"]) / per + 1e-9)))
        q = min(q, int((sz.get("caps") or {}).get(sym, q)))
        room = int(sz["aggregate_max"]) - self.used()
        if room < 1:
            return None, f"aggregate: {sz['aggregate_max']} contracts already open", per
        return min(q, room), None, per

    def lock_reason(self, sym: str, side: int, now: datetime) -> str | None:
        lk = (self.s.get("locks") or {}).get(sym)
        if not lk:
            return None
        if now < _dt(lk["until"]):
            return f"reentry_lock until {lk['until']}" + (" (after a loss)" if lk.get("loss") else "")
        if lk.get("loss") and lk.get("side") == side and lk.get("day") == str(lucid_day(now)):
            return "no same-side re-entry after a loss (this session)"
        return None

    def guard(self, new_risk: float) -> str | None:
        room = self.s["balance"] - self.s["mll_floor"]
        osr = self.open_stop_risk()
        buf = float(self.cc["risk"]["mll_buffer_usd"])
        if room - osr - new_risk < buf:
            return (f"trailing guard: room ${room:,.0f} - open stop risk ${osr:,.0f} - new ${new_risk:,.0f} "
                    f"< ${buf:,.0f}")
        return None

    def intake(self, sig: dict, now: datetime) -> dict | None:
        """Queue a pending entry for a Carbon signal or log why not. Returns the pending row or None."""
        self.roll_day(now)
        if sig.get("action") not in ("BUY", "SELL"):
            return None
        self.s["stats"]["signals_seen"] += 1
        sym, side = sig["symbol"], (1 if sig["action"] == "BUY" else -1)
        conf = sig.get("confidence")
        why = None
        if conf is None or float(conf) < float(self.cc["min_confidence"]) - 1e-9:
            why = f"confidence<min: {conf} < {self.cc['min_confidence']}"
        elif not sig.get("atr") or float(sig["atr"]) <= 0:
            why = "no ATR"
        elif self.held(sym):
            why = "held: already in a position on this symbol"
        elif self.s["day"].get("flattened"):
            why = "daily_stop: flattened at the daily loss limit (until the next session)"
        elif not self.eng.entries_allowed(now):
            why = "window: outside the entry window / session"
        if why is None:
            why = self.lock_reason(sym, side, now)
        from .other_venue import get_hub, is_other_venue_symbol
        if why is None and is_other_venue_symbol(self.eng.cfg, sym):
            if self.eng.ov_held(self.name, sym):
                why = "held: already in an Other-venue position on this symbol"
            else:
                hub = get_hub(self.eng.cfg)
                if hub is None:
                    why = "other-venue hub not started"
                else:
                    quote = sig.get("live_price") or sig.get("entry")
                    r = hub.intake_signal(self.name, sig, now, quote=quote)
                    hub.persist(now)
                    ev = {"book": self.name, "symbol": sym, "action": sig["action"],
                          "confidence": conf, "atr": sig.get("atr"), "kind": sig.get("kind")}
                    self.eng.event(now, "other_venue", result={"skipped": (r or {}).get("skipped"),
                                                               "qty": (r or {}).get("qty"),
                                                               "entry": (r or {}).get("entry")}, **ev)
                    return r if r and not (r or {}).get("skipped") else None
        if why is None and not is_tradeable(self.cc, sym):
            why = f"site-only/what-if: {sym} is not a Lucid contract (what-if only)"
        contract = self.eng.contract_for(sym)
        q, per = None, 0.0
        if why is None:
            if contract is None:
                why = "no full-size contract"
            else:
                q, why, per = self.size(sym, contract, float(sig["atr"]))
        new_risk = 0.0
        if why is None:
            new_risk = per * q + 2 * self.spec(contract)["commission"] * q
            why = self.guard(new_risk)
        ev = {"book": self.name, "symbol": sym, "action": sig["action"], "confidence": conf, "atr": sig.get("atr"),
              "kind": sig.get("kind"), "signal_at": sig.get("signal_at"), "signal_entry": sig.get("entry")}
        if why is not None:
            self._skip(skip_key(why))
            self.eng.event(now, "skip", reason=why, **ev)
            return None
        pe = {"symbol": sym, "side": side, "contract": contract, "qty": q, "atr": float(sig["atr"]),
              "confidence": float(conf), "signal_entry": sig.get("entry"), "signal_at": sig.get("signal_at"),
              "queued_at": _iso(now), "kind": sig.get("kind"), "new_risk": round(new_risk, 2),
              "rationale": (sig.get("rationale") or "")[:300], "bar": sig.get("bar")}
        self.s["pending"].append(pe)
        self.eng.event(now, "queued", contract=contract, qty=q, **ev)
        return pe

    # ---- fills / exits
    def fill_pendings(self, quotes: dict, now: datetime) -> list[dict]:
        opened, keep = [], []
        for pe in self.s["pending"]:
            q = quotes.get(pe["symbol"])
            prob = self.eng.quote_problem(q)
            if prob is not None:
                if (now - _dt(pe["queued_at"])).total_seconds() > float(self.cc["pending_expiry_seconds"]):
                    self._skip("pending_expired")
                    self.eng.event(now, "expired", book=self.name, symbol=pe["symbol"], reason=prob)
                else:
                    keep.append(pe)
                continue
            sp = self.spec(pe["contract"])
            tick = float(sp["tick"])
            entry = round_adverse(float(q["price"]) + pe["side"] * int(self.cc["slippage_ticks"]) * tick, tick,
                                  pe["side"], True)
            ref = pe.get("signal_entry")
            if ref is not None and pe["side"] * (entry - float(ref)) > float(self.cc["max_chase_atr"]) * pe["atr"]:
                self._skip("chase")
                self.eng.event(now, "chase_skip", book=self.name, symbol=pe["symbol"], fill=entry, signal_entry=ref,
                               atr=pe["atr"])
                continue
            et = _dt(q.get("fetched_at")) or now
            p = {k: v for k, v in pe.items() if k != "new_risk"}
            p["id"] = self.s["next_id"]
            p["book"] = self.name
            open_trade(p, self.rules, entry, pe["atr"], tick, et)
            comm = round(sp["commission"] * p["qty"], 2)
            p["risk_usd"] = round(abs(entry - p["stop"]) * sp["point_value"] * p["qty"] + 2 * comm, 2)
            p["target_usd"] = round(abs(p["target"] - entry) * sp["point_value"] * p["qty"] - 2 * comm, 2)
            p["quote_price"], p["quote_source"] = float(q["price"]), q.get("source")
            self.s["next_id"] += 1
            self.s["balance"] = round(self.s["balance"] - comm, 2)
            self.s["stats"]["commissions"] = round(self.s["stats"]["commissions"] + comm, 2)
            self.s["stats"]["signals_taken"] += 1
            self.s["positions"].append(p)
            self.s["last_price"][p["symbol"]] = float(q["price"])
            self.eng.event(now, "open", book=self.name, **{k: p[k] for k in (
                "id", "symbol", "contract", "qty", "entry", "stop", "target", "deadline", "entry_time", "confidence",
                "atr", "risk_usd", "kind", "quote_source")}, side=_side_word(p["side"]))
            opened.append(p)
        self.s["pending"] = keep
        return opened

    def slip(self, p: dict, price: float) -> float:
        tick = float(self.spec(p["contract"])["tick"])
        return round_adverse(float(price) - p["side"] * int(self.cc["slippage_ticks"]) * tick, tick, p["side"], False)

    def close(self, p: dict, price: float, t: datetime, reason: str, extra: dict | None = None) -> dict:
        sp = self.spec(p["contract"])
        comm = round(sp["commission"] * p["qty"], 2)
        gross = round(p["side"] * (price - p["entry"]) * sp["point_value"] * p["qty"], 2)
        net = round(gross - 2 * comm, 2)
        s, st = self.s, self.s["stats"]
        s["balance"] = round(s["balance"] + gross - comm, 2)
        s["day"]["realized"] = round(float(s["day"]["realized"]) + net, 2)
        st["trades"] += 1
        st["commissions"] = round(st["commissions"] + comm, 2)
        st["net_pnl"] = round(st["net_pnl"] + net, 2)
        if net > 0:
            st["wins"] += 1; st["gross_win"] = round(st["gross_win"] + net, 2)
        else:
            st["losses"] += 1; st["gross_loss"] = round(st["gross_loss"] + net, 2)
        R = round(net / p["risk_usd"], 3) if p.get("risk_usd") else None
        st["r_total"] = round(st["r_total"] + (R or 0), 3)
        hold = max(0, int((t - _dt(p["entry_time"])).total_seconds()))
        st["hold_seconds"] += hold
        st["exit_reasons"][reason] = st["exit_reasons"].get(reason, 0) + 1
        s["peak_balance"] = max(float(s["peak_balance"]), s["balance"])
        s["max_drawdown"] = round(min(float(s["max_drawdown"]), s["balance"] - s["peak_balance"]), 2)
        s["positions"] = [q for q in s["positions"] if q["id"] != p["id"]]
        until = next_m15_close(t)
        lk = {"until": _iso(until), "loss": net < 0, "side": p["side"], "day": str(lucid_day(t)), "exit_t": _iso(t)}
        if net < 0:
            lk["until"] = _iso(max(until, t + timedelta(minutes=float(self.cc["risk"]["after_loss_lock_min"]))))
        s["locks"][p["symbol"]] = lk
        atr = float(p.get("atr") or 0)
        rec = {"event": "close", "book": self.name, "t_exit": _iso(t), "t_exit_dubai": t.astimezone(DUBAI).isoformat(),
               "id": p["id"], "symbol": p["symbol"], "contract": p["contract"], "qty": p["qty"],
               "side": _side_word(p["side"]), "entry_time": p["entry_time"], "entry": p["entry"],
               "init_stop": p.get("init_stop"), "stop": p["stop"], "target": p["target"], "exit": round(price, 6),
               "reason": reason, "stopped": "stop" in reason, "gross": gross, "commission": round(2 * comm, 2),
               "net": net, "R": R, "risk_usd": p.get("risk_usd"), "hold_seconds": hold, "held": held_str(hold),
               "balance_after": s["balance"], "confidence": p.get("confidence"), "atr": atr, "kind": p.get("kind"),
               "signal_at": p.get("signal_at"), "locked": p.get("locked"),
               "mfe_atr": round(p.get("mfe", 0) / atr, 3) if atr else None,
               "mae_atr": round(p.get("mae", 0) / atr, 3) if atr else None,
               "checks": p.get("checks", 0), "model": self.cc["model"], **(extra or {})}
        rec["exit_prompt_version"] = close_tag(self.eng.cfg)
        rec["exit_mode"] = close_tag(self.eng.cfg)
        self.eng.jsonl("carbon_trades.jsonl", t, rec)
        return rec

    def manage(self, quotes: dict, m1: dict, now: datetime) -> list[dict]:
        out = []
        in_sess = self.eng.in_session(now)
        gap_s = float(self.cc["gap_seconds"])
        for p in list(self.s["positions"]):
            tick = float(self.spec(p["contract"])["tick"])
            hit = step_bars(p, (m1 or {}).get(p["symbol"]), self.rules, tick)
            if hit:
                reason, px, ts = hit
                out.append(self.close(p, self.slip(p, px), max(ts, _dt(p["entry_time"])), reason, {"trigger": "m1_path"}))
                continue
            q = quotes.get(p["symbol"])
            if self.eng.quote_problem(q) is not None:
                lp = self.s["last_price"].get(p["symbol"])
                if lp is not None and (now >= _dt(p["deadline"]) or not in_sess):
                    r = f"time_{int(float(self.rules['max_hold_min']))}min" if now >= _dt(p["deadline"]) else "flat_2145utc"
                    out.append(self.close(p, self.slip(p, lp), now, r, {"trigger": "last_price (quote unusable)"}))
                continue
            price = float(q["price"])
            self.s["last_price"][p["symbol"]] = price
            p["checks"] = p.get("checks", 0) + 1
            r = step_price(p, price, now, self.rules, tick, gap_s, in_sess)
            p["last_check_at"] = _iso(now)
            if r:
                out.append(self.close(p, self.slip(p, r[1]), now, r[0], {"trigger": "live_price", "live_price": price}))
        out += self.daily_stop(quotes, now)
        return out

    def daily_stop(self, quotes: dict, now: datetime) -> list[dict]:
        """Day P&L (realized + open) <= daily_stop_usd -> flatten everything, no entries until the next session."""
        self.roll_day(now)
        day = float(self.s["day"]["realized"]) + self.open_pnl()
        if self.s["day"].get("flattened") or day > float(self.cc["risk"]["daily_stop_usd"]):
            return []
        out = []
        for p in list(self.s["positions"]):
            lp = (quotes.get(p["symbol"]) or {}).get("price") or self.s["last_price"].get(p["symbol"], p["entry"])
            out.append(self.close(p, self.slip(p, float(lp)), now, "daily_stop_flatten", {"day_pnl": round(day, 2)}))
        self.s["pending"] = []
        self.s["day"]["flattened"] = True
        self.eng.event(now, "daily_stop", book=self.name, day_pnl=round(day, 2))
        return out

    # ---- site
    def recent_trades(self) -> list[dict]:
        rows = [r for r in self.eng.read_logs("carbon_trades.jsonl") if r.get("book") == self.name
                and r.get("t_exit", "") >= self.s["created_at"][:19]]
        rows.sort(key=lambda r: r["t_exit"])
        return rows[-int(self.cc["recent_trades"]):][::-1]

    def view(self, now: datetime) -> dict:
        s, st = self.s, self.s["stats"]
        n = st["trades"]
        op = self.open_pnl()
        pos = []
        for p in s["positions"]:
            lp = s["last_price"].get(p["symbol"], p["entry"])
            held = (now - _dt(p["entry_time"])).total_seconds()
            pos.append({k: p.get(k) for k in ("id", "symbol", "contract", "qty", "entry", "entry_time", "stop",
                                              "init_stop", "target", "deadline", "confidence", "risk_usd", "locked",
                                              "kind")}
                       | {"side": _side_word(p["side"]), "last_price": lp, "held": held_str(held),
                          "unrealized": round(self.pnl(p, lp), 2),
                          "open_R": round((self.pnl(p, lp) - self.comm_rt(p)) / p["risk_usd"], 2) if p.get("risk_usd") else None,
                          "mfe_atr": round(p.get("mfe", 0) / p["atr"], 2) if p.get("atr") else None})
        gw, gl = st["gross_win"], st["gross_loss"]
        return {"book": self.name, "label": s.get("label") or BOOK_LABELS.get(self.name), "since": s["created_at"],
                "rules": self.rules,
                "account": {"start_balance": s["start_balance"], "balance": s["balance"],
                            "equity": round(s["balance"] + op, 2), "net_pnl": st["net_pnl"], "open_pnl": op,
                            "trades": n, "wins": st["wins"], "losses": st["losses"],
                            "win_rate": round(st["wins"] / n, 3) if n else None,
                            "profit_factor": round(gw / -gl, 2) if gl < 0 else None,
                            "r_total": st["r_total"], "r_avg": round(st["r_total"] / n, 3) if n else None,
                            "max_drawdown": s["max_drawdown"], "day_realized": s["day"]["realized"],
                            "day_flattened": s["day"]["flattened"], "mll_floor": s["mll_floor"],
                            "mll_room": round(s["balance"] - s["mll_floor"], 2),
                            "stopped": sum(v for k, v in st["exit_reasons"].items() if "stop" in k),
                            "exit_reasons": st["exit_reasons"], "open_positions": len(s["positions"]),
                            "pending": len(s["pending"]), "signals_seen": st["signals_seen"],
                            "signals_taken": st["signals_taken"], "skips": st["skips"],
                            "model_exit_checks": st.get("model_exit_checks", 0), "commissions": st["commissions"]},
                "open_positions": pos, "recent_trades": self.recent_trades()}


# ---------------------------------------------------------------------------------------------- default I/O
async def _default_snapshot(sym: str, cfg: dict, now: datetime):
    from .exit_manager import market_snapshot
    return await market_snapshot(sym, cfg, now)


async def _default_render(payloads: list[dict]) -> dict[str, bytes]:
    from .renderer import render_all
    return await render_all(payloads)


async def _default_quotes(eng: "CarbonEngine", symbols, now: datetime):
    from .live_price import enrich_live_prices, fill_quote_from_context
    payloads = []
    for s in symbols:
        scfg = (eng.cfg.get("symbols") or {}).get(s) or {}
        payloads.append({"symbol": s, "source": "dukascopy" if scfg.get("dukascopy") else None,
                         "context": {"digits": int(scfg.get("digits") or 5)}})
    info = await enrich_live_prices(payloads, eng.cfg, now)
    q = {p["symbol"]: fill_quote_from_context(p["symbol"], p["context"]) for p in payloads}
    ctx = {p["symbol"]: p["context"] for p in payloads}
    return q, (info or {}).get("m1") or {}, ctx


# ---------------------------------------------------------------------------------------------- engine
class CarbonEngine:
    def __init__(self, cfg: dict, io_lock: asyncio.Lock | None = None, *, state_dir=None, logs_dir=None,
                 site_dir=None, snapshot=None, render=None, api=None, quotes=None):
        self.cfg = cfg
        self.cc = carbon_cfg(cfg)
        self.L = cfg["lucid"]
        self.tz = ZoneInfo(cfg["general"]["session_tz"])
        self.root = Path(state_dir or cfg["general"]["state_dir"]) / "carbon"
        self.logs_dir = Path(logs_dir or cfg["general"]["logs_dir"])
        self.site_dir = Path(site_dir or (cfg.get("site") or {}).get("dir") or self.root.parent.parent)
        self.root.mkdir(parents=True, exist_ok=True)
        self.io_lock = io_lock or asyncio.Lock()
        self.mu = asyncio.Lock()        # book state
        self.call_mu = asyncio.Lock()   # one Carbon call at a time
        self._snapshot = snapshot or _default_snapshot
        self._render = render or _default_render
        self._api = api or carbon_generate
        self._quotes = quotes or (lambda syms, now: _default_quotes(self, syms, now))
        self.e = self._load_engine()
        self.books = {b: CarbonBook(self, b) for b in BOOKS}
        self.busy = False

    # ---- state
    def _load_engine(self) -> dict:
        p = self.root / "engine.json"
        try:
            st = json.loads(p.read_text()) if p.exists() else None
        except Exception:
            st = None
        if st is None or st.get("reset_token") != self.cc["reset_token"]:
            keep_spend = (st or {}).get("spend") or {}     # daily spend caps survive an account reset
            if st is not None:
                _atomic_write(self.root / f"engine_pre_{self.cc['reset_token']}.json", st)
            st = {"reset_token": self.cc["reset_token"], "created_at": _iso(datetime.now(UTC)), "spend": keep_spend,
                  "last_scan_bar": None, "triggers": {"day": None, "n": 0, "per_bar": {}}, "cache": {},
                  "whatif": [], "next_vid": 1, "last_call": None, "first_call": None, "calls_total": 0,
                  "last_scan": None}
        return st

    def jsonl(self, name: str, t: datetime, obj: dict) -> None:
        d = self.logs_dir / t.astimezone(self.tz).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a") as f:
            f.write(json.dumps(obj, default=str, separators=(",", ":")) + "\n")

    def event(self, t: datetime, _event: str, **kw) -> None:
        self.jsonl("carbon_events.jsonl", t, {"t": _iso(t), "event": _event, **kw})

    def read_logs(self, name: str) -> list[dict]:
        rows = []
        for f in sorted(self.logs_dir.glob(f"*/{name}")):
            for line in f.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        return rows

    # ---- calendar
    def in_session(self, t: datetime) -> bool:
        """Lucid week (Sun 18:00 ET -> Fri) minus the daily flat window 21:45 -> 23:00 UTC."""
        if lucid_day(t).weekday() >= 5:
            return False
        u = t.astimezone(UTC).time()
        return not (_hm(self.cc["flat_utc"]) <= u < _hm(self.cc["reopen_utc"]))

    def in_window(self, t: datetime) -> bool:
        a, b = (_hm(x) for x in self.cc["entry_window_utc"])
        u = t.astimezone(UTC).time()
        return a <= u <= b

    def entries_allowed(self, t: datetime) -> bool:
        return self.in_window(t) and self.in_session(t)

    def contract_for(self, sym: str) -> str | None:
        cs = ((self.L.get("symbols") or {}).get(sym) or {}).get("contracts") or []
        cs = [c for c in cs if c in self.L["contracts"] and not self.L["contracts"][c].get("micro")]
        return cs[0] if cs else None

    def quote_problem(self, q: dict | None) -> str | None:
        from .live_price import fill_quote_problem
        srcs = tuple(self.L.get("fill_quote_sources") or ("dukascopy:tick", "firmtape:NQ"))
        return fill_quote_problem(q, sources=srcs, max_age_s=float(self.cc["fill_quote_max_age_s"]))

    # ---- spend
    def spend_today(self, now: datetime) -> dict:
        d = spend_day(now)
        sp = self.e["spend"].setdefault(d, {"usd": 0.0, "calls": 0, "failed": 0, "by_kind": {},
                                            "tokens": {"input": 0, "output": 0, "thoughts": 0}})
        for k in sorted(self.e["spend"])[:-14]:
            self.e["spend"].pop(k, None)
        return sp

    def gate(self, kind: str, now: datetime) -> str | None:
        """None = the call may go out; else why not. entry = M15 scan; trigger / exit stop at the soft cap."""
        usd = self.spend_today(now)["usd"]
        cap = self.cc["spend_cap"]
        if usd >= float(cap["hard_usd"]):
            return f"hard spend cap: ${usd:.2f} >= ${cap['hard_usd']} today (Dubai)"
        if kind in ("trigger", "exit") and usd >= float(cap["soft_usd"]):
            return f"soft spend cap: ${usd:.2f} >= ${cap['soft_usd']} today (Dubai)"
        return None

    def book_call(self, kind: str, r: dict, now: datetime, **extra) -> dict:
        price = price_for(self.cc, now.astimezone(DUBAI).date())
        cost = call_cost(r.get("usage"), price) if r.get("usage") else 0.0
        tok = usage_tokens(r.get("usage"))
        sp = self.spend_today(now)
        sp["usd"] = round(sp["usd"] + cost, 6)
        sp["calls"] += 1
        sp["failed"] += 0 if r.get("ok") else 1
        bk = sp["by_kind"].setdefault(kind, {"calls": 0, "usd": 0.0})
        bk["calls"] += 1
        bk["usd"] = round(bk["usd"] + cost, 6)
        for k in ("input", "output", "thoughts"):
            sp["tokens"][k] += tok[k]
        rec = {"t": _iso(now), "t_dubai": now.astimezone(DUBAI).isoformat(), "kind": kind, "model": self.cc["model"],
               "model_version": r.get("model_version"), "thinking": extra.pop("thinking", None),
               "ok": bool(r.get("ok")), "error": r.get("error"), "latency_s": r.get("latency_s"),
               "finish_reason": r.get("finish_reason"), "tokens": tok, "thoughts_token_count": tok["thoughts"],
               "price_per_mtok": list(price), "cost_usd": cost, "spend_today_usd": round(sp["usd"], 4), **extra}
        self.jsonl("carbon_calls.jsonl", now, rec)
        self.e["last_call"] = rec
        if self.e.get("first_call") is None:
            self.e["first_call"] = rec
        self.e["calls_total"] = int(self.e.get("calls_total") or 0) + 1
        log.info("carbon %s call: ok=%s %.1fs tokens=%s cost=$%.4f today=$%.2f %s", kind, rec["ok"],
                 rec["latency_s"] or 0, tok, cost, sp["usd"], rec["error"] or "")
        return rec

    # ---- entries
    def symbols_to_send(self) -> list[str]:
        """A symbol held by BOTH books has nobody to trade it; anything else is sent."""
        return [s for s in self.cc["symbols"] if not all(b.held(s) for b in self.books.values())]

    def _cache_payload(self, p: dict) -> None:
        c = p.get("context") or {}
        atr = float(c.get("atr") or 0)
        price = c.get("live_price") if c.get("live_price") is not None else c.get("last_close")
        lv = []
        for x in c.get("levels") or p.get("levels") or []:
            lab = str(x.get("label") or "")
            if x.get("price") is not None and lab and lab != "S/R":
                lv.append([lab, float(x["price"])])
        for k in ("poc", "vah", "val"):
            if c.get(k):
                lv.append([k.upper(), float(c[k])])
        g = c.get("gex") or {}
        for side, name in (("call_walls", "Call Wall"), ("put_walls", "Put Wall")):
            for w in g.get(side) or []:
                if w.get("price") is not None:
                    lv.append([f"{name} #{w.get('rank', 1)}", float(w["price"])])
        vols = [float(v.get("value") or 0) for v in (p.get("volume") or [])][-20:]
        self.e["cache"][p["symbol"]] = {
            "bar": c.get("last_closed_bar_utc"), "atr": atr, "price": price,
            "levels": [[l, px, (1 if price is not None and price >= px else -1)] for l, px in lv],
            "vol_avg": (sum(vols) / len(vols)) if vols else None,
            "blocked": bool(c.get("session_blocks_automation")), "session": c.get("session")}

    async def _snapshots(self, syms: list[str], now: datetime) -> dict[str, dict]:
        got = await asyncio.gather(*[self._snapshot(s, self.cfg, now) for s in syms], return_exceptions=True)
        out = {}
        for s, r in zip(syms, got):
            if isinstance(r, BaseException) or not r:
                log.warning("carbon snapshot %s failed: %s", s, r)
                continue
            out[s] = r[0]
        return out

    def ov_held(self, book: str, sym: str) -> bool:
        from .other_venue import get_hub
        hub = get_hub(self.cfg)
        return bool(hub and hub.held(book, sym))

    async def scan(self, now: datetime | None = None, *, kind: str = "entry", symbols: list[str] | None = None,
                   trigger: dict | None = None, snaps=None, pngs=None, snapshot_meta=None) -> dict:
        """One Carbon entry call (M15 scan: all eligible symbols; trigger: one symbol). Never raises."""
        try:
            now = now or datetime.now(UTC)
            async with self.call_mu:
                return await self._scan(now, kind, symbols, trigger, snaps=snaps, pngs=pngs,
                                        snapshot_meta=snapshot_meta)
        except Exception as e:
            log.exception("carbon scan failed (other books unaffected)")
            return {"error": f"{type(e).__name__}: {e}"}

    async def _scan(self, now: datetime, kind: str, symbols, trigger, snaps=None, pngs=None,
                    snapshot_meta=None) -> dict:
        from .gemini_client import SignalBatch, build_contents, system_prompt
        from .validator import parse_raw
        syms = symbols or self.cc["symbols"]
        if snaps is None:
            snaps = await self._snapshots(list(syms), now)
        else:
            snaps = {s: snaps[s] for s in (syms if symbols else snaps) if s in snaps}
        for p in snaps.values():
            self._cache_payload(p)
        if kind == "entry":
            self.e["last_scan_bar"] = next(iter(snaps.values()))["context"].get("last_closed_bar_utc") if snaps else None
        out = {"kind": kind, "t": _iso(now), "trigger": trigger}
        if snapshot_meta:
            out.update({k: snapshot_meta.get(k) for k in
                        ("snapshot_id", "snapshot_hash", "bar_close_ts", "snapshot_ts")})
        if not self.entries_allowed(now):
            out["skipped"] = "outside entry window / session"
            return out
        send = [s for s in self.symbols_to_send() if s in snaps and s in syms]
        blocked = [s for s in send if (snaps[s]["context"] or {}).get("session_blocks_automation")]
        send = [s for s in send if s not in blocked]
        out.update({"sent": send, "blocked_session": blocked})
        if not send:
            out["skipped"] = "no symbol to send"
            self.e["last_scan"] = out
            return out
        why = self.gate(kind if kind != "entry" else "entry", now)
        if why:
            out["skipped"] = why
            self.event(now, "call_blocked", kind=kind, reason=why)
            self.e["last_scan"] = out
            return out
        payloads = []
        for s in send:
            p = snaps[s]
            p["page_key"] = f"carbon-{s}"
            payloads.append(p)
        t_r = time.monotonic()
        if pngs is None:
            pngs = await self._render(payloads)
        else:
            missing = [p for p in payloads if p["symbol"] not in pngs]
            if missing:
                pngs = {**pngs, **(await self._render(missing) or {})}
        render_ms = round((time.monotonic() - t_r) * 1000)
        r = await self._api(self.cc, system_text=system_prompt(self.cfg, send), contents=build_contents(payloads, pngs),
                            schema=SignalBatch, thinking=self.cc["entry_thinking"],
                            max_output_tokens=int(self.cc["spend_cap"]["max_output_tokens"]["entry"]))
        t_done = now + timedelta(seconds=float(r.get("latency_s") or 0) + render_ms / 1000.0)
        obj, perr = parse_raw(r["raw_text"]) if r.get("raw_text") else (None, r.get("error"))
        crec = self.book_call(kind, r, t_done, thinking=self.cc["entry_thinking"], symbols=send, render_ms=render_ms,
                              parse_error=perr, trigger=trigger)
        out.update({"ok": crec["ok"], "latency_s": crec["latency_s"], "cost_usd": crec["cost_usd"],
                    "tokens": crec["tokens"], "error": crec["error"] or perr,
                    "finish_reason": crec.get("finish_reason"), "answer_ts": _iso(t_done)})
        sigs = []
        by = {}
        for s in (obj or {}).get("signals") or []:
            if isinstance(s, dict) and s.get("symbol"):
                by[str(s["symbol"]).upper().replace("/", "").strip()] = s
        async with self.mu:
            for sym in send:
                m = by.get(sym) or {}
                ctx = snaps[sym]["context"]
                act = str(m.get("action") or "HOLD").upper()
                try:
                    conf = float(m.get("confidence")) if m.get("confidence") is not None else None
                except (TypeError, ValueError):
                    conf = None
                ref = m.get("entry") or ctx.get("live_price") or ctx.get("last_close")
                sig = {"symbol": sym, "action": act if act in ("BUY", "SELL", "HOLD") else "HOLD", "confidence": conf,
                       "entry": float(ref) if ref else None, "atr": ctx.get("atr"), "kind": kind,
                       "thinking_level": self.cc["entry_thinking"],
                       "signal_at": _iso(t_done), "bar": ctx.get("last_closed_bar_utc"), "session": ctx.get("session"),
                       "rationale": m.get("rationale"), "level_event": m.get("level_event"),
                       "live_price_at_chart": ctx.get("live_price"), "missing": not m}
                self.jsonl("carbon_signals.jsonl", t_done, {"t": _iso(t_done), "model": self.cc["model"],
                                                             "trigger": trigger, **sig})
                sigs.append(sig)
                if sig["action"] in ("BUY", "SELL"):
                    self.add_whatif(sig, t_done)
                    for b in self.books.values():
                        b.intake(sig, t_done)
            await self._persist(t_done)
        out["signals"] = [{k: s[k] for k in ("symbol", "action", "confidence", "thinking_level")} for s in sigs]
        out["thinking_level"] = self.cc["entry_thinking"]
        self.e["last_scan"] = out
        if any(b.s["pending"] for b in self.books.values()) or any(not w.get("entry") for w in self.e["whatif"]):
            await self.tick(t_done)
        return out

    # ---- what-if (every BUY/SELL, any confidence, server exits only, no account rules)
    def add_whatif(self, sig: dict, now: datetime) -> None:
        sp = whatif_spec(self.cfg, self.cc, sig["symbol"])
        if sp is None or not sig.get("atr"):
            return
        for b in BOOKS:
            self.e["whatif"].append({"vid": self.e["next_vid"], "exit": b, "symbol": sig["symbol"],
                                     "side": 1 if sig["action"] == "BUY" else -1, "contract": sp["contract"],
                                     "tick": sp["tick"], "point_value": sp["point_value"],
                                     "commission": sp["commission"], "site_only": bool(sp["site_only"]),
                                     "flag": sp["flag"], "unit": sp["unit"],
                                     "atr": float(sig["atr"]), "confidence": sig.get("confidence"),
                                     "signal_entry": sig.get("entry"), "signal_at": sig.get("signal_at"),
                                     "kind": sig.get("kind"), "thinking_level": sig.get("thinking_level"),
                                     "queued_at": _iso(now), "entry": None})
            self.e["next_vid"] += 1

    def _whatif_step(self, quotes: dict, m1: dict, now: datetime) -> None:
        keep = []
        in_sess = self.in_session(now)
        for w in self.e["whatif"]:
            sp = whatif_spec(self.cfg, self.cc, w["symbol"]) or {
                "tick": w.get("tick"), "point_value": w.get("point_value"),
                "commission": w.get("commission") or 0.0, "contract": w.get("contract")}
            if sp.get("tick") is None or sp.get("point_value") is None:
                continue
            tick = float(sp["tick"])
            rules = exit_rules(self.cc, w["exit"])
            q = quotes.get(w["symbol"])
            if w.get("entry") is None:
                if self.quote_problem(q) is not None:
                    if (now - _dt(w["queued_at"])).total_seconds() <= float(self.cc["pending_expiry_seconds"]):
                        keep.append(w)
                    continue
                e = round_adverse(float(q["price"]) + w["side"] * tick, tick, w["side"], True)
                open_trade(w, rules, e, w["atr"], tick, _dt(q.get("fetched_at")) or now)
                one = float(rules["stop_atr"]) * w["atr"] * sp["point_value"]
                w["qty"] = 1
                w["risk_usd"] = round(abs(e - w["stop"]) * sp["point_value"] + 2 * sp["commission"], 2)
                w["chase_atr"] = (round(w["side"] * (e - float(w["signal_entry"])) / w["atr"], 3)
                                  if w.get("signal_entry") else None)
                w["risk_1c_usd"] = round(one, 2)
                keep.append(w)
                continue
            hit = step_bars(w, (m1 or {}).get(w["symbol"]), rules, tick)
            res = None
            if hit:
                res = (hit[0], hit[1], max(hit[2], _dt(w["entry_time"])))
            elif self.quote_problem(q) is None:
                r = step_price(w, float(q["price"]), now, rules, tick, float(self.cc["gap_seconds"]), in_sess)
                w["last_check_at"] = _iso(now)
                if r:
                    res = (r[0], r[1], now)
            if not res:
                keep.append(w)
                continue
            px = round_adverse(float(res[1]) - w["side"] * tick, tick, w["side"], False)
            net = round(w["side"] * (px - w["entry"]) * sp["point_value"] - 2 * sp["commission"], 2)
            self.jsonl("carbon_whatif.jsonl", res[2], {
                "t_exit": _iso(res[2]), "vid": w["vid"], "exit_rules": w["exit"], "symbol": w["symbol"],
                "side": _side_word(w["side"]), "confidence": w.get("confidence"),
                "passes_filter": (w.get("confidence") or 0) >= float(self.cc["min_confidence"]) - 1e-9,
                "kind": w.get("kind"), "signal_at": w.get("signal_at"), "entry_time": w["entry_time"],
                "entry": w["entry"], "exit": px, "reason": res[0], "net_1c": net,
                "R": round(net / w["risk_usd"], 3) if w.get("risk_usd") else None, "atr": w["atr"],
                "chase_atr": w.get("chase_atr"), "mfe_atr": round(w["mfe"] / w["atr"], 3) if w["atr"] else None,
                "site_only": bool(w.get("site_only")), "flag": w.get("flag"), "unit": w.get("unit"),
                "thinking_level": w.get("thinking_level")})
        self.e["whatif"] = keep

    def whatif_summary(self) -> dict:
        rows = self.read_logs("carbon_whatif.jsonl")

        def pack(rs: list[dict]) -> dict:
            Rs = [r["R"] for r in rs if r.get("R") is not None]
            return {"n": len(rs), "r_avg": round(sum(Rs) / len(Rs), 3) if Rs else None,
                    "r_total": round(sum(Rs), 3), "net_1c": round(sum(r.get("net_1c") or 0 for r in rs), 2),
                    "win_rate": round(sum(1 for r in rs if (r.get("net_1c") or 0) > 0) / len(rs), 3) if rs else None}

        out = {}
        for b in BOOKS:
            bb = [r for r in rows if r.get("exit_rules") == b]
            out[b] = {
                "all": pack(bb),
                "conf>=min": pack([r for r in bb if r.get("passes_filter")]),
                "conf<min": pack([r for r in bb if not r.get("passes_filter")]),
                "site_only": pack([r for r in bb if r.get("site_only") or r.get("flag") == SITE_ONLY_FLAG]),
                "tradeable": pack([r for r in bb if not (r.get("site_only") or r.get("flag") == SITE_ONLY_FLAG)]),
            }
        out["open"] = len(self.e["whatif"])
        out["open_rows"] = [{"vid": w["vid"], "exit": w["exit"], "symbol": w["symbol"],
                             "side": _side_word(w["side"]), "entry": w.get("entry"),
                             "stop": w.get("stop"), "target": w.get("target"),
                             "confidence": w.get("confidence"), "site_only": bool(w.get("site_only")),
                             "flag": w.get("flag"), "unit": w.get("unit"),
                             "thinking_level": w.get("thinking_level")}
                            for w in self.e["whatif"]]
        out["recent"] = rows[-30:][::-1]
        out["note"] = ("BTCUSD trades on the Other-venue $100k CFD book (~$900 / 1 ATR); also scored 1-unit what-if; "
                       "CB-BRACKET and CB-RUNNER never open it (no Lucid contract).")
        return out

    # ---- exits: 18 s server loop
    def has_work(self) -> bool:
        return bool(self.e["whatif"]) or any(b.s["positions"] or b.s["pending"] for b in self.books.values())

    async def tick(self, now: datetime | None = None) -> dict:
        if self.busy:
            return {"skipped": "busy"}
        self.busy = True
        try:
            now = now or datetime.now(UTC)
            if not self.has_work():
                return {"idle": True}
            syms = sorted({p["symbol"] for b in self.books.values() for p in b.s["positions"] + b.s["pending"]}
                          | {w["symbol"] for w in self.e["whatif"]})
            res = await self._quotes(syms, now)
            quotes, m1 = (res[0] or {}), (res[1] or {})
            out = {"opened": [], "closed": []}
            async with self.mu:
                for b in self.books.values():
                    b.roll_day(now)
                    out["opened"] += [(b.name, p["id"]) for p in b.fill_pendings(quotes, now)]
                    out["closed"] += [(b.name, c["id"], c["reason"], c["net"]) for c in b.manage(quotes, m1, now)]
                self._whatif_step(quotes, m1, now)
                await self._persist(now)
            return out
        except Exception as e:
            log.exception("carbon tick failed (other books unaffected)")
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            self.busy = False

    # ---- CB-RUNNER model exits at an M15 close (only once MFE >= lock_at ATR)
    async def runner_exit_checks(self, now: datetime | None = None) -> list[dict]:
        out = []
        try:
            now = now or datetime.now(UTC)
            b = self.books.get("CB-RUNNER")
            if b is None or not b.rules.get("model_exits") or not gemini_exit_calls_enabled(self.cfg):
                return out
            for p in list(b.s["positions"]):
                if p["mfe"] + 1e-12 < float(b.rules["lock_at_atr"]) * p["atr"]:
                    continue
                why = self.gate("exit", now)
                if why:
                    self.event(now, "call_blocked", kind="exit", reason=why, id=p["id"])
                    break
                async with self.call_mu:
                    out.append(await self._runner_exit(b, p, now))
            if out:
                async with self.mu:
                    await self._persist(now)
        except Exception:
            log.exception("carbon runner exit checks failed")
        return out

    async def _runner_exit(self, b: CarbonBook, p: dict, now: datetime) -> dict:
        from .exit_manager import (ExitDecision, build_exit_prompt, decorate_exit_payload, exit_user_text,
                                   find_walls, parse_decision, position_metrics)
        from .gemini_client import _image_mime
        from google.genai import types
        rec = {"t": _iso(now), "book": b.name, "id": p["id"], "symbol": p["symbol"], "side": _side_word(p["side"]),
               "mfe_atr": round(p["mfe"] / p["atr"], 3)}
        snaps = await self._snapshots([p["symbol"]], now)
        payload = snaps.get(p["symbol"])
        if payload is None:
            rec["skipped"] = "market data failed"
            self.jsonl("carbon_exit_checks.jsonl", now, rec)
            return rec
        ctx = payload["context"]
        price = ctx.get("live_price")
        if price is None or ctx.get("live_price_stale"):
            rec["skipped"] = "live price stale"
            self.jsonl("carbon_exit_checks.jsonl", now, rec)
            return rec
        sp = b.spec(p["contract"])
        pos = {**p, "giveback_hist": p.get("giveback_hist") or []}
        m = position_metrics(p["side"], p["entry"], float(price), p["mfe"], p["atr"], sp["point_value"] * p["qty"],
                             b.comm_rt(p), pos["giveback_hist"])
        walls = find_walls(ctx, p["side"], float(price), p["entry"], p["atr"], ctx.get("signal_candle"))
        system_text = build_exit_prompt(self.cfg, pos, ctx, m, walls, now)
        pl = decorate_exit_payload(payload, pos, m)
        pl["page_key"] = f"carbon-exit-{p['symbol']}"
        png = next(iter((await self._render([pl])).values()))
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=exit_user_text(pos)),
                                                      types.Part.from_bytes(data=png, mime_type=_image_mime(png))])]
        r = await self._api(self.cc, system_text=system_text, contents=contents, schema=ExitDecision,
                            thinking=self.cc["exit_thinking"],
                            max_output_tokens=int(self.cc["spend_cap"]["max_output_tokens"]["exit"]))
        t_done = now + timedelta(seconds=float(r.get("latency_s") or 0))
        crec = self.book_call("exit", r, t_done, thinking=self.cc["exit_thinking"], symbols=[p["symbol"]], id=p["id"])
        d, perr = parse_decision(r.get("raw_text")) if r.get("ok") else (None, r.get("error"))
        rec.update({"ok": crec["ok"], "latency_s": crec["latency_s"], "cost_usd": crec["cost_usd"],
                    "decision": (d or {}).get("decision", "HOLD"), "reason": (d or {}).get("reason"),
                    "error": perr, "price": price, "pnl_atr": m.get("pnl_atr")})
        p["giveback_hist"] = (p.get("giveback_hist") or [])[-1:] + [m.get("giveback_pct")]
        async with self.mu:
            b.s["stats"]["model_exit_checks"] = b.s["stats"].get("model_exit_checks", 0) + 1
            if rec["decision"] == "CLOSE" and any(q["id"] == p["id"] for q in b.s["positions"]):
                q = (await self._quotes([p["symbol"]], t_done))[0].get(p["symbol"])
                px = float(q["price"]) if self.quote_problem(q) is None else float(price)
                c = b.close(p, b.slip(p, px), t_done, "carbon_close", {"trigger": "m15_close_model_exit",
                                                                        "exit_reason_text": rec["reason"]})
                rec["closed"] = {"net": c["net"], "R": c["R"]}
        self.jsonl("carbon_exit_checks.jsonl", t_done, rec)
        return rec

    # ---- triggered mid-bar checks
    def trigger_for(self, sym: str, quote: dict | None, ctx: dict | None, m1: pd.DataFrame | None,
                    now: datetime) -> dict | None:
        """Code-side trigger on the forming bar; mutates the cached level sides on a cross."""
        c = self.e["cache"].get(sym)
        if not c or not c.get("atr") or quote is None or quote.get("price") is None:
            return None
        tc = self.cc["triggered_checks"]
        atr, price = float(c["atr"]), float(quote["price"])
        hit = None
        for lv in c["levels"]:
            lab, px, side = lv[0], float(lv[1]), int(lv[2])
            now_side = 1 if price >= px else -1
            if now_side != side and abs(price - px) >= float(tc["level_cross_atr"]) * atr:
                lv[2] = now_side
                hit = hit or {"trigger": "level_cross", "level": lab, "level_price": px, "price": price,
                              "direction": "up" if now_side > 0 else "down"}
        if hit:
            return hit
        ctx = ctx or {}
        fh, fl = ctx.get("forming_m15_high"), ctx.get("forming_m15_low")
        if fh is not None and fl is not None and (float(fh) - float(fl)) >= float(tc["bar_range_atr"]) * atr:
            return {"trigger": "bar_range", "range_atr": round((float(fh) - float(fl)) / atr, 3), "price": price}
        if m1 is not None and len(m1) and c.get("vol_avg") and "volume" in m1:
            start = pd.Timestamp(next_m15_close(now) - timedelta(minutes=15))
            v = float(m1[m1.index >= start]["volume"].sum())
            rv = v / float(c["vol_avg"]) if c["vol_avg"] else 0.0
            if rv > float(tc["rvol_gt"]):
                return {"trigger": "rvol", "rvol": round(rv, 3), "price": price}
        return None

    async def trigger_poll(self, now: datetime | None = None) -> list[dict]:
        out = []
        try:
            now = now or datetime.now(UTC)
            if not self.entries_allowed(now) or self.gate("trigger", now):
                return out
            tc = self.cc["triggered_checks"]
            tr = self.e["triggers"]
            d = spend_day(now)
            if tr.get("day") != d:
                tr.update({"day": d, "n": 0, "per_bar": {}})
            bar = _iso(next_m15_close(now) - timedelta(minutes=15))
            syms = [s for s in self.symbols_to_send()
                    if not (self.e["cache"].get(s) or {}).get("blocked") and tr["per_bar"].get(s) != bar]
            if not syms or tr["n"] >= int(tc["max_per_day"]):
                return out
            stale = [s for s in syms if s not in self.e["cache"]]
            if stale:
                for p in (await self._snapshots(stale, now)).values():
                    self._cache_payload(p)
            res = await self._quotes(syms, now)
            quotes, m1 = res[0] or {}, res[1] or {}
            ctxs = res[2] if len(res) > 2 else {}
            for s in syms:
                if tr["n"] >= int(tc["max_per_day"]) or self.gate("trigger", now):
                    break
                q = quotes.get(s)
                if self.quote_problem(q) is not None:
                    continue
                hit = self.trigger_for(s, q, (ctxs or {}).get(s), (m1 or {}).get(s), now)
                if not hit:
                    continue
                tr["n"] += 1
                tr["per_bar"][s] = bar
                hit.update({"symbol": s, "bar": bar, "n_today": tr["n"]})
                self.event(now, "trigger", **hit)
                out.append(await self.scan(now, kind="trigger", symbols=[s], trigger=hit))
        except Exception:
            log.exception("carbon trigger poll failed")
        return out

    # ---- site
    def site_view(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        sp = self.spend_today(now)
        cap = self.cc["spend_cap"]
        state = "hard_cap" if sp["usd"] >= cap["hard_usd"] else "soft_cap" if sp["usd"] >= cap["soft_usd"] else "ok"
        from .pipeline import next_boundary
        nxt = next_boundary(now, 15, int(self.cc["scan_offset_seconds"]))
        return {"updated_at": _iso(now), "paper_only": True, "model": self.cc["model"],
                "entry_thinking": self.cc["entry_thinking"], "exit_thinking": self.cc["exit_thinking"],
                "symbols": self.cc["symbols"], "entry_window_utc": self.cc["entry_window_utc"],
                "min_confidence": self.cc["min_confidence"],
                "spend": {"day_dubai": spend_day(now), "usd": round(sp["usd"], 4), "soft_usd": cap["soft_usd"],
                          "hard_usd": cap["hard_usd"], "state": state, "calls": sp["calls"], "failed": sp["failed"],
                          "by_kind": sp["by_kind"], "tokens": sp["tokens"],
                          "price_per_mtok": list(price_for(self.cc, now.astimezone(DUBAI).date())),
                          "history": {k: round(v["usd"], 4) for k, v in sorted(self.e["spend"].items())}},
                "triggers_today": self.e["triggers"].get("n", 0) if self.e["triggers"].get("day") == spend_day(now) else 0,
                "max_triggers_per_day": self.cc["triggered_checks"]["max_per_day"],
                "next_scan_at": _iso(nxt), "in_window": self.in_window(now),
                "last_call": self.e.get("last_call"), "first_call": self.e.get("first_call"),
                "calls_total": self.e.get("calls_total", 0), "last_scan": self.e.get("last_scan"),
                "books": [b.view(now) for b in self.books.values()], "whatif": self.whatif_summary(),
                "gemini_exit_calls_enabled": gemini_exit_calls_enabled(self.cfg),
                "uniform_1atr_exits": uniform_exits_enabled(self.cfg),
                "exit_mode": close_tag(self.cfg) if uniform_exits_enabled(self.cfg) else None,
                "exit_calls_note": (UNIFORM_BANNER if uniform_exits_enabled(self.cfg)
                                    else ("Gemini exit calls paused — mechanical exits"
                                          if not gemini_exit_calls_enabled(self.cfg) else None)),
                "rules": ("gemini-4-carbon entries (thinking high, live entry prompt unchanged) on XAUUSD / EURUSD / "
                          "US100 / BTCUSD at every M15 close +20 s in 08:00-20:30 UTC + triggered one-symbol checks; "
                          "BTCUSD is Other-venue (not a Lucid contract) — CB Lucid books never open it. conf >= "
                          f"{self.cc['min_confidence']}; chase <= {self.cc['max_chase_atr']} ATR. Size floor($900 / 1 ATR"
                          " risk), caps NQ 1 / GC 1 / 6E 4, skip if 1 contract > $1,200, 6 aggregate, one per symbol; "
                          "re-entry after the next M15 close (after a loss 30 min, no same-side re-entry this session); "
                          "day P&L <= -$1,500 flattens; trailing guard $300; flat 21:45 UTC. Spend cap $15 soft / $19 "
                          "hard per Dubai day."
                          + (" CB-BRACKET unchanged (1 ATR / 1 ATR / 90 min). CB-RUNNER keeps mechanical stop/lock/"
                             "trail / 2.5 ATR target / 150-min cap; Carbon exit-model checks are paused."
                             if not gemini_exit_calls_enabled(self.cfg) else
                             " Both Carbon books use the uniform 1 ATR / 1 ATR / 90-min bracket; no model exits."))}

    async def _persist(self, now: datetime) -> None:
        view = self.site_view(now)
        async with self.io_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.root / "engine.json", self.e)
            for b in self.books.values():
                _atomic_write(b.path, b.s)
            try:
                _atomic_write(self.site_dir / "carbon.json", view)
            except Exception:
                log.exception("carbon.json write failed")

    async def persist(self, now: datetime | None = None) -> None:
        try:
            async with self.mu:
                await self._persist(now or datetime.now(UTC))
        except Exception:
            log.exception("carbon persist failed")


# ---------------------------------------------------------------------------------------------- loops
async def run_carbon_scan_loop(eng: CarbonEngine, stop: asyncio.Event | None = None) -> None:
    """M15 close + 20 s: CB-RUNNER model exits (MFE >= 1 ATR), then the Carbon entry scan. Own task."""
    from .pipeline import next_boundary
    await eng.persist()
    while not (stop and stop.is_set()):
        wake = next_boundary(datetime.now(UTC), 15, int(eng.cc["scan_offset_seconds"]))
        await asyncio.sleep(max(0.0, (wake - datetime.now(UTC)).total_seconds()))
        try:
            now = datetime.now(UTC)
            await eng.tick(now)
            await eng.runner_exit_checks(now)
            if getattr(eng, "shared_m15", False):
                # Entry scan is owned by the shared M15+20s coordinator (same chart bytes
                # as Claude / C-OPUS-B / DS-FLASH). This loop still runs tick + runner exits.
                await eng.persist(now)
            elif eng.entries_allowed(now):
                r = await eng.scan(now, kind="entry")
                log.info("carbon scan: %s", json.dumps(r, default=str)[:400])
            else:
                await eng.persist(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("carbon scan loop iteration failed")


async def run_carbon_trigger_loop(eng: CarbonEngine, stop: asyncio.Event | None = None) -> None:
    period = float(eng.cc["triggered_checks"].get("poll_seconds", 30))
    while not (stop and stop.is_set()):
        await asyncio.sleep(period)
        try:
            await eng.trigger_poll()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("carbon trigger loop failed")


async def run_carbon_exit_loop(eng: CarbonEngine, stop: asyncio.Event | None = None) -> None:
    period = float(eng.cc.get("check_seconds", 18))
    nxt = time.monotonic()
    while not (stop and stop.is_set()):
        try:
            await eng.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("carbon exit tick failed")
        nxt += period
        now = time.monotonic()
        if nxt < now:
            nxt += math.ceil((now - nxt) / period) * period
        await asyncio.sleep(nxt - now)
