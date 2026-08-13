# Scalpr Trade — Paper-Trading Pilot Protocol

**Version: `pilot-protocol-v2` — FROZEN / pre-registered.**

This protocol is fixed *before* any Phase-1 data is collected, so results mean
something. The refinements from external review were incorporated **before the
first session existed** (0/20 regime, 0/20 shadow, SIP not subscribed); that is
legitimate pre-registration, not fitting to data. Once collection begins, do not
move a gate, threshold, or definition to fit an outcome. Only a genuine
correctness defect justifies a change, and any methodology change creates a new
version with a fresh evaluation.

> **Change log — v1 → v2 (made before any Phase-3 evidence existed):** the
> no-0DTE prohibition was **removed**. Rationale: 0DTE options are central to the
> operator's intended options strategy, so excluding them would make the pilot
> evaluate a different strategy than the one actually traded. Because this was
> changed before any Phase-3 trade was recorded, there is no cohort to restart and
> no contamination. 0DTE carries elevated intraday gamma, theta, spread-widening,
> and pin risk (see the 0DTE note in Phase 3), so the associated liquidity and
> guard-persistence controls are *especially* load-bearing for these trades.

> **Scope & disclaimer.** This is a validation and risk-management framework, not
> a profitability plan and not financial advice. **Passing every gate below does
> not imply the strategy is profitable or safe — it only establishes that a
> specified evidence bar was met.** Automated and frequent trading can produce
> substantial losses and costs and may be unsuitable depending on your resources
> and risk tolerance (per FINRA guidance). Any eventual real-money use must be
> money you can lose without affecting savings, debt payments, retirement, or
> household obligations. The author of this document is not a financial advisor.

Over-arching rule (unchanged): **market context ≠ entry signal ≠ execution
system.** Each phase proves one of those before the next is allowed.

Instrument scope: **the first formal pilot is SPY shares only.** Every instrument
class (shares, options) requires its **own** frozen entry policy and its **own**
evaluation cohort. Successful SPY-share results do **not** authorize SPY options;
options results do not pool across expirations or structures without a
pre-registered rule.

---

## Definitions (frozen)

**Clean regime session** (`regime-research-v3`) — all of:
- timing-quality gate passed;
- expected session bar-coverage met;
- no unresolved data outage;
- correct research version;
- immutable snapshot successfully written;
- no manual modification of source data;
- eligible under the pre-registered daily gate.

**Eligible premarket shadow session** — assessment frozen pre-cutoff (09:25 ET),
outcomes complete (≥90% first-hour bars + 60m and close present), not a revision:
i.e. `eligible_for_primary_evaluation == true`.

**Complete feed-quality session** — a `live_parallel_stream` run with the quote
gate passed, `same_feed_suspected == false`, and separate regular-session and
premarket passes.

**Unique candidate** (Phase 3 denominator) — **one candidate per (symbol,
direction, setup episode).** Repeated alerts for the same episode are *updates to
the same candidate* until the setup is invalidated or a fixed cooldown expires.
Rejected candidates remain in the denominator. Long and short are not pooled;
symbols are not pooled. This prevents one market move from inflating the count via
repeated alerts.

---

## Phase 1 — Operational qualification

*Reliability only. No profitability judgment is made or evaluated.*

Complete **all** of:

- [ ] ≥ **20 clean sessions** for `regime-research-v3` (per definition above)
- [ ] ≥ **20 eligible** premarket shadow sessions
- [ ] ≥ **5 complete live** SIP-vs-IEX feed-quality sessions
- [ ] **1 deliberate feed disconnect + recovery** test (behavior documented)
- [ ] **1 server restart while holding a paper position** — see note
- [ ] **Replay determinism** verified (same raw data → identical bars + signals via
      `input_hash`)

**Restart-test note (important):** the current guard state is in-memory, so this
test is *expected to demonstrate the known failure*. It **passes if the actual
behavior is documented accurately and the failure mode is confirmed** — it does
**not** certify restart safety. Restart safety remains a pre-live blocker until
state persistence + startup reconciliation are implemented and retested. "Box
checked" here means "behavior known and recorded," not "restart is safe."

**Phase-1 exit artifact (immutable, links to underlying session records):**

```json
{
  "protocol_version": "pilot-protocol-v1",
  "phase": 1,
  "completed_at": "...",
  "regime_clean_sessions": 20,
  "shadow_eligible_sessions": 20,
  "feed_quality_live_sessions": 5,
  "disconnect_test_passed": true,
  "restart_test_completed": true,
  "restart_safety_certified": false,
  "replay_determinism_passed": true,
  "exceptions": [],
  "phase_exit_status": "passed"
}
```

## Phase 2 — Exploratory Level-1 alerts (alerts only, NO orders)

Only after the SIP feed passes Phase 1. Alerts *propose*; you decide.

```
LONG CANDIDATE — NO ORDER
Background:     Favorable
Regime:         Trending up
Entry trigger:  Confirmed break and hold above opening range
VWAP:           Rising
Risk geometry:  Acceptable (reward:risk stated)
Feed quality:   Passed for Level-1 research
```

- You decide whether to take the paper trade.
- **Record every alert, including rejected ones** (rejections and misses are data).
- **Phase 2 is exploratory.** It is used to catch behavioral/logical flaws and to
  design `entry-policy-v1`. Because the policy is chosen after observing Phase 2,
  **Phase 2 results are contaminated by selection and cannot count toward Phase 3
  performance** (see Phase 3).

## Phase 3 — Frozen forward paper policy (`entry-policy-v1`)

No live capital, no discretionary rule changes once frozen. **Only NEW forward
observations recorded after the policy is frozen count** — no Phase-2 data may be
counted toward Phase 3 performance.

**Sample requirements:**

- [ ] ≥ **100 unique paper candidates** (per the uniqueness definition)
- [ ] preferably **100–200 completed paper trades**
- [ ] **realistic bid/ask fills** (not midpoint)
- [ ] **commissions, routing charges, and regulatory (SEC/FINRA) fees** included
- [ ] **0DTE permitted** (v2) — but only under 0DTE-appropriate liquidity gates
      (see note); 0DTE and non-0DTE results should be *tagged and reported
      separately* so a 0DTE edge (or its absence) is visible on its own
- [ ] results spanning **trend, chop, high-volatility, and event-driven** sessions

**Pre-registered numeric performance gates** (set before Phase 3 begins; the
maximum-drawdown cap MUST be filled in before the first Phase-3 trade):

| Gate | Threshold |
|---|---|
| Net expectancy after costs | > 0 (point estimate) |
| Bootstrap: P(expectancy > 0) | ≥ 0.90 of resamples |
| Maximum drawdown cap | `<set before Phase 3>` — required |
| Single best day's contribution | ≤ 25% of total net profit |
| Profit concentration (top 5 trades) | < 50% of net profit |
| Slippage stress | expectancy stays > 0 at base +1 tick AND base +2 ticks |
| Minimum regime coverage | ≥ 15 completed trades in each required regime |

**Required statistics (not point estimate alone):** bootstrap confidence interval
for mean net return per trade; median trade result; profit factor; drawdown
distribution; win/loss payoff ratio; sample count by regime; % of bootstrap
resamples with expectancy > 0. A Phase-3 pass requires positive expectancy **and**
evidence it is not driven by a few trades.

**Core measure (a high win rate alone is not enough):**

```
Expectancy = P(win) × average win − P(loss) × average loss − costs
```

**0DTE note (v2):** 0DTE options are permitted but carry elevated intraday gamma
(the delta moves fast), theta (value decays through the day), spread-widening near
the close, and pin risk around the strike. The in-memory guard (Overview #1) and
the missing broker-side protection (#2) are *most* dangerous here — a crash or
sleep near expiry with no ratchet can move against you very quickly. 0DTE trades
must therefore run under the stale-quote / spread / liquidity gates, and their
results are tagged and reported separately from non-0DTE trades.

**Manual intervention during Phase 3:** allowed **only** for safety or operational
reasons. Such a trade stays in the record, is marked `intervention_contaminated`,
is **excluded from the primary policy-performance cohort**, and is included in
operational-failure reporting. Never delete an intervened trade.

## Phase 4 — Human-confirmed micro-live pilot

The first real-money step is human-confirmed orders, **not** autonomy. The system
proposes; you approve each order:

```
Proposed entry: SPY long
Order type:     Limit
Entry:          $___
Hard stop:      $___
Target:         $___
Maximum loss:   $___
Reason:         entry-policy-v1 conditions satisfied
[ Approve ]  [ Reject ]
```

Requires all **pre-live controls** (below) implemented and tested. Risk caps per
the real-money section.

## Phase 5 — Limited autonomous pilot

Only after Phase 4 live execution, risk behavior, and failure handling match
expectations. Broader capital requires multiple market regimes and a
substantially larger evidence set beyond this protocol.

---

## Control tiers

### Required before Phase 3 (automated paper entries) — paper-execution controls
- frozen entry policy (`entry-policy-v1`);
- deterministic order state machine **in paper mode**;
- stale-data and spread gates;
- maximum paper notional;
- daily paper-loss cap;
- emergency stop;
- complete candidate and fill logging.

### Required before ANY real money (Phase 4+) — pre-live controls
Maps to Known-limitations numbers in `SCALPR_OVERVIEW.md`:

| Control | Overview limitation |
|---|---|
| Persisted guard state + startup broker reconciliation | #1 |
| Broker-side protective orders (or equivalent) | #2 |
| Real order state machine reconciled to websocket (partial/cancel/reject/race) | #7, #12 |
| Hard paper/live separation (config, credentials, account-ID match, UI banner) | #15 |
| Live account-level exposure + daily-loss limits | #19 |
| Disaster-recovery procedure (upstream outage) | #13 |
| Manual liquidation path (broker app) | #13 |
| Tested production kill switch | (new) |
| Limit orders for options (no uncontrolled market orders) | #4 |
| Emergency exits override the 60-second grace period | #18 |
| Stale-quote / spread / liquidity / max-notional live gates | #4, #17 |

*Principle (not a regulatory claim):* the SEC market-access framework emphasizes
preventing erroneous orders before they reach the market and systematically
limiting exposure. Scalpr is not a broker-dealer system, but these are the correct
engineering principles for any automated order submission.

## Graduation gates

| Stage | Minimum requirement |
|---|---|
| Continue paper research | Now |
| Level-1 alerts (Phase 2) | SIP passes the 5-session live quality test |
| Automated paper entries (Phase 3) | Frozen entry policy + **paper-execution controls** |
| Human-confirmed live pilot (Phase 4) | 100–200 forward paper trades, positive net expectancy (with CI), **all pre-live controls** |
| Small autonomous live test (Phase 5) | Phase-4 live behavior matches expectations + all safety blockers fixed |
| Broader capital | Multiple market regimes + substantially larger evidence set |

## Real-money pilot — risk caps (MAXIMUMS, not targets)

- Per-trade risk **may not exceed 0.10%–0.25%** of the dedicated pilot account;
  the actual initial cap is set before the live cohort begins.
- Daily stop **may not exceed 0.50%.** A stopped day prohibits new entries for the
  remainder of the session. **No discretionary override.**
- Also enforced: a **weekly loss stop**; a **maximum consecutive-loss pause**; a
  **maximum trades per day**; a **maximum aggregate open risk**; and an
  **automatic block after any data-quality or reconciliation failure.**
- **Hard safety block:** any unresolved broker-position mismatch, stale-feed
  failure, or protective-order failure immediately blocks new entries, regardless
  of daily P&L.
- **No scaling** until enough live fills show real execution resembles the paper
  model. Capital must be genuinely loss-tolerant (see disclaimer).

These are conservative starting constraints, **not** guarantees of safety.

## Change control

- This protocol is **frozen** once Phase 1 begins. Gates/definitions are not
  adjusted to fit observed results.
- Correctness defects may be fixed; a fix that changes methodology bumps the
  version (`pilot-protocol-v2`) and restarts the affected evaluation.
- Interim looks (5/10 sessions; running Phase-3 counts) are operational monitoring
  only — never evidence for changing a threshold.
- Each phase produces an **immutable exit artifact** linking to its underlying
  session/trade records.

---

*Current status at freeze: Phase 1 not started (0/20 regime, 0/20 shadow, SIP not
yet subscribed). Nothing in Scalpr is approved for live execution. Passing any
gate establishes only that a specified evidence bar was met — never profitability
or safety.*
