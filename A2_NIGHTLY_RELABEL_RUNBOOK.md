# A2 Nightly Dense Re-label Runbook

## Isolation decision

`scalp_server.py` imports `a2_measurement` in its post-close shadow loop.
`entry_bid_collector_v1.py` does not import it. Dense-source code therefore stays
on `feature/a2-dense-endpoint-source-v0` in the dedicated worktree:

`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel`

The operative `/Scalpr7` checkout remains on `safety/pre-cleanup-snapshot`.

## Live data boundary

The wrapper runs code from the worktree and Python from the operative `.venv`,
but all evidence inputs and A2 outputs are absolute paths under:

`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7`

It never reads a worktree copy of an evidence file.

## Schedule

The plist label is `com.scalpr.a2-relabel-nightly`. It runs Monday through
Friday at 21:00 Mac-local time. Adjust the `Hour` and `Minute` integers in the
plist before installation if needed.

## Operator installation and first run

The first authenticated run is operator-only because macOS may prompt for
Keychain access. Choose **Always Allow** if prompted so future launchd runs can
load the Alpaca credentials non-interactively.

```bash
cp "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel/com.scalpr.a2-relabel-nightly.plist" \
  "$HOME/Library/LaunchAgents/com.scalpr.a2-relabel-nightly.plist"

launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.scalpr.a2-relabel-nightly.plist"

launchctl kickstart -k "gui/$(id -u)/com.scalpr.a2-relabel-nightly"
launchctl print "gui/$(id -u)/com.scalpr.a2-relabel-nightly" | \
  grep -E "state|last exit|runs|program"
```

Confirm the first run:

```bash
tail -n 100 "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/a2_relabel_launchd.err.log"
ls -lt "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/v2_data/a2_measurement/nightly_logs" | head
tail -n 100 "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/v2_data/a2_measurement/nightly_logs/"*.out.log
```

The newest dated stdout log must contain `SUCCESS verify=PASS` and
`clean_a2_labelable_episode_count=<n>`. The matching stderr log should contain
no error. Any failure generates a macOS notification and appends to
`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/a2_relabel_alerts.log`.

Uninstall:

```bash
launchctl bootout "gui/$(id -u)/com.scalpr.a2-relabel-nightly"
```
