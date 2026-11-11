# Lucid paper confluence report

All days and all accounts; paper trades only; net P&L after round-trip commission

Updated: 2026-11-11 14:40:31 (Dubai UTC+4)

**Every bucket with n < 20 is LOW CONFIDENCE. Descriptive only; no proven edge.**

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| All closed trades | 6 | 16.7% | $-1,231.60 | $757.25 | $-397.77 | $-205.27 | LOW CONFIDENCE |

## Session

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION | 6 | 16.7% | $-1,231.60 | $757.25 | $-397.77 | $-205.27 | LOW CONFIDENCE |

## Value Area Position

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| below_val | 5 | 20.0% | $-493.00 | $757.25 | $-312.56 | $-98.60 | LOW CONFIDENCE |
| inside | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |

## Cvd Agrees

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |
| yes | 5 | 20.0% | $-493.00 | $757.25 | $-312.56 | $-98.60 | LOW CONFIDENCE |

## Rvol Spike

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| no | 4 | 0.0% | $-1,503.35 | n/a | $-375.84 | $-375.84 | LOW CONFIDENCE |
| yes | 2 | 50.0% | $271.75 | $757.25 | $-485.50 | $135.88 | LOW CONFIDENCE |

## Vwap Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 6 | 16.7% | $-1,231.60 | $757.25 | $-397.77 | $-205.27 | LOW CONFIDENCE |

## Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 6 | 16.7% | $-1,231.60 | $757.25 | $-397.77 | $-205.27 | LOW CONFIDENCE |

## Vwap Ema50 Aligned

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| yes | 6 | 16.7% | $-1,231.60 | $757.25 | $-397.77 | $-205.27 | LOW CONFIDENCE |

## Confidence Band

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| 0.6-0.7 | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |
| 0.7-0.8 | 1 | 0.0% | $-423.00 | n/a | $-423.00 | $-423.00 | LOW CONFIDENCE |
| >=0.8 | 4 | 25.0% | $-70.00 | $757.25 | $-275.75 | $-17.50 | LOW CONFIDENCE |

## Symbol

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 3 | 33.3% | $415.50 | $757.25 | $-170.88 | $138.50 | LOW CONFIDENCE |
| GBPUSD | 2 | 0.0% | $-908.50 | n/a | $-454.25 | $-454.25 | LOW CONFIDENCE |
| XAUUSD | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |

## Exit Reason

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| ema50_cross_15m | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |
| stop | 3 | 0.0% | $-1,057.50 | n/a | $-352.50 | $-352.50 | LOW CONFIDENCE |
| target | 1 | 100.0% | $757.25 | $757.25 | n/a | $757.25 | LOW CONFIDENCE |
| time_cut_no_mfe | 1 | 0.0% | $-192.75 | n/a | $-192.75 | $-192.75 | LOW CONFIDENCE |

## Top combinations: Session × Value area × CVD agreement

Ranked by net P&L; small samples are not a strategy recommendation.

| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |
|---|---:|---:|---:|---:|---:|---:|---|
| LONDON_SESSION × below_val × CVD yes | 5 | 20.0% | $-493.00 | $757.25 | $-312.56 | $-98.60 | LOW CONFIDENCE |
| LONDON_SESSION × inside × CVD no | 1 | 0.0% | $-738.60 | n/a | $-738.60 | $-738.60 | LOW CONFIDENCE |

## Entry skips (161)

| Reason | Count |
|---|---:|
| already_position_or_pending | 104 |
| chase | 6 |
| entry 1.15 atr from market (max 1.0) | 2 |
| sell requires take_profit < entry < stop_loss | 1 |
| session_blocked | 2 |
| sizing_unreachable | 25 |
| stop 0.24 atr from entry (min 0.25) | 19 |
| stop 0.25 atr from entry (min 0.25) | 2 |

Legacy records without telemetry remain in the totals and are grouped as unknown.
MFE/MAE are gross price excursions, not hypothetical net realized P&L. Bar-data exit-bar extremes are ambiguous.
