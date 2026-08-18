# Discretionary Override Capture — Live Integration Directive (for Codex)

**Status: integration directive. Paper/shadow, advisory-only.** Wires `discretionary_override_log_v0.capture_decision_record` into the operator decision surface so decisions are logged automatically. **The capture is a pure side-effect: it records only, and must never block, delay, gate, size, or alter any order or Guard path.** No change to precheck, detector, regime, admission, or Guard logic.

## Non-negotiable guardrails

- **Fail-safe.** Wrap every capture call in `try/except`; on any error, log a warning and continue. A logging failure must never raise into `open_trade`, the fill path, or Guard creation. Capture is best-effort telemetry, not a dependency.
- **Never precede the Guard.** The existing safety ordering (Guard created immediately after a broker-confirmed fill, ~scalp_server.py:818) is untouched. Capture runs **after** the Guard is created, off the critical path.
- **No authority.** No order/sizing/gate influence. Advisory-only; the module already stamps `execution_authority=false`.
- **Idempotent.** One `DECISION` row per fill. Key the capture to the trade/fill id so a retry, reconnect, or duplicate call does not double-log.

## Primary — auto-capture TOOK at manual fill (zero operator burden, unbiased)

Instrument the successful `manual=True` branch of `open_trade` (scalp_server.py:675), **after** the fill is confirmed and the Guard is created:

- `took = True`.
- `precheck_snapshot = entry_signals` — the `run_precheck()` snapshot already captured at fill time (may be `None` → the module records precheck `MISSING` → `NO_READ_TOOK`).
- `implied_direction` from the contract right (CALL → bullish, PUT → bearish); reuse `precheck.implied_direction`.
- **Derive `followed` — do not ask the operator.** Compare the precheck's directional headline to the trade direction:
  - precheck decision **supports** the taken direction (YES) → `followed=True` → `FOLLOWED_TOOK`.
  - precheck decision **opposes** it (NO) → `followed=False` → `OVERRODE_TOOK`.
  - precheck `NO READ`/missing → `NO_READ_TOOK`.
- `minute_bars` / `now_minute`: pass the bars available at decision time so the regime tag is computed as-of the fill.
- `operator_rationale`: optional; leave `None` unless a manual note is supplied.

This fully powers the **primary hypothesis** (mean `signed_return_60m` of `OVERRODE_TOOK` vs the matched null) with no operator effort and no selection bias — every manual fill is logged, period.

## Secondary — optional skip control (SKIPPED cells, operator-driven)

A "skip" has no fill event to hook, so capturing `FOLLOWED_SKIPPED` / `OVERRODE_SKIPPED` requires an explicit operator action. Add a minimal decision-surface control ("log skip" with the direction considered) that calls `capture_decision_record` with `took=False`.

- **Flag this data honestly as operator-driven and secondary.** Skip-logging depends on the operator remembering to log, so the skip cells carry survivorship risk the taken cells do not. The primary override-edge test **does not depend on skip data**; skips only enrich the fuller "did I correctly avoid?" picture.
- Do not synthesize skips or infer them. Absent an explicit skip action, no skip row.

## Later (not in this task)

Outcome labeling stays a separate, deferred step: `label_outcome` appends an `OUTCOME_LABEL` revision once the A2 horizon completes — never at capture time, never operator-edited. The pre-registered override-vs-null analysis is still unbuilt and remains out of scope here.

## Tests (run in `.venv`)

- A successful manual fill appends exactly one `DECISION` row; a second call for the same fill id does not duplicate.
- `followed`/`overrode`/`no-read` derivation is correct for precheck YES / NO / NO-READ against both CALL and PUT.
- **Fault injection:** force `capture_decision_record` to raise; assert `open_trade` still completes, the order/fill/Guard path is unaffected, and the error is logged not raised.
- Capture runs after Guard creation (ordering assertion).
- `entry_signals=None` → `NO_READ_TOOK`, precheck `MISSING` (not imputed).
