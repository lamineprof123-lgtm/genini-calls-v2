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
from .site import write_latest_signal, write_status, write_in_position_overlay
from .publisher import Publisher
from .validator import parse_raw, validate_batch
from .scalp_levels import apply_server_levels
from .lucid import lucid_step
from .live_price import enrich_live_prices
from .firmtape import enrich_firmtape

log = logging.getLogger("signals")


def _kick_deepseek(cfg: dict, payloads: list[dict] | None, pngs: dict | None = None,
                   skipped: str | None = None, now=None, dry_run: bool = False) -> None:
    """Start a DS-FLASH entry cycle in the background. Never awaited, never raises."""
    if dry_run or not payloads:
        return
    try:
        from .deepseek import kick
        kick(cfg, payloads, pngs=pngs, skipped=skipped, now=now)
    except Exception:
        log.exception("deepseek kick failed (Gemini loop unaffected)")


# Lucid paper-sim symbols (BTCUSD is site-only). Session throttle is decided from these four;
# they share the same America/New_York session clock.
_DEFAULT_LUCID_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD", "US100")
THROTTLE_SKIP = "blocked_session_throttle"
ALL_IN_POS_SKIP = "all_symbols_in_position"


def lucid_symbols(cfg: dict) -> list[str]:
    lu = (cfg.get("lucid") or {}).get("symbols") or {}
    return list(lu) if lu else list(_DEFAULT_LUCID_SYMBOLS)


def lucid_busy_symbols(cfg: dict, state_dir: str | Path | None = None) -> set[str]:
    """Symbols with an open or pending Lucid position. BTCUSD is site-only and is never excluded."""
    d = Path(state_dir or cfg["general"]["state_dir"])
    p = d / "lucid" / "account.json"
    try:
        acct = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        acct = {}
    busy: set[str] = set()
    for row in (acct.get("positions") or []) + (acct.get("pending") or []):
        sym = str(row.get("symbol") or "").upper().replace("/", "").strip()
        if sym:
            busy.add(sym)
    busy.discard("BTCUSD")
    allowed = set(lucid_symbols(cfg))
    return {s for s in busy if s in allowed}


def filter_entry_payloads(payloads: list[dict], busy: set[str]) -> list[dict]:
    """Drop in-position Lucid symbols from the batched entry prompt (no image, context, or schema slot)."""
    return [p for p in payloads if p.get("symbol") not in busy]


def session_blocked_for_scan(payloads: list[dict], cfg: dict) -> bool:
    """True when every available Lucid (non-BTC) symbol is in a blocked session."""
    want = set(lucid_symbols(cfg))
    rows = [p for p in payloads if p.get("symbol") in want]
    if not rows:
        return False
    return all(bool((p.get("context") or {}).get("session_blocks_automation")) for p in rows)


def gemini_bar_changed(payloads: list[dict], seen: dict, cfg: dict | None = None) -> bool:
    """True if any Lucid symbol's last_closed_bar_utc differs from the last Gemini-called cycle."""
    want = set(lucid_symbols(cfg)) if cfg is not None else None
    rows = [p for p in payloads if want is None or p.get("symbol") in want] or list(payloads)
    for p in rows:
        if seen.get(p["symbol"]) != (p.get("context") or {}).get("last_closed_bar_utc"):
            return True
    return False


def should_throttle_gemini(payloads: list[dict], seen: dict, cfg: dict) -> bool:
    """Skip Gemini mid-bar during ASIAN_SESSION_CHOP / LOW_LIQUIDITY_DEADZONE (Lucid symbols)."""
    return session_blocked_for_scan(payloads, cfg) and not gemini_bar_changed(payloads, seen, cfg)


def scan_interval_minutes(blocked: bool, cfg: dict) -> int:
    """2 (or scheduler default) while sessions are active; 15 during blocked windows (M15 close)."""
    active = int(cfg["scheduler"]["interval_minutes"])
    tf = int(cfg["general"]["timeframe_minutes"])
    return tf if blocked else active


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


async def run_once(cfg: dict, dry_run: bool = False, force: bool = True, model: str | None = None,
                   no_throttle: bool = False) -> dict:
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

    # 1b. live M1 / forming M15 (does not change signal_candle). Failure -> last close + live_price_stale.
    t_live0 = time.monotonic()
    live_info = await enrich_live_prices(payloads, cfg, now)
    log.info("timing live_m1=%.1fms (fetch_total was %.1fms)", live_info.get("ms"), fetch_ms)
    # 1c. FirmTape GEX (US100/XAUUSD, NY RTH only) + US100 live price from NQ + rolling basis. Never raises.
    await enrich_firmtape(payloads, cfg, now)
    price_stamp = {p["symbol"]: {"last_price": p["context"].get("live_price", p["context"].get("last_close")),
                                 "last_price_ts_utc": p["context"].get("live_price_ts_utc",
                                                                       p["context"].get("last_closed_bar_utc")),
                                 "live_price_stale": p["context"].get("live_price_stale"),
                                 "minutes_into_bar": p["context"].get("minutes_into_bar")}
                   for p in payloads}

    # 2. new-closed-candle gate
    last_bars = {p["symbol"]: p["context"]["last_closed_bar_utc"] for p in payloads}
    seen_path = state_dir / "last_bars.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    if not force and cfg["scheduler"]["skip_if_no_new_candle"] and all(seen.get(k) == v for k, v in last_bars.items()):
        log.info("no new closed candle since last cycle - skipping")
        blocked = session_blocked_for_scan(payloads, cfg)
        return {"skipped": True, "last_bars": last_bars,
                "scan_interval_minutes": scan_interval_minutes(blocked, cfg),
                "session_blocked": blocked}

    gemini_seen_path = state_dir / "last_gemini_bars.json"
    gemini_seen = _load_json(gemini_seen_path, {})
    blocked = session_blocked_for_scan(payloads, cfg)
    interval = scan_interval_minutes(blocked, cfg)
    busy = lucid_busy_symbols(cfg, state_dir)
    entry_payloads = filter_entry_payloads(payloads, busy)
    if busy:
        log.info("entry scan excluding in-position symbols: %s (entry=%s)",
                 sorted(busy), [p["symbol"] for p in entry_payloads])
    if not no_throttle and should_throttle_gemini(payloads, gemini_seen, cfg):
        log.info("blocked session throttle - skipping Gemini (keep last site signals); next scan on M%s close",
                 cfg["general"]["timeframe_minutes"])
        record = _throttle_skip_record(cfg, payloads, data_errors, last_bars, now, tz, equity=None)
        record["in_position"] = sorted(busy)
        if busy:
            write_in_position_overlay(cfg, busy)
        _kick_deepseek(cfg, payloads, skipped=THROTTLE_SKIP, now=now, dry_run=dry_run)
        return {"skipped": True, "skipped_reason": THROTTLE_SKIP, "last_bars": last_bars,
                "scan_interval_minutes": interval, "session_blocked": True, "record": record,
                "payloads": payloads}

    if not entry_payloads:
        log.info("all eligible symbols in a Lucid position - skipping Gemini (%s)", ALL_IN_POS_SKIP)
        record = _throttle_skip_record(cfg, payloads, data_errors, last_bars, now, tz, equity=None)
        record["skipped_reason"] = ALL_IN_POS_SKIP
        record["in_position"] = sorted(busy)
        record["entry_symbols"] = []
        # rewrite the compact cycle file with the real skip reason
        if record.get("run_dir") and Path(record["run_dir"]).exists() and str(record["run_dir"]).endswith(".json"):
            Path(record["run_dir"]).write_text(json.dumps(record, separators=(",", ":"), default=str))
        write_in_position_overlay(cfg, busy)
        _atomic_write(seen_path, last_bars)
        _kick_deepseek(cfg, payloads, skipped=ALL_IN_POS_SKIP, now=now, dry_run=dry_run)
        return {"skipped": True, "skipped_reason": ALL_IN_POS_SKIP, "last_bars": last_bars,
                "scan_interval_minutes": interval, "session_blocked": blocked, "record": record,
                "payloads": payloads}

    # 3. render only the charts that will go to Gemini (v2: warm Playwright pool + JPEG)
    t_render0 = time.monotonic()
    pngs = await render_all(entry_payloads)
    render_ms = round((time.monotonic() - t_render0) * 1000, 1)
    log.info("timing render_total=%.1fms ready_for_gemini=%.1fms bytes=%s",
             render_ms, fetch_ms + render_ms, {s: len(b) for s, b in pngs.items()})
    # DS-FLASH entry call starts now so it overlaps the Gemini request and cannot delay it.
    _kick_deepseek(cfg, payloads, pngs=pngs, now=now, dry_run=dry_run)

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
        resp = mock_response(entry_payloads, cfg)
    else:
        try:
            resp = await call_gemini(cfg, entry_payloads, pngs, model)
        except Exception as e:
            ai_error = f"{type(e).__name__}: {e}"
            log.error("Gemini call failed: %s", ai_error)
            resp = {"model": None, "raw_text": "", "usage": None, "error": ai_error,
                    "attempts": getattr(e, "attempts", None)}

    # 6. parse + server-side scalp SL/TP (source of truth) + validate + breaker override
    obj, parse_err = parse_raw(resp["raw_text"]) if resp["raw_text"] else (None, ai_error or "empty response")
    contexts = {p["symbol"]: p["context"] for p in entry_payloads}
    # Overwrite Gemini stop/target with deterministic scalp levels (direction + confidence kept).
    if obj and (cfg.get("scalp") or {}).get("enabled", False):
        for s in obj.get("signals") or []:
            if not isinstance(s, dict) or not s.get("symbol"):
                continue
            sym = str(s["symbol"]).upper().replace("/", "").strip()
            ctx = contexts.get(sym)
            if not ctx:
                continue
            # Server entry is the live quote when fresh; otherwise last M15 close.
            if not ctx.get("live_price_stale") and ctx.get("live_price") is not None:
                s["entry"] = ctx["live_price"]
            elif s.get("entry") is None:
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
                  "last_closed_bar_utc", "last_price_ts_utc", "poc", "vah", "val",
                  "live_price", "live_price_ts_utc", "live_price_stale", "live_price_source",
                  "live_price_fetched_at_utc", "forming_m15_open", "forming_m15_high",
                  "forming_m15_low", "forming_m15_open_utc", "minutes_into_bar", "live_price_age_s"):
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
        "gemini_attempts": resp.get("attempts"), "gemini_latency_s": resp.get("latency_s"),
        "signals_only": True, "equity": equity, "circuit_breaker": bstat.__dict__,
        "data": {p["symbol"]: {"source": p["source"], "ticker": p["ticker"], **p["context"]} for p in payloads},
        "data_errors": data_errors, "parse_error": parse_err, "ai_error": ai_error,
        "market_summary": (obj or {}).get("market_summary"), "signals": validated,
        "in_position": sorted(busy),
        "entry_symbols": [p["symbol"] for p in entry_payloads],
        "thinking_level": (cfg.get("gemini") or {}).get("entry_thinking_level")
                          or (cfg.get("gemini") or {}).get("thinking_level"),
    }
    if record["thinking_level"]:
        for v in validated:
            v["thinking_level"] = record["thinking_level"]
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
    _atomic_write(gemini_seen_path, last_bars)
    log.info("run complete -> %s", run_dir)
    for v in validated:
        log.info("  %-7s %-4s conf=%s rr=%s %s", v["symbol"], v["action"], v["confidence"], v["rr_local"],
                 "; ".join(v["reasons"]))
    return {"skipped": False, "run_dir": record["run_dir"], "record": record,
            "scan_interval_minutes": interval, "session_blocked": blocked, "payloads": payloads}


def _throttle_skip_record(cfg: dict, payloads: list[dict], data_errors: dict,
                          last_bars: dict, now: datetime, tz, equity=None) -> dict:
    """Compact cycle log when Gemini is skipped during a blocked session. Does not touch latest_signal.json."""
    g = cfg["general"]
    local = now.astimezone(tz)
    compact = g.get("log_mode") == "compact"
    if compact:
        run_dir = Path(g["logs_dir"]) / local.strftime("%Y-%m-%d") / "cycles"
    else:
        run_dir = Path(g["logs_dir"]) / local.strftime("%Y-%m-%d") / local.strftime("%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "run_dir": str(run_dir),
        "run_at_utc": now.isoformat(), "run_at_local": local.isoformat(), "dry_run": False,
        "model": None, "model_version": None, "gemini_attempts": None, "signals_only": True,
        "equity": equity, "circuit_breaker": None,
        "data": {p["symbol"]: {"source": p["source"], "ticker": p["ticker"], **p["context"]} for p in payloads},
        "data_errors": data_errors, "parse_error": None, "ai_error": None,
        "market_summary": None, "signals": [],
        "skipped_reason": THROTTLE_SKIP,
        "scan_interval_minutes": scan_interval_minutes(True, cfg),
    }
    if compact:
        record["run_dir"] = str(run_dir / f"{local.strftime('%H%M%S')}.json")
        record["gemini_raw_text"] = None
        record["gemini_usage"] = None
        Path(record["run_dir"]).write_text(json.dumps(record, separators=(",", ":"), default=str))
    else:
        (run_dir / "validated.json").write_text(json.dumps(record, indent=2, default=str))
        with open(run_dir.parent / "signals.jsonl", "a") as f:
            f.write(json.dumps({"run_dir": str(run_dir), **record}, default=str) + "\n")
    log.info("throttle skip logged -> %s", record["run_dir"])
    return record


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
    scan_interval = interval
    cycles = 0
    last = {}
    pub = Publisher(cfg)
    lucid_task = None
    log.info("scheduler: every %d min aligned to the clock (+%ds)%s", interval, sc["align_offset_seconds"],
             " [DRY RUN]" if dry_run else "")
    try:
        while max_cycles is None or cycles < max_cycles:
            wake = next_boundary(datetime.now(timezone.utc), interval, sc["align_offset_seconds"])
            nxt = next_boundary(datetime.now(timezone.utc), scan_interval, sc["align_offset_seconds"])
            write_status(cfg, "running", scan_interval, next_run_at=nxt.isoformat(), **last)
            pub.publish(f"running, next {nxt.strftime('%H:%M')}Z" + (" (signal)" if cycles else ""))
            log.info("next cycle at %s UTC (countdown interval %d min, next scan %s)",
                     wake.strftime("%Y-%m-%d %H:%M:%S"), scan_interval, nxt.strftime("%H:%M:%S"))
            await asyncio.sleep(max(0, (wake - datetime.now(timezone.utc)).total_seconds()))
            started = datetime.now(timezone.utc).isoformat()
            write_status(cfg, "analyzing", scan_interval, cycle_started_at=started, **last)
            # (no push here: the page derives "Reading The Market" from the clock; saves a Pages deploy per cycle)
            t0 = time.monotonic()
            res = {}
            try:
                res = await run_once(cfg, dry_run=dry_run, force=False, model=model)
            except Exception:
                log.exception("cycle failed")
            scan_interval = int((res or {}).get("scan_interval_minutes") or scan_interval)
            # Lucid paper sim runs in the background so the site publish is never delayed (no extra Gemini call)
            if lucid_task is not None and not lucid_task.done():
                await lucid_task
            if not dry_run:
                lucid_task = asyncio.create_task(lucid_step(cfg, (res or {}).get("record")))
            if not (res or {}).get("skipped"):
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
