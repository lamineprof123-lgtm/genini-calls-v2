#!/usr/bin/env python3
"""Gemini vision trading-signal pipeline. SIGNALS ONLY - never places orders.

  python run.py once [--dry-run] [--model M]          one cycle now
  python run.py loop [--dry-run] [--interval 15]      clock-aligned scheduler (default 10 min)
  python run.py lucid-status                         Lucid 100K paper-account summary
  python run.py models                                list Flash models available to your key
  python run.py gha-loop --data DIR --role holder    GitHub Actions in-job loop (state/logs/site JSON in DIR)
  python run.py gha-loop --data DIR --max-cycles 1   single cycle: data -> charts -> Gemini -> Lucid -> commit
  python run.py exit-sample --symbol EURUSD --side SELL --out DIR   exit-manager chart + prompt (fake position)
"""
import argparse, asyncio, logging, os, sys

from signals.config import load_config, load_env
from signals.pipeline import run_loop, run_once


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["once", "loop", "models", "seed-site", "pause", "lucid-status", "lucid-step", "lucid-report", "gha-loop", "exit-sample"])
    ap.add_argument("path", nargs="?", help="seed-site: a logs/.../validated.json")
    ap.add_argument("--dry-run", action="store_true", help="render + validate with a mocked Gemini response")
    ap.add_argument("--model", help="override [gemini].model (entry scans only)")
    ap.add_argument("--exit-model", help="override [gemini].exit_model (18s exit checks only)")
    ap.add_argument("--probe", help="models command: comma-separated IDs to probe (default: configured entry,exit)")
    ap.add_argument("--interval", type=int, help="loop interval minutes (15 = align to M15 closes)")
    ap.add_argument("--max-cycles", type=int)
    ap.add_argument("--config")
    ap.add_argument("--data", help="gha-loop: data-branch checkout (state/, logs/, site JSON)")
    ap.add_argument("--role", default="holder", choices=["holder", "successor"])
    ap.add_argument("--run-id", default=os.environ.get("LOOP_RUN_ID", "local"))
    ap.add_argument("--max-runtime-min", type=float, default=float(os.environ.get("SIGNALS_MAX_RUNTIME_MIN", "330")))
    ap.add_argument("--hard-cap-min", type=float, default=float(os.environ.get("SIGNALS_HARD_CAP_MIN", "350")))
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--symbol", default="EURUSD", help="exit-sample: symbol of the fake open position")
    ap.add_argument("--side", default="SELL", choices=["BUY", "SELL"], help="exit-sample: fake position side")
    ap.add_argument("--out", default="exit_sample", help="exit-sample: output folder")
    a = ap.parse_args()
    load_env()
    if a.cmd == "gha-loop":
        if not a.data:
            ap.error("gha-loop needs --data")
        os.environ["SIGNALS_DATA_DIR"] = a.data
        if a.interval:
            os.environ["SIGNALS_INTERVAL_MINUTES"] = str(a.interval)
    cfg = load_config(a.config)
    if getattr(a, "exit_model", None):
        cfg.setdefault("gemini", {})["exit_model"] = a.exit_model
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    for noisy in ("httpx", "httpx2", "google_genai", "yfinance"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if a.cmd == "models":
        from signals.model_verify import verify_models, format_report
        probe = [x.strip() for x in (a.probe or "").split(",") if x.strip()] or None
        res = verify_models(cfg, probe)
        print(format_report(res))
        if not res.get("ok"):
            sys.exit(1)
        return
    if a.cmd == "seed-site":  # rebuild site/latest_signal.json from a past run
        import json
        from signals.site import write_latest_signal
        print(json.dumps(write_latest_signal(json.load(open(a.path)), cfg), indent=1))
        return
    if a.cmd == "pause":  # mark the site paused (use after killing the loop with SIGKILL)
        from signals.site import write_status
        write_status(cfg, "paused")
        return
    if a.cmd == "lucid-report":
        from signals.lucid_report import write_report
        _, md = write_report(cfg["general"]["logs_dir"])
        print(md)
        return
    if a.cmd == "exit-sample":  # exit-manager chart + prompt for a FAKE open position (no Gemini call)
        import json
        from signals.exit_manager import render_sample
        res = asyncio.run(render_sample(cfg, a.symbol, 1 if a.side == "BUY" else -1, a.out))
        print(json.dumps(res, indent=1, default=str))
        return
    if a.cmd == "lucid-status":  # paper prop-account summary (no network, no Gemini)
        from signals.lucid import LucidSim, format_status
        print(format_status(LucidSim(cfg).summary(), cfg))
        return
    if a.cmd == "lucid-step":  # replay M1 bars up to now without new signals (no Gemini call)
        from signals.lucid import lucid_step, LucidSim, format_status
        print(asyncio.run(lucid_step(cfg)))
        print(format_status(LucidSim(cfg).summary(), cfg))
        return
    if a.cmd == "gha-loop":
        from pathlib import Path
        from signals.gha_loop import run_gha_loop
        reason = asyncio.run(run_gha_loop(cfg, Path(a.data).resolve(), a.run_id, a.role, dry_run=a.dry_run,
                                          max_runtime_min=a.max_runtime_min, hard_cap_min=a.hard_cap_min,
                                          deploy=not a.no_deploy and bool(os.environ.get("GITHUB_ACTIONS")),
                                          max_cycles=a.max_cycles, model=a.model))
        print(f"exit_reason={reason}")
        return
    if a.cmd == "once":
        res = asyncio.run(run_once(cfg, dry_run=a.dry_run, force=True, model=a.model))
        print(res.get("run_dir"))
    else:
        import signal

        async def _main():
            task = asyncio.current_task()
            lp = asyncio.get_running_loop()
            for sg in (signal.SIGTERM, signal.SIGINT):
                lp.add_signal_handler(sg, task.cancel)  # -> finally: status "paused"
            try:
                await run_loop(cfg, dry_run=a.dry_run, model=a.model, interval=a.interval, max_cycles=a.max_cycles)
            except asyncio.CancelledError:
                pass
        asyncio.run(_main())


if __name__ == "__main__":
    main()
