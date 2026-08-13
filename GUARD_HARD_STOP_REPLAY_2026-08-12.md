# Guard Hard Stop Replay 2026-08-12

**Status:** paper-only replay characterization
**Scope:** `scalp_journal.csv` end-state replay approximation across the full 239-trade journal
**Important caveat:** the journal stores entry, exit, peak, and realized outcome, but not the full tick-by-tick intra-trade path. This is therefore a journal-end characterization, not a tick-perfect historical re-simulation.

## Pre-committed setting

Chosen as a risk decision, not by sweeping for best performance:

- `hard_stop_loss_pct = 25`
- `hard_stop_profit_activation_pct = 20`
- `hard_stop_peak_giveback_pct = 10`
- `hard_stop_loss_cap_confirmation_ticks = 2`
- `hard_stop_quote_degradation_seconds = 12`

## Result

### Full journal

- Trades evaluated: `239`
- Baseline mean realized return: `-2.167%`
- Replay mean post-stop return: `-0.401%`
- Estimated improvement: `+1.766 percentage points`
- Stops triggered: `26`
- Hard-cap stops: `15`
- Trail stops: `11`
- Winners clipped by the stop: `11` of `103` wins
- Winner-clipping rate: `10.7%`
- Estimated spurious-stop rate: `42.3%` of stopped trades

### 0DTE slice

- Trades evaluated: `226`
- Baseline mean realized return: `-1.793%`
- Replay mean post-stop return: `+0.075%`
- Estimated improvement: `+1.868 percentage points`
- Stops triggered: `26`
- Hard-cap stops: `15`
- Trail stops: `11`
- Winners clipped by the stop: `11` of `100` wins
- Winner-clipping rate: `11.0%`
- Estimated spurious-stop rate: `42.3%` of stopped trades

### Tail-loss effect

On the deepest-loss subset that would have hit the `-25%` floor:

- Mean realized loss: `-49.349%`
- Mean stop exit: `-25.000%`

That is the clearest economic benefit of the stop: it cuts the long tail materially while leaving the rest of the distribution mostly intact.

## Decision

**GO for paper-only wiring at this pre-committed level.**

Reason:

- The journal is tail-dominated enough that downside control has clear value even without assuming an edge.
- The stop meaningfully reduces mean loss and clamps the worst tail.
- The winner-clipping cost is real, but at this chosen setting it is smaller than the tail-risk reduction on the replayed journal.

## Next check

Verify in paper over a few sessions that:

- real losers are exited,
- quote noise does not cause whipsaw exits,
- and the trail does not clip obvious winners prematurely.

