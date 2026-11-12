<h1 align="center">🐧 dlus for Linux</h1>

<p align="center">The plan for a Linux version: a <code>.deb</code> for Ubuntu and Debian, built and tested in CI.<br>
Everything a contributor needs: what already works, what is macOS-only and what replaces it, how it is packaged and tested.<br>
Tracking: <a href="https://github.com/amahbadi/dlus/issues/21">#21</a> · Tasks: <a href="https://github.com/amahbadi/dlus/labels/linux">issues labelled <b>linux</b></a> · Roadmap: <a href="ROADMAP.md">ROADMAP.md</a></p>

> [!NOTE]
> **Status: planned.** macOS stays the main platform. Pick a task from the [linux issues](https://github.com/amahbadi/dlus/labels/linux), say in the issue that you are on it, and follow [CONTRIBUTING.md](../CONTRIBUTING.md). Each task is small enough for one pull request.

## 🎯 Goal

| | |
|---|---|
| **Targets** | Ubuntu 22.04 / 24.04 and Debian 12 (x86-64 and arm64) - other distributions may follow |
| **Package** | `dlus_<version>_all.deb` (dlus is Python: one package for every CPU) |
| **Runs as** | a `systemd --user` service, UI in the normal web browser at `http://127.0.0.1:8642/` |
| **Same as macOS** | downloads, checksums, players, TV control (LG webOS + DLNA), subtitles + auto sync, sharing with device approval, proxy, activity log |
| **Later** | the router (nftables), a native window |
| **Not planned** | Snap / Flatpak / AppImage before the `.deb` is solid |

## ✅ What already works on Linux

Most of dlus is plain Python standard library and runs unchanged:

- downloads (`curl`), chunk checks, checksums, SHA-256, Verify
- both web servers, the TV player pages, HLS, DLNA, the proxy, device approval, the activity log
- LG TV control (WebSocket over TLS) and casting, subtitle search and sync (`ffmpeg`)
- the test suite: most tests already pass on Linux (CI runs the security scans on Ubuntu today)

## 🔁 macOS-only parts and their Linux replacements

Every macOS-specific call in `dlus.py`, and what replaces it. The rule: **one small helper per feature** (e.g. `notify()`, `prevent_sleep()`), choosing the macOS or Linux command inside - never `if linux` spread through the code.

| Feature | macOS today | Linux | Where in `dlus.py` |
|---|---|---|---|
| Notifications | the app (`--notify`), `osascript` fallback | `notify-send` (libnotify) | `Manager.notify` |
| Keep the computer awake while downloading | `caffeinate` | `systemd-inhibit --what=sleep` | `Manager.tick` |
| Mark downloads as from the internet | `xattr com.apple.quarantine` | `setfattr -n user.xdg.origin.url` (if available); no Gatekeeper exists | `quarantine()` |
| Sleep when all downloads finish | `pmset sleepnow` | `systemctl suspend` | `Manager.tick` |
| Show file / open with a player | `open -R`, `open -a <app>` | `xdg-open <folder>`, `mpv` / `vlc` / `xdg-open <url>` | `Manager.action` |
| Network: route, interfaces, addresses | `route`, `ifconfig`, `networksetup` | `ip -j route`, `ip -j addr` | `Net.slow`, `own_ips`, `Gateway.lan` |
| Wi-Fi name | `ipconfig getsummary` | `nmcli -t -f active,ssid dev wifi` (or `iw`) | `Net.slow` |
| VPN profiles | `scutil --nc list` | `nmcli -t con show --active` (VPN / WireGuard types), `wg show` | `Net.slow` |
| System proxy | `scutil --proxy` | `gsettings get org.gnome.system.proxy` / `$http_proxy` | `Net.slow` |
| Running VPN apps | `ps -axco comm` | `ps -eo comm` | `Net.slow` |
| Devices on the network | `arp -an` | `ip -j neigh` | `Gateway.scan`, `Manager.mac_of` |
| Hardware video (convert for the TV) | VideoToolbox (`-hwaccel videotoolbox`, `hevc_videotoolbox`, `scale_vt`) | VA-API (`hevc_vaapi`, `scale_vaapi`) when `/dev/dri` exists, else `libx265` | `compat_cmd`, `Manager.thumb` |
| Background service | LaunchAgent (`macos/setup.sh`) | `systemd --user` unit | new `linux/dlus.service` |
| App window | Swift + WebKit (`macos/main.swift`) | the default browser (`xdg-open`); a GTK window later | new `linux/dlus` launcher |
| Router (VPN for the TV) | root helper with `pf` (`gateway/`) | nftables helper, same checked-command design | later milestone |
| Install | DMG | `.deb` (`dpkg-deb`) | new `linux/build_deb.sh` |

## 📦 Package layout

```text
/usr/lib/dlus/dlus.py                       the program (and icons/)
/usr/bin/dlus                               launcher: starts the user service if needed, opens the browser
/usr/lib/systemd/user/dlus.service          the background service (Restart=on-failure)
/usr/share/applications/dlus.desktop        menu entry
/usr/share/icons/hicolor/*/apps/dlus.png    icons
/usr/share/doc/dlus/                        README, SECURITY, CHANGELOG, copyright (MIT)
~/.dlus/                                    the user's data, exactly as on macOS (mode 700, secrets 600)
```

`Depends: python3 (>= 3.9), curl, ffmpeg` · `Recommends: libnotify-bin, network-manager` · `Suggests: mpv | vlc`. No Python packages from PyPI: dlus uses only the standard library.

## 🔒 Security on Linux

The security model stays the same ([SECURITY.md](../SECURITY.md), [assurance case](ASSURANCE-CASE.md)):

- the UI listens on `127.0.0.1` only and needs the key in `~/.dlus/ui-key` (600); `~/.dlus` is 700
- sharing, DLNA and the proxy are off by default; every device must be approved
- **no Gatekeeper:** Linux has no equivalent - the README must say clearly that downloaded programs are not checked by the system
- **router later:** the nftables helper must keep the macOS design: installed root-owned in `/usr/libexec/dlus/`, a fixed command set with checked arguments, a sudoers rule for that one file (or a polkit action), rules only in its own nftables table, everything undone on *off*
- the systemd unit runs as the user with `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ReadWritePaths=%h/.dlus %h/Downloads` (plus the download folder from the settings)

## 🧪 Tests and CI

- run the existing test suite on `ubuntu-latest` (the mac-only tests skip themselves with `@unittest.skipUnless(sys.platform == "darwin")`)
- new tests feed the Linux network parsers recorded `ip -j` / `nmcli` output (like `NetworkDetection` in `tests/test_edges.py`)
- install test: build the `.deb`, then in clean `ubuntu:22.04`, `ubuntu:24.04` and `debian:12` containers: `apt install ./dlus_*.deb`, start `dlus.py` as a user, check `/api/health`, run a download against the local test server, uninstall cleanly
- `lintian` on the package
- coverage stays above 90 % on both platforms

## 🚀 Release

The release workflow builds the `.deb` next to the DMG and attaches it with the same Sigstore provenance and `SHA256SUMS`. Later: a signed APT repository so `apt upgrade` updates dlus.

## 🧭 Order of work

1. platform helpers for notifications, staying awake, quarantine, reveal / open (small, independent) - [#11](https://github.com/amahbadi/dlus/issues/11)
2. network information and devices with `ip` / `nmcli` - [#12](https://github.com/amahbadi/dlus/issues/12) [#13](https://github.com/amahbadi/dlus/issues/13)
3. video conversion with VA-API / software encoders - [#14](https://github.com/amahbadi/dlus/issues/14)
4. systemd service + launcher - [#15](https://github.com/amahbadi/dlus/issues/15)
5. `.deb` package - [#16](https://github.com/amahbadi/dlus/issues/16)
6. CI: tests on Linux + install tests in containers - [#17](https://github.com/amahbadi/dlus/issues/17) [#18](https://github.com/amahbadi/dlus/issues/18)
7. release the `.deb`, README install section - [#19](https://github.com/amahbadi/dlus/issues/19)
8. later: router with nftables; a native window - [#20](https://github.com/amahbadi/dlus/issues/20)
