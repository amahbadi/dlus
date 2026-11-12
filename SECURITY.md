# Security and privacy

## What dlus can access

| Permission | When | Why |
|---|---|---|
| Local Network (macOS asks) | finding and controlling your TV | discovery (SSDP / DLNA) and the TV's remote-control service |
| Folders you choose | only folders you add under Sharing | so a TV can browse them |
| Administrator password | only if you install the VPN router helper | IP forwarding and firewall rules; see [docs/VPN-ROUTER.md](docs/VPN-ROUTER.md) |
| Internet | your downloads, subtitle searches, optional font downloads | nothing else |

There are no accounts, no analytics and no telemetry. dlus never sends your files, history or settings anywhere.

## How it is protected

- **The dlus window (`127.0.0.1:8642`)** listens on this Mac only.
  - It needs a secret key that only your macOS account can read (`~/.dlus/ui-key`, mode 600). Other users and programs on the Mac cannot control it.
  - It accepts only requests addressed to `127.0.0.1` or `localhost`, which blocks "DNS rebinding".
  - It refuses commands sent from other websites (cross-site requests).
  - Pages carry `X-Frame-Options`, `frame-ancestors`, `nosniff` and `no-referrer` headers, so other sites cannot frame it (clickjacking).
- **Your network (`:8643`)**: TV pages, streams and shared files sit behind a random secret code.
  - Wrong codes are rate-limited (10 tries per 10 minutes per device).
  - Only private network addresses are answered.
  - Shared folders cannot be escaped with `../`.
  - Sharing, DLNA and the proxy are off until you turn them on.
- **Data**: `~/.dlus` (settings, TV pairing keys, download list) is readable only by you.
- **Input**:
  - File names from servers are cleaned, so a download cannot write outside its folder.
  - Subtitles from the internet are shown as text; only bold, italic and underline are allowed.
  - External programs (`curl`, `ffmpeg`) are started with argument lists, never through a shell.
- **The app**: signed with the hardened runtime. Links to the internet open in your normal browser, not inside dlus. The only certificate exception is your TV's own self-signed service (port 3001 on your home network), used for its app icons.

## Reporting a problem

Please report it privately: **Security → Report a vulnerability** on this repository ([link](https://github.com/amahbadi/dlus/security/advisories/new)). Do not open a public issue for security problems. Fixes are released as a new DMG.
