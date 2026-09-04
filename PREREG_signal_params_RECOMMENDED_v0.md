# Operator-Approved Frozen Parameters — Signal Studies A & B

**Approved:** 2026-09-04. The two study preregistrations are normative and are SHA-256 stamped into every episode record. Any post-freeze parameter change requires a new study ID and resets N to zero.

---

## Study A — Prior-Regime Flip Reclaim

| Parameter | Recommended | Why this value |
|---|---|---|
| `A` — acceptance band (pts below reclaimed flip still counts as "holding") | **0.20** | Absorbs normal SPY tick noise (~2–3 bp) so a real hold isn't broken by jitter, but tight enough that a failed reclaim can't masquerade as acceptance. |
| `W` — acceptance hold window | **900 s (15 min)** | Long enough to separate a genuine reclaim from a spike-through; aligns with the shortest outcome horizon (5–15 min). |
| `G` — cumulative back-below grace within `W` | **120 s** | Tolerates brief dips under the level; rejects sustained failure to hold. |
| `N` — target episodes **per cohort** (H1a, H1b separately) | **150** | Inferential floor for the matched-null + 4-fold design. See the timeline note below. |
| `p` threshold | **≤ 0.01** | Standard strict bar; permutation null `p=(k+1)/(n+1)`. |
| Walk-forward | **4-fold chronological, sign consistent in ≥ 3 of 4** | Guards against a result driven by one regime window. |
| Horizons / freshness | 5/15/30/60 min, all required; ≤ 5 s (fixed) | Unchanged from spec. |

## Study B — Intraday Continuation

| Parameter | Recommended | Why this value |
|---|---|---|
| `S` — sweep depth beyond local extreme | **0.25 pts** | A real liquidity sweep, clearly above routine intrabar jitter, without requiring a rare large spike. |
| `R` — reclaim window | **180 s** | Operator-approved compromise after calibration proposed 425 s; preserves a rapid-reclaim requirement without retaining the original 90 s cutoff. |
| Acceptance | **≥ 3 consecutive positive 1-min returns** (fixed) | Confirms follow-through before the anchor. |
| `V` — proxy-VWAP slope window | **2 min** | Calibrated shortest window meeting the pre-freeze quote-mid proxy slope-persistence criterion. |
| Wall-migration confirm | next 5-min FlashAlpha read; `t0` set at confirmation (fixed) | No look-ahead — outcome measured only after the full signature is in hand. |
| Executed volume | **EXCLUDED** (fixed) | No trustworthy point-in-time trade-volume source yet. |
| `N`, `p`, folds | **150**, **≤ 0.01**, **4-fold ≥ 3/4** | Same design as Study A. |

## Shared (already fixed, listed for completeness)

Matched null = same session-time block + realized-vol bucket, `p=(k+1)/(n+1)`. Outcome basis = SPY underlying dense forward return. Today and all pre-lock sessions are in-sample/excluded. Guard operability is scored separately (Study 3) and never enters these verdicts.

---

## Freeze boundary

Every session on or before 2026-09-04 is in-sample and excluded. Prospective accrual begins on the next full RTH session after the freeze commit. N remains 150 per Study A cohort and 150 for Study B; p ≤ 0.01 and the four-fold chronological ≥3/4 sign-consistency rule are unchanged.
