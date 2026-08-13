# Cohort A — Operational Findings Log

*Documentation only. Records operational findings observed while accumulating
`wave-riding-v0-observation-cohort-a`. This file changes **no** code, behavior,
or reporting schema, and the frozen config hash remains `995379c6d360…`.
Findings here are deferred to the review checkpoint unless marked as requiring
immediate intervention.*

---

## F1 — API rate limit under high concurrency  ·  LIMITATION  ·  severity: LOW (normal use)

- **Observed:** 2026-07-30 (cohort day 1), during a burst of ~15 concurrent
  simulations started for testing.
- **Symptom:** `tick log skipped: {"message": "too many requests."}` — Alpaca
  API rate limit.
- **Cause:** each active shadow simulation fetches, every poll cycle, an
  underlying quote + an option quote + underlying minute bars. Many simultaneous
  simulations multiply the per-cycle API calls and exceed Alpaca's request cap.
- **Impact:** a rate-limited observation is **skipped gracefully** (per-position
  try/except; the simulation resumes on the next tick) — **no data corruption**.
  The burst did briefly starve the core Scalpr tick logger.
- **Severity:** **LOW under normal single-user cadence** (a few simulations per
  day). Only appears under many-concurrent bursts, which is not the intended
  usage pattern.
- **Workaround (no code, in effect now):** keep the number of *concurrently
  active* simulations low (roughly ≤ 5). Let running ones finish before starting
  new ones. A few per day comfortably stays under the limit.
- **Proper fix (DEFERRED — not applied mid-cohort):** throttle / batch the
  observer's quote calls, or cap concurrent active simulations. Either changes
  how often observations are taken, i.e. **observation acceptance / add timing /
  reversal timing** — which, per the freeze rules, would require **restarting
  Cohort A or registering a new cohort version + hash**. Therefore it is *not*
  patched during the frozen run.
- **Status:** logged; revisit at the review checkpoint (or sooner only if it
  starts materially degrading the core tick logger during normal low-concurrency
  use).

---

## O1 — No additions observed on day 1  ·  OBSERVATION  ·  not a conclusion

- **Observed:** across all day-1 simulations (~14 completed + several open),
  the `additions` count is **0** for every one; reversals exit at the giveback
  floor before an add trigger fires.
- **Why it matters:** whether additions ever fire is the central question this
  cohort exists to answer — "no adds ever" would mean the feature never actually
  "rides."
- **Discipline:** far too early to conclude anything (n≈14, a single session).
  **No action, no config change.**
- **Action at review:** quantify with the report's add-trigger-behavior,
  reversal-behavior, and time-held sections across the full 30-sim / 10-session
  cohort.

---

*Frozen config hash: `995379c6d360…` (unchanged). No thresholds, observer logic,
fill model, or reporting schema were modified in response to these findings.*
