# SCALPR Audit 2026-08-12

**Status:** read-only audit summary
**Scope:** all outcome-bearing SCALPR streams currently available locally, including the 0DTE journal, entry-policy prototype, wave shadow outcomes, incubation status, and the reversal / entry-intelligence evidence base

## Executive Result

SCALPR is operational and capturing evidence, but the current 0DTE path is **not yet a validated edge**.

The strongest conclusion from the full record is that the platform has two separate problems:

1. **Loss control is too weak on the realized Guard path.**
2. **The 0DTE evidence base is too sparse and too brittle to support a clean edge claim.**

That means the best near-term improvement is not more signal looseness. It is better risk control, cleaner evidence separation, and a better outcome basis for labelable contracts.

## Wins

- The platform is capturing end-to-end evidence instead of operating blind.
- The clock-skew freshness fix is correct and non-regressive.
- Freshness now separates artifact from real staleness instead of collapsing them together.
- The Guard / journal / shadow plumbing is working well enough to produce reviewable records.
- Wave shadow exits are fully captured and deterministic in the local record.
- The entry-policy prototype is abstaining aggressively, which is a valid safety posture while the edge is unproven.

## Losses

### Realized 0DTE journal

- `239` total paper trades in `scalp_journal.csv`.
- Roughly `227` are 0DTE and `12` are non-0DTE.
- The 0DTE slice is net negative: `100` wins, `127` losses, mean realized return about `-1.87%`, median about `-2.78%`.
- The non-0DTE slice is also negative in this sample.
- The loss distribution is ugly enough that the main issue is not just low edge; it is **tail loss control**.
- The journal shows winners can run, but losers can bleed badly, which is exactly the condition an asymmetric stop should address.

### Guard behavior

- The current Guard path has a ratchet / giveback behavior, but **no separate catastrophic hard stop**.
- That leaves the system exposed to the worst kind of downside: trades that never recover and drift into very large losses.
- Manual exits are common, which means human intervention is currently compensating for missing structure.

### Entry-policy prototype

- The prototype is mostly abstaining:
  - `825` `NO_TRADE`
  - `530` `WAIT`
  - `15` `LONG_CANDIDATE`
  - `10` `SHORT_CANDIDATE`
- There are only `18` new-candidate rows in the whole set.
- The candidate sample is too small to treat this as evidence of a durable edge.
- The returns are roughly flat-to-noisy, not decisively positive.

### Reversal / entry-intelligence

- The current single-option executable-bid basis is not reliably labelable as specified.
- The labelability issue is structural, not a one-off data glitch.
- The direction layer is also sparse enough that the current reversal cohort is doubly constrained:
  - rare signals
  - unreliable strike coverage

### Incubation and wave

- Incubation is not yet producing completed evidence:
  - active list is empty
  - cohort report shows no completed simulations yet
- Wave shadow exits are operationally valid but not proof of alpha:
  - `47` simulated exits
  - all with the same exit reason
- That is useful operationally, but it is not a substitute for a true edge study.

## What To Improve

### 1. Add an asymmetric hard stop to Guard

This is the clearest immediate improvement to realized performance.

The system should stop relying on the ratchet alone and add a separate downside breaker that:

- exits quickly on meaningful underwater excursions,
- preserves upside once a trade starts working,
- cannot be bypassed by ordinary ratchet grace logic,
- and is fail-closed when quote quality or feed health is degraded.

This is the main change most likely to improve win quality and cut the worst losses without pretending the strategy is already an edge.

### 2. Separate evidence pools

Do not merge these into one performance story:

- manual Guard trades
- wave-riding shadow outcomes
- incubation evidence
- entry-policy prototype outputs
- reversal / entry-intelligence study records

Each has a different decision rule, risk model, and label basis.

### 3. Make contract selection liquidity-first

The 0DTE path should treat coverage as a first-class gate.

- Pick the most labelable contract first.
- Treat delta as a constrained target, not the primary optimizer.
- Record which strikes are being selected so liquidity bias is visible instead of hidden.

### 4. Use a different outcome basis if coverage remains weak

The single-option executable-bid basis is not strong enough to force into validity.

If broad strike coverage cannot reliably clear the freshness bar, the right answer is to adopt a different pre-registered outcome basis instead of loosening the definition of freshness.

### 5. Reduce decision sparsity only through a new frozen cohort

The direction-definition problem is real.

If the reversal study is salvaged, it should be:

- a new frozen cohort,
- pre-registered,
- prospectively tested on fresh data,
- and never a loosened reuse of the current cohort.

## Data The Platform Still Needs

- Quote coverage by strike, DTE, and time-of-day across a wider SPY grid.
- Selection rationale for every chosen contract.
- Realized fill quality, slippage, spread, and latency.
- Order-flow / depth data for entries and exits.
- Regime context so performance can be split by market condition.
- Clear separation of real staleness, clock artifact, crossed quotes, and missing feed states.
- More evidence on whether loosened direction rules actually improve edge or just increase fire rate.
- A comparable alternative outcome basis if executable-bid labelability remains unstable.

## Recommendation

If the goal is to improve wins and create a durable 0DTE edge, the ranking is:

1. **Add the asymmetric hard stop.**
2. **Tighten evidence separation and contract-selection discipline.**
3. **Solve labelability with liquidity-aware selection or a new outcome basis.**
4. **Only then revisit direction looseness in a new frozen cohort.**

The current state does **not** support a claim of validated predictive edge. It does support a stronger risk-control design and a better evidence architecture.

