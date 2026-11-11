# pipeline-data (machine-written)

Written every cycle by the GitHub Actions signal loop (`.github/workflows/pipeline.yml` on `main`).
Do not edit by hand while the loop runs, except:

- **Stop the loop:** add a file named `STOP` at the root of this branch (or run the workflow with action `stop`).
  Start again with action `start` (removes `STOP`).

Contents
- `status.json`, `latest_signal.json`, `history.json`: the site data (deployed to Pages with `main`'s index.html).
- `state/`: Lucid 100K paper account (`state/lucid/account.json`), circuit breaker, bar gate, loop lock
  (`loop_holder.json` / `loop_successor.json`).
- `logs/YYYY-MM-DD/`: `cycles/HHMMSS.json` (one compact record per cycle, kept 14 days),
  `lucid_trades.jsonl` / `lucid_events.jsonl` (paper trades + telemetry snapshots, kept forever),
  `logs/lucid_report.{json,md}` (confluence breakdown, refreshed every cycle).
  Older box-era logs (2026-11-09..11) carried over without chart images.
