<h1 align="center">🔎 Security review</h1>

<p align="center">The latest security review of dlus: what was reviewed, against what, what was found and what was done.<br>Requirements, threat model and trust boundaries: <a href="ASSURANCE-CASE.md">ASSURANCE-CASE.md</a></p>

| | |
|---|---|
| **Date** | 12-13 November 2026 (releases 7.23 - 7.30) |
| **Reviewers** | the maintainer, assisted by Claude (Anthropic) |
| **Scope** | all of dlus: `dlus.py` (service, both web servers, proxy, downloads, media, TV control), `macos/main.swift` (window), `gateway/` (root helper and installers), build and release pipeline |
| **Next review** | at the latest November 2027, and before any change to a trust boundary |

## 📐 Basis

The review checked every requirement and trust boundary in the [assurance case](ASSURANCE-CASE.md):

- **Requirements R1-R6:** download integrity, control of the Mac, home network, root privileges, the Mac's network, supply chain.
- **Boundaries:** internet → dlus · home-network devices → dlus · web pages in the browser → the local API · dlus → root helper · dlus → `~/.dlus` · dlus ↔ the LG TV.

## 🧪 Method

1. **Manual review** of every input that crosses a boundary: URLs, headers and file names from servers; requests, codes and SSDP answers from the network; subtitle files and archives; every root-helper command and argument.
2. **Attack tests** turned into automated tests, kept in the suite: path traversal, DNS rebinding, CSRF, open redirect, log injection, disguised playlists, zip bombs, a changed TV certificate (fake TV), damaged and changed pieces, an unknown device at the door, code guessing ([tests/](../tests)).
3. **Tools:** CodeQL (security-extended), Bandit, Semgrep, Gitleaks over the whole history, ShellCheck, Atheris fuzzing, OSV / Dependabot, OpenSSF Scorecard. All findings fixed or documented as false positives in the code.
4. **Test coverage** of the reviewed code raised from 17 % to 90 % of statements (84 % of branches), so the reviewed behaviour stays checked.

## 🐛 Findings and fixes

| Found | Severity | Fixed in |
|---|---|---|
| Root helper installed in `/usr/local/libexec`, which can be writable by the user on Macs with Homebrew - any program could swap the root file | high | 7.23 (root-only folder, installer refuses unsafe paths) |
| Any device on the home network could use the Mac as a router into the VPN's networks when the router was on | high | 7.23 (only listed devices may route) |
| The 6-character code alone gave full access to shared films, folders and the proxy; codes could be guessed through the address without a limit | high | 7.23 (per-device approval, MAC-bound; rate limit everywhere) |
| No checksums from publishers, no quarantine flag (Gatekeeper skipped downloaded apps), https links could redirect to http | medium | 7.23 |
| A "video" that is really an HLS playlist could make ffmpeg read local files | medium | 7.23 |
| Subtitle archives without a size limit (zip bomb) | medium | 7.23 |
| TV discovery followed any `LOCATION` (local files, other hosts); TV connection without certificate check, with silent downgrade | medium | 7.23 (same-host http only; certificate pinned, no downgrade) |
| Proxy: DNS rebinding to the Mac itself, non-constant-time password check | medium | 7.23 |
| Credentials (TV code, proxy password, TV keys) inside `settings.json` | low | 7.26 (`secrets.json`, mode 600) |
| Open redirect after the key, log-line injection | low | 7.24 |
| Malformed `CONNECT` crashed a proxy handler; any web server was taken for a VPN proxy | low | 7.28 |
| A checked piece that changed on disk before joining went into the file unnoticed | medium | 7.29 (SHA-256 per piece, verified while joining) |
| The unencrypted LG remote-control port was used automatically when the encrypted one did not answer | low | 7.30 (opt-in setting, off by default) |
| Auto sync accepted a subtitle for another film on short videos | low (correctness) | 7.28 |

## ⚠️ Remaining risks (accepted, documented)

- **Home-network sharing is plain HTTP** (TV browsers and DLNA cannot do local HTTPS): off by default, private addresses only, every device approved. Someone on the same Wi-Fi can still read an approved stream.
- **First pairing with a TV trusts its certificate** (trust on first use) - every later connection is checked.
- **One maintainer** (bus factor 1): mitigated by a GitHub successor and key-free releases ([GOVERNANCE.md](../GOVERNANCE.md#-continuity)).
- **Not notarized by Apple** (no paid developer membership): releases are instead signed with Sigstore build provenance ([REPRODUCIBLE.md](REPRODUCIBLE.md)).
