<h1 align="center">🗺️ Roadmap</h1>

> [!NOTE]
> Order: finish **macOS** completely (security, releases), then **Linux**.

## 🍏 1. macOS - close out

- [x] Security review and fixes (7.23): device approval, activity log, checksums + Verify, quarantine, https-only redirects, root helper in a root-only folder, only listed devices may route, TV certificate pinning, playlist-disguise guard, zip-bomb limits, safe shared folders
- [x] Security pipeline: Bandit, Semgrep, CodeQL, Gitleaks, ShellCheck, security tests, pf rule check on macOS; pull-request feedback comment + labels; pinned actions + Dependabot
- [x] OpenSSF Scorecard (automatic rating + badge)
- [x] Signed build provenance, Sigstore bundle, in-toto provenance and `SHA256SUMS` on every release
- [x] Fuzzing (Atheris) of every parser that reads untrusted input, in the Security workflow
- [x] No known-vulnerable packages in the pipeline (semgrep from a pinned container image)
- [ ] OpenSSF Best Practices badge - a self-assessment form at [bestpractices.dev](https://www.bestpractices.dev), filled in by the maintainer (sign in with GitHub)
- [ ] Developer ID signing + Apple notarization: removes the "unidentified developer" warning. Needs a paid Apple Developer Program membership (maintainer decision); then the release workflow signs and notarizes with secrets
- [ ] ~~Mac App Store~~ - **not planned** (maintainer decision), see below
- [ ] ~~iPad / iPhone~~ - **not planned**, see below

### 🛒 Why not the Mac App Store

The Mac App Store requires every app to run in the App Sandbox and follow the App Review Guidelines. dlus breaks several rules by design:

| Rule | dlus today |
|---|---|
| Sandbox: no root, no helper tools outside the app, no system firewall (2.4.5) | the VPN router needs a root helper and `pf` |
| Sandbox: no starting arbitrary programs | dlus runs `curl`, `ffmpeg` and the system Python |
| Background services must use `SMAppService` from inside the bundle | dlus installs a LaunchAgent plist |
| Apps must be self-contained (2.4.5, 2.5.2) | needs Homebrew ffmpeg and Python 3 from the developer tools |
| No downloading media from third-party sources without permission (5.2.3) | subtitle search scrapes a third-party site |

A store version would be a different, reduced app (no VPN router, bundled ffmpeg, no subtitle scraping, Swift or bundled Python). Possible later as "dlus Lite"; the DMG stays the full version.

### 📱 Why not iPad / iPhone

dlus is a server: it downloads, converts and streams from the Mac. An iPad cannot keep a server running in the background, and the iPad already uses dlus today - open the stream links or the TV home page in Safari, or add it to the home screen.

## 🐧 2. Linux (after macOS is closed)

Goal: `dlus_<version>_all.deb` for Ubuntu 22.04+ / Debian 12+, built and tested in the pipeline, published with every release.

- [ ] Make the core platform-neutral: replace macOS-only calls (`osascript` notifications, `caffeinate`, `xattr` quarantine, `ifconfig`/`netstat`/`arp` parsing, `networksetup`, VideoToolbox) with Linux equivalents behind small helpers (`notify-send`, `systemd-inhibit`, `ip`, VA-API / software encode)
- [ ] Window: open the UI in the default browser (or a small GTK/WebKit window later); Mac-only features (VPN router, Mac player window) hidden on Linux
- [ ] Service: a `systemd --user` unit instead of the LaunchAgent
- [ ] Package: `.deb` with `Depends: python3 (>= 3.9), curl, ffmpeg`, files under `/usr/lib/dlus`, launcher `/usr/bin/dlus`, desktop entry and icons; `postinst` enables the user service
- [ ] VPN router on Linux: `nftables` + IP forwarding helper with the same checked-command design (polkit / sudoers) - separate milestone
- [ ] Pipeline: build the `.deb` on `ubuntu-latest`, `lintian`, install it in clean `ubuntu:22.04`, `ubuntu:24.04` and `debian:12` containers, start the service, call `/api/health`, run the security tests on Linux
- [ ] Release: attach the `.deb` + provenance + checksum; README install section for Ubuntu / Debian
- [ ] Optional: an APT repository (signed) so `apt upgrade` updates dlus
