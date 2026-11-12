#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
# Builds dlus.app twice and checks that both builds are bit-for-bit identical (repeatable build).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
apphash() { (cd "$SRC/dist/dlus.app" && find . -type f | LC_ALL=C sort | xargs shasum -a 256); }
bash "$SRC/macos/build_dmg.sh" >/dev/null
first=$(apphash)
sleep 2
bash "$SRC/macos/build_dmg.sh" >/dev/null
second=$(apphash)
if [ "$first" != "$second" ]; then
	diff <(echo "$first") <(echo "$second") || true
	echo "dlus.app differs between two builds"
	exit 1
fi
echo "repeatable: dlus.app is bit-for-bit identical in two builds ($(echo "$first" | shasum -a 256 | cut -c1-16))"
