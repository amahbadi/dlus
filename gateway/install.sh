#!/bin/bash
# Installs the dlus VPN-router helper. Run once:  sudo bash <dlus>/gateway/install.sh (dlus shows the exact command)
# - copies dlus-gw to /usr/local/libexec (owned by root, so dlus itself cannot change it)
# - allows YOUR user to run exactly that one file with sudo, nothing else
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run it with sudo: sudo bash $0"; exit 1; }
U="${SUDO_USER:-${1:-}}"  # from Terminal: sudo sets SUDO_USER; from the dlus panel: the user name is passed
[[ $U =~ ^[a-z_][a-z0-9_.-]{0,31}$ && $U != root ]] && id "$U" >/dev/null 2>&1 || { echo "run it with sudo from your own account"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p /usr/local/libexec
install -o root -g wheel -m 755 "$DIR/dlus-gw" /usr/local/libexec/dlus-gw
TMP=$(mktemp)
echo "$U ALL=(root) NOPASSWD: /usr/local/libexec/dlus-gw" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -o root -g wheel -m 440 "$TMP" /etc/sudoers.d/dlus-gw
rm -f "$TMP"
echo "installed. checking the firewall rules:"
/usr/local/libexec/dlus-gw selftest
echo "done - $(/usr/local/libexec/dlus-gw version). Turn on the VPN router in dlus."
