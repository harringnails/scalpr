# Scalpr Decisions

## ADR-001: Authoritative source

The loose files in `Scalpr7/` are authoritative. The compressed directory and ZIP are historical references and are excluded from Git.

## ADR-002: Safety mode

V2 is paper/shadow only. Existing real-money code is not migrated into the initial V2 runtime.

## ADR-003: Initial market scope

Use SPY options with 0-2 DTE for the first V2 cohort. Expansion requires its own decision and evidence plan.

## ADR-004: Deployment

Build local-first while separating capture, analytics, and API boundaries so an always-on capture worker can be added later.

## ADR-005: Migration style

Use a side-by-side, behavior-preserving migration. Do not rewrite validated research logic and change its thresholds simultaneously.

## ADR-006: Data storage in Git

Source, tests, frozen configuration, and documentation are versioned. Credentials, journals, ticks, observations, generated state, logs, and historical exports remain local and ignored.

## ADR-007: Credentials during MFA incident

The existing key file remains in place solely so the running paper service is not disrupted. Development must not read, copy, stage, commit, or migrate it. Rotate credentials when Alpaca restores account access.

## ADR-008: Frozen contracts

Existing frozen versions and cohort parameters are preserved verbatim. Intentional changes require a new version, decision record, and fresh cohort.

## ADR-009: VOO CALL Wave shadow scope

At the user's direction on 2026-08-04, add VOO CALL contracts with 7-60 DTE to
Wave Riding simulations under `wave-shadow-scope-spy-voo-call-v1` and
`wave-riding-v0-voo-call-observation-cohort-b`. This is shadow-only: it cannot
submit broker orders, VOO puts remain blocked, Standard-mode trading remains SPY
options with 0-2 DTE, and Cohort A artifacts and counts remain isolated.

## ADR-010: QQQ Wave shadow study scope

At the user's direction on 2026-08-04, add QQQ CALL and PUT contracts with 7-60
DTE to Wave Riding simulations under
`wave-shadow-scope-spy-voo-call-qqq-v2` and
`wave-riding-v0-qqq-observation-cohort-c`. This expansion is shadow-only and
cannot submit broker orders. Standard trading remains SPY options with 0-2 DTE;
the frozen Cohort A and VOO CALL Cohort B remain isolated.

## ADR-011: Cached-Workup Wave shadow study scope

At the user's direction on 2026-08-04, replace the per-ticker Wave research
whitelist with `wave-shadow-cached-workup-scope-v3`. Any valid equity ticker may
be loaded when a local Workup cache provides an eligible 7-60 DTE contract;
SPY remains 0-2 DTE and VOO remains CALL-only. A simulation may start only from
an eligible contract in that ticker's latest cached Workup. New general-scope
simulations are tagged `wave-riding-v0-multi-underlying-observation-cohort-d`.
This remains shadow-only and cannot submit broker orders; Standard trading and
all prior frozen cohorts remain unchanged.

## ADR-012: Wave index capability gate

Status: superseded for SPX/SPXW only by ADR-016 after Alpaca added native index
values. It remains active for NDX/NDXP, RUT, and VIX.

The Wave observer requires synchronized Alpaca equity/ETF quotes and intraday
minute bars to calculate its frozen ATR. Cached Workup data alone is not enough
to simulate an index option. Under `wave-shadow-cached-equity-workup-scope-v4`,
SPX/SPXW, NDX/NDXP, RUT, and VIX are rejected before contract selection with a
plain-language capability message. They remain available in Workup for evidence
review. No equity/ETF proxy may be silently substituted because that would
invalidate synchronization and contaminate the research cohort.

## ADR-013: Paper-only Scalpr climb-add ladder

At the user's direction on 2026-08-04, add `scalpr-climb-add-paper-v0` as a
separate, default-off Standard-mode option. It may reuse Wave Riding v0's pure
eligibility gates and synchronized observation builder, but it does not modify
the frozen Wave study or use its shadow order adapter. Adds are restricted to
the existing SPY options 0-2 DTE scope, blocked in live mode, price-capped,
idempotent, throttled, and reconciled against Alpaca before Guard quantity and
weighted entry are rebased. The whole-position ratchet remains engaged after an
add. Activation requires both a per-trade opt-in and
`SCALPR_CLIMB_ADDS_ENABLED=1`.

## ADR-014: Guard loop isolation and action attribution

Following the 2026-08-05 filled-but-unguarded and delayed-exit incidents, the
Guard loop is execution-only. Entry analytics, Wave observations, incubation
observations, and climb-add evaluation run outside it. A Guard is installed
immediately after a broker-confirmed fill, before optional entry enrichment.
Incubation processing is bounded to one active record per research cycle and
stale active-index pointers are retired without rewriting historical paths.
Duplicate new-trade requests are rejected and audited; adding to a protected
position requires the explicit paper-only Add contracts action. Execution events
carry a request ID and initiator so dashboard actions and automatic ratchet exits
are distinguishable. Restart scripts fail closed when holdings are nonzero or
cannot be verified.

## ADR-015: Explicit automatic-exit authorization

Following the 2026-08-05 disputed SPY exit, Scalpr treats automatic selling as
an explicit per-action capability. Every new Standard trade must acknowledge
that the whole-position ratchet may sell 100% without another prompt. A paused
Guard cannot be re-engaged by a bare or stale request: the dashboard requires
the exact contract symbol to be typed, and the backend independently requires
that symbol plus an automatic-sell acknowledgement and request ID. Confirmed
resume actions are audited separately from Guard-triggered sells. These controls
do not change the frozen ladder thresholds or historical journal outcome.

## ADR-016: Native SPX Wave shadow cohort

Alpaca added native index-value endpoints on 2026-06-03, superseding ADR-012's
SPX capability assumption. Wave scope v5 admits SPX and the SPXW option root to
`wave-riding-v0-spx-index-observation-cohort-e`. The underlying must come from
Alpaca's native SPX value series; SPY is never substituted. Timestamped index
points are cached incrementally and aggregated into regular-session minute bars
for the existing 5-minute ATR. Because index values have no exchange volume,
alignment uses an explicitly labeled sampled-index TWAP, not a fabricated VWAP.
SPX option execution values still come from Alpaca option bids. The feature is
shadow-only, requires a cached SPX Workup contract, and fails closed on missing
entitlement, stale/unsynchronized values, ATR warm-up, or unusable option quotes.
NDX/NDXP, RUT, and VIX remain blocked. Existing Wave cohorts and Standard-mode
scope are unchanged.

## ADR-017: Institutional-flow provider

Unusual Whales remains Scalpr V2's sole institutional options-flow provider.
Overlapping secondary flow providers are out of scope because they duplicate an
existing licensed integration without proven incremental value. Unusual Whales is
accessed through the provider-neutral `InstitutionalFlowProvider` contract, and
its events become versioned, deduplicated evidence rather than trade commands.
Missing or stale flow remains unknown, never neutral. IVolatility has a separate
role for historical and current options-state analytics. This decision does not
change frozen `flow-evidence-v0`, Wave, incubation, Guard, or execution behavior.

## ADR-018: Bounded provider capture and Cloud SQL evidence mirror

Validated Unusual Whales flow is captured once per minute for SPY during the
regular-session window, and IVolatility captures one bounded SPY 0-2 DTE EOD
chain after the close. Both workers are read-only, run outside the Guard loop,
and have no broker imports or execution authority. Local append-only evidence
remains authoritative. A separate `.venv` worker mirrors versioned evidence to
Cloud SQL idempotently by content hash; database failure cannot interrupt the
server, Guard, or local capture. This operational wiring does not qualify an
entry policy or allow provider data to submit an order.

## ADR-019: Wide manual paper option builder with research isolation

At the operator's direction on 2026-08-06, restore the manual paper builder for
Alpaca-supported optionable US equities and ETFs with 0-60 calendar DTE. Every
manual contract must pass Alpaca underlying and exact-contract validation as
active and tradable before submission. The expanded path is options-only and is
physically blocked in live mode.

The existing `scalpr-spy-options-0-2dte-v1` validator remains byte-for-byte
unchanged and is still the default for automated direction, Entry Intelligence,
the forward bid collector, Wave, climb adds, and provider capture. Manual trades
outside that baseline are ratchet-managed without retuning and tagged
`scope_class=manual_out_of_envelope`; their behavior is explicitly unvalidated.
They are excluded from entry enrichment, incubation, qualified cohorts, journal
learning, and validated performance metrics while remaining visible in the
operator journal. This decision implements `SCOPE_EXPANSION_DIRECTIVE.md` and
does not alter any frozen cohort, hash, schema, or automated threshold.
