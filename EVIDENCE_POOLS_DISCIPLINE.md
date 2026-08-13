# Evidence Pools Discipline

**Status:** discipline spec
**Scope:** all SCALPR outcome-bearing records and analyses
**Rule:** do not pool these streams into one edge story

## Why this exists

SCALPR now has multiple evidence streams that look similar at a glance but are not interchangeable:

- manual Guard trades
- wave shadow outcomes
- incubation outcomes
- entry-intelligence / reversal study outputs

If these get mixed, the platform can accidentally claim one signal has edge when the evidence actually came from a different decision rule, different risk model, or different label basis.

## The four pools

### 1. Manual Guard

**Purpose:** realized paper trade behavior under the live Guard / ratchet / hard-stop mechanics.

**What it answers:**

- How does the protection logic behave?
- Do winners run?
- Do losers get cut before catastrophic bleed?

**What it is not:**

- Not a signal-validity claim
- Not a clean edge estimate
- Not a substitute for prospective study design

### 2. Wave Shadow

**Purpose:** simulated or shadow-only wave-riding behavior.

**What it answers:**

- Does the shadow exit logic execute deterministically?
- Are the observer and exit records complete?
- Does the shadow path stay isolated from live orders?

**What it is not:**

- Not live-trading evidence
- Not proof of profitability
- Not a direct proxy for Guard

### 3. Incubation

**Purpose:** controlled forward-only experiments that are still in observation / cohort formation.

**What it answers:**

- Does the candidate flow behave as designed?
- Are the exit and observation lifecycles coherent?
- Are the cohort gates working?

**What it is not:**

- Not a final performance claim
- Not allowed to leak into Guard conclusions

### 4. Entry-Intelligence / Reversal

**Purpose:** directional setup characterization and outcome-basis testing.

**What it answers:**

- Does the setup fire?
- Can a contract be labeled reliably?
- Is there an honest edge verdict against a null?

**What it is not:**

- Not the same thing as Guard outcome quality
- Not the same thing as wave or incubation

## Analysis rules

- Keep the pools separate in docs, plots, summaries, and conclusions.
- Never blend counts across pools without explicitly labeling the blend as a meta-summary.
- Never use one pool as hidden validation for another.
- If a result depends on a particular pool, say so explicitly.
- If a decision changes the pool definition, register that as a new version.

## Practical enforcement

- Separate filenames and separate summary sections.
- Separate baseline metrics.
- Separate nulls and walk-forwards.
- Separate cohort labels.
- Separate bias notes.

## Resulting discipline

This keeps the platform honest:

- Guard can improve realized downside control without being mistaken for signal edge.
- Wave can validate mechanics without being mistaken for live alpha.
- Incubation can mature without contaminating the reversal study.
- Entry-intelligence can answer the edge question on its own terms.
