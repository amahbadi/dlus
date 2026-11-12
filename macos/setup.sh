#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
# Installs / updates the dlus background service (launchd agent) for this copy of dlus.app. Run by the app at every start.
set -u
RES="$(cd "$(dirname "$0")" && pwd)"  # setup.sh lives in dlus.app/Contents/Resources
LABEL=com.dlus
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/.dlus" "$HOME/Library/LaunchAgents"

say() { osascript -e "display dialog \"$1\" with title \"dlus\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null 2>&1; }

PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
	if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY=$p; break; fi
done
if [ -z "$PY" ]; then
	say "dlus needs Python 3 (built into macOS developer tools).\n\nOpen Terminal and run:\n  xcode-select --install\n\nthen open dlus again."
	exit 1
fi

cat > "$PLIST.new" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>$LABEL</string>
	<key>ProgramArguments</key>
	<array><string>$PY</string><string>-I</string><string>$RES/dlus.py</string><string>--no-open</string></array>
	<key>EnvironmentVariables</key>
	<dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>ThrottleInterval</key><integer>15</integer>
	<key>StandardOutPath</key><string>$HOME/.dlus/launchd.log</string>
	<key>StandardErrorPath</key><string>$HOME/.dlus/launchd.log</string>
</dict>
</plist>
PL

DOM="gui/$(id -u)"
loaded() { launchctl print "$DOM/$LABEL" >/dev/null 2>&1; }
start() {  # bootstrap can fail while launchd is still tearing the old copy down: retry
	for _ in 1 2 3 4 5 6 7 8 9 10; do
		loaded && return 0
		launchctl bootstrap "$DOM" "$PLIST" 2>/dev/null && return 0
		sleep 1
	done
	say "dlus could not start its background service. Open dlus again, or restart your Mac."
	return 1
}
if ! cmp -s "$PLIST.new" "$PLIST" 2>/dev/null; then  # new install, update, or the app moved
	if loaded; then
		launchctl bootout "$DOM/$LABEL" 2>/dev/null
		for _ in $(seq 1 20); do loaded || break; sleep 0.5; done  # wait until the old copy is gone
	fi
	mv -f "$PLIST.new" "$PLIST"
else
	rm -f "$PLIST.new"
fi
start || exit 1
exit 0
