# Signal Study Session Automation v0

## Scope

`com.scalpr.signal-studies-session-v0` runs one paper/shadow collection lifecycle on each scheduled weekday. It has no server, execution, admission, order, pre-check, or Guard path. It does not use `KeepAlive` and does not restart any process.

The LaunchAgent starts at 08:25 in the Mac's local `America/Chicago` timezone. The runner then queries Alpaca's read-only market calendar for the current date:

- Closed day or holiday: records `SKIPPED_MARKET_CLOSED`; no provider capture starts.
- Regular session: waits until the returned open, captures until the returned close, then evaluates Studies A and B.
- Early close: poll counts are shortened to the returned close.
- Duplicate launch: an OS lock and session status prevent a second run from duplicating accrual.

## Session lifecycle

1. Source paper Alpaca credentials through `load_keychain_env.sh`; FlashAlpha remains Keychain-only.
2. Run `market_context_shadow_v0.py` every 60 seconds.
3. Run the existing combined FlashAlpha pin/iron-condor shadow poll every 300 seconds. This supplies `flashalpha_pin_study_v0.jsonl`, which both context capture and the signal loggers consume.
4. At the market-calendar close, run both frozen episode evaluators. Missing inputs or horizons remain explicit exclusions.
5. Write only the existing isolated study ledgers plus ignored `signal_study_session_status_v0.json`.

Do not also run the standalone FlashAlpha scanner or its combined poll manually while this job is active; that would duplicate the scanner's observations.

## Installation

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
./install_signal_studies_session_v0.sh
```

Installation registers the job but deliberately does not kick-start it. The first automatic run occurs at the next configured session. Inspect its state with:

```bash
launchctl print "gui/$(id -u)/com.scalpr.signal-studies-session-v0"
```

Logs:

- `~/Library/Logs/scalpr/signal_studies_session.out.log`
- `~/Library/Logs/scalpr/signal_studies_session.err.log`
- `signal_study_session_status_v0.json`
