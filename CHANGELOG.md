<h1 align="center">📝 Changelog</h1>

<p align="center">Every release, newest first. Downloads: <a href="https://github.com/amahbadi/dlus/releases">Releases</a>. Version numbers are <code>major.minor</code>; every release has its own tag <code>v&lt;version&gt;</code>.</p>

> [!NOTE]
> Security fixes are marked 🔒. dlus has had no publicly known vulnerabilities so far; the fixes below were found by our own review and scanners.

## Unreleased

- 🐛 Proxy: a local web server is no longer mistaken for a VPN app's proxy (only a real proxy answer counts)
- 🐛 Proxy: a malformed CONNECT request is answered with 400 instead of crashing its connection handler
- 🐛 Styled subtitles (LG player): a line could start up to a second early when its time rounded up (same fix as 7.27, for the styled format)
- 🧪 Test suite for the download engine, the local API, the home-network server and media (ffmpeg): coverage 17 % → 76 % (incl. a fake LG TV, a fake DLNA TV, fake VPN-app proxies and the proxy end to end); coverage badge
- 🔔 A device asking for access is named by its network name (e.g. *iPad*, *PS5*) instead of "Unknown device" when dlus knows it
- 🧪 Tests never send real macOS notifications

## 7.27 - 2026-11-12

- 🔔 Notifications show the dlus logo and open dlus when clicked (sent through the app; macOS asks once to allow them)
- 🐛 Subtitles on the TV: a line could start up to one second early when its time rounded up to a full second (milliseconds now carry into the seconds)
- 🧪 Regression tests for every fix since the first public release ([tests/test_regressions.py](tests/test_regressions.py))
- 🤖 Coding style enforced in CI: full PEP 8 for Python (ruff) and `swift format` for the Swift window; code cleaned up to pass both (no behaviour change)

## 7.26 - 2026-11-12

- 🔒 Credentials (TV code, proxy password, TV pairing keys and certificates) moved out of `settings.json` into their own `secrets.json` (mode 600); existing setups are moved automatically and old backups cleaned
- ⚡ TV streams start faster: an unused ffmpeg probe on every start was removed
- 🤖 Repeatable build checked in CI (dlus.app bit-for-bit identical in two builds); coding standard enforced with ruff; DCO sign-off check on pull requests
- 📖 Governance, Code of Conduct, architecture, assurance case, one-year roadmap, `make setup` + Brewfile, license headers in every source file

## 7.25 - 2026-11-12

- 🔒 Every file the network can ask for (icons, fonts, shared folders, the file browser, TV streams) goes through one checked path function that can never leave its folder
- 🔒 LG TV connection requires TLS 1.2 or newer
- 🔒 Remaining request values in log lines sanitized; redirect header cannot carry line breaks
- 🤖 New tests and a fuzz property for the path check; CodeQL findings closed in code

## 7.24 - 2026-11-12

- 🔒 Redirect after unlocking the window can only stay on the dlus page (never another site)
- 🔒 Every log entry stays on one line (values from the network cannot forge log lines)
- 🔒 Releases carry a Sigstore signature bundle and SLSA build provenance next to the DMG
- 🤖 Fuzzing (Atheris) of the file-name, checksum, subtitle and path parsers on every change
- 🤖 Semgrep runs from a pinned container image; all scanner packages pinned with hashes
- 📖 Documentation restyled; new [interface reference](docs/INTERFACES.md) and this changelog

## 7.23 - 2026-11-12

Security release - see [SECURITY.md](SECURITY.md) for how each part works.

- 🔒 **Device approval**: every device on the network must be allowed in the app (Allow / Deny banner), tied to its hardware address; activity log of access and admin actions
- 🔒 **Download integrity**: checksums (pasted, publisher's checksum file, found automatically for installers and disk images), SHA-256 + Verify for every file, macOS quarantine flag, mismatching files set aside
- 🔒 `https` downloads never downgrade to `http`; `curl` ignores `~/.curlrc`; file names lose control and right-to-left characters
- 🔒 Disguised HLS / concat playlists are refused before ffmpeg touches them; subtitle archives limited (zip bombs)
- 🔒 Proxy: approval required, DNS-rebinding-safe, never reaches the Mac itself; TV discovery only talks to the device that answered
- 🔒 LG TV certificate pinned at pairing, no fallback to the unencrypted port; **Forget** button
- 🔒 VPN router helper moved to a root-only folder; devices that are not on a list cannot route through the Mac
- 🔒 Shared folders: no system or private folders, no hidden files
- 🤖 Security pipeline: Bandit, Semgrep, CodeQL, Gitleaks, ShellCheck, security tests, `pf` rule check; pull-request feedback; OpenSSF Scorecard

## 7.22 - 2026-11-12

First public release: resumable verified downloads, LG TV player (LG's player and the dlus player), subtitle search and auto sync, Magic Remote on the Mac, DLNA, folder sharing, HTTP proxy, VPN router.
