"""One pipeline cycle + the clock-aligned async scheduler. SIGNALS ONLY - nothing here places orders."""
from __future__ import annotations
import asyncio, json, logging, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .data_providers import fetch_symbol, last_fetch_timings
from .gemini_client import call_gemini, mock_response
from .indicators import build_payload
from .renderer import render_all
from .risk import DailyDrawdownBreaker, make_equity_source, _atomic_write
from .site import write_latest_signal, write_status
from .publisher import Publisher
from .validator import parse_raw, validate_batch
from .scalp_levels import apply_server_levels
from .lucid import lucid_step

log = logging.getLogger("signals")


async def run_once(cfg: dict, dry_run: bool = False, force: bool = True, model: str | None = None) -> dict:
    g = cfg["general"]
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(g["session_tz"])
    state_dir = Path(g["state_dir"]); state_dir.mkdir(parents=True, exist_ok=True)

    # 1. data (parallel, per-symbol provider fallback; v2 uses bar cache)
    t_fetch0 = time.monotonic()
    results = await asyncio.gather(*[fetch_symbol(s, cfg) for s in g["symbols"]], return_exceptions=True)
    fetch_ms = round((time.monotonic() - t_fetch0) * 1000, 1)
    fetch_per = last_fetch_timings()
    payloads, data_errors = [], {}
    for sym, res in zip(g["symbols"], results):
        if isinstance(res, Exception):
            data_errors[sym] = str(res); log.error("data %s: %s", sym, res); continue
        df, src, ticker = res
        payloads.append(build_payload(sym, df, cfg, src, ticker))
    if not payloads:
        raise RuntimeError(f"no data for any symbol: {data_errors}")
    price_stamp = {p["symbol"]: {"last_price": p["context"].get("last_price", p["context"].get("last_close")),
                                 "last_price_ts_utc": p["context"].get("last_price_ts_utc",
                                                                       p["context"].get("last_closed_bar_utc"))}
                   for p in payloads}
    log.info("timing fetch_total=%.1fms fetch_per_ms=%s prices=%s", fetch_ms, fetch_per, price_stamp)

    # 2. new-closed-candle gate (10-min polling on 15-min candles => some cycles see nothing new)
    last_bars = {p["symbol"]: p["context"]["last_closed_bar_utc"] for p in payloads}
    seen_path = state_dir / "last_bars.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    if not force and cfg["scheduler"]["skip_if_no_new_candle"] and all(seen.get(k) == v for k, v in last_bars.items()):
        log.info("no new closed candle since last cycle - skipping")
        return {"skipped": True, "last_bars": last_bars}

    # 3. render all charts in parallel (v2: warm Playwright pool + JPEG)
    t_render0 = time.monotonic()
    pngs = await render_all(payloads)
    render_ms = round((time.monotonic() - t_render0) * 1000, 1)
    log.info("timing render_total=%.1fms ready_for_gemini=%.1fms bytes=%s",
             render_ms, fetch_ms + render_ms, {s: len(b) for s, b in pngs.items()})

    # 4. equity + circuit breaker (checked before the AI call; latched halt skips the paid call)
    equity = make_equity_source(cfg).get_equity()
    breaker = DailyDrawdownBreaker(state_dir / "circuit_breaker.json", cfg["risk"]["max_daily_loss_pct"],
                                   g["session_tz"])
    bstat = breaker.check(equity, now)

    # 5. Gemini (or mock)
    ai_error = None
    if bstat.halted:
        resp = {"model": None, "raw_text": json.dumps({"signals": [], "market_summary": "circuit breaker halted"}),
                "usage": None, "skipped_reason": bstat.reason}
    elif dry_run:
        resp = mock_response(payloads, cfg)
    else:
        try:
            resp = await call_gemini(cfg, payloads, pngs, model)
        except Exception as e:
            ai_error = f"{type(e).__name__}: {e}"
            log.error("Gemini call failed: %s", ai_error)
            resp = {"model": None, "raw_text": "", "usage": None, "error": ai_error,
                    "attempts": getattr(e, "attempts", None)}

    # 6. parse + server-side scalp SL/TP (source of truth) + validate + breaker override
    obj, parse_err = parse_raw(resp["raw_text"]) if resp["raw_text"] else (None, ai_error or "empty response")
    contexts = {p["symbol"]: p["context"] for p in payloads}
    # Overwrite Gemini stop/target with deterministic scalp levels (direction + confidence kept).
    if obj and (cfg.get("scalp") or {}).get("enabled", False):
        for s in obj.get("signals") or []:
            if not isinstance(s, dict) or not s.get("symbol"):
                continue
            sym = str(s["symbol"]).upper().replace("/", "").strip()
            ctx = contexts.get(sym)
            if not ctx:
                continue
            # Prefer a near-market entry; keep Gemini entry if present and sane.
            if s.get("entry") is None:
                s["entry"] = ctx.get("last_close")
            apply_server_levels(s, ctx, cfg, sym)
    validated = validate_batch(obj, contexts, cfg, equity, bstat.halted, bstat.reason)
    # Mirror scalp metadata onto validated rows for Lucid / logs.
    if obj and (cfg.get("scalp") or {}).get("enabled", False):
        by = {str(s.get("symbol", "")).upper().replace("/", "").strip(): s
              for s in (obj.get("signals") or []) if isinstance(s, dict)}
        for v in validated:
            raw = by.get(v["symbol"]) or {}
            if raw.get("scalp"):
                v["scalp"] = raw["scalp"]
                v["levels_source"] = "server_scalp"
    # Stamp session + CVD onto every validated signal (logged; public site still publishes).
    for v in validated:
        ctx = contexts.get(v["symbol"]) or {}
        for k in ("session", "cvd", "cvd_label", "cvd_slope",
                  "last_closed_bar_utc", "last_price_ts_utc", "poc", "vah", "val"):
            if v.get(k) is None and ctx.get(k) is not None:
                v[k] = ctx[k]
    if ai_error:
        for v in validated:
            v["reasons"] = [f"AI call failed: {ai_error[:160]}" if r == "missing from model output" else r
                            for r in v["reasons"]]

    # 7. logs/YYYY-MM-DD/HHMMSS/
    local = now.astimezone(tz)
    compact = g.get("log_mode") == "compact"  # GitHub Actions: one small JSON per cycle, no chart images
    if compact:
        run_dir = Path(g["logs_dir"]) / local.strftime("%Y-%m-%d") / "cycles"
    else:
        run_dir = Path(g["logs_dir"]) / local.strftime("%Y-%m-%d") / local.strftime("%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    if not compact:
        for sym, png in pngs.items():
            (run_dir / f"{sym}.png").write_bytes(png)
        (run_dir / "gemini_raw.json").write_text(json.dumps({**resp, "parse_error": parse_err}, indent=2))
    record = {
        "run_dir": str(run_dir),
        "run_at_utc": now.isoformat(), "run_at_local": local.isoformat(), "dry_run": dry_run,
        "model": resp.get("model"), "model_version": resp.get("model_version"),
        "gemini_attempts": resp.get("attempts"), "signals_only": True, "equity": equity, "circuit_breaker": bstat.__dict__,
        "data": {p["symbol"]: {"source": p["source"], "ticker": p["ticker"], **p["context"]} for p in payloads},
        "data_errors": data_errors, "parse_error": parse_err, "ai_error": ai_error,
        "market_summary": (obj or {}).get("market_summary"), "signals": validated,
    }
    if compact:
        record["run_dir"] = str(run_dir / f"{local.strftime('%H%M%S')}.json")
        record["gemini_raw_text"] = resp.get("raw_text")
        record["gemini_usage"] = resp.get("usage")
        Path(record["run_dir"]).write_text(json.dumps(record, separators=(",", ":"), default=str))
    else:
        (run_dir / "validated.json").write_text(json.dumps(record, indent=2, default=str))
        with open(run_dir.parent / "signals.jsonl", "a") as f:
            f.write(json.dumps({"run_dir": str(run_dir), **record}, default=str) + "\n")
    _atomic_write(state_dir / "latest_signals.json", record)
    write_latest_signal(record, cfg)  # site/latest_signal.json for the one-page site
    _atomic_write(seen_path, last_bars)
    log.info("run complete -> %s", run_dir)
    for v in validated:
        log.info("  %-7s %-4s conf=%s rr=%s %s", v["symbol"], v["action"], v["confidence"], v["rr_local"],
                 "; ".join(v["reasons"]))
    return {"skipped": False, "run_dir": record["run_dir"], "record": record}


def next_boundary(now: datetime, minutes: int, offset_s: int) -> datetime:
    step = minutes * 60
    ts = now.timestamp()
    nxt = (int(ts - offset_s) // step + 1) * step + offset_s
    return datetime.fromtimestamp(nxt, timezone.utc)


async def run_loop(cfg: dict, dry_run: bool = False, model: str | None = None,
                   interval: int | None = None, max_cycles: int | None = None) -> None:
    """Clock-aligned loop. Keeps site/status.json current: running -> analyzing -> running ...;
    writes 'paused' on exit (Ctrl-C / SIGTERM / pkill / max_cycles)."""
    sc = cfg["scheduler"]
    interval = interval or sc["interval_minutes"]
    cycles = 0
    last = {}
    pub = Publisher(cfg)
    lucid_task = None
    log.info("scheduler: every %d min aligned to the clock (+%ds)%s", interval, sc["align_offset_seconds"],
             " [DRY RUN]" if dry_run else "")
    try:
        while max_cycles is None or cycles < max_cycles:
            nxt = next_boundary(datetime.now(timezone.utc), interval, sc["align_offset_seconds"])
            write_status(cfg, "running", interval, next_run_at=nxt.isoformat(), **last)
            pub.publish(f"running, next {nxt.strftime('%H:%M')}Z" + (" (signal)" if cycles else ""))
            log.info("next cycle at %s UTC", nxt.strftime("%Y-%m-%d %H:%M:%S"))
            await asyncio.sleep(max(0, (nxt - datetime.now(timezone.utc)).total_seconds()))
            started = datetime.now(timezone.utc).isoformat()
            write_status(cfg, "analyzing", interval, cycle_started_at=started, **last)
            # (no push here: the page derives "Reading The Market" from the clock; saves a Pages deploy per cycle)
            t0 = time.monotonic()
            res = {}
            try:
                res = await run_once(cfg, dry_run=dry_run, force=False, model=model)
            except Exception:
                log.exception("cycle failed")
            # Lucid paper sim runs in the background so the site publish is never delayed (no extra Gemini call)
            if lucid_task is not None and not lucid_task.done():
                await lucid_task
            if not dry_run:
                lucid_task = asyncio.create_task(lucid_step(cfg, (res or {}).get("record")))
            last = {"last_cycle_started_at": started,
                    "last_cycle_finished_at": datetime.now(timezone.utc).isoformat()}
            cycles += 1
            log.info("cycle took %.1fs", time.monotonic() - t0)
    finally:
        if lucid_task is not None and not lucid_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(lucid_task), 60)
            except BaseException:
                pass
        write_status(cfg, "paused", interval, **last)
        pub.publish("paused")
        await asyncio.to_thread(pub.flush, 90)
        log.info("scheduler stopped -> status paused")
