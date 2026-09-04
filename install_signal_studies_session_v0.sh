#!/bin/bash
# Install the session-scoped shadow-study LaunchAgent. It does not run the job immediately.
set -eu

ROOT="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
SOURCE="$ROOT/com.scalpr.signal-studies-session-v0.plist"
TARGET="$HOME/Library/LaunchAgents/com.scalpr.signal-studies-session-v0.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/scalpr"
plutil -lint "$SOURCE"
cp "$SOURCE" "$TARGET"
launchctl bootout "$DOMAIN/com.scalpr.signal-studies-session-v0" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/com.scalpr.signal-studies-session-v0"
launchctl print "$DOMAIN/com.scalpr.signal-studies-session-v0"
