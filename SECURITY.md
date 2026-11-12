<h1 align="center">🔒 Security and privacy</h1>

<p align="center">dlus downloads files from the internet, shares them on your home network and can route other devices through your Mac.<br>Each of these is a way in, so each one is locked down. This page says how, and how to check it yourself.</p>

> [!CAUTION]
> **Found a security problem?** Report it privately: **[Report a vulnerability](https://github.com/amahbadi/dlus/security/advisories/new)**. Please do not open a public issue.

| | In one line |
|---|---|
| ⬇️ **Downloads** | checked piece by piece, checksums (yours or the publisher's), SHA-256 + Verify, Gatekeeper quarantine |
| 💻 **Your Mac** | the dlus window needs a key only your account can read; nothing else can control it |
| 🏠 **Your network** | off until you turn it on; then **every device must be allowed by you**; everything is logged |
| 🛡️ **VPN router** | one small checked root helper in a root-only folder; only your devices may route |
| 🤖 **Every change** | Bandit, Semgrep, CodeQL, Gitleaks, ShellCheck, fuzzing, security tests; signed releases |

## 🔑 What dlus can access

| Permission | When | Why |
|---|---|---|
| Local Network (macOS asks) | finding and controlling your TV | discovery (SSDP / DLNA) and the TV's remote-control service |
| Folders you choose | only folders you add under Sharing | so a TV can browse them |
| Administrator password | only if you install the VPN router helper | IP forwarding and firewall rules; see [docs/VPN-ROUTER.md](docs/VPN-ROUTER.md) |
| Internet | your downloads, subtitle searches, optional font downloads | nothing else |

> [!NOTE]
> There are no accounts, no analytics and no telemetry. dlus never sends your files, history or settings anywhere.

## ⬇️ Downloads: the file you get is the file you asked for

- **Every piece is checked.** Each 256 MB chunk is compared with the server in several places; a bad chunk is downloaded again on its own. A resumed download is never silently corrupted.
- **Checksums.** Paste the publisher's checksum (SHA-256, SHA-512, SHA-1 or MD5), or a link to their checksum file, when you add a link. For installers, disk images and archives, dlus also looks for the publisher's checksum file next to the download (`file.iso.sha256`, `SHA256SUMS` ...) by itself.
  - If it does not match, the file is **set aside** as `<name>.checksum-failed` and the download is marked as failed, so you do not open it by mistake.

> [!TIP]
> A checksum from the same server only proves the file arrived intact. A checksum from the **publisher's own website** (for example the SHA-256 shown on a download page) also proves nobody swapped the file on the server or a mirror. Paste that one when you have it.

- **SHA-256 of every finished file** is shown in its details. **Verify** checks it again later: against a checksum you paste, or against the file as it was downloaded (was it changed since?).
- **Gatekeeper still checks downloads.** Finished files get the macOS quarantine flag, exactly like files from Safari, so macOS checks apps and installers from them before they run.
- **No downgrade.** A `https` link may only redirect to `https`. Only `http`, `https` and `ftp` are used; `curl` ignores `~/.curlrc` and gets an argument list, never a shell.
- **Safe names.** File names from servers lose `/`, control characters and right-to-left tricks (a file shown as `photo-gpj.exe` that is really an `.exe`), so a download cannot write outside its folder or pretend to be another file type.
- **Disguised playlists are refused.** A "video" that is really an HLS / ffconcat / DASH playlist could make ffmpeg or a player read other files on the Mac or addresses on your network. dlus never plays or converts such a file and logs it.
- **Subtitles** from the internet are shown as text (only bold, italic and underline). Archives that unpack to more than 30 MB are refused (zip bombs).

## 💻 Your Mac: nobody else can control dlus

- **The dlus window (`127.0.0.1:8642`)** listens on this Mac only.
  - It needs a secret key that only your macOS account can read (`~/.dlus/ui-key`, mode 600). Other users and programs on the Mac cannot control it.
  - It accepts only requests addressed to `127.0.0.1` or `localhost`, which blocks "DNS rebinding".
  - It refuses commands sent from other websites (cross-site requests).
  - Pages carry `X-Frame-Options`, `frame-ancestors`, `nosniff` and `no-referrer` headers, so other sites cannot frame it (clickjacking).
- **Data**: `~/.dlus` (settings, TV pairing keys, download list, activity log) is readable only by you (folder mode 700).
- **The app** is signed with the hardened runtime. Links to the internet open in your normal browser, not inside dlus.

## 🏠 Your network: every device must be allowed by you

> [!IMPORTANT]
> Sharing, DLNA and the proxy are **off** until you turn them on. When they are on:

- **A new device must be allowed in the app.** Even with the right 6-character code, a TV, phone or computer gets only a "waiting for approval" page until you press **Allow** on the banner in dlus. **Deny** blocks it for an hour. Allowed devices are listed under **Devices & viewers**, each with a **Remove** button.
- **Approval is tied to the device.** dlus remembers the device's hardware (MAC) address. If another device takes over that IP address, it is asked again.
- **TVs you control from the Mac** (paired with an *Allow* on the TV, or a film you sent to them) are allowed by themselves.
- **Code guessing is blocked.** Wrong codes, typed or put into an address, are limited to 10 per 10 minutes per device. **New code** under Sharing also removes every allowed device except paired TVs.
- **Only private addresses** (your home network) are answered. Shared folders cannot be escaped with `../`, hidden files (`.ssh`, `.git` ...) are never served, and system folders, `~/Library`, `~/.ssh` and the dlus settings folder cannot be shared at all.
- **The proxy** asks for a password (on by default) and for approval. It never lets a device reach this Mac itself (the dlus window or other local services), also not through a DNS name that points back to the Mac.
- **TV discovery** only talks to the device that answered, over plain `http`, so a device on the network cannot point dlus at local files or at another machine.
- **The LG TV connection is pinned.** The TV's remote-control port uses a self-signed certificate. dlus remembers that certificate when you pair, refuses the connection if it ever changes (someone listening in), and never falls back to the unencrypted port once a TV used the encrypted one. **Forget** on the TV page removes the pairing.

## 🛡️ The VPN router: what runs as root

Only if you install it. The helper is one short shell script, [`gateway/dlus-gw`](gateway/dlus-gw).

- It lives in `/Library/PrivilegedHelperTools`, a folder only root can change. The installer refuses to install if any folder on that path is writable by others.
- `sudo` lets **your** user run **only that file**, nothing else. It accepts a fixed set of commands; every argument is checked (interface names, IPv4 addresses, network sizes).
- All firewall rules live in their own anchor (`com.apple/dlus`). `/etc/pf.conf` and your other rules are never edited. No `route-to` (it crashed macOS 26 with some VPN tunnels).
- **Only devices you add can use the Mac as a router.** Any other device on the home network that points its gateway at the Mac is dropped, so it cannot reach your VPN's private networks through it.
- Turning it off restores IP forwarding to what it was and removes every rule and route.

## 📜 The activity log

**Devices & viewers → Activity log** records who did what and when:

- devices asking for access, allowed, denied, removed;
- wrong codes, and an address taken over by another device;
- the TV home page, streams, shared folders, DLNA and the proxy being used;
- sharing, proxy and VPN router turned on or off, a new code, the root helper installed or removed;
- downloads added, checksum results, refused files.

It is kept in `~/.dlus/audit.json` (last 2000 entries).

## 🤖 Checked on every change

| Check | What it looks for |
|---|---|
| [Security workflow](.github/workflows/security.yml) | Bandit and Semgrep (Python, command injection, secrets, GitHub Actions), Gitleaks over the whole history, ShellCheck on the root helper and installers |
| [Fuzzing](fuzz/) | Atheris (Google's fuzzer) throws millions of random inputs at the parsers that read untrusted data: file names, checksums, subtitles, release names, shared-folder paths |
| [CodeQL](.github/workflows/codeql.yml) | GitHub's code analysis (`security-extended`) for the Python code and the workflows |
| [CI](.github/workflows/ci.yml) | [security tests](tests/test_security.py), the helper's firewall rules loaded by macOS `pf`, every build |
| [OpenSSF Scorecard](https://scorecard.dev/viewer/?uri=github.com/amahbadi/dlus) | an independent rating of how the project is run (pinned actions, branch protection, reviews ...) |

Pull requests get a comment with the findings and must pass every check before the maintainer reviews them. All GitHub Actions are pinned to exact commits and kept up to date by Dependabot.

## ✅ Check a download of dlus yourself

Every release is built by GitHub Actions from the tagged source and carries a signed build provenance:

```sh
gh attestation verify dlus-7.24.dmg --repo amahbadi/dlus   # proves it was built here, from this repository
shasum -a 256 -c SHA256SUMS                                # matches the checksum published with the release
```

Each release also carries `dlus-<version>.dmg.sigstore.json` (the Sigstore signature bundle, for offline checks with `gh attestation verify --bundle` or `cosign`) and `dlus-<version>.dmg.intoto.jsonl` (the SLSA build provenance).

## 📮 Reporting a problem

Please report it privately: **Security → Report a vulnerability** on this repository ([link](https://github.com/amahbadi/dlus/security/advisories/new)). Do not open a public issue for security problems.

| Step | When |
|---|---|
| ✉️ We confirm we received your report | within **7 days** |
| 🔍 We tell you whether we can reproduce it, and the plan | within **14 days** |
| 🛠️ Fix released as a new version | as soon as possible; **within 60 days** for confirmed vulnerabilities, sooner for critical ones |
| 📣 Public advisory and credit (if you want it) | when the fix is released, listed in the [changelog](CHANGELOG.md) |

Only the latest release gets security fixes - please update.
