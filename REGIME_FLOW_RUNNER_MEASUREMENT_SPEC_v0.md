# Regime-Flow Runner — Paired-Counterfactual Measurement Spec v0

**Status:** pre-registered measurement spec. **Records only — no Guard/order/exit authority, changes nothing about the runner or the static ladder.** Paper/shadow. Turns the opt-in runner into an honest, self-contained A/B so it either proves it helps or gets ruled out.

## The question

When the regime-flow runner widens the profit trail, does it produce **better realized exits than the static ladder would have** on the *same* trade — measured, not assumed?

## The key insight (why no control arm is needed)

The runner only ever *widens* the trail and is floored at `+10%`, so a runner-managed position always holds **at least as long as** the static ladder would have. Therefore the static exit point always falls **within** the runner's realized mark path. That makes every runner-live position its **own paired observation**: from one realized path we can read the actual runner exit *and* compute exactly what the static ladder would have done. No separate control arm, no randomization, and — critically — **no counterfactual-future problem** (we never have to guess what a later-exiting policy would have done, because the runner is the later-exiting policy and it's the one we run live).

## What to capture (per runner-*eligible* position)

Record for **every** position where the runner is enabled — whether or not it ever activates:

- Full **confirmed-mark path** from entry to actual exit: timestamped fresh two-sided marks, running peak, giveback.
- **Activation events:** when the runner engaged/disengaged, and at each engagement the regime `state`/`status`/age, the flow `tier`/`direction`/age, and the `_source_signature`.
- The **actual (runner) exit:** price, profit %, timestamp, exit reason.
- Position id, direction, config/runner-policy versions.

Missing/insufficient marks → the paired comparison for that position is `UNAVAILABLE` (missing stays missing; §K), recorded with the reason, not silently dropped.

## At close — compute the static counterfactual

Deterministically replay the **static ladder** over the identical recorded mark path to get its exit (price, %, timestamp, reason). Then log the paired row:

- `runner_exit_pct`, `static_exit_pct`, and the **delta = runner_exit_pct − static_exit_pct** (the paired outcome).
- `runner_activated` (bool): if the runner never engaged (gates never passed — currently the common case), delta ≡ 0 and the row is tagged `NO_ACTIVATION`.
- The regime/flow context that gated activation.

## Pool

Separate, tagged, append-only pool (gitignored `regime_flow_runner_shadow_v0.jsonl`). **Never merged** with the paper journal, the discretionary pool, or the entry-intelligence episodes. Advisory/research only.

## Pre-registered analysis

- **Population:** all runner-eligible positions; the inferential set is the subset with `runner_activated = true` and an `AVAILABLE` paired outcome.
- **Primary statistic:** mean paired **delta** (runner − static) across activated positions.
- **Null:** paired sign-flip / label-shuffle null on the deltas, `p = (k+1)/(n+1)`.
- **Temporal robustness:** sign of the mean delta consistent across a chronological split.
- **Pre-registered floors (operator confirms):** `< 30` activated positions → `UNDERPOWERED` (report count only); `≥ 30` preliminary; `≥ 50` with matched-null `p ≤ 0.05` and split-consistent sign → verdict.
- **Verdict:** `RUNNER_HELPS` / `RUNNER_NEUTRAL_OR_HURTS` / `UNDERPOWERED`. No "the runner helps" claim until it clears — same bar as every other signal.

## Honesty notes

- The runner is **near-inert today** because regime is `UNAVAILABLE` almost all session (the warmup bug). So activations — and therefore this pool — will be sparse and `UNDERPOWERED` for a long time, and effectively won't accrue until the **regime v0.1** fix ships. Report the activation count explicitly every time; do not read `NO_ACTIVATION` rows as evidence of anything.
- The delta only measures giveback/profit-capture behavior; it says nothing about whether the regime or flow signals have predictive edge in general.

## Safety invariants (record-only; assert in tests)

- The measurement layer has **no** Guard/order/exit authority. It observes and logs; it never alters an exit.
- The **loss-side asymmetric hard stop remains independent and always wins** — the runner (profit-only, floored at `+10%`) and this wrapper never delay, loosen, or override the catastrophic-loss breaker. Assert this explicitly.
- Because the runner is profit-only, it is **orthogonal to the Phase-1 hard-stop (tail-loss) verification**; keep the two in separate pools and do not cross-read.

## Tests (run in `.venv`)

- Static-counterfactual replay on a synthetic path matches a hand-computed static exit; runner exit ≥ static exit in time on the same path.
- A position where the runner never activates → delta 0, tagged `NO_ACTIVATION`.
- Missing marks → paired outcome `UNAVAILABLE`, not dropped.
- `UNDERPOWERED` below the activation floor.
- Invariant test: the wrapper changes no exit and the loss-side hard stop fires identically with the wrapper present or absent.
