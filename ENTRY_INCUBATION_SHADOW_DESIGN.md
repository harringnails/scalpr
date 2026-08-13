# ENTRY_INCUBATION_SHADOW — Design Return (awaiting approval)

*Forward-looking, SHADOW-ONLY observational study. It must not modify the live
Guard, live exit behavior, broker orders, or Standard mode. It records
decision-time + forward option-market data for every eligible real trade so the
existing `entry_incubation_study` engine can compare the current immediate-arm
ratchet against incubation variants on the exact same observed option path.*

**Design only — no code written. Stops here for approval.**

Determines whether: (1) Current Scalpr exits recoverable trades prematurely,
(2) current initial protection lets weak trades fall too far, (3) incubation
improves opportunity capture enough to justify its added downside risk.

---

## 1. Reusable existing components

| Asset | Reused for |
|---|---|
| `entry_incubation_study.py` (built + tested) | the pure replay engine — variants A–H, diagnostics, hard stop. Capture feeds it; it is unchanged. |
| `wave_quotes.py` | synchronized read-only observation pattern: underlying (IEX) + option (OPRA), independent timestamps, skew gate, quote-quality. |
| `wave_store.py` | atomic append, per-entity stream, active index, restart `reconstruct`, sequence dedupe. |
| `feature_engine.py` | `_atomic_append`, `_iter_jsonl`, `canonical_hash`, `assess_quote_quality` (two-tier staleness). |
| `wave_atr.py` (`intraday-atr-v0`) | the entry snapshot's 5-minute ATR. |
| `workup.py` `greeks()` (Black-Scholes) | delta/gamma/theta/IV at entry when the broker snapshot lacks them (else explicit null). |
| `wave_cohort.py` pattern | frozen cohort + hash + acceptance gates + aggregate report. |
| `scalp_server.Guard.snapshot()` | reading live Guard state/peak/exit into the forward path (read-only). |
| `session_snapshot._atomic_write_json` pattern | atomic per-trade record. |
| poll loop's `option_data.get_option_latest_quote` + `stock_data.get_stock_latest_quote` | the exact capture sources already in use. |

## 2. Proposed files and module boundaries

Clean split — **capture** ≠ **analysis** ≠ **reporting**:

| Module | Responsibility |
|---|---|
| `incubation_config.py` | versioned `IncubationConfig` (recovery window, frozen hard stop, materiality thresholds, activation params, feature flag). |
| `incubation_shadow.py` | capture lifecycle: entry-snapshot builder, forward-observation tick, recovery-window state machine, per-trade record. Read-only. |
| `incubation_store.py` | persistence: entry snapshots, per-trade forward-path streams, active index, dedupe, restart reconstruction (thin wrapper over `feature_engine` atomics; mirrors `wave_store`). |
| `entry_incubation_study.py` | **existing** pure replay/analysis engine — consumes a captured path, emits A–H + diagnostics. Unchanged. |
| `incubation_cohort.py` | frozen cohort registration, "fully observed" gating, aggregate report + breakdowns + variant ranking (calls `entry_incubation_study` at finalize). |
| `test_incubation_shadow.py` | capture + dedupe + restart + isolation tests. |

Server: one gated entry hook + one gated read-only forward-observation tick +
read-only status/report endpoints. No changes to Guard or order paths.

## 3. Exact entry-hook location

At the confirmed live fill, immediately after the Guard is created — in
`scalp_server.py`, `Platform`, where `self.guards[sym] = Guard(cfg, entry, qty,
entry_signals)` (≈ line 331). A single gated, exception-isolated call:

```
if incubation_config.enabled():
    try: incubation_shadow.on_entry(self, sym, guard)   # READ-ONLY snapshot
    except Exception as e: log(f"incubation snapshot skipped: {e}")
```

It reads the just-created guard (entry, qty) + fetches one synchronized quote,
writes an entry snapshot, and registers the trade for forward observation. It
never mutates the guard, the order, or `self.guards`.

## 4. Option-quote capture source and cadence

- **Source (field-level provenance, not uniformly IEX):** option bid/ask/mid +
  quote timestamp from `option_data.get_option_latest_quote` (**OPRA**);
  underlying price + timestamp from `stock_data.get_stock_latest_quote`
  (**IEX/SIP**); 5-min ATR from IEX minute bars. Greeks/IV from the Alpaca option
  snapshot if available, else computed via `workup.greeks()`, else explicit null
  with availability status.
- **Cadence:** piggyback the existing poll loop (`POLL_SECONDS`), read-only, like
  the Wave observer — but with a **rate-limit fix learned from Cohort A**: batch
  ALL currently-observed option contracts into ONE `get_option_latest_quote`
  request per cycle (the endpoint accepts a symbol list), and one batched
  underlying request. This keeps the added API load to ~2 calls/cycle regardless
  of how many trades are under observation.

## 5. Observation identity and deduplication

- **Identity:** `canonical_hash(trade_id, option_quote_timestamp,
  underlying_quote_timestamp, observation_sequence, study_version)`.
- **Monotonic per-trade sequence.** An observation is appended only if its option
  provider timestamp is newer than the last recorded one; a repeated poll that
  returns the same quote timestamp is an auditable no-op (`DUPLICATE_OBSERVATION`),
  out-of-order is `OUT_OF_ORDER_OBSERVATION`. Identity is persisted **before** the
  record is committed. (Same discipline as `wave_observer`.)
- Repeated polls therefore never create duplicate canonical observations.

## 6. Post-live-exit recovery-window lifecycle

State machine per trade:

```
ENTRY_SNAPSHOT_PENDING → OBSERVING_PRE_EXIT
  → (live Guard exit captured) → OBSERVING_RECOVERY
  → COMPLETE            (recovery window fully captured)
  or INCOMPLETE_TERMINAL (ended for a valid reason: market close / expiration /
                          data-quality failure over limit)
  or ABANDONED          (restart gap made it unobservable)
```

Capture continues **after** the live exit until the earliest of: **15 min after
live exit · market close · option expiration · data-quality failure exceeding the
configured limit** (recovery window configurable + versioned). The live position
may already be closed; all shadow variants are still evaluated later against the
full recorded path.

## 7. Variant state model

**Capture is variant-agnostic** — it records ONE canonical option path plus the
live Guard's own exit event. At finalize (trade `COMPLETE`), the pure
`entry_incubation_study.replay_trade(trade, path)` deterministically computes all
eight variants + both diagnostics from that single path. No live per-variant
state machines run; nothing is live.

Integrity cross-check: the replayed `CURRENT` exit is compared to the **actual
live Guard exit** captured in the path (a parity check like the Wave baseline
parity). Agreement validates the replay; divergence is flagged, not hidden.

## 8. Materiality definitions (versioned + pre-registered)

- `PREMATURE_EXIT`: the live Guard exits AND the executable option **bid**
  subsequently recovers by **≥ `RECOVERY_MATERIALITY_PP`** (default +10 pp of
  return) within the observation window.
- `LOOSE_INITIAL_PROTECTION`: the current initial-rung behavior yields a loss
  **≥ `LOOSE_MATERIALITY_PP`** (default 10 pp) larger than at least one safer
  variant AND no later qualifying recovery occurs.

Both thresholds are frozen in the cohort config hash.

## 9. Cohort acceptance rules

Register `standard-entry-incubation-shadow-cohort-a` (frozen config + hash).
Gates: **≥ 30 fully-observed trades · ≥ 10 sessions · ≥ 10 CALL · ≥ 10 PUT · ≥ 5
underlyings · no policy/threshold change** during the cohort. A trade is
**fully observed** only when: a valid entry snapshot exists, the option path is
sufficiently complete (coverage/gap thresholds met), the live exit is captured,
the recovery window is captured or ended for a valid terminal reason, and all
variants are evaluable from the same path. Report only when gates are met.

## 10. Storage-volume estimate

- Forward observation ≈ 15–20 fields ≈ ~350–450 B JSON. At `POLL_SECONDS` (~2–3 s)
  a ~30-min trade + 15-min recovery ≈ ~900 observations ≈ **~0.3–0.5 MB/trade**
  (less after dedupe of unchanged-timestamp polls).
- Entry snapshot ≈ 1–2 KB. A 30-trade cohort ≈ **~10–20 MB** total. Bounded and
  small; append-only logs rotate per cohort.

## 11. Failure and restart behavior

- **Append-only + atomic** writes (`_atomic_append` for streams; temp+fsync+rename
  for per-trade records); **tolerant reads** skip corrupt individual records.
- **Restart:** reload active observed trades from the active index; **do not
  manufacture** missed observations (a gap is recorded honestly); resume from the
  current time with a fresh, sequence-continuing observation; a trade whose
  recovery window elapsed during downtime → `ABANDONED` (excluded from
  fully-observed). Dedupe prevents double observation across restart.
- **Idempotent:** observation-identity dedupe; finalize is idempotent (a re-run
  yields the same canonical variant results or a versioned correction).
- **No interpolation:** missing/unsynchronized quotes are recorded as such; gaps
  are never filled unless a separately versioned interpolation policy is
  explicitly approved (default: none).

## 12. Proof no live behavior will change

- The entry hook only **reads** the just-created guard + fetches a quote and
  writes to its own store; it never mutates `self.guards`, the Guard's fields, or
  submits/cancels/replaces any order.
- The forward tick is a **separate, read-only** poll-loop call, exception-isolated
  (a failure cannot break the live poll) and feature-flagged (default OFF → zero
  incubation code runs, Standard mode byte-for-byte identical).
- Variants are **replayed offline** at finalize (pure functions) — never live.
- Verifiable the same way as the Wave modules: `grep` shows no `submit_order` /
  order requests and no `platform.guards[...] =` writes in any incubation module;
  an isolation test asserts Standard-mode endpoints are unchanged when the flag is
  off.

## 13. Design note — your two-problem hypothesis (built in)

You're likely right that these are **two problems, not one**:

- **Strong trades need incubation** — addressed directly by variants B–H
  (delay/buffer arming). `PREMATURE_EXIT` measures the benefit.
- **Weak trades need tighter downside control than the 15% giveback** — the
  delay variants alone make early protection *looser*, so the lever for this
  problem is the **frozen hard stop**. To actually test problem 2, I recommend
  the hard-stop threshold be a **versioned study dimension** (e.g. evaluate the
  cohort at a couple of hard-stop levels, one *tighter* than the current ~−15%
  effective initial-rung loss). `LOOSE_INITIAL_PROTECTION` measures whether a
  tighter early cap would have reduced losses without killing recoveries.

That lets the one cohort answer the real question: can a single activation policy
+ hard-stop level improve **both**, or does Scalpr need **two distinct
entry-management regimes** (incubation for strong trades, tighter early stop for
weak ones)? The study is designed to distinguish those outcomes rather than
assume one policy fixes both.

---

*Live Guard, thresholds, order paths, and Standard mode are untouched by this
design. No code written yet — awaiting approval before implementation.*
