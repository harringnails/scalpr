# Scalpr Risk Controls

## Current controls

- Paper account is the operational default.
- Localhost binding at `127.0.0.1`.
- Market-closed check before entry, though it currently fails open on lookup failure.
- OCC option-symbol validation.
- Whole-position ratchet with grace period, confirmation reads, and optional stall exit.
- Wave/Incubation experiments are feature-gated, exception-isolated, and shadow-only.
- Wave live order adapter raises instead of placing an order.

## Current critical gaps

- Guard state is in memory and disappears on restart.
- No separate catastrophic hard stop or broker-side protective order.
- Option orders use market orders.
- No account-level exposure or loss limits.
- No stale/crossed/wide-market execution gate.
- No persistent order state machine, idempotency, or partial-fill reconciliation.
- Pause deliberately removes protection; pause/resume events are not yet persisted by the endpoints.
- No request authentication or CSRF protection.
- Real-money mode can be selected with a command-line flag and typed phrase.

## V2 paper/shadow controls required at foundation

- Compile/runtime exclusion of live broker configuration.
- Explicit expected paper account ID and startup verification.
- Persistent positions, intents, orders, fills, Guard state, and audit events.
- Fail-closed market status and quote-quality gates.
- Maximum allocation, open positions, daily loss, symbol concentration, and pending notional.
- Deterministic emergency-exit path that is never blocked by grace or ordinary ratchet logic.
- Idempotent client order IDs and restart reconciliation.
- Origin/session protection for state-changing requests.
- Visible PAPER/SHADOW identity in every operator surface.

No V2 control authorizes live use. Live-money requirements will be designed only under a later explicit decision.

