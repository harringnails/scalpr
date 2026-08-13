# Optional Multi-Contract Scale-Out and Runner Mode

**Status: FUTURE FEATURE — not built, not a current bug, not part of any current
implementation.** Scalpr today runs a single execution mode,
`WHOLE_POSITION_RATCHET`: the ladder rungs are profit-activation + allowed-giveback
tiers, and a valid exit sells 100% of the remaining position in one order. This
document preserves a proposed *optional* scale-out capability for possible future
work. It should not be treated as an active defect or included in the current
whole-position implementation.

> Origin note: an external report interpreted the current rungs as partial
> scale-out levels and described a "single-contract incompatibility." On review of
> the code, the current ladder is a tiered **whole-position** giveback mechanism
> (`Platform.sell` submits `qty=guard.qty`), so there is no partial-exit path and
> therefore no single-contract defect. The material below is a specification for a
> *new* feature, should scale-out ever be desired — not a fix.

## Concept

Add an optional mode that takes partial profit at staged targets and lets a
"runner" ride under a trailing/giveback rule. Because a position must be divisible
to scale out, the mode must be chosen from the affordable whole-contract quantity
**before** the trade is approved.

## Execution modes (proposed)

```
MULTI_CONTRACT_LADDER   # quantity >= minimum_contracts_for_ladder → staged exits
SINGLE_CONTRACT_MODE    # quantity == 1 (and single-contract mode enabled) → full-position rules
TRADE_REJECTED          # quantity below ladder minimum and single-contract mode disabled
```

Default selection:

```
if quantity >= minimum_contracts_for_ladder:   MULTI_CONTRACT_LADDER
elif quantity == 1 and single_contract_mode:    SINGLE_CONTRACT_MODE
else:                                           TRADE_REJECTED
```

The system must **never** auto-increase position size to make the ladder
executable. `minimum_contracts_for_ladder` is configurable (report suggested 3).

## SINGLE_CONTRACT_MODE (full-position rules)

Effectively the current `WHOLE_POSITION_RATCHET`: initial hard stop, ratchet
activation threshold, allowed giveback after activation, full-profit target, and a
time-based exit. Sell the entire single contract when any fires. All thresholds
configurable; none hard-coded. (This is why single-contract trades are already
safe today — the whole system currently behaves this way.)

## MULTI_CONTRACT_LADDER (the new capability)

Take partial profit at staged targets; manage the remainder under a trailing/
giveback rule. Illustrative 5-contract flow: sell 1 at target A, 1 at target B,
1 at target C, and manage the remaining 2 with a giveback rule.

## Hard requirements for any scale-out implementation

- Whole-contract arithmetic for **all** exit quantities.
- Never emit a fractional-contract sale.
- Never emit a zero-quantity exit.
- Cumulative scheduled exits may never exceed the open quantity.
- Recalculate remaining quantity after **every** fill; handle partial fills
  without corrupting the remaining ladder.
- Cancel all remaining ladder instructions once the position is fully closed.
- Record the selected execution mode **before** entry and show it in the
  pre-trade snapshot.
- Block the trade when the affordable quantity is incompatible with the selected
  strategy (rather than forcing a bigger budget).
- Backtests/replay must model whole-contract constraints — never simulate a
  fractional exit that couldn't happen live.
- Logs must identify each exit's cause: hard stop, ratchet giveback, profit
  target, time exit, event-risk rule, or manual.

## Proposed pre-trade panel copy

When only one contract is affordable and single-contract mode is enabled:

```
POSITION-SIZING NOTE
The configured budget supports one contract.
Staged exits require multiple contracts; partial profit-taking is unavailable.
Selected execution mode: SINGLE_CONTRACT_MODE
This trade uses full-position stop, ratchet, profit-target, and time-exit rules.
```

If single-contract mode is disabled and the strategy requires multiple contracts:

```
TRADE BLOCKED
The budget supports one contract, but the selected strategy requires multiple.
Reduce contract cost, choose a cheaper structure, increase affordable units
within risk limits, or select a compatible exit strategy.
```

## Acceptance criteria (for the future build)

1. Affordable whole-contract quantity determined before approval.
2. A one-contract position never generates a fractional or zero-quantity exit.
3. One-contract positions use explicit full-position rules.
4. Multi-contract positions use staged exits.
5. Selected execution mode appears in the pre-trade artifact.
6. All remaining exit orders cancel after the position closes.
7. Backtest and live execution use identical whole-contract rules.
8. Trade is blocked when the exit strategy is incompatible with the affordable qty.
9. Position size is never auto-increased to satisfy the ladder.
10. Logs identify each exit cause.

## Tests to add (for the future build)

1 contract: no partial-exit request; single-contract mode activates; a ratchet
breach closes exactly one contract; a full-profit target closes exactly one; no
extra exit fires after closure.
2 contracts: exit percentages resolve to valid whole quantities; the first exit
does not accidentally close both unless configured; remaining qty recomputed.
3+ contracts: standard ladder; scheduled exit qty never exceeds the position.
Budget boundary: a small premium change that drops affordability 2→1 switches
modes correctly.
Partial fill: a partially-filled exit recalculates the ladder on the actual
remaining quantity.
Backtest consistency: no fractional exits that couldn't occur live.

## Core principle

The exit strategy must be compatible with the number of whole contracts actually
purchasable. Position sizing determines the execution mode; the execution strategy
must never assume divisibility it doesn't have.
