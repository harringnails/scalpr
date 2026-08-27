# A2 Nightly Dense Re-label Runbook

## Isolation decision

`scalp_server.py` imports `a2_measurement` in its post-close shadow loop.
`entry_bid_collector_v1.py` does not import it. Dense-source code therefore stays
on `feature/a2-dense-endpoint-source-v0` in the dedicated worktree:

`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel`

The operative `/Scalpr7` checkout remains on `safety/pre-cleanup-snapshot`.

This is Route A. launchd invokes the operative `.venv` Python directly and
opens the job code in the isolated worktree. Route B was rejected because the
dense branch changes `a2_measurement.py`, which `scalp_server.py` imports in its
post-close loop. Putting that module on the safety checkout would change
server-loaded code and require the flat-gated restart path.

## Live data boundary

The launchd job runs Python code from the worktree with the operative `.venv`,
but all evidence inputs and dense A2 outputs are absolute paths under:

`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7`

It never reads a worktree copy of an evidence file. Dense output is isolated at:

- `v2_data/a2_measurement/a2_labels_dense_v0.jsonl`
- `v2_data/a2_measurement/a2_summary_dense_v0.json`
- `v2_data/a2_measurement/a2_dense_source_comparison_v0.json`

The live server retains `a2_labels_v2.jsonl` and `a2_summary_v2.json` as the
legacy `live_tick_log` cross-check. It does not write any dense path. The dense
summary is the authoritative source for clean A2 accrual and the future edge
harness; `post_close_audit.py` fails closed rather than substituting legacy data.

## Schedule

The plist label is `com.scalpr.a2-relabel-nightly`. It runs Monday through
Friday at 21:00 Mac-local time. Adjust the `Hour` and `Minute` integers in the
plist before installation if needed.

## Operator grants

Both grants are operator-only.

1. In **System Settings > Privacy & Security > Full Disk Access**, click `+`,
   press `Command-Shift-G`, and add the resolved interpreter:
   `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14`.
   Toggle it on. The plist invokes the operative `.venv/bin/python` symlink,
   which resolves to this binary. This is a broad grant for that shared Python
   interpreter, not just this job.
2. On the first calendar-triggered run, macOS may ask whether `/usr/bin/security`
   can read `scalpr.alpaca.paper.key` and `scalpr.alpaca.paper.secret`. Choose
   **Always Allow** for both. The Python job reads those exact items directly,
   ignores inherited Alpaca environment variables, and never logs their values.

## Install

```bash
launchctl bootout "gui/$(id -u)/com.scalpr.a2-relabel-nightly" 2>/dev/null || true

cp "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel/com.scalpr.a2-relabel-nightly.plist" \
  "$HOME/Library/LaunchAgents/com.scalpr.a2-relabel-nightly.plist"

launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.scalpr.a2-relabel-nightly.plist"
```

Do not use `launchctl kickstart` as deployment proof. It can inherit the caller's
session permissions.

## Genuine scheduled proof

Use the installed plist for two temporary calendar triggers. The first grants
Keychain ACL access if prompted. The second must complete with no interaction.

```bash
PLIST="$HOME/Library/LaunchAgents/com.scalpr.a2-relabel-nightly.plist"
LABEL="gui/$(id -u)/com.scalpr.a2-relabel-nightly"

schedule_proof_two_minutes_from_now() {
  local when hour minute
  when="$(date -v+2M '+%H:%M')"
  hour="$((10#${when%:*}))"
  minute="$((10#${when#*:}))"
  /usr/libexec/PlistBuddy -c "Delete :StartCalendarInterval" "$PLIST"
  /usr/libexec/PlistBuddy -c "Add :StartCalendarInterval dict" "$PLIST"
  /usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:Hour integer $hour" "$PLIST"
  /usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:Minute integer $minute" "$PLIST"
  printf 'scheduled for %02d:%02d local time\n' "$hour" "$minute"
}

# Scheduled run 1: grant Always Allow if Keychain prompts.
launchctl bootout "$LABEL" 2>/dev/null || true
schedule_proof_two_minutes_from_now
launchctl bootstrap "gui/$(id -u)" "$PLIST"
# Wait until the scheduled minute has completed before continuing.

# Scheduled run 2: do not interact with the Mac while this run executes.
launchctl bootout "$LABEL"
schedule_proof_two_minutes_from_now
launchctl bootstrap "gui/$(id -u)" "$PLIST"
# Wait until the scheduled minute has completed, then collect proof.

launchctl print "$LABEL" | grep -E "state|last exit|runs|program"
LATEST_OUT="$(ls -t "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/v2_data/a2_measurement/nightly_logs/"*.out.log | head -1)"
LATEST_ERR="${LATEST_OUT/.out.log/.err.log}"
grep -E "launchd_service=com.scalpr.a2-relabel-nightly|credential_source=macos_keychain_security_cli|SUCCESS verify=PASS" "$LATEST_OUT"
test ! -s "$LATEST_ERR"

"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel/a2_dense_source.py" verify \
  --labels "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/v2_data/a2_measurement/a2_labels_dense_v0.jsonl" \
  --comparison "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/v2_data/a2_measurement/a2_dense_source_comparison_v0.json"
```

Required proof is `last exit code = 0`, all three log markers above, an empty
dated stderr log, and dense verification `status = PASS`. Any failure generates
a macOS notification and appends to
`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/a2_relabel_alerts.log`.

Confirm the dense summary reports
`endpoint_source=alpaca_historical_stock_quote_v1` and a nonzero
`clean_a2_labelable_episode_count`. A freshly written legacy `a2_summary_v2.json`
must not change any of the three dense files.

Restore the committed weekday schedule after capturing proof:

```bash
launchctl bootout "$LABEL"
cp "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel/com.scalpr.a2-relabel-nightly.plist" "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
```

Uninstall:

```bash
launchctl bootout "gui/$(id -u)/com.scalpr.a2-relabel-nightly"
```
