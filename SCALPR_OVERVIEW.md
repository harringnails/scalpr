# Scalpr Trade — System Overview & Intended Use

*A summary for external review. The goal of sharing this is to have other reviewers
check whether anything material was missed in the design, the research discipline,
or the safety boundaries. Honest gaps and known-unbuilt items are listed explicitly
in "Known limitations" and "Open questions for reviewers" — please scrutinize those
hardest.*

Status as of this writing: **paper trading only.** Nothing in this system is
approved for autonomous live execution. Multiple layers exist specifically to
prevent that from happening prematurely.

---

## Executive summary

**Current capabilities**
- Manual paper trading with an automated local ratcheting guard (place/exit/
  liquidate paper positions; the server manages the ratchet).
- Observational regime research (4-state HMM, walk-forward, time-aware nulls).
- Observational premarket scorecard + a frozen forward (shadow) test.
- Historical/provisional cross-feed comparison tooling (SIP vs IEX).

**Not currently permitted / not built**
- Autonomous paper *entries* (entries are human-initiated today).
- Live-order preparation, live execution, autonomous position sizing.

**Current evidence status**
- Regime research (`regime-research-v3`): **0 / 20** eligible sessions (collection
  not yet started).
- Premarket shadow (`premarket-scorecard-v1`): **0 / 20** eligible sessions.
- SIP live-feed qualification: **not started** (SIP not yet subscribed).

**Critical blockers before any live use**
Guard persistence + startup reconciliation · broker-side protective orders ·
websocket-reconciled execution state machine · account-level risk controls ·
live feed qualification (with quote capture) · disaster-recovery protocol ·
hard paper/live environment separation.

### Claim vs evidence

| Claim | Current evidence | Required evidence | Status |
|---|---|---|---|
| HMM adds incremental predictive value | Synthetic tests only; forward sessions pending | Pre-registered forward cohort, then multiple regimes | Unproven |
| Premarket scorecard improves decisions | Shadow logger built, 0 sessions | 20+ eligible sessions, then broader regimes | Unproven |
| SIP is suitable for Level-1 research | Historical comparison tooling only | 5 live parallel sessions + quote gate | Pending |
| Ratchet protects positions | Paper/local testing | Persistence, broker protection, outage tests | Paper only |

---

## 1. What Scalpr is

Scalpr Trade is a local, single-user trading platform whose original core idea is
a **ratcheting profit ladder**: as a trade rises, the allowed giveback from its
peak tightens in steps, so gains get locked in progressively. Peaks only ratchet
up, never down. The ladder is enforced server-side and is advisory-market-read
proof (a market read can never loosen the ratchet).

Around that core, the project has grown a disciplined **research layer** whose
purpose is to answer, with evidence rather than assertion, three separate
questions:

1. Does a latent market-state model add predictive value beyond simple rules?
2. Does a premarket context scorecard improve directional or decision quality?
3. Is the market data even reliable enough to support entry-timing research?

The guiding principle throughout: **market context ≠ entry signal ≠ execution
system.** The system is deliberately built to prove whether its data, its market
interpretation, and its eventual entry logic actually deserve trust — before any
of them is allowed near real capital.

*Scope note:* essentially all current tuning and data collection is **SPY-specific.**
Thresholds are not assumed transferable; any other symbol (e.g. QQQ) would require
its own independently frozen policies and its own evidence cohort.

## 2. Environment

- Runs locally on a Mac, Python 3.14, in `/Users/.../Scalpr Trading/Scalpr7`.
- Backend: FastAPI + uvicorn (`scalp_server.py`), serving a single-page dashboard
  (`dashboard.html`) at `http://localhost:8420`.
- Broker/data: **Alpaca** paper trading. Market data currently the **free IEX
  feed** (a single exchange, ~2.5% of consolidated US volume). Alpaca **SIP**
  (full consolidated tape, ~$99/mo "Algo Trader Plus") is supported via a `--sip`
  flag but **not yet subscribed**.
- Start command: `caffeinate -i bash restart.sh` (frees port 8420 if needed,
  starts the server, keeps the Mac awake). Server is a foreground process; it does
  not auto-start.

## 3. Core trading engine (paper)

Implemented in `scalp_server.py` (`Guard` class + poll loop):

- **Ratcheting ladder** — per-trade rungs of the form "once peak reaches AT%,
  allow only TOL% giveback." Tolerance tightens as new peaks are set.
- **Grace period** (default 60s) — no *ordinary ratchet* exit in the first N
  seconds after entry. (See Known limitations #18: this must never gate an
  emergency risk exit.)
- **Exit confirmation** (default 2 consecutive reads) — a breach must persist to
  fire, to avoid single-tick noise exits.
- **Stall timer** — optionally banks a winner that stops making new highs for N
  seconds once above a minimum profit.
- Price poll loop at 2×/second (`POLL_SECONDS = 0.5`).
- **Guard mark:** stocks guarded off the **bid**; options currently guarded off
  the **mid** (see Known limitations #3 — flagged issue).
- Options contract builder (auto-defaults to at-the-money), holdings panel with
  one-click liquidation, performance panel, and a CSV trade journal
  (`scalp_journal.csv`).
- Guardrails: market-closed check, OCC-symbol validation for options.

This engine is **not** merely observational — it places and manages paper orders.
See the automation ladder (Section 8): the *research tracks* are observational
(Level 0A), but the trading engine is manual paper trading with an automated local
guard (Level 0B).

## 4. The eight-signal pre-trade checklist (`precheck.py`)

An **uncalibrated evidence checklist** (not a forecast, no probability) of 8
macro signals from ETF proxies: Trend, Momentum, Volatility, Fear gauge (VIXY),
Breadth (SPY vs RSP), Tech leadership (SMH vs QQQ), Rates (TLT), Credit (HYG).
Presented as supporting / opposing / caution / neutral counts. Abstains when too
few signals compute. This module also **feeds the journal's entry/exit signal
columns**, so it is intentionally left unchanged even as richer panels were added.

## 5. Journal + historical-lean scorer (`journal_model.py`)

The trade journal records entry/exit signals alongside P&L. `journal_model.py`
learns per-signal historical win rates and combines them (naive-Bayes-style
log-odds) into a directional lean — but **abstains below 30 labeled trades**,
because a "lean" from a handful of trades is confident-looking noise. Explicitly
uncalibrated; not walk-forward validated. Surfaced in the dashboard under the
pre-trade check.

## 6. Three independent research tracks

All three are **observational, versioned, and isolated from execution / sizing /
signal weighting.** None influences a trade.

### 6a. Regime model — `regime-research-v3` (`regime_model.py`, `regime_research.py`)
- A **4-state Gaussian HMM** (hand-rolled numpy, Baum-Welch EM in log space,
  multiple restarts) over 10-second bars built from a standing tick logger. States
  labeled post-hoc: trending_up, trending_down, low_vol_chop, high_vol_disorder.
- **Live inference uses filtered probabilities only** (no look-ahead).
- Per-fold **robust scaling** (median/MAD·1.4826, floor 1e-8, on the training
  window only — no leakage; parameters un-scaled back to data units after fit).
- Tiered data thresholds: MIN_FIT_BARS 300 / MIN_REPORTABLE_BARS 1800 /
  MIN_VALIDATION_BARS 5000, surfaced as a `status` field so "fit ran" never reads
  as "trustworthy."
- Quality gates: state occupancy (collapse/dominance), pairwise separation,
  expected durations, label confidence, per-bar posterior entropy.

**Model reproducibility specification** (for external review):

```
Observation covariance : diagonal
Variance floor         : 1e-10
Probability floor       : 1e-300  (numerical log(0) safeguard — see note below)
EM convergence tol      : 1e-4    (Δ log-likelihood)
Maximum EM iterations   : 100
Random restarts         : 5
Restart selection       : highest finite terminal log-likelihood
```

**Probability handling:** initial-state and transition probabilities are clipped
at `1e-300` before logarithms to prevent numerical log(0) failures. This is a
**numerical floor, not substantive transition regularization** — no transition
probability is materially constrained away from zero.

**Restart selection (candid limitation):** the selected restart is the fit with
the highest finite terminal log-likelihood. State-health diagnostics (occupancy,
separation, durations, label confidence, posterior entropy) are evaluated and
reported afterward, but **currently do not participate in restart selection**.
That is a legitimate future research question, not a claim of present strictness.

- **Walk-forward research report** (`/api/regime/research`): the target is the
  **cumulative forward log return** over each horizon (fixed a v2 bug that used
  only the isolated final bar); horizons {1,3,6,12,30} bars with **6 (≈1 min)
  pre-designated primary**; the rest diagnostics.
- **Null testing:** a **session-aware moving-block bootstrap** is the *controlling*
  null. Block length is estimated from the target's autocorrelation (first lag
  where ACF falls below 1/e, capped); blocks are resampled with within-session
  wraparound and **never cross market dates**; the target blocks are resampled
  against a **fixed** state sequence (a true no-association null, not paired
  resampling). Whole-series circular shift and IID shuffle are diagnostics only.
  Finite-sample p-value `(1+#≥obs)/(n+1)` so p is never a misleading 0.0.
  *Disclosed limitation:* the null preserves local target dependence and session
  boundaries but may **not** fully preserve time-of-day volatility structure (an
  opening-period block can be relocated into midday). A clock-time-stratified
  block null remains a possible robustness test; this does not block v3.
- **Baseline comparison:** the HMM is scored against a **pre-registered primary
  baseline** (`rolling_return_sign`, 6-bar), not the best-of-several (which is its
  own selection bias). Two gates kept separate: "beats the baseline" vs "has
  positive predictive value." (If multiple theoretical baselines are ever tested,
  Holm-Bonferroni or equivalent is required.)
- Immutable daily research snapshots (`/api/regime/snapshot`) with a frozen data
  cutoff, two hashes (raw manifest + analysis dataset), full gate sequence, and a
  multi-session aggregator (`/api/regime/snapshots`) evaluated against a
  **pre-registered 20-session preliminary research gate** (≥20 eligible sessions,
  positive-edge rate, baseline-win rate, time-aware-null pass rate, median edges,
  timing-failure rate). Aggregator refuses to blend research versions.
  **Passing this gate does not authorize entry-policy integration, paper
  automation, or live use** — it screens for gross failure over one likely-single
  regime; robustness needs materially more.

### 6b. Premarket scorecard — `premarket-scorecard-v1` / `premarket-policy-v1` (`premarket.py`)
A trade-readiness scorecard (`/api/premarket`) producing **six independent
assessments plus a data-confidence score**: Regime, Direction, Confirmation,
Entry quality, Tradability, Event risk.
- **Design rule that matters most:** a *degraded* or *unavailable* input **lowers
  data confidence and is excluded from directional aggregation — it is never
  counted as neutral.** Six missing indicators produce "low confidence," not a
  false "balanced."
- Every input tagged available / degraded / unavailable / stale; component status
  = worst-of-inputs.
- Components on real data: multi-horizon EMA trend structure; expanded leadership
  set (QQQ/SPY, IWM/SPY, RSP/SPY, SMH/QQQ, cyclicals vs defensives, HYG/LQD credit,
  SHY 2yr proxy); realized-vol regime; entry quality from **true ATR (OHLC)** +
  prior-day high/low/close in ATR units.
- Degraded on the thin IEX premarket feed: overnight gap/range, premarket VWAP,
  relative volume.
- Explicitly **unavailable** (disclosed, lowering confidence, not faked): true
  advance/decline & up-down volume breadth, % of S&P above MAs, VIX9D/VIX/VIX3M
  term structure, opening-auction imbalance, options OI/skew depth, ES futures,
  economic-event calendar.
- **Explicit, versioned decision policy** (`premarket-policy-v1`): the categorical
  rule that combines axes into a conclusion is published in the output (min data
  confidence, tradability-required-for-entry, event-risk veto, etc.), so the
  interpretation logic can't drift silently. `background` (market direction) is
  reported separately from the `readiness` conclusion, and the `blocking_condition`
  is named — so a favorable background that concludes WAIT (e.g. tradability
  unconfirmed) reads as "can't approve execution," not "bearish."
- Uncalibrated structured evidence summary — no probability, no weights until
  validation.

### 6c. Premarket shadow forward test (`premarket_shadow.py`)
Observational forward test of the scorecard:
- **Freezes the assessment before the open** (09:25 ET cutoff; assessments after
  the cutoff are recorded but flagged ineligible). DST-correct wall-clock via
  `zoneinfo`.
- Attaches **outcomes after the session** as a separate immutable file: cumulative
  log returns measured from the **session open — defined as the first valid
  regular-session minute-bar open under `price-convention-v1`, which is not
  necessarily the primary-exchange opening-auction print** — at 15/30/**60
  (primary)**/close, plus MFE/MAE, opening range, prior-day-high/low touch,
  first-hour realized vol, data completeness.
- **Three eligibility flags kept distinct:** `assessment_eligible`,
  `outcome_data_complete`, `eligible_for_primary_evaluation` (all three required).
  Incomplete afternoon data (<90% first-hour bars, or missing 60m/close) is
  excluded and counted separately.
- **Frozen price convention** (`price-convention-v1`) in the outcomes metadata.
- Read-only summary (`/api/premarket/shadow/summary`) reports **directional
  correctness AND decision-quality usefulness as separate things** — a WAIT is not
  "correct" just because price fell; it's useful if it avoided poor entries
  (wait vs non-wait MAE). Review milestones: 5 (operational) / 10 (descriptive) /
  20 (formal) eligible sessions. The 5- and 10-session looks are **interim
  operational monitoring, not evidence for changing thresholds.**

## 7. Data-timing infrastructure

### 7a. Audited bar builder (`bar_builder.py`, `bar-builder-v1`)
The event→bar path with an explicit timing contract, fully unit-tested
(`test_bar_builder.py`, 11 cases): every event carries provider **and** receipt
time (receipt never substitutes for event time); **half-open bars by provider
time** (a boundary tick belongs to exactly one bar); **finalization watermark +
grace** so late-but-in-window events are included; a **late event never mutates a
finalized bar** (logged separately, with a would-have-changed check);
targets start strictly after the feature bar; per-bar **input hash** gives
replay determinism; timing-quality report covers duplicate / late / out-of-order /
**negative-delay (clock-skew)** rates and p95/p99 arrival delay, with a pass/fail
gate. Regime endpoints **abstain when timing quality fails** (audited) and flag
legacy pre-provider-ts data as `unaudited`.

### 7b. Feed-quality validator — `feed-quality-v2` (`feed_quality.py`)
Infrastructure qualification for adding a real intraday feed (SIP) — **not a market
model**. Compares a new feed vs the reference over the same window
(`/api/feed/quality`):
- Feeds requested **explicitly** (never Alpaca's subscription-default), with a
  **same-feed trap** (identical volumes → `same_feed_suspected`, which **blocks
  eligibility for Level-1 research**).
- **Bar quality gate and quote quality gate are separate**; without live quote/trade
  capture the quote gate is `pending_live_capture` and overall status stays
  `provisional`.
- **ET-session segmentation** with a **separate premarket gate** (regular-session
  quality cannot compensate for thin premarket).
- Volume ratio is **descriptive only** (IEX ≈ 2.5% of tape; ratio varies), never a
  hard-coded requirement.
- **`validation_mode`**: a `historical_delayed_comparison` can check bar
  construction but **can never qualify a feed for live use**;
  `eligible_for_level1_research` is only true for a `live_parallel_stream` that also
  passed the quote gate and same-feed check. Deliberately **not** named "approved"
  to avoid drift into "approved to trade" — passing means *suitable for Level-1
  alerts and paper entry research only.*

### 7c. Dual-feed capture architecture (decision record)

*Status: architecture frozen; unbuilt pending live SIP feed subscription.*

To enable the live parallel dual-feed capture required to graduate a feed past
the historical validator, the system mandates a **two-tier** capture design. It
explicitly **rejects a stateful time-series DB** (e.g. QuestDB) to minimize local
operational overhead and keep replay determinism under our own control, and
**rejects columnar formats** (e.g. Parquet) at the capture edge to prevent
batch-flush data loss on crash.

**Tier 1 — capture edge (append-only length-framed binary log):** the single
source of truth, optimized for write throughput and strict replay determinism,
guaranteed by five constraints:

- **Integer scaling.** Prices stored as an integer number of ticks, timestamps as
  int64 nanoseconds. Floating-point types are banned in storage to eliminate
  serialization/deserialization rounding variance.
- **Stable tiebreaks.** Each record stores provider timestamp, local receipt
  timestamp, and a monotonically increasing `capture_seq` (uint64) — the
  deterministic tiebreaker when sorting bursts of events with identical provider
  timestamps.
- **Crash boundary (CRC32).** Each record carries a length prefix and a CRC32
  checksum. On **process** failure the reader truncates to the last complete
  CRC-valid record. On **machine or power** failure, events written since the most
  recent successful fsync may **also** be lost — CRC protects record-boundary
  integrity, it does not guarantee page-cache writes reached disk.
- **File topology + durability policy.** One file per `(feed, session)`,
  append-only, led by a self-describing header that records the durability policy,
  e.g. `{"schema_version":..., "feed_label":..., "session_date":...,
  "fsync_policy":"every_n_records", "fsync_interval_records":500,
  "last_durable_capture_seq":<n>}`, so the exact durability guarantee of any log
  is auditable after the fact.
- **Pipeline alignment.** Each binary record maps 1:1 to `bar_builder.MarketEvent`,
  so the entire audited pipeline runs identically on replayed data — turning the
  existing arrival-order shuffle test into the acceptance test for the capture
  format itself.

**Tier 2 — analytical read layer (Parquet):** used exclusively as a *derived*
read layer for `feed-quality-v2` cross-feed comparisons, generated post-session
directly from the Tier-1 binary log, and never a secondary source of truth.

## 8. Staged automation ladder (where we are, and are NOT)

- **Level 0A — Observational research (CURRENT):** HMM, scorecard, shadow tests,
  and feed qualification. No orders.
- **Level 0B — Manual paper trading with automated local guard (CURRENT):** the
  user initiates paper trades; Scalpr manages the local ratchet and paper exits /
  liquidation. This is where the core trading engine (Section 3) sits.
- **Level 1 — Entry-candidate alerts (NEXT, not built):** system proposes
  candidates; human decides. No automated order submission.
- **Level 2 — Frozen-policy paper automation (not built):** system places and
  manages paper orders under validated rules.
- **Level 3 — Human-confirmed live orders (not built).**
- **Level 4 — Small-capital automation with hard limits + broker-side stops +
  kill switch (not built).**
- **Level 5 — Broader automation (requires materially more evidence).**

The planned next artifact after feed qualification is **`entry-policy-v1`**: a
separate, frozen, paper-only entry engine consuming the regime + scorecard outputs,
recording every candidate (including rejected and missed), with explicit trade
geometry (entry / invalidation / target / reward:risk) and **net expectancy after
modeled costs** — kept separate from sizing. It has **not** been built, pending a
qualified intraday feed. Note: "modeled costs" must simulate **realistic bid/ask
crossing, routing fees, and SEC/FINRA fees on every hypothetical trade** — a paper
model that fills at mid with no fees will vastly overstate edge.

## 9. Known limitations / explicitly NOT yet built

These are **prerequisites for any live-money use** and are the first place a
reviewer should look.

1. **Guard state is in-memory only.** If the process stops (crash, sleep,
   restart), the ladder protection for open positions is lost while the position
   remains at the broker. No persistence/reconciliation on startup.
2. **No broker-side protective order.** The ratchet is entirely local; a crash /
   sleep / outage / stale feed removes all protection exactly when it matters.
3. **Options guarded off the mid, but exit at market.** Can overstate executable
   profit vs the bid (recommended fix: guard off the bid, keep mid informational).
4. **Market orders on options** (entry and exit) — hazardous in wide/illiquid
   contracts; no server-side liquidity gates (spread width, bid>0, size, max
   notional) enforced. Live code should ban option market orders in favor of
   marketable limit orders capped by a max-slippage threshold.
5. **Market-open check fails open** — if the clock lookup fails the trade proceeds
   (acceptable for paper, not for live; should fail closed in live mode).
6. **`/api/trades` input validation is minimal** (no Pydantic model / account-level
   risk caps). Empty ladder, negative tol/qty, absurd notional not all rejected.
7. **Manual sell can report success before the broker confirms the fill;** cancel/
   fill race after the 30s fill wait not fully reconciled; partial fills not handled.
8. **Data is quote-polling, not true tick/trade data.** "quote_size_imbalance" is
   from displayed quote size, not traded order flow. Real order flow needs a
   websocket trade feed.
9. **Dashboard inserts some API text via innerHTML** (localhost-only, but XSS-shaped
   if content were ever hostile). See #20 for the broader browser threat model.
10. **No depth-of-book, auction imbalance, ES futures, or options vol/skew** — needs
    feeds beyond SIP (e.g., Databento) for a full order-book strategy.
11. **No live parallel dual-feed capture / quote-level capture harness.** This is
    the structural roadblock to *finishing* feed qualification. The historical
    `/api/feed/quality` mode can only sanity-check bar construction; it is
    explicitly forbidden from approving a feed. NBBO spread accuracy, quote
    sequencing, crossed/locked detection, real-time latency, and disconnect
    behavior all require streaming both feeds live and logging quotes — not built.
12. **No order-execution state machine.** Execution today is HTTP fill-polling. A
    live engine needs an explicit state machine
    (`PENDING_SUBMIT → SUBMITTED → PARTIAL → FILLED / CANCELED / REJECTED`)
    reconciled against **websocket trade updates**, not polling — this is what
    properly resolves the manual-sell success-before-confirm and cancel/fill race
    (#7) and handles partial fills.
13. **No disaster-recovery protocol** for an upstream outage while holding a
    position. If Alpaca's API is down with an open guarded position, there is no
    defined fallback (e.g., out-of-band operator alert to liquidate manually via a
    mobile app). Must exist before any live tier.
14. **Cumulative late-tick magnitude not tracked.** `bar_builder` counts late /
    would-have-changed events but not the cumulative *magnitude* of rejected late
    ticks. During high-message-rate bursts (open, CPI/FOMC releases) finalized
    feature bars could silently drift; the running delta should be tracked + gated.
15. **No hard paper/live environment separation.** Going live must require more
    than a different endpoint/keyfile: a separately named live config, expected
    account-ID match, typed operator confirmation, confirmed broker-side
    protective-order capability, a passed persistence/reconciliation check, loaded
    risk limits, an unmistakable UI banner, and **no automatic fallback** between
    paper and live in either direction.
16. **No idempotent client-order-key policy.** Network retries can create duplicate
    orders unless every intended order has a deterministic client order ID and
    retry logic reconciles against it. Needed before even human-confirmed live orders.
17. **No stale-price / crossed-market execution gate.** The execution path must
    independently reject an order when: quote age exceeds a limit; bid or ask is
    absent; spread exceeds a limit; market is crossed/locked unexpectedly; provider
    timestamp is in the future; feed status is degraded; or broker and market-data
    symbols disagree.
18. **Grace period could suppress a necessary emergency exit.** The 60s "no exit
    after entry" rule applies **only to ordinary ratchet exits.** It must never
    override a catastrophic loss limit, a broker rejection/reconciliation fault, a
    stale-feed fail-safe, an invalid contract/quote state, or the account kill
    switch. (Not yet enforced as a separate emergency path.)
19. **No account-level exposure model.** No portfolio-wide controls for total gross
    exposure, correlated positions, option delta/gamma, max daily loss, max open
    positions, symbol concentration, or cumulative pending-order notional.
    Per-trade protection is insufficient once multiple trades can coexist.
20. **Localhost does not eliminate browser-origin risk.** Trade-changing endpoints
    are sensitive; the threat model must also cover CSRF-like requests from a
    malicious page, DNS rebinding, other local users/processes, accidental bind to
    `0.0.0.0`, and lack of request authentication. Even pre-live, consider: bind
    explicitly to `127.0.0.1`, a local API token/session secret, origin validation,
    CSRF protection for state-changing ops, no side-effectful GET endpoints, and a
    Content Security Policy.
21. **Credentials at rest.** `.gitignore` prevents Git inclusion but does not
    protect keys on disk. For live use, prefer macOS Keychain or an encrypted
    credential store over the plaintext `scalp.keys`.
22. **No clock-synchronization operating requirement.** Negative-delay/skew
    *detection* exists, but there is no operational requirement: host clock
    synchronized, max allowed skew, a startup clock-health test, periodic checks,
    and abstention if synchronization becomes unreliable. Without remediation the
    system can't distinguish a provider anomaly from a host-clock failure.

## 10. Statistical caveats (hold in mind during evaluation)

- **Overlapping-horizon serial dependence / effective sample size.** The
  cumulative forward-log-return targets overlap heavily at longer horizons (the
  30-bar target most of all), so the *effective* sample size is well below the
  fold count. The session-aware block bootstrap preserves this dependence in the
  null, but significance interpretation must not treat overlapping folds as
  independent observations.
- **20 sessions is a preliminary research gate, not a regime-representative
  sample.** Twenty sessions barely span one options-expiration cycle and likely
  capture a single volatility regime; they screen for gross failure but cannot
  validate value across volatility, macro, earnings, expiration, and trend
  regimes. Passing does **not** authorize entry-policy integration, paper
  automation, or live use.
- **Family-wise error correction.** Against a single pre-registered baseline
  (`rolling_return_sign`) none is required; if multiple theoretical baselines are
  ever tested, apply Holm-Bonferroni (or equivalent) rather than reporting the best.
- **Interim vs formal looks.** The 5- and 10-session reviews are interim
  operational monitoring; only the 20-session evaluation is the pre-registered
  formal look. Interim looks must not be used as evidence for changing thresholds.

## 11. Operational notes / gotchas

- Run the server in one Terminal tab; run any commands in a **separate** tab
  (Cmd+T), or commands queue into the busy server tab instead of executing.
- Browser caching can hide UI updates; the server sends no-cache headers, but a
  hard refresh / incognito is the fallback.
- `lsof -ti:8420 | xargs kill -9` frees a stuck port (`restart.sh` does this).
- The premarket shadow assessment must be frozen in the **09:05–09:25 ET** window,
  so the server needs to be up before ~09:00 ET on a session you want recorded.
- `.gitignore` excludes secrets (`scalp.keys`) and private data; the folder is not
  currently a git repo.

## 12. Endpoint reference

Trading/state: `GET /`, `GET /api/state`, `POST /api/trades`,
`GET /api/option/{expirations,strikes,quote}`, `POST /api/positions/{sym}/sell`,
`GET /api/holdings`, `POST /api/holdings/{sym}/liquidate`,
`POST /api/holdings/liquidate-all`, `GET /api/stats`, `GET /api/journal`.

Advisory / research (all observational): `GET /api/precheck`,
`GET /api/premarket`, `GET /api/journal/score`, `GET /api/microread`,
`GET /api/regime`, `GET /api/regime/validate`, `GET /api/regime/research`,
`POST /api/regime/snapshot`, `GET /api/regime/snapshots`, `GET /api/ticks`,
`GET /api/feed/quality`, `POST /api/premarket/shadow/log`,
`GET /api/premarket/shadow/summary`, `GET /api/premarket/shadow/session`.

## 13. Component version register

| Component | Version | Status |
|---|---|---|
| Regime research | `regime-research-v3` | frozen, 0/20 sessions collected |
| HMM model | `hmm-v1` | frozen |
| Premarket scorecard | `premarket-scorecard-v1` | frozen, 0/20 sessions collected |
| Premarket policy | `premarket-policy-v1` | frozen |
| Price convention | `price-convention-v1` | frozen |
| Bar builder | `bar-builder-v1` | frozen |
| Feed quality | `feed-quality-v2` | provisional, awaiting SIP |
| Pilot protocol | `pilot-protocol-v2` | frozen — 0DTE permitted (see `PILOT_PROTOCOL.md`) |
| Intel feature schema | `scalpr-intel-v0` | exploratory — non-qualifying, no ML |
| Intel rules score | `scalpr-intel-v0` | exploratory — three separate scores, no probability |
| Intel label policy | `scalpr-intel-label-v0` | exploratory — delta-gamma proxy labels |
| Intel outcome engine | `label-lifecycle-v0` | exploratory — lifecycle manager |
| Wave Riding engine | `wave-riding-v0` | EXPERIMENTAL — shadow-only, no live orders |
| Wave Riding ATR | `intraday-atr-v0` | experimental — 5-min frozen intraday ATR |
| Wave Riding shadow fill | `wave-riding-shadow-fill-v0` | experimental — conservative sim fills |
| Wave Riding baselines | `wave-riding-baseline-v0` | experimental — 3-track comparison |
| Wave Riding observer | `wave-riding-shadow-observer-v0` | EXPERIMENTAL — live shadow observation, no live orders |
| Wave Riding cohort A | `wave-riding-v0-observation-cohort-a` | operational validation — report-only, frozen config, not an edge claim |
| Entry incubation study | `standard-entry-incubation-study-v0` | replay-only engine (activation × hard-stop factorial) |
| Entry incubation shadow | `entry-incubation-shadow-v0` | DORMANT — capture infra built, flag off, not wired, cohort unlocked |
| Incubation hard-stop dim | `incubation-hard-stop-dimension-v0` | verified: live Guard has NO hard stop; scenarios None/15/12.5/10 |
| Options-flow evidence | `flow-evidence-v0` | read-only 🟢/🟠 ranking; evidence only, NOT a probability/signal |

## 13a. Scalpr Intelligence — Phase 1 (`scalpr-intel-v0`)

**Status: exploratory data-generation only. `formal_cohort_eligible = false`
everywhere. No machine learning. No calibrated probabilities. No expected-value
scoring, intent inference, spread selection, or any change to live execution —
those are explicitly out of scope for Phase 1.** This layer instruments and
labels; it does not recommend or trade.

**Files:** `feature_engine.py` (feature record + rules score + snapshot writer),
`label_lifecycle.py` (label lifecycle manager). Endpoints: `GET
/api/intel/features/{ticker}`, `GET /api/intel/score/{ticker}`, `POST
/api/intel/label/{ticker}` (ad-hoc). The automated path runs inside the existing
shadow-loop close workflow — not a second scheduler.

**Versions.**
- Feature schema: `scalpr-intel-v0`. Field *meanings* are immutable within a
  version; new fields arrive under a bumped version (stub-now / fill-later is
  additive, never a rewrite).
- Rules score: `scalpr-intel-v0`. Three **separate** scores — direction,
  trade-quality, executability — never blended, no probability. A correct
  direction can still be `NO_TRADE` (executability veto).
- Label policy: `scalpr-intel-label-v0`. Outcome engine: `label-lifecycle-v0`.

**Delta-gamma proxy labels.** There is no realized forward option-price feed yet,
so per-contract target-before-stop is computed from the realized *underlying* bar
path translated to option terms by a first-order delta+gamma proxy. Every label
carries `label_basis: "delta_gamma_proxy"`, `realized_option_path: false`, and a
`proxy_ignores` list (IV change, theta decay, real bid/ask at exit, real fills).
Automation never presents the proxy as more authoritative than it is; realized
labels will supersede it under a new label version.

**Lifecycle, not a nightly calc.** The close routine reads the immutable
decision-time snapshot (feature record + frozen in-band contract universe,
delta 0.15–0.70), labels using only bars strictly after the decision timestamp,
and advances a state machine: `PENDING` (horizon not yet elapsed — supports
multi-session horizons) → `FINAL` / `UNLABELABLE`, with `SUPERSEDED` corrections
and `ERROR_RETRYABLE` for transient failures. Canonical identity is
`(ticker, decision_timestamp, schema_version, label_policy_version, contract)`;
identical reruns are no-ops, a differing result after a version bump is written
as a versioned correction (prior + new label hash, reason, timestamp, code
version) with both records preserved. Per-contract/ticker failures are isolated.

**Explicitly unavailable data groups (honest stubs, never fabricated).** Emitted
as explicit `null` with a per-group `data_status` until wired:
- *market_regime:* `spy_trend`, `qqq_trend`, `sector_relative_strength`,
  `vix_percentile`, `breadth_score` (HMM state + premarket background ARE wired).
- *options_flow (side-classified):* `call_premium_bought`, `put_premium_bought`,
  `net_directional_delta`, `sweep_persistence`, opening buy volume by side,
  `multi_expiry_confirmation` (aggregate OI/volume + `oi_since_prev` ARE wired).
- *volatility:* `iv_percentile`, `term_structure_slope`, `put_call_skew`,
  `realized_implied_spread`, `expected_move` — these are the workup's
  fetched-then-discarded endpoints, gated on the still-open payload check
  (`iv_rank` + realized vol ARE wired).
- *underlying:* `rsi_5m`, `relative_volume`, `distance_from_vwap_atr` (VWAP,
  opening range, session structure ARE wired).

**Pipeline.** decision-time snapshot → frozen contract universe → forward
observation → PENDING/FINAL contract labels → rules-score-vs-outcome comparison.
This is the dataset that will eventually show whether Scalpr's rules carry
predictive value — before any model is trained on top of them.

**Production-readiness hardening (audit).** The snapshot freezes the COMPLETE
in-band universe (no top-N/affordable cap). Snapshot identity is version-scoped
`(ticker, session_date, decision-minute, schema, universe, rules version)` —
one snapshot per ticker per minute per version; same-minute re-fires dedupe and
are logged. Persistence is crash-safe (atomic append + fsync, resync past a
truncated line; readers skip corrupt/incomplete lines). Decision-time quote
quality is frozen (crossed/missing/unusable → UNLABELABLE) with quote age vs the
workup pull, and staleness is TWO-TIER: age > 15 min is a minor-stale WARNING
(still labelable); age > 60 min is materially stale → `UNLABELABLE_STALE` (a
distinct terminal state). Materially-stale and other unlabelable contracts stay
frozen in the research universe — only the label is withheld. The validation
report tallies label outcomes separately by quote bucket (high_quality /
warning_quality / locked / stale_unlabelable / unusable_unlabelable). Collision policy is versioned and
conservative (`stop_first`: a same-bar target+stop is `ambiguous_same_bar`,
never a win — intrabar ordering is never inferred from OHLC). Forward bars come
from the exchange trading calendar (early closes, holidays, DST), not an assumed
09:30–16:00. Operational metrics: `intel_validation_report.py` →
`INTEL_VALIDATION_REPORT.md`.

## 14. Terminology glossary

- **available / degraded / unavailable / stale** — per-input data status in the
  premarket scorecard. *available*: reliable, normal calculation. *degraded*:
  usable proxy or partial data (e.g. thin IEX premarket). *unavailable*: no data on
  the current feed — excluded from aggregation, lowers confidence, never neutral.
  *stale*: last update older than the allowed age.
- **unaudited** — tick data predating provider-timestamp capture; timing quality
  cannot be verified, so it's flagged rather than trusted.
- **experimental / validation_ready** — regime data tiers: *experimental* = enough
  bars to display but not to trust; *validation_ready* = enough to power
  out-of-sample predictive claims.
- **eligible** — a session/assessment that passes all gates required to enter a
  formal sample (e.g. shadow: pre-cutoff freeze + complete outcomes).
- **passed** — a specific quality gate met its thresholds (e.g. bar_quality_gate).
- **frozen** — a versioned component whose logic is locked; a change requires a new
  version and a fresh evidence cohort.
- **provisional** — a feed-quality result that has not (and, if historical, cannot)
  earn live-research eligibility.
- **Level-1 research** — entry-candidate alerts + paper entry research; explicitly
  NOT autonomous live execution.

## 15. Open questions for reviewers

Please scrutinize especially:

1. **Research validity:** Is the session-aware moving-block bootstrap the right
   controlling null for intraday, overlapping folds — and is the time-of-day
   seasonality caveat adequately handled, or is a clock-stratified null needed? Is
   a single pre-registered baseline sufficient? Is 20 sessions defensible even as a
   preliminary gate?
2. **Model stability across refits:** Beyond single-fit diagnostics, are
   transition-matrix drift, emission-parameter drift, state-label continuity,
   posterior-calibration stability, label-reassignment frequency, and sensitivity
   to training-window length being watched? A model can pass occupancy/separation
   gates yet produce economically inconsistent states day to day.
3. **Premarket scorecard:** Does the data-confidence model correctly prevent
   false-balanced reads? Are the degraded/unavailable classifications honest given
   the IEX feed? Is the decision-policy gate ordering sound?
4. **Missing-data mechanism:** Is data missing at random, or does it disappear
   precisely during stressed periods (opens, halts, CPI, vol shocks)? If the latter,
   excluding those observations biases performance upward.
5. **Abstention performance:** Is abstention actually useful — frequency, outcomes
   during abstained sessions, and whether "high confidence" is better calibrated
   than "low confidence"?
6. **Shadow forward test:** Are the eligibility gates (pre-cutoff freeze +
   completeness) sufficient to keep the sample uncontaminated? Is the
   directional-correctness vs decision-quality split measured correctly?
7. **Timing contract:** Any remaining look-ahead or leakage paths in the
   event→bar→feature→target chain? (Note the bar-timestamp-semantics assumption —
   bar-start vs bar-end.)
8. **Feed qualification:** Are the gates and the historical-can't-approve rule
   sufficient before trusting SIP for Level-1 research?
9. **Safety boundaries:** Given the Known-limitations list (esp. #15–#22), are the
   paper-only constraints and the staged ladder adequate to prevent premature live
   use? What is missing before even Level-3 (human-confirmed) live orders?
10. **Symbol scope / transferability:** Everything is SPY-tuned. Are any thresholds
    silently assumed transferable to other symbols?
11. **Anything structurally absent** — a whole category of risk, validation, or
    data integrity not represented here at all.

---

*Review history: this document has incorporated multiple external-review rounds.
The current version added an executive summary + claim/evidence table, corrected
the Level-0 observational/manual-paper-trading distinction (Section 8), tightened
"official open" to the `price-convention-v1` definition (6c), corrected the binary-
log durability statement around fsync/power loss (7c), added safety limitations
#15–#22, added the HMM reproducibility spec and moving-block seasonality caveat
(6a), and added a glossary and model-stability / missing-data / abstention research
questions. New reviewers are encouraged to go beyond all of the above.*
