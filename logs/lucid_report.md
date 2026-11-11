# Lucid paper confluence report

All days and all accounts; paper trades only; net P&L after round-trip commission

Updated: 2026-11-11 14:28:30 (Dubai UTC+4)

**Every bucket with n < 20 is LOW CONFIDENCE. Descriptive only; no proven edge.**

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| All closed trades | 4 | 25.0% | $-659.60 | $757.25 | $-472.28 | $-164.90 | LOW CONFIDENCE |

## Session

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION | 4 | 25.0% | $-659.60 | $757.25 | $-472.28 | $-164.90 | LOW CONFIDENCE |

## Value Area Position

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| below_val | 3 | 33.3% | $79.00 | $757.25 | $-339.12 | $26.33 | LOW CONFIDENCE |
| inside | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |

## Cvd Agrees

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |
| yes | 3 | 33.3% | $79.00 | $757.25 | $-339.12 | $26.33 | LOW CONFIDENCE |

## Rvol Spike

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 2 | 0.0% | $-931.35 | n/a | $-465.68 | $-465.68 | LOW CONFIDENCE |
| yes | 2 | 50.0% | $271.75 | $757.25 | $-485.50 | $135.88 | LOW CONFIDENCE |

## Vwap Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 4 | 25.0% | $-659.60 | $757.25 | $-472.28 | $-164.90 | LOW CONFIDENCE |

## Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 4 | 25.0% | $-659.60 | $757.25 | $-472.28 | $-164.90 | LOW CONFIDENCE |

## Vwap Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 4 | 25.0% | $-659.60 | $757.25 | $-472.28 | $-164.90 | LOW CONFIDENCE |

## Confidence Band

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| 0.6-0.7 | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |
| >=0.8 | 3 | 33.3% | $79.00 | $757.25 | $-339.12 | $26.33 | LOW CONFIDENCE |

## Symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 2 | 50.0% | $564.50 | $757.25 | $-192.75 | $282.25 | LOW CONFIDENCE |
| GBPUSD | 1 | 0.0% | $-485.50 | n/a | $-485.50 | $-485.50 | LOW CONFIDENCE |
| XAUUSD | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |

## Exit Reason

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| ema50_cross_15m | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |
| stop | 1 | 0.0% | $-485.50 | n/a | $-485.50 | $-485.50 | LOW CONFIDENCE |
| target | 1 | 100.0% | $757.25 | $757.25 | n/a | $757.25 | LOW CONFIDENCE |
| time_cut_no_mfe | 1 | 0.0% | $-192.75 | n/a | $-192.75 | $-192.75 | LOW CONFIDENCE |

## Top combinations: Session × Value area × CVD agreement

Ranked by net P&L; small samples are not a strategy recommendation.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION × below_val × CVD yes | 3 | 33.3% | $79.00 | $757.25 | $-339.12 | $26.33 | LOW CONFIDENCE |
| LONDON_SESSION × inside × CVD no | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |

## Entry skips (151)

| Reason | Count |
|---|---:|
| already_position_or_pending | 103 |
| chase | 6 |
| entry 1.15 atr from market (max 1.0) | 2 |
| sell requires take_profit < entry < stop_loss | 1 |
| session_blocked | 2 |
| sizing_unreachable | 20 |
| stop 0.24 atr from entry (min 0.25) | 15 |
| stop 0.25 atr from entry (min 0.25) | 2 |

Legacy records without telemetry remain in the totals and are grouped as unknown.
MFE/MAE are gross price excursions, not hypothetical net realized P&L. Bar-data exit-bar extremes are ambiguous.
