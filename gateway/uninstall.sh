#!/bin/bash
# Removes the dlus VPN-router helper and its firewall rules:  sudo bash <dlus>/gateway/uninstall.sh
set -u
[ "$(id -u)" = 0 ] || { echo "run it with sudo: sudo bash $0"; exit 1; }
for h in /Library/PrivilegedHelperTools/dlus-gw /usr/local/libexec/dlus-gw; do
	[ -x "$h" ] && head -3 "$h" | grep -q 'dlus-gw - root helper' && "$h" disable && rm -f "$h"
done
rm -f /etc/sudoers.d/dlus-gw
echo "removed. Devices you pointed at this Mac need their network set back to automatic."
