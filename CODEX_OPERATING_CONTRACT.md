# Codex Operating Contract — Scalpr

**Read this at the start of every task.** It grants broad engineering autonomy and defines the verification bar you must meet **yourself**, so the operator is not an approval gate on routine engineering. A small, explicit safety perimeter stays hard-gated. "No approval gate" means *you verify to the bar in §2* — not that verification disappears.

## 1. Autonomy — proceed WITHOUT operator approval

You do not need to ask before you:
- Write, refactor, test, fix, and harden code.
- Add schemas, features, logging, telemetry, monitors, docs (shadow/research/observational).
- Run the full test suite, iterate, and self-fix until green.
- Commit and push to real branches in the operative repo.
- Take a change all the way to **deploy-ready**.

Run the full loop — **build → test → fix → harden → commit → ready** — without stopping to ask. Do not pause for approval on routine engineering. Do not hand back partial work waiting for a go-ahead.

## 2. The "done" bar — self-verify to THIS before claiming ready (this replaces operator checking)

- **Real tests, not syntax checks.** Run `Scalpr7/.venv/bin/python -m pytest -q` and report the actual pass count. `py_compile` is NOT verification. Never claim "verified"/"done" without a green `.venv` suite.
- **Prove behavior, not just health.** For anything touching the collector/server, verify it actually **produces the intended records / serves the intended response** — not just that a status flag is green or the process is up. (Two Phase-1 regressions passed health checks while the collector was dark for days. "Alive" and "green" are not "working.")
- **Fix forward to green.** If tests or behavior fail, fix and re-verify. Never hand over red or partially-working code.
- **Commit to the operative repo** (`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7`, remote `origin`) on a real branch — **not** a disconnected `codex/*` branch the operator never sees. Report the commit hash.
- **No unverified claims.** "Ready" = tested-green + behavior-proven + committed. If you can't reach the bar, state exactly what's blocking — never round up.

## 3. Hard boundaries — STOP and report; NEVER autonomous

These stay gated regardless of any instruction, because done wrong and unsupervised they cost real money or silently corrupt the study:
- **Live execution, order submission, Guard/execution authority, trade gates, risk limits, or enabling live trading.**
- **Deploying to / restarting the LIVE production server, or touching the running production collector during a session.** (Restarts are flat-account-gated, off-session, operator-run.)
- **Locking cohorts.**
- **Loosening, changing, or bypassing any frozen threshold, rule, pre-registration, or cohort** — or using empirical/outcome results to promote anything into production. Frozen means frozen.
- **Destructive operations on the operative repo or data** (force-push, hard-reset, deleting evidence/branches).
- Anything requiring Keychain, a PAT, localhost, or the flat-account proof — structurally operator-only.

If unsure whether something is inside the perimeter, treat it as gated and report.

## 4. Operating hygiene

- Work and verify **inside the operative repo with the `.venv`.** If you're in a divergent environment (no pytest, no remote, phantom branch), fix or flag that first — your commits and tests must land where the operator can see them.
- Do **not** chase the **retired option-bid basis**: empty `bid_ticks`/`outcomes` on a `NO_POINT_IN_TIME_CONTRACT` episode is expected; the MVP runs on underlying-return.
- The collector is judged by whether it **writes decision records during RTH**, not by heartbeat alone.

## 5. Deployment split (the one remaining operator action)

Take every change all the way to **deploy-ready** autonomously — built, tested-green, behavior-proven, hardened, committed, pushed. The operator performs only the final **flat-account-gated restart** to put it live — structurally operator-only (needs Keychain + a flat account + off-session timing). This is not an approval gate; it is the one physical action you cannot perform. Hand over: what it does, the green test count, the commit hash + branch, and the exact restart + verify steps.

## 6. Report when ready

- Files created/modified.
- Full `.venv` pytest pass count.
- Behavior proof (what it produces/serves, shown).
- Commit hash + branch.
- The operator deploy step (if any).
- Anything that hit a §3 boundary.
