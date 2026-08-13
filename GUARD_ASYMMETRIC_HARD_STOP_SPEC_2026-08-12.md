# Guard Asymmetric Hard Stop Spec 2026-08-12

**Status:** proposal only
**Scope:** paper / shadow Guard behavior only
**Goal:** cap catastrophic downside without cutting off winners too early

## Problem

The current Guard path has ratchet / giveback behavior, but it does **not** have a separate hard downside breaker.

That leaves the system vulnerable to exactly the kind of trade the journal shows most clearly:

- winners can run,
- but losers can keep bleeding,
- and a few large losses can dominate the sample.

## Proposed Behavior

Add an explicit asymmetric stop with two parts:

### 1. Initial loss cap

If a trade is underwater by more than a configured maximum adverse excursion before it has established meaningful profit, exit immediately.

Starting calibration point:

- `initial_hard_stop_pct = 20%` to `25%` loss from entry

This is a starting range for replay calibration, not a claim of optimality.

### 2. Profit-protecting trail

Once the trade has moved materially in the favorable direction, switch from a simple loss cap to a trailing giveback rule.

Starting calibration point:

- activate the trail after roughly `+15%` to `+20%` peak profit
- allow a limited giveback from peak, rather than all the way back to zero
- never let the trail become looser than the initial hard stop logic
- peak and giveback calculations use fresh, confirmed marks only

## Quote-Quality Gating

The dirty 0DTE feed cannot be treated as a single-tick truth source.

- A **single** stale, crossed, or one-sided quote tick must **not** trigger an exit by itself.
- A bad tick should be treated as unusable for stop evaluation, not as a stop signal.
- Exit on quote-quality grounds only when the system cannot obtain a trustworthy fresh two-sided mark for a sustained interval.

Starting calibration point:

- `quote_quality_degradation_seconds = 10` to `15`

If the system cannot obtain a fresh, two-sided mark for that long, then it should fail closed and exit because it can no longer protect the position reliably.

## Loss-Cap Confirmation

The loss cap itself should require a fresh mark and light confirmation.

Starting calibration point:

- require `2` consecutive fresh ticks below the configured loss cap before exiting

That keeps the hard stop independent of ordinary ratchet confirmation while avoiding stop-outs on one bad print.

## Non-Negotiables

- The hard stop must be independent of ordinary ratchet confirmation and grace logic.
- It must not be bypassed by pause/resume behavior.
- It must be visible in logs and journaled as a distinct exit reason.
- It must remain paper/shadow until separately reviewed.

## Implementation Shape

The spec should be implemented as a separate Guard path, not as a hidden tweak to the existing ratchet:

- `hard_stop_loss_pct`
- `profit_activation_pct`
- `peak_giveback_pct`
- `quote_quality_fail_closed = true`
- `quote_quality_degradation_seconds`
- `loss_cap_confirmation_ticks`

## Rationale

This change is not about making every trade better.
It is about making the distribution safer:

- fewer catastrophic tails,
- fewer spurious exits from transient quote jitter,
- better average realized outcome,
- less dependence on manual intervention,
- and a cleaner basis for evaluating whether the underlying signal has any edge at all.

## Replay Discipline

Calibration must be honest:

- Choose the stop level as a risk decision first.
- Do **not** sweep many candidate loss caps on the same journal to find the best-performing value.
- Use replay only to characterize the historical effect of the pre-committed stop.
- Report both sides of the tradeoff:
  - tail-loss reduction
  - winners clipped / spurious-stop rate

The goal is not to optimize the stop on the same sample that motivated it.
The goal is to make the downside distribution safer without hiding the cost.
