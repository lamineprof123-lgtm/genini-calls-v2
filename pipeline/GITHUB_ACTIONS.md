# Running on GitHub Actions

`.github/workflows/pipeline.yml` runs the whole pipeline (charts -> Gemini -> server-side scalp SL/TP ->
Lucid 100K paper sim -> site JSON -> Pages) inside one long job:

- **Cadence:** a cycle every `interval_minutes` (default 2), aligned to the clock (+20 s). `status.json`
  carries the same interval so the panel countdown matches.
- **Loop length:** ~5h30m per run, then a queued successor run takes over (handoff gap ~1 cycle).
  The successor is started by the `*/5` cron or by the loop's own `workflow_dispatch` 20 min before its end.
- **One loop at a time:** lock files on the `pipeline-data` branch (`state/loop_holder.json`,
  `state/loop_successor.json`). Extra cron runs exit in the `gate` job within seconds.
- **State:** `pipeline-data` branch. Committed after every cycle (Lucid account, circuit breaker, logs, site JSON),
  so a killed run loses at most one cycle; Lucid replays 1-minute bars since `last_processed`, so any gap is caught up.
- **Pages:** deployed from inside the loop after every cycle (`tools/deploy-pages.mjs`, same API as
  `actions/deploy-pages`); failed deploys back off for 1-4 cycles. `pages.yml` still deploys on pushes to `main`
  and overlays the live JSON from `pipeline-data`.
- **Secrets:** `GEMINI_API_KEY` repo secret (never in files). Optional repo variable `GEMINI_MODEL`.
- **Stop / start:** Actions -> Signal Pipeline -> Run workflow -> action `stop` / `start`
  (or add/remove a `STOP` file on `pipeline-data`).
- **Single cycle locally:** `SIGNALS_DATA_DIR=... python run.py gha-loop --data DIR --max-cycles 1 [--dry-run]`.
