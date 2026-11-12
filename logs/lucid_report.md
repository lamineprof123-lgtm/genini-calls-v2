# Lucid paper confluence report

Post-reset account only (pre-reset trades listed separately); paper trades only; net P&L after round-trip commission

Updated: 2026-11-12 14:58:31 (Dubai UTC+4)

**Every bucket with n < 20 is LOW CONFIDENCE. Descriptive only; no proven edge.**

Account reset at 2026-11-12T10:14:47.081605+00:00 (reset6: profit-lock exit rules). Tables below are POST-RESET only; pre-reset trades are kept as history and listed separately.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| Post-reset (since 2026-11-12T10:14Z) | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |
| Pre-reset (archived account history) | 18 | 27.8% | $-4,581.95 | $618.31 | $-590.27 | $-254.55 | LOW CONFIDENCE |
| All closed trades (both) | 20 | 35.0% | $-4,366.05 | $472.49 | $-590.27 | $-218.30 | n >= 20 |

## Pre-reset history by symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 8 | 12.5% | $-1,125.55 | $757.25 | $-268.97 | $-140.69 | LOW CONFIDENCE |
| GBPUSD | 2 | 0.0% | $-908.50 | n/a | $-454.25 | $-454.25 | LOW CONFIDENCE |
| US100 | 4 | 25.0% | $-4,067.10 | $76.50 | $-1,381.20 | $-1,016.77 | LOW CONFIDENCE |
| XAUUSD | 4 | 75.0% | $1,519.20 | $752.60 | $-738.60 | $379.80 | LOW CONFIDENCE |

Pre-reset entry skips: 498

## Session

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |

## Value Area Position

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| above_vah | 1 | 100.0% | $213.00 | $213.00 | n/a | $213.00 | LOW CONFIDENCE |
| inside | 1 | 100.0% | $2.90 | $2.90 | n/a | $2.90 | LOW CONFIDENCE |

## Cvd Agrees

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |

## Rvol Spike

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |

## Vwap Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |

## Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |

## Vwap Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |

## Confidence Band

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| 0.7-0.8 | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |

## Symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 1 | 100.0% | $2.90 | $2.90 | n/a | $2.90 | LOW CONFIDENCE |
| US100 | 1 | 100.0% | $213.00 | $213.00 | n/a | $213.00 | LOW CONFIDENCE |

## Exit Reason

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| gemini_close | 2 | 100.0% | $215.90 | $107.95 | n/a | $107.95 | LOW CONFIDENCE |

## Top combinations: Session × Value area × CVD agreement

Ranked by net P&L; small samples are not a strategy recommendation.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION × above_vah × CVD yes | 1 | 100.0% | $213.00 | $213.00 | n/a | $213.00 | LOW CONFIDENCE |
| LONDON_SESSION × inside × CVD yes | 1 | 100.0% | $2.90 | $2.90 | n/a | $2.90 | LOW CONFIDENCE |

## DLL / MLL / breaker breaches (log-only, 1)

- 2026-11-12T10:44:00+00:00 DLL: equity $98,049.96 vs level $98,200.00 (log-only (gemini_only): no block, no forced close)

## Entry skips (1)

| Reason | Count |
|---|---:|
| sizing_unreachable | 1 |

Legacy records without telemetry remain in the totals and are grouped as unknown.
MFE/MAE are gross price excursions, not hypothetical net realized P&L. Bar-data exit-bar extremes are ambiguous.
