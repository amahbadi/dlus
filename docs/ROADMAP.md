<h1 align="center">🗺️ Roadmap</h1>

> [!NOTE]
> Order: finish **macOS** completely (security, releases), then **Linux**.

## 📅 The next 12 months (to the end of 2027)

| | We intend to | We do not intend to |
|---|---|---|
| 🍏 macOS | keep dlus secure and current with each macOS release; raise test coverage to 80 %+; more LG TV models tested | a Mac App Store or iPad version |
| 🐧 Linux | a `.deb` for Ubuntu / Debian with install tests in CI (section 2) | other package formats before the `.deb` is solid |
| 🔒 Security | keep every scanner green, answer reports within the times in SECURITY.md, OpenSSF Silver | accounts, cloud services or telemetry |
| 👥 Project | find a second reviewer / maintainer | |


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

> [!TIP]
> **Want to help?** The full design, every macOS → Linux replacement and the package layout are in **[docs/LINUX.md](LINUX.md)**. Progress is tracked in **[#21](https://github.com/amahbadi/dlus/issues/21)**; each task below is one issue and one pull request.

- [ ] [#11](https://github.com/amahbadi/dlus/issues/11) platform helpers: notifications (`notify-send`), staying awake (`systemd-inhibit`), quarantine, reveal / open (`xdg-open`) - *good first issue*
- [ ] [#12](https://github.com/amahbadi/dlus/issues/12) network information with `ip -j` / `nmcli` instead of `ifconfig` / `route` / `scutil`
- [ ] [#13](https://github.com/amahbadi/dlus/issues/13) devices on the network with `ip neigh` instead of `arp` - *good first issue*
- [ ] [#14](https://github.com/amahbadi/dlus/issues/14) video conversion with VA-API or `libx265` instead of VideoToolbox
- [ ] [#15](https://github.com/amahbadi/dlus/issues/15) `systemd --user` service and the `dlus` launcher (UI in the default browser)
- [ ] [#16](https://github.com/amahbadi/dlus/issues/16) `.deb` package: `/usr/lib/dlus`, `/usr/bin/dlus`, desktop entry, icons; `Depends: python3 (>= 3.9), curl, ffmpeg`
- [ ] [#17](https://github.com/amahbadi/dlus/issues/17) test suite in CI on Ubuntu - *good first issue*
- [ ] [#18](https://github.com/amahbadi/dlus/issues/18) install tests in clean `ubuntu:22.04`, `ubuntu:24.04` and `debian:12` containers
- [ ] [#19](https://github.com/amahbadi/dlus/issues/19) publish the `.deb` with provenance and checksums in every release; README install section
- [ ] [#20](https://github.com/amahbadi/dlus/issues/20) *(later)* router with an nftables helper, same checked-command design as on macOS
- [ ] *(later)* a signed APT repository so `apt upgrade` updates dlus; a native GTK window
