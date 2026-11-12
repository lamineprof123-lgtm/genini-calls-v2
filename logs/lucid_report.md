# Lucid paper confluence report

Post-reset account only (pre-reset trades listed separately); paper trades only; net P&L after round-trip commission

Updated: 2026-11-12 12:44:31 (Dubai UTC+4)

**Every bucket with n < 20 is LOW CONFIDENCE. Descriptive only; no proven edge.**

Account reset at 2026-11-12T01:26:22.307439+00:00 (reset4: exit-prompt-v2 (closed-bar invalidation, wall harvest, giveback ratchet)). Tables below are POST-RESET only; pre-reset trades are kept as history and listed separately.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| Post-reset (since 2026-11-12T01:26Z) | 3 | 33.3% | $1,030.40 | $1,410.80 | $-190.20 | $343.47 | LOW CONFIDENCE |
| Pre-reset (archived account history) | 11 | 18.2% | $-4,819.25 | $746.73 | $-701.41 | $-438.11 | LOW CONFIDENCE |
| All closed trades (both) | 14 | 21.4% | $-3,788.85 | $968.08 | $-608.46 | $-270.63 | LOW CONFIDENCE |

## Pre-reset history by symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 4 | 25.0% | $235.25 | $757.25 | $-174.00 | $58.81 | LOW CONFIDENCE |
| GBPUSD | 2 | 0.0% | $-908.50 | n/a | $-454.25 | $-454.25 | LOW CONFIDENCE |
| US100 | 3 | 0.0% | $-4,143.60 | n/a | $-1,381.20 | $-1,381.20 | LOW CONFIDENCE |
| XAUUSD | 2 | 50.0% | $-2.40 | $736.20 | $-738.60 | $-1.20 | LOW CONFIDENCE |

Pre-reset entry skips: 498

## Session

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION | 3 | 33.3% | $1,030.40 | $1,410.80 | $-190.20 | $343.47 | LOW CONFIDENCE |

## Value Area Position

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| below_val | 1 | 100.0% | $1,410.80 | $1,410.80 | n/a | $1,410.80 | LOW CONFIDENCE |
| inside | 2 | 0.0% | $-380.40 | n/a | $-190.20 | $-190.20 | LOW CONFIDENCE |

## Cvd Agrees

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 3 | 33.3% | $1,030.40 | $1,410.80 | $-190.20 | $343.47 | LOW CONFIDENCE |

## Rvol Spike

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 3 | 33.3% | $1,030.40 | $1,410.80 | $-190.20 | $343.47 | LOW CONFIDENCE |

## Vwap Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 3 | 33.3% | $1,030.40 | $1,410.80 | $-190.20 | $343.47 | LOW CONFIDENCE |

## Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 3 | 33.3% | $1,030.40 | $1,410.80 | $-190.20 | $343.47 | LOW CONFIDENCE |

## Vwap Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 3 | 33.3% | $1,030.40 | $1,410.80 | $-190.20 | $343.47 | LOW CONFIDENCE |

## Confidence Band

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| 0.7-0.8 | 2 | 0.0% | $-380.40 | n/a | $-190.20 | $-190.20 | LOW CONFIDENCE |
| >=0.8 | 1 | 100.0% | $1,410.80 | $1,410.80 | n/a | $1,410.80 | LOW CONFIDENCE |

## Symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 2 | 0.0% | $-380.40 | n/a | $-190.20 | $-190.20 | LOW CONFIDENCE |
| XAUUSD | 1 | 100.0% | $1,410.80 | $1,410.80 | n/a | $1,410.80 | LOW CONFIDENCE |

## Exit Reason

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| gemini_close | 3 | 33.3% | $1,030.40 | $1,410.80 | $-190.20 | $343.47 | LOW CONFIDENCE |

## Top combinations: Session × Value area × CVD agreement

Ranked by net P&L; small samples are not a strategy recommendation.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION × below_val × CVD yes | 1 | 100.0% | $1,410.80 | $1,410.80 | n/a | $1,410.80 | LOW CONFIDENCE |
| LONDON_SESSION × inside × CVD yes | 2 | 0.0% | $-380.40 | n/a | $-190.20 | $-190.20 | LOW CONFIDENCE |

## DLL / MLL / breaker breaches (log-only, 0)

None.

## Entry skips (0)

| Reason | Count |
|---|---:|
| None yet | 0 |

Legacy records without telemetry remain in the totals and are grouped as unknown.
MFE/MAE are gross price excursions, not hypothetical net realized P&L. Bar-data exit-bar extremes are ambiguous.
