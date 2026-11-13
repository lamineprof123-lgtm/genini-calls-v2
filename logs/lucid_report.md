# Lucid paper confluence report

Post-reset account only (pre-reset trades listed separately); paper trades only; net P&L after round-trip commission

Updated: 2026-11-13 05:08:21 (Dubai UTC+4)

**Every bucket with n < 20 is LOW CONFIDENCE. Descriptive only; no proven edge.**

Account reset at 2026-11-12T18:11:32.231856+00:00 (t255u: reset all accounts to $100k after the last model change (uniform 1 ATR bracket)). Tables below are POST-RESET only; pre-reset trades are kept as history and listed separately.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| Post-reset (since 2026-11-12T18:11Z) | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |
| Pre-reset (archived account history) | 94 | 41.5% | $-5,731.15 | $470.19 | $-437.61 | $-60.97 | n >= 20 |
| All closed trades (both) | 95 | 41.1% | $-7,634.65 | $470.19 | $-463.79 | $-80.36 | n >= 20 |

## Pre-reset history by symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 16 | 25.0% | $-2,454.15 | $365.54 | $-326.36 | $-153.38 | LOW CONFIDENCE |
| GBPUSD | 17 | 23.5% | $-1,212.80 | $101.11 | $-124.40 | $-71.34 | LOW CONFIDENCE |
| US100 | 30 | 50.0% | $-2,998.60 | $482.70 | $-682.61 | $-99.95 | n >= 20 |
| XAUUSD | 31 | 51.6% | $934.40 | $576.90 | $-553.07 | $30.14 | n >= 20 |

Pre-reset entry skips: 525

## Session

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| NEW_YORK_SESSION | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Value Area Position

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| above_vah | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Cvd Agrees

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Rvol Spike

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Vwap Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Vwap Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Confidence Band

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| 0.7-0.8 | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| US100 | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Exit Reason

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| stop_1atr | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## Top combinations: Session × Value area × CVD agreement

Ranked by net P&L; small samples are not a strategy recommendation.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| NEW_YORK_SESSION × above_vah × CVD no | 1 | 0.0% | $-1,903.50 | n/a | $-1,903.50 | $-1,903.50 | LOW CONFIDENCE |

## DLL / MLL / breaker breaches (log-only, 1)

- 2026-11-12T19:41:00+00:00 DLL: equity $98,096.50 vs level $98,200.00 (log-only (gemini_only): no block, no forced close)

## Entry skips (2)

| Reason | Count |
|---|---:|
| session_blocked | 2 |

Legacy records without telemetry remain in the totals and are grouped as unknown.
MFE/MAE are gross price excursions, not hypothetical net realized P&L. Bar-data exit-bar extremes are ambiguous.
