#!/bin/bash
# Removes the dlus background service (and, if installed, the VPN-router helper). Your downloads and ~/.dlus stay.
launchctl bootout "gui/$(id -u)/com.dlus" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.dlus.plist"
if [ -x /Library/PrivilegedHelperTools/dlus-gw ] || [ -x /usr/local/libexec/dlus-gw ]; then
	echo "Removing the VPN-router helper (asks for your Mac password)..."
	sudo bash /Applications/dlus.app/Contents/Resources/gateway/uninstall.sh
fi
echo "dlus is stopped and will not start again. Drag dlus from Applications to the Trash to remove the app."
echo "Your settings and download list are in ~/.dlus (delete it to remove everything)."
