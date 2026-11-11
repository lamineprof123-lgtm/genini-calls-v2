# Lucid paper confluence report

Post-reset account only (pre-reset trades listed separately); paper trades only; net P&L after round-trip commission

Updated: 2026-11-12 02:24:21 (Dubai UTC+4)

**Every bucket with n < 20 is LOW CONFIDENCE. Descriptive only; no proven edge.**

Account reset at 2026-11-11T19:08:31.906022+00:00 (gemini-only: no SL/TP). Tables below are POST-RESET only; pre-reset trades are kept as history and listed separately.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| Post-reset (since 2026-11-11T19:08Z) | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |
| Pre-reset (archived account history) | 9 | 11.1% | $-3,201.95 | $757.25 | $-494.90 | $-355.77 | LOW CONFIDENCE |
| All closed trades (both) | 10 | 10.0% | $-5,555.45 | $757.25 | $-701.41 | $-555.54 | LOW CONFIDENCE |

## Pre-reset history by symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 4 | 25.0% | $235.25 | $757.25 | $-174.00 | $58.81 | LOW CONFIDENCE |
| GBPUSD | 2 | 0.0% | $-908.50 | n/a | $-454.25 | $-454.25 | LOW CONFIDENCE |
| US100 | 2 | 0.0% | $-1,790.10 | n/a | $-895.05 | $-895.05 | LOW CONFIDENCE |
| XAUUSD | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |

Pre-reset entry skips: 460

## Session

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| NEW_YORK_SESSION | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Value Area Position

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| above_vah | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Cvd Agrees

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Rvol Spike

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Vwap Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Vwap Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Confidence Band

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| >=0.8 | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| US100 | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Exit Reason

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| gemini_close | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## Top combinations: Session × Value area × CVD agreement

Ranked by net P&L; small samples are not a strategy recommendation.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| NEW_YORK_SESSION × above_vah × CVD yes | 1 | 0.0% | $-2,353.50 | n/a | $-2,353.50 | $-2,353.50 | LOW CONFIDENCE |

## DLL / MLL / breaker breaches (log-only, 2)

- 2026-11-11T20:49:00+00:00 DLL: equity $98,002.13 vs level $98,200.00 (log-only (gemini_only): no block, no forced close)
- 2026-11-11T21:56:00+00:00 MLL: equity $96,987.10 vs level $97,000.00 (log-only (gemini_only): no block, no forced close)

## Entry skips (38)

| Reason | Count |
|---|---:|
| already_position_or_pending | 35 |
| session_blocked | 1 |
| stop 0.25 atr from entry (min 0.25) | 2 |

Legacy records without telemetry remain in the totals and are grouped as unknown.
MFE/MAE are gross price excursions, not hypothetical net realized P&L. Bar-data exit-bar extremes are ambiguous.
