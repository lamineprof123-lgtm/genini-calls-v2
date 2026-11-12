# Blue Bird: answers to the brief, version 2

Prompts: `entry_prompt_v2.txt`, `exit_prompt_v2.txt`, `exit_blind_prompt.txt`. This file replaces `claude_answers.md`.

## What changed from version 1

**Three things version 1 got wrong**

1. **The entry rules blocked every fade trade.** Version 1 required price on the trade side of VWAP and EMA50 for any entry. A short off a Call Wall or a VAH rejection is almost always *above* VWAP, so the rule contradicted the prompt's own mean-reversion guidance. Version 2 defines two setups, TREND and FADE, each with its own two must-haves.
2. **The 30-minute time stop leaned on evidence the brief said not to trust.** The "0 of 6" bucket came from the server-exit trades on 11 Nov. Version 2 keeps a time stop as a parameter to test (45 minutes), not a finding.
3. **The sizing formula ignored the contract limits.** With no micros and a 6-contract cap, the sizes are almost forced (section C). The formula made it look like there was a choice.

**Four additions**

1. **Test design.** As planned, the Claude-vs-Gemini comparison can't tell you which model is better (section A).
2. **An eval simulation** to pick risk per trade from numbers (section C).
3. **A bias fix for the exit model.** The logs show it stays 0.85 confident while a loss deepens. Version 2 attacks that directly, including a "blind" exit book where the model isn't told a position exists (section B).
4. **Exact API settings** for the current Claude models, checked against the docs (section D).

---

## A. Test design: make the comparison mean something

**The problem.** If Claude runs with new prompts and new risk rules while Gemini keeps the old ones, a difference in P&L could come from the model, the prompt, or the rules. You won't know which.

**Fix: same rules, same prompts, several books.** Every book below takes the same fills and the same server stops and floors. They differ in one thing only.

| Book | Entry by | Exit by | What it answers |
|---|---|---|---|
| G-old | Gemini, old prompts | Gemini, old exit prompt | Control: the system as it runs today |
| G-new | Gemini, v2 prompts | Gemini, v2 exit prompt | Do the new prompts and rules help? |
| C-new | Claude, v2 prompts | Claude, v2 exit prompt | Claude vs Gemini, like for like |
| Mechanical | same fills as C-new | server rules only, no exit model | **Does the exit model add anything?** |
| Blind | same fills as C-new | blind-direction prompt | Is the exit model biased by knowing its position? |
| Random twin | coin-flip side at the same timestamps | server rules only | Do the *entries* have any edge at all? |

The mechanical and random-twin books cost nothing: they're code on data already logged. If the exit model can't beat the mechanical book, you can drop about 2,400 calls a day. If entries can't beat the random twin, no exit rule will save them.

**Score in R, not dollars.** R = trade result ÷ the dollars risked at the stop. Section 4's dollar totals mix 24×6E, 10×6B and 2 NQ tickets and can't be compared. R can.

**Score every signal, not only the trades taken.** One position per symbol means most BUY/SELL calls are never traded. Run each logged signal through the mechanical exit rules as a "what if". That gives roughly 20–30 scored signals per model per day, against a handful of real trades.

**How long before deciding.** Telling a 55% win rate from 50% takes about 400 trades at even payoff; two days gives 40–60 scored signals. Treat two days as a bug hunt. Call a model or prompt "better" only after about 150 scored signals each, roughly 5–7 trading days.

---

## B. Prompts: what's different and why

### Entry (`entry_prompt_v2.txt`)

- **Two setups, each with two must-haves:** TREND (T1 structure, T2 closed-bar event) and FADE (F1 rejection at an extreme, F2 a magnet at least 0.6 ATR away and no acceptance). Five supporting points on top. A trade needs both must-haves and a score of 5 or more out of 7.
- **Two new schema fields, placed before `action`:** `setup_type` ("trend" | "fade" | "none") and `score` (integer). Reasons: the server can gate on the score instead of an uncalibrated confidence number, and results can be split by setup type, which is the fastest way to find where any edge is.
- **`stop_loss` and `take_profit` are real now.** The model names the price where the idea is wrong; the server places the stop there and sizes from it. This removes the old contradiction (prompt said "server overwrites stops", account had none).
- **`confidence` has a definition:** the probability price reaches +0.5 ATR before the stop. It can now be checked against outcomes. Today it clusters at 0.72 and carries no information.
- **A hard HOLD list at the top:** stale price, blocked session, news within 10 minutes, no fresh event on the last closed bar, already chased 0.5 ATR, or under 0.6 ATR of room. "Trend still intact" is explicitly not a new trade, which targets the 40-repeats-in-2-hours behaviour.
- **"Numbers are authoritative, the image is for shape."** Small axis labels are the easiest thing for a vision model to misread.
- **`rationale` capped at 60 words using point codes** (T1, S3…). Shorter output is faster and cheaper, and easy to parse.

### Exit (`exit_prompt_v2.txt`)

The HOLD-while-red problem comes from the framing more than the rules. The old prompt tells the model it is "the ONLY risk control" and shows dollar P&L, so every answer is made under pressure not to book a loss.

- **The model is told it is not the safety net.** The server holds the stop and the floors, so "hold to avoid a loss" and "close to be safe" are both ruled out by name.
- **The fresh-eyes test:** "forget the position exists; would you open this same side here now?" If not, close, red or green.
- **No dollars, no account numbers, no memory of its own previous answers** in the input. P&L is in ATR only. Dollars invite hoping, and prior answers invite consistency.
- **Phases A/B/C stay,** with the server computing the phase. The giveback arithmetic lives in code (section C).
- **M5 chart instead of M15.** At 18 seconds the M15 picture barely changes.
- **Exit `confidence` is defined too:** the probability of +0.25 ATR for the trade before 0.25 against, with CLOSE required below 0.45. That makes it testable; if it still sits at 0.85 on losers, stop logging it.
- **Static system prompt, dynamic user message.** The template is split so the instructions never change between calls.

### Blind exit (`exit_blind_prompt.txt`), shadow only

Same chart without the entry line and with no mention of a position. The model answers UP / DOWN / FLAT plus a probability, and the server maps that to HOLD/CLOSE. If this book beats the position-aware one, the exit model was being biased by knowing its side. It's one extra Haiku call per check, about $1 a day.

---

## C. Risk rules

### What the simulation says about risk per trade

20,000 simulated evals per cell. Rules: $6,000 target, $3,000 trailing loss limit treated as binding intraday, 4 trades a day, $15 costs per trade, day stops at −$1,200, 60-trading-day cutoff.

| Edge | $300 risk | $600 risk | $900 risk | $1,500 risk |
|---|---|---|---|---|
| None (50% wins at 1:1) | 5% | 17% | 24% | 22% |
| Small (55% wins at 1:1) | 37%, 34 days | 44%, 12 days | 45%, 6 days | 34%, 3 days |
| Good (60% wins at 1:1) | 84%, 27 days | 71%, 10 days | 67%, 5 days | 48%, 3 days |
| Small-target style (72% wins of +0.5R) | 16% | 52%, 22 days | 49%, 11 days | 42%, 6 days |

Cells show pass rate and median trading days to pass.

- **With no edge, size doesn't rescue it.** The best case is about 1 pass in 4, or roughly $800 or more in eval fees per pass at $190.40.
- **With an edge, $600–900 is the range.** Pass rates are about equal, and $900 passes in half the time. $1,500 is worse in every row that has an edge.
- **$300 is too slow.** Many runs hit the 60-day cutoff before reaching the target.
- **Recommendation: a $900 ceiling per trade.** It fits the many-accounts plan (same cost per pass as $600, twice the speed), and without micros it's the smallest budget at which NQ is tradable at all.

These are simplified coin-flip outcomes, not Blue Bird's real trade distribution. They show how size and edge interact, not what the bot will do.

### C1. Hard stop and size (set at fill, resting at the broker)

Stop = the entry model's `stop_loss`, clamped to 0.6–1.5 ATR. With no micros and the 6-contract cap, sizes are nearly fixed:

| Symbol | 1 ATR per contract (current ATR) | Size | Risk at a 1 ATR stop |
|---|---|---|---|
| NQ | ~$930 | **1** (skip if the stop costs more than $900) | ~$930 → needs a stop under ~0.97 ATR |
| GC | ~$540 | **1** | ~$540 |
| 6E | ~$67 | **4** | ~$270 |
| 6B | ~$40 | **4** | ~$160 |

- **Never 2 NQ or 2 GC on this account.** Every large loss in the logs was 2 NQ, 2–3 GC, or 1 NQ with no stop.
- **FX capped at 4, not 6,** so an FX position doesn't use the whole 6-contract allowance and block NQ and GC.
- **The eval will be decided by NQ and GC trades.** One NQ loss equals three or four 6E losses. Watch R per symbol.

### C2. Profit floors in code (replace the model-computed 50/40/30 tiers)

- Best profit ≥ +0.5 ATR → stop moves to entry plus round-trip commission.
- Best profit ≥ +1.0 ATR → floor at 50% of the best; ≥ +1.5 ATR → 60%.
- These thresholds are guesses until tested. Run two or three variants as extra mechanical books (for example break-even at +0.5 vs +0.75 ATR) and keep the one with the best R.

### C3. Time stop

Phase A for 45 minutes → close. It's a parameter, tested the same way.

### C4. Account guards

- **Daily:** no new trade if today's loss + open stop risk + the new trade's risk would exceed **$1,500**. Flatten everything at −$1,500 on the day. That keeps the worst case inside the $1,800 daily limit with $300 for slippage.
- **Trailing loss limit:** no new trade unless (room to the limit − open stop risk − new risk) ≥ $300. Stops alone can then never breach it.
- Make both **binding in the paper sim now.** A sim that can't fail teaches nothing about an eval.

### C5. Re-entry (the 14:17 / 14:21 US100 case)

- After any exit, the symbol is locked until the next M15 close **and** needs a fresh closed-bar event (the entry prompt requires it too).
- After a loss: locked 30 minutes, and no same-side re-entry until price has retested the losing trade's stop level.

### C6. Correlation

- One new entry per cycle, highest score first.
- 6E and 6B on the same side count as one position: 4 contracts between them.

### C7. When the exit model fails

- If the exit call errors or times out 3 checks in a row (about a minute), the position runs on server rules only until calls recover. Missing answers must never mean "hold forever".
- If the live price is stale for over 60 seconds, ignore model answers until it's fresh. The broker stop still works.

---

## D. Models and API settings

| Call | Model | Settings |
|---|---|---|
| Entry batch | `claude-sonnet-5-5` | Structured output with the JSON schema; adaptive thinking **on** as a test variant (see below) |
| Exit, every 18 s | `claude-haiku-5-5` | Structured output; **no thinking**, low effort |
| Blind exit (shadow) | `claude-haiku-5-5` | Same as exit |

- **Use structured outputs** (`output_config.format` with `type: "json_schema"`) on every call. The API then guarantees valid JSON in the schema. Mark all fields required so the analysis-before-decision order holds. Number ranges aren't enforced by the API, so validate 0–1 in code.
- **Thinking works differently on these models.** There are no manual thinking budgets; it's adaptive thinking plus an `effort` setting. Haiku 5.5 thinks by default, so exits must send `thinking: {"type": "disabled"}` with effort low; confirm `usage.output_tokens_details.thinking_tokens` is 0 in the first responses. Opus 5.5's thinking is always on and can't be disabled. (Correction to an earlier draft; `claude_wiring_handoff.md` has the exact settings.)
- **Thinking on entry is now affordable.** With entries on the M15 close, a 20–30 second answer is fine. Whether thinking improves the calls is unknown, so run it as a shadow variant and compare scored signals.
- **Prompt caching doesn't matter at these prices.** Don't design around it. The static/dynamic split is for clean, repeatable prompts.
- **Entries on the M15 close only** (96 batches a day), not every 2 minutes. The bar being judged only changes every 15 minutes.

**Estimated weekday cost** (1280×720 chart = 1,196 image tokens; entry output ~130 tokens per symbol; exit output ~45 tokens):

| Item | Per day |
|---|---|
| Entry, Sonnet 5.5, 96 batches | ≈ $2.70 (≈ $4.20 with thinking) |
| Entry at the current 2-minute cadence, ~350 batches | ≈ $10 |
| Exit, Haiku 5.5, 1–2 positions × 8 h (~2,400 calls) | ≈ $0.70 |
| Exit, busy case, 2–3 positions (~4,000 calls) | ≈ $1.10 |
| Blind exit shadow | about the same as exit again |
| **Recommended total** (M15 entries + exits + blind shadow) | **≈ $4–7** |

**Optional: test the bigger models with data.** At 96 batches a day, `claude-opus-5-5` costs about $5.50 and `claude-fable-5-2` about $11 to run as extra shadow entry models on the same charts. Nothing here shows a larger model reads these charts better; a week of scored signals would. Check exit latency (p95) in the first hour; if it runs over 5 seconds, shrink the exit image to 960×540.

---

## E. Chart and inputs

**Add**
- **`minutes_to_tier1_event`** from an economic calendar (CPI, FOMC, NFP, rate decisions). Neither model knows the calendar, and a 2-contract position into a release is a coin flip at 3× the normal range. The logs can't show whether news caused any of the losses; this is cheap insurance.
- **`h1_trend`** (H1 EMA50 slope: up / down / flat) plus prior-day high and low. Longs went 3 for 12; this is the cheapest way to test whether they were fighting the higher timeframe.
- **Exit:** an M5 chart, `phase`, `mae_atr`, the last three M5 closes, the entry's own stop, target, setup type and short rationale, and the current server floor.

**Remove**
- **From the exit input:** dollar P&L, commission in dollars, the giveback history fields (the server handles giveback now), and anything about account limits.
- **From the entry rules:** gating on `confidence` ≥ 0.55. Gate on `score` ≥ 5 until confidence proves calibrated.

**Skip**
- Bid/ask (spot quotes aren't the futures book) and a clock (the time is already in the numbers).

**Before the real eval:** move the data to the CME contracts themselves (6E, 6B, NQ, GC). Then stops, levels, volume and CVD all describe what's being traded.
