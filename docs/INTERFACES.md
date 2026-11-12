<h1 align="center">🔌 Interfaces</h1>

<p align="center">Everything that goes into dlus and comes out of it: the command line, the local API, the network addresses and the files it writes.<br>For everyday use see the <a href="USER-GUIDE.md">user guide</a>; for how each part is protected see <a href="../SECURITY.md">SECURITY.md</a>.</p>

## ⌨️ Command line

```sh
python3 dlus.py [--port N] [--no-open]                       # start dlus (the app does this for you)
python3 dlus.py add URL... [-f FOLDER] [-c CONNS] [--paused]  # add downloads to the running dlus
```

| Option | Meaning |
|---|---|
| `--port N` | port of the local window and API (default `8642`) |
| `--no-open` | do not open a window or browser at start |
| `add URL...` | one or more `http`, `https` or `ftp` links |
| `-f FOLDER` | folder name under the download folder, or a full path |
| `-c CONNS` | connections per file (1-32) |
| `--paused` | add without starting |

Output: the ids of the added downloads, or the reason a link was skipped. Exit code `0` on success.

## 🖥️ Local API (`http://127.0.0.1:8642`)

Used by the dlus window. Only this Mac can reach it, and every request needs the key from `~/.dlus/ui-key` (cookie `dlus_k`).

| Method | Path | Input (JSON) | Output (JSON) |
|---|---|---|---|
| `GET` | `/api/health` | - | `{"ok": true, ...}` |
| `GET` | `/api/state` | - | downloads, settings, network, devices, access requests, activity log |
| `POST` | `/api/add` | `{"urls": "text with links", "folder": "", "conns": 0, "checksum": "", "start": true}` | `{"ids": [...], "skipped": [...]}` |
| `POST` | `/api/d/<id>/<action>` | action `pause`, `resume`, `start`, `delete`, `verify` (`{"checksum": "..."}`) | `{"ok": true}` |
| `POST` | `/api/settings` | the settings to change | `{"ok": true}` |
| `POST` | `/api/access` | `{"ip": "...", "act": "allow" \| "deny" \| "remove"}` | `{"ok": true}` |
| `POST` | `/api/tv/<tv>/<action>` | TV remote actions (`button`, `launch`, `open`, `forget`, ...) | action result |
| `POST` | `/api/gw/...` | VPN router: on/off, devices, helper install, checks | router state |

Errors: HTTP `4xx` with `{"error": "what went wrong"}`.

## 🏠 Home network (`http://<mac>:8643`, only when you turn sharing on)

| Address | What |
|---|---|
| `/` | page to type the 6-character code |
| `/<code>/tv` | TV home page: films and shared folders |
| `/<code>/<number>` | the dlus player for one film |
| `/<code>/<number>...` | stream links (original file, converted streams, HLS) for players |
| `/<code>/files/...` | shared folders (read only) |
| `/dlna/...` | DLNA / UPnP media server (when DLNA is on) |
| `:8118` | HTTP proxy (when the proxy is on; password by default) |

Every device must be allowed in the app first; until then it gets *Waiting for approval* (HTTP `403`).

## 📁 Files

| Path | Contents |
|---|---|
| your download folder (default `~/Downloads/dlm`) | finished files; `<name>.checksum-failed` for files that failed their checksum |
| `<file>.parts/` | pieces of a download in progress (removed when it finishes) |
| `~/.dlus/settings.json`, `state.json` | settings and the download list |
| `~/.dlus/audit.json` | activity log (last 2000 entries) |
| `~/.dlus/dlus.log` | technical log (rotated, 2 MB × 4) |
| `~/.dlus/ui-key`, `media-key` | secret keys for the window and the players (mode 600) |

## 🛡️ Root helper (`/Library/PrivilegedHelperTools/dlus-gw`)

Only if you install the VPN router. Commands: `enable`, `set`, `kill`, `status`, `disable`, `selftest`, `full on|off`, `version` - see the comment at the top of [`gateway/dlus-gw`](../gateway/dlus-gw) and [VPN-ROUTER.md](VPN-ROUTER.md).
