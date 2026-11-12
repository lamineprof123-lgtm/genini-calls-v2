# Lucid paper confluence report

Post-reset account only (pre-reset trades listed separately); paper trades only; net P&L after round-trip commission

Updated: 2026-11-12 13:28:35 (Dubai UTC+4)

**Every bucket with n < 20 is LOW CONFIDENCE. Descriptive only; no proven edge.**

Account reset at 2026-11-12T09:00:41.662867+00:00 (reset5: exit-check start-delay fix). Tables below are POST-RESET only; pre-reset trades are kept as history and listed separately.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| Post-reset (since 2026-11-12T09:00Z) | 2 | 50.0% | $-604.40 | $110.80 | $-715.20 | $-302.20 | LOW CONFIDENCE |
| Pre-reset (archived account history) | 15 | 20.0% | $-4,054.05 | $968.08 | $-579.86 | $-270.27 | LOW CONFIDENCE |
| All closed trades (both) | 17 | 23.5% | $-4,658.45 | $753.76 | $-590.27 | $-274.03 | LOW CONFIDENCE |

## Pre-reset history by symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 7 | 14.3% | $-410.35 | $757.25 | $-194.60 | $-58.62 | LOW CONFIDENCE |
| GBPUSD | 2 | 0.0% | $-908.50 | n/a | $-454.25 | $-454.25 | LOW CONFIDENCE |
| US100 | 3 | 0.0% | $-4,143.60 | n/a | $-1,381.20 | $-1,381.20 | LOW CONFIDENCE |
| XAUUSD | 3 | 66.7% | $1,408.40 | $1,073.50 | $-738.60 | $469.47 | LOW CONFIDENCE |

Pre-reset entry skips: 498

## Session

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION | 2 | 50.0% | $-604.40 | $110.80 | $-715.20 | $-302.20 | LOW CONFIDENCE |

## Value Area Position

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| above_vah | 1 | 0.0% | $-715.20 | n/a | $-715.20 | $-715.20 | LOW CONFIDENCE |
| below_val | 1 | 100.0% | $110.80 | $110.80 | n/a | $110.80 | LOW CONFIDENCE |

## Cvd Agrees

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 2 | 50.0% | $-604.40 | $110.80 | $-715.20 | $-302.20 | LOW CONFIDENCE |

## Rvol Spike

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 2 | 50.0% | $-604.40 | $110.80 | $-715.20 | $-302.20 | LOW CONFIDENCE |

## Vwap Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 2 | 50.0% | $-604.40 | $110.80 | $-715.20 | $-302.20 | LOW CONFIDENCE |

## Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 2 | 50.0% | $-604.40 | $110.80 | $-715.20 | $-302.20 | LOW CONFIDENCE |

## Vwap Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 2 | 50.0% | $-604.40 | $110.80 | $-715.20 | $-302.20 | LOW CONFIDENCE |

## Confidence Band

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| 0.7-0.8 | 1 | 0.0% | $-715.20 | n/a | $-715.20 | $-715.20 | LOW CONFIDENCE |
| >=0.8 | 1 | 100.0% | $110.80 | $110.80 | n/a | $110.80 | LOW CONFIDENCE |

## Symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 1 | 0.0% | $-715.20 | n/a | $-715.20 | $-715.20 | LOW CONFIDENCE |
| XAUUSD | 1 | 100.0% | $110.80 | $110.80 | n/a | $110.80 | LOW CONFIDENCE |

## Exit Reason

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| gemini_close | 2 | 50.0% | $-604.40 | $110.80 | $-715.20 | $-302.20 | LOW CONFIDENCE |

## Top combinations: Session × Value area × CVD agreement

Ranked by net P&L; small samples are not a strategy recommendation.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION × below_val × CVD yes | 1 | 100.0% | $110.80 | $110.80 | n/a | $110.80 | LOW CONFIDENCE |
| LONDON_SESSION × above_vah × CVD yes | 1 | 0.0% | $-715.20 | n/a | $-715.20 | $-715.20 | LOW CONFIDENCE |

## DLL / MLL / breaker breaches (log-only, 1)

- 2026-11-12T09:26:00+00:00 DLL: equity $98,164.00 vs level $98,200.00 (log-only (gemini_only): no block, no forced close)

## Entry skips (0)

| Reason | Count |
|---|---:|
| None yet | 0 |

Legacy records without telemetry remain in the totals and are grouped as unknown.
MFE/MAE are gross price excursions, not hypothetical net realized P&L. Bar-data exit-bar extremes are ambiguous.
