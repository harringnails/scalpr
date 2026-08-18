# Technical-Debt Cleanup — Sequenced Execution Checklist

**Status:** execution plan. Paper/shadow. **No step runs during a live study session.** Derived from the post-incident debt inventory. Do the phases in order; do not start a phase until the previous one's gate passes.

## The gate that applies to EVERY step

> **A step is DONE only when: the full `.venv` suite is green (the whole suite, never a slice) AND the server has served a full session** — heartbeat advancing every few seconds and endpoints answering *from the Mac itself* (not a sandbox probe), start to close.

"Tests pass" alone is never done — that's the exact gap that put a startup hang onto the tape. Serving is proven live, not inferred.

## Cross-cutting rules (all phases)

- **Behavior-preserving for research.** No change may alter the detector, thresholds, A2 basis definition, regime definition, or cohort state. Full-suite-green + a served session are what enforce this.
- **Commit points first.** Every logical change is its own commit on a branch with the suite green at that commit. No work on the uncommitted tree after Phase 0.
- **Flat-gated restarts only,** after the close, never on an open position. The runner stays **default-OFF** throughout.
- **In paper, around capture** — never mid-session. The current server keeps serving/accruing while this proceeds.

---

## Phase 0 — Preserve (do first, the moment today closes)

The single highest-value, lowest-risk action. Nothing else starts until this is done.

- **0.1** Re-confirm `.gitignore` excludes `*.db*`, `*.jsonl`, `*.csv`, secrets, `.tools/`, data dirs (avoid the SQLite-race / bloat we hit before).
- **0.2** **Verbatim snapshot commit** of the current working tree on a `safety/pre-cleanup-snapshot` branch. Back it up off-machine. This preserves months of work in 30 seconds with zero reorganization risk.
- **0.3** *(Lower priority, non-gating)* Reconstruct a clean integration history from `54fc119` with logical commits (A2 accrual, regime v0.1, runner policy, runner measurement, discretionary logging, collector v1.2, ops). Suite green after each. **This must never delay 0.2 or any later phase.**
- **Gate:** the snapshot commit exists and is backed up off-machine.

## Phase 1 — Stability cluster + smoke test (the incident root cause)

Kills the chain: external call pre-bind → can't serve → can't prove flat → deadlock.

- **1.1** Bind FastAPI **first**, with explicit `STARTING` / `READY` / `DEGRADED` states. Add a dependency-free `/health/live` and a readiness endpoint reporting each subsystem. **No Alpaca/SIP/historical/Cloud SQL/research call before bind.**
- **1.2** Move flat-gating **out** of server init: a small direct-Alpaca preflight for `restart.sh` with explicit connect + total timeouts, proving PAPER identity + 0 positions + 0 open orders. `/api/account-flat-proof` stays but is no longer required to recover a dead process.
- **1.3** Make `restart.sh` readiness **real**: require process alive **AND** `/health/live` answers **AND** flat-proof returns bounded JSON **AND** heartbeat advances at least once. A listening socket alone never passes.
- **1.4** Hermetic subprocess **smoke + failure-injection tests** with fake providers: require bind + flat-proof within a fixed deadline (~10s); cover missing creds, provider timeout, SIP failure, dense-source timeout, Cloud SQL failure, runner-measurement init. Each case must serve degraded health or fail fast with an exact reason.
- **Gate:** full suite green + smoke tests pass + a controlled flat-gated restart brings it up and serves a full session. (This restart also loads the already-staged **runner rewire** — confirm C.10 of the runner checklist: runner reads `regime_layer_v0`, not the legacy model. Runner stays OFF.)

## Phase 2 — Observability (pulled forward: makes the next incident diagnosable)

This is what was missing when the stack trace was unrecoverable and logs blurred three launches.

- **2.1** Per-launch **run ID**, PID file, startup-phase log, and per-run error log — so any hang is attributable to an exact phase + commit.
- **2.2** Dedicated **heartbeat file** + a **stale-heartbeat alert during market hours** (would have caught the overnight death in minutes, not hours).
- **2.3** Define one clear **liveness contract** (resolve the collector-status vs runtime-health ambiguity).
- **Gate:** a killed/hung process is attributable to an exact phase + run; stale heartbeat fires an alert. Full suite green + served session.

## Phase 3 — Runtime hardening (kill the fragile Terminal lifecycle)

- **3.1** Replace the foreground-window server with a **managed macOS service / supervised launcher** that survives window close + sleep, **while preserving the flat-account gate and preventing unsafe auto-restart around open positions.**
- **3.2** Make credential loading deterministic (not dependent on which shell sourced Keychain vars).
- **Gate:** server survives a window close and a sleep; never auto-restarts on a non-flat account; full suite green + served session.

## Phase 4 — Feature re-adds, behind the hardened startup

- **4.1** **Bound the legacy regime path:** background cache with a deadline + stale-result policy; `/api/regime` returns `UNAVAILABLE`/marked-stale instead of hanging. (Runner already reads `regime_layer_v0`; keep it OFF until this is bounded and measured.)
- **4.2** **Reintroduce dense-source A2** as a lazy background job that starts only after `READY`: bounded provider timeouts, limited retries, circuit breaker, explicit `UNAVAILABLE` fallback, provenance. **Never** in `Platform.__init__`, import, or pre-bind. The Phase-1 smoke test's dense-source-timeout case must prove it cannot block startup.
- **Gate:** dense-source failure provably cannot block startup; A2 endpoint availability rises vs tick_log; full suite green + served session.

## Phase 5 — Remaining hygiene

- **5.1** Resolve production files from the **project root**, not cwd (remove reliance on the test chdir fixture).
- **5.2** Pin + deliberately upgrade the deprecation-warning dependency.
- **5.3** Make runner-checklist Section C **config-aware** so an operator gets a literal all-pass under the safe (dense-source-reverted) configuration too.
- **Gate:** full suite green; no cwd-relative production reads; served session.

## The runner, across all of this

Default **OFF** throughout. It goes *live* (rewired to `regime_layer_v0`) at the Phase-1 restart, but does not *activate* until: the legacy path is bounded (4.1), a controlled per-position PAPER opt-in produces a **verified `regime_flow_runner_shadow_v0.jsonl` row**, and the operator deliberately flips the default. Per `SYNC_RESTART_AND_HOLD_THE_RUNNER_CHECKLIST.md` §D.

## Completion criteria

Clean reproducible history; full suite green at every logical commit; **no external call before bind**; restart readiness is endpoint- and heartbeat-based; dense-source failure cannot block startup; legacy regime calls are bounded; and the runner remains measured and default-OFF.
