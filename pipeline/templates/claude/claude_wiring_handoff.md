# Blue Bird: wiring the Claude sims (handoff for the pipeline maintainer)

Decision: run two Claude paper sims side by side, identical except for the entry model.
- **Sim C-OPUS:** entries by `claude-opus-5-5`
- **Sim C-SONNET:** entries by `claude-sonnet-5-5`
- **Both:** exits by `claude-haiku-5-5`, the same v2 prompts, the same server risk rules, and the same charts and data on the same cycle.

The Gemini sim keeps running unchanged as the control.

Prompts to use (separate files): `entry_prompt_v2.txt`, `exit_prompt_v2.txt`. Design rationale: `claude_answers_v2.md`.

---

## 1. Setup

- Add a repo secret **`ANTHROPIC_API_KEY`** (from console.anthropic.com) and pass it into the pipeline job env next to `GEMINI_API_KEY`.
- Add `anthropic` to `pipeline/requirements.txt`.
- Add a `[claude]` section to `config.toml`, overridable by env the same way as the Gemini models:

```toml
[claude]
entry_models = ["claude-opus-5-5", "claude-sonnet-5-5"]   # one sim per entry model
exit_model   = "claude-haiku-5-5"
entry_effort = "medium"
exit_effort  = "low"
timeout_seconds_entry = 60
timeout_seconds_exit  = 5
max_attempts = 2
```

## 2. API calls (exact settings)

**Entry (once per closed M15 bar, one batched call per entry model, same images and numbers for both):**

```python
resp = client.messages.create(
    model=entry_model,                      # "claude-opus-5-5" or "claude-sonnet-5-5"
    max_tokens=8000,                        # room for adaptive thinking + ~700 output tokens
    system=ENTRY_SYSTEM_PROMPT_V2,          # static text, never changes between calls
    messages=[{"role": "user", "content": blocks}],   # per symbol: text block, then image block
    thinking={"type": "adaptive"},          # Opus 5.5: always on, cannot be disabled
    output_config={
        "effort": "medium",                 # SAME effort for both models (fair comparison)
        "format": {"type": "json_schema", "schema": SIGNAL_BATCH_SCHEMA},
    },
)
```

- Both models run adaptive thinking at medium effort, so the comparison is model against model, not thinking against no thinking. (Sonnet 5.5's default effort is high; set it explicitly.)
- **Schema:** mark every field required, in this order per symbol: `symbol, trend_vs_vwap, volatility, level_event, rationale, setup_type, score, action, confidence, risk_pct, entry, stop_loss, take_profit, risk_reward`, then `market_summary`. `setup_type` is an enum `trend|fade|none`, `score` an integer. Required fields keep the analysis-before-decision order. For HOLD, the nullable fields need `["number","null"]` types.
- The API does not enforce numeric ranges: validate `confidence` and `risk_pct` in code, and clamp `stop_loss` to 0.6–1.5 ATR server-side.
- Gate entries on **`score >= 5` and `action != HOLD`**, not on confidence.

**Exit (every 18 s per open position):**

```python
resp = client.messages.create(
    model="claude-haiku-5-5",
    max_tokens=200,
    system=EXIT_SYSTEM_PROMPT_V2,           # static text
    messages=[{"role": "user", "content": [exit_user_text, chart_image]}],
    thinking={"type": "disabled"},          # Haiku 5.5 thinks by default; must be disabled explicitly
    output_config={
        "effort": "low",
        "format": {"type": "json_schema", "schema": EXIT_DECISION_SCHEMA},
    },
    timeout=5,
)
```

- Check `usage.output_tokens_details.thinking_tokens == 0` on the first responses.
- Log latency per call. If p95 goes over 5 s, downscale the exit image to 960×540.
- Each sim makes its own exit call for its own positions.

## 3. Server rules, binding in both Claude sims

These are not log-only like the current Gemini sim. Full reasoning is in `claude_answers_v2.md`, section C.

| Rule | Setting |
|---|---|
| Hard stop at fill | Entry model's `stop_loss`, clamped 0.6–1.5 ATR; skip the trade if 1 contract would risk more than $900 |
| Size | NQ 1, GC 1, 6E up to 4, 6B up to 4, all floor($900 / stop risk per contract); 6-contract account cap |
| Break-even floor | Best ≥ +0.5 ATR → stop to entry + round-trip commission |
| Profit lock | Best ≥ +1.0 ATR → floor at 50% of best; ≥ +1.5 ATR → 60% |
| Time stop | Never reached +0.25 ATR after 45 min → close |
| Daily | No new trade if day loss + open stop risk + new risk > $1,500; flatten at −$1,500 |
| Trailing loss limit | No new trade unless (room − open stop risk − new risk) ≥ $300 |
| Re-entry | Symbol locked until the next M15 close after any exit; after a loss, 30 min and no same-side entry until price retests the losing stop |
| Correlation | One new entry per cycle (highest score first); 6E and 6B on the same side share 4 contracts |
| Exit model failure | 3 failed/timed-out checks in a row → server rules only until calls recover |

## 4. Inputs the v2 prompts expect

The pipeline doesn't produce these yet. Lines marked "(if present)" in the prompts can ship without them.

- **Entry:** `setup_type`/`score` in the schema (required); `h1_trend` (H1 EMA50 slope: up/down/flat) and prior-day high/low (if present); `minutes_to_tier1_event` from an economic calendar (if present).
- **Exit:** `phase` (A/B/C from MFE), `mae_atr`, `floor_price` + `floor_label`, the entry's `setup_type` / short rationale / `stop_loss` / `take_profit`, the last 3 closed M5 closes and the forming M5 OHLC.
- **Exit chart:** an M5 render (last ~60 bars) with the M15 levels and the entry line. Until that exists, send the current M15 exit chart and change the prompt's "M5 candles" line to "M15 candles".
- **Leave out of the exit input:** dollar P&L, account balance, loss-limit room, giveback history, and the model's previous answers.

## 5. What to log and report (per sim, plus Gemini for reference)

- **Every trade in R** (result ÷ dollars at risk at the stop) and in dollars, by symbol, side, `setup_type` and session.
- **Every BUY/SELL signal, traded or not,** scored as a what-if with the server rules only (no exit model). This is the main comparison between Opus and Sonnet entries: it ignores which trades happened to get filled.
- **Two free control books on the same signals:** "mechanical" (server rules, no exit model) and "random twin" (coin-flip side at the same timestamps, same rules).
- **Calibration:** entry `confidence` buckets (0.5–0.6, 0.6–0.7, 0.7+) against the rate of hitting +0.5 ATR before the stop; exit `confidence` against what happened in the next 0.25 ATR.
- **Ops:** latency p50/p95 per call type, failed calls, and daily cost from the `usage` fields (input, output, thinking tokens).

## 6. When to decide

About **150 scored signals per entry model**, roughly 5–7 trading days. Before that, use the results to find bugs, not to pick a winner.

Opus wins only if it beats Sonnet on R per scored signal by a margin bigger than the day-to-day noise, *and* covers its extra cost. If neither beats the random twin, the entries have no edge, and no exit rule fixes that.

## 7. Expected API cost per weekday (estimates)

| Item | Per day |
|---|---|
| Opus 5.5 entries, 96 batches, thinking always on | ≈ $8 (thinking length is a guess; read the real figure from `usage` on day 1) |
| Sonnet 5.5 entries, 96 batches, adaptive at medium | ≈ $4 |
| Haiku 5.5 exits, ~2,400 calls per sim, thinking disabled | ≈ $0.70 per sim |
| **Both Claude sims** | **≈ $14** |
