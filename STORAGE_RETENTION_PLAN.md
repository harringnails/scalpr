# Storage, Retention & Data/Code Separation Plan (`storage-hygiene-v1`)

**Status: draft plan for Codex. Ops/reproducibility only — no trading-logic change.** Addresses audit steps 1–2 (git baseline, data/code separation, disk protection). Nothing here deletes evidence or touches frozen cohorts or the Guard.

## Objectives
1. Separate generated evidence from source code so the repo is a clean, reviewable, reproducible artifact.
2. Preserve append-only integrity and frozen-cohort reproducibility **byte-exactly** through the move.
3. Bound disk growth with retention/archival rules and early alerts.
4. Enable a clean git baseline and a single reproducible test command.

## Non-negotiable principles
1. **Evidence is immutable and append-only.** Every move is byte-preserving and hash-verified. Deletion happens only via recorded, archived, reconciled retention — **never silent mutation** (same rule as the flow ledgers' sealed-partition policy).
2. **Frozen cohorts/hashes untouched.** The lock registry (`entry_intelligence_cohort_locks_v1.jsonl`), incubation `3425809…`, `scalpr-intel-v0`, wave versions, and every file they reference keep identical content.
3. **Change only with the server stopped and the paper account flat** (`holdings == 0`, reusing the existing restart guard's check).
4. **One authoritative data root, config-driven.** All writers resolve paths from a single `SCALPR_DATA_DIR` (one source of truth) — no module hardcodes a relative filename.
5. **Secrets never in the repo or the data dir.** Keychain + `load_keychain_env.sh` stay as they are.

## Target layout

Repo (`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/`) holds **only**: `*.py`, `test_*.py`, `*.md`, `config/`, `requirements*`, `restart*.sh`. All runtime evidence moves under a git-ignored data root:

```
$SCALPR_DATA_DIR         (default: ~/ScalprData/, OUTSIDE the repo)
  evidence/              append-only authoritative jsonl
                         guard_events_v0.jsonl, wave_exits_v0.jsonl,
                         wave_observations_v0.jsonl, scalpr_intel_feature_snapshots_v0.jsonl,
                         entry_policy_prototype_v0_*.jsonl, entry_intelligence_*.jsonl
  cohorts/               frozen cohort JSON + entry_intelligence_cohort_locks_v1.jsonl (byte-preserved)
  journals/             scalp_journal.csv
  ticks/                tick_log.csv (rotating)
  snapshots/            research_snapshots/, incubation_snapshots/, incubation_paths/, session snapshots
  telemetry/            incubation_telemetry/, wave_obs_streams/, wave_positions/, wave_seeds/, premarket_shadow/
  runs/                 cached UW workups (prunable cache)
  logs/                 scalpr_server.log, scalpr_autorestart.log (rotating)
  archive/              compressed sealed partitions (cold storage)
  manifests/            move + reconciliation manifests
```

## Data classification & retention

| Class | Examples | Authoritative | Hot retention | Deletion rule |
|---|---|---|---|---|
| Frozen/authoritative evidence | `*_v0/v1.jsonl` append logs, cohort locks, `scalp_journal.csv` | **Yes** | keep hot ≥ 90 days / current studies | only after archived **and** hash-reconciled; recorded, never silent |
| Immutable snapshots | research/incubation/session snapshots | **Yes** | until the owning cohort closes | sealed-partition archive only |
| Raw ticks | `tick_log.csv` | Semi (raw feed) | rotate daily, gzip, keep ≥ 30 days hot | prune compressed past window, recorded |
| Runs cache | `runs/*.json` workups | No (cache) | latest per ticker + ≤ 14 days | freely prunable |
| Logs | server / autorestart logs | No | size+time rotate, keep tail | rotate/prune |

Retention windows are defaults — set the exact numbers in `config/scalpr.yaml` so they're versioned, not hardcoded.

## `.gitignore` scheme

Ensure the repo ignores (superset of the existing list): `scalp.keys`, `*.keys`, `.env`, `.env.*`, the entire `ScalprData/` root (or `$SCALPR_DATA_DIR`), `*.db`, `*.jsonl`, `*.csv`, `research_snapshots/`, `incubation_*/`, `wave_obs_streams/`, `wave_positions/`, `wave_seeds/`, `premarket_shadow/`, `runs/`, `*.log`, `__pycache__/`, `Scalpr7 Compressed*`. If any test needs a small fixture file of an ignored type, un-ignore that exact path explicitly (`!tests/fixtures/x.jsonl`).

## Migration procedure (byte-safe, hash-verified)

1. **Stop server; confirm flat** (`holdings == 0`).
2. **Pre-move manifest:** for every evidence file, record `path, size, sha256` → `manifests/manifest_before.json`.
3. **Introduce `SCALPR_DATA_DIR`** and route every writer/reader through a single path resolver. Transitional safety net: leave symlinks at the old locations so nothing breaks if a path was missed.
4. **Move** files with `mv` (preserves bytes) into the target layout.
5. **Post-move manifest:** recompute `sha256`; assert **every file is byte-identical** to `manifest_before`. Any mismatch → **abort and roll back** (symlinks make this safe).
6. **Cohort integrity check:** confirm the lock registry and every referenced cohort file hash-match pre-move.
7. **Restart; smoke test:** a new append lands in the new path; the 33 suites pass.
8. **Clean baseline commit:** repo now contains zero evidence; `git status` clean.

## Disk protection

- **Alerts:** warn at **15%** free, critical at **10%** free — emit to `scalpr_autorestart.log`, the dashboard health panel, and (optional) a notification. Add a daily disk check to the scheduled task.
- **Hard floor (~5%) — fail-closed, but safety-first:** pause **new research collection** (Entry Intelligence capture, shadow observers) to preserve headroom, and alert loudly. **Never** block Guard exits or execution safety on disk — protective writes must always be attempted; the floor throttles *new* append-heavy collection, not position protection.
- **Structural fix that matters most:** moving evidence off the repo volume + rotation/retention keeps headroom so the floor is rarely approached. A full disk breaks *all* appends (including `guard_events`), so early headroom is the real mitigation.

## Git baseline + reproducible tests (supports audit step 1)

- After separation, make **one clean baseline commit** (review the ~93 dirty/untracked paths first; most should now be git-ignored data).
- **One canonical test command** (e.g., `make test` or `./run_tests.sh`) that runs all 33 suites; either unify the two Python environments or document/scripted-activate both behind that command.
- **CI:** a pre-deploy check that runs the same suites and blocks deploy on failure.

## Acceptance checklist

- Repo contains zero evidence files; `$SCALPR_DATA_DIR` fully git-ignored; `git status` clean.
- `manifest_before == manifest_after` (byte-identical moves); rollback tested.
- Frozen cohort lock registry + referenced files hash-unchanged.
- Append works post-move (new record appears under the new path).
- Disk alerts fire at 15% / 10% in a forced test; hard floor pauses only new collection, never Guard/execution.
- All 33 suites pass via the single command; CI blocks on failure.
- Secrets absent from repo and data root.

## Out of scope / cautions (this pass)

- **No deletion of evidence** — this pass does separation, alerts, and retention *rules* only. Actual pruning happens later, recorded, after archive + reconciliation.
- **No change** to frozen cohort content, the Guard, or any trading logic.
- **No secrets** relocated into the data dir; keychain stays authoritative.
- Cloud SQL mirror: local append-only files remain authoritative until row-count + hash reconciliation proves the mirror complete (per the audit).
