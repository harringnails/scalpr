# Entry Incubation Study — Return (`standard-entry-incubation-study-v0`)

*Replay-only research. Changes NO live Guard behavior, thresholds, order paths,
or Standard mode. Purpose: does the current ratchet (a) exit strong trades during
normal early volatility, (b) let weak trades fall too far, or (c) both under
different conditions? Engine: `entry_incubation_study.py`; tests:
`test_entry_incubation.py` (all pass).*

---

## 1. Data coverage — the gating finding

**The retrospective replay CANNOT be run on existing logs.** A faithful replay
needs each trade's OPTION executable-price path from entry through a recovery
window. That data does not exist:

- `scalp_journal.csv`: 168 trades, 61 distinct option contracts. Stores only
  endpoints (entry, exit, peak %, realized %, reason). **No entry timestamp, no
  greeks, no intra-trade path.**
- `tick_log.csv`: 261,059 rows, **`SPY` underlying only** — zero option-contract
  quote history.
- Option contracts with usable tick history: **0**. Trades replayable now: **0 /
  168**.

So we cannot reconstruct what any traded option did minute-by-minute, nor what it
did *after* the guard exited. This is a data-capture gap, not a modeling choice.

## 2. Replay methodology

For a given trade the engine takes the option executable-bid path
`[{t_seconds_from_entry, bid}, …]` (extending PAST the current exit so recoveries
are visible) and, using the SAME frozen entry, quantity, path, executable-price
(bid − slippage) and costs for every variant, simulates each arming policy:

- Before the dynamic ratchet activates, only a **versioned hard max-loss stop**
  protects the position (retained THROUGHOUT the trade, not just during
  incubation) — so no policy ever leaves a loser uncapped.
- On activation the ratchet arms with its peak re-armed from the current return
  and a fresh grace window (matching the live re-engage behavior), then runs the
  existing laddered giveback rules (mirrors `scalp_server.Guard`, kept in
  lockstep; used for replay only).
- `CURRENT` has no separate hard stop — it faithfully models the live system
  (the ratchet is its only protection).

Per trade × variant it reports: activation timestamp/reason, exit
timestamp/reason, realized return + P&L, peak return, profit retained from peak,
MFE, MAE, time in trade, plus the two diagnostics below.

## 3. Variant definitions (A–H)

| | Variant | Activation of the dynamic ratchet |
|---|---|---|
| A | `CURRENT` | immediately (existing whole-position ratchet) |
| B | `TIME_DELAY_5M` | after 5 min; hard stop during incubation |
| C | `TIME_DELAY_10M` | after 10 min; hard stop during incubation |
| D | `PROFIT_BUFFER_5` | after executable return reaches +5% |
| E | `PROFIT_BUFFER_10` | after executable return reaches +10% |
| F | `TIME_OR_PROFIT` | at 5 min OR +10%, whichever first |
| G | `MOMENTUM_CONFIRMED` | when the option return is positive and rising (available evidence only; no fabricated indicators) |
| H | `INCUBATION_WITH_HARD_STOP` | 5-min incubation protected only by the versioned hard stop, then the current ratchet |

Hard stop: `HARD_STOP_PCT = 25%` (versioned `incubation-hard-stop-v0`).
Ladder / grace / confirm mirror the live guard.

## 4. Results

**No real results can be produced** (0/168 replayable — §1). The engine is
validated on synthetic paths (see §6) and is ready to run the moment option
paths are captured.

**What the endpoints CAN tell us (coarse, honest):** a trade that exited at the
initial 15% rung with peak < 15% *must* have exited at a loss (sell level = peak
− 15% < 0). Those are structurally-guaranteed losses:

> **74 of 168 trades (44%)** are `LOOSE_INITIAL_PROTECTION` candidates — exited
> at the initial rung, peak < 15%, mean realized **−11.7%**, 0% win.

We **cannot** say from endpoints whether any of those would have recovered
(that needs a price path). So this quantifies "weak trades falling too far"
(question b), but says nothing yet about "strong trades exited too early"
(question a) — precisely the half that needs the missing data.

## 5. The July 30 winner case studies

Requested answers — *and why each is currently unanswerable from the logs:*

- **741C** (entry 1.19 → exit 1.82, peak +56.7%, exited at the 2.5% rung) and
  **737C** (2.75 → 3.67, peak +38.0%, 2.5% rung).
- *Would CURRENT have exited during the disengaged 5–10 min window?* **Unknown.**
  We have no option price path for the first minutes, and no entry timestamp, so
  we cannot place the window or test it.
- *Max loss after entry before the move / recovery time / which variants would
  have preserved it?* **Unknown** for the same reason.

What we *can* say is mechanistic, not measured: both exited at the **tight 2.5%
rung near a high peak**, i.e. the ratchet was active at the END and captured the
top. You disengaged at the START. The 74-trade loose-protection pattern shows the
initial rung frequently stops trades out at a loss on early weakness — so it is
*plausible* CURRENT would have stopped these two out early had it stayed engaged.
But that is a hypothesis about two hand-picked winners, not evidence.

## 6. Harm-vs-benefit tradeoff (demonstrated on synthetic paths)

The engine makes the two-sided nature explicit:

- **Benefit (winners):** on an early-dip-then-recovery path, `CURRENT` stops out
  at a loss while `TIME_DELAY_5M` / `TIME_OR_PROFIT` sit through the noise and
  capture the move — `PREMATURE_EXIT` fires with a large missed-opportunity gap.
- **Harm (losers):** on a monotonic decline, the delayed / profit-buffer variants
  **enlarge** the loss versus `CURRENT` (the ratchet arms late or never), *but*
  the retained hard stop **caps** the downside near −25%. A profit-buffer variant
  on a never-profitable trade never arms and rides to the hard stop.

So the honest expectation is **both (c)**: incubation helps the trades the
initial rung exits too early, and hurts the trades that simply fade — with the
hard stop bounding the damage. Your candidate (`TIME_OR_PROFIT` + hard stop) is
the design that most directly balances the two, which is why it's the sensible
first thing to *measure*.

## 7. Data limitations

- No option-contract tick history anywhere (tick log is underlying-only).
- No entry timestamps or greeks in the journal → cannot even locate the
  incubation window or translate underlying moves to option moves.
- The journal ends at exit → no post-exit continuation, so `PREMATURE_EXIT`
  (recovery-after-exit) is unobservable retrospectively by construction.
- Approximating option paths from the SPY underlying via a delta-gamma proxy was
  considered and rejected: without entry time or entry greeks it would be
  guesswork, and 0DTE theta/gamma make such a proxy unreliable exactly where
  these trades live.

## 8. Recommendation — a frozen shadow cohort

Do **not** change the live Guard on two winners. Instead capture the missing data
prospectively with a shadow-only mode, then run this engine on real paths:

**`ENTRY_INCUBATION_SHADOW` (proposed, not built):**
- For each *real* Standard-mode entry, record the option's executable-bid path
  from entry through a fixed recovery window (e.g. 30–60 min past exit), plus the
  entry timestamp and greeks — the exact inputs this engine needs.
- It **observes only**: it never changes the live Guard, never places orders, and
  during any modeled incubation it preserves a **hard maximum-loss stop and
  emergency exits** (never fully unprotected).
- Freeze it by config hash, like Cohort A. Accumulate a pre-registered sample
  (e.g. ≥ 30 trades over ≥ 10 sessions) before interpreting.
- Then `entry_incubation_study` produces the full A–H comparison, the two
  diagnostics, the requested aggregates (calls/puts, time of day, premium, spread
  bucket, peak bucket, exit rung, win/loss), and the variant ranking (net P&L,
  mean/median return, win rate, profit factor, max drawdown, tail loss,
  opportunity recovered, additional loss created).
- Only if that frozen cohort shows a robust, favorable harm/benefit balance would
  a live Guard change even be proposed — and that would be a separate, explicit
  approval.

*Live Guard, thresholds, order paths, and Standard mode are unchanged. This
document is analysis + tested machinery + a data-capture recommendation only.*
