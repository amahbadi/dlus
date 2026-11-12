#!/bin/bash
# Build the DMG and install it on THIS Mac (for development). Stops at the first error; the installed app
# is only replaced after the new one is built, mounted and verified.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
python3 -m py_compile "$SRC/dlus.py"
python3 "$SRC/macos/check_names.py" "$SRC/dlus.py"   # a name used but defined nowhere stops the deploy
python3 "$SRC/macos/check_js.py" "$SRC/dlus.py"
(cd "$SRC" && python3 -m unittest discover -s tests >/dev/null 2>&1) || { echo "security tests failed: python3 -m unittest discover -s tests"; exit 1; }
DMG=$(bash "$SRC/macos/build_dmg.sh" | tail -1)
[ -f "$DMG" ] || { echo "build failed: no DMG"; exit 1; }
MNT=$(mktemp -d)
hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DMG" >/dev/null
trap 'hdiutil detach "$MNT" >/dev/null 2>&1 || true' EXIT
codesign --verify --deep --strict "$MNT/dlus.app"
osascript -e 'tell application "dlus" to quit' >/dev/null 2>&1 || true
rm -rf /Applications/dlus.app.new
cp -R "$MNT/dlus.app" /Applications/dlus.app.new
rm -rf /Applications/dlus.app.old
[ -d /Applications/dlus.app ] && mv /Applications/dlus.app /Applications/dlus.app.old
mv /Applications/dlus.app.new /Applications/dlus.app
rm -rf /Applications/dlus.app.old
launchctl kickstart -k "gui/$(id -u)/com.dlus"
open /Applications/dlus.app
echo "installed $(basename "$DMG")"
