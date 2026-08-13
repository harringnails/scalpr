# SCALPR 0DTE Audit Note

**Status:** read-only audit summary
**Scope:** current SCALPR 0DTE / options path, using local journal, decision, outcome, and freshness evidence

## Wins

- The platform is capturing and recording evidence end to end.
- The clock-skew freshness fix is correct and non-regressive.
- Freshness handling now separates artifact from real staleness.
- Manual paper exits and the ratchet path are functioning and leaving a usable journal trail.
- Duplicate reversal references are being suppressed as designed.

## Losses

- The 0DTE paper journal is still negative overall.
- Current 0DTE journal totals: `227` trades, `100` wins, `127` losses, median realized `-2.778%`.
- The non-0DTE slice is also negative in this sample: `12` trades, `3` wins, `9` losses, median realized `-11.1225%`.
- Today’s reversal decision layer produced `706` `NO_TRADE` decisions.
- The dominant blockers are still directional:
  - `momentum_slowing`
  - `causal_confirmation`
  - `multiple_nearby_levels`
- The current executable-bid outcome basis is not reliably labelable on the chosen strike.
- The selected outcome strike is too brittle: coverage remains structurally bimodal across strikes.

## What To Improve

### Behavior

- Reduce minute-by-minute re-evaluation of the same reversal reference when it adds no new information.
- Revisit the direction definition so the setup is less sparse but still causal.
- Make contract selection liquidity-aware first, not delta-first.
- Keep freshness strict, but stop expecting one thin strike to be labelable in every case.
- Treat the selection path as part of the experiment design, not just a lookup detail.

### Decisions

- Keep the current clock-skew fix.
- Do not loosen the `<= 5s` freshness threshold to rescue coverage.
- Do not reuse the current reversal cohort as a template for a looser variant.
- If the outcome basis cannot clear coverage reliably, switch to a pre-registered alternative basis rather than forcing the current one.

### Data

The platform still needs:

- Coverage by strike, DTE, and time-of-day across a wider SPY option grid.
- More sessions across calm, active, and event-driven regimes.
- Selected-strike distribution so liquidity bias is visible and controlled.
- A direct comparison of delta-targeted versus liquidity-first contract selection.
- More quote-density evidence on 0DTE and 1-2 DTE strikes.
- Better separation of real staleness from artifact in the quote feed.
- More evidence on whether the direction definition can be widened without destroying edge.

## What This Means

- The platform is operational.
- The 0DTE strategy path is not yet a validated edge.
- The main limiter is not order plumbing; it is the combination of sparse signal generation and unstable labelability.
- The next meaningful platform upgrade is better evidence quality, not more threshold fiddling.

## Recommended Next Step

Run the Stage A style coverage audit across a broader strike grid and time window, then decide whether the 0DTE study should:

1. keep executable-bid tracking with a liquidity-first selector, or
2. move to a different pre-registered outcome basis.

