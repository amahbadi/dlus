<h1 align="center">📝 Changelog</h1>

<p align="center">Every release, newest first. Downloads: <a href="https://github.com/amahbadi/dlus/releases">Releases</a>. Version numbers are <code>major.minor</code>; every release has its own tag <code>v&lt;version&gt;</code>.</p>

> [!NOTE]
> Security fixes are marked 🔒. dlus has had no publicly known vulnerabilities so far; the fixes below were found by our own review and scanners.

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
