#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
# Builds dist/dlus.app and dist/dlus-<version>.dmg from ~/Desktop/dlus.   bash macos/build_dmg.sh
set -eu
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$SRC/dist"; WORK="$OUT/work"
VER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/dlus.py" | head -1)
rm -rf "$WORK" "$OUT/dlus.app"; mkdir -p "$WORK"
# app icon: the committed macos/AppIcon.icns; only re-rendered (headless Chrome) when it is missing
if [ -f "$SRC/macos/AppIcon.icns" ]; then cp "$SRC/macos/AppIcon.icns" "$WORK/AppIcon.icns"; else
CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
printf '<html><body style="margin:0;background:transparent">%s</body></html>' "$(cat "$SRC/macos/icon.svg")" > "$WORK/icon.html"
"$CH" --headless=new --mute-audio --disable-gpu --hide-scrollbars --default-background-color=00000000 --window-size=1024,1024 \
	--screenshot="$WORK/icon-1024.png" "file://$WORK/icon.html" >/dev/null 2>&1
IS="$WORK/AppIcon.iconset"; mkdir -p "$IS"
for s in 16 32 128 256 512; do
	sips -z $s $s "$WORK/icon-1024.png" --out "$IS/icon_${s}x${s}.png" >/dev/null
	sips -z $((s*2)) $((s*2)) "$WORK/icon-1024.png" --out "$IS/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$IS" -o "$WORK/AppIcon.icns"
fi
# the app
APP="$OUT/dlus.app"; C="$APP/Contents"
mkdir -p "$C/MacOS" "$C/Resources"
# native window (Swift + WebKit), one binary for Apple silicon and Intel
for a in arm64 x86_64; do
	# honours SWIFTC (compiler) and SWIFTFLAGS / LDFLAGS (extra flags are added, never replaced)
	# shellcheck disable=SC2086
	"${SWIFTC:-swiftc}" -O -target "$a-apple-macos12.0" ${SWIFTFLAGS:-} ${LDFLAGS:+-Xlinker $LDFLAGS} -o "$WORK/dlus-$a" "$SRC/macos/main.swift"
done
lipo -create "$WORK/dlus-arm64" "$WORK/dlus-x86_64" -output "$C/MacOS/dlus"
cp "$SRC/macos/setup.sh" "$C/Resources/setup.sh"; chmod 755 "$C/Resources/setup.sh"
cp "$SRC/dlus.py" "$C/Resources/"; cp -R "$SRC/icons" "$SRC/gateway" "$C/Resources/"
rm -f "$C/Resources/gateway/"*.v1
cp "$WORK/AppIcon.icns" "$C/Resources/"
cat > "$C/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
	<key>CFBundleName</key><string>dlus</string>
	<key>CFBundleDisplayName</key><string>dlus</string>
	<key>CFBundleIdentifier</key><string>app.dlus</string>
	<key>CFBundleExecutable</key><string>dlus</string>
	<key>CFBundleIconFile</key><string>AppIcon</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>$VER</string>
	<key>CFBundleVersion</key><string>$VER</string>
	<key>LSMinimumSystemVersion</key><string>12.0</string>
	<key>LSApplicationCategoryType</key><string>public.app-category.video</string>
	<key>NSHighResolutionCapable</key><true/>
	<key>NSLocalNetworkUsageDescription</key><string>dlus finds your TV on your home network and shows its apps and inputs.</string>
	<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
	<key>NSHumanReadableCopyright</key><string>dlus - downloader for us</string>
</dict></plist>
PL
xattr -cr "$APP"
codesign --force --deep --options runtime --sign - "$APP"   # local (ad-hoc) signature, hardened runtime
# the disk image: app + Applications shortcut + uninstaller
ST="$WORK/stage"; mkdir -p "$ST"
cp -R "$APP" "$ST/"; ln -s /Applications "$ST/Applications"
cp "$SRC/macos/uninstall.command" "$ST/Uninstall dlus.command"
printf 'dlus %s - downloader for us\n\n1. Drag dlus onto Applications.\n2. Open dlus from Applications (first time: right-click -> Open).\n   Downloads and TV streaming keep running in the background, also when the window is closed.\n   To stop everything: dlus menu -> Stop dlus Completely.\n\nNeeds Python 3 (xcode-select --install) and curl; ffmpeg (brew install ffmpeg) for TV streaming.\n' "$VER" > "$ST/Read me.txt"
DMG="$OUT/dlus-$VER.dmg"; rm -f "$DMG"
hdiutil create -volname "dlus $VER" -srcfolder "$ST" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$WORK"
echo "$DMG"
