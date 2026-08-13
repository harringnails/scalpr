# Deployment Sequence — Entry Intelligence v1 (staged, for Codex)

**"Deploy" is four ordered actions, not one.** Do them in sequence; do not skip the gate between the dry run and the lock. Paper/shadow only throughout — no live trading, no Guard contact, no order authority at any stage.

**Roles:** Codex executes the flag/restart/lock mechanics **only on explicit operator instruction**. The operator authorizes each gate — especially the cohort lock, which is a one-way commitment.

---

## Stage 1 — Enable the collector as a NON-COHORT dry run (safe to do now)

Goal: start forward executable-bid capture and prove it works, while everything stays cohort-ineligible.

Codex actions (on instruction):
1. Confirm the paper account is **flat** (`holdings == 0`) — the restart guard already enforces this.
2. Flip the collector enable flag on (the default-off `entry_bid_capture_enabled()` control), leaving cohorts **unlocked** (`operator_confirmed: false`, `target_session: null`).
3. Controlled restart to pick up the flag.

Acceptance before Stage 1 is "done" (verify, ideally across 1–2 sessions):
- Collector status reports **ENABLED / capturing** (no longer `DISABLED_DEFAULT_OFF`).
- Bid records are being written to the capture store; coverage is healthy (meets the 80% / ≤15s-gap rules).
- `decision_events` / decision packets are appending; NO_TRADE hypothetical outcomes land in the **separate** hypothetical store.
- Both cohort JSONs still show `operator_confirmed: false`, `target_session: null` — **the dry run must not have locked anything.**
- No broker orders, no Guard interaction, no execution authority (spot-check the import graph / health events).

**If any acceptance item fails → stop, fix, do not proceed to the lock.**

---

## Stage 2 — GATE (must all be true before any lock)

Do **not** proceed to Stage 3 until:
- **Bar/quote-quality gate is repaired and green.** The audit flagged it *provisional / failing* RTH-bar and premarket checks. The cohort's direction axis is computed from completed 5-minute bars, so locking before this passes freezes the study on an unvalidated data foundation. Confirm the bars feeding the direction axis are trustworthy.
- **Dry-run capture verified** clean over ≥1–2 sessions (coverage, no gaps, correct simulated-fill labeling).
- **Disk headroom** is comfortable (not near the 15%/10% alert thresholds) so append-only writes can't fail mid-session.
- Thresholds are the approved set (6% / $5.00 / 0.20 + agreed defaults) and implementation hashes are stamped.

---

## Stage 3 — Pre-open cohort lock (operator-authorized, one-way)

Only after Stage 2. This is the prospective commitment.

Timing/safety:
- Perform **before 09:30 America/New_York**, with the account **flat**.
- Apply to **both mirrored cohorts** (`low_reversal_v1` CALL and `high_reversal_v1` PUT) or lock them independently — but each is its own commitment.

Codex actions (on explicit operator lock approval):
1. Set `target_session` to the specific upcoming session date.
2. Set `operator_confirmed: true`; clear remaining `unconfirmed_fields`.
3. Stamp rules/capture/outcome/episode/resolved-config hashes.
4. Set status `READY_TO_LOCK` → run the fail-closed lock registry (`entry_cohort_lock_v1.py`) → append the lock entry to `entry_intelligence_cohort_locks_v1.jsonl`.

After lock: **no parameter change without a new cohort id.** A cohort id can never be relocked to different content.

---

## Stage 4 — Confirm prospective capture is counting

On/after the target session:
- Eligible, non-overlapping episodes begin accruing toward the 200-candidate gate.
- Rejected episodes accrue in the separate hypothetical store (shared overlap controls prevent double-population).
- Net outcomes compute at 0/1/2-tick sensitivity; the primary metric stays **unclaimed** until ≥200 candidates + walk-forward + session-block null all pass.

---

## Stop / rollback conditions
- Any Stage 1 acceptance failure → disable the flag, restart, investigate. (Disabling is safe; it just stops capture.)
- Capture coverage degrades or the bar-quality gate regresses → do not lock / pause before the next session.
- Disk crosses the critical alert → pause new collection (never block the Guard).

## Hard nevers (all stages)
- No live/real-money trading; paper/shadow only.
- No Guard, order, or execution authority from the collector or replay modules.
- No cohort lock outside the pre-open, account-flat window.
- No relock or post-lock parameter edit — new cohort id instead.
- No promotion / "edge" claim before the full validation gate (cost-adjusted, walk-forward, null-cleared).
