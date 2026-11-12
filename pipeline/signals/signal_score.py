"""Universal signal scorer (t248u). PAPER ONLY. No model calls.

Every BUY/SELL from every entry model (traded or not, any confidence) is scored against the
uniform 1 ATR / 1 ATR / 90-min bracket, a seeded coin-flip twin, and M1 MFE/MAE over 2h/3h/4h.
HOLDs are also scored as long+short counterfactuals (M1 only — cheap, no extra API spend).
Rows are appended once to logs/<day>/signal_scores.jsonl when the 4h window is complete.
"""
from __future__ import annotations
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .risk import _atomic_write
from .uniform_exit import (HORIZONS_H, TIME_CAP_MINUTES, UNIFORM_EXIT_VERSION, bracket_levels,
                           excursion, first_touch, result_R, twin_side)
from .carbon import whatif_spec

log = logging.getLogger("signals.signal_score")
UTC = timezone.utc
DUBAI = ZoneInfo("Asia/Dubai")
CONF_BANDS = ((0.0, 0.55, "<0.55"), (0.55, 0.75, "0.55-0.75"), (0.75, 0.90, "0.75-0.90"),
              (0.90, 1.01, "0.90-1.00"))


def _iso(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, str):
        return t
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat()


def _dt(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def score_key(row: dict) -> str:
    return "|".join(str(row.get(k) or "") for k in
                    ("model", "symbol", "signal_at", "action", "side", "kind", "role"))


def conf_band(c) -> str:
    try:
        x = float(c)
    except (TypeError, ValueError):
        return "none"
    for a, b, name in CONF_BANDS:
        if a <= x < b:
            return name
    return "none"


def spec_for(cfg: dict, sym: str) -> dict | None:
    from .carbon import carbon_cfg
    return whatif_spec(cfg, carbon_cfg(cfg), sym)


class SignalScorer:
    def __init__(self, cfg: dict, *, state_dir=None, logs_dir=None, site_dir=None, m1_fetch=None):
        g = cfg.get("general") or {}
        self.cfg = cfg
        self.root = Path(state_dir or g.get("state_dir") or "state") / "signal_scores"
        self.logs_dir = Path(logs_dir or g.get("logs_dir") or "logs")
        self.site_dir = Path(site_dir or (cfg.get("site") or {}).get("dir") or self.root.parent.parent)
        self.tz = ZoneInfo(g.get("session_tz") or "Asia/Dubai")
        self.root.mkdir(parents=True, exist_ok=True)
        self._m1_fetch = m1_fetch
        self.s = self._load()

    def _load(self) -> dict:
        p = self.root / "engine.json"
        try:
            st = json.loads(p.read_text()) if p.exists() else None
        except Exception:
            st = None
        if not isinstance(st, dict):
            st = {"version": 1, "queue": [], "done": [], "backfill_done": False,
                  "seen": {}, "updated_at": None}
        st.setdefault("queue", [])
        st.setdefault("seen", {})
        return st

    def save(self) -> None:
        # keep seen keys compact: drop entries older than 14 days from the map if huge
        self.s["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_write(self.root / "engine.json", self.s)

    def enqueue(self, sig: dict) -> list[dict]:
        """Queue BUY/SELL as one row plus a coin-flip twin; HOLD as long+short counterfactuals + twins."""
        added = []
        act = str(sig.get("action") or "HOLD").upper()
        if act not in ("BUY", "SELL", "HOLD"):
            return added
        base = {
            "model": sig.get("model"), "model_version": sig.get("model_version"),
            "thinking_level": sig.get("thinking_level"),
            "prompt_version": sig.get("prompt_version") or sig.get("entry_prompt_version"),
            "symbol": str(sig.get("symbol") or "").upper(),
            "session": sig.get("session"), "confidence": sig.get("confidence"),
            "signal_at": _iso(_dt(sig.get("signal_at") or sig.get("t") or sig.get("run_at_utc"))),
            "entry": sig.get("entry") or sig.get("live_price") or sig.get("last_close") or sig.get("price"),
            "atr": sig.get("atr"), "kind": sig.get("kind") or "entry",
            "bar": sig.get("bar") or sig.get("last_closed_bar_utc"),
            "traded": bool(sig.get("traded")),
        }
        if not base["symbol"] or not base["signal_at"] or not base["entry"] or not base["atr"]:
            return added
        jobs = []
        if act in ("BUY", "SELL"):
            jobs.append(("signal", 1 if act == "BUY" else -1, act, False))
        else:
            jobs.append(("hold_long", 1, "HOLD", True))
            jobs.append(("hold_short", -1, "HOLD", True))
        for kind, side, action, cf in jobs:
            row = {**base, "action": action, "side": side, "role": kind,
                   "counterfactual": cf, "exit_mode": UNIFORM_EXIT_VERSION}
            self._queue_with_twin(row, added)
        return added

    def _queue_with_twin(self, row: dict, added: list) -> None:
        k = score_key(row)
        if self.s["seen"].get(k):
            return
        tside, seed = twin_side(k)
        twin = {**row, "role": "twin", "side": tside, "twin_of": k, "twin_seed": seed,
                "counterfactual": True}
        tk = score_key(twin)
        for r, key in ((row, k), (twin, tk)):
            if self.s["seen"].get(key):
                continue
            self.s["seen"][key] = r["signal_at"]
            self.s["queue"].append(r)
            added.append(r)

    def jsonl_path(self, t: datetime) -> Path:
        d = self.logs_dir / t.astimezone(self.tz).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        return d / "signal_scores.jsonl"

    async def _bars(self, sym: str, start: datetime, end: datetime):
        if self._m1_fetch:
            return await self._m1_fetch(sym, start, end)
        from .lucid import fetch_m1
        scfg = (self.cfg.get("symbols") or {}).get(sym) or {}
        ticker = scfg.get("dukascopy") or scfg.get("binance") or sym.lower()
        try:
            got = await fetch_m1(ticker, start - timedelta(minutes=2), end + timedelta(minutes=2))
            # fetch_m1 returns a DataFrame (not (df, source)); unpacking the frame raises
            # "too many values to unpack" because iteration walks the columns.
            return got[0] if isinstance(got, tuple) else got
        except Exception as e:
            log.warning("signal_score m1 %s failed: %s", sym, e)
            return None

    def _score_one(self, row: dict, bars) -> dict | None:
        """Return a finished record, or None if the 4h window is not yet available."""
        t0 = _dt(row["signal_at"])
        now = datetime.now(UTC)
        horizon = t0 + timedelta(hours=max(HORIZONS_H))
        if bars is None or not len(bars) or bars.index.max() < pd_ts(horizon):
            return None
        sp = spec_for(self.cfg, row["symbol"])
        if not sp:
            return {**row, "error": "no_spec", "scored_at": _iso(now)}
        side = int(row["side"])
        entry = float(row["entry"])
        atr = float(row["atr"])
        tick = float(sp["tick"])
        lv = bracket_levels(side, entry, atr, tick)
        hit = first_touch(bars, side=side, entry=entry, stop=lv["stop"], target=lv["target"],
                          entry_time=t0, tick=tick)
        exc = excursion(bars, side=side, entry=entry, start=t0)
        rec = {**row, "exit_mode": UNIFORM_EXIT_VERSION, "unit": sp.get("unit"),
               "contract": sp.get("contract"), "site_only": bool(sp.get("site_only")),
               "stop": lv["stop"], "target": lv["target"],
               "bracket_hours": TIME_CAP_MINUTES / 60.0, "scored_at": _iso(now),
               "conf_band": conf_band(row.get("confidence")),
               "excursions": {}}
        if hit:
            rr = result_R(side, entry, float(hit["price"]), lv["stop"], sp["point_value"],
                          sp["commission"], qty=1)
            rec.update({"bracket_reason": hit["reason"], "bracket_exit": hit["price"],
                        "bracket_t_exit": _iso(hit["t"]), **rr,
                        "bracket_mfe": hit.get("mfe"), "bracket_mae": hit.get("mae"),
                        "bracket_mfe_atr": round(hit["mfe"] / atr, 3) if atr else None,
                        "bracket_mae_atr": round(hit["mae"] / atr, 3) if atr else None,
                        "win": bool(rr["net"] > 0)})
        else:
            rec.update({"bracket_reason": "open", "R": None, "net": None, "win": None})
        for h, ev in exc.items():
            rec["excursions"][h] = {
                **ev,
                "mfe_atr": round(ev["mfe"] / atr, 3) if ev.get("mfe") is not None and atr else None,
                "mae_atr": round(ev["mae"] / atr, 3) if ev.get("mae") is not None and atr else None,
            }
        return rec

    async def tick(self, now: datetime | None = None, *, max_jobs: int = 12) -> dict:
        """Score queued rows whose 4h window is ready. Never raises."""
        now = now or datetime.now(UTC)
        out = {"scored": 0, "pending": len(self.s["queue"]), "errors": 0}
        try:
            keep, i = [], 0
            # group by symbol to reuse M1
            by_sym = defaultdict(list)
            later = []
            horizon = timedelta(hours=max(HORIZONS_H))
            for row in self.s["queue"]:
                t0 = _dt(row["signal_at"])
                # fetch a little early so backfill can complete as soon as bars exist
                if t0 and now < t0 + horizon - timedelta(minutes=5) and not self.s.get("backfill_done"):
                    # still try if this is a live tick after 4h; otherwise park
                    if now < t0 + horizon:
                        later.append(row)
                        continue
                by_sym[row["symbol"]].append(row)
            keep.extend(later)
            for sym, rows in by_sym.items():
                if i >= max_jobs:
                    keep.extend(rows)
                    continue
                tmin = min(_dt(r["signal_at"]) for r in rows)
                tmax = max(_dt(r["signal_at"]) for r in rows) + horizon
                bars = await self._bars(sym, tmin, tmax)
                i += 1
                for r in rows:
                    rec = self._score_one(r, bars)
                    if rec is None:
                        keep.append(r)
                        continue
                    if rec.get("error") and rec["error"] != "no_spec":
                        out["errors"] += 1
                    self._append(rec)
                    out["scored"] += 1
            self.s["queue"] = keep
            out["pending"] = len(keep)
            self._write_summary()
            self.save()
        except Exception:
            log.exception("signal_score tick failed")
            out["errors"] += 1
        return out

    def _append(self, rec: dict) -> None:
        t = _dt(rec.get("signal_at")) or datetime.now(UTC)
        with open(self.jsonl_path(t), "a") as f:
            f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")

    def read_all(self) -> list[dict]:
        rows = []
        for f in sorted(self.logs_dir.glob("*/signal_scores.jsonl")):
            for line in f.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        return rows

    def summary(self) -> dict:
        rows = self.read_all()
        sigs = [r for r in rows if r.get("role") in ("signal", "hold_long", "hold_short")
                and r.get("R") is not None]
        twins = {r.get("twin_of") or score_key({**r, "role": r.get("role"), "side": r.get("side")}): r
                 for r in rows if r.get("role") == "twin" and r.get("R") is not None}
        # re-key twins by twin_of
        twins = {r["twin_of"]: r for r in rows if r.get("role") == "twin" and r.get("twin_of") and r.get("R") is not None}

        def pack(rs: list[dict], label: str) -> dict:
            if not rs:
                return {"n": 0, "win_pct": None, "avg_R": None, "twin_avg_R": None, "edge_R": None,
                        "label": label, "by_band": {}, "excursions": {}}
            wins = [r for r in rs if r.get("win")]
            Rs = [r["R"] for r in rs if r.get("R") is not None]
            tRs = []
            for r in rs:
                tw = twins.get(score_key(r))
                if tw and tw.get("R") is not None:
                    tRs.append((r["R"], tw["R"]))
            def avg(xs):
                return round(sum(xs) / len(xs), 3) if xs else None
            bands = {}
            for r in rs:
                bands.setdefault(r.get("conf_band") or "none", []).append(r)
            band_out = {}
            for b, br in bands.items():
                bR = [x["R"] for x in br if x.get("R") is not None]
                bT = []
                for x in br:
                    tw = twins.get(score_key(x))
                    if tw and tw.get("R") is not None and x.get("R") is not None:
                        bT.append((x["R"], tw["R"]))
                band_out[b] = {"n": len(br),
                               "win_pct": round(sum(1 for x in br if x.get("win")) / len(br), 3) if br else None,
                               "avg_R": avg(bR),
                               "twin_avg_R": avg([t[1] for t in bT]),
                               "edge_R": avg([t[0] - t[1] for t in bT])}
            exc = {}
            for h in (f"{x}h" for x in HORIZONS_H):
                mfe = [((r.get("excursions") or {}).get(h) or {}).get("mfe_atr") for r in rs]
                mae = [((r.get("excursions") or {}).get(h) or {}).get("mae_atr") for r in rs]
                mfe, mae = [x for x in mfe if x is not None], [x for x in mae if x is not None]
                exc[h] = {"avg_mfe_atr": avg(mfe), "avg_mae_atr": avg(mae), "n": len(mfe)}
            return {"n": len(rs), "label": label,
                    "win_pct": round(len(wins) / len(rs), 3) if rs else None,
                    "avg_R": avg(Rs),
                    "twin_avg_R": avg([t[1] for t in tRs]),
                    "edge_R": avg([t[0] - t[1] for t in tRs]),
                    "by_band": band_out, "excursions": exc}

        by_model = defaultdict(list)
        for r in sigs:
            if r.get("role") == "signal":
                by_model[r.get("model") or "unknown"].append(r)
        models = {m: pack(rs, m) for m, rs in by_model.items()}
        return {"updated_at": datetime.now(UTC).isoformat(), "exit_mode": UNIFORM_EXIT_VERSION,
                "paper_only": True, "pending": len(self.s.get("queue") or []),
                "n_scored": len(sigs), "models": models,
                "note": ("Every BUY/SELL (any confidence, traded or not) vs the uniform 1 ATR / 1 ATR / 90-min "
                         "bracket, plus a seeded coin-flip twin and M1 MFE/MAE over 2h/3h/4h. HOLDs scored as "
                         "long+short counterfactuals. Append-only logs/<day>/signal_scores.jsonl.")}

    def _write_summary(self) -> None:
        try:
            _atomic_write(self.site_dir / "signal_scores.json", self.summary())
        except Exception:
            log.exception("signal_scores.json write failed")

    def ingest_logs(self) -> int:
        """Enqueue historical signals from cycle / carbon / claude / deepseek logs. Idempotent."""
        n = 0
        n += self._ingest_cycles()
        n += self._ingest_jsonl("carbon_signals.jsonl", model="gemini-4-carbon",
                                thinking_default="medium")
        n += self._ingest_claude()
        n += self._ingest_jsonl("deepseek_signals.jsonl", model="deepseek-flash",
                                nested=True)
        self.save()
        return n

    def _ingest_cycles(self) -> int:
        n = 0
        for f in sorted(self.logs_dir.glob("*/cycles/*.json")):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            model = d.get("model") or "gemini-3.8-flash"
            think = d.get("thinking_level") or "low"
            t = d.get("run_at_utc")
            ctx = d.get("data") or {}
            for s in d.get("signals") or []:
                c = (ctx.get(s.get("symbol")) or {})
                n += len(self.enqueue({
                    **s, "model": model, "thinking_level": s.get("thinking_level") or think,
                    "signal_at": t, "atr": s.get("atr") or c.get("atr"),
                    "entry": s.get("entry") or c.get("live_price") or c.get("last_close"),
                    "session": s.get("session") or c.get("session"),
                    "kind": "entry", "prompt_version": d.get("prompt_version"),
                }))
        return n

    def _ingest_jsonl(self, name: str, *, model: str, thinking_default=None, nested=False) -> int:
        n = 0
        for f in sorted(self.logs_dir.glob(f"*/{name}")):
            for line in f.read_text().splitlines():
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if nested:
                    think = d.get("thinking_level") or thinking_default
                    t = d.get("t") or d.get("signal_at")
                    for s in d.get("signals") or []:
                        n += len(self.enqueue({
                            **s, "model": d.get("model") or model,
                            "thinking_level": s.get("thinking_level") or think,
                            "signal_at": t, "kind": d.get("kind") or "entry",
                        }))
                else:
                    n += len(self.enqueue({
                        **d, "model": d.get("model") or model,
                        "thinking_level": d.get("thinking_level") or thinking_default,
                        "signal_at": d.get("signal_at") or d.get("t"),
                    }))
        return n

    def _ingest_claude(self) -> int:
        n = 0
        for f in sorted(self.logs_dir.glob("*/claude_signals.jsonl")):
            for line in f.read_text().splitlines():
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                model = d.get("entry_model") or d.get("model")
                t = d.get("t") or d.get("signal_at")
                sigs = d.get("signals") or ([d] if d.get("symbol") else [])
                for s in sigs:
                    n += len(self.enqueue({
                        **s, "model": s.get("model") or model,
                        "thinking_level": s.get("thinking_level") or d.get("entry_effort"),
                        "signal_at": s.get("signal_at") or t, "kind": "entry",
                        "prompt_version": s.get("exit_prompt_version") or d.get("prompt_version"),
                    }))
        return n


def pd_ts(t: datetime):
    import pandas as pd
    ts = pd.Timestamp(t)
    return ts if ts.tzinfo else ts.tz_localize("UTC")
