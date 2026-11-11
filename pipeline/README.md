# gemini-signals

Automated vision trading-signal pipeline: 15-minute candles → Lightweight Charts PNGs (headless Chromium)
→ one multimodal Gemini request → local validation, R:R check, position sizing, daily-drawdown circuit breaker
→ JSON signals + screenshots logged to `logs/YYYY-MM-DD/`.

**SIGNALS ONLY. Nothing in this project places, modifies, or cancels orders.**

## Layout
| Path | What |
|---|---|
| `run.py` | CLI: `once`, `loop`, `models` |
| `config.toml` | All settings: symbols, data providers, chart colors/params, model, risk, scheduler |
| `chart.html` + `static/lightweight-charts.standalone.production.js` | Chart template (Lightweight Charts v5.2.1, vendored = works offline) |
| `signals/data_providers.py` | Pluggable providers: `dukascopy`, `binance`, `yfinance`, `mt5` (Windows/bridge), `rest_broker` (stub) |
| `signals/indicators.py` | Session VWAP, EMA20 ± 2×ATR14 channel, S/R (session/prev/24h H/L + 12-bar pivots) |
| `signals/renderer.py` | Playwright: one browser, one page per symbol, `asyncio.gather`, PNG bytes in memory |
| `templates/system_prompt.j2` | Strict system prompt (Jinja2, filled from config) |
| `signals/gemini_client.py` | google-genai call, `response_mime_type=application/json` + pydantic `response_schema`, dry-run mock |
| `signals/validator.py` | JSON parse, per-signal checks, local R:R recompute, sizing |
| `signals/risk.py` | Equity sources (`paper_file`, `mt5`) + `DailyDrawdownBreaker` (state persisted) |
| `signals/pipeline.py` | One cycle + clock-aligned async scheduler |
| `tests/test_risk.py` | Circuit breaker + validator unit tests |

## Setup (already done on this box)
```bash
cd /workspace/gemini-signals
python3 -m venv venv && ./venv/bin/pip install google-genai playwright pandas jinja2 yfinance httpx python-dotenv pytest
./venv/bin/playwright install chromium          # add --with-deps on a fresh Linux host
npm i dukascopy-node                            # Dukascopy data CLI (needs Node.js)
```
API key: `.env` (chmod 600, git-ignored) with `GEMINI_API_KEY=...`. Loaded automatically; never logged.

## Run
```bash
./venv/bin/python run.py once --dry-run         # real data + charts, MOCKED Gemini reply (no API cost)
./venv/bin/python run.py once                   # one live cycle (model from config.toml)
./venv/bin/python run.py once --model gemini-3.6-flash
./venv/bin/python run.py loop                   # every 5 min (interval_minutes) at :00/:05/:10... (+20 s)
./venv/bin/python run.py loop --interval 15     # align to M15 candle closes instead
./venv/bin/python run.py models                 # Flash models your key can use
./venv/bin/python -m pytest -q tests
```
Keep the loop alive with `nohup ./venv/bin/python run.py loop >> logs/loop.log 2>&1 &`, tmux, or systemd.

**5-min polling vs 15-min candles:** a 5-minute cadence sees a new closed M15 candle only every third cycle.
With `skip_if_no_new_candle = true` (default) those cycles are logged as "skipping" and no Gemini call is made.
`--interval 15` (or `interval_minutes = 15`) aligns every cycle to a fresh candle close.

## Signal page (one-pager)
`site/` is a pure static site: `index.html` (inline CSS/JS, no CDNs) + `status.json` + `latest_signal.json`,
fetched with relative paths every 4 s, so the folder can be published unchanged (e.g. GitHub Pages).
It contains no secrets, logs or charts.

```bash
./serve_site.sh start     # http://127.0.0.1:8787/  (localhost only, nohup background)
./serve_site.sh status
./serve_site.sh stop
```
Page states (one focal element at a time):
- **Live, waiting** - big countdown to the next run (`status.next_run_at`); quiet "No trade · ..." line if the last cycle had no trade.
- **Analyzing...** - from the boundary until the cycle writes its result (Gemini can take 1-2 min with retries).
- **Signal** - each BUY/SELL large with Entry / Stop / Target, small R:R; HOLDs collapse to one quiet line;
  a small "Next signal in mm:ss" below. Time + model only in the muted footer.
- **Paused / Offline** - "Paused" badge + the last signal with its timestamp, no countdown. Offline = status says
  running but nothing updated for 4+ minutes past the scheduled time.

Files written for the page: `site/status.json` (by `run.py loop`: `running` with `next_run_at`, `analyzing` with
`cycle_started_at`, `paused` on exit/SIGTERM/Ctrl-C; plus `interval_minutes`, `last_cycle_*`) and
`site/latest_signal.json` (after every cycle, also `run.py once`). Helpers:
`run.py seed-site logs/.../validated.json` rebuilds latest_signal.json from a past run;
`run.py pause` marks the page paused (e.g. after a `kill -9`).

## Outputs
`logs/YYYY-MM-DD/HHMMSS/` (session timezone, default Asia/Dubai): `<SYMBOL>.png`, `gemini_raw.json`
(raw model text, model id, token usage, errors), `validated.json` (final signals, equity, breaker state,
numeric context). `logs/YYYY-MM-DD/signals.jsonl` appends every run. `state/latest_signals.json` is the
latest validated batch for any downstream consumer.

Each validated signal: `symbol, action (BUY/SELL/HOLD), entry, stop_loss, take_profit, rr_local, confidence,
risk_pct, risk_amount, position_units, position_lots, downgraded, reasons[]` plus the model's own fields.

## Validation & guardrails
Downgraded to HOLD (with a reason) when: symbol missing, invalid action, missing prices, SL/TP on the wrong
side of entry, entry more than `max_entry_distance_atr` ATR from last close, stop tighter than
`min_stop_distance_atr` ATR, **locally recomputed R:R < `min_rr`** (2.5; set 3.0 for 1:3), or confidence <
`min_confidence`. Sizing: `risk_pct` (model suggestion, capped at `max_risk_pct`) of equity ÷ stop distance =
base units; lots = units ÷ `contract_size` (set per symbol for your broker).

**Circuit breaker:** at the first check of each session day (`session_tz`), start-of-day equity is recorded.
If `(start − equity)/start > max_daily_loss_pct` (3.5%), every signal becomes HOLD, the Gemini call is skipped,
and the halt is latched in `state/circuit_breaker.json` until the next session day (survives restarts).
Equity comes from `state/paper_equity.json` (`{"equity": 100000}`) by default; set `[equity] provider = "mt5"`
on a Windows host with MetaTrader 5.

## Data sources & symbol mapping
| Symbol | Primary | Fallbacks | Notes |
|---|---|---|---|
| XAUUSD | Dukascopy `xauusd` (spot, tick volume) | yfinance `GC=F` | GC=F = COMEX gold futures proxy |
| EURUSD | Dukascopy `eurusd` | yfinance `EURUSD=X` | yfinance FX has no volume → equal-weight VWAP |
| GBPUSD | Dukascopy `gbpusd` | yfinance `GBPUSD=X` | |
| US100 | Dukascopy `usatechidxusd` (USA Tech 100 CFD) | yfinance `NQ=F` | NQ=F = E-mini Nasdaq-100 futures proxy |
| BTCUSD | Binance `BTCUSDT` (exchange volume) | Dukascopy `btcusd`, yfinance `BTC-USD` | USDT proxy for USD |

Prices from Dukascopy/Binance will differ slightly from your broker's feed. For broker-exact charts use
`mt5` (MetaTrader5 package is Windows-only; on Linux run it via a bridge such as mt5linux) or implement
`RestBrokerProvider.fetch()`, then put it first in each symbol's `chain`. Only closed candles are used.

## Chart spec (config `[chart.*]`)
Background `#0E1117`; candles bull `#26A69A` / bear `#EF5350` (body + wick); exactly 100 visible M15 candles,
fixed spacing, no scroll/zoom/autofit; volume histogram in the bottom 15% at low opacity; no oscillator panes.
- VWAP: session-anchored, offset 0, `#FF9800` 3px, no bands. Anchor 00:00 UTC (FX, gold, BTC);
  US100 uses `cme_globex` = 18:00 America/New_York (CME futures session open, where the overnight session begins).
- ATR channel: EMA20 baseline `#4B5563` 1px dashed; ±2.0×ATR14 bands upper `#00E676`, lower `#FF1744` 1px solid;
  8% fill between bands drawn by a custom series primitive (`BandFill` in `chart.html`).
- S/R: current session H/L, previous session H/L, rolling 24h H/L, pivot highs/lows (12-bar lookback);
  deduped within 0.35×ATR, max 3 per side; resistance `#E91E63`, support `#7C4DFF`, 2px dashed
  (Lightweight Charts only supports integer widths, so 1.5 → 2), right-axis labels `Session High`, `Prev Low`, `24h High`, `S/R`.

## Model
`[gemini] model = "gemini-3.6-flash"` with `fallback_models = ["gemini-3.6-flash", "gemini-3.8-flash"]`
(override the first choice with `--model`). Each model gets up to `max_attempts_per_model` (2) tries within
`retry_budget_seconds` (60 s, 45 s per request, backoff 1.5-5 s) with exponential backoff + jitter on 429/5xx/UNAVAILABLE/rate-limit errors;
402 (billing) and 404 (model not available) skip straight to the next model. If every model fails, all symbols
are HOLD and the attempts are logged in `gemini_raw.json` / `validated.json`. The answering model is logged.
For Gemini 3.x the request uses `thinking_level` ("low" for latency) and omits `temperature` (sampling params are deprecated from
3.6 on); `temperature` is only sent to older models. History: `gemini-2.0-flash` was shut down 2026-06-01 and
`gemini-2.5-flash` now returns 404 "no longer available to new users".

## Lucid 100K paper simulator (`signals/lucid.py`, config `[lucid]`)
PAPER ONLY. Each loop cycle (no extra Gemini call) queues the cycle's BUY/SELL signals for US100->MNQ/NQ,
XAUUSD->MGC/GC, EURUSD->6E, GBPUSD->6B (BTCUSD excluded). Fills/exits are replayed on Dukascopy 1-minute bid bars
(spot/CFD proxy for the futures price). Low-R:R sizing: fixed ~$750 **NET of round-trip commission** target
($500–$1000 net; skip if net would be below $500), stop = 2× net target capped at $1,500, contract count chosen so
the target distance is ~0.6 ATR(14,M15). Rules: EOD-trailing MLL (intraday binding), soft DLL, flat 16:45 ET,
payouts (40% consistency, $103,100 buffer, caps, 90/10), blown accounts pay a reset/new-eval fee and the next
account trades immediately.

**USER RULE OVERRIDE:** `max_minis = 40` (400 micros). Lucid's real LucidPro 100K aggregate cap is **6 minis /
60 micros**; this paper sim intentionally exceeds it per user request. DLL/MLL and `max_risk_usd` still bind.

State `state/lucid/account.json`; logs `logs/YYYY-MM-DD/lucid_trades.jsonl` + `lucid_events.jsonl`.
`python run.py lucid-status`. Tests: `tests/test_lucid.py`.

## Gemini exit manager (paper books, branch `exit-manager`)

`signals/exit_manager.py` copies every Lucid sim entry into two paper books and lets Gemini manage the exit on
the same M15 chart as the entry model plus a white ENTRY line (prompt: `templates/exit_prompt.j2`).
One Gemini call per open position every 18 s (`[exit_manager]` in `config.toml`), shared by both books:
`1-strike` exits on the first CLOSE; `2-strike` needs two consecutive CLOSEs while MFE < 0.8 ATR, one after.
Both keep the hard stop, 16:45 ET flat, 3.5% daily breaker and a 90-min max hold; no fixed TP / 2-bar time cut.
Logs: `logs/YYYY-MM-DD/exits.jsonl`, `logs/YYYY-MM-DD/exit_book_trades.jsonl`; summary: `books.json` (data root).
Review sample (no Gemini call): `python run.py exit-sample --symbol EURUSD --side SELL --out exit_sample`.
