#!/bin/bash
# Installs the dlus VPN-router helper. Run once:  sudo bash <dlus>/gateway/install.sh (dlus shows the exact command)
# - copies dlus-gw to /Library/PrivilegedHelperTools (a root-only folder, so no other program can swap the file)
# - allows YOUR user to run exactly that one file with sudo, nothing else
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run it with sudo: sudo bash $0"; exit 1; }
U="${SUDO_USER:-${1:-}}"  # from Terminal: sudo sets SUDO_USER; from the dlus panel: the user name is passed
[[ $U =~ ^[a-z_][a-z0-9_.-]{0,31}$ && $U != root ]] && id "$U" >/dev/null 2>&1 || { echo "run it with sudo from your own account"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR=/Library/PrivilegedHelperTools
DEST=$DEST_DIR/dlus-gw
OLD=/usr/local/libexec/dlus-gw  # helper v1-v2 location
SRC="$DIR/dlus-gw"
[ -f "$SRC" ] && [ ! -L "$SRC" ] || { echo "dlus-gw is missing next to this script"; exit 1; }
head -3 "$SRC" | grep -q 'dlus-gw - root helper' || { echo "$SRC is not the dlus helper"; exit 1; }
mkdir -p "$DEST_DIR"
chown root:wheel "$DEST_DIR"
chmod go-w "$DEST_DIR"
# every folder above the helper must be root-owned and writable by root only
d=$DEST_DIR
while [ "$d" != / ]; do
	[ "$(stat -f '%u' "$d")" = 0 ] && [ $(( 0$(stat -f '%Lp' "$d") & 022 )) = 0 ] || { echo "unsafe folder $d (not root-only) - not installing"; exit 1; }
	d=$(dirname "$d")
done
install -o root -g wheel -m 755 "$SRC" "$DEST"
TMP=$(mktemp)
echo "$U ALL=(root) NOPASSWD: $DEST" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -o root -g wheel -m 440 "$TMP" /etc/sudoers.d/dlus-gw
rm -f "$TMP"
# remove the older helper location
if [ -f "$OLD" ] && head -3 "$OLD" | grep -q 'dlus-gw - root helper'; then
	rm -f "$OLD"
	rmdir /usr/local/libexec 2>/dev/null || true
fi
echo "installed. checking the firewall rules:"
"$DEST" selftest
echo "done - $("$DEST" version). Turn on the VPN router in dlus."
