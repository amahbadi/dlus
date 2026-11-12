#!/usr/bin/env python3
"""dlus - downloader for us. Fast, safe, resumable download manager with a live web UI
(Python stdlib + curl, macOS first).

  python3 dlus.py                   start; UI at http://127.0.0.1:8642/
  python3 dlus.py add URL... [-f FOLDER] [-c CONNS] [--paused]
  options: --port N, --no-open

Downloads
  * Split into 256 MB chunks, N parallel range connections. Progress lives in
    "<file>.parts/" next to the target, so stops, crashes, reboots and network
    loss never restart a download. Cut connections keep their data (reused).
  * Checks: last 64 KB re-fetched before continuing a chunk; 3 samples per
    finished chunk compared with the server; only HTTP 206 accepted; size+ETag
    pinned; a failed check re-downloads that chunk alone; optional checksum.
  * Play while downloading: /stream/<id>/<name> serves the downloaded part with
    range support; jumping ahead makes that part download first.
  * Queue with order + scheduler (time window), traffic counter, network/VPN
    panel, link-IP-binding warning.

State: ~/.dlus/ (state.json, settings.json, stats.json, dlus.log, lock); an old ~/.dlm is moved here.
"""
import argparse
import base64
import bisect
import collections
import errno
import getpass
import glob
import gzip
import fcntl
import hashlib
import html
import http.server
import io
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import secrets
import select
import socketserver
import ssl
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from xml.sax.saxutils import escape as xesc

PORT = 8642
VERSION = "7.25"
APP_DIR = os.path.dirname(os.path.abspath(__file__))  # ~/Desktop/dlus, or dlus.app/Contents/Resources when installed
ICON_DIR = os.path.join(APP_DIR, "icons")
APP_HEAD = ('<link rel=manifest href="/manifest.webmanifest"><link rel=icon type=image/png sizes=192x192 href="/icon-192.png">'
            '<link rel=apple-touch-icon sizes=180x180 href="/apple-touch-icon.png"><link rel=image_src href="/icon-512.png">'
            '<meta name=theme-color content="#6d5efc"><meta name=application-name content=dlus><meta name=apple-mobile-web-app-title content=dlus>'
            '<meta name=mobile-web-app-capable content=yes><meta name=apple-mobile-web-app-capable content=yes>'
            '<meta name=apple-mobile-web-app-status-bar-style content=black-translucent><meta property=og:title content=dlus>'
            '<meta property=og:image content="/icon-512.png"><meta name=msapplication-TileImage content="/icon-512.png">'
            '<meta name=msapplication-TileColor content="#6d5efc">')


SEC_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
UI_OPEN = {"/api/health", "/favicon.svg", "/favicon.ico", "/favicon.png", "/logo.svg", "/manifest.webmanifest", "/manifest.json",
           "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/icon-32.png", "/icon-180.png", "/icon-192.png", "/icon-512.png"}
LOCKED_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>dlus</title><style>body{margin:0;height:100vh;display:grid;place-items:center;
background:#09090b;color:#e4e4e7;font:16px -apple-system,system-ui,sans-serif;text-align:center}img{width:88px;border-radius:24px}p{color:#a1a1aa;max-width:420px}</style></head>
<body><div><img src="/icon-192.png" alt=""><h2>Open dlus from the dlus app</h2><p>This page needs the key that only the dlus app on this Mac has.
Open <b>dlus</b> from Applications, or use View &rarr; Open in Browser there.</p></div></body></html>"""


def media_key():
    """second, weaker key: lets ffmpeg / ffprobe / video players read media (/stream /s /sub) - nothing else.
    It ends up in process lists and player links, so it must never unlock the rest of the UI."""
    p = os.path.join(STATE_DIR, "media-key")
    k = rd(p).strip() if os.path.exists(p) else ""
    if len(k) < 16:
        k = secrets.token_urlsafe(18)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(k)
    return k


def _ptr_query(name, qid=0x1d1d, unicast=False):
    q = struct.pack(">HHHHHH", qid, 0, 1, 0, 0, 0)
    for part in name.split("."):
        q += bytes([len(part)]) + part.encode()
    return q + b"\0" + struct.pack(">HH", 12, 0x8001 if unicast else 1)


def _dns_name(buf, off):
    out, jumped, end = [], False, off
    for _ in range(64):
        n = buf[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if not jumped:
                end = off + 2
            off, jumped = ((n & 0x3F) << 8) | buf[off + 1], True
            continue
        out.append(buf[off + 1:off + 1 + n].decode("utf-8", "ignore"))
        off += 1 + n
    return ".".join(out), (end if jumped else off)


def _ptr_answer(buf):
    an = struct.unpack(">H", buf[6:8])[0]
    off = 12
    for _ in range(struct.unpack(">H", buf[4:6])[0]):  # skip the questions
        _, off = _dns_name(buf, off)
        off += 4
    for _ in range(an):
        _, off = _dns_name(buf, off)
        typ, _, _, ln = struct.unpack(">HHIH", buf[off:off + 10])
        off += 10
        if typ == 12:
            return _dns_name(buf, off)[0]
        off += ln
    return ""


def lan_name(ip, router=""):
    """a LAN device's name without the system DNS (which may go through a VPN that does not know home devices):
    ask the device itself over Bonjour (mDNS), then the home router's DNS"""
    rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    for dst, port, uni in ((ip, 5353, True), ("224.0.0.251", 5353, True), (router, 53, False)):
        if not dst:
            continue
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_.settimeout(1.2)
        try:
            s_.sendto(_ptr_query(rev, unicast=uni), (dst, port))
            end = time.time() + 1.2
            while time.time() < end:
                buf, src = s_.recvfrom(2048)
                if dst == "224.0.0.251" and src[0] != ip:
                    continue
                n = _ptr_answer(buf)
                if n:
                    return re.sub(r"\.(local|lan|home|localdomain)\.?$", "", n.rstrip("."))
        except (OSError, struct.error, IndexError):
            pass
        finally:
            s_.close()
    return ""


def private_mac(mac):
    """random 'private Wi-Fi address' (iPhone, iPad, Android, Windows): the locally-administered bit is set"""
    try:
        return bool(int(mac.split(":")[0], 16) & 2)
    except ValueError:
        return False


def ui_key():
    """secret for the local UI, readable only by this user (~/.dlus is 0700, the file 0600)"""
    p = os.path.join(STATE_DIR, "ui-key")
    k = rd(p).strip() if os.path.exists(p) else ""
    if len(k) < 20:
        k = secrets.token_urlsafe(24)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(k)
    return k


def app_manifest(start):
    """installable web app: icon + name on a TV / phone home screen (the TV starts at the code page, which remembers the code)"""
    return json.dumps({"name": "dlus", "short_name": "dlus", "description": "downloads, movies and remote for your TV", "id": "/dlus",
                       "start_url": start, "scope": "/", "display": "fullscreen", "display_override": ["fullscreen", "standalone"],
                       "orientation": "landscape", "background_color": "#09090b", "theme_color": "#6d5efc",
                       "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}).encode()


def app_icon(h, path, start):
    """serves /icon-*.png, /apple-touch-icon.png, /manifest.webmanifest; True if it answered"""
    name = {"/apple-touch-icon.png": "icon-180.png", "/apple-touch-icon-precomposed.png": "icon-180.png",
            "/favicon.png": "icon-32.png"}.get(path) or (path[1:] if re.match(r"^/icon-(32|180|192|512)\.png$", path) else "")
    icon = safe_join(ICON_DIR, name) if name else None
    if icon and os.path.isfile(icon):
        with open(icon, "rb") as f:
            h.out(200, f.read(), "image/png", {"Cache-Control": "max-age=86400"})
        return True
    if path in ("/manifest.webmanifest", "/manifest.json"):
        h.out(200, app_manifest(start), "application/manifest+json")
        return True
    return False


LOGO = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="18" fill="#6d5efc"/><path d="M32 13v21M23.5 25.5 32 34l8.5-8.5" fill="none" stroke="#fff" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M19 38.5a13 13 0 0 0 26 0" fill="none" stroke="#fff" stroke-width="5.5" stroke-linecap="round"/></svg>'
C = int(os.environ.get("DLM_CHUNK_MB") or 256) << 20  # chunk size (env only for tests)
OV = 64 << 10  # check sample size
STATE_DIR = os.environ.get("DLM_HOME") or os.path.expanduser("~/.dlus")
if not os.environ.get("DLM_HOME") and not os.path.exists(STATE_DIR) and os.path.isdir(os.path.expanduser("~/.dlm")):
    os.rename(os.path.expanduser("~/.dlm"), STATE_DIR)  # migrate from the old name
CURL = ["/usr/bin/curl", "-q", "-sS", "-L", "--max-redirs", "10", "--proto", "=http,https,ftp,ftps", "--proto-redir", "=http,https,ftp,ftps",
        "--connect-timeout", "30", "--speed-limit", "10240", "--speed-time", "30"]  # -q: ignore ~/.curlrc
CATS = {
    "Video": "mp4 mkv avi mov webm m4v ts m2ts wmv flv mpg mpeg 3gp vob",
    "Audio": "mp3 flac m4a aac wav ogg opus wma alac aiff ape dsf",
    "Images": "jpg jpeg png gif webp heic heif svg bmp tiff tif raw cr2 nef arw dng avif",
    "Documents": "pdf doc docx xls xlsx ppt pptx txt csv odt ods odp rtf md pages numbers key",
    "Ebooks": "epub mobi azw azw3 fb2 djvu cbz cbr",
    "Archives": "zip rar 7z tar gz tgz bz2 xz zst lz4 cab",
    "Disk images": "iso dmg img vhd vhdx vmdk qcow2",
    "Programs": "pkg exe msi apk aab ipa deb rpm appimage app jar bin run",
    "Subtitles": "srt ass ssa vtt sub sup idx",
    "Torrents": "torrent",
    "Fonts": "ttf otf woff woff2",
    "Design": "psd ai sketch fig xd blend obj fbx stl",
}
EXT2CAT = {e: c for c, es in CATS.items() for e in es.split()}
MIME = {"mkv": "video/x-matroska", "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
        "avi": "video/x-msvideo", "ts": "video/mp2t", "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
        "flac": "audio/flac", "wav": "audio/wav", "ogg": "audio/ogg", "opus": "audio/ogg"}
RUNNING = ("downloading", "waiting", "joining", "verifying")
log = logging.getLogger("dlm")


# ---------------------------------------------------------------- helpers
def fsize(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def rm(*ps):
    for p in ps:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def rd(p, default=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return default


def wr(p, s):
    with open(p, "w") as f:
        f.write(s)


def wr_sync(p, s):
    """durable atomic write; previous version kept as .bak"""
    with open(p + ".new", "w") as f:
        f.write(s)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(p):
        shutil.copyfile(p, p + ".bak")
    os.replace(p + ".new", p)


def load_json(p, default):
    for cand in (p, p + ".bak"):
        try:
            return json.loads(rd(cand) or "null") or default
        except ValueError:
            log.warning("corrupt %s, trying backup", cand)
    return default


def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def category(name):
    n = name.lower()
    if re.search(r"\.(7z|zip|rar|tar)\.\d{3}$|\.part\d+\.rar$|\.r\d\d$", n):
        return "Archives"
    return EXT2CAT.get(n.rsplit(".", 1)[-1], "Other") if "." in n else "Other"


def safe_name(n):
    n = re.sub(r"[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", n)  # control and text-direction characters
    n = re.sub(r'[/\\:*?"<>|]', "_", n).strip().lstrip(".").rstrip(". ")
    return n[:200] or "download"


HASH_LEN = {32: "md5", 40: "sha1", 64: "sha256", 96: "sha384", 128: "sha512"}
SENSITIVE_CATS = ("Disk images", "Programs", "Archives")  # look for the publisher's checksum file for these


def parse_checksum(text, names=(), strict=False):
    """(algo, hex) from what a user pastes ('sha256:ab..', 'SHA-256: ab..', 'ab..') or from a checksum file
    (sha256sum 'hex  name', BSD 'SHA256 (name) = hex'). A line that names another file is ignored."""
    names = {n for n in names if n}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        algo = file = None
        mm = re.match(r"^(MD5|SHA-?1|SHA-?256|SHA-?384|SHA-?512)\s*\((.+)\)\s*=\s*([0-9A-Fa-f]+)$", line, re.I)
        if mm:
            algo, file, hx = mm.group(1).lower().replace("-", ""), mm.group(2), mm.group(3)
        else:
            mm = re.match(r"^(?:(MD5|SHA-?1|SHA-?256|SHA-?384|SHA-?512)\s*[:=]?\s*)?([0-9A-Fa-f]{32,128})(?:\s+\*?(.+))?$", line, re.I)
            if not mm:
                if strict:
                    continue
                mm2 = re.search(r"\b([0-9A-Fa-f]{32,128})\b", line)
                if not mm2:
                    continue
                hx = mm2.group(1)
            else:
                algo, hx, file = (mm.group(1) or "").lower().replace("-", "") or None, mm.group(2), mm.group(3)
        a = HASH_LEN.get(len(hx))
        if not a or (algo and algo != a):
            continue
        if file and names and os.path.basename(file.strip()) not in names:
            continue
        if strict and not file and names and len([l for l in text.splitlines() if l.strip()]) > 1:
            continue  # a list of hashes without names: cannot tell which one is ours
        return a, hx.lower()
    return None


def fetch_small(url, limit=1 << 20, timeout=15):
    """a small text file over http(s) only (checksum files)"""
    if not re.match(r"^https?://", url):
        raise ValueError("not a http(s) link")
    # security scan: scheme checked
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "dlus/" + VERSION}), timeout=timeout) as f:  # nosec B310
        if f.geturl().startswith("http://") and url.startswith("https://"):
            raise ValueError("redirected from https to http")
        return f.read(limit).decode("utf-8", "ignore")


def published_checksum(url, names):
    """the publisher's checksum file next to the download (file.iso.sha256, SHA256SUMS ...) -> (algo, hex, source) or None"""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or u.query:  # signed / one-time links: nothing to look next to
        return None
    base = url.rsplit("/", 1)[0] + "/"
    for c in [url + e for e in (".sha256", ".sha256sum", ".sha512", ".sha1", ".md5")] + [base + f for f in ("SHA256SUMS", "sha256sums.txt", "SHA512SUMS")]:
        try:
            txt = fetch_small(c, timeout=8)
        except (OSError, ValueError):
            continue
        if "<html" in txt[:2000].lower():  # a "not found" web page
            continue
        got = parse_checksum(txt, names, strict=True)
        if got:
            return got + (c,)
    return None


def quarantine(path):
    """mark a downloaded file the way Safari does, so macOS Gatekeeper checks apps and installers that come from it"""
    try:
        subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine", "0083;%08x;dlus;%s" % (int(time.time()), str(uuid.uuid4()).upper()), path],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


class OneLineFormatter(logging.Formatter):
    """every log entry stays one line, whatever a message contains (tracebacks keep their own lines)"""

    def formatMessage(self, record):
        return super().formatMessage(record).replace("\r", "\\r").replace("\n", "\\n")


def logsafe(v, n=300):
    """a value from the network, safe for one log line (no forged lines)"""
    return str(v)[:n].replace("\r", "\\r").replace("\n", "\\n")


def safe_join(base, rel):
    """real path of rel inside base, or None if it would leave base (../, links)"""
    base = os.path.realpath(base)
    p = os.path.realpath(os.path.join(base, rel))
    if p == base:
        return base
    if not p.startswith(base + os.sep):
        return None
    return p


def hidden_inside(base, path):
    """True if path goes through a hidden file or folder below base (.ssh, .git, .dlus ...): never shown on the network"""
    rel = os.path.relpath(path, base)
    return rel != "." and any(p.startswith(".") for p in rel.split(os.sep))


def share_ok(folder):
    """a folder that may be shared on the network: not a system folder, not one that holds dlus' keys or your private keys"""
    home = os.path.realpath(os.path.expanduser("~"))
    private = [os.path.realpath(STATE_DIR), os.path.join(home, ".ssh"), os.path.join(home, ".gnupg"), os.path.join(home, "Library")]
    for p in private:  # sharing the folder itself, or one that contains it
        if folder == p or p.startswith(folder.rstrip(os.sep) + os.sep) or folder.startswith(p + os.sep):
            return False
    system = ("/System", "/Library", "/private", "/etc", "/usr", "/bin", "/sbin", "/var", "/Applications", "/Users")
    return not any(folder == x or folder.startswith(x + os.sep) for x in system if x != "/Users") and folder != "/Users"


def name_from_url(u):
    path = urllib.parse.urlsplit(u).path
    return safe_name(urllib.parse.unquote(os.path.basename(path.rstrip("/"))))


def bound_ip(url):
    """public IPv4 embedded in the URL path (CDNs that bind links to the client IP)"""
    for s in urllib.parse.urlsplit(url).path.split("/"):
        try:
            ip = ipaddress.IPv4Address(s)
        except ValueError:
            continue
        if ip.is_global:
            return str(ip)
    return ""


def read_at(p, off, n):
    with open(p, "rb") as f:
        f.seek(off)
        return f.read(n)


SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub")
TEXT_SUBS = ("subrip", "ass", "ssa", "webvtt", "mov_text", "text", "srt")
LANG_NAMES = {"fa": "Persian (\u0641\u0627\u0631\u0633\u06cc)", "per": "Persian (\u0641\u0627\u0631\u0633\u06cc)", "fas": "Persian (\u0641\u0627\u0631\u0633\u06cc)",
              "en": "English", "eng": "English", "ar": "Arabic", "ara": "Arabic", "fr": "French", "fre": "French", "fra": "French",
              "es": "Spanish", "spa": "Spanish", "de": "German", "ger": "German", "tr": "Turkish", "tur": "Turkish"}


def sub_dir(did):
    return os.path.join(STATE_DIR, "subs", did)


def decode_sub(raw):
    """subtitle bytes -> text; Persian files are often Windows-1256, not UTF-8"""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            return raw.decode(enc, "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # old Persian files: Windows-1256 has only the Arabic yeh/kaf -> use the Persian letters
        return raw.decode("cp1256", "replace").replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")


def guess_lang(name, text):
    toks = set(re.split(r"[^a-z]+", name.lower()))
    for lang, keys in (("fa", {"fa", "per", "fas", "farsi", "persian"}), ("en", {"en", "eng", "english"}), ("ar", {"ar", "ara", "arabic"})):
        if toks & keys:
            return lang
    arab = len(re.findall("[\u0600-\u06ff]", text))
    latin = len(re.findall("[A-Za-z]", text))
    if arab > latin:
        return "fa" if re.search("[\u067e\u0686\u0698\u06af\u06a9\u06cc]", text) else "ar"
    return "en" if latin else ""


def to_vtt(text, name):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    ext = name.rsplit(".", 1)[-1].lower()
    if text.startswith("WEBVTT"):
        return text + "\n"
    if ext == "srt" or (ext not in ("ass", "ssa", "sub") and "-->" in text):
        return "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d{1,3})", r"\1.\2", text) + "\n"
    tmp = os.path.join(STATE_DIR, "subs", "conv-%s.%s" % (uuid.uuid4().hex[:6], ext))
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    wr(tmp, text)
    try:
        return sh(["ffmpeg", "-nostdin", "-v", "error", "-i", tmp, "-f", "webvtt", "pipe:1"], 60)
    finally:
        rm(tmp)


RES_TOK = {"2160p": "2160p", "4k": "2160p", "uhd": "2160p", "1080p": "1080p", "720p": "720p", "480p": "480p", "576p": "576p"}
SRC_TOK = {"remux": "remux", "bluray": "bluray", "blu": "bluray", "bdrip": "bluray", "brrip": "bluray", "web": "web",
           "webdl": "web", "webrip": "webrip", "hdtv": "hdtv", "cam": "cam", "hdcam": "cam", "dvdrip": "dvd", "hdrip": "hdrip"}
CODEC_TOK = {"x265": "hevc", "h265": "hevc", "hevc": "hevc", "x264": "avc", "h264": "avc", "avc": "avc", "av1": "av1"}


def parse_release(name):
    """'Some_Movie_2026_10bit_HDR_2160p_x265_BluRay_DTS-HD-GROUP.mkv' -> title, year, tokens"""
    base = re.sub(r"\.(mkv|mp4|avi|mov|m4v|ts|srt|ass|vtt|zip)$", "", name, flags=re.I)
    words = [w for w in re.split(r"[\s._\-\[\]()]+", base) if w]
    title, year, i = [], "", 0
    for i, w in enumerate(words):
        if re.match(r"^(19|20)\d\d$", w) and title:
            year = w
            break
        title.append(w)
    low = [w.lower() for w in words]
    toks = {"res": next((RES_TOK[w] for w in low if w in RES_TOK), ""),
            "src": next((SRC_TOK[w] for w in low if w in SRC_TOK and w != "blu"), "") or ("bluray" if "blu" in low else ""),
            "codec": next((CODEC_TOK[w] for w in low if w in CODEC_TOK), ""),
            "hdr": "hdr" in low or "dv" in low or "hdr10" in low,
            "groups": set(low[-3:]) - set(RES_TOK) - set(SRC_TOK) - set(CODEC_TOK) - {"5", "1", "7", "ma", "dts", "hd", "mkv"}}
    if "remux" in low:
        toks["src"] = "remux"
    return " ".join(title), year, toks


def release_score(mine, other):
    """0..100: how well a subtitle's release name matches our file"""
    _, _, a = parse_release(mine)
    _, _, b = parse_release(other)
    pts, tot = 0, 0
    for k, w in (("res", 3), ("src", 2), ("codec", 1)):
        if a[k]:
            tot += w
            pts += w if a[k] == b[k] else (w // 2 if k == "src" and {a[k], b[k]} <= {"bluray", "remux"} else 0)
    tot += 1
    pts += 1 if a["hdr"] == b["hdr"] else 0
    tot += 4
    pts += 4 if a["groups"] & b["groups"] else 0
    return round(pts * 100 / tot)


def vtt_starts(path):
    out = []
    for l in rd(path).splitlines():
        mm = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d\d)[.,](\d{1,3})\s*-->", l.strip())
        if mm:
            out.append(int(mm.group(1) or 0) * 3600 + int(mm.group(2)) * 60 + int(mm.group(3)) + int((mm.group(4) + "00")[:3]) / 1000)
    return sorted(out)


def sync_check(starts, ref):
    """best time shift (s) of a subtitle vs. a reference track of the same movie, and the share of lines that line up"""
    if len(starts) < 10 or len(ref) < 10:
        return None
    lim = ref[-1] + 5
    starts = [x for x in starts if x <= lim + 30]

    def hits(off, tol=0.35):
        n = 0
        for x in starts:
            y = x + off
            k = bisect.bisect_left(ref, y - tol)
            if k < len(ref) and ref[k] <= y + tol:
                n += 1
        return n
    best = max((hits(o / 2), o / 2) for o in range(-60, 61))
    diffs = []  # exact shift = median distance of the lines that line up
    for x in starts:
        k = bisect.bisect_left(ref, x + best[1] - 0.4)
        if k < len(ref) and ref[k] <= x + best[1] + 0.4:
            diffs.append(ref[k] - x)
    off = sorted(diffs)[len(diffs) // 2] if diffs else best[1]
    usable = len([x for x in starts if x + off <= lim]) or 1
    # positive offset = subtitle shows too early -> show it later by that much
    return {"offset": round(off, 2), "match": round(min(1.0, best[0] / usable) * 100)}


FONTS = {
    "Vazirmatn": ("https://cdn.jsdelivr.net/npm/vazirmatn@33.0.3/fonts/webfonts/", "Vazirmatn-Regular.woff2", "Vazirmatn-Bold.woff2"),
    "Sahel": ("https://cdn.jsdelivr.net/gh/rastikerdar/sahel-font@v3.4.0/dist/", "Sahel.woff2", "Sahel-Bold.woff2"),
    "Shabnam": ("https://cdn.jsdelivr.net/gh/rastikerdar/shabnam-font@v5.0.1/dist/", "Shabnam.woff2", "Shabnam-Bold.woff2"),
    "Samim": ("https://cdn.jsdelivr.net/gh/rastikerdar/samim-font@v4.0.5/dist/", "Samim.woff2", "Samim-Bold.woff2"),
}


JD = "https://cdn.jsdelivr.net/"
FS = JD + "npm/@fontsource/%s/files/%s-arabic-%d-normal.woff2"
FONT_CATALOG = {  # all free (OFL); "default" ones are installed automatically
    "Vazirmatn": {"kind": "Modern", "default": True, "files": [(JD + "npm/vazirmatn@33.0.3/fonts/webfonts/Vazirmatn-Regular.woff2", 400), (JD + "npm/vazirmatn@33.0.3/fonts/webfonts/Vazirmatn-Bold.woff2", 700)]},
    "Sahel": {"kind": "Modern", "default": True, "files": [(JD + "gh/rastikerdar/sahel-font@v3.4.0/dist/Sahel.woff2", 400), (JD + "gh/rastikerdar/sahel-font@v3.4.0/dist/Sahel-Bold.woff2", 700)]},
    "Shabnam": {"kind": "Modern", "default": True, "files": [(JD + "gh/rastikerdar/shabnam-font@v5.0.1/dist/Shabnam.woff2", 400), (JD + "gh/rastikerdar/shabnam-font@v5.0.1/dist/Shabnam-Bold.woff2", 700)]},
    "Samim": {"kind": "Modern", "default": True, "files": [(JD + "gh/rastikerdar/samim-font@v4.0.5/dist/Samim.woff2", 400), (JD + "gh/rastikerdar/samim-font@v4.0.5/dist/Samim-Bold.woff2", 700)]},
    "Vazir": {"kind": "Modern", "files": [(JD + "gh/rastikerdar/vazir-font@v30.1.0/dist/Vazir-Regular.woff2", 400), (JD + "gh/rastikerdar/vazir-font@v30.1.0/dist/Vazir-Bold.woff2", 700)]},
    "Estedad": {"kind": "Modern", "files": [(FS % ("estedad", "estedad", 400), 400), (FS % ("estedad", "estedad", 700), 700)]},
    "Noto Sans Arabic": {"kind": "Modern", "files": [(FS % ("noto-sans-arabic", "noto-sans-arabic", 400), 400), (FS % ("noto-sans-arabic", "noto-sans-arabic", 700), 700)]},
    "Parastoo": {"kind": "Classic", "files": [(JD + "gh/rastikerdar/parastoo-font@v2.0.1/dist/Parastoo.woff2", 400), (JD + "gh/rastikerdar/parastoo-font@v2.0.1/dist/Parastoo-Bold.woff2", 700)]},
    "Nahid": {"kind": "Classic", "files": [(JD + "gh/rastikerdar/nahid-font@v0.3.0/dist/Nahid.woff2", 400)]},
    "Noto Naskh": {"kind": "Classic", "files": [(FS % ("noto-naskh-arabic", "noto-naskh-arabic", 400), 400), (FS % ("noto-naskh-arabic", "noto-naskh-arabic", 700), 700)]},
    "Amiri": {"kind": "Classic", "files": [(FS % ("amiri", "amiri", 400), 400)]},
    "Lalezar": {"kind": "Display", "files": [(FS % ("lalezar", "lalezar", 400), 400)]},
}
FONT_MAGIC = (b"wOF2", b"wOFF", b"\x00\x01\x00\x00", b"OTTO", b"true")
FONT_MIME = {"woff2": "font/woff2", "woff": "font/woff", "ttf": "font/ttf", "otf": "font/otf"}


def font_file(url):
    return os.path.join(fonts_dir(), url.rsplit("/", 1)[1])


def font_installed(name):
    f = FONT_CATALOG.get(name)
    return bool(f) and all(fsize(font_file(u)) > 1000 for u, _ in f["files"])


def install_font(name):
    f = FONT_CATALOG.get(name)
    if not f:
        raise ValueError("unknown font")
    os.makedirs(fonts_dir(), exist_ok=True)
    for url, _ in f["files"]:
        dst = font_file(url)
        if fsize(dst) > 1000:
            continue
        try:
            # security scan: fixed https font URL
            data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "dlus/" + VERSION}), timeout=30).read(4 << 20)  # nosec B310
        except OSError as e:
            raise ValueError("download failed: %s" % e)
        if not data.startswith(FONT_MAGIC):
            raise ValueError("the download is not a font")
        with open(dst + ".tmp", "wb") as o:
            o.write(data)
        os.replace(dst + ".tmp", dst)


def user_fonts(m):
    return [x for x in m.settings.get("fonts_user") or [] if fsize(os.path.join(fonts_dir(), x["file"])) > 1000]


def font_names(m):
    """fonts the players can use, installed only"""
    return [n for n in FONT_CATALOG if font_installed(n)] + [x["name"] for x in user_fonts(m)]


def fonts_dir():
    return os.path.join(STATE_DIR, "fonts")


FONT_TTF = {"Vazirmatn": "https://cdn.jsdelivr.net/npm/vazirmatn@33.0.3/fonts/ttf/Vazirmatn-Bold.ttf",
            "Sahel": "https://cdn.jsdelivr.net/gh/rastikerdar/sahel-font@v3.4.0/dist/Sahel-Bold.ttf",
            "Shabnam": "https://cdn.jsdelivr.net/gh/rastikerdar/shabnam-font@v5.0.1/dist/Shabnam-Bold.ttf",
            "Samim": "https://cdn.jsdelivr.net/gh/rastikerdar/samim-font@v4.0.5/dist/Samim-Bold.ttf"}


def fetch_fonts():
    """download the subtitle fonts once (all OFL-licensed, ~50 KB each)"""
    os.makedirs(fonts_dir(), exist_ok=True)
    for url in FONT_TTF.values():
        dst = os.path.join(fonts_dir(), url.rsplit("/", 1)[1])
        if fsize(dst) < 1000:
            try:
                # security scan: fixed https font URL
                data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "dlus/" + VERSION}), timeout=30).read(4 << 20)  # nosec B310
                if data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                    with open(dst + ".tmp", "wb") as o:
                        o.write(data)
                    os.replace(dst + ".tmp", dst)
            except OSError:
                log.warning("font download failed: %s", url)
    for name, f in FONT_CATALOG.items():
        if f.get("default"):
            try:
                install_font(name)
            except ValueError:
                log.warning("font download failed: %s", name)
    for base, reg, bold in ():
        for f in (reg, bold):
            dst = os.path.join(fonts_dir(), f)
            if fsize(dst) > 1000:
                continue
            try:
                req = urllib.request.Request(base + f, headers={"User-Agent": "dlus/" + VERSION})
                # security scan: fixed https font URL
                data = urllib.request.urlopen(req, timeout=30).read(2 << 20)  # nosec B310
                if data[:4] == b"wOF2":
                    with open(dst + ".tmp", "wb") as o:
                        o.write(data)
                    os.replace(dst + ".tmp", dst)
            except OSError:
                log.warning("font download failed: %s", f)


def font_css(m=None):
    css = []
    for name, f in FONT_CATALOG.items():
        if font_installed(name):
            for url, w in f["files"]:
                css.append("@font-face{font-family:'%s';src:url('/fonts/%s') format('woff2');font-weight:%d;font-display:swap}" % (name, url.rsplit("/", 1)[1], w))
            if len(f["files"]) == 1:  # no bold file: let the browser draw bold from the regular one
                css.append("@font-face{font-family:'%s';src:url('/fonts/%s') format('woff2');font-weight:700;font-display:swap}" % (name, f["files"][0][0].rsplit("/", 1)[1]))
    for x in user_fonts(m) if m else []:
        ext = x["file"].rsplit(".", 1)[-1]
        css.append("@font-face{font-family:'%s';src:url('/fonts/%s') format('%s');font-display:swap}" % (
            x["name"].replace("'", ""), x["file"], {"woff2": "woff2", "woff": "woff", "ttf": "truetype", "otf": "opentype"}[ext]))
    return "\n".join(css)


def fonts_js(m):
    return "var DLUS_FONTS = %s;\n" % json.dumps([[n, n] for n in font_names(m)] + [["system", "System font"]])


def styled_ass(vtt_path, style, delay):
    """subtitle as ASS with the chosen font, colour, size, outline/box and position (for the TV's own player)"""
    font = style.get("font") if style.get("font") in FONTS else "Vazirmatn"
    key = hashlib.sha1(json.dumps([vtt_path, os.path.getmtime(vtt_path), style, delay], sort_keys=True).encode(), usedforsecurity=False).hexdigest()[:12]
    out = os.path.join(os.path.dirname(vtt_path), "styled-%s.ass" % key)
    if os.path.exists(out):
        return out, font
    c = (style.get("color") or "#ffffff").lstrip("#")
    col = "&H00%s%s%s" % (c[4:6], c[2:4], c[0:2]) if len(c) == 6 else "&H00FFFFFF"
    bg = style.get("bg") or "shadow"
    bs, ol, sh, back = {"box": (3, 2, 0, "&H30000000"), "soft": (3, 2, 0, "&H90000000"), "none": (1, 0, 0, "&H00000000")}.get(bg, (1, 3.2, 1.2, "&H80000000"))
    fs = round(float(style.get("size") or 5.2) * 1080 / 100 * 1.08)
    mv = round(float(style.get("pos") or 7) * 1080 / 100)
    bold = -1 if style.get("bold", True) else 0

    def ts(x):
        x = max(0.0, x)
        return "%d:%02d:%02d.%02d" % (x // 3600, x % 3600 // 60, x % 60, round(x % 1 * 100) % 100)
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
             "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
             "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             "Style: Default,%s,%d,%s,&H000000FF,&H00000000,%s,%d,0,0,0,100,100,0,0,%d,%s,%s,2,90,90,%d,1" % (font, fs, col, back, bold, bs, ol, sh, mv),
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for blk in re.split(r"\n{2,}", rd(vtt_path).replace("\r", "")):
        L = blk.split("\n")
        k = next((i for i, l in enumerate(L) if "-->" in l), None)
        if k is None:
            continue
        mm = re.match(r"(?:(\d+):)?(\d+):(\d\d)[.,](\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d\d)[.,](\d+)", L[k].strip())
        if not mm:
            continue
        g = mm.groups()
        a = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2]) + int((g[3] + "00")[:3]) / 1000 + delay
        b = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6]) + int((g[7] + "00")[:3]) / 1000 + delay
        txt = "\\N".join(re.sub(r"<[^>]+>", "", l).strip() for l in L[k + 1:] if l.strip())
        if txt and b > 0:
            lines.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (ts(a), ts(b), txt.replace("{", "(").replace("}", ")")))
    wr(out, "\n".join(lines) + "\n")
    return out, font


def short_code(n=6):
    return "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(n))


def in_window(a, b):
    now = time.strftime("%H:%M")
    return a <= now < b if a <= b else now >= a or now < b


# ---------------------------------------------------------------- network info
class Net:
    VPN_APPS = {"V2Box": "V2Box", "netbird": "NetBird", "netbird-ui": "NetBird", "WireGuard": "WireGuard",
                "openvpn": "OpenVPN", "OpenVPN Connect": "OpenVPN", "Tailscale": "Tailscale", "tailscaled": "Tailscale",
                "Cisco Secure Client": "Cisco", "FortiClient": "FortiClient", "ProtonVPN": "ProtonVPN",
                "NordVPN": "NordVPN", "ExpressVPN": "ExpressVPN", "Mullvad VPN": "Mullvad", "v2rayN": "v2rayN",
                "v2ray": "v2ray", "xray": "Xray", "sing-box": "sing-box", "clash": "Clash", "Clash Verge": "Clash Verge",
                "mihomo": "Clash", "Hiddify": "Hiddify", "HiddifyNext": "Hiddify", "Outline": "Outline",
                "Psiphon": "Psiphon", "Shadowrocket": "Shadowrocket", "FoXray": "FoXray", "Streisand": "Streisand",
                "Windscribe": "Windscribe", "Tunnelblick": "Tunnelblick (OpenVPN)", "Viscosity": "Viscosity (OpenVPN)",
                "vpnagentd": "Cisco AnyConnect", "Cisco AnyConnect Secure Mobility Client": "Cisco AnyConnect",
                "Surfshark": "Surfshark", "Private Internet Access": "PIA", "pia-daemon": "PIA", "CyberGhost VPN": "CyberGhost",
                "TunnelBear": "TunnelBear", "Pritunl": "Pritunl", "Cloudflare WARP": "Cloudflare WARP", "warp-svc": "Cloudflare WARP",
                "GlobalProtect": "GlobalProtect", "PanGPS": "GlobalProtect", "IVPN": "IVPN", "AmneziaVPN": "AmneziaVPN"}
    TUN = re.compile(r"^(utun|ipsec|ppp|tun|tap|wg)\d*$")
    PROVIDERS = (("wireguard", "WireGuard"), ("v2box", "V2Box"), ("netbird", "NetBird"), ("tailscale", "Tailscale"),
                 ("openvpn", "OpenVPN"), ("fortinet", "FortiClient"), ("cisco", "Cisco"), ("outline", "Outline"),
                 ("proton", "ProtonVPN"), ("nord", "NordVPN"), ("expressvpn", "ExpressVPN"), ("mullvad", "Mullvad"),
                 ("sshproxy", "SSH tunnel"), ("hiddify", "Hiddify"), ("foxray", "FoXray"), ("shadowrocket", "Shadowrocket"),
                 ("streisand", "Streisand"), ("clash", "Clash"), ("sing-box", "sing-box"), ("psiphon", "Psiphon"),
                 ("windscribe", "Windscribe"), ("tunnelblick", "Tunnelblick"), ("anyconnect", "Cisco AnyConnect"), ("surfshark", "Surfshark"),
                 ("privateinternetaccess", "PIA"), ("cloudflare", "Cloudflare WARP"), ("paloalto", "GlobalProtect"), ("amnezia", "AmneziaVPN"))

    def __init__(self):
        self.info = {"online": False, "iface": "", "kind": "", "ssid": "", "local_ip": "", "gateway": "",
                     "public_ip": "", "rx": 0, "tx": 0, "vpn": False, "full_tunnel": False, "tunnels": [], "apps": [],
                     "services": []}
        self.prev = None
        self.t_slow = self.t_ip = 0
        self.ports = {}

    def loop(self):
        while True:
            try:
                now = time.time()
                if now - self.t_slow > 10:
                    self.slow()
                    self.t_slow = now
                if now - self.t_ip > 120:
                    self.t_ip = now
                    threading.Thread(target=self.pubip, daemon=True).start()
                self.usage()
            except Exception:
                log.exception("net")
            time.sleep(2)

    def slow(self):
        i = self.info
        rt = sh(["route", "-n", "get", "default"])
        m = re.search(r"interface:\s*(\S+)", rt)
        g = re.search(r"gateway:\s*(\S+)", rt)
        i["online"] = bool(m)
        i["route_iface"] = m.group(1) if m else ""
        i["gateway"] = g.group(1) if g else ""
        if not self.ports:
            for blk in sh(["networksetup", "-listallhardwareports"]).split("\n\n"):
                pm, dm = re.search(r"Hardware Port: (.+)", blk), re.search(r"Device: (\S+)", blk)
                if pm and dm:
                    self.ports[dm.group(1)] = pm.group(1)
        ifs, cur = {}, None
        for l in sh(["ifconfig"]).splitlines():
            mm = re.match(r"^(\S+?):\s+flags=\d+<([^>]*)>", l)
            if mm:
                cur = mm.group(1)
                ifs[cur] = {"up": "UP" in mm.group(2).split(","), "inet": [], "active": False}
            elif cur and l.strip().startswith("inet "):
                ifs[cur]["inet"].append(l.split()[1])
            elif cur and "status: active" in l:
                ifs[cur]["active"] = True
        tun = [(n, d["inet"][0]) for n, d in ifs.items() if self.TUN.match(n) and d["up"] and d["inet"]]
        i["tunnels"] = [{"if": n, "ip": ip, "kind": self.tun_kind(ip)} for n, ip in tun]
        i["full_tunnel"] = bool(self.TUN.match(i["route_iface"]))
        phys = i["route_iface"]
        if i["full_tunnel"] or not phys:
            phys = next((n for n, d in ifs.items() if n.startswith("en") and d["inet"] and d["active"]), phys)
        i["iface"] = phys
        i["kind"] = self.ports.get(phys, phys)
        i["local_ip"] = (ifs.get(phys) or {}).get("inet", [""])[0] if phys in ifs and ifs[phys]["inet"] else ""
        i["ssid"] = ""
        if "wi-fi" in i["kind"].lower():
            mm = re.search(r"^\s*SSID : (.+)$", sh(["ipconfig", "getsummary", phys]), re.M)
            i["ssid"] = mm.group(1).strip() if mm else ""
        procs = set(sh(["ps", "-axco", "comm"]).splitlines())
        i["apps"] = sorted({v for k, v in self.VPN_APPS.items() if k in procs})
        # VPN profiles known to macOS (WireGuard, V2Box, IKEv2, ...) with their real state
        vpns = []
        for l in sh(["scutil", "--nc", "list"]).splitlines():
            mm = re.search(r'\((\w+)\)\s+[0-9A-F-]{36}\s+(.+?)\s+"([^"]*)"\s+\[([^\]]*)\]', l)
            if mm:
                prov = mm.group(4).split(":", 1)[-1]
                app = next((v for k, v in self.PROVIDERS if k in prov.lower()), prov.split(".")[-1] or mm.group(2))
                vpns.append({"name": mm.group(3), "app": app, "status": mm.group(1), "type": mm.group(2).split(" ")[0]})
        # NetBird runs its own daemon (not a macOS VPN profile): ask its CLI
        if shutil.which("netbird"):
            nb = sh(["netbird", "status"], timeout=4)
            mm = re.search(r"NetBird IP:\s*(\S+)", nb)
            ip = mm.group(1).split("/")[0] if mm and mm.group(1) != "N/A" else ""
            vpns.append({"name": (re.search(r"Profile:\s*(\S+)", nb) or [None, "NetBird"])[1], "app": "NetBird",
                         "status": "Connected" if "Management: Connected" in nb else "Disconnected", "type": "mesh", "ip": ip})
            for t in i["tunnels"]:
                if ip and t["ip"] == ip:
                    t["kind"] = "NetBird"
        i["vpns"] = vpns
        i["services"] = [v["app"] + " (" + v["name"] + ")" for v in vpns if v["status"] == "Connected"]
        # system proxy (Safari/Chrome use it; curl - so dlus downloads - does NOT)
        px, kv = {}, dict(re.findall(r"^\s*(\w+)\s*:\s*(.+?)\s*$", sh(["scutil", "--proxy"]), re.M))
        for k, name in (("HTTP", "HTTP"), ("HTTPS", "HTTPS"), ("SOCKS", "SOCKS")):
            if kv.get(k + "Enable") == "1":
                px[name] = "%s:%s" % (kv.get(k + "Proxy", "?"), kv.get(k + "Port", "?"))
        if kv.get("ProxyAutoConfigEnable") == "1":
            px["PAC"] = kv.get("ProxyAutoConfigURLString", "auto config")
        i["proxy"] = px
        i["env_proxy"] = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
        i["vpn"] = bool(i["tunnels"] or i["services"])

    @staticmethod
    def tun_kind(ip):
        a = ipaddress.IPv4Address(ip)
        if a in ipaddress.IPv4Network("100.64.0.0/10"):
            return "mesh VPN (NetBird / Tailscale)"
        if a in ipaddress.IPv4Network("198.18.0.0/15") or a in ipaddress.IPv4Network("172.19.0.0/30"):
            return "proxy tunnel (V2Box / Clash / sing-box)"
        return "VPN tunnel"

    def usage(self):
        ifc = self.info.get("iface")
        if not ifc:
            return
        for l in sh(["netstat", "-ibn", "-I", ifc]).splitlines():
            c = l.split()
            if len(c) >= 10 and c[0] == ifc and c[2].startswith("<Link"):
                rx, tx, t = int(c[-5]), int(c[-2]), time.time()
                if self.prev and self.prev[0] == ifc and t > self.prev[3]:
                    dt = t - self.prev[3]
                    self.info["rx"] = max(0, int((rx - self.prev[1]) / dt))
                    self.info["tx"] = max(0, int((tx - self.prev[2]) / dt))
                self.prev = (ifc, rx, tx, t)
                return

    def pubip(self):
        for u in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"):
            try:
                # security scan: fixed https URLs
                ip = urllib.request.urlopen(u, timeout=6).read().decode().strip()  # nosec B310
                ipaddress.ip_address(ip)
                self.info["public_ip"] = ip
                self.info["public_ip_t"] = time.time()
                return
            except Exception:
                continue
        self.info["public_ip"] = ""


# ---------------------------------------------------------------- QR codes (for phones / TV cameras)
QR_EC = {1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]), 6: (16, [(4, 27)]),
         7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)])}
QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_rem(data, deg):
    div, root = [0] * (deg - 1) + [1], 1
    for _ in range(deg):
        for j in range(deg):
            div[j] = _gf_mul(div[j], root)
            if j + 1 < deg:
                div[j] ^= div[j + 1]
        root = _gf_mul(root, 2)
    res = [0] * deg
    for b in data:
        f = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(div):
            res[i] ^= _gf_mul(c, f)
    return res


def qr_matrix(text):
    data = text.encode("utf-8")
    for ver in range(1, 11):
        ec, groups = QR_EC[ver]
        cap = sum(n * k for n, k in groups)
        if 4 + (8 if ver < 10 else 16) + 8 * len(data) <= cap * 8:
            break
    else:
        raise ValueError("text too long for a QR code")
    bits = []
    app = lambda v, n: bits.extend((v >> i) & 1 for i in range(n - 1, -1, -1))
    app(4, 4)
    app(len(data), 8 if ver < 10 else 16)
    for b in data:
        app(b, 8)
    app(0, min(4, cap * 8 - len(bits)))
    app(0, (-len(bits)) % 8)
    k = 0
    while len(bits) < cap * 8:
        app((0xEC, 0x11)[k % 2], 8)
        k += 1
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    blocks, pos = [], 0
    for n, kk in groups:
        for _ in range(n):
            blocks.append(cw[pos:pos + kk])
            pos += kk
    eccs = [_rs_rem(b, ec) for b in blocks]
    out = [b[i] for i in range(max(map(len, blocks))) for b in blocks if i < len(b)] + [e[i] for i in range(ec) for e in eccs]
    size = ver * 4 + 17
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def put(x, y, v):
        mod[y][x] = bool(v)
        fn[y][x] = True
    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    al = QR_ALIGN[ver]
    for i, ax in enumerate(al):
        for j, ay in enumerate(al):
            if (i, j) in ((0, 0), (0, len(al) - 1), (len(al) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def fmt(mask):
        d = (0 << 3) | mask  # EC level M = 00
        r = d
        for _ in range(10):
            r = (r << 1) ^ ((r >> 9) * 0x537)
        b = ((d << 10) | r) ^ 0x5412
        g = lambda i: (b >> i) & 1
        for i in range(6):
            put(8, i, g(i))
        put(8, 7, g(6))
        put(8, 8, g(7))
        put(7, 8, g(8))
        for i in range(9, 15):
            put(14 - i, 8, g(i))
        for i in range(8):
            put(size - 1 - i, 8, g(i))
        for i in range(8, 15):
            put(8, size - 15 + i, g(i))
        put(8, size - 8, 1)
    fmt(0)
    if ver >= 7:
        r = ver
        for _ in range(12):
            r = (r << 1) ^ ((r >> 11) * 0x1F25)
        vb = (ver << 12) | r
        for i in range(18):
            b = (vb >> i) & 1
            put(size - 11 + i % 3, i // 3, b)
            put(i // 3, size - 11 + i % 3, b)
    i, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fn[y][x] and i < len(out) * 8:
                    mod[y][x] = bool((out[i >> 3] >> (7 - (i & 7))) & 1)
                    i += 1
        right -= 2
    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
             lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
             lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def penalty(g):
        p = 0
        lines = g + [list(c) for c in zip(*g)]
        for row in lines:
            run, prev = 0, None
            for v in row + [None]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += 3 + run - 5
                    run, prev = 1, v
            sr = "".join("1" if v else "0" for v in row)
            p += 40 * (sr.count("10111010000") + sr.count("00001011101"))
        for y in range(size - 1):
            for x in range(size - 1):
                if g[y][x] == g[y][x + 1] == g[y + 1][x] == g[y + 1][x + 1]:
                    p += 3
        dark = sum(map(sum, g))
        p += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
        return p
    best = None
    for mk in range(8):
        fmt(mk)
        g = [[mod[y][x] ^ (not fn[y][x] and masks[mk](x, y)) for x in range(size)] for y in range(size)]
        sc = penalty(g)
        if best is None or sc < best[0]:
            best = (sc, mk, g)
    fmt(best[1])
    return [[mod[y][x] if fn[y][x] else best[2][y][x] for x in range(size)] for y in range(size)]


def qr_svg(text, dark="#000", light="#fff"):
    g = qr_matrix(text)
    n = len(g) + 8
    path = "".join("M%d %dh1v1h-1z" % (x + 4, y + 4) for y, row in enumerate(g) for x, v in enumerate(row) if v)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges"><rect width="%d" height="%d" fill="%s"/>'
            '<path d="%s" fill="%s"/></svg>' % (n, n, n, n, light, path, dark))


# ---------------------------------------------------------------- proxy sharing (like Privoxy)
def proxy_kind(host, port):
    """'socks5' / 'http' / '' for a local proxy port"""
    try:
        with socket.create_connection((host, port), 2) as c:
            c.sendall(b"\x05\x01\x00")
            c.settimeout(2)
            if c.recv(2) == b"\x05\x00":
                return "socks5"
    except OSError:
        pass
    try:
        with socket.create_connection((host, port), 2) as c:
            c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            c.settimeout(3)
            if c.recv(64).startswith(b"HTTP/1."):
                return "http"
    except OSError:
        pass
    return ""


def open_upstream(up, host, port):
    """TCP connection to host:port, directly or through a local SOCKS5 / HTTP proxy (the Mac's tunnel)"""
    if up == "direct":
        return socket.create_connection((host, port), 15)
    kind, addr = up.split("://", 1)
    ph, pp = addr.rsplit(":", 1)
    c = socket.create_connection((ph, int(pp)), 10)
    c.settimeout(20)
    if kind == "socks5":
        c.sendall(b"\x05\x01\x00")
        if c.recv(2) != b"\x05\x00":
            raise OSError("SOCKS proxy refused")
        h = host.encode("idna")
        c.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
        rep = c.recv(4)
        if len(rep) < 4 or rep[1] != 0:
            raise OSError("SOCKS connect failed (%s)" % (rep[1] if len(rep) > 1 else "?"))
        c.recv({1: 4, 3: c.recv(1)[0] if rep[3] == 3 else 0, 4: 16}.get(rep[3], 0) + 2)
    else:
        c.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (host, port, host, port)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = c.recv(4096)
            if not d:
                raise OSError("HTTP proxy closed")
            buf += d
        if not re.match(rb"HTTP/1\.\d 200", buf):
            raise OSError("HTTP proxy refused: " + buf.split(b"\r\n")[0].decode(errors="ignore"))
    c.settimeout(None)
    return c


class ProxyServer:
    """HTTP/HTTPS proxy for other devices on the local network; traffic leaves through this Mac (and its tunnel)"""

    def __init__(self, m):
        self.m = m
        self.clients = {}
        self.rates = collections.deque(maxlen=600)
        self.lock = threading.Lock()
        self.prev = 0
        own = set()
        for l in sh(["ifconfig"]).splitlines():
            if l.strip().startswith("inet "):
                own.add(l.split()[1])
        # security scan: list of this Mac's own addresses, not a bind
        self.own = own | {"127.0.0.1", "0.0.0.0", "localhost", "::1"}  # nosec B104
        srv = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                srv.handle(self.request, self.client_address[0])

        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
        # security scan: list of this Mac's own addresses, not a bind
        self.srv = S(("0.0.0.0", int(m.settings["proxy_port"])), H)  # nosec B104
        threading.Thread(target=self.srv.serve_forever, daemon=True, name="proxy").start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def own_ips(self):
        """this Mac's addresses, refreshed every 30 s (they change with the network)"""
        if time.time() - getattr(self, "own_t", 0) > 30:
            own = set()
            for l in sh(["ifconfig"]).splitlines():
                if l.strip().startswith(("inet ", "inet6 ")):
                    own.add(l.split()[1].split("%")[0])
            # security scan: proxy for the home network, only when the user turns it on; private addresses only
            self.own, self.own_t = own | {"127.0.0.1", "0.0.0.0", "localhost", "::1"}, time.time()  # nosec B104
        return self.own

    @staticmethod
    def local_dest(a):
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_unspecified or ip.is_multicast

    def client(self, ip):
        with self.lock:
            c = self.clients.get(ip)
            if not c:
                c = self.clients[ip] = {"ip": ip, "up": 0, "down": 0, "conns": 0, "total": 0, "host": "", "first": time.time(),
                                        "last": time.time(), "rates": collections.deque(maxlen=120), "prev": 0}
                self.m.sessions.host_info(ip)
            return c

    def handle(self, conn, ip):
        try:
            a = ipaddress.ip_address(ip)
            if not (a.is_private or a.is_loopback or a.is_link_local):
                return
        except ValueError:
            return
        conn.settimeout(30)
        buf = b""
        try:
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                d = conn.recv(8192)
                if not d:
                    return
                buf += d
        except OSError:
            return
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, ver = lines[0].split(" ", 2)
        except ValueError:
            return
        hdrs = {}
        for l in lines[1:]:
            k, _, v = l.partition(":")
            hdrs[k.strip().lower()] = v.strip()
        st = self.m.settings
        if st.get("proxy_auth"):
            want = "Basic " + base64.b64encode(("%s:%s" % (st["proxy_user"], st["proxy_pass"])).encode()).decode()
            if not secrets.compare_digest(hdrs.get("proxy-authorization", "").encode(), want.encode()):
                conn.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="dlus"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                return
        if not self.m.lan_access(ip, hdrs.get("user-agent", ""), "used the web proxy"):
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nWaiting for approval in dlus on the Mac.")
            return
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port or 443)
        else:
            u = urllib.parse.urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            host, port = u.hostname, u.port or 80
        host = host.strip("[]")
        if not (0 < port < 65536) or not re.match(r"^[\w.\-:]{1,253}$", host):
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        upstream = self.m.upstream()
        try:  # never let other devices reach this Mac itself (control panel, other local services)
            dest = socket.gethostbyname(host)
        except OSError:
            dest = host
        if host.lower() in self.own_ips() or dest in self.own_ips() or self.local_dest(dest):
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        c = self.client(ip)
        try:
            # direct: connect to the address that was checked, so a second DNS answer cannot point at this Mac (DNS rebinding)
            up = open_upstream(upstream, dest if upstream == "direct" else host, port)
        except OSError as e:
            conn.sendall(("HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\ndlus proxy: %s" % e).encode())
            return
        with self.lock:
            c["conns"] += 1
            c["total"] += 1
            c["host"] = host
            c["last"] = time.time()
        try:
            if method == "CONNECT":
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    up.sendall(rest)
            else:
                u = urllib.parse.urlsplit(target)
                path = (u.path or "/") + ("?" + u.query if u.query else "")
                keep = [l for l in lines[1:] if not l.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
                req = "%s %s %s\r\n%s\r\nConnection: close\r\n\r\n" % (method, path, ver, "\r\n".join(keep))
                up.sendall(req.encode("latin-1") + rest)
                c["up"] += len(req) + len(rest)
            self.pump(conn, up, c)
        finally:
            with self.lock:
                c["conns"] -= 1
                c["last"] = time.time()
            up.close()

    def pump(self, a, b, c):
        a.settimeout(None)
        socks = [a, b]
        while True:
            r, _, x = select.select(socks, [], socks, 300)
            if x or not r:
                return
            for s_ in r:
                try:
                    d = s_.recv(65536)
                except OSError:
                    return
                if not d:
                    return
                try:
                    (b if s_ is a else a).sendall(d)
                except OSError:
                    return
                if s_ is a:
                    c["up"] += len(d)
                else:
                    c["down"] += len(d)
                    self.m.count_proxy(len(d))

    def tick(self):
        tot = 0
        with self.lock:
            for c in self.clients.values():
                n = c["up"] + c["down"]
                c["rates"].append(max(0, n - c["prev"]))
                c["prev"] = n
                tot += c["rates"][-1]
        self.rates.append(tot * 2)  # tick runs every 0.5 s

    def stats(self):
        with self.lock:
            cl = [{k: v for k, v in c.items() if k not in ("rates", "prev")} | {"rate": (c["rates"][-1] * 2 if c["rates"] else 0),
                  "device": self.m.sessions.seen.get(c["ip"], {}).get("device", ""), **self.m.sessions.hosts.get(c["ip"], {})}
                  for c in self.clients.values()]
        return {"clients": sorted(cl, key=lambda x: -x["last"]), "rates": list(self.rates)}


def send_file(h, path):
    """plain file download with range support (file sharing)"""
    L = fsize(path)
    a, b, partial = 0, max(0, L - 1), False
    mm = re.match(r"bytes=(\d*)-(\d*)", h.headers.get("Range", ""))
    if mm and (mm.group(1) or mm.group(2)) and L:
        partial = True
        a = int(mm.group(1)) if mm.group(1) else max(0, L - int(mm.group(2)))
        b = min(int(mm.group(2)), L - 1) if mm.group(1) and mm.group(2) else L - 1
    h.send_response(206 if partial else 200)
    h.send_header("Content-Type", MIME.get(path.rsplit(".", 1)[-1].lower(), "application/octet-stream"))
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(b - a + 1 if L else 0))
    h.send_header("Content-Disposition", "inline; filename*=UTF-8''" + urllib.parse.quote(os.path.basename(path)))
    if partial:
        h.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, L))
    h.end_headers()
    if h.command == "HEAD" or not L:
        return
    try:
        with open(path, "rb") as f:
            f.seek(a)
            left = b - a + 1
            while left > 0:
                d = f.read(min(left, 1 << 20))
                if not d:
                    break
                h.wfile.write(d)
                left -= len(d)
    except (BrokenPipeError, ConnectionResetError):
        pass


def files_page(m, rel):
    """read-only file browser for the local network: dlus downloads + the download folder"""
    tok = m.settings["lan_token"]
    base = os.path.realpath(os.path.expanduser(m.settings["base_dir"]))
    rows, title = [], "dlus files"
    icon = lambda c: {"Video": "&#127916;", "Audio": "&#127925;", "Images": "&#128444;", "Archives": "&#128230;", "Documents": "&#128196;",
                      "Ebooks": "&#128214;", "Programs": "&#128187;", "Subtitles": "&#128172;", "Disk images": "&#128191;"}.get(c, "&#128196;")
    if not rel:
        for d in list(m.dls.values()):
            r = d.r
            if not r.get("size") or not (r["status"] == "done" or d.snap.get("prefix")):
                continue
            cat = category(r["name"])
            play = "<a class=pl href='/%s/%d'>&#9654; play</a>" % (tok, r["num"]) if cat in ("Video", "Audio") else ""
            rows.append((cat, "<tr><td>%s</td><td><a href='/%s/%s'>%s</a> %s</td><td>%s</td><td>%s</td></tr>" % (
                icon(cat), tok, m.short_path(d), xesc(r["name"]), play, cat,
                "%.2f GB" % (r["size"] / 2 ** 30) + ("" if r["status"] == "done" else " (downloading %d%%)" % (r["done"] * 100 // r["size"])))))
        rows.sort()
        body = "<h2>Downloads</h2><table>%s</table>" % "".join(x[1] for x in rows)
        if os.path.isdir(base):
            body += "<h2>Download folder</h2><p><a href='/%s/files/dir/'>&#128193; %s</a></p>" % (tok, xesc(base.replace(os.path.expanduser("~"), "~")))
        return title, body
    path = safe_join(base, urllib.parse.unquote(rel))
    if not path or not os.path.exists(path) or hidden_inside(base, path):
        return None, None
    if os.path.isfile(path):
        return "file", path
    ents = []
    try:
        names = list_dir(path)
    except OSError:
        names = []
    for n in names:
        if n.startswith(".") or n.endswith((".parts", ".part", ".assembling")):
            continue
        fp = os.path.join(path, n)
        relp = os.path.relpath(fp, base)
        if os.path.isdir(fp):
            ents.append("<tr><td>&#128193;</td><td><a href='/%s/files/dir/%s/'>%s</a></td><td>folder</td><td></td></tr>" % (tok, urllib.parse.quote(relp), xesc(n)))
        else:
            ents.append("<tr><td>%s</td><td><a href='/%s/files/dir/%s'>%s</a></td><td>%s</td><td>%.1f MB</td></tr>" % (
                icon(category(n)), tok, urllib.parse.quote(relp), xesc(n), category(n), fsize(fp) / 2 ** 20))
    up = "/%s/files/" % tok if path == base else "/%s/files/dir/%s/" % (tok, urllib.parse.quote(os.path.relpath(os.path.dirname(path), base)))
    return xesc(os.path.basename(path) or "Download folder"), "<p><a href='%s'>&larr; back</a></p><table>%s</table>" % (up.replace("/dir/./", "/dir/"), "".join(ents) or "<tr><td>empty</td></tr>")


PIN_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>dlus</title>
<style>html,body{margin:0;height:100%;background:#09090b;color:#fff;font-family:"LG Smart UI",Roboto,-apple-system,sans-serif}
.c{height:100%;display:grid;place-items:center;text-align:center}.logo{display:block;width:120px;height:120px;border-radius:34px;margin:0 auto 26px;box-shadow:0 18px 60px rgba(109,94,252,.45)}
h1{font-size:46px;margin:0 0 10px}p{font-size:26px;color:#a1a1aa;margin:0 0 34px}
input{font:600 56px ui-monospace,Menlo,monospace;letter-spacing:.35em;text-align:center;width:420px;padding:18px 10px;border-radius:20px;border:3px solid #333;background:#16161a;color:#fff;outline:none;text-transform:lowercase}
input:focus{border-color:#6d5efc}button{display:block;margin:28px auto 0;font:600 30px sans-serif;padding:16px 60px;border-radius:16px;border:0;background:#6d5efc;color:#fff}button:focus{outline:4px solid #fff}
#e{color:#ff6b6b;font-size:26px;height:34px;margin-top:20px}</style></head><body><div class=c><div>
<img class=logo src="/icon-192.png" alt=""><h1>dlus</h1><p>Enter the 6-character code shown in dlus on the Mac<br>(VPN router &amp; TV page &rarr; &ldquo;Code for the TV&rdquo;)</p>
<input id=c maxlength=6 autocomplete=off autofocus><button id=b>Connect</button><div id=e></div></div></div>
<script>
var c = document.getElementById("c"), saved = localStorage.getItem("dlus-code");
function go(code) {
  var x = new XMLHttpRequest(); x.open("GET", "/pin?c=" + encodeURIComponent(code), true);
  x.onload = function () { var r = {}; try { r = JSON.parse(x.responseText) } catch (e) {}
    if (r.ok) { localStorage.setItem("dlus-code", code); location.href = r.home } else { localStorage.removeItem("dlus-code"); document.getElementById("e").textContent = r.error || "wrong code"; c.value = ""; c.focus() } };
  x.send();
}
if (saved) go(saved);
document.getElementById("b").onclick = function () { go(c.value.trim().toLowerCase()) };
c.addEventListener("keydown", function (e) { if (e.keyCode == 13) go(c.value.trim().toLowerCase()) });
</script></body></html>"""


def net_setup_page(m, ip):
    v = m.gw_values(ip)
    rows = "".join("<div class=v><span>%s</span><b>%s</b></div>" % (k, xesc(x)) for k, x in
                   (("IP address", v["ip"]), ("Subnet mask", v["mask"]), ("Gateway", v["gw"]), ("DNS", v["dns"])))
    return NET_PAGE.replace("__ROWS__", rows)


NET_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>dlus - VPN setup</title>
<style>html,body{margin:0;height:100%;background:radial-gradient(1200px 700px at 15% 0%,#2a2160,#09090b 60%);color:#fff;font-family:Vazirmatn,"LG Smart UI",Roboto,sans-serif}
.w{display:grid;grid-template-columns:1.1fr 1fr;gap:60px;padding:70px 90px}h1{font-size:52px;margin:18px 0 8px;letter-spacing:-.02em}p{font-size:24px;color:#a1a1b5;margin:0 0 26px;line-height:1.5}
.lg{width:72px;height:72px;border-radius:22px}ol{font-size:26px;line-height:1.75;padding-left:34px;margin:0}ol b{color:#c4bcff}
.v{display:flex;justify-content:space-between;align-items:center;padding:22px 30px;margin-bottom:14px;border-radius:24px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);font-size:26px}
.v span{color:#a1a1b5}.v b{font:700 38px ui-monospace,Menlo,monospace;letter-spacing:.02em}
.ck{display:flex;gap:16px;align-items:center;font-size:24px;padding:10px 0}.ck i{width:30px;height:30px;border-radius:50%;flex:none;background:#3a3a46;display:grid;place-items:center;font-style:normal;font-size:18px}
.ck.ok i{background:#22c55e}.ck.no{color:#a1a1b5}h2{font-size:28px;margin:36px 0 10px}#big{font-size:30px;font-weight:800;margin-top:10px}</style></head><body>
<div class=w><div><img class=lg src="/icon-192.png" alt=""><h1>Connect this TV to the VPN</h1><p>Once only. The TV then sends its internet through the Mac's VPN; dlus checks it for you.</p>
<ol><li>Remote: <b>Settings</b> &rarr; <b>General</b> &rarr; <b>Network</b> &rarr; <b>Wi-Fi Connection</b></li><li><b>Advanced Wi-Fi Settings</b> &rarr; <b>Edit</b></li>
<li>Untick <b>Set Automatically</b>, enter the values on the right, <b>Connect</b></li><li>Come back here (or open YouTube) &mdash; the checks turn green</li></ol>
<h2>Live check</h2><div id=cks></div><div id=big></div></div><div>__ROWS__<p style="margin-top:20px">dlus also shows these values as a message on the TV while its network settings are open.</p></div></div>
<script>
var ms = 0;
function test() { var t0 = +new Date(), im = new Image(), done = false;
  im.onload = function () { if (!done) { done = true; rep(1, +new Date() - t0) } }; im.onerror = function () { if (!done) { done = true; rep(0, 0) } };
  setTimeout(function () { if (!done) { done = true; rep(0, 0) } }, 8000); im.src = "https://www.youtube.com/favicon.ico?" + t0 }
function rep(ok, ms) { var x = new XMLHttpRequest(); x.open("GET", "netcheck?ok=" + ok + "&ms=" + ms, true); x.onload = function () { show(x.responseText) }; x.send() }
function show(t) { var r = {}; try { r = JSON.parse(t) } catch (e) { return }
  document.getElementById("cks").innerHTML = (r.checks || []).map(function (c) { return "<div class='ck " + (c.ok ? "ok" : "no") + "'><i>" + (c.ok ? "&#10003;" : "") + "</i>" + (c.ok ? c.t : c.bad) + "</div>" }).join("");
  document.getElementById("big").textContent = r.done ? "\u2713 Working: this TV goes through the VPN" : "" }
test(); setInterval(test, 6000);
</script></body></html>"""


def tv_home(m):
    tok = m.settings["lan_token"]
    tiles = []
    for d in list(m.dls.values()):
        r = d.r
        cat = category(r["name"])
        if cat not in ("Video", "Audio") or not r.get("size") or not (r["status"] == "done" or d.snap.get("prefix")):
            continue
        ready = 100 if r["status"] == "done" else int(d.snap.get("prefix", 0) * 100 / r["size"])
        tiles.append('<a class=t href="/%s/%d"><div class=ic>%s</div><b>%s</b><span>%s</span><i style="width:%d%%"></i></a>' % (
            tok, r["num"], "&#127916;" if cat == "Video" else "&#127925;", xesc(r["name"].rsplit(".", 1)[0].replace("_", " ").replace(".", " ")),
            "ready to watch" if ready == 100 else "%d%% downloaded &middot; playable now" % ready, ready))
    for i, pth in m.share_dirs():
        tiles.append('<a class="t f" href="/%s/files/sdir/%d/"><div class=ic>&#128193;</div><b>%s</b><span>shared folder</span></a>' % (tok, i, xesc(os.path.basename(pth))))
    return TV_HOME % ("dlus", "".join(tiles) or "<p class=none>Nothing to watch yet - start a download in dlus on the Mac.</p>")


DIR_BLOCKED = ("macOS does not let dlus read this folder. On the Mac: System Settings &rarr; Privacy &amp; Security &rarr; "
               "Files and Folders (or Full Disk Access) &rarr; allow it for the app that started dlus, then restart dlus.")


def list_dir(path):
    """folder entries; PermissionError when macOS privacy protection (TCC) blocks the folder"""
    return sorted(os.listdir(path), key=str.lower)


def tv_folder(m, i, rel):
    tok, base_i = m.settings["lan_token"], dict(m.share_dirs())[i]
    full = m.shared_path(i, rel)
    tiles = []
    if full != base_i:
        up = os.path.relpath(os.path.dirname(full), base_i)
        tiles.append('<a class="t f" href="/%s/files/sdir/%d/%s"><div class=ic>&#11013;</div><b>Back</b><span>%s</span></a>' % (
            tok, i, "" if up == "." else urllib.parse.quote(up) + "/", xesc(os.path.basename(os.path.dirname(full)))))
    try:
        names = list_dir(full)
    except PermissionError:
        m.notify("The TV could not open the shared folder %s: macOS blocks dlus from reading it (Privacy & Security -> Files and Folders)." % os.path.basename(full))
        return TV_HOME % (xesc(os.path.basename(full) or "Shared"), "".join(tiles) + "<p class=none>" + DIR_BLOCKED + "</p>")
    except OSError as e:
        return TV_HOME % (xesc(os.path.basename(full) or "Shared"), "".join(tiles) + "<p class=none>Cannot open this folder: %s</p>" % xesc(str(e)))
    for n in names:
        if n.startswith(".") or n.endswith((".parts", ".part", ".assembling")):
            continue
        fp = os.path.join(full, n)
        r = urllib.parse.quote(os.path.relpath(fp, base_i))
        if os.path.isdir(fp):
            tiles.append('<a class="t f" href="/%s/files/sdir/%d/%s/"><div class=ic>&#128193;</div><b>%s</b><span>folder</span></a>' % (tok, i, r, xesc(n)))
            continue
        cat = category(n)
        href = "/%s/fplay/%d/%s" % (tok, i, r) if cat in ("Video", "Audio") else "/%s/files/sdir/%d/%s" % (tok, i, r)
        tiles.append('<a class=t href="%s"><div class=ic>%s</div><b>%s</b><span>%s &middot; %.1f GB</span></a>' % (
            href, {"Video": "&#127916;", "Audio": "&#127925;", "Images": "&#128444;", "Subtitles": "&#128172;"}.get(cat, "&#128196;"),
            xesc(n), cat.lower(), fsize(fp) / 2 ** 30))
    return TV_HOME % (xesc(os.path.basename(full) or "Shared"), "".join(tiles) or "<p class=none>This folder is empty.</p>")


TV_HOME = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>dlus</title>
<style>html,body{margin:0;background:#09090b;color:#fff;font-family:"LG Smart UI",Roboto,-apple-system,sans-serif;min-height:100%%}
header{display:flex;align-items:center;gap:20px;padding:44px 70px 10px}header .l{width:66px;height:66px;border-radius:20px;box-shadow:0 10px 36px rgba(109,94,252,.45)}
header h1{margin:0;font-size:40px}header span{margin-left:auto;color:#8e8e9a;font-size:24px}
.g{display:grid;grid-template-columns:repeat(4,1fr);gap:26px;padding:30px 70px 60px}
.t{position:relative;display:block;padding:28px;border-radius:26px;background:#16161a;border:3px solid transparent;color:#fff;text-decoration:none;overflow:hidden;min-height:210px;transition:transform .15s}
.t:focus{outline:none;border-color:#6d5efc;transform:scale(1.04);background:#1c1c24}.t .ic{font-size:54px;margin-bottom:16px}
.t b{display:block;font-size:25px;line-height:1.3;max-height:98px;overflow:hidden}.t span{display:block;color:#9a9aa6;font-size:20px;margin-top:10px}
.t i{position:absolute;left:0;bottom:0;height:8px;background:#6d5efc}.f .ic{color:#f5a524}.none{color:#8e8e9a;font-size:28px;grid-column:1/-1}
footer{position:fixed;bottom:22px;left:70px;color:#6b6b78;font-size:20px}</style></head><body>
<header><img class=l src="/icon-192.png" alt=""><h1>%s</h1><span>&#9664; &#9650; &#9660; &#9654; choose &middot; OK open</span></header>
<div class=g id=g>%s</div><footer>Playing from your Mac over the local network</footer>
<script>
var t = [].slice.call(document.querySelectorAll(".t")), i = 0, cols = 4;
if (t.length) t[0].focus();
document.addEventListener("keydown", function (e) {
  var k = e.keyCode, n = i;
  if (k == 37) n = i - 1; else if (k == 39) n = i + 1; else if (k == 38) n = i - cols; else if (k == 40) n = i + cols; else return;
  if (n >= 0 && n < t.length) { i = n; t[i].focus(); t[i].scrollIntoView({ block: "nearest" }) }
  e.preventDefault();
});
t.forEach(function (a, k) { a.addEventListener("focus", function () { i = k }) });
setTimeout(function () { location.reload() }, 60000);
</script></body></html>"""


FILES_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>%s</title>
<style>body{margin:0;padding:24px;font:15px/1.5 -apple-system,Roboto,"Segoe UI",sans-serif;background:#0b0b0f;color:#ececf1}
h1{font-size:22px;margin:0 0 18px;display:flex;align-items:center;gap:12px}h1 span{width:34px;height:34px;border-radius:10px;background:#6d5efc;display:inline-grid;place-items:center;font-size:18px}
h2{font-size:15px;color:#9a9aa6;margin:22px 0 8px}table{width:100%%;border-collapse:collapse}td{padding:10px 8px;border-bottom:1px solid #222230;vertical-align:middle}
td:first-child{width:28px;font-size:18px}td:nth-child(3),td:nth-child(4){color:#9a9aa6;font-size:13px;white-space:nowrap}
a{color:#b9b2ff;text-decoration:none;word-break:break-all}a:hover{text-decoration:underline}.pl{margin-left:10px;padding:2px 10px;border-radius:99px;background:#6d5efc;color:#fff;font-size:12px;white-space:nowrap}
</style></head><body><h1><span>&#8595;</span>%s</h1>%s<p style="color:#666;font-size:12px;margin-top:30px">Shared by dlus &middot; read-only &middot; local network only</p></body></html>"""


# ---------------------------------------------------------------- LG webOS remote control (the API LG's phone app uses)
DLUS_MANIFEST = {"manifestVersion": 1, "appVersion": "1.0", "appId": "com.dlus.remote", "vendorId": "dlus",
                 "localizedAppNames": {"": "dlus"}, "localizedVendorNames": {"": "dlus"},
                 "permissions": ["LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE", "CONTROL_AUDIO", "CONTROL_DISPLAY", "CONTROL_INPUT_MEDIA_PLAYBACK",
                                 "CONTROL_POWER", "CONTROL_INPUT_TV", "CONTROL_INPUT_JOYSTICK", "CONTROL_MOUSE_AND_KEYBOARD", "CONTROL_INPUT_TEXT",
                                 "CONTROL_TV_SCREEN", "CONTROL_TV_STANBY", "WRITE_NOTIFICATION_TOAST", "READ_INSTALLED_APPS", "READ_RUNNING_APPS",
                                 "READ_INPUT_DEVICE_LIST", "READ_TV_CHANNEL_LIST", "READ_CURRENT_CHANNEL", "READ_POWER_STATE", "READ_APP_STATUS",
                                 "READ_NETWORK_STATE", "READ_TV_CURRENT_TIME", "READ_SETTINGS"]}


class TVError(ValueError):
    """the TV answered with an error (e.g. 401 = LG does not give this permission to apps like dlus)"""

    def __str__(self):
        msg = ValueError.__str__(self)
        if "401" in msg or "insufficient" in msg.lower():
            return "the TV does not allow this for remote apps like dlus (%s)" % msg
        return msg


class TVCertChanged(OSError):
    pass


class WebOSTV:
    """minimal WebSocket client for webOS (ws :3000 / wss :3001). Pairing = one 'Allow' on the TV, then a stored key."""

    def __init__(self, ip, key="", pin=""):
        self.ip, self.key, self.n = ip, key or "", 0
        self.pin, self.seen_pin = pin or "", ""  # "tls:<sha256 of the TV's certificate>" or "plain" (old TVs), fixed at pairing
        self.sock, self.rest, self.btn = None, b"", None
        self.lock = threading.Lock()

    def _ws(self, host, port, tls, path="/"):
        s = socket.create_connection((host, port), 6)
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # the TV uses a self-signed certificate: pinned below instead (trust on first use)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            s = ctx.wrap_socket(s, server_hostname=host)
            fp = hashlib.sha256(s.getpeercert(binary_form=True) or b"").hexdigest()
            if self.pin.startswith("tls:") and not secrets.compare_digest(fp, self.pin[4:]):
                s.close()
                raise TVCertChanged("the TV's security certificate is not the one it had when you paired it - someone on the network may be "
                                    "listening in. If you reset or updated the TV, press Forget on the TV page and pair it again.")
            self.seen_pin = "tls:" + fp
        else:
            self.seen_pin = self.seen_pin or "plain"
        k = base64.b64encode(os.urandom(16)).decode()
        s.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, k)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(2048)
            if not c:
                raise OSError("TV closed the connection")
            buf += c
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise OSError("TV refused: " + buf.split(b"\r\n")[0].decode(errors="ignore"))
        return s, buf.split(b"\r\n\r\n", 1)[1]

    @staticmethod
    def _frame(data, op, s):
        n = len(data)
        h = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                   else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        s.sendall(h + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self, timeout):
        self.sock.settimeout(timeout)

        def read(n):
            out = b""
            while len(out) < n:
                if self.rest:
                    take = self.rest[:n - len(out)]
                    self.rest = self.rest[len(take):]
                    out += take
                    continue
                c = self.sock.recv(n - len(out))
                if not c:
                    raise OSError("TV closed the connection")
                out += c
            return out
        while True:
            b0, b1 = read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", read(8))[0]
            mask = read(4) if b1 & 0x80 else None
            data = read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 9:
                self._frame(data, 10, self.sock)
                continue
            if op == 8:
                raise OSError("TV closed the connection")
            if op in (0, 1, 2):
                return json.loads(data.decode("utf-8", "ignore") or "{}")

    def connect(self, wait=60):
        last = None
        # a TV paired over the encrypted port is never contacted unencrypted again (nobody can force a downgrade)
        for port, tls in ((3001, True),) if self.pin.startswith("tls:") else ((3001, True), (3000, False)):
            try:
                self.sock, self.rest = self._ws(self.ip, port, tls)
                break
            except TVCertChanged:
                raise
            except (OSError, ssl.SSLError) as e:
                last = e
        else:
            raise OSError("cannot reach the TV's remote-control service: %s" % last)
        payload = {"forcePairing": False, "pairingType": "PROMPT", "manifest": DLUS_MANIFEST}
        if self.key:
            payload["client-key"] = self.key
        self._frame(json.dumps({"type": "register", "id": "reg0", "payload": payload}).encode(), 1, self.sock)
        end = time.time() + wait
        while time.time() < end:
            try:
                msg = self._recv(max(1, end - time.time()))
            except socket.timeout:
                break
            if msg.get("type") == "registered":
                self.key = (msg.get("payload") or {}).get("client-key", self.key)
                return self.key
            if msg.get("type") == "error":
                raise OSError("TV said: %s" % msg.get("error"))
        raise OSError("no answer - press Allow on the TV when it asks")

    def request(self, uri, payload=None):
        with self.lock:
            self.n += 1
            rid = "r%d" % self.n
            self._frame(json.dumps({"type": "request", "id": rid, "uri": uri, "payload": payload or {}}).encode(), 1, self.sock)
            end = time.time() + 12
            while time.time() < end:
                msg = self._recv(12)
                if msg.get("id") == rid:
                    if msg.get("type") == "error":
                        raise TVError("TV said: %s" % msg.get("error"))
                    return msg.get("payload") or {}
            raise OSError("TV did not answer")

    def button(self, name):
        if not self.btn:
            path = self.request("ssap://com.webos.service.networkinput/getPointerInputSocket").get("socketPath", "")
            u = urllib.parse.urlsplit(path)
            if u.hostname != self.ip:
                raise OSError("the TV gave a pointer address on another device")
            self.btn, _ = self._ws(u.hostname, u.port or (3001 if u.scheme == "wss" else 3000), u.scheme == "wss", u.path)
        try:
            self._frame(("type:button\nname:%s\n\n" % name).encode(), 1, self.btn)
        except OSError:
            self.btn = None
            raise

    def close(self):
        for x in (self.sock, self.btn):
            try:
                x and x.close()
            except OSError:
                pass


# ---------------------------------------------------------------- VPN router (devices use this Mac as their gateway)
GW_PATH = "/Library/PrivilegedHelperTools/dlus-gw"  # root-only folder (helper v3+)
GW_OLD_PATH = "/usr/local/libexec/dlus-gw"  # helper v1-v2: /usr/local can be user-writable (Homebrew on Intel Macs)


def gw_helper():
    if os.environ.get("DLUS_GW_HELPER"):
        return os.environ["DLUS_GW_HELPER"]
    return GW_OLD_PATH if not os.path.exists(GW_PATH) and os.path.exists(GW_OLD_PATH) else GW_PATH


class Gateway:
    """per-device choice: through the VPN tunnel or direct. The devices point their gateway at this Mac
    (set once in their network settings); a root helper (dlus-gw) runs pf + IP forwarding."""

    def __init__(self, m):
        self.m = m
        self.wake = threading.Event()
        self.last_args = None
        self.last_sets = {}
        self.devices = {}
        self.traffic = {}
        self.t_check = 0
        self.st = {"installed": False, "active": False, "error": "", "tun": "-", "tun_name": "", "lan": {}}
        self.vers = (0, 0)
        self.full = ""  # tunnel the "route everything" switch points at (runtime only)
        self.last_result = None  # last install / update / route change, shown under its button

    def call(self, *args, timeout=25):
        cmd = [gw_helper()] + list(args) if os.environ.get("DLUS_GW_HELPER") else ["/usr/bin/sudo", "-n", gw_helper()] + list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def lan(self):
        ifc = self.m.net.info.get("iface") or "en0"
        out = sh(["ifconfig", ifc])
        mm = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask 0x([0-9a-f]{8})", out)
        if not mm:
            return None
        ip = mm.group(1)
        net = ipaddress.IPv4Network("%s/%s" % (ip, ipaddress.IPv4Address(int(mm.group(2), 16))), strict=False)
        gw = re.search(r"gateway:\s*(\S+)", sh(["route", "-n", "get", "-ifscope", ifc, "default"]))
        gw = gw.group(1) if gw else self.m.net.info.get("gateway") or ""
        try:
            ipaddress.IPv4Address(gw)
        except ValueError:
            return None
        return {"if": ifc, "ip": ip, "cidr": str(net), "mask": str(net.netmask), "gw": gw}

    def tunnels(self):
        """VPN tunnels on this Mac that can carry the devices' traffic (mesh VPNs like NetBird excluded)"""
        out = []
        apps = [v["app"] for v in self.m.net.info.get("vpns") or [] if v["status"] == "Connected" and v["app"] != "NetBird"]
        for t in self.m.net.info.get("tunnels") or []:
            if "mesh" in t["kind"] or t["kind"] == "NetBird":
                continue
            out.append({"if": t["if"], "ip": t["ip"], "name": (apps[0] if apps else t["kind"])})
        return out

    def pick_tunnel(self):
        """the VPN tunnel that carries this Mac's internet traffic. Devices are forwarded along the Mac's own
        routes (no pf route-to), so this works with any VPN app - but only one that takes all traffic."""
        via = self.m.net.info.get("route_iface") or ""
        tl = self.tunnels()
        return next((t for t in tl if t["if"] == via), None)

    def split_tunnels(self):
        """VPN tunnels that are up but do not carry the Mac's internet (split tunnel / not full-tunnel mode)"""
        via = self.m.net.info.get("route_iface") or ""
        return [t for t in self.tunnels() if t["if"] != via]

    BUNDLED_GW = os.path.join(APP_DIR, "gateway", "dlus-gw")

    def helper_versions(self):
        mm = re.search(r"^VERSION=(\d+)", rd(self.BUNDLED_GW), re.M)
        want = int(mm.group(1)) if mm else 1
        have = 0
        if os.path.exists(gw_helper()):
            ok, out = self.call("version", timeout=8)
            mm = re.search(r"dlus-gw (\d+)", out) if ok else None
            # helper v1 had no "version" command: it answers with its help text instead
            have = int(mm.group(1)) if mm else (0 if "password" in out.lower() or not out.strip() else 1)
        return have, want

    def scan(self, lan):
        own = lan["ip"]
        tvs = {t["ip"]: t["name"] for t in self.m.settings.get("tvs") or []}
        names = self.m.settings.get("gw_names") or {}
        devs = {}
        for l in sh(["arp", "-an", "-i", lan["if"]]).splitlines():
            mm = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f:]{11,17}) on", l)
            if not mm:
                continue
            ip, mac = mm.group(1), mm.group(2)
            if ip in (own, lan["gw"]) or mac.startswith("ff:ff") or ipaddress.IPv4Address(ip) not in ipaddress.IPv4Network(lan["cidr"]):
                continue
            mac = ":".join(x.zfill(2) for x in mac.split(":"))
            seen = self.m.sessions.seen.get(ip, {})
            host = self.m.sessions.host_info(ip).get("host", "")
            name = (names.get(mac) or tvs.get(ip) or seen.get("device") or (host.split(".")[0] if host else "")
                    or ("Phone / tablet (private address)" if private_mac(mac) else "Device"))
            kind = "tv" if (ip in tvs or "TV" in name) else seen.get("kind") or "computer"
            devs[mac] = {"mac": mac, "ip": ip, "name": name, "kind": kind, "host": host}
        return devs

    MARK = os.path.join(STATE_DIR, "gw-active")

    def crash_guard(self):
        """the Mac crashed (kernel panic) while the router was on -> do not switch it on again by itself"""
        if not os.path.exists(self.MARK):
            return
        since = os.path.getmtime(self.MARK)
        panics = [f for f in glob.glob("/Library/Logs/DiagnosticReports/panic-*.panic") if os.path.getmtime(f) > since]
        os.remove(self.MARK)
        if panics and self.m.settings.get("gw_on"):
            self.m.settings["gw_on"] = False
            self.m.save()
            self.st["error"] = "The Mac restarted after a crash while the router was on - left off for safety. Turn it on again when you want."
            self.m.notify("Your Mac crashed while the VPN router was on. dlus left the router off - see the VPN router page.")

    def loop(self):
        try:
            self.crash_guard()
        except Exception:
            log.exception("gateway crash guard")
        while True:
            try:
                self.sync()
            except Exception:
                log.exception("gateway")
            self.wake.wait(5)
            self.wake.clear()

    def sync(self):
        st, S = self.st, self.m.settings
        if time.time() - self.t_check > 60 or not st["installed"]:
            st["installed"] = os.path.exists(gw_helper()) and self.call("status", timeout=10)[0]
            self.t_check = time.time()
        lan = self.lan()
        st["lan"] = lan or {}
        if not lan:
            return
        self.devices = self.scan(lan)
        known = S.setdefault("known_macs", {})
        for mac, dv in self.devices.items():  # a new device joined the network
            if mac not in known:
                known[mac] = {"first": time.time(), "name": dv["name"]}
                if mac not in (S.get("gw_policy") or {}) and S.get("gw_on"):
                    self.m.gw_new[mac] = dv
                    self.m.notify("New device on your network: %s (%s) - tunnel or direct? Decide in dlus." % (dv["name"], dv["ip"]))
        if not S.get("gw_on"):
            if os.path.exists(self.MARK):
                os.remove(self.MARK)
            if st["active"]:
                self.call("disable")
                self.full = ""
                st["active"], self.last_args, self.last_sets = False, None, {}
            return
        if not st["installed"]:
            st["error"] = "helper not installed"
            return
        tun = self.pick_tunnel()
        st["tun"], st["tun_name"] = (tun["if"], tun["name"]) if tun else ("-", "")
        args = ("enable", lan["if"], lan["cidr"], lan["gw"], st["tun"], "1" if S.get("gw_killswitch", True) else "0")
        if args != self.last_args:
            ok, out = self.call(*args)
            st["error"] = "" if ok else out
            if not ok:
                return
            self.last_args, self.last_sets = args, {}
            st["active"] = True
            with open(self.MARK, "w") as f:
                f.write(str(time.time()))
            log.info("gateway: %s", out)
        pol = S.get("gw_policy") or {}
        want = {"tunnel": [], "direct": []}
        for mac, dv in self.devices.items():
            want[pol.get(mac) or S.get("gw_default") or "direct"].append(dv["ip"])
        moved, before = set(), {k: set(v or []) for k, v in self.last_sets.items()}
        for t, ips in want.items():
            ips = sorted(ips)
            if ips != self.last_sets.get(t):
                ok, out = self.call("set", t, *ips)
                if ok:
                    other = "direct" if t == "tunnel" else "tunnel"
                    moved |= set(ips) & before.get(other, set())  # only devices that switched sides
                    self.last_sets[t] = ips
        if moved:  # a device switched: drop its open connections so they reconnect the new way
            self.call("kill", *sorted(set(moved)))
        ok, out = self.call("status")
        if ok:
            self.parse_status(out)

    USAGE = os.path.join(STATE_DIR, "traffic.json")
    KEEP_DAYS = 62

    def load_usage(self):
        self.usage = load_json(self.USAGE, {}) if not hasattr(self, "usage") else self.usage
        self.conn_bytes, self.t_usage_save = {}, time.time()

    def count_usage(self, ip, n):
        """add n bytes to a device's totals: per day (kept 62 days), split tunnel / direct, plus all-time"""
        dv = next((x for x in self.devices.values() if x["ip"] == ip), None)
        if not dv or n <= 0:
            return
        S = self.m.settings
        way = (S.get("gw_policy") or {}).get(dv["mac"]) or S.get("gw_default") or "direct"
        u = self.usage.setdefault(dv["mac"], {"since": time.time(), "total": 0, "tunnel": 0, "direct": 0, "days": {}})
        day = time.strftime("%Y-%m-%d")
        u["total"] += n
        u[way] = u.get(way, 0) + n
        u["days"][day] = u["days"].get(day, 0) + n
        self.usage_dirty = True

    def save_usage(self, force=False):
        if not getattr(self, "usage_dirty", False) or (not force and time.time() - self.t_usage_save < 60):
            return
        cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - self.KEEP_DAYS * 86400))
        for u in self.usage.values():
            u["days"] = {k: v for k, v in u["days"].items() if k >= cut}
        tmp = self.USAGE + ".new"
        with open(tmp, "w") as f:
            json.dump(self.usage, f)
        os.replace(tmp, self.USAGE)
        self.usage_dirty, self.t_usage_save = False, time.time()

    def usage_view(self, mac):
        u = self.usage.get(mac)
        if not u:
            return None
        today, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
        return {"today": u["days"].get(today, 0), "month": sum(v for k, v in u["days"].items() if k.startswith(month)),
                "total": u["total"], "tunnel": u.get("tunnel", 0), "since": u["since"]}

    def parse_status(self, out):
        now, agg = time.time(), {}
        if not hasattr(self, "conn_bytes"):
            self.load_usage()
        seen = {}
        for l in out.splitlines():
            if " | " not in l or "bytes" not in l:
                continue
            ips = re.findall(r"(\d+\.\d+\.\d+\.\d+):\d+", l)
            b = re.search(r"(\d+):(\d+) bytes", l)
            if not ips or not b:
                continue
            a = agg.setdefault(ips[0], [0, 0])
            a[0] += 1
            cur = int(b.group(1)) + int(b.group(2))
            a[1] += cur
            key = l.split(" | ")[0].strip()  # one connection: count only what is new since the last look
            prev = self.conn_bytes.get(key, 0)
            self.count_usage(ips[0], cur - prev if cur >= prev else cur)
            seen[key] = cur
        self.conn_bytes = seen
        self.save_usage()
        for ip, (conns, total) in agg.items():
            t = self.traffic.setdefault(ip, {"total": 0, "rate": 0, "conns": 0, "prev": (now, total), "last": now})
            pt, pb = t["prev"]
            t["rate"] = max(0, int((total - pb) / (now - pt))) if now > pt and total >= pb else 0
            t.update(total=total, conns=conns, prev=(now, total), last=now)
        for ip, t in self.traffic.items():
            if ip not in agg:
                t.update(conns=0, rate=0)

    def state(self):
        S = self.m.settings
        pol = S.get("gw_policy") or {}
        devs = []
        for mac, dv in sorted(self.devices.items(), key=lambda x: x[1]["ip"]):
            t = self.traffic.get(dv["ip"], {})
            eff = pol.get(mac) or S.get("gw_default") or "direct"
            if not hasattr(self, "usage"):
                self.load_usage()
            devs.append(dict(dv, usage=self.usage_view(mac), policy=pol.get(mac, "default"), effective=eff, routed=time.time() - t.get("last", 0) < 30 and t.get("conns", 0) > 0,
                             total=t.get("total", 0), rate=t.get("rate", 0), conns=t.get("conns", 0), new=mac in self.m.gw_new))
        if time.time() - getattr(self, "t_ver", 0) > 60:
            self.t_ver, self.vers = time.time(), self.helper_versions()
        info = self.m.net.info
        return dict(self.st, on=bool(S.get("gw_on")), default=S.get("gw_default") or "direct", killswitch=bool(S.get("gw_killswitch", True)),
                    tunnels=self.tunnels(), split=self.split_tunnels(), via=info.get("route_iface") or "", full=self.full,
                    vpn_apps=info.get("apps") or [], vpn_profiles=[v for v in info.get("vpns") or [] if v["status"] == "Connected"],
                    devices=devs, helper=gw_helper(), helper_have=self.vers[0], helper_want=self.vers[1],
                    result=self.last_result if self.last_result and time.time() - self.last_result["at"] < 900 else None,
                    install="sudo bash " + shlex.quote(os.path.join(APP_DIR, "gateway", "install.sh")))


# ---------------------------------------------------------------- devices / viewers
DEVICE_RULES = [(r"Web0S|webOS|NetCast|LG Browser|LGE", "LG TV", "tv"), (r"Tizen|SMART-TV|SamsungBrowser.*TV", "Samsung TV", "tv"),
                (r"AppleTV|tvOS", "Apple TV", "tv"), (r"AFT\w+", "Fire TV", "tv"), (r"Android.*(TV|BRAVIA|SHIELD)|GoogleTV|Chromecast", "Android TV", "tv"),
                (r"Roku", "Roku", "tv"), (r"iPhone", "iPhone", "phone"), (r"iPad", "iPad", "phone"), (r"Android", "Android phone", "phone"),
                (r"VLC|LibVLC", "VLC", "player"), (r"Kodi", "Kodi", "player"), (r"Infuse", "Infuse", "player"), (r"Plex", "Plex", "player"),
                (r"IINA|mpv", "IINA / mpv", "player"), (r"Macintosh|Mac OS X", "Mac", "computer"), (r"Windows", "Windows PC", "computer"),
                (r"Linux|X11", "Linux PC", "computer"), (r"DLNA|UPnP", "DLNA player", "tv")]


_OWN = {"t": 0, "ips": set()}


def own_ips():
    """this Mac's own addresses (a request from them is the Mac itself, not another device)"""
    if time.time() - _OWN["t"] > 30:
        _OWN["ips"] = set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", sh(["ifconfig"])))
        _OWN["t"] = time.time()
    return _OWN["ips"]


def device_name(ua, ip):
    name, kind = "Unknown device", "computer"
    for rx, n, k in DEVICE_RULES:
        if re.search(rx, ua or "", re.I):
            name, kind = n, k
            break
    if kind == "computer":
        br = next((b for b in ("Edg", "Firefox", "Chrome", "Safari") if b in (ua or "")), "")
        if br:
            name += " · " + {"Edg": "Edge"}.get(br, br)
    if ip in ("127.0.0.1", "::1") or ip in own_ips():
        name = "This Mac" + (" · " + name.split(" · ")[-1] if " · " in name else "")
        kind = "computer"
    return name, kind


class Sessions:
    """live viewers of streams (any device), a play history and devices that browsed the DLNA library"""
    IDLE = 45

    def __init__(self, m):
        self.m = m
        self.lock = threading.Lock()
        self.live = {}
        self.seen = {}
        self.hosts = {}
        self.rates = collections.deque(maxlen=600)
        self.hist = collections.deque(load_json(os.path.join(STATE_DIR, "sessions.json"), [])[-300:], maxlen=300)

    def host_info(self, ip):
        if ip in self.hosts:
            return self.hosts[ip]
        self.hosts[ip] = {"host": "", "mac": ""}

        def look():
            try:
                self.hosts[ip]["host"] = socket.gethostbyaddr(ip)[0]
            except OSError:
                pass
            if not self.hosts[ip]["host"] or re.match(r"^[\d.]+$", self.hosts[ip]["host"]):
                gw = (self.m.gw.st.get("lan") or {}).get("gw", "") if hasattr(self.m, "gw") else ""
                self.hosts[ip]["host"] = lan_name(ip, gw)
            mm = re.search(r" at ([0-9a-f:]{11,17}) ", sh(["arp", "-n", ip], 3))
            if mm:
                self.hosts[ip]["mac"] = mm.group(1)
        if ip not in ("127.0.0.1", "::1"):
            threading.Thread(target=look, daemon=True).start()
        return self.hosts[ip]

    def begin(self, h, d, variant):
        ip, ua = h.client_address[0], h.headers.get("User-Agent", "")
        if ua.startswith("Lavf") and ip in ("127.0.0.1", "::1"):
            return None  # dlus' own ffmpeg reading the file
        key = (ip, d.r["id"], variant)
        now = time.time()
        with self.lock:
            sx = self.live.get(key)
            if not sx:
                name, kind = device_name(ua, ip)
                sx = self.live[key] = {"ip": ip, "ua": ua[:180], "device": name, "kind": kind, "id": d.r["id"], "title": d.r["name"],
                                       "variant": variant, "start": now, "last": now, "bytes": 0, "reqs": 0, "frac": 0, "active": 0,
                                       "samples": collections.deque(maxlen=12), "rates": collections.deque(maxlen=120)}
                self.host_info(ip)
                d.log("%s (%s) started watching: %s" % (name, ip, variant))
                if ip not in ("127.0.0.1", "::1"):
                    self.m.audit("started watching %s (%s)" % (d.r["name"], variant), ip, name)
            sx["reqs"] += 1
            sx["active"] += 1
            sx["last"] = now
        return sx

    def add(self, sx, n, frac=None):
        if sx:
            sx["bytes"] += n
            sx["last"] = time.time()
            if frac is not None:
                sx["frac"] = frac

    def end(self, sx):
        if sx:
            with self.lock:
                sx["active"] -= 1
                sx["last"] = time.time()

    def saw(self, h, what):
        ip, ua = h.client_address[0], h.headers.get("User-Agent", "")
        name, kind = device_name(ua, ip)
        self.seen[ip] = {"ip": ip, "device": name, "kind": kind, "ua": ua[:180], "what": what, "last": time.time()}
        self.host_info(ip)

    def tick(self):
        now, total, done = time.time(), 0, []
        with self.lock:
            for key, sx in list(self.live.items()):
                sx["samples"].append((now, sx["bytes"]))
                old = sx["samples"][0]
                r = (sx["bytes"] - old[1]) / (now - old[0]) if now > old[0] else 0
                sx["rates"].append(int(r))
                total += r
                if sx["active"] <= 0 and now - sx["last"] > self.IDLE:
                    done.append(key)
            for key in done:
                sx = self.live.pop(key)
                if sx["bytes"] > 256 << 10:
                    self.hist.append({k: sx[k] for k in ("ip", "device", "kind", "title", "variant", "start", "last", "bytes", "frac")}
                                     | {"host": self.hosts.get(sx["ip"], {}).get("host", "")})
        self.rates.append(int(total))
        if done:
            try:
                wr_sync(os.path.join(STATE_DIR, "sessions.json"), json.dumps(list(self.hist)))
            except OSError:
                pass

    def snapshot(self):
        with self.lock:
            live = [{k: v for k, v in sx.items() if k not in ("samples", "rates")} | {"rate": sx["rates"][-1] if sx["rates"] else 0,
                    "rates": list(sx["rates"]), "host": self.hosts.get(sx["ip"], {}).get("host", ""),
                    "mac": self.hosts.get(sx["ip"], {}).get("mac", "")} for sx in self.live.values()]
        seen = sorted(({**x, **self.hosts.get(x["ip"], {})} for x in self.seen.values() if time.time() - x["last"] < 3600),
                      key=lambda x: -x["last"])
        return {"live": sorted(live, key=lambda x: -x["start"]), "hist": list(self.hist)[-60:][::-1], "seen": seen[:20],
                "rates": list(self.rates)}


# ---------------------------------------------------------------- one download
class DL:
    def __init__(self, m, r):
        self.m = m
        self.r = r
        self.lock = threading.Lock()
        self.procs = set()
        self.cproc = {}  # chunk -> curl process
        self.stop_ev = threading.Event()
        self.thread = None
        self.fatal = False
        self.stc = None  # chunk states cache {i: state}
        self.samples = collections.deque(maxlen=130)
        self.hist = collections.deque(maxlen=600)
        self.tmp_prev = None
        self.counted = {}  # in-flight file -> bytes already added to the traffic counter
        self.conn_sp = {}
        self.dirty = True
        self.pending = set()
        self.plock = threading.Lock()
        self.active_chunks = set()
        self.yield_req = set()
        self.focus = None
        self.t_preempt = 0
        self.snap = {"done": r.get("done", 0), "now": 0, "avg": 0, "conn": [], "chunks": "", "pc": [], "prefix": 0}
        self.events = collections.deque(maxlen=300)
        lines = rd(os.path.join(self.parts, "events.log")).splitlines()
        self.events.extend(lines[-300:] if lines else r.get("events", []))
        if "fails" not in r:
            r["fails"] = sum("FAILED" in l for l in lines)
        r.setdefault("bound_ip", bound_ip(r["url"]))

    @property
    def out(self):
        return os.path.join(self.r["dir"], self.r["name"])

    @property
    def parts(self):
        return self.out + ".parts"

    def cpath(self, i):
        return os.path.join(self.parts, "%06d" % i)

    @property
    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        self.events.append(line)
        if os.path.isdir(self.parts):
            try:
                with open(os.path.join(self.parts, "events.log"), "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def load_states(self):
        self.stc = {}
        sd = os.path.join(self.parts, "s")
        for n in os.listdir(sd) if os.path.isdir(sd) else []:
            if n.isdigit():
                self.stc[int(n)] = rd(os.path.join(sd, n))

    def state(self, i):
        return self.stc.get(i, "")

    def setst(self, i, v):
        self.stc[i] = v
        wr(os.path.join(self.parts, "s", str(i)), v + "\n")

    def status(self, v, msg=None):
        if not self.stop_ev.is_set():
            self.r["status"] = v
            if msg is not None:
                self.r["msg"] = msg

    def fail(self, msg):
        self.fatal = True
        self.r["status"] = "error"
        self.r["msg"] = msg
        self.log("ERROR: " + msg)
        log.error("%s: %s", self.r["name"], msg)
        self.m.notify("Failed: %s - %s" % (self.r["name"], msg))
        self.stop_ev.set()
        self.kill()

    # control
    def start(self):
        self.stop_ev.clear()
        self.fatal = False
        self.dirty = True
        self.samples.clear()
        self.tmp_prev = None
        self.yield_req.clear()
        self.thread = threading.Thread(target=self.run, daemon=True, name="dl-" + self.r["id"])
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        self.kill()

    def kill(self):
        with self.lock:
            for p in list(self.procs):
                try:
                    p.terminate()
                except OSError:
                    pass

    def sleep(self, sec):
        return self.stop_ev.wait(sec)

    # network
    def curl(self, args, key=None):
        """run curl; returns (stdout, killed)"""
        if self.stop_ev.is_set():
            return "", True
        # a https link may only redirect to https: no silent downgrade to plain http (browsers block those downloads too)
        noweak = ["--proto-redir", "=https"] if self.r.get("url", "").startswith("https://") else []
        p = subprocess.Popen(CURL + noweak + args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        with self.lock:
            self.procs.add(p)
            if key is not None:
                self.cproc[key] = p
        try:
            out, _ = p.communicate()
        finally:
            with self.lock:
                self.procs.discard(p)
                if key is not None and self.cproc.get(key) is p:
                    del self.cproc[key]
        return out, p.returncode < 0

    def traffic(self, path, final=False):
        """add newly received bytes of an in-flight file to the traffic counter"""
        with self.lock:
            if path not in self.counted:
                return
            sz = fsize(path)
            if sz > self.counted[path]:
                self.m.count(sz - self.counted[path])
                self.counted[path] = sz
            if final:
                del self.counted[path]

    def fetch(self, out, a, b, key=None):
        args = ["--fail", "-r", "%d-%d" % (a, b), "-o", out, "-w", "%{http_code}"]
        lim = self.m.conn_limit()
        if lim:
            args += ["--limit-rate", str(lim)]
        with self.lock:
            self.counted[out] = 0
        o, killed = self.curl(args + [self.r["url"]], key)
        self.traffic(out, final=True)
        return "" if killed else o.strip()

    def probe(self):
        o, killed = self.curl(["-r", "0-0", "-D", "-", "-o", os.devnull, "-w",
                               "\n%{http_code} %{remote_ip} %{local_ip} %{time_connect} %{http_version}", self.r["url"]])
        lines = o.replace("\r", "").split("\n")
        last = (lines[-1] if lines else "").split()
        code = last[0] if last else ""
        if len(last) >= 5 and code not in ("", "000"):
            self.r["net"] = {"server": last[1], "local": last[2], "connect_ms": int(float(last[3]) * 1000),
                             "http": last[4], "t": time.time()}
        total = length = None
        etag = fname = ""
        for l in lines[:-1]:
            k, _, v = l.partition(":")
            k = k.strip().lower()
            if k == "content-range":
                mm = re.search(r"/(\d+)\s*$", v)
                total = int(mm.group(1)) if mm else None
            elif k == "content-length" and v.strip().isdigit():
                length = int(v.strip())
            elif k == "etag":
                etag = (v.split() or [""])[0]
            elif k == "content-disposition":
                mm = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", v) or re.search(r'filename\s*=\s*"?([^";]+)', v)
                if mm:
                    fname = safe_name(urllib.parse.unquote(mm.group(1).strip()))
        return code, total, length, etag, fname

    # ---------------------------------------------------------- streaming
    def media_ok(self):
        """False for a "video" that is really a text playlist (HLS / ffconcat / DASH): such a file can make ffmpeg or a
        player read other files on this Mac or addresses on your network (known attack) - dlus never plays it"""
        if getattr(self, "_media_ok", None) is not None:
            return self._media_ok
        if category(self.r["name"]) not in ("Video", "Audio"):
            return True
        a = self.avail(0)
        if not a:
            return True
        try:
            with open(a[0], "rb") as f:
                f.seek(a[1])
                head = f.read(4096)
        except OSError:
            return True
        h = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        bad = h.startswith((b"#extm3u", b"ffconcat", b"#ext-x-")) or (h.startswith(b"<") and b"<mpd" in h)
        if len(head) >= 4096 or bad:
            self._media_ok = not bad
            if bad:
                self.log("ERROR: this file is a playlist, not a video - dlus will not play or convert it")
                self.m.audit("refused to play %s: a playlist disguised as a video" % self.r["name"], level="warn")
        return not bad

    def avail(self, off):
        """(path, offset in path, bytes available there) for byte `off`, or None"""
        r = self.r
        L = r.get("size") or 0
        if r["status"] == "done":
            return (self.out, off, L - off) if off < fsize(self.out) else None
        if r.get("ranges") is False:
            n = fsize(self.out + ".part")
            return (self.out + ".part", off, n - off) if off < n else None
        i, within = divmod(off, C)
        a = int(rd(os.path.join(self.parts, "assembled"), "0") or 0)
        if i < a:
            return self.out + ".assembling", off, a * C - off
        p = self.cpath(i)
        hp = fsize(p)
        if within < hp:
            return p, within, hp - within
        frm = rd(p + ".from")
        if frm.isdigit():
            t = off - int(frm)
            n = fsize(p + ".tmp")
            if 0 <= t < n:
                return p + ".tmp", t, n - t
        return None

    def want(self, off):
        """a player needs byte `off`: download that chunk first (pre-empt the furthest one)"""
        i = off // C
        self.focus = i
        if i in self.active_chunks or i not in self.pending or time.time() - self.t_preempt < 10:
            return
        later = [j for j in self.active_chunks if j > i]
        if later:
            j = max(later)
            self.t_preempt = time.time()
            self.yield_req.add(j)
            self.log("player jumped to %d MB - downloading that part first" % (off >> 20))
            with self.lock:
                p = self.cproc.get(j)
            if p:
                p.terminate()

    # ---------------------------------------------------------- progress
    def sample(self):
        if not (self.alive or self.dirty):
            self.snap["now"] = 0
            return
        self.dirty = self.alive
        r, t = self.r, time.time()
        L = r.get("size") or 0
        tmps, sizes, chunks, pc, prefix = {}, {}, "", [], 0
        if r["status"] == "done":
            done = prefix = L
        elif r.get("ranges") is False:
            done = prefix = fsize(self.out + ".part")
            tmps = {0: done}
        else:
            try:
                names = os.listdir(self.parts)
            except OSError:
                names = []
            for n in names:
                if len(n) == 6 and n.isdigit():
                    sizes[int(n)] = fsize(os.path.join(self.parts, n))
                elif n.endswith(".tmp") and n[:6].isdigit():
                    tmps[int(n[:6])] = fsize(os.path.join(self.parts, n))
            a = int(rd(os.path.join(self.parts, "assembled"), "0") or 0)
            done = sum(sizes.values()) + sum(tmps.values()) + (fsize(self.out + ".assembling") if a else 0)
            if L:
                if self.stc is None:
                    self.load_states()
                N = (L + C - 1) // C
                cs, contig = [], True
                for i in range(N):
                    w = min(C, L - i * C)
                    h = sizes.get(i, 0) + tmps.get(i, 0)
                    s = self.stc.get(i)
                    cs.append("o" if i < a or s == "ok" else "k" if s == "check" else "b" if s == "bad" else "d" if h else "w")
                    pc.append(min(100, int(h * 100 / w)))
                    if contig:
                        if i < a or sizes.get(i, 0) == w:
                            prefix += w
                        else:
                            prefix += sizes.get(i, 0) + max(0, tmps.get(i, 0) - (OV if sizes.get(i, 0) >= OV else 0))
                            contig = False
                chunks = "".join(cs)
        for path in list(self.counted):
            self.traffic(path)
        conn = []
        if r.get("ranges") is not False:
            for i, sz in sorted(tmps.items()):
                prev = (self.tmp_prev or {}).get(i)
                sp = (sz - prev[1]) / (t - prev[0]) if prev and sz >= prev[1] and t > prev[0] else 0
                self.conn_sp[i] = 0.6 * self.conn_sp.get(i, sp) + 0.4 * sp
                conn.append([i, pc[i] if i < len(pc) else 0, int(self.conn_sp[i])])
        self.tmp_prev = {i: (t, sz) for i, sz in tmps.items()} if self.alive else None
        self.samples.append((t, done))

        def rate(win):
            old = [s for s in self.samples if t - s[0] <= win]
            if len(old) < 2 or old[-1][0] == old[0][0]:
                return 0
            return max(0, (old[-1][1] - old[0][1]) / (old[-1][0] - old[0][0]))

        now = rate(5.5) if self.alive else 0
        self.hist.append(int(now))
        r["done"] = done
        self.snap = {"done": done, "now": int(now), "avg": int(rate(61) if self.alive else 0),
                     "conn": conn, "chunks": chunks, "pc": pc, "prefix": prefix}

    # ---------------------------------------------------------- main logic
    def run(self):
        try:
            self._run()
        except OSError as e:
            log.exception("download %s", self.r["name"])
            self.fail("disk is full - free some space, then press Resume" if e.errno == errno.ENOSPC else "error: %s" % e)
        except Exception as e:
            log.exception("download %s", self.r["name"])
            self.fail("internal error: %s" % e)
        finally:
            self.dirty = True

    def _run(self):
        r = self.r
        self.status("downloading", "")
        if r.get("named") and r.get("size") and fsize(self.out) == r["size"] and not os.path.isdir(self.parts):
            return self.finish()
        info = self.probe_loop()
        if not info:
            return
        code, total, length, etag, fname = info
        os.makedirs(r["dir"], exist_ok=True)
        if not r.get("named"):
            if not os.path.isdir(os.path.join(r["dir"], r["name"] + ".parts")) and fname:
                r["name"] = fname
            r["name"] = self.m.unique_name(self, total or length)
            r["named"] = True
        if code == "200":
            return self.single(length)
        L = r["size"] = total
        r["ranges"] = True
        os.makedirs(os.path.join(self.parts, "s"), exist_ok=True)
        self.load_states()
        meta = "size=%d etag=%s chunk=%d" % (L, etag, C)
        mp = os.path.join(self.parts, "meta")
        if os.path.exists(mp):
            if rd(mp) != meta:
                return self.fail("the file on the server changed (%s vs %s) - add it as a new download" % (rd(mp), meta))
        else:
            wr(mp, meta + "\n")
            self.log("started: %s, %d MB, %d chunks, %d connections" % (r["name"], L >> 20, (L + C - 1) // C, r["conns"]))
        N = (L + C - 1) // C
        self.adopt(L)
        free = shutil.disk_usage(r["dir"]).free
        need = L - self.snap.get("done", 0) + C
        if free < need:
            return self.fail("not enough disk space: need %d GB, have %d GB" % (need >> 30, free >> 30))
        a = int(rd(os.path.join(self.parts, "assembled"), "0") or 0)
        with self.plock:
            self.pending = set(range(a, N))

        def pick():
            with self.plock:
                if not self.pending:
                    return None
                f = self.focus
                j = min((k for k in self.pending if f is None or k >= f), default=min(self.pending))
                self.pending.discard(j)
                self.active_chunks.add(j)
                return j

        def give_back(i):
            with self.plock:
                self.active_chunks.discard(i)
                self.pending.add(i)

        def worker():
            while not self.stop_ev.is_set():
                i = pick()
                if i is None:
                    return
                try:
                    res = self.chunk(i, L)
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        give_back(i)
                        return self.fail("disk is full - free some space, then press Resume")
                    log.exception("chunk %d", i)
                    self.log("chunk %d: error (%s) - retrying" % (i, e))
                    give_back(i)
                    if self.sleep(5):
                        return
                    continue
                except Exception as e:
                    log.exception("chunk %d", i)
                    self.log("chunk %d: internal error (%s) - retrying" % (i, e))
                    give_back(i)
                    if self.sleep(5):
                        return
                    continue
                if res == "yield" or res is None:
                    give_back(i)
                    if res is None:
                        return
                    continue
                with self.plock:
                    self.active_chunks.discard(i)
                if res is False:
                    return

        ths = [threading.Thread(target=worker, daemon=True) for _ in range(min(r["conns"], N - a) or 1)]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        self.active_chunks.clear()
        if self.stop_ev.is_set():
            return
        for i in range(a, N):
            if fsize(self.cpath(i)) != min(C, L - i * C) or self.state(i) != "ok":
                return self.fail("chunk %d incomplete - press Resume to fix it" % i)
        self.join(L, N)

    def probe_loop(self):
        tries = 0
        while not self.stop_ev.is_set():
            code, total, length, etag, fname = info = self.probe()
            if (code == "206" and total) or code == "200":
                if tries:
                    self.log("server reachable again")
                self.status("downloading", "")
                return info
            if not code or code == "000":
                if self.stop_ev.is_set():
                    return None
                msg = "no connection to the server"
            elif code in ("403", "404", "410"):
                msg = "HTTP %s - link expired or your IP changed (use Refresh link)" % code
            else:
                msg = "server answered HTTP %s" % code
            if tries == 0:
                self.log(msg + " - retrying")
            self.status("waiting", msg + " - retrying")
            tries += 1
            if self.sleep(min(5 * tries, 30)):
                return None
        return None

    def adopt(self, L):
        """turn an existing partial target file (e.g. from wget) into chunks"""
        if not os.path.exists(self.out) or fsize(self.out) >= L:
            return
        size = fsize(self.out)
        self.log("adopting existing partial file (%d MB) - it will be checked, not re-downloaded" % (size >> 20))
        with open(self.out, "rb") as f:
            for i in range((size + C - 1) // C):
                p = self.cpath(i)
                rm(p + ".adopt")
                if os.path.exists(p):
                    continue
                f.seek(i * C)
                with open(p + ".adopt", "wb") as o:
                    left = min(C, size - i * C)
                    while left > 0:
                        b = f.read(min(left, 8 << 20))
                        if not b:
                            break
                        o.write(b)
                        left -= len(b)
                    o.flush()
                    os.fsync(o.fileno())
                os.replace(p + ".adopt", p)
        rm(self.out)

    def sample_check(self, p, start, want, i):
        """0 = 3 samples match server, 1 = mismatch, 2 = network error / stopped"""
        chk = p + ".chk"
        for off in (0, (want // 2 // OV) * OV, max(0, want - OV)):
            n = min(OV, want - off)
            code = self.fetch(chk, start + off, start + off + n - 1, i)
            if code != "206" or fsize(chk) != n:
                rm(chk)
                return 2
            same = read_at(chk, 0, n) == read_at(p, off, n)
            rm(chk)
            if not same:
                return 1
        return 0

    def chunk(self, i, L):
        """download + check one chunk: True done, False fatal, None stopped, "yield" = give way"""
        start = i * C
        end = min(start + C, L) - 1
        want = end - start + 1
        p, tmp = self.cpath(i), self.cpath(i) + ".tmp"
        rm(tmp + ".app", p + ".chk")
        tries = 0
        while not self.stop_ev.is_set():
            if i in self.yield_req:
                self.yield_req.discard(i)
                return "yield"
            have = fsize(p)
            if have == want:
                if self.state(i) == "ok":
                    return True
                self.setst(i, "check")
                rc = self.sample_check(p, start, want, i)
                if rc == 0:
                    self.setst(i, "ok")
                    self.log("chunk %d: check OK (3 samples match server)" % i)
                    return True
                if rc == 1:
                    self.setst(i, "bad")
                    self.r["fails"] = self.r.get("fails", 0) + 1
                    self.log("chunk %d: CHECK FAILED - re-downloading only this chunk (%d MB)" % (i, want >> 20))
                    rm(p)
                    continue
                code = "net"
            else:
                if have > want:
                    self.log("chunk %d: too big, redoing" % i)
                    rm(p)
                    have = 0
                if self.state(i) != "bad":
                    self.setst(i, "dl")
                ov = OV if have >= OV else 0
                frm = start + have - ov
                if fsize(tmp) > 0 and rd(p + ".from", str(frm)) == str(frm):
                    code = "206"
                    self.log("chunk %d: reusing %d MB from interrupted connection" % (i, fsize(tmp) >> 20))
                else:
                    rm(tmp)
                    wr(p + ".from", str(frm))
                    code = self.fetch(tmp, frm, end, i)
                got = fsize(tmp)
                if code == "206" and got > ov:
                    if ov:
                        if read_at(tmp, 0, ov) == read_at(p, have - ov, ov):
                            self.log("chunk %d: resume check OK at %d MB" % (i, have >> 20))
                        else:
                            self.setst(i, "bad")
                            self.r["fails"] = self.r.get("fails", 0) + 1
                            self.log("chunk %d: RESUME CHECK FAILED - data on disk differs, re-downloading only this chunk" % i)
                            rm(p, tmp)
                            continue
                    # .app is not counted as progress, so appending never double-counts;
                    # a .app left by a crash is dropped (what reached p is a valid prefix)
                    os.replace(tmp, tmp + ".app")
                    with open(tmp + ".app", "rb") as src, open(p, "ab") as dst:
                        src.seek(ov)
                        shutil.copyfileobj(src, dst, 8 << 20)
                        dst.flush()
                        os.fsync(dst.fileno())
                    rm(tmp + ".app", p + ".from")
                    tries = 0
                    continue
                if code == "":  # connection cut on purpose (pause / player jump / new limit): keep data
                    tries = 0
                    continue
                rm(tmp)
            if code == "200":
                self.fail("server ignored the byte range (HTTP 200)")
                return False
            if code == "416":
                self.fail("HTTP 416 - the file on the server changed?")
                return False
            if code in ("403", "404", "410"):
                msg = "HTTP %s - link expired or your IP changed (use Refresh link)" % code
                if self.r.get("msg") != msg:
                    self.log("chunk %d: %s" % (i, msg))
                self.status("waiting", msg)
            elif tries:
                self.log("chunk %d: network error (%s), retrying" % (i, code or "none"))
            tries += 1
            if self.sleep(min(5 * tries, 20)):
                return None
            if self.r["status"] == "waiting":
                self.status("downloading", "")
        return None

    def single(self, length):
        r = self.r
        r["ranges"] = False
        r["size"] = length or 0
        part = self.out + ".part"
        self.log("server does not support resume - downloading in one piece (a stop restarts this file)")
        tries = 0
        while not self.stop_ev.is_set():
            rm(part)
            args = ["--fail", "-o", part, "-w", "%{http_code}"]
            lim = self.m.conn_limit()
            if lim:
                args += ["--limit-rate", str(lim)]
            with self.lock:
                self.counted[part] = 0
            o, killed = self.curl(args + [r["url"]])
            self.traffic(part, final=True)
            if killed:
                return
            if o.strip() == "200" and (not length or fsize(part) == length):
                r["size"] = fsize(part)
                os.replace(part, self.out)
                return self.finish()
            tries += 1
            self.log("download failed (HTTP %s), retrying" % (o.strip() or "none"))
            if self.sleep(min(5 * tries, 30)):
                return

    def join(self, L, N):
        self.status("joining", "joining chunks into the final file")
        self.log("all %d chunks downloaded and checked - joining into one file" % N)
        asm = self.out + ".assembling"
        ap = os.path.join(self.parts, "assembled")
        a = int(rd(ap, "0") or 0)
        with open(asm, "r+b" if os.path.exists(asm) else "w+b") as f:
            for i in range(a, N):
                if self.stop_ev.is_set():
                    return
                f.truncate(i * C)
                f.seek(i * C)
                with open(self.cpath(i), "rb") as src:
                    shutil.copyfileobj(src, f, 8 << 20)
                f.flush()
                os.fsync(f.fileno())
                wr(ap, str(i + 1))
                rm(self.cpath(i))
                self.r["msg"] = "joining chunks: %d / %d" % (i + 1, N)
        if fsize(asm) != L:
            return self.fail("joined size %d != %d" % (fsize(asm), L))
        os.replace(asm, self.out)
        self.finish()

    def hash_file(self, algos, label):
        """hex digests of the finished file, one pass; None when stopped"""
        hs = {a: hashlib.new(a) for a in dict.fromkeys(algos)}
        with open(self.out, "rb") as f:
            n = 0
            while True:
                b = f.read(8 << 20)
                if not b:
                    break
                for h in hs.values():
                    h.update(b)
                n += len(b)
                if self.stop_ev.is_set():
                    return None
                if n % (1 << 30) < (8 << 20):
                    self.r["msg"] = "%s: %d%%" % (label, n * 100 // max(1, self.r.get("size") or 1))
        return {a: h.hexdigest() for a, h in hs.items()}

    def finish(self):
        r = self.r
        names = (r["name"], name_from_url(r["url"]))
        want = None
        cs = (r.get("checksum") or "").strip()
        if re.match(r"^https?://", cs):  # a link to the publisher's checksum file
            try:
                want = parse_checksum(fetch_small(cs), names, strict=True)
            except (OSError, ValueError) as e:
                return self.fail("could not read the checksum file: %s" % e)
            if not want:
                return self.fail("the checksum file has no checksum for %s" % r["name"])
            r["checksum_src"] = cs
        elif cs:
            want = parse_checksum(cs, names)
        elif category(r["name"]) in SENSITIVE_CATS and not r.get("local"):
            self.status("verifying", "looking for the publisher's checksum")
            got = published_checksum(r["url"], names)
            if got:
                want = got[:2]
                r["checksum"], r["checksum_src"] = "%s:%s" % want, got[2]
                self.log("found the publisher's checksum: " + got[2])
        algos = ["sha256"] + ([want[0]] if want else [])
        self.status("verifying", "verifying %s of the whole file" % (want[0] if want else "sha256"))
        got = self.hash_file(algos, "verifying " + algos[-1])
        if got is None:
            return
        r["sha256"] = got["sha256"]
        if want:
            ok = secrets.compare_digest(got[want[0]], want[1])
            r["checksum_ok"] = ok
            self.log("checksum %s %s" % (want[0], "OK - file is exactly right" if ok else "FAILED: got " + got[want[0]]))
            if not ok:
                bad = self.out + ".checksum-failed"
                os.replace(self.out, bad)
                quarantine(bad)
                shutil.rmtree(self.parts, ignore_errors=True)  # "resume" downloads it again from scratch
                return self.fail("checksum mismatch - this is NOT the expected file. Set aside as %s - do not open it" % os.path.basename(bad))
        quarantine(self.out)  # Gatekeeper checks apps / installers from it, like a browser download
        r["status"] = "done"
        r["msg"] = ""
        r["force"] = False
        r["finished"] = time.time()
        r["done"] = r["size"] = fsize(self.out)
        self.log("done: %s (%d bytes%s)" % (r["name"], r["size"], ", all chunks checked" if r.get("ranges") else ""))
        r["events"] = list(self.events)[-100:]
        shutil.rmtree(self.parts, ignore_errors=True)
        self.dirty = True
        self.m.notify("Finished: " + r["name"])


# ---------------------------------------------------------------- manager
class Manager:
    DEFAULTS = {"base_dir": os.path.expanduser("~/Downloads/dlm"), "max_active": 3, "conns": 8, "limit_mb": 0,
                "sched_on": False, "sched_from": "02:00", "sched_to": "08:00", "on_done": "none", "notify": True,
                "lan_on": False, "dlna_on": False, "lan_port": 8643,
                "sub_style": {"size": 5.2, "color": "#ffffff", "bg": "shadow", "pos": 7, "bold": True, "font": "Vazirmatn"},
                "tv_audio": "original", "watch_limit_mb": 1.0,
                "files_on": False, "proxy_on": False, "proxy_port": 8118, "proxy_auth": True, "proxy_upstream": "auto"}

    def __init__(self, port=PORT):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.port = port
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.speech_jobs = set()
        self.sync_locks = {}
        self.ui_key = ui_key()
        shutil.rmtree(os.path.join(STATE_DIR, "tmp"), ignore_errors=True)  # temporary subtitles of earlier streams
        self.hls = None  # created on first use (HLS class is defined later in the file)
        self.media_key = media_key()
        self.thumb_lock = threading.Lock()
        self.gw_wiz = {}
        self.gw_vpn_probe = {}
        self.gw_tvnet = {}
        self.settings = dict(self.DEFAULTS)
        self.settings.update(load_json(os.path.join(STATE_DIR, "settings.json"), {}))
        if len(self.settings.get("lan_token") or "") != 6:
            self.settings["lan_token"] = short_code()
        self.settings.setdefault("dlna_uuid", str(uuid.uuid4()))
        self.lan = self.ssdp = None
        self.settings.setdefault("proxy_user", "dlus")
        self.settings.setdefault("proxy_pass", short_code(8))
        self.proxy = None
        self.gw = Gateway(self)
        self.tv_conns = {}
        self.tv_cmds = collections.deque(maxlen=60)
        self.tv_seq = 0
        self.tv_now = None
        self.pin_fail = {}
        self.gw_new = {}
        self.sub_jobs = {}
        self.sub_lock = threading.Lock()
        self.sync_running = set()
        self.casting = None
        self.sessions = Sessions(self)
        self.audit_log = collections.deque(load_json(os.path.join(STATE_DIR, "audit.json"), [])[-2000:], maxlen=2000)
        self.audit_dirty = False
        self.lan_pending = {}
        self.lan_denied = {}
        self.mac_cache = {}
        self.settings.setdefault("lan_allowed", {})
        for t in self.settings.get("tvs") or []:  # TVs you paired (accepted on the TV itself) stay allowed after the update
            if t.get("id") in (self.settings.get("tv_keys") or {}) and t.get("ip") and t["ip"] not in self.settings["lan_allowed"]:
                self.settings["lan_allowed"][t["ip"]] = {"name": t.get("name") or t["ip"], "mac": t.get("mac", ""), "at": time.time(),
                                                         "why": "paired TV", "last": time.time()}
        self.settings["sub_style"] = dict(self.DEFAULTS["sub_style"], **(self.settings.get("sub_style") or {}))
        self.dlna_update, self.dlna_key = 1, None
        self.stats = load_json(os.path.join(STATE_DIR, "stats.json"), {"days": {}, "total": 0})
        self.dls = collections.OrderedDict()
        for r in load_json(os.path.join(STATE_DIR, "state.json"), []):
            if r.get("status") in RUNNING:
                r["status"] = "queued"
            self.dls[r["id"]] = DL(self, r)
        n = max([r.get("num") or 0 for r in (d.r for d in self.dls.values())] + [0])
        for d in self.dls.values():  # short numbers for short stream links
            if not d.r.get("num"):
                n += 1
                d.r["num"] = n
        self.net = Net()
        self.gnow = 0
        self.ghist = collections.deque(maxlen=600)
        self.caff = None
        self.was_busy = False
        self.sched_wait = False

    def notify(self, msg):
        if self.settings.get("notify", True) and sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", "display notification %s with title \"dlus\"" % json.dumps(msg, ensure_ascii=False)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ------------------------------------------------------ who may use dlus from the network + the audit log
    def audit(self, event, ip="", device="", level="info"):
        """one line in the access / activity log (Devices & viewers -> Activity log), kept in ~/.dlus/audit.json"""
        if ip and not device:
            a = (self.settings.get("lan_allowed") or {}).get(ip) or self.lan_pending.get(ip) or {}
            device = a.get("name") or (self.sessions.hosts.get(ip) or {}).get("host", "")
        self.audit_log.append({"t": time.time(), "ip": ip, "device": device, "event": event[:300], "level": level})
        self.audit_dirty = True
        log.info("audit %s %s: %s", ip or "-", device or "", event)

    def save_audit(self):
        if self.audit_dirty:
            self.audit_dirty = False
            wr_sync(os.path.join(STATE_DIR, "audit.json"), json.dumps(list(self.audit_log)))

    def mac_of(self, ip):
        c = self.mac_cache.get(ip)
        if c and time.time() - c[1] < 60:
            return c[0]
        mm = re.search(r" at ([0-9a-f:]{11,17}) ", sh(["arp", "-n", ip], 3))
        mac = ":".join(x.zfill(2) for x in mm.group(1).split(":")) if mm else ""
        self.mac_cache[ip] = (mac, time.time())
        return mac

    def lan_allow(self, ip, why, name=""):
        """allow a device (the user pressed Allow, or sent a film to that TV from this Mac)"""
        if ip in ("127.0.0.1", "::1"):
            return
        p = self.lan_pending.pop(ip, None) or {}
        al = self.settings.setdefault("lan_allowed", {})
        if ip not in al:
            al[ip] = {"name": name or p.get("name") or (self.sessions.hosts.get(ip) or {}).get("host") or ip, "mac": self.mac_of(ip),
                      "at": time.time(), "why": why, "last": time.time()}
            self.audit("allowed: " + why, ip, al[ip]["name"])
            self.save()
        self.lan_denied.pop(ip, None)

    def lan_access(self, ip, ua, what):
        """may this device use the shared library / streams / proxy? Unknown devices wait until the user allows them in the app."""
        if ip in ("127.0.0.1", "::1"):
            return True
        al = self.settings.setdefault("lan_allowed", {})
        a = al.get(ip)
        if a:
            mac = self.mac_of(ip)
            if a.get("mac") and mac and mac != a["mac"]:  # another device took over this address
                self.audit("a different device (%s) now uses the address of %s - asking again" % (mac, a.get("name", ip)), ip, level="warn")
                al.pop(ip, None)
                self.save()
            else:
                if not a.get("mac") and mac:
                    a["mac"] = mac
                if time.time() - a.get("last", 0) > 600:
                    self.audit(what, ip, a.get("name", ""))
                a["last"] = time.time()
                return True
        if time.time() < self.lan_denied.get(ip, 0):
            return False
        name, kind = device_name(ua, ip)
        host = (self.sessions.host_info(ip) or {}).get("host", "")
        if ip not in self.lan_pending:
            self.lan_pending[ip] = {"ip": ip, "name": name if name != ip else host or ip, "kind": kind, "host": host, "mac": self.mac_of(ip),
                                    "ua": ua[:180], "what": what, "at": time.time()}
            self.audit("asks for access (%s)" % what, ip, name, "warn")
            self.notify("%s (%s) asks to use dlus - open dlus to allow or deny" % (name, ip))
        self.lan_pending[ip]["last"] = time.time()
        return False

    def lan_decide(self, b):
        ip = str(b.get("ip") or "")
        if b.get("act") == "remove":
            a = (self.settings.get("lan_allowed") or {}).pop(ip, None)
            if a:
                self.audit("access removed by you", ip, a.get("name", ""), "warn")
                self.save()
            return {"ok": True}
        if ip not in self.lan_pending:
            raise ValueError("no such request")
        if b.get("act") == "allow":
            self.lan_allow(ip, "you allowed it in the app")
        else:
            p = self.lan_pending.pop(ip)
            self.lan_denied[ip] = time.time() + 3600
            self.audit("denied by you (blocked for 1 hour)", ip, p.get("name", ""), "warn")
        return {"ok": True}

    def access_state(self):
        now = time.time()
        for ip in [ip for ip, p in self.lan_pending.items() if now - p.get("last", p["at"]) > 1800]:
            self.lan_pending.pop(ip, None)
        return {"pending": sorted(self.lan_pending.values(), key=lambda p: p["at"]),
                "allowed": [dict(v, ip=k) for k, v in sorted((self.settings.get("lan_allowed") or {}).items(), key=lambda kv: -kv[1].get("last", 0))],
                "log": list(self.audit_log)[-300:][::-1]}

    def count_proxy(self, n):
        k = time.strftime("%Y-%m-%d")
        pd = self.stats.setdefault("proxy_days", {})
        pd[k] = pd.get(k, 0) + n

    def count(self, n):
        if n > 0:
            k = time.strftime("%Y-%m-%d")
            self.stats["days"][k] = self.stats["days"].get(k, 0) + n
            self.stats["total"] = self.stats.get("total", 0) + n

    def save(self):
        with self.lock:
            data = json.dumps([d.r for d in self.dls.values()], indent=1)
        with self.save_lock:
            self._save(data)

    def _save(self, data):
        try:
            wr_sync(os.path.join(STATE_DIR, "state.json"), data)
            wr_sync(os.path.join(STATE_DIR, "settings.json"), json.dumps(self.settings, indent=1))
            wr_sync(os.path.join(STATE_DIR, "stats.json"), json.dumps(self.stats))
        except OSError:
            log.exception("save")

    def conn_limit(self):
        lim = float(self.settings.get("limit_mb") or 0) * 1048576
        wl = float(self.settings.get("watch_limit_mb") or 0) * 1048576
        if getattr(self, "throttled", None) and wl:
            lim = min(lim, wl) if lim else wl
        if not lim:
            return 0
        n = sum(d.r["conns"] if d.r.get("ranges", True) else 1 for d in self.dls.values() if d.alive) or 1
        return max(4096, int(lim / n))

    def folder(self, name, folder):
        base = os.path.expanduser(self.settings["base_dir"])
        folder = (folder or "").strip()
        if not folder:
            return os.path.join(base, category(name))
        if folder.startswith(("/", "~")):
            return os.path.expanduser(folder)
        return os.path.join(base, folder)

    def unique_name(self, d, total):
        base = d.r["name"]
        stem, ext = (base.rsplit(".", 1)[0], "." + base.rsplit(".", 1)[1]) if "." in base else (base, "")
        cand, k = base, 1
        while True:
            p = os.path.join(d.r["dir"], cand)
            taken = any(o is not d and o.r.get("named") and o.r["dir"] == d.r["dir"] and o.r["name"] == cand
                        for o in self.dls.values())
            complete = os.path.exists(p) and not os.path.isdir(p + ".parts") and (not total or fsize(p) >= total)
            if not taken and not complete:
                return cand
            cand = "%s (%d)%s" % (stem, k, ext)
            k += 1

    def add(self, url, folder="", conns=0, start=True, checksum=""):
        url = url.strip().rstrip(").,;'\"")
        if not re.match(r"^(https?|ftp)://", url):
            raise ValueError("not a http(s)/ftp URL: " + url[:80])
        with self.lock:
            dup = next((d for d in self.dls.values() if d.r["url"] == url), None)
        if dup:
            raise ValueError("already in the list: " + dup.r["name"])
        checksum = (checksum or "").strip()
        if checksum and not re.match(r"^https?://\S+$", checksum) and not parse_checksum(checksum):
            raise ValueError("checksum not understood - paste an MD5 / SHA-1 / SHA-256 / SHA-512 value or a link to the checksum file")
        name = name_from_url(url)
        r = {"id": uuid.uuid4().hex[:10], "url": url, "name": name, "dir": self.folder(name, folder),
             "conns": max(1, min(32, int(conns or self.settings["conns"]))), "status": "queued" if start else "paused",
             "added": time.time(), "size": 0, "done": 0, "msg": "", "checksum": checksum.strip(), "named": False,
             "fails": 0, "bound_ip": bound_ip(url), "num": max([d.r.get("num") or 0 for d in self.dls.values()] + [0]) + 1}
        with self.lock:
            self.dls[r["id"]] = DL(self, r)
        self.save()
        self.audit("download added: %s" % url[:200])
        return r["id"]

    def move(self, did, where):
        with self.lock:
            ids = list(self.dls)
            k = ids.index(did)
            ids.pop(k)
            pos = {"top": 0, "bottom": len(ids), "up": max(0, k - 1), "down": min(len(ids), k + 1)}[where]
            ids.insert(pos, did)
            self.dls = collections.OrderedDict((i, self.dls[i]) for i in ids)

    def verify(self, d, cs):
        """check a finished file again: against a checksum you paste (or a checksum-file link),
        or against its own SHA-256 from the end of the download (was it changed since?)"""
        r = d.r
        names = (r["name"], name_from_url(r["url"]))
        if re.match(r"^https?://", cs):
            want = parse_checksum(fetch_small(cs), names, strict=True)
            if not want:
                raise ValueError("that checksum file has no checksum for " + r["name"])
        elif cs:
            want = parse_checksum(cs, names)
            if not want:
                raise ValueError("checksum not understood - paste an MD5 / SHA-1 / SHA-256 / SHA-512 value or a link to the checksum file")
        else:
            want = ("sha256", r["sha256"]) if r.get("sha256") else None
        if getattr(d, "verifying", False):
            raise ValueError("already checking this file")

        def run():
            d.verifying = True
            r["verify"] = {"state": "running", "at": time.time()}
            try:
                got = d.hash_file(["sha256"] + ([want[0]] if want else []), "checking")
                r["msg"] = ""
                if got is None:
                    r["verify"] = None
                    return
                if not r.get("sha256"):
                    r["sha256"] = got["sha256"]
                if not want:
                    r["verify"] = {"state": "info", "at": time.time(), "text": "SHA-256 computed - compare it with the one the publisher shows"}
                    return
                ok = secrets.compare_digest(got[want[0]], want[1])
                what = "the checksum you gave" if cs else "the file as it was downloaded"
                r["verify"] = {"state": "ok" if ok else "bad", "at": time.time(), "algo": want[0],
                               "text": ("matches %s" if ok else "does NOT match %s - the file is different") % what}
                if cs:
                    r["checksum"], r["checksum_ok"] = "%s:%s" % want, ok
                    if cs.startswith("http"):
                        r["checksum_src"] = cs
                d.log("check %s: %s" % (want[0], r["verify"]["text"]))
            finally:
                d.verifying = False
                self.dirty = True
        threading.Thread(target=run, daemon=True, name="verify").start()
        return {"ok": True, "started": True}

    def action(self, did, act, body):
        with self.lock:
            targets = list(self.dls.values()) if did == "all" else [self.dls[did]] if did in self.dls else []
        if not targets:
            raise KeyError("no such download")
        res = {"ok": True}
        for d in targets:
            r = d.r
            if act == "pause" and r["status"] not in ("done", "error", "paused"):
                r["status"] = "paused"
                r["msg"] = ""
                r["force"] = False
                d.stop()
                d.log("paused - connections closed, partial data kept")
            elif act in ("resume", "start") and r["status"] in ("paused", "error", "queued"):
                r["status"] = "queued"
                r["msg"] = ""
                if act == "start":
                    r["force"] = True
                    self.move(r["id"], "top")
                    if not d.alive:
                        d.start()
                d.log("resumed" if act == "resume" else "started now (ignores queue limit and schedule)")
            elif act == "verify" and r["status"] == "done" and os.path.isfile(d.out):
                res = self.verify(d, str(body.get("checksum") or "").strip())
            elif act == "delete":
                d.stop()
                if d.thread:
                    d.thread.join(10)
                with self.lock:
                    self.dls.pop(r["id"], None)
                if body.get("files") and r.get("named") and not r.get("local"):
                    shutil.rmtree(d.parts, ignore_errors=True)
                    rm(d.out + ".assembling", d.out + ".part", d.out)
            elif act == "refresh":
                url = (body.get("url") or "").strip()
                if not re.match(r"^(https?|ftp)://", url):
                    raise ValueError("not a http(s)/ftp URL")
                r["url"] = url
                r["bound_ip"] = bound_ip(url)
                d.log("link refreshed - continuing (same size/ETag is checked)")
                d.kill()
                if r["status"] == "error":
                    r["status"] = "queued"
            elif act == "open":
                os.makedirs(r["dir"], exist_ok=True)
                subprocess.Popen(["open", "-R", d.out] if os.path.exists(d.out) else ["open", r["dir"]])
            elif act in ("up", "down", "top", "bottom"):
                self.move(r["id"], act)
            elif act == "play":
                url = "http://127.0.0.1:%d/s/%s?ik=%s" % (self.port, self.short_path(d), self.media_key)
                app = body.get("app", "")
                if app == "mpv":
                    subprocess.Popen(["mpv", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif app in self.players():
                    subprocess.Popen(["open", "-a", app, url])
                else:
                    raise ValueError("player not installed: " + app)
            elif act == "clear" and did == "all":
                with self.lock:
                    for k in [k for k, x in self.dls.items() if x.r["status"] == "done"]:
                        self.dls.pop(k)
                break
        self.save()
        return res

    @staticmethod
    def players():
        ps = [n for n in ("IINA", "VLC", "Infuse 7", "QuickTime Player") if
              os.path.exists("/Applications/%s.app" % n) or os.path.exists("/System/Applications/%s.app" % n)]
        if shutil.which("mpv"):
            ps.append("mpv")
        return ps

    def lan_ip(self):
        return self.net.info.get("local_ip") or "127.0.0.1"

    def friendly(self):
        return "dlus on " + socket.gethostname().split(".")[0]

    def import_local(self, path):
        """make a local video/audio file playable in the dlus players (library entry pointing at the file)"""
        path = os.path.realpath(path)
        if not os.path.isfile(path):
            raise ValueError("file not found")
        with self.lock:
            for d in self.dls.values():
                if d.r.get("named") and os.path.realpath(d.out) == path:
                    return d
            r = {"id": uuid.uuid4().hex[:10], "url": "file://" + urllib.parse.quote(path), "name": os.path.basename(path), "dir": os.path.dirname(path),
                 "conns": 1, "status": "done", "added": time.time(), "finished": time.time(), "size": fsize(path), "done": fsize(path),
                 "msg": "", "named": True, "ranges": True, "fails": 0, "bound_ip": "", "local": True,
                 "num": max([x.r.get("num") or 0 for x in self.dls.values()] + [0]) + 1}
            d = self.dls[r["id"]] = DL(self, r)
        self.save()
        return d

    def by_num(self, n):
        return next((d for d in list(self.dls.values()) if d.r.get("num") == n), None)

    def short_path(self, d, v=""):
        """'' = original file, 's' = TV-compatible sound (+ chosen subtitle), 't' = original sound + chosen subtitle"""
        v = "s" if v is True else (v or "")
        e = d.r["name"].rsplit(".", 1)[-1].lower() if "." in d.r["name"] else "bin"
        return "%d%s.%s" % (d.r["num"], v, "mkv" if v else e)

    def find(self, prefix):
        hits = [d for k, d in list(self.dls.items()) if k.startswith(prefix)] if len(prefix) >= 4 else []
        return hits[0] if len(hits) == 1 else None

    # ---------------------------------------------------------- subtitles
    def scan_sidecars(self, d):
        """subtitle files next to the movie with the same name are imported automatically"""
        r = d.r
        if not r.get("named") or not os.path.isdir(r["dir"]):
            return
        stem = r["name"].rsplit(".", 1)[0].lower()
        seen = set(r.get("sidecars") or [])
        for f in sorted(os.listdir(r["dir"])):
            fs = f.rsplit(".", 1)[0].lower()
            if f.lower().endswith(SUB_EXT) and f not in seen and (fs.startswith(stem) or stem.startswith(fs)):
                seen.add(f)
                try:
                    with open(os.path.join(r["dir"], f), "rb") as fh:
                        self.subs_add(d, f, fh.read())
                except (OSError, ValueError):
                    pass
        r["sidecars"] = sorted(seen)

    def subs_info(self, d):
        r = d.r
        self.scan_sidecars(d)
        tracks = [{"sid": t["sid"], "label": t["label"] + (" \u00b7 sync %d%%" % t["sync"]["match"] if t.get("sync") else ""),
                   "lang": t["lang"], "kind": "file", "ok": True, "cues": t.get("cues", 0), "sync": t.get("sync")}
                  for t in r.get("subs") or [] if os.path.exists(t["file"])]
        for x in probe(self, d).get("subs") or []:
            name = LANG_NAMES.get(x["lang"], x["lang"] or "Track %d" % (x["k"] + 1))
            ok = x["codec"] in TEXT_SUBS
            tracks.append({"sid": "e%d" % x["k"], "lang": x["lang"], "kind": "embedded", "ok": ok,
                           "label": name + (" \u00b7 " + x["title"] if x["title"] else "") + ("" if ok else " (image, not supported)")})
        for t in r.get("subs") or []:  # timing check not done yet (e.g. interrupted by a restart): run it
            if not t.get("sync") and t["sid"] not in self.sync_running and os.path.exists(t["file"]):
                self.sync_running.add(t["sid"])
                threading.Thread(target=self.auto_sync, args=(d, t["sid"]), daemon=True).start()
        sid = r.get("sub_default")
        if sid is None:  # never chosen: Persian if there is one, else off
            sid = next((t["sid"] for t in tracks if t["ok"] and t["lang"] in ("fa", "per", "fas")), "")
        return {"tracks": tracks, "sid": sid, "delay": r.get("sub_delay", 0), "scale": r.get("sub_scale", 1), "style": self.settings["sub_style"]}

    def subs_add(self, d, name, raw, label=None):
        files = []
        lim = 30 << 20  # a subtitle is never this big: refuse "zip bombs"
        if raw[:2] == b"\x1f\x8b":
            try:
                dz = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw = dz.decompress(raw, lim + 1)
            except zlib.error:
                raise ValueError("broken .gz subtitle")
            if len(raw) > lim:
                raise ValueError("the subtitle archive unpacks to more than 30 MB - refused")
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                infos = [i for i in z.infolist() if i.filename.lower().endswith(SUB_EXT) and "__MACOSX" not in i.filename][:10]
                if sum(i.file_size for i in infos) > lim:
                    raise ValueError("the subtitle archive unpacks to more than 30 MB - refused")
                files = []
                for i in infos:
                    with z.open(i) as zf:
                        data = zf.read(lim + 1)
                    if len(data) > lim:
                        raise ValueError("the subtitle archive unpacks to more than 30 MB - refused")
                    files.append((os.path.basename(i.filename), data))
        else:
            files = [(name or "subtitle.srt", raw)]
        if not files:
            raise ValueError("no subtitle file found (.srt .ass .ssa .vtt or a .zip with one)")
        added, seen_txt = [], set()
        files.sort(key=lambda f: 0 if re.search(r"utf-?8", f[0], re.I) else 1)  # prefer the UTF-8 copy
        for fn, data in files:
            text = decode_sub(data)
            # same subtitle in another encoding = same timing lines
            key = hashlib.sha1("".join(re.findall(r"\d+:\d\d:\d\d[.,]\d+ --> \d+:\d\d:\d\d[.,]\d+", text)).encode(), usedforsecurity=False).hexdigest()
            if key in seen_txt:  # same subtitle in another encoding (ANSI + UTF-8 copies)
                continue
            seen_txt.add(key)
            vtt = to_vtt(text, fn)
            n = vtt.count("-->")
            if not n:
                continue
            tkey = hashlib.sha1("".join(re.findall(r"\d+:\d\d:\d\d\.\d+ --> \d+:\d\d:\d\d\.\d+", vtt)).encode(), usedforsecurity=False).hexdigest()
            same = None
            for old in d.r.get("subs") or []:
                if not old.get("tkey") and os.path.exists(old.get("file", "")):
                    old["tkey"] = hashlib.sha1("".join(re.findall(r"\d+:\d\d:\d\d\.\d+ --> \d+:\d\d:\d\d\.\d+", rd(old["file"]))).encode(), usedforsecurity=False).hexdigest()
                if old.get("tkey") == tkey:
                    same = old
                    break
            if same:  # this exact subtitle is already there: select it instead of adding a copy
                added.append(same)
                continue
            sid = "u" + uuid.uuid4().hex[:6]
            path = os.path.join(sub_dir(d.r["id"]), sid + ".vtt")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            wr(path, vtt)
            lang = guess_lang(fn, text)
            t = {"sid": sid, "lang": lang, "file": path, "name": fn, "cues": n, "tkey": tkey,
                 "label": label or "%s \u00b7 %s" % (LANG_NAMES.get(lang, "Subtitle"), fn[:48])}
            d.r.setdefault("subs", []).append(t)
            added.append(t)
        if not added:
            raise ValueError("could not read subtitles from this file")
        d.r["sub_default"] = added[0]["sid"]
        if added[0].get("sync") is None:  # new file: check its timing (an existing one was checked already)
            d.log("subtitle added: %s (%d lines)" % (added[0]["label"], added[0]["cues"]))
            threading.Thread(target=self.auto_sync, args=(d, added[0]["sid"]), daemon=True).start()
        self.save()
        return added

    def local_prefix(self, d):
        """files that hold the movie contiguously from the start (what is downloaded so far)"""
        if not d.media_ok():
            return []
        r = d.r
        L = r.get("size") or 0
        if r["status"] == "done":
            return [d.out] if os.path.exists(d.out) else []
        if r.get("ranges") is False:
            return [d.out + ".part"] if os.path.exists(d.out + ".part") else []
        files, a = [], int(rd(os.path.join(d.parts, "assembled"), "0") or 0)
        if a:
            files.append(d.out + ".assembling")
        for i in range(a, (L + C - 1) // C if L else 0):
            pth = d.cpath(i)
            if fsize(pth) != min(C, L - i * C):
                break
            files.append(pth)
        return files

    def thumb(self, d, t):
        """a small JPEG frame at second t (10 s steps) from what is downloaded; hardware decoded, HDR tone-mapped"""
        t = max(0, int(float(t) // 10 * 10))
        folder = os.path.join(STATE_DIR, "thumbs", d.r["id"])
        out = os.path.join(folder, "%d.jpg" % t)
        if fsize(out) > 0:
            return out
        if not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg missing")
        info = probe(self, d)
        dur, L = info.get("duration") or 0, d.r.get("size") or 0
        files = self.local_prefix(d)
        if not files:
            raise ValueError("nothing downloaded yet")
        if d.r["status"] != "done" and dur and L and t / dur > sum(fsize(f) for f in files) / L - 0.01:
            raise ValueError("not downloaded yet")
        with self.thumb_lock:  # one at a time: never disturbs playback
            if fsize(out) > 0:
                return out
            os.makedirs(folder, exist_ok=True)
            src = files[0] if len(files) == 1 else "concat:" + "|".join(files)
            hdr = info.get("transfer") in ("smpte2084", "arib-std-b67")
            deep = hdr or "10" in (info.get("pix") or "")
            vt = ("scale_vt=w=480:h=-2" + (":color_matrix=bt709:color_primaries=bt709:color_transfer=bt709" if hdr else "") +
                  ",hwdownload,format=" + ("p010le" if deep else "nv12") + ",format=yuvj420p")
            tmp = out + ".tmp.jpg"
            for hw in (True, False):
                cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"] + (["-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld"] if hw else [])
                cmd += ["-ss", str(t), "-i", src, "-frames:v", "1", "-vf", vt if hw else "scale=480:-2,format=yuvj420p", "-q:v", "4", tmp]
                try:
                    # security scan: argument list (no shell), absolute file paths and numbers only
                    subprocess.run(cmd, capture_output=True, timeout=25)  # nosemgrep
                except subprocess.TimeoutExpired:
                    continue
                if fsize(tmp) > 0:
                    os.replace(tmp, out)
                    return out
            raise ValueError("could not make a preview")

    def sub_path(self, d, sid):
        """path of a WebVTT file, None while an embedded track is still being extracted"""
        for t in d.r.get("subs") or []:
            if t["sid"] == sid:
                return t["file"]
        mm = re.match(r"^e(\d+)$", sid)
        if not mm:
            raise KeyError(sid)
        k = int(mm.group(1))
        info = probe(self, d)
        dur = info.get("duration") or 0
        done = d.r["status"] == "done"
        # partly downloaded: only read up to 5 min before the edge (never wait for the download)
        avail = dur if done else d.snap.get("prefix", 0) / max(1, d.r.get("size") or 1) * dur
        out = os.path.join(sub_dir(d.r["id"]), "e%d.vtt" % k)
        have = float(rd(out + ".t", "0") or 0)
        fresh = os.path.exists(out) and (have >= dur - 1 or (not done and have >= avail - 600))
        key = (d.r["id"], k)
        files = self.local_prefix(d)
        with self.sub_lock:  # several subtitles may ask at once: only one extraction per track
            start = not fresh and avail > 0 and files and key not in self.sub_jobs and shutil.which("ffmpeg")
            if start:
                self.sub_jobs[key] = None
        if start:
            covered = dur if done else sum(fsize(f) for f in files) / max(1, d.r.get("size") or 1) * dur

            def job():
                try:
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    src = files[0] if len(files) == 1 else "concat:" + "|".join(files)
                    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-map", "0:s:%d" % k, "-f", "webvtt", out + ".tmp"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
                    if fsize(out + ".tmp") > 10:
                        os.replace(out + ".tmp", out)
                        wr(out + ".t", str(covered))
                except (OSError, subprocess.SubprocessError):
                    log.exception("subtitle extraction")
                finally:
                    self.sub_jobs.pop(key, None)
            th = threading.Thread(target=job, daemon=True, name="subs")
            self.sub_jobs[key] = th
            th.start()
        return out if os.path.exists(out) else None

    SUB_LANGS = {"fa": "farsi_persian", "en": "english", "ar": "arabic", "tr": "turkish", "fr": "french", "es": "spanish", "de": "german"}

    OS_LANG = {"fa": "per", "en": "eng", "ar": "ara", "tr": "tur", "fr": "fre", "es": "spa", "de": "ger"}

    @staticmethod
    def web(url, timeout=25, limit=4 << 20, referer=None, headers=None):
        h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/537.36 Chrome/140 Safari/537.36"}
        h.update(headers or {})
        if referer:
            h["Referer"] = referer
        if not re.match(r"^https?://", url):
            raise ValueError("not a web link")
        # security scan: scheme checked
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as f:  # nosec B310
            return f.read(limit)

    def subs_search(self, d, lang="fa", query=""):
        """search subf2m.co, rank by how well each subtitle's releases match our file"""
        title, year, _ = parse_release(d.r["name"])
        q = query.strip() or ("%s %s" % (title, year)).strip()
        code = self.SUB_LANGS.get(lang, lang)
        base = "https://subf2m.co"
        try:
            page = self.web(base + "/subtitles/searchbytitle?query=" + urllib.parse.quote_plus(q)).decode("utf-8", "ignore")
        except OSError as e:
            raise ValueError("subtitle site not reachable: %s" % e)
        slugs = []
        for slug, label in re.findall(r'href="/subtitles/([a-z0-9-]+)">([^<]+)', page):
            lab = html.unescape(label)
            if (not year or year in lab) and slug not in slugs:
                slugs.append(slug)
        results = []
        for slug in slugs[:3]:
            try:
                t = self.web("%s/subtitles/%s/%s" % (base, slug, code)).decode("utf-8", "ignore")
            except OSError:
                continue
            for it in re.split(r"<li class='item[^']*'>", t)[1:]:
                link = re.search(r"href='(/subtitles/%s/%s/(\d+))'" % (re.escape(slug), code), it)
                if not link:
                    continue
                rels = [html.unescape(x).strip() for x in re.findall(r"<li>([^<]{3,})</li>", it)]
                who = re.search(r'href="/u/[^"]*">\s*([^<]+?)\s*<', it)
                note = re.search(r"<p>(.*?)</p>", it, re.S)
                rate = re.search(r"class='rate ([^']*)'", it)
                note_txt = html.unescape(re.sub(r"<[^>]+>", "", note.group(1))).strip() if note else ""
                scores = [(release_score(d.r["name"], x), x) for x in rels] or [(0, "")]
                sc, best_rel = max(scores)
                site = re.search(r"_(30nama|digimoviez|film2media|avamovie|zardfilm)\b", d.r["name"].lower())
                if site and site.group(1) in note_txt.lower().replace(" ", ""):
                    sc = min(100, sc + 10)  # made for the same site's release
                results.append({"id": link.group(2), "path": link.group(1), "slug": slug, "score": sc, "release": best_rel,
                                "releases": rels[:12], "by": who.group(1) if who else "", "note": note_txt[:160],
                                "rated": (rate.group(1) if rate else "").replace("not rated", "").strip(),
                                "added": any(x.get("src") == link.group(2) for x in d.r.get("subs") or [])})
        for x in results:
            x["src"] = "subf2m"
        # second source: OpenSubtitles
        try:
            url = "https://rest.opensubtitles.org/search/query-%s/sublanguageid-%s" % (urllib.parse.quote(q.lower()), self.OS_LANG.get(lang, "per"))
            for x in json.loads(self.web(url, headers={"X-User-Agent": "TemporaryUserAgent"}).decode("utf-8", "ignore") or "[]"):
                link = x.get("SubDownloadLink") or ""
                if not link.startswith("https://dl.opensubtitles.org/") or x.get("SubFormat") not in ("srt", "ass", "ssa", "vtt", "sub"):
                    continue
                rel = x.get("MovieReleaseName") or ""
                sc = max(release_score(d.r["name"], rel), release_score(d.r["name"], x.get("SubFileName") or ""))
                site = re.search(r"_(30nama|digimoviez|film2media|avamovie|zardfilm)\b", d.r["name"].lower())
                if site and site.group(1) in (rel + (x.get("SubFileName") or "")).lower():
                    sc = min(100, sc + 10)
                results.append({"id": "os" + str(x.get("IDSubtitleFile")), "path": link, "src": "opensubtitles", "score": sc,
                                "release": rel.strip() or x.get("SubFileName", ""), "releases": [rel.strip(), x.get("SubFileName", "")],
                                "by": x.get("UserNickName") or "OpenSubtitles", "name": x.get("SubFileName") or "subtitle.srt",
                                "note": "%s downloads \u00b7 rating %s" % (x.get("SubDownloadsCnt", "?"), x.get("SubRating", "-")),
                                "rated": "positive" if float(x.get("SubRating") or 0) >= 8 else "",
                                "added": any(t.get("src") == "os" + str(x.get("IDSubtitleFile")) for t in d.r.get("subs") or [])})
        except (OSError, ValueError):
            log.info("opensubtitles search failed")
        results.sort(key=lambda x: (-x["score"], x["rated"] != "positive"))
        for x in results:
            x["best"] = False
        if results:
            results[0]["best"] = True
        return {"query": q, "lang": lang, "results": results[:50], "sources": ["subf2m", "OpenSubtitles"]}

    def subs_get(self, d, path, label):
        page_url = "https://subf2m.co" + path
        try:
            page = self.web(page_url).decode("utf-8", "ignore")
            dl = re.search(r"href=['\"](/subtitles/[^'\"]*download[^'\"]*)['\"]", page)
            if not dl:
                raise ValueError("no download link on the subtitle page")
            raw = self.web("https://subf2m.co" + html.unescape(dl.group(1)), 60, 20 << 20, referer=page_url)
        except OSError as e:
            raise ValueError("download failed: %s" % e)
        added = self.subs_add(d, "subtitle.zip", raw, label)
        for t in added:
            t["src"] = path.rsplit("/", 1)[-1]
        self.save()
        return added

    def speech_map(self, d, wait=False):
        """0/1 per 0.1 s: where people speak (voice band of the centre channel). Made once per film (~1 min for 3 h)
        in the background and cached; None while it is being made"""
        f = os.path.join(sub_dir(d.r["id"]), "speech.bin")
        if not d.media_ok():
            return None
        if fsize(f) > 0:
            return list(open(f, "rb").read())
        if d.r["status"] != "done" or not os.path.exists(d.out):
            raise ValueError("auto sync works when the film is fully downloaded")
        with self.lock:
            busy = d.r["id"] in self.speech_jobs
            if not busy:
                self.speech_jobs.add(d.r["id"])

        def job():
            try:
                info = probe(self, d)
                multi = max([x.get("ch") or 0 for x in info.get("audio") or []] + [0]) >= 3
                af = ("pan=mono|c0=FC," if multi else "pan=mono|c0=0.5*c0+0.5*c1,") + "highpass=f=250,lowpass=f=3500,aresample=8000," \
                     "asetnsamples=n=800:p=0,astats=metadata=1:reset=1:measure_overall=RMS_level:measure_perchannel=none," \
                     "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
                p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", d.out, "-map", "0:a:0", "-vn", "-af", af, "-f", "null", "-"],
                                   capture_output=True, text=True, timeout=3600)
                rms = [float(x) if x not in ("-inf", "inf", "nan") else -120.0 for x in re.findall(r"RMS_level=(\S+)", p.stdout)]
                sp, floor = bytearray(), -60.0
                for i, v in enumerate(rms):  # speech = clearly above the local background level (30 s median)
                    if i % 50 == 0:
                        win = sorted(rms[max(0, i - 150):i + 150])
                        floor = win[len(win) // 2]
                    sp.append(1 if v > floor + 6 and v > -60 else 0)
                if sp:
                    os.makedirs(os.path.dirname(f), exist_ok=True)
                    open(f + ".new", "wb").write(bytes(sp))
                    os.replace(f + ".new", f)
                    log.info("speech map for %s: %d s", d.r["num"], len(sp) // 10)
            except Exception:
                log.exception("speech map")
            finally:
                self.speech_jobs.discard(d.r["id"])
        if not busy:
            threading.Thread(target=job, daemon=True, name="speech").start()
        if wait:
            end = time.time() + 3600
            while time.time() < end and d.r["id"] in self.speech_jobs:
                time.sleep(2)
            return list(open(f, "rb").read()) if fsize(f) > 0 else None
        return None

    def speech_sync_track(self, d, sid, apply=True):
        """match subtitle `sid` to the film's speech and apply what fits (see speech_sync)"""
        t = next((x for x in d.r.get("subs") or [] if x["sid"] == sid), None)
        path = t["file"] if t else self.sub_path(d, sid)
        if not path:
            raise ValueError("this subtitle is still being prepared")
        speech = self.speech_map(d)
        if speech is None:
            return {"pending": True, "eta": int((probe(self, d).get("duration") or 6000) / 170) + 5}
        with self.lock:
            lk = self.sync_locks.setdefault(sid, threading.Lock())
        with lk:  # the automatic sync (new subtitle) and the Auto sync button must not rewrite the file at the same time
            return self._speech_sync_track(d, sid, t, path, speech, apply)

    def _speech_sync_track(self, d, sid, t, path, speech, apply):
        src = path + ".orig" if t is not None and os.path.exists(path + ".orig") else path  # always fit the untouched file
        text = rd(src).replace("\r", "")
        cues = speech_starts(src)
        if len(cues) < 20:
            raise ValueError("too few subtitle lines to sync")
        res = speech_sync(cues, speech)
        chosen = d.r.get("sub_default") == sid or not d.r.get("sub_default")
        pct = max(0, min(99, int(res["confidence"] * 18)))
        out = {"verdict": res["verdict"], "offset": res["offset"], "scale": res["scale"], "confidence": res["confidence"], "confidence_pct": pct, "applied": False}
        if res["verdict"] == "nomatch":
            out.update(error="this subtitle does not match this version of the film (its parts fit at different times) - choose another one",
                       delay=d.r.get("sub_delay", 0), scale=d.r.get("sub_scale", 1))
        elif res["verdict"] == "sections" and t is not None and apply:
            # another cut of the film: write a corrected copy line by line (the original is kept as .orig)
            if not os.path.exists(path + ".orig"):
                shutil.copyfile(path, path + ".orig")
            lines, k = [], 0
            for blk in re.split(r"\n\s*\n", text):
                mm = re.search(r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d{3}) --> (?:(\d+):)?(\d\d):(\d\d)[.,](\d{3})", blk)
                if not mm:
                    continue
                g = [int(x or 0) for x in mm.groups()]
                a = (g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000) * res["scale"] + res["shifts"][min(k, len(res["shifts"]) - 1)]
                b = (g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000) * res["scale"] + res["shifts"][min(k, len(res["shifts"]) - 1)]
                k += 1
                if b <= 0:
                    continue
                f = lambda x: "%02d:%02d:%02d.%03d" % (x // 3600, x % 3600 // 60, x % 60, round(x % 1 * 1000) % 1000)
                lines.append("%s --> %s\n%s" % (f(max(0, a)), f(b), blk[mm.end():].strip("\n")))
            wr(path, "WEBVTT\n\n" + "\n\n".join(lines) + "\n")
            t["fixed"] = "sections"
            if chosen:
                d.r["sub_delay"], d.r["sub_scale"] = 0, 1
            out.update(applied=True, delay=0, scale=1, offset=res["runs"][0]["off"],
                       note="fixed in %d parts (%s)" % (len(res["runs"]), ", ".join("%+.1f s from %s" % (r["off"], fmt_hms(r["from"])) for r in res["runs"])))
            d.log("subtitle fitted to another cut: %s" % out["note"])
        elif res["verdict"] == "fits":
            out["applied"] = apply and res["confidence"] >= 3.5
            if out["applied"] and chosen:
                d.r["sub_delay"], d.r["sub_scale"] = res["offset"], res["scale"]
                d.log("subtitle synced to the speech: %+.2f s%s (%d%% sure)" % (res["offset"], "" if res["scale"] == 1 else ", speed x%.4f" % res["scale"], pct))
            out.update(delay=d.r.get("sub_delay", 0) if out["applied"] else res["offset"], scale=d.r.get("sub_scale", 1) if out["applied"] else res["scale"])
        if t is not None:
            t["speech"] = {k2: v for k2, v in out.items() if k2 in ("verdict", "offset", "scale", "confidence", "note")}
        self.save()
        return out

    def auto_sync(self, d, sid):
        """compare the new subtitle with the movie's own text subtitle: report the match and fix a constant offset"""
        try:
            t = next(x for x in d.r.get("subs") or [] if x["sid"] == sid)
            ref_k = next((x["k"] for x in probe(self, d).get("subs") or [] if x["codec"] in TEXT_SUBS), None)
            if ref_k is None:
                return
            ref = None
            for _ in range(240):  # the reference track may still be extracting
                ref = self.sub_path(d, "e%d" % ref_k)
                if ref and (d.r["id"], ref_k) not in self.sub_jobs:
                    break
                time.sleep(2)
            res = ref and sync_check(vtt_starts(t["file"]), vtt_starts(ref))
            if not res:
                return
            t["sync"] = res
            if abs(res["offset"]) >= 0.3 and res["match"] >= 30 and d.r.get("sub_default") == sid:
                d.r["sub_delay"] = res["offset"]
                d.log("subtitle auto-synced by %+.2f s (%d%% of lines line up with the movie)" % (res["offset"], res["match"]))
            else:
                d.log("subtitle timing checked: %d%% of lines line up with the movie%s" % (
                    res["match"], "" if abs(res["offset"]) < 0.3 else " (offset %+.2f s not applied)" % res["offset"]))
            self.save()
        except Exception:
            log.exception("auto sync")
        finally:
            self.sync_running.discard(sid)
        try:  # the speech check works for every film (with or without an embedded subtitle)
            if d.r["status"] == "done":
                self.speech_map(d, wait=True)
                self.speech_sync_track(d, sid)
        except Exception:
            log.exception("speech sync")

    def subs_action(self, d, act, body):
        if act == "search":
            return self.subs_search(d, body.get("lang") or "fa", body.get("query") or "")
        if act == "autosync":
            return self.speech_sync_track(d, str(body.get("sid") or d.r.get("sub_default") or ""))
        if act == "get":
            path = body.get("path") or ""
            if path.startswith("https://dl.opensubtitles.org/"):
                try:
                    raw = self.web(path, 60, 20 << 20)
                except OSError as e:
                    raise ValueError("download failed: %s" % e)
                added = self.subs_add(d, body.get("name") or "subtitle.srt", raw, body.get("label"))
                for t in added:
                    t["src"] = body.get("id") or ""
                self.save()
                return {"added": [t["sid"] for t in added]}
            if not re.match(r"^/subtitles/[a-z0-9-]+/[a-z_]+/\d+$", path):
                raise ValueError("bad subtitle reference")
            return {"added": [t["sid"] for t in self.subs_get(d, path, body.get("label"))]}
        if act == "add":
            if body.get("url"):
                url = body["url"].strip()
                if not re.match(r"^https?://", url):
                    raise ValueError("not a http(s) link")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) dlus/" + VERSION})
                try:
                    # security scan: scheme checked above
                    with urllib.request.urlopen(req, timeout=30) as f:  # nosec B310
                        raw = f.read(20 << 20)
                        cd = f.headers.get("Content-Disposition") or ""
                except OSError as e:
                    raise ValueError("could not download the subtitle: %s" % e)
                mm = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
                name = urllib.parse.unquote(mm.group(1)) if mm else name_from_url(url)
            else:
                name, raw = body.get("name") or "subtitle.srt", base64.b64decode(body.get("b64") or "")
            return {"added": [t["sid"] for t in self.subs_add(d, name, raw)]}
        if act == "del":
            d.r["subs"] = [t for t in d.r.get("subs") or [] if t["sid"] != body.get("sid") or rm(t["file"])]
        elif act == "set":
            if "sid" in body:
                d.r["sub_default"] = str(body["sid"] or "")
            if "delay" in body:
                d.r["sub_delay"] = round(float(body["delay"]), 2)
            if "scale" in body:
                d.r["sub_scale"] = float(body["scale"]) if 0.9 < float(body["scale"]) < 1.1 else 1.0
            if "sid" in body and "scale" not in body:
                d.r["sub_scale"] = next((x.get("speech", {}).get("scale", 1) for x in d.r.get("subs") or [] if x["sid"] == str(body["sid"] or "")), 1)
        else:
            raise KeyError(act)
        self.save()
        return {"ok": True}

    # ---------------------------------------------------------- TVs on the network (DLNA renderers)
    def find_tvs(self, wait=3.0):
        ip = self.lan_ip()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if ip != "127.0.0.1":
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.bind((ip, 0))
        for st in ("urn:schemas-upnp-org:device:MediaRenderer:1", "urn:schemas-upnp-org:service:AVTransport:1"):
            msg = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: %s\r\n\r\n' % st).encode()
            for _ in range(2):
                s.sendto(msg, ("239.255.255.250", 1900))
        s.settimeout(0.4)
        locs, end = {}, time.time() + wait
        while time.time() < end:
            try:
                data, addr = s.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            mm = re.search(r"^LOCATION:\s*(\S+)", data.decode("utf-8", "ignore"), re.M | re.I)
            if mm:
                locs[mm.group(1)] = addr[0]
        s.close()
        found = {}
        for loc, host in locs.items():
            # a device may only describe itself: plain http to the address that answered (no file:// or other hosts)
            if urllib.parse.urlsplit(loc).scheme != "http" or urllib.parse.urlsplit(loc).hostname != host:
                continue
            try:
                # security scan: http to the answering device only
                xml = urllib.request.urlopen(loc, timeout=4).read(1 << 20).decode("utf-8", "ignore")  # nosec B310
            except (OSError, ValueError):
                continue
            tag = lambda t, x=xml: html.unescape((re.search(r"<%s>(.*?)</%s>" % (t, t), x, re.S) or [None, ""])[1].strip())
            ctl = next((tag("controlURL", b) for b in re.findall(r"<service>(.*?)</service>", xml, re.S)
                        if "AVTransport" in tag("serviceType", b)), "")
            if not ctl:
                continue
            ctl = urllib.parse.urljoin(tag("URLBase") or loc, ctl)
            if urllib.parse.urlsplit(ctl).scheme != "http" or urllib.parse.urlsplit(ctl).hostname != host:
                continue  # its control address must be the device itself (stream links with the TV code go there)
            udn = tag("UDN") or loc
            mm = re.search(r" at ([0-9a-f:]{11,17}) ", sh(["arp", "-n", host], 3))
            mac = ":".join(x.zfill(2) for x in mm.group(1).split(":")) if mm else ""
            found[udn] = {"id": re.sub(r"\W", "", udn)[-16:], "name": tag("friendlyName") or host, "maker": tag("manufacturer"), "mac": mac,
                          "model": tag("modelName"), "ip": host, "ctl": ctl, "seen": time.time()}
        tvs = {t["id"]: t for t in self.settings.get("tvs") or []}
        for t in found.values():  # keep a known hardware address if this scan could not read it
            t["mac"] = t["mac"] or (tvs.get(t["id"]) or {}).get("mac", "")
        tvs.update({t["id"]: t for t in found.values()})
        self.settings["tvs"] = sorted(tvs.values(), key=lambda t: -t.get("seen", 0))[:12]
        self.save()
        return [dict(t, online=t["id"] in {f["id"] for f in found.values()}) for t in self.settings["tvs"]]

    @staticmethod
    def av_call(ctl, action, args):
        inner = "".join("<%s>%s</%s>" % (k, xesc(str(v)), k) for k, v in args)
        body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                '%s</u:%s></s:Body></s:Envelope>' % (action, inner, action))
        req = urllib.request.Request(ctl, body.encode(), {"Content-Type": 'text/xml; charset="utf-8"',
                                                         "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#%s"' % action})
        try:
            # security scan: http to the TV's own address only (checked at discovery)
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")  # nosec B310
        except urllib.error.HTTPError as e:
            fault = re.search(r"<errorDescription>(.*?)</errorDescription>", e.read().decode("utf-8", "ignore"))
            raise ValueError("the TV refused %s (%s)" % (action, fault.group(1) if fault else "HTTP %d" % e.code))
        except OSError as e:
            raise ValueError("cannot reach the TV: %s" % e)

    def cast(self, d, tvid, sound, at=0.0):
        self.need_lan()
        tv = self.tv_ensure(tvid)
        self.lan_allow(tv["ip"], "you sent a film to this TV from the Mac", tv.get("name", ""))
        sub_on = bool(self.subs_info(d)["sid"])
        v = "s" if sound == "tv" else ("t" if sub_on else "")
        if v and not shutil.which("ffmpeg"):
            raise ValueError("ffmpeg is needed for TV sound / subtitles (brew install ffmpeg)")
        url = "http://%s:%d/%s/%s" % (self.lan_ip(), int(self.settings["lan_port"]), self.settings["lan_token"], self.short_path(d, v))
        if v and at > 1:
            url += "?t=%.1f" % at
        mime = "video/x-matroska" if v else MIME.get(d.r["name"].rsplit(".", 1)[-1].lower(), "video/x-matroska")
        meta = DIDL % didl_item({"id": d.r["id"], "title": d.r["name"].rsplit(".", 1)[0], "url": url, "mime": mime,
                                 "cls": "object.item.videoItem", "size": 0 if v else d.r.get("size") or 0,
                                 "flags": DLNA_LIVE if v else DLNA_FLAGS, "dur": probe(self, d).get("duration") or 0})
        try:
            self.av_call(tv["ctl"], "Stop", [("InstanceID", 0)])
        except ValueError:
            pass
        self.av_call(tv["ctl"], "SetAVTransportURI", [("InstanceID", 0), ("CurrentURI", url), ("CurrentURIMetaData", meta)])
        try:
            self.av_call(tv["ctl"], "Play", [("InstanceID", 0), ("Speed", 1)])
        except ValueError as e:  # some TVs answer Play only after they started buffering
            if "timed out" not in str(e):
                raise
        if not v and at > 1:  # the untouched file: ask the TV's player to jump there once it plays
            def seek_later():
                for _ in range(10):
                    time.sleep(1.5)
                    try:
                        self.av_call(tv["ctl"], "Seek", [("InstanceID", 0), ("Unit", "REL_TIME"),
                                                         ("Target", "%d:%02d:%02d" % (at // 3600, at % 3600 // 60, at % 60))])
                        return
                    except ValueError:
                        continue
            threading.Thread(target=seek_later, daemon=True).start()
        self.casting = {"tv": tvid, "name": tv["name"], "id": d.r["id"], "title": d.r["name"], "url": url, "t": time.time()}
        d.log("playing on %s (%s)" % (tv["name"], "TV sound" if v == "s" else "original sound") + (" with subtitle" if sub_on else ""))
        return self.casting

    TV_BUTTONS = {"UP", "DOWN", "LEFT", "RIGHT", "ENTER", "BACK", "HOME", "EXIT", "RED", "GREEN", "YELLOW", "BLUE", "INFO", "MENU",
                  "PLAY", "PAUSE", "STOP", "REWIND", "FASTFORWARD", "VOLUMEUP", "VOLUMEDOWN", "MUTE", "CHANNELUP", "CHANNELDOWN",
                  "QMENU", "SETTINGS", "AI", "MIC", "HOMEHUB", "A11Y", "INPUT", "GUIDE", "LIST", "AD", "SAP", "ASPECT_RATIO", "RECENT", "DASH",
                  "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "POWER"}
    # buttons LG keeps for its own signed remotes: open the screen they lead to instead
    TV_LAUNCH = {"HOME": ["com.webos.app.home"], "SETTINGS": ["com.palm.app.settings"], "MENU": ["com.palm.app.settings"],
                 "QMENU": ["com.palm.app.settings"], "A11Y": ["com.palm.app.settings"], "HOMEHUB": ["com.webos.app.homeconnect", "com.webos.app.iot-thirdparty-login"],
                 "AI": ["com.webos.app.aiconcierge", "com.webos.app.voice"], "MIC": ["com.webos.app.voice"], "GUIDE": ["com.webos.app.livemenu"],
                 "LIST": ["com.webos.app.livemenu"], "INPUT": ["com.webos.app.inputpicker", "com.webos.app.inputmgr"], "RECENT": ["com.webos.app.recentsapps"]}
    PLAYER_KEYS = {"UP", "DOWN", "LEFT", "RIGHT", "ENTER", "BACK", "RED", "GREEN", "YELLOW", "BLUE", "INFO", "PLAY", "PAUSE", "STOP", "REWIND", "FASTFORWARD"}
    MEDIA = {"PLAY": "play", "PAUSE": "pause", "STOP": "stop", "REWIND": "rewind", "FASTFORWARD": "fastForward"}

    def tv_button(self, c, name):
        player = self.tv_now if time.time() - (self.tv_now or {}).get("at", 0) < 5 else None
        if name in ("VOLUMEUP", "VOLUMEDOWN"):
            c.request("ssap://audio/" + ("volumeUp" if name == "VOLUMEUP" else "volumeDown"))
            return {"ok": True, "via": "api"}
        if name == "MUTE":
            st = c.request("ssap://audio/getStatus")
            c.request("ssap://audio/setMute", {"mute": not st.get("mute", st.get("muted", False))})
            return {"ok": True, "via": "api"}
        if name in ("CHANNELUP", "CHANNELDOWN"):
            c.request("ssap://tv/" + ("channelUp" if name == "CHANNELUP" else "channelDown"))
            return {"ok": True, "via": "api"}
        if name == "POWER":
            c.request("ssap://system/turnOff")
            return {"ok": True, "via": "api"}
        if player and name in self.PLAYER_KEYS:
            self.tv_push({"key": name})  # the dlus player on the TV executes it
            return {"ok": True, "via": "player", "player": player}
        if name in self.MEDIA:
            c.request("ssap://media.controls/" + self.MEDIA[name])
            return {"ok": True, "via": "api"}
        if not getattr(c, "no_pointer", False):
            try:
                c.button(name)
                return {"ok": True, "via": "remote"}
            except (OSError, ValueError) as e:
                c.no_pointer = "401" in str(e) or "insufficient" in str(e).lower()
                c.btn = None
        for app in self.TV_LAUNCH.get(name, []):
            try:
                c.request("ssap://system.launcher/launch", {"id": app})
                return {"ok": True, "via": "app", "app": app}
            except ValueError:
                continue
        raise ValueError("Your TV does not let other apps press %s (LG keeps it for its own remote). It works inside the dlus player on the TV." % name)

    @staticmethod
    def tv_up(ip, wait=1.0):
        for port in (3001, 1693, 3000):
            try:
                socket.create_connection((ip, port), wait).close()
                return True
            except OSError:
                continue
        return False

    def tv_wake(self, tv):
        mac = (tv.get("mac") or "").replace(":", "")
        if len(mac) != 12:
            return False
        pkt = b"\xff" * 6 + bytes.fromhex(mac) * 16
        lan = self.gw.lan() or {}
        bcast = str(ipaddress.IPv4Network(lan["cidr"]).broadcast_address) if lan.get("cidr") else "255.255.255.255"
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for dst in {bcast, "255.255.255.255"}:
            for port in (9, 7):
                try:
                    s_.sendto(pkt, (dst, port))
                except OSError:
                    pass
        s_.close()
        return True

    def tv_ensure(self, tvid, wait=20):
        """the TV must be on the network: wake it, wait, and look for it again if its address changed"""
        tv = next((t for t in self.settings.get("tvs") or [] if t["id"] == tvid), None)
        if not tv:
            raise ValueError("TV not found - press Find TVs")
        if self.tv_up(tv["ip"]):
            return tv
        woke = self.tv_wake(tv)
        end = time.time() + wait
        while time.time() < end:
            time.sleep(2)
            if self.tv_up(tv["ip"]):
                return tv
            for t in self.find_tvs(wait=1.5):
                if t["id"] == tvid and t.get("online") and t["ip"] != tv["ip"]:
                    self.tv_conns.pop(tvid, None)
                    return t
        raise ValueError("Your TV (%s) is not on the network right now. Turn it on and check it is connected to the same Wi-Fi/hotspot as this Mac "
                         "(iPhone hotspots drop idle devices).%s" % (tv["ip"], " dlus also tried to wake it - to make that work, turn on "
                         "'TV On With Mobile / Turn on via Wi-Fi' in the TV's settings." if woke else ""))

    def tv_remote(self, tvid):
        tv = self.tv_ensure(tvid)
        c = self.tv_conns.get(tvid)
        if c and c.sock:
            return tv, c
        pins = self.settings.setdefault("tv_pins", {})
        c = WebOSTV(tv["ip"], (self.settings.get("tv_keys") or {}).get(tvid, ""), pins.get(tvid, ""))
        try:
            key = c.connect(wait=60)
        except TVCertChanged as e:
            self.audit("TV %s: certificate changed since pairing - connection refused" % tv.get("name", tvid), tv["ip"], level="warn")
            raise ValueError(str(e))
        if key and c.seen_pin and c.seen_pin != pins.get(tvid) and not (pins.get(tvid, "").startswith("tls:") and c.seen_pin == "plain"):
            pins[tvid] = c.seen_pin  # first pairing, or an old TV that now speaks the encrypted port
            self.save()
        if key:
            self.lan_allow(tv["ip"], "paired TV you control from the Mac", tv.get("name", ""))
        if key and key != (self.settings.get("tv_keys") or {}).get(tvid):
            self.settings.setdefault("tv_keys", {})[tvid] = key
            self.save()
            try:
                c.request("ssap://system.notifications/createToast", {"message": "dlus is connected"})
            except (OSError, ValueError):
                pass
        self.tv_conns[tvid] = c
        return tv, c

    def tv_push(self, cmd):
        with self.lock:
            self.tv_seq += 1
            self.tv_cmds.append(dict(cmd, seq=self.tv_seq, at=time.time()))

    def need_lan(self):
        """TVs and phones reach dlus through its network port (8643). It stays closed until the user allows it."""
        if not self.settings.get("lan_on"):
            raise ValueError("NEED_LAN")

    def tv_lan_base(self):
        self.need_lan()
        return "http://%s:%d/%s" % (self.lan_ip(), int(self.settings["lan_port"]), self.settings["lan_token"])

    def tv_action(self, tvid, act, body):
        if act == "forget":  # unpair: drop the key and the remembered certificate; the next connection asks on the TV again
            (self.settings.get("tv_keys") or {}).pop(tvid, None)
            (self.settings.get("tv_pins") or {}).pop(tvid, None)
            c = self.tv_conns.pop(tvid, None)
            if c and c.sock:
                try:
                    c.sock.close()
                except OSError:
                    pass
            self.audit("TV forgotten (pairing removed): %s" % tvid)
            self.save()
            return {"ok": True}
        if act == "open":
            self.need_lan()  # ask before anything is opened on the network
        for attempt in (0, 1):  # a stale connection is reopened once
            try:
                tv, c = self.tv_remote(tvid)
                if act in ("pair", "wake"):
                    return {"ok": True, "paired": True, "name": tv["name"]}
                if act == "open":  # dlus on the TV: its home screen, a shared folder, or straight into a movie
                    base = self.tv_lan_base()
                    url = base + ("/netsetup" if body.get("page") == "netsetup" else "/tv")
                    if body.get("folder") is not None:
                        rel = str(body.get("rel") or "")
                        full = self.shared_path(int(body["folder"]), rel)
                        if not full:
                            raise ValueError("not a shared folder")
                        if os.path.isfile(full):
                            body = {"id": self.import_local(full).r["id"]}
                        else:
                            url = "%s/files/sdir/%d/%s" % (base, int(body["folder"]), urllib.parse.quote(rel.strip("/") + "/" if rel.strip("/") else ""))
                    if body.get("id"):
                        d = self.dls.get(body["id"])
                        if not d:
                            raise ValueError("no such download")
                        url = "%s/%d?autoplay=1%s" % (base, d.r["num"], "&t=%.0f" % float(body["at"]) if body.get("at") else "")
                    c.request("ssap://system.launcher/open", {"target": url})
                    return {"ok": True, "url": url}
                if act == "button":
                    name = str(body.get("name", "")).upper()
                    if name not in self.TV_BUTTONS:
                        raise ValueError("unknown button")
                    return self.tv_button(c, name)
                if act == "status":
                    out = {"ok": True, "player": self.tv_now if time.time() - (self.tv_now or {}).get("at", 0) < 5 else None}
                    for k, uri in (("audio", "ssap://audio/getStatus"), ("app", "ssap://com.webos.applicationManager/getForegroundAppInfo")):
                        try:
                            out[k] = c.request(uri)
                        except ValueError:
                            out[k] = None
                    a = out.pop("audio") or {}
                    vs = a.get("volumeStatus") or {}
                    out["volume"], out["muted"] = a.get("volume", vs.get("volume")), a.get("mute", vs.get("muteStatus"))
                    out["app"] = (out.get("app") or {}).get("appId", "")
                    return out
                if act == "apps":
                    lp = c.request("ssap://com.webos.applicationManager/listLaunchPoints").get("launchPoints") or []
                    return {"ok": True, "apps": [{"id": a.get("id"), "title": a.get("title"), "icon": a.get("largeIcon") or a.get("icon") or "",
                                                  "color": a.get("bgColor") or ""} for a in lp if a.get("id")]}
                if act == "launch":
                    app = str(body.get("app", ""))
                    if not re.match(r"^[\w.\-]+$", app):
                        raise ValueError("bad app id")
                    c.request("ssap://system.launcher/launch", {"id": app})
                    return {"ok": True}
                if act == "inputs":
                    dv = c.request("ssap://tv/getExternalInputList").get("devices") or []
                    return {"ok": True, "inputs": [{"id": x.get("id"), "label": x.get("label") or x.get("id"), "connected": x.get("connected"),
                                                    "icon": x.get("icon") or ""} for x in dv]}
                if act == "input":
                    c.request("ssap://tv/switchInput", {"inputId": str(body.get("input", ""))})
                    return {"ok": True}
                if act == "setvol":
                    c.request("ssap://audio/setVolume", {"volume": max(0, min(100, int(body.get("level", 10))))})
                    return {"ok": True}
                if act == "text":
                    if body.get("enter"):
                        c.request("ssap://com.webos.service.ime/sendEnterKey")
                    elif body.get("delete"):
                        c.request("ssap://com.webos.service.ime/deleteCharacters", {"count": int(body["delete"])})
                    else:
                        c.request("ssap://com.webos.service.ime/insertText", {"text": str(body.get("text", ""))[:500], "replace": 0})
                    return {"ok": True}
                if act == "screen":
                    on = bool(body.get("on"))
                    for uri in (("ssap://com.webos.service.tvpower/power/turnOnScreen", "ssap://com.webos.service.tv.power/turnOnScreen") if on else
                                ("ssap://com.webos.service.tvpower/power/turnOffScreen", "ssap://com.webos.service.tv.power/turnOffScreen")):
                        try:
                            c.request(uri, {"standbyMode": "active"})
                            return {"ok": True}
                        except ValueError as e:
                            err = e
                    raise err
                if act == "picture":  # the TV's picture mode (read-only for remote apps) and the modes it offers now
                    if body.get("open"):
                        c.request("ssap://system.launcher/launch", {"id": "com.palm.app.settings", "params": {"target": "picture"}})
                        return {"ok": True}
                    cur = c.request("ssap://settings/getSystemSettings", {"category": "picture", "keys": ["pictureMode", "brightness", "contrast", "color", "backlight"]})
                    modes = []
                    try:
                        desc = c.request("ssap://settings/getSystemSettingDesc", {"category": "picture", "keys": ["pictureMode"]})
                        modes = [v["value"] for v in ((desc.get("results") or [{}])[0].get("values") or {}).get("arrayExt", []) if v.get("visible")]
                    except ValueError:
                        pass
                    return dict(cur.get("settings") or {}, ok=True, modes=modes)
                if act == "netsettings":  # VPN setup: open the TV's network settings and keep the values on screen
                    try:
                        c.request("ssap://system.launcher/launch", {"id": "com.palm.app.settings", "params": {"target": "network"}})
                    except ValueError:
                        c.request("ssap://system.launcher/launch", {"id": "com.palm.app.settings"})
                    self.gw_toast_loop(tvid)
                    return {"ok": True}
                if act == "seek":
                    self.tv_push({"seek": float(body.get("by", 0))})
                    return {"ok": True}
                if act == "toast":
                    c.request("ssap://system.notifications/createToast", {"message": str(body.get("message", "dlus"))[:120]})
                    return {"ok": True}
                if act == "volume":
                    c.request("ssap://audio/" + {"up": "volumeUp", "down": "volumeDown"}.get(body.get("dir"), "volumeUp"))
                    return {"ok": True}
                if act == "mute":
                    c.request("ssap://audio/setMute", {"mute": bool(body.get("mute", True))})
                    return {"ok": True}
                if act == "off":
                    c.request("ssap://system/turnOff")
                    return {"ok": True}
                raise KeyError(act)
            except OSError as e:
                old = self.tv_conns.pop(tvid, None)
                if old:
                    old.close()
                if attempt or act == "pair":
                    raise ValueError(str(e))

    def cast_ctl(self, act):
        c = getattr(self, "casting", None)
        tv = c and next((t for t in self.settings.get("tvs") or [] if t["id"] == c["tv"]), None)
        if not tv:
            raise ValueError("nothing is playing on a TV")
        if act == "pause":
            self.av_call(tv["ctl"], "Pause", [("InstanceID", 0)])
        elif act == "play":
            self.av_call(tv["ctl"], "Play", [("InstanceID", 0), ("Speed", 1)])
        elif act == "stop":
            self.av_call(tv["ctl"], "Stop", [("InstanceID", 0)])
            self.casting = None
        else:
            raise KeyError(act)
        return {"ok": True}

    def apply_lan(self):
        """start/stop the local-network stream server and DLNA announcements"""
        s = self.settings
        if (s.get("lan_on") or s.get("dlna_on") or s.get("files_on")) and not self.lan:
            self.lan = LanServer(self)
            log.info("LAN sharing on port %s", s["lan_port"])
        elif not (s.get("lan_on") or s.get("dlna_on") or s.get("files_on")) and self.lan:
            self.lan.stop()
            self.lan = None
        if s.get("dlna_on") and not self.ssdp:
            self.ssdp = SSDP(self)
        elif not s.get("dlna_on") and self.ssdp:
            self.ssdp.stop()
            self.ssdp = None

    def share_dirs(self):
        out = []
        for i, pth in enumerate(self.settings.get("share_dirs") or []):
            pth = os.path.realpath(os.path.expanduser(pth))
            if os.path.isdir(pth) and share_ok(pth):  # an older unsafe choice is ignored, not shared
                out.append((i, pth))
        return out

    def shared_path(self, i, rel):
        """a file or folder inside shared folder i, or None (never outside it)"""
        dirs = dict(self.share_dirs())
        if i not in dirs:
            return None
        full = safe_join(dirs[i], rel or ".")
        if not full or hidden_inside(dirs[i], full):
            return None
        return full

    def dlna_folder(self, oid):
        """children of a DLNA container: 0 = root, dl = dlus downloads, f<i>[:<relpath>] = shared folder"""
        base = "http://%s:%d/%s" % (self.lan_ip(), int(self.settings["lan_port"]), self.settings["lan_token"])
        if oid == "0":
            items = [{"container": True, "id": "dl", "title": "dlus downloads", "count": len(self.dlna_items())}]
            for i, pth in self.share_dirs():
                items.append({"container": True, "id": "f%d" % i, "title": os.path.basename(pth) or pth, "count": 0})
            return items
        if oid == "dl":
            return self.dlna_items()
        mm = re.match(r"^f(\d+)(?::(.*))?$", oid)
        if not mm:
            return []
        i, rel = int(mm.group(1)), urllib.parse.unquote(mm.group(2) or "")
        full = self.shared_path(i, rel)
        if not full or not os.path.isdir(full):
            return []
        out = []
        try:
            names = list_dir(full)
        except OSError:
            return []
        for n in names:
            if n.startswith(".") or n.endswith((".parts", ".part", ".assembling")):
                continue
            fp = os.path.join(full, n)
            r2 = os.path.relpath(fp, dict(self.share_dirs())[i])
            if os.path.isdir(fp):
                out.append({"container": True, "id": "f%d:%s" % (i, urllib.parse.quote(r2)), "title": n, "count": 0})
                continue
            cat = category(n)
            if cat not in ("Video", "Audio", "Images"):
                continue
            mime = MIME.get(n.rsplit(".", 1)[-1].lower(), {"Images": "image/jpeg"}.get(cat, "video/x-matroska"))
            out.append({"id": "f%d:%s" % (i, urllib.parse.quote(r2)), "title": n, "url": "%s/f/%d/%s" % (base, i, urllib.parse.quote(r2)),
                        "cls": {"Video": "object.item.videoItem", "Audio": "object.item.audioItem.musicTrack", "Images": "object.item.imageItem.photo"}[cat],
                        "mime": mime, "size": fsize(fp), "flags": DLNA_FLAGS})
        return out

    def dlna_items(self):
        base = "http://%s:%d/%s" % (self.lan_ip(), int(self.settings["lan_port"]), self.settings["lan_token"])
        ff = bool(shutil.which("ffmpeg"))
        out = []
        for d in list(self.dls.values()):
            r = d.r
            cat = category(r["name"])
            if cat not in ("Video", "Audio") or not r.get("size") or not (r["status"] == "done" or d.snap.get("prefix")):
                continue
            mime = MIME.get(r["name"].rsplit(".", 1)[-1].lower(), "video/x-matroska" if cat == "Video" else "audio/mpeg")
            out.append({"id": r["id"], "title": r["name"], "url": "%s/%s" % (base, self.short_path(d)),
                        "cls": "object.item.videoItem" if cat == "Video" else "object.item.audioItem.musicTrack",
                        "mime": mime, "size": r["size"], "flags": DLNA_FLAGS})
            if ff and cat == "Video":
                stem = r["name"].rsplit(".", 1)[0]
                has_sub = bool(r.get("sub_default"))
                out.append({"id": r["id"] + "-c", "title": stem + (" [TV sound + subtitle]" if has_sub else " [TV sound]"),
                            "cls": "object.item.videoItem", "url": "%s/%s" % (base, self.short_path(d, "s")),
                            "mime": "video/x-matroska", "size": 0, "flags": DLNA_LIVE})
                if has_sub:
                    out.append({"id": r["id"] + "-t", "title": stem + " [subtitle]", "cls": "object.item.videoItem",
                                "url": "%s/%s" % (base, self.short_path(d, "t")), "mime": "video/x-matroska",
                                "size": 0, "flags": DLNA_LIVE})
        key = tuple(x["id"] + x["title"] for x in out)
        if key != self.dlna_key:
            self.dlna_key = key
            self.dlna_update += 1
        return out

    def set_settings(self, s):
        old = self.settings.get("limit_mb")
        for k, what in (("lan_on", "Sharing on the home network"), ("dlna_on", "DLNA library"), ("files_on", "Shared folders"),
                        ("proxy_on", "Web proxy for other devices"), ("proxy_auth", "Proxy password")):
            if k in s and bool(s[k]) != bool(self.settings.get(k)):
                self.audit("%s turned %s" % (what, "on" if s[k] else "off"), level="warn" if bool(s[k]) != (k == "proxy_auth") else "info")
        if isinstance(s.get("share_dirs"), list) and s["share_dirs"] != self.settings.get("share_dirs"):
            self.audit("shared folders changed: %s" % ", ".join(os.path.basename(str(x).rstrip("/")) for x in s["share_dirs"][:20]) or "none")
        if isinstance(s.get("share_dirs"), list):
            dirs = []
            for d_ in s["share_dirs"][:20]:
                full = os.path.realpath(os.path.expanduser(str(d_).strip()))
                if not os.path.isdir(full):
                    raise ValueError("not a folder: %s" % d_)
                if full in ("/", os.path.expanduser("~")):
                    raise ValueError("share a specific folder, not the whole disk or home folder")
                if not share_ok(full):
                    raise ValueError("%s holds private or system files (keys, settings) - share a folder with your media instead" % d_)
                dirs.append(full)
            self.settings["share_dirs"] = dirs
        if s.get("dlna_quality") in QUALITY_LABELS:
            self.settings["dlna_quality"] = s["dlna_quality"]
        if s.get("ui_open") in ("app", "browser"):
            self.settings["ui_open"] = s["ui_open"]
        if s.get("tv_quality") in list(QUALITY_LABELS) + ["auto"]:
            self.settings["tv_quality"] = s["tv_quality"]
        if s.get("tv_audio") in AUDIO_MODES:
            self.settings["tv_audio"] = s["tv_audio"]
        if str(s.get("watch_limit_mb", "")).strip():
            new = max(0.0, float(s["watch_limit_mb"]))
            if new != self.settings.get("watch_limit_mb") and getattr(self, "throttled", None):
                for d in self.dls.values():
                    if d.alive:
                        d.kill()  # reconnect with the new limit now (data is kept)
            self.settings["watch_limit_mb"] = new
        for k, cast in (("base_dir", str), ("max_active", int), ("conns", int), ("limit_mb", float),
                        ("sched_from", str), ("sched_to", str), ("on_done", str)):
            if k in s and str(s[k]).strip() != "":
                self.settings[k] = cast(s[k])
        if isinstance(s.get("sub_style"), dict):
            st = dict(self.settings.get("sub_style") or {})
            for k, cast in (("size", float), ("pos", float), ("color", str), ("bg", str), ("bold", bool), ("font", str)):
                if k in s["sub_style"]:
                    st[k] = cast(s["sub_style"][k])
            self.settings["sub_style"] = st
        for k in ("files_on", "proxy_on", "proxy_auth"):
            if k in s:
                self.settings[k] = bool(s[k])
        if "proxy_upstream" in s:
            up = str(s["proxy_upstream"]).strip()
            if up not in ("auto", "direct") and not re.match(r"^(socks5|http)://[\w.\-]+:\d+$", up):
                raise ValueError("upstream must be auto, direct, socks5://host:port or http://host:port")
            self.settings["proxy_upstream"] = up
        if str(s.get("proxy_port", "")).strip():
            pp = int(s["proxy_port"])
            if pp != int(self.settings["proxy_port"]) and self.proxy:
                self.proxy.stop()
                self.proxy = None
            self.settings["proxy_port"] = pp
        if s.get("proxy_newpass"):
            self.settings["proxy_pass"] = short_code(8)
            self.audit("new proxy password made")
        for k in ("sched_on", "notify", "lan_on", "dlna_on"):
            if k in s:
                self.settings[k] = bool(s[k])
        if s.get("rotate_token"):
            self.settings["lan_token"] = short_code()
            al = self.settings.get("lan_allowed") or {}
            self.settings["lan_allowed"] = {k: v for k, v in al.items() if v.get("why", "").startswith("paired")}
            self.audit("new code made - %d device(s) must be allowed again (paired TVs stay)" % (len(al) - len(self.settings["lan_allowed"])), level="warn")
        if str(s.get("lan_port", "")).strip():
            port = int(s["lan_port"])
            if port != int(self.settings["lan_port"]) and self.lan:
                self.lan.stop()
                self.lan = None
            self.settings["lan_port"] = port
        for k in ("sched_from", "sched_to"):
            if not re.match(r"^\d\d:\d\d$", self.settings[k]):
                raise ValueError("time must look like 02:00")
        self.settings["max_active"] = max(1, min(20, self.settings["max_active"]))
        self.settings["conns"] = max(1, min(32, self.settings["conns"]))
        if self.settings.get("limit_mb") != old:
            for d in self.dls.values():
                if d.alive:
                    d.kill()  # connections restart with the new limit, data kept
        try:
            self.apply_proxy()
        except OSError as e:
            self.settings["proxy_on"] = False
            self.save()
            raise ValueError("cannot start the proxy on port %s: %s" % (self.settings["proxy_port"], e))
        if self.settings.get("files_on") and not self.lan:
            self.settings["lan_on"] = True
        try:
            self.apply_lan()
        except OSError as e:
            self.settings["lan_on"] = self.settings["dlna_on"] = False
            self.save()
            raise ValueError("cannot share on the local network: %s" % e)
        self.save()

    def apply_proxy(self):
        if self.settings.get("proxy_on") and not self.proxy:
            self.proxy = ProxyServer(self)
            log.info("proxy sharing on port %s", self.settings["proxy_port"])
        elif not self.settings.get("proxy_on") and self.proxy:
            self.proxy.stop()
            self.proxy = None

    def local_proxies(self):
        """proxy ports of VPN apps running in proxy mode on this Mac (V2Box, xray, sing-box, clash, ...)"""
        if time.time() - getattr(self, "_lp_t", 0) < 20:
            return self._lp
        found = []
        for l in sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], 5).splitlines()[1:]:
            c = l.split()
            mm = re.search(r"(127\.0\.0\.1|\*|localhost):(\d+)$", c[8] if len(c) > 8 else "")
            if not mm or not re.search(r"v2box|xray|v2ray|sing-?box|clash|mihomo|hiddify|shadow|trojan|privoxy|tor|nekoray|foxray", c[0], re.I):
                continue
            port = int(mm.group(2))
            if any(x["port"] == port for x in found):
                continue
            kind = proxy_kind("127.0.0.1", port)
            if kind:
                found.append({"app": c[0].replace("\\x20", " "), "port": port, "kind": kind, "url": "%s://127.0.0.1:%d" % (kind, port)})
        self._lp, self._lp_t = found, time.time()
        return found

    def upstream(self):
        up = self.settings.get("proxy_upstream") or "auto"
        if up == "auto":
            lp = self.local_proxies()
            return lp[0]["url"] if lp else "direct"
        return up

    def share_state(self):
        ip, port = self.lan_ip(), int(self.settings["proxy_port"])
        st = {"dirs": [{"i": i, "path": pth.replace(os.path.expanduser("~"), "~"), "name": os.path.basename(pth)} for i, pth in self.share_dirs()],
              "files": {"on": bool(self.settings.get("files_on")), "url": "http://%s:%d/%s/files/" % (ip, int(self.settings["lan_port"]), self.settings["lan_token"])},
              "proxy": {"on": bool(self.settings.get("proxy_on")), "running": bool(self.proxy), "ip": ip, "port": port,
                        "auth": bool(self.settings.get("proxy_auth")), "user": self.settings["proxy_user"], "pass": self.settings["proxy_pass"],
                        "upstream": self.settings.get("proxy_upstream") or "auto", "using": self.upstream() if self.proxy else "",
                        "local": self.local_proxies(), "pac": "http://%s:%d/%s/proxy.pac" % (ip, int(self.settings["lan_port"]), self.settings["lan_token"])}}
        if self.proxy:
            st["proxy"].update(self.proxy.stats())
        return st

    def fonts_set(self, b):
        if b.get("install"):
            install_font(b["install"])
            return {"ok": True}
        if b.get("remove"):
            name = b["remove"]
            if name in FONT_CATALOG:
                if FONT_CATALOG[name].get("default"):
                    raise ValueError("this font comes with dlus")
                for url, _ in FONT_CATALOG[name]["files"]:
                    rm(font_file(url))
            else:
                keep = []
                for x in self.settings.get("fonts_user") or []:
                    if x["name"] == name:
                        rm(os.path.join(fonts_dir(), x["file"]))
                    else:
                        keep.append(x)
                self.settings["fonts_user"] = keep
            if (self.settings.get("sub_style") or {}).get("font") == name:
                self.settings["sub_style"]["font"] = "Vazirmatn"
            self.save()
            return {"ok": True}
        if b.get("upload"):
            u = b["upload"]
            raw = base64.b64decode(u.get("b64") or "")
            ext = (u.get("filename") or "").rsplit(".", 1)[-1].lower()
            if ext not in FONT_MIME or not raw.startswith(FONT_MAGIC):
                raise ValueError("not a font file (.ttf .otf .woff .woff2)")
            name = re.sub(r"[^\w \-]", "", u.get("name") or u.get("filename", "").rsplit(".", 1)[0]).strip()[:40] or "My font"
            if name in FONT_CATALOG or any(x["name"] == name for x in self.settings.get("fonts_user") or []):
                raise ValueError("a font with this name already exists")
            fn = "user-%s.%s" % (re.sub(r"\W", "", name.lower())[:30] or uuid.uuid4().hex[:6], ext)
            os.makedirs(fonts_dir(), exist_ok=True)
            with open(os.path.join(fonts_dir(), fn), "wb") as o:
                o.write(raw)
            self.settings.setdefault("fonts_user", []).append({"name": name, "file": fn})
            self.save()
            return {"ok": True, "name": name}
        raise KeyError("fonts")

    def gw_values(self, ip):
        lan = self.gw.st.get("lan") or self.gw.lan() or {}
        return {"ip": ip, "mask": lan.get("mask", ""), "gw": lan.get("ip", ""), "dns": "1.1.1.1", "dns2": "8.8.8.8"}

    def gw_vpn_ok(self, tun):
        """can the tunnel itself reach YouTube? (curl bound to the tunnel interface, cached 30 s)"""
        c = self.gw_vpn_probe
        if c.get("if") == tun and time.time() - c.get("at", 0) < 30:
            return c
        r = sh(["curl", "-s", "-o", os.devnull, "--interface", tun, "-m", "8", "-w", "%{http_code} %{time_total}", "https://www.youtube.com/generate_204"], 12)
        code, _, tt = (r.strip() + "  ").partition(" ")
        self.gw_vpn_probe = c = {"if": tun, "at": time.time(), "ok": code in ("204", "200"), "ms": int(float(tt or 0) * 1000) if tt.strip() else 0}
        return c

    def gw_check_ip(self, ip):
        G, S = self.gw, self.settings
        st = G.st
        dv = next((x for x in G.devices.values() if x["ip"] == ip), None)
        tun = G.pick_tunnel()
        t = G.traffic.get(ip, {})
        routed = time.time() - t.get("last", 0) < 30 and t.get("conns", 0) > 0
        mac = dv["mac"] if dv else next((x.get("mac") for x in S.get("tvs") or [] if x["ip"] == ip), "")
        eff = (S.get("gw_policy") or {}).get(mac) or S.get("gw_default") or "direct"
        vp = self.gw_vpn_ok(tun["if"]) if tun else {"ok": False}
        tn = self.gw_tvnet.get(ip) or {}
        tn_fresh = time.time() - tn.get("at", 0) < 120
        checks = [
            {"k": "helper", "ok": bool(st.get("installed")), "t": "Router helper installed", "bad": "Install the router helper (one Terminal command, below)"},
            {"k": "router", "ok": bool(st.get("active")), "t": "VPN router is on", "bad": st.get("error") or "Turning the router on..."},
            {"k": "vpn", "ok": bool(tun), "t": "VPN tunnel on this Mac: %s (%s)" % (tun["name"], tun["if"]) if tun else "",
             "bad": "No system-wide VPN tunnel on this Mac - turn on TUN / system mode in V2Box (or connect WireGuard)"},
            {"k": "vpnnet", "ok": bool(vp.get("ok")), "t": "The VPN reaches YouTube (%d ms)" % vp.get("ms", 0), "bad": "The tunnel is up but YouTube does not answer through it" if tun else "Waiting for the VPN tunnel"},
            {"k": "seen", "ok": bool(dv), "t": "Device is on the network (%s)" % ip, "bad": "Device %s is not on the network - is it on?" % ip},
            {"k": "uses", "ok": routed, "t": "Device sends its traffic through this Mac (%d connections)" % t.get("conns", 0),
             "bad": "Waiting for the device to use this Mac - set Gateway = %s on it, then open any app" % self.gw_values(ip)["gw"]},
            {"k": "tunnel", "ok": routed and eff == "tunnel", "t": "Its traffic goes through the VPN", "bad": "Set this device to Tunnel" if eff != "tunnel" else "Waiting for traffic..."},
        ]
        if tn_fresh:
            checks.append({"k": "tv", "ok": tn.get("ok"), "t": "The device itself opened YouTube (%d ms)" % tn.get("ms", 0), "bad": "The device could not open YouTube"})
        done = all(c["ok"] for c in checks)
        return {"ok": True, "done": done, "checks": checks, "values": self.gw_values(ip), "rate": t.get("rate", 0), "total": t.get("total", 0)}

    def gw_toast_loop(self, tvid):
        """while the TV's network settings are open, keep the values on its screen (until it works or 3 minutes pass)"""
        tv = next((x for x in self.settings.get("tvs") or [] if x["id"] == tvid), None)
        if not tv or self.gw_wiz.get("toasting"):
            return
        self.gw_wiz["toasting"] = True

        def run():
            try:
                end = time.time() + 180
                while time.time() < end:
                    v = self.gw_values(tv["ip"])
                    try:
                        _, c = self.tv_remote(tvid)
                        c.request("ssap://system.notifications/createToast", {"message": "dlus VPN · IP %s · Mask %s · Gateway %s · DNS %s" % (v["ip"], v["mask"], v["gw"], v["dns"])})
                    except (OSError, ValueError):
                        pass
                    for _ in range(15):
                        time.sleep(1)
                        if self.gw_check_ip(tv["ip"]).get("done"):
                            try:
                                _, c = self.tv_remote(tvid)
                                c.request("ssap://system.notifications/createToast", {"message": "dlus: this TV now goes through the VPN ✓"})
                            except (OSError, ValueError):
                                pass
                            return
            finally:
                self.gw_wiz["toasting"] = False
        threading.Thread(target=run, daemon=True).start()

    def gw_wizard(self, b):
        mac = str(b.get("mac", ""))
        dv = self.gw.devices.get(mac)
        ip = dv["ip"] if dv else str(b.get("ip", ""))
        if not ip:
            raise ValueError("pick a device")
        if b.get("act") == "start":
            if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac):
                raise ValueError("that device is not on the network right now")
            self.gw_set({"on": True, "policy": {mac: "tunnel"}})
            self.gw_wiz.update(mac=mac, ip=ip, at=time.time())
        tv = next((x for x in self.settings.get("tvs") or [] if x["ip"] == ip), None)
        return dict(self.gw_check_ip(ip), tv=tv["id"] if tv else "", paired=bool(tv and tv.get("paired")), name=(dv or {}).get("name", ip))

    @staticmethod
    def route_table():
        """the Mac's IPv4 routes that matter (default, networks, VPN) - not the cached per-host entries"""
        out = []
        for l in sh(["netstat", "-rn", "-f", "inet"]).splitlines():
            f = l.split()
            if len(f) < 4 or f[0] in ("Routing", "Internet:", "Destination") or "W" in f[2] or "L" in f[2]:
                continue
            if f[0].startswith(("224", "255", "169.254", "127")):
                continue
            out.append({"dest": f[0], "via": f[1], "if": f[3]})
        return out

    def gw_result(self, title, ok, output, before, extra=None):
        after = self.route_table()
        key = lambda r: (r["dest"], r["via"], r["if"])
        b, a = {key(r) for r in before}, {key(r) for r in after}
        rows = [dict(r, st="added" if key(r) not in b else "") for r in after] + [dict(r, st="removed") for r in before if key(r) not in a]
        lines = [l for l in (output or "").splitlines() if l.strip() and "pfctl: Use of -f" not in l and "present in the main" not in l and "See /etc/pf.conf" not in l]
        res = {"title": title, "ok": ok, "at": time.time(), "lines": lines[-40:], "routes": rows,
               "added": sum(1 for r in rows if r["st"] == "added"), "removed": sum(1 for r in rows if r["st"] == "removed")}
        res.update(extra or {})
        self.gw.last_result = res
        return res

    def gw_admin(self, b):
        """install / update / remove the root helper with the standard macOS password prompt; returns the output"""
        act = b.get("act")
        if act not in ("install", "uninstall"):
            raise ValueError("unknown action")
        user = getpass.getuser()
        if not re.match(r"^[a-z_][a-z0-9_.-]{0,31}$", user):
            raise ValueError("unsupported user name")
        script = os.path.join(APP_DIR, "gateway", act + ".sh")
        if not os.path.isfile(script):
            raise ValueError("%s is missing" % script)
        cmd = "/bin/bash %s %s 2>&1" % (shlex.quote(script), shlex.quote(user))
        before, v0 = self.route_table(), self.gw.helper_versions()[0]
        why = ("dlus wants to install its VPN-router helper. It can only change dlus's own firewall rules and IP forwarding."
               if act == "install" else "dlus wants to remove its VPN-router helper and firewall rules.")
        osa = 'do shell script "%s" with prompt "%s" with administrator privileges' % (cmd.replace("\\", "\\\\").replace('"', '\\"'), why)
        try:
            r = subprocess.run(["/usr/bin/osascript", "-e", osa], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise ValueError("no answer from the password prompt")
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 and "-128" in out:
            return {"ok": False, "cancelled": True, "output": "Cancelled - nothing was changed."}
        self.gw.t_check = self.gw.t_ver = 0
        self.gw.vers = self.gw.helper_versions()
        self.gw.wake.set()
        ok = r.returncode == 0
        title = ("Helper updated" if v0 else "Helper installed") if act == "install" else "Helper removed"
        self.audit("VPN-router root helper: %s%s" % (title.lower(), "" if ok else " - failed"), level="warn")
        res = self.gw_result(title if ok else title.replace("updated", "update").replace("installed", "install").replace("removed", "removal") + " failed", ok, out, before,
                             {"kind": "admin", "act": act, "v_from": v0, "v_to": self.gw.vers[0], "command": "sudo " + cmd.replace(" 2>&1", ""),
                              "checks": sum(1 for l in out.splitlines() if l.startswith("ok rules"))})
        return dict(res, output=out)

    def gw_full(self, b):
        """split-tunnel VPN: send ALL of this Mac's traffic into the tunnel (two /1 routes). Undone by itself
        if the internet stops working within 12 s, and always when the router is turned off."""
        G = self.gw
        if not b.get("on"):
            before = self.route_table()
            ok, out = G.call("full", "off")
            G.full = ""
            G.wake.set()
            return dict(self.gw_result("Routes put back", ok, out, before, {"kind": "full"}), output=out)
        tun = str(b.get("tun", ""))
        if not any(t["if"] == tun for t in G.tunnels()):
            raise ValueError("that VPN tunnel is not up")
        before = self.route_table()
        ok, out = G.call("full", "on", tun)
        if not ok:
            return dict(self.gw_result("Could not route everything through " + tun, False, out, before, {"kind": "full"}), output=out)
        test = sh(["curl", "-s", "-o", os.devnull, "-m", "8", "-w", "%{http_code}", "https://www.gstatic.com/generate_204"], 12).strip()
        if test not in ("204", "200"):
            _, out2 = G.call("full", "off")
            out = out + "\nNo internet through %s - undone automatically.\n%s" % (tun, out2)
            return dict(self.gw_result("Undone: no internet through " + tun, False, out, before, {"kind": "full"}), output=out)
        G.full = tun
        G.wake.set()
        out += "\ninternet check through %s: OK" % tun
        return dict(self.gw_result("All traffic now goes through " + tun, True, out, before, {"kind": "full"}), output=out)

    def gw_diag(self):
        """'Check routes': what the router sees, as text"""
        G = self.gw
        parts = [("Internet route of this Mac (route get 1.1.1.1)", sh(["route", "-n", "get", "1.1.1.1"])),
                 ("Default and VPN routes (netstat -rn)", "\n".join(l for l in sh(["netstat", "-rn", "-f", "inet"]).splitlines()
                                                                    if re.match(r"^(Destination|default|0/1|128\.0/1|0\.0\.0\.0)", l) or "utun" in l or "ipsec" in l)[:4000]),
                 ("VPN tunnels (up, with IPv4)", "\n".join("%s  %s  %s%s" % (t["if"], t["ip"], t["name"], "  <- carries the internet" if t["if"] == (self.net.info.get("route_iface") or "") else "")
                                                       for t in G.tunnels()) or "none"),
                 ("VPN apps running", ", ".join(self.net.info.get("apps") or []) or "none"),
                 ("macOS VPN profiles", "\n".join("%s  %s  %s" % (v["status"], v["app"], v["name"]) for v in self.net.info.get("vpns") or []) or "none")]
        if os.path.exists(gw_helper()):
            ok, out = G.call("status", timeout=15)
            parts.append(("Router helper (dlus-gw status)", out if ok else "not usable: " + out))
        else:
            parts.append(("Router helper", "not installed"))
        return {"ok": True, "text": "\n\n".join("== %s\n%s" % (k, (v or "").strip()) for k, v in parts)}

    def gw_set(self, b):
        S = self.settings
        if "on" in b and bool(b["on"]) != bool(S.get("gw_on")):
            self.audit("VPN router turned " + ("on" if b["on"] else "off"), level="warn" if b["on"] else "info")
        if "on" in b:
            S["gw_on"] = bool(b["on"])
        if b.get("default") in ("tunnel", "direct"):
            S["gw_default"] = b["default"]
        if "killswitch" in b:
            S["gw_killswitch"] = bool(b["killswitch"])
        S.pop("gw_tunnel", None)
        for mac, v in (b.get("policy") or {}).items():
            if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac):
                raise ValueError("bad device")
            pol = S.setdefault("gw_policy", {})
            if v in ("tunnel", "direct"):
                pol[mac] = v
            elif v == "default":
                pol.pop(mac, None)
            self.gw_new.pop(mac, None)
        for mac, name in (b.get("names") or {}).items():
            if re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac):
                S.setdefault("gw_names", {})[mac] = str(name)[:40]
        if b.get("dismiss"):
            self.gw_new.pop(b["dismiss"], None)
        if b.get("reset_usage") and hasattr(self.gw, "usage"):
            self.gw.usage.pop(str(b["reset_usage"]), None)
            self.gw.usage_dirty = True
            self.gw.save_usage(force=True)
        self.save()
        self.gw.wake.set()
        return {"ok": True}

    def shutdown(self):
        if getattr(self, "hls", None):
            self.hls.shutdown()
        if self.proxy:
            self.proxy.stop()
        if self.ssdp:
            self.ssdp.stop()
        for d in list(self.dls.values()):
            if d.alive:
                d.stop()
        for d in list(self.dls.values()):
            if d.thread:
                d.thread.join(5)
        for d in self.dls.values():
            if d.r["status"] in RUNNING:
                d.r["status"] = "queued"
        self.save()
        if self.caff:
            self.caff.terminate()

    def loop(self):
        last = 0
        while True:
            time.sleep(0.5)
            try:
                self.tick()
                if time.time() - last > 3:
                    self.save()
                    last = time.time()
            except Exception:
                log.exception("loop")

    def tick(self):
        s = self.settings
        with self.lock:
            dls = list(self.dls.values())
        allowed = not s.get("sched_on") or in_window(s["sched_from"], s["sched_to"])
        self.sched_wait = not allowed and any(d.r["status"] == "queued" for d in dls)
        if not allowed:
            for d in dls:
                if d.alive and not d.r.get("force") and d.r["status"] in ("downloading", "waiting"):
                    d.stop()
                    d.r["status"] = "queued"
                    d.r["msg"] = "waiting for the schedule (%s-%s)" % (s["sched_from"], s["sched_to"])
                    d.log("stopped by the schedule - continues at " + s["sched_from"])
        n = sum(d.alive for d in dls)
        for d in dls:
            if n >= s["max_active"]:
                break
            if d.r["status"] == "queued" and not d.alive and (allowed or d.r.get("force")):
                d.start()
                n += 1
        total = 0
        for d in dls:
            d.sample()
            total += d.snap["now"]
        self.sessions.tick()
        try:
            self.save_audit()
        except OSError:
            pass
        if self.proxy:
            self.proxy.tick()
        # another device is watching over the local network: give the video the Wi-Fi airtime
        now = time.time()
        watchers = [x for x in self.sessions.live.values() if x["ip"] not in ("127.0.0.1", "::1") and (x["active"] > 0 or now - x["last"] < 20)]
        w = watchers[0]["device"] if watchers and float(self.settings.get("watch_limit_mb") or 0) else None
        if w != getattr(self, "throttled", None):
            self.throttled = w
            for d in dls:
                if d.alive:
                    d.kill()  # reconnect with the new speed limit (data is kept)
                    d.log(("%s is watching - downloads limited to %s MB/s so the video does not stutter" % (w, self.settings["watch_limit_mb"]))
                          if w else "nobody is watching any more - full download speed again")
        self.gnow = total
        self.ghist.append(int(total))
        busy = any(d.alive for d in dls)
        if busy and not self.caff and shutil.which("caffeinate"):
            self.caff = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not busy and self.caff:
            self.caff.terminate()
            self.caff = None
        if self.was_busy and not busy and not any(d.r["status"] == "queued" for d in dls):
            self.notify("All downloads finished")
            if s.get("on_done") == "sleep":
                log.info("queue finished - putting the Mac to sleep")
                subprocess.Popen(["pmset", "sleepnow"])
        self.was_busy = busy

    def state(self):
        with self.lock:
            dls = list(self.dls.values())
        base = os.path.expanduser(self.settings["base_dir"])
        folders = set(os.path.join(base, c) for c in list(CATS) + ["Other"])
        cats = list(CATS) + ["Other"]
        items, qpos = [], 0
        for d in dls:
            r = d.r
            folders.add(r["dir"])
            if r["status"] == "queued" and not d.alive:
                qpos += 1
            items.append({k: r.get(k) for k in ("id", "url", "name", "dir", "size", "status", "msg", "conns", "ranges",
                                                 "added", "finished", "checksum", "checksum_ok", "checksum_src", "sha256", "verify", "fails", "bound_ip",
                                                 "net", "force", "num")}
                         | {"done": d.snap["done"] if r["status"] != "done" else r.get("size", 0), "now": d.snap["now"],
                            "avg": d.snap["avg"], "cat": category(r["name"]), "nconn": len(d.snap["conn"]),
                            "prefix": d.snap.get("prefix", 0), "qpos": qpos if r["status"] == "queued" and not d.alive else 0,
                            "missing": r["status"] == "done" and r.get("named") and not os.path.exists(d.out)})
        disk = {}
        try:
            root = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
            du = shutil.disk_usage(root)
            mine = sum(d.snap["done"] if d.r["status"] != "done" else (d.r.get("size") or 0) for d in dls)
            disk = {"free": du.free, "total": du.total, "used": du.used, "mine": mine}
            bd = base if os.path.isdir(base) else os.path.expanduser("~")
            if os.stat(bd).st_dev != os.stat(root).st_dev:
                b2 = shutil.disk_usage(bd)
                disk["dl_vol"] = {"path": bd, "free": b2.free, "total": b2.total}
        except OSError:
            pass
        month = time.strftime("%Y-%m")
        st = {"today": self.stats["days"].get(time.strftime("%Y-%m-%d"), 0),
              "month": sum(v for k, v in self.stats["days"].items() if k.startswith(month)), "total": self.stats.get("total", 0),
              "days": [[time.strftime("%m-%d", time.localtime(time.time() - 86400 * k)),
                        self.stats["days"].get(time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * k)), 0)]
                       for k in range(13, -1, -1)]}
        return {"cats": cats, "version": VERSION, "state_dir": STATE_DIR, "home": os.path.expanduser("~"), "settings": self.settings, "now": self.gnow, "hist": list(self.ghist), "items": items,
                "folders": sorted(folders), "t": time.time(), "net": self.net.info, "stats": st, "disk": disk,
                "players": self.players(), "sched_wait": self.sched_wait, "casting": self.casting, "devices": self.sessions.snapshot(), "access": self.access_state(),
                "share": self.share_state(), "throttled": getattr(self, "throttled", None), "audio_modes": AUDIO_MODES, "mk": self.media_key,
                "tv_now": self.tv_now if self.tv_now and time.time() - self.tv_now["at"] < 5 else None, "qualities": QUALITY_LABELS,
                "gw": self.gw.state(),
                "fonts": [{"name": n, "kind": f["kind"], "installed": font_installed(n), "builtin": bool(f.get("default"))} for n, f in FONT_CATALOG.items()]
                         + [{"name": x["name"], "kind": "Yours", "installed": True, "builtin": False, "user": True} for x in user_fonts(self)],
                "tvs": [dict(t, paired=t["id"] in (self.settings.get("tv_keys") or {})) for t in self.settings.get("tvs") or []],
                "local_player": "http://127.0.0.1:%d/%s/" % (LOCAL_PLAYER_PORT, self.settings["lan_token"]),
                "lan": {"on_lan": bool(self.settings.get("lan_on")), "dlna": bool(self.settings.get("dlna_on")),
                        "running": bool(self.lan), "name": self.friendly(), "ffmpeg": bool(shutil.which("ffmpeg")),
                        "base": "http://%s:%d/%s" % (self.lan_ip(), int(self.settings["lan_port"]), self.settings["lan_token"])}}

    def detail(self, did):
        d = self.dls[did]
        s = d.snap
        return {"chunks": s["chunks"], "pc": s["pc"], "conn": s["conn"], "ev": list(d.events)[-80:][::-1],
                "hist": list(d.hist), "ok": s["chunks"].count("o"), "fails": d.r.get("fails", 0)}


# ---------------------------------------------------------------- streaming, LAN, DLNA
DLNA_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
DLNA_LIVE = "DLNA.ORG_OP=10;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=01700000000000000000000000000000"


class Base(http.server.BaseHTTPRequestHandler):
    server_version = "dlus/" + VERSION

    def out(self, code, body, ct="application/json", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        if ct.startswith("text/html") and b"rel=manifest" not in body[:6000]:
            body = body.replace(b"</title>", b"</title>" + APP_HEAD.encode(), 1)
        extra = dict(SEC_HEADERS, **(extra or {}))
        if ct.startswith("text/html"):  # no other site may show dlus pages in a frame (clickjacking)
            extra.setdefault("X-Frame-Options", "SAMEORIGIN")
            extra.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, *a):
        pass


def send_stream(h, d, lan=False):
    """serve the file (also while downloading) with range support"""
    L = d.r.get("size") or 0
    if not L:
        return h.out(404, {"error": "not started yet"})
    if not d.media_ok():
        return h.out(415, {"error": "this file is a playlist disguised as a video - refused for safety"})
    a, b, partial = 0, L - 1, False
    mm = re.match(r"bytes=(\d*)-(\d*)", h.headers.get("Range", ""))
    if mm and (mm.group(1) or mm.group(2)):
        partial = True
        if mm.group(1):
            a = int(mm.group(1))
            b = min(int(mm.group(2)), L - 1) if mm.group(2) else L - 1
        else:
            a = max(0, L - int(mm.group(2)))
    if a >= L:
        return h.out(416, b"", "text/plain", {"Content-Range": "bytes */%d" % L})
    h.send_response(206 if partial else 200)
    h.send_header("Content-Type", MIME.get(d.r["name"].rsplit(".", 1)[-1].lower(), "application/octet-stream"))
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(b - a + 1))
    if partial:
        h.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, L))
    if lan:
        h.send_header("transferMode.dlna.org", "Streaming")
        h.send_header("contentFeatures.dlna.org", DLNA_FLAGS)
    h.end_headers()
    if h.command == "HEAD":
        return
    sx = d.m.sessions.begin(h, d, "original file" + (" (local network)" if lan else ""))
    pos, waited = a, 0.0
    try:
        while pos <= b:
            av = d.avail(pos)
            if not av:
                d.want(pos)
                if waited > 600 or (not d.alive and d.r["status"] != "queued"):
                    return
                time.sleep(0.5)
                waited += 0.5
                continue
            waited = 0.0
            path, off, n = av
            try:
                data = read_at(path, off, min(n, b - pos + 1, 1 << 20))
            except OSError:
                time.sleep(0.2)
                continue
            if not data:
                time.sleep(0.2)
                continue
            h.wfile.write(data)
            pos += len(data)
            d.m.sessions.add(sx, len(data), pos / L)
    except (BrokenPipeError, ConnectionResetError):
        return
    finally:
        d.m.sessions.end(sx)


def probe(m, d):
    """duration, codecs and audio tracks via ffprobe (cached per download)"""
    if getattr(d, "probe_info", None):
        return d.probe_info
    if not shutil.which("ffprobe") or not (d.snap.get("prefix") or d.r["status"] == "done") or not d.media_ok():
        return {}
    url = "http://127.0.0.1:%d/stream/%s/x?ik=%s" % (m.port, d.r["id"], m.media_key)
    raw = sh(["ffprobe", "-v", "error", "-show_entries",
              "stream=index,codec_type,codec_name,channels,pix_fmt,color_transfer,width,height:stream_tags=language,title:format=duration", "-of", "json", url], 60)
    try:
        j = json.loads(raw or "{}")
    except ValueError:
        return {}
    st = j.get("streams") or []
    vs = [x for x in st if x.get("codec_type") == "video"]
    au = [x for x in st if x.get("codec_type") == "audio"]
    info = {"duration": float((j.get("format") or {}).get("duration") or 0), "video": vs[0].get("codec_name", "") if vs else "",
            "pix": vs[0].get("pix_fmt", "") if vs else "", "transfer": vs[0].get("color_transfer", "") if vs else "",
            "width": int(vs[0].get("width") or 0) if vs else 0,
            "audio": [{"i": k, "codec": x.get("codec_name", ""), "ch": x.get("channels", 0),
                       "lang": (x.get("tags") or {}).get("language", ""), "title": (x.get("tags") or {}).get("title", "")}
                      for k, x in enumerate(au)],
            "subs": [{"k": k, "codec": x.get("codec_name", ""), "lang": (x.get("tags") or {}).get("language", ""),
                      "title": (x.get("tags") or {}).get("title", "")}
                     for k, x in enumerate(y for y in st if y.get("codec_type") == "subtitle")]}
    if info["video"] or info["audio"]:
        d.probe_info = info
    return info


# measured on Apple M5: every level encodes 4K HDR >= 2.7x faster than real time
QUALITY = {"high": ("4k", 80), "saver": ("4k", 76), "1080": ("1080", 80), "1080low": ("1080", 74), "enhance": ("enh", 82)}
QUALITY_LABELS = {"orig": "Original (best, needs ~60 Mbit/s)", "high": "4K High (~30 Mbit/s)", "saver": "4K Saver (~16 Mbit/s)",
                  "1080": "1080p (~8 Mbit/s)", "1080low": "1080p Low (~4 Mbit/s)",
                  "enhance": "Enhanced (cleaned + upscaled to 1080p)"}
AUDIO_MODES = {"original": "Original sound (no remix, best quality)", "dialog": "Clear dialogue (TV speakers)", "night": "Night mode (even volume)",
               "surround": "5.1 surround (soundbar / home theater)"}


def audio_filter(mode, chans):
    """TV speakers: 5.1 downmixed with the voice channel boosted; night: also compressed"""
    if mode in ("surround", "original"):
        return []
    if chans < 6:  # stereo: no voice channel to lift, but night mode still evens loud and quiet parts
        return ["-af", "acompressor=threshold=0.08:ratio=5:attack=8:release=250:makeup=2.5,alimiter=limit=0.95"] if mode == "night" else []
    mix = "pan=stereo|FL=1.15*FC+0.65*FL+0.45*SL+0.25*LFE|FR=1.15*FC+0.65*FR+0.45*SR+0.25*LFE"
    if mode == "night":
        mix += ",acompressor=threshold=0.08:ratio=5:attack=8:release=250:makeup=2.5,alimiter=limit=0.95"
    else:
        mix += ",dynaudnorm=f=300:g=11:m=6"
    return ["-af", mix]


def fmt_hms(x):
    x = int(x)
    return "%d:%02d:%02d" % (x // 3600, x % 3600 // 60, x % 60)


def cut_srt(vtt_path, start, delay, scale=1.0):
    """the subtitle as SRT whose times start at 'start' (where the video really starts), with the sync applied.
    ffmpeg's own jump inside a subtitle file lands on the previous line, seconds off - so dlus cuts it itself."""
    def sec(h, mi, se, ms):
        return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms) / 1000

    def ts(x):
        x = max(0, x)
        return "%02d:%02d:%02d,%03d" % (x // 3600, x % 3600 // 60, x % 60, round(x % 1 * 1000) % 1000)
    out, n = [], 0
    for blk in re.split(r"\n\s*\n", rd(vtt_path).replace("\r", "")):
        mm = re.search(r"(\d+):(\d\d):(\d\d)[.,](\d{3}) --> (\d+):(\d\d):(\d\d)[.,](\d{3})", blk)
        if not mm:
            continue
        a, b = sec(*mm.groups()[:4]) * scale + delay - start, sec(*mm.groups()[4:]) * scale + delay - start
        if b <= 0:
            continue
        text = blk[mm.end():].strip("\n")
        n += 1
        out.append("%d\n%s --> %s\n%s\n" % (n, ts(a), ts(b), text))
    p = os.path.join(STATE_DIR, "tmp", "cut-%s.srt" % secrets.token_hex(5))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    wr(p, "\n".join(out))
    return p


def speech_starts(path):
    """subtitle lines as (start, end) seconds"""
    out = []
    for mm in re.finditer(r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d{3}) --> (?:(\d+):)?(\d\d):(\d\d)[.,](\d{3})", rd(path)):
        g = [int(x or 0) for x in mm.groups()]
        out.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000, g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000))
    return out


SYNC_SCALES = (1.0, 25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24, 25 / 24, 24 / 25)


def speech_sync(cues, speech):
    """fit a subtitle to the film's speech (speech = 0/1 per 0.1 s).
    1) one shift (-20..+20 s) and speed (25 / 24 / 23.976 fps mixups) for the whole film;
    2) the same check in 12 sections (+-60 s each): do all parts agree?
    verdict 'fits' = one shift + speed; 'sections' = parts of the film need different shifts (another cut: other intro,
    extra / missing scene) -> per-line shifts; 'nomatch' = the subtitle belongs to another version, nothing is applied."""
    cum = [0]
    for v in speech:
        cum.append(cum[-1] + v)
    n = len(speech)

    def cover(cs, off, sc):
        tot = ln = 0
        for a, b in cs:
            i, j = int((a * sc + off) * 10), int((b * sc + off) * 10)
            if i < 0 or j >= n or j <= i:
                continue
            tot += cum[j] - cum[i]
            ln += j - i
        return tot / ln if ln else 0.0

    def fit(cs, sc, lo, hi):
        vals = [(cover(cs, o / 10, sc), o / 10) for o in range(lo, hi + 1, 2)]
        top, o = max(vals)
        mu = sum(v for v, _ in vals) / len(vals)
        sd = (sum((v - mu) ** 2 for v, _ in vals) / len(vals)) ** 0.5 or 1
        fine = max((cover(cs, o + k / 100, sc), o + k / 100) for k in range(-25, 26))
        return round(fine[1], 2), round((top - mu) / sd, 1)
    best = max(((cover(cues, o / 10, sc), sc) for sc in SYNC_SCALES for o in range(-200, 201, 2)), key=lambda x: x[0])
    sc = best[1]
    off, conf = fit(cues, sc, -200, 200)
    k = 12
    secs = []
    for q in range(k):
        part = cues[q * len(cues) // k:(q + 1) * len(cues) // k]
        if len(part) >= 8:
            o, c = fit(part, sc, -600, 600)
            secs.append({"from": part[0][0], "i0": q * len(cues) // k, "off": o, "conf": c, "ok": c >= 3.0})
    good = [x for x in secs if x["ok"]]
    res = {"offset": off, "scale": sc, "confidence": conf, "cover": round(cover(cues, off, sc), 3), "sections": secs}
    if len(good) >= 0.75 * len(secs) and sum(1 for x in good if abs(x["off"] - off) <= 0.5) >= 0.75 * len(good):
        return dict(res, verdict="fits")
    # runs of neighbouring sections that agree with each other (another cut: a jump between parts)
    runs = []
    for x in secs:
        if not x["ok"]:
            continue
        if runs and abs(runs[-1]["off"] - x["off"]) <= 0.5:
            runs[-1]["secs"].append(x)
            runs[-1]["off"] = sorted(y["off"] for y in runs[-1]["secs"])[len(runs[-1]["secs"]) // 2]
        else:
            runs.append({"off": x["off"], "secs": [x]})
    if len(good) >= 0.6 * len(secs) and 2 <= len(runs) <= 4 and all(len(r["secs"]) >= 2 for r in runs):
        # exact line where each jump happens: best split between the last section of a run and the first of the next
        bounds = [0]
        for r1, r2 in zip(runs, runs[1:]):  # the exact line where each jump happens
            a, b = r1["secs"][-1]["i0"], min(len(cues), r2["secs"][0]["i0"] + len(cues) // k)
            bounds.append(max(range(a + 1, b), key=lambda m: cover(cues[a:m], r1["off"], sc) * (m - a) + cover(cues[m:b], r2["off"], sc) * (b - m)))
        bounds.append(len(cues))
        shifts = []
        for r, lo, hi in zip(runs, bounds, bounds[1:]):
            part = cues[lo:hi]
            r["off"] = round(max((cover(part, r["off"] + q / 100, sc), r["off"] + q / 100) for q in range(-100, 101))[1], 2)
            shifts += [r["off"]] * (hi - lo)
        return dict(res, verdict="sections", shifts=shifts, runs=[{"from": r["secs"][0]["from"], "off": r["off"]} for r in runs])
    return dict(res, verdict="nomatch")


def tv_copy_ok(info):
    """video a TV browser plays as it is: H.264 (8-bit) and HEVC. VP9 / AV1 / MPEG-4 / 10-bit H.264 are converted"""
    v, pix = info.get("video", ""), info.get("pix") or ""
    return v == "hevc" or (v == "h264" and "10" not in pix)


def compat_cmd(m, d, fmt, t=0.0, a=0, ch=0, snd=None, quality=None, sub_start=None):
    """ffmpeg input + codec options for a converted stream (no container / output yet); returns (cmd, info, sub_in)"""
    info = dict(probe(m, d))  # a copy: per-stream notes (temporary subtitle) must not land in the shared cache
    src = "http://127.0.0.1:%d/stream/%s/x?ik=%s" % (m.port, d.r["id"], m.media_key)
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if quality is None and fmt in ("mkv", "mkvsub"):
        quality = m.settings.get("dlna_quality") or "saver"  # the TV's own player: smaller stream unless "orig" is chosen
    vq = QUALITY.get(quality) if fmt in ("tv", "mkv", "mkvsub") and shutil.which("ffmpeg") else None
    if not vq and fmt == "tv" and info.get("video") and not tv_copy_ok(info):
        vq = ("4k", 80)  # 'Original' of a codec the TV browser cannot play: near-lossless HEVC, same resolution
        info["_converted"] = True
    if vq and vq[0] != "enh" and info.get("video") in ("h264", "hevc"):  # hardware decode (VideoToolbox) where the Mac has it
        cmd += ["-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld"]
    if t > 0:
        cmd += ["-ss", "%.3f" % t]
    sub_in, sub_lang = [], ""
    if fmt in ("mkv", "mkvsub"):
        try:
            si = m.subs_info(d)
            sp = si["sid"] and m.sub_path(d, si["sid"])
        except (KeyError, OSError):
            sp = None
        if sp:
            delay = float(d.r.get("sub_delay") or 0)  # the sync set in the player is applied to the stream too
            # TV players show plain UTF-8 SRT reliably (styled ASS was recognised but not drawn by the LG player)
            cut = cut_srt(sp, t if sub_start is None else sub_start, delay, float(d.r.get("sub_scale") or 1))  # starts exactly with the video
            info["_tmp_sub"] = cut
            sub_in = ["-sub_charenc", "UTF-8", "-i", cut]
            sub_lang = next((x["lang"] for x in si["tracks"] if x["sid"] == si["sid"]), "")
    cmd += ["-i", src] + sub_in + ["-map", "0:v:0?", "-map", "0:a:%d?" % a]
    if vq:
        hdr = info.get("transfer") in ("smpte2084", "arib-std-b67")
        if vq[0] == "1080":
            cmd += ["-vf", "scale_vt=w=1920:h=-2" if info.get("video") in ("h264", "hevc") else "scale=w='min(1920,iw)':h=-2"]
        elif vq[0] == "enh":  # low-resolution video: light denoise (blocks, grain) -> Lanczos upscale to 1080p -> gentle sharpening
            cmd += ["-vf", "hqdn3d=1.5:1.2:4:3,scale=w=-2:h=1080:flags=lanczos+accurate_rnd+full_chroma_int,unsharp=5:5:0.55:3:3:0.0"]
        ten = hdr or "10" in (info.get("pix") or "")
        cmd += ["-c:v", "hevc_videotoolbox", "-profile:v", "main10" if ten else "main", "-q:v", str(vq[1]), "-g", "48", "-tag:v", "hvc1"]
        if ten and (info.get("video") not in ("h264", "hevc") or vq[0] == "enh"):  # software-decoded frames: give the 10-bit encoder 10-bit input
            cmd += ["-pix_fmt", "p010le"]
        if hdr:
            cmd += ["-color_primaries", "bt2020", "-color_trc", info["transfer"], "-colorspace", "bt2020nc"]
    else:
        cmd += ["-c:v", "copy"]
        if info.get("video") == "hevc" and fmt in ("mp4", "tv"):
            cmd += ["-tag:v", "hvc1"]
    chans = min(6, max((x.get("ch") or 2 for x in info.get("audio", [])[a:a + 1]), default=2))
    if ch:
        chans = min(chans, ch)
    mode = snd or d.r.get("tv_audio") or m.settings.get("tv_audio") or "original"  # per film, else the default
    if fmt in ("mkv", "tv") and mode in AUDIO_MODES:
        af = audio_filter(mode, chans)
        if af:
            chans = 2
            cmd += af
    if fmt == "mkvsub":
        cmd += ["-c:a", "copy"]
    elif fmt == "mp4":
        # FLAC needs whole blocks (after a seek the first decoded frame can be tiny)
        cmd += ["-af", "asetnsamples=n=4096:p=0", "-c:a", "flac", "-strict", "experimental"]
    else:
        if mode == "original":  # untouched mix; Dolby Digital Plus is the best format TV browsers decode (not DTS / TrueHD)
            cmd += ["-c:a", "eac3", "-b:a", "1536k" if chans > 2 else "640k", "-ac", str(chans)]
        else:
            cmd += ["-c:a", "aac", "-b:a", "448k" if chans > 2 else "256k", "-ac", str(chans)]
    if sub_in:
        lang3 = {"fa": "per", "en": "eng", "ar": "ara"}.get(sub_lang, sub_lang or "und")
        cmd += ["-map", "1:0", "-c:s", "srt", "-metadata:s:s:0", "language=" + lang3, "-metadata:s:s:0", "title=" + LANG_NAMES.get(sub_lang, "Subtitle"),
                "-disposition:s:0", "default", "-dn", "-map_chapters", "-1"]
    else:
        cmd += ["-sn", "-dn", "-map_chapters", "-1"]
    return cmd, info, sub_in


def send_compat(h, m, d, fmt, t=0.0, a=0, ch=0, snd=None, seek_hdr=False, quality=None, mkv=False):
    """video passed through untouched (bit-identical), only the audio is converted on the fly:
    mp4 (computer browsers) = FLAC, lossless, all channels; mkv (TV players) = AAC 5.1 448k;
    tv (TV web browsers) = MP4 with AAC 5.1 (ch=2 for stereo)"""
    if not shutil.which("ffmpeg"):
        return h.out(501, {"error": "ffmpeg is not installed (brew install ffmpeg)"})
    q_eff = quality if quality is not None or fmt not in ("mkv", "mkvsub") else (m.settings.get("dlna_quality") or "saver")
    copy = fmt in ("mkv", "mkvsub") and q_eff not in QUALITY and t > 0
    land = HLS.landing("http://127.0.0.1:%d/stream/%s/x?ik=%s" % (m.port, d.r["id"], m.media_key), t) if copy else t
    # ffmpeg times input 0 from the asked t (then shifts every stream equally): the subtitle is cut at t too
    cmd, info, sub_in = compat_cmd(m, d, fmt, t, a, ch, snd, quality)
    if copy and "-ss" in cmd:
        cmd.insert(cmd.index("-ss"), "-noaccurate_seek")
    # LG's video engine (webOS "NetCast") plays only the first fragment of a fragmented MP4 and then reports "ended":
    # the TV player gets a continuous Matroska stream instead (same codecs; the TV's own player uses it too)
    as_mp4 = fmt == "mp4" or (fmt == "tv" and not mkv)
    cmd += (["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"] if as_mp4 else ["-f", "matroska", "-live", "1"])
    cmd += ["pipe:1"]
    h.send_response(200)
    h.send_header("Content-Type", "video/mp4" if as_mp4 else "video/x-matroska")
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Connection", "close")
    if fmt in ("mkv", "mkvsub"):
        h.send_header("transferMode.dlna.org", "Streaming")
        h.send_header("contentFeatures.dlna.org", DLNA_LIVE)
        dur0 = info.get("duration") or 0
        if dur0:
            h.send_header("TimeSeekRange.dlna.org", "npt=%.3f-%.3f/%.3f" % (t, dur0, dur0))
            h.send_header("X-AvailableSeekRange", "1 npt=0.0-%.3f" % dur0)
    h.end_headers()
    if h.command == "HEAD":
        if info.get("_tmp_sub"):
            rm(info["_tmp_sub"])
        return
    label = {"mp4": "player with lossless sound", "tv": "TV browser player", "mkv": "TV-compatible sound",
             "mkvsub": "original sound + subtitle"}.get(fmt, fmt)
    sx = m.sessions.begin(h, d, label)
    dur, t_req = (info.get("duration") or 0), time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = []
    threading.Thread(target=lambda: err.append(p.stderr.read().decode("utf-8", "ignore")), daemon=True).start()
    sent, why = 0, "ffmpeg finished"
    log.info("media %s %s %s t=%.1f q=%s range=%s ua=%s", h.client_address[0], logsafe(fmt, 20), d.r["num"], t, logsafe(quality, 20), logsafe(h.headers.get("Range", "-")),
             logsafe(h.headers.get("User-Agent") or "", 60))
    try:
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                break
            h.wfile.write(chunk)
            sent += len(chunk)
            m.sessions.add(sx, len(chunk), min(1, (t + time.time() - t_req) / dur) if dur else None)
    except (BrokenPipeError, ConnectionResetError) as e:
        why = "client closed (%s)" % e.__class__.__name__
    finally:
        m.sessions.end(sx)
        p.kill()
        rc = p.wait()
        if info.get("_tmp_sub"):
            rm(info["_tmp_sub"])
        time.sleep(0.1)
        log.info("media end %s %s t=%.1f: %.1f MB in %.1f s (%.1f Mbit/s), %s, ffmpeg rc=%s %s", h.client_address[0], fmt, t, sent / 1e6,
                 time.time() - t_req, sent * 8 / 1e6 / max(0.1, time.time() - t_req), why, rc, "".join(err)[-300:].replace("\n", " | "))


class HLS:
    """TV browser player stream as HLS (4 s pieces + a growing playlist). LG's browser video engine fixes a progressive
    stream's length at the start (0.3-1.6 s) and then reports 'ended'; with HLS it keeps fetching - like streaming apps.
    ffmpeg pauses when it is far ahead of the TV, played pieces are deleted, idle sessions are removed."""
    ROOT = os.path.join(STATE_DIR, "hls")
    AHEAD, KEEP, IDLE = 20, 4, 600  # pieces ahead before pausing ffmpeg, pieces kept behind the TV, idle seconds

    def __init__(self, m):
        self.m, self.sess, self.lock = m, {}, threading.Lock()
        shutil.rmtree(self.ROOT, ignore_errors=True)
        threading.Thread(target=self.loop, daemon=True, name="hls").start()

    @staticmethod
    def landing(src, t):
        """where ffmpeg's jump to t really lands: copied video can only start at an indexed keyframe, often up to a
        second before t. Asked from ffmpeg itself (one frame, original timestamps) - ffprobe lands elsewhere."""
        if t <= 0:
            return 0.0
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-noaccurate_seek", "-ss", "%.3f" % t, "-i", src, "-map", "0:v:0", "-c", "copy",
                            "-copyts", "-frames:v", "1", "-f", "framemd5", "-"], capture_output=True, text=True, timeout=30)
        tb = re.search(r"#tb 0: (\d+)/(\d+)", r.stdout)
        row = [l for l in r.stdout.splitlines() if l and not l.startswith("#")]
        if not tb or not row:
            return t
        pts = int(row[0].split(",")[2])
        land = pts * int(tb.group(1)) / int(tb.group(2))
        return land if 0 <= land <= t + 0.01 and t - land < 30 else t

    def start(self, d, ip, t, a, ch, snd, quality):
        copy = quality not in QUALITY and tv_copy_ok(probe(self.m, d))
        ask = t
        if copy:  # original video (copied): start picture and sound together where the file's index really lands
            t = self.landing("http://127.0.0.1:%d/stream/%s/x?ik=%s" % (self.m.port, d.r["id"], self.m.media_key), t)
        cmd, info, _ = compat_cmd(self.m, d, "tv", ask, a, ch, snd, quality)
        if copy and "-ss" in cmd:
            cmd.insert(cmd.index("-ss"), "-noaccurate_seek")  # do not trim the sound to 'ask': keep it with the picture
        with self.lock:
            for sid, x in list(self.sess.items()):  # one stream per TV: a seek / quality change replaces the old one
                if x["ip"] == ip:
                    self.stop(sid)
            sid = secrets.token_hex(6)
            path = os.path.join(self.ROOT, sid)
            os.makedirs(path, exist_ok=True)
            cmd += ["-f", "hls", "-hls_time", "4", "-hls_list_size", "0", "-hls_playlist_type", "event", "-hls_segment_type", "fmp4",
                    "-hls_flags", "independent_segments+temp_file", "-hls_fmp4_init_filename", "init.mp4",
                    "-hls_segment_filename", os.path.join(path, "s%05d.m4s"), os.path.join(path, "index.m3u8")]
            err = open(os.path.join(path, "ffmpeg.log"), "wb")
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
            self.sess[sid] = {"ip": ip, "dir": path, "p": p, "num": d.r["num"], "d": d, "t": t, "q": quality, "at": time.time(),
                              "seen": time.time(), "want": 0, "stopped": False, "sx": None}
        log.info("hls start %s %s t=%.1f q=%s sid=%s", ip, d.r["num"], t, quality, sid)
        end = time.time() + 25  # answer when the first piece is ready
        while time.time() < end:
            if os.path.exists(os.path.join(path, "s00000.m4s")) and os.path.exists(os.path.join(path, "index.m3u8")):
                self.sess[sid]["off"] = self.first_time(path)
                return sid
            if p.poll() is not None:
                break
            time.sleep(0.2)
        tail = rd(os.path.join(path, "ffmpeg.log"))[-300:]
        self.stop(sid)
        raise ValueError("the stream did not start: " + (tail.strip() or "timeout"))

    @staticmethod
    def first_time(path):
        """time of the first frame in the first piece (ffmpeg starts a converted stream a little after 0)"""
        try:
            data = open(os.path.join(path, "init.mp4"), "rb").read() + open(os.path.join(path, "s00000.m4s"), "rb").read()
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=start_time", "-of", "csv=p=0", "-i", "pipe:0"],
                               input=data, capture_output=True, timeout=10)
            vals = [float(x) for x in r.stdout.decode().split() if re.match(r"^-?[\d.]+$", x)]
            return round(min(vals), 3) if vals else 0.0
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0.0

    def stop(self, sid):
        x = self.sess.pop(sid, None)
        if not x:
            return
        try:
            x["p"].send_signal(signal.SIGCONT)
            x["p"].kill()
            x["p"].wait(5)
        except (OSError, subprocess.SubprocessError):
            pass
        if x["sx"]:
            self.m.sessions.end(x["sx"])
        shutil.rmtree(x["dir"], ignore_errors=True)
        log.info("hls stop %s", sid)

    def serve(self, h, sid, name):
        x = self.sess.get(sid)
        if not x or not re.match(r"^(index\.m3u8|init\.mp4|s\d{5}\.m4s)$", name):
            return h.out(404, b"gone", "text/plain")
        x["seen"] = time.time()
        f = safe_join(x["dir"], name)
        if not f or not os.path.exists(f):
            return h.out(404, b"not yet", "text/plain")
        data = open(f, "rb").read()
        if name == "index.m3u8":
            data = data.replace(b"#EXTM3U\n", b"#EXTM3U\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES\n", 1)
        mm = re.match(r"^s(\d{5})\.m4s$", name)
        if mm:
            x["want"] = max(x["want"], int(mm.group(1)))
            if not x["sx"]:
                x["sx"] = self.m.sessions.begin(h, x["d"], "TV browser player")
            if x["sx"]:
                dur = (probe(self.m, x["d"]).get("duration") or 0)
                self.m.sessions.add(x["sx"], len(data), min(1, (x["t"] + x["want"] * 4) / dur) if dur else None)
        ct = {"m3u8": "application/vnd.apple.mpegurl", "mp4": "video/mp4", "m4s": "video/iso.segment"}[name.rsplit(".", 1)[1]]
        h.send_response(200)
        h.send_header("Content-Type", ct)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-cache" if name.endswith(".m3u8") else "max-age=600")
        h.send_header("Access-Control-Allow-Origin", "*")
        h.end_headers()
        if h.command != "HEAD":
            try:
                h.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def loop(self):
        while True:
            time.sleep(1)
            now = time.time()
            for sid, x in list(self.sess.items()):
                try:
                    if now - x["seen"] > self.IDLE or (x["p"].poll() is not None and now - x["seen"] > 120):
                        self.stop(sid)
                        continue
                    made = sorted(int(n[1:6]) for n in os.listdir(x["dir"]) if re.match(r"^s\d{5}\.m4s$", n))
                    last = made[-1] if made else 0
                    if x["p"].poll() is None:  # pace ffmpeg to the TV: pause when far ahead
                        ahead = last - x["want"]
                        if ahead > self.AHEAD and not x["stopped"]:
                            x["p"].send_signal(signal.SIGSTOP)
                            x["stopped"] = True
                        elif ahead < self.AHEAD // 2 and x["stopped"]:
                            x["p"].send_signal(signal.SIGCONT)
                            x["stopped"] = False
                    for n in made:  # pieces the TV has played are not needed again (a seek starts a new stream)
                        if n < x["want"] - self.KEEP:
                            try:
                                os.remove(os.path.join(x["dir"], "s%05d.m4s" % n))
                            except OSError:
                                pass
                except (OSError, ValueError):
                    pass

    def shutdown(self):
        for sid in list(self.sess):
            self.stop(sid)


def tv_page(m, d):
    info = probe(m, d)
    cfg = {"snd": d.r.get("tv_audio") or m.settings.get("tv_audio") or "original", "qual": QUALITY_LABELS, "qdef": m.settings.get("tv_quality") or "auto",
           "hdr": info.get("transfer") in ("smpte2084", "arib-std-b67"), "vcodec": info.get("video", ""), "host": m.lan_ip(),
           "width": info.get("width", 0), "ach": max([x.get("ch") or 0 for x in info.get("audio") or []] + [0]),
           "num": d.r["num"], "title": d.r["name"], "dur": info.get("duration") or 0, "audio": info.get("audio") or [],
           "ffmpeg": bool(shutil.which("ffmpeg")), "dlna": m.friendly()}
    cfg["subs"] = m.subs_info(d)
    pt, yr, _ = parse_release(d.r["name"])
    cfg["pretty"], cfg["year"] = pt.strip().title() if pt.islower() else pt.strip(), yr
    return (TV_PAGE.replace("__TITLE__", xesc(d.r["name"])).replace("__CFG__", json.dumps(cfg).replace("</", "<\\/"))
            .replace("/*SUBJS*/", fonts_js(m) + SUBJS).replace("/*SUBCSS*/", font_css(m) + SUBCSS))


# 10-foot player for TV web browsers (LG webOS, Samsung, Android TV): ES5 only, remote-control keys
TV_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>__TITLE__ - dlus</title><style>
/*SUBCSS*/
:root{--acc:#8b7bff;--acc2:#5b8cff;--glass:rgba(28,28,38,.46);--glass2:rgba(255,255,255,.10);--edge:rgba(255,255,255,.16);--txt:#f5f5f8;--mut:rgba(235,235,245,.62);
--spring:cubic-bezier(.32,1.4,.48,1);--ease:cubic-bezier(.22,.9,.3,1)}
html,body{margin:0;width:100%;height:100%;background:#000;overflow:hidden;color:var(--txt);font-family:"Vazirmatn","LG Smart UI","Roboto","Helvetica Neue",Arial,sans-serif;cursor:none;-webkit-user-select:none;user-select:none;-webkit-font-smoothing:antialiased}
body.ptr{cursor:default}
html{height:100%;background:#000}video{position:fixed;inset:0;width:100%;height:100%;object-fit:contain;background:#000;border:0;outline:0}
/* nothing ever touches the video element itself (no filter / transform): the picture stays exactly as decoded */
#dim{position:fixed;inset:0;z-index:3;pointer-events:none;background:rgba(0,0,0,.55);opacity:0;transition:opacity .6s var(--ease)}
body.paused #dim{opacity:1}
.glass{background:var(--glass);-webkit-backdrop-filter:blur(30px) saturate(170%);backdrop-filter:blur(30px) saturate(170%);border:1px solid var(--edge);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.22),inset 0 -1px 0 rgba(255,255,255,.04),0 24px 70px rgba(0,0,0,.45)}
.ov{position:fixed;z-index:5;transition:opacity .45s var(--ease),transform .5s var(--ease)}
#shade{position:fixed;inset:0;z-index:4;pointer-events:none;background:linear-gradient(180deg,rgba(0,0,0,.55),transparent 22%,transparent 62%,rgba(0,0,0,.72));transition:opacity .5s}
body.hide #shade{opacity:0}
#top{left:80px;right:80px;top:56px;display:flex;align-items:flex-start;gap:24px}
#closep{display:none;position:fixed;z-index:7;top:24px;left:24px;height:44px;padding:0 18px;border-radius:22px;color:#fff;font:700 15px -apple-system,system-ui,sans-serif;cursor:pointer;align-items:center;gap:8px;transition:opacity .45s,transform .3s}
#closep:hover{background:#fff;color:#000}body.mac #closep{display:inline-flex}body.mac #top{top:84px}body.hide #closep{opacity:0;pointer-events:none}
#top .lg{width:52px;height:52px;border-radius:16px;flex:none;display:grid;place-items:center;background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 8px 30px rgba(120,100,255,.45)}
#top .lg svg{width:30px;height:30px}
#top .ti{min-width:0;flex:1}
#title{margin:0;font-size:44px;font-weight:800;letter-spacing:-.02em;line-height:1.1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{display:flex;gap:10px;margin-top:14px;align-items:center;flex-wrap:wrap}
.chip{height:30px;padding:0 12px;border-radius:9px;display:inline-flex;align-items:center;font-size:17px;font-weight:700;letter-spacing:.04em;background:var(--glass2);border:1px solid var(--edge);color:#fff}
.chip.hdr{background:linear-gradient(90deg,#ffb547,#ff6a3d);border-color:transparent;color:#1a1000}
.chip.dim{background:none;border-color:transparent;color:var(--mut);font-weight:500;letter-spacing:0;padding:0 4px}
#clock{flex:none;text-align:right}#clock b{display:block;font-size:34px;font-weight:300;letter-spacing:.01em}#clock span{font-size:18px;color:var(--mut)}
body.hide #top{opacity:0;transform:translateY(-36px)}
#bot{left:80px;right:80px;bottom:52px}
body.hide #bot{opacity:0;transform:translateY(46px)}
.tl{display:flex;align-items:center;gap:28px;font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.tl span{min-width:110px}.tl span:last-child{text-align:right;color:var(--mut)}
.track{position:relative;flex:1;height:6px;border-radius:6px;background:rgba(255,255,255,.18);transition:height .25s var(--spring)}
.track.f{height:12px}.track i{position:absolute;left:0;top:0;bottom:0;border-radius:6px}
#ready{background:rgba(255,255,255,.32)}#played{background:linear-gradient(90deg,var(--acc2),var(--acc));box-shadow:0 0 18px rgba(139,123,255,.75)}
#knob{position:absolute;top:50%;width:20px;height:20px;margin:-10px 0 0 -10px;border-radius:50%;background:#fff;box-shadow:0 0 0 6px rgba(255,255,255,.18),0 4px 14px rgba(0,0,0,.6);transition:transform .25s var(--spring)}
.track.f #knob{transform:scale(1.4)}
#prev{position:absolute;bottom:34px;transform:translateX(-50%) translateY(10px) scale(.92);opacity:0;transition:opacity .2s,transform .3s var(--spring);pointer-events:none;
  border-radius:22px;padding:8px;width:384px}
#prev.on{opacity:1;transform:translateX(-50%) translateY(0) scale(1)}
#prev .img{width:368px;height:207px;border-radius:15px;background:#111 center/cover no-repeat;position:relative;overflow:hidden}
#prev .img.load:after{content:"";position:absolute;inset:0;background:linear-gradient(100deg,transparent 30%,rgba(255,255,255,.12) 50%,transparent 70%);animation:shim 1.1s infinite}
@keyframes shim{from{transform:translateX(-100%)}to{transform:translateX(100%)}}
#prev .img .nd{position:absolute;inset:0;display:none;place-items:center;text-align:center;font-size:19px;color:var(--mut);padding:20px}
#prev .img.nodl .nd{display:grid}
#prev .pt{display:flex;justify-content:space-between;align-items:center;padding:10px 8px 4px;font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
#prev .pt span{font-size:17px;font-weight:600;color:var(--mut)}
.dock{display:flex;align-items:center;justify-content:center;gap:14px;margin:34px auto 0;padding:12px;border-radius:999px;width:max-content}
.b{position:relative;display:grid;place-items:center;width:76px;height:76px;border-radius:50%;border:0;background:transparent;color:#fff;cursor:pointer;transition:transform .35s var(--spring),background .2s,box-shadow .3s}
.b svg{width:32px;height:32px}.b.main{width:92px;height:92px;background:rgba(255,255,255,.14)}.b.main svg{width:42px;height:42px}
.b .bt{display:flex;flex-direction:column;align-items:center;line-height:1}.b .bt b{font-size:23px;font-weight:800;letter-spacing:-.02em}
.b .bt small{font-size:10.5px;font-weight:800;letter-spacing:.09em;margin-top:4px;opacity:.72}.b .bt small:empty{display:none}
.b.f,.b:hover{background:#fff;color:#0b0b10;transform:scale(1.14);box-shadow:0 0 0 6px rgba(139,123,255,.38),0 14px 40px rgba(139,123,255,.45)}
.b .lb{position:absolute;bottom:-50px;left:50%;transform:translateX(-50%) translateY(-6px);opacity:0;white-space:nowrap;font-size:19px;font-weight:700;color:#fff;
  padding:6px 14px;border-radius:12px;background:rgba(0,0,0,.55);transition:opacity .2s,transform .3s var(--spring);pointer-events:none}
.b.f .lb,.b:hover .lb{opacity:1;transform:translateX(-50%) translateY(0)}.b .lb em{font-style:normal;color:var(--mut);font-weight:500;margin-left:6px}
.sep{width:1px;height:44px;background:var(--edge);margin:0 6px}
.b .dot{position:absolute;top:14px;right:14px;width:10px;height:10px;border-radius:50%;background:var(--acc);box-shadow:0 0 10px var(--acc)}
#pause{position:fixed;left:80px;bottom:300px;z-index:5;max-width:60vw;opacity:0;transform:translateY(20px);transition:opacity .5s var(--ease),transform .6s var(--ease);pointer-events:none}
body.paused:not(.hide):not(.seeking) #pause{opacity:1;transform:none}
#pause .pl{font-size:21px;font-weight:700;letter-spacing:.12em;color:var(--acc);text-transform:uppercase}
#pause h2{margin:10px 0 12px;font-size:64px;font-weight:800;letter-spacing:-.025em;line-height:1.05}
#pause p{margin:0;font-size:24px;color:var(--mut)}#pause p b{color:#fff}
#spin{position:fixed;left:50%;top:50%;z-index:6;transform:translate(-50%,-50%);display:none;text-align:center;padding:30px 44px;border-radius:30px}
#spin .r{width:72px;height:72px;margin:0 auto;border-radius:50%;background:conic-gradient(from 0deg,transparent 0 20%,var(--acc) 90%,#fff);-webkit-mask:radial-gradient(farthest-side,transparent calc(100% - 7px),#000 calc(100% - 6px));mask:radial-gradient(farthest-side,transparent calc(100% - 7px),#000 calc(100% - 6px));animation:rot .9s linear infinite}
#spin p{margin:20px 0 0;font-size:24px;font-weight:700}#spin small{display:block;font-size:18px;color:var(--mut);margin-top:6px;max-width:440px}
@keyframes rot{to{transform:rotate(360deg)}}
#big{position:fixed;left:50%;top:50%;z-index:6;width:140px;height:140px;margin:-70px 0 0 -70px;border-radius:50%;display:grid;place-items:center;opacity:0;transform:scale(.7);transition:opacity .3s,transform .45s var(--spring)}
#big.on{opacity:1;transform:scale(1)}#big svg{width:58px;height:58px}
#seekhint{position:fixed;top:50%;z-index:6;transform:translateY(-50%);display:none;align-items:center;gap:12px;font-size:30px;font-weight:800;padding:18px 28px;border-radius:24px;font-variant-numeric:tabular-nums}
#sheet{position:fixed;top:40px;right:40px;bottom:40px;z-index:8;width:620px;max-width:44vw;padding:40px 34px 30px;box-sizing:border-box;border-radius:36px;overflow:auto;
  transform:translateX(calc(100% + 60px)) scale(.97);transition:transform .5s var(--spring);background:rgba(22,22,30,.62)}
#sheet.on{transform:none}
#sheet .sh{display:flex;align-items:center;gap:16px;margin-bottom:6px}#sheet .sh i{width:48px;height:48px;border-radius:14px;display:grid;place-items:center;background:var(--glass2);border:1px solid var(--edge)}
#sheet .sh i svg{width:26px;height:26px}#sheet h2{margin:0;font-size:32px;font-weight:800;letter-spacing:-.01em}
#sheet .sub{color:var(--mut);font-size:19px;margin:10px 0 24px;line-height:1.45}
.opt{display:flex;align-items:center;gap:18px;padding:18px 20px;margin-bottom:8px;border-radius:20px;font-size:23px;cursor:pointer;border:1px solid transparent;transition:background .2s,transform .3s var(--spring)}
.opt .chk{width:28px;height:28px;border-radius:50%;border:2px solid rgba(255,255,255,.35);flex:none;display:grid;place-items:center;transition:all .3s var(--spring)}
.opt.sel .chk{border-color:transparent;background:linear-gradient(135deg,var(--acc),var(--acc2))}.opt.sel .chk:after{content:"";width:8px;height:14px;margin-top:-3px;border:solid #fff;border-width:0 3px 3px 0;transform:rotate(45deg)}
.opt b{font-weight:700}.opt .d{display:block;color:var(--mut);font-size:18px;margin-top:3px;font-weight:500}
.opt.f,.opt:hover{background:#fff;color:#0b0b10;transform:scale(1.02)}.opt.f .d,.opt:hover .d{color:#555}.opt.f .chk,.opt:hover .chk{border-color:#bbb}
#sheet .smenu{position:static;display:block;background:none;border:0;box-shadow:none;padding:0;min-width:0;max-width:none;font-size:23px;backdrop-filter:none;-webkit-backdrop-filter:none}
#sheet .smh{display:none}#sheet .smr{padding:17px 20px;border-radius:20px;border:1px solid transparent;margin-bottom:6px;transition:background .2s,transform .3s var(--spring)}
#sheet .smr em{max-width:300px}#sheet .smr b{opacity:.35}
#sheet .smr.on{background:#fff;color:#0b0b10;transform:scale(1.02)}#sheet .smr.on span{color:#444}#sheet .smr.on b{opacity:1}
#sheet .smf{font-size:17px;color:var(--mut);padding:14px 4px}
#sprev{margin:4px 0 22px;padding:22px;border-radius:22px;background:#000 linear-gradient(135deg,#1d2a44,#3b2a46);text-align:center;font-size:30px;min-height:46px;position:relative;overflow:hidden}
.info div{display:flex;justify-content:space-between;gap:20px;padding:16px 4px;border-bottom:1px solid rgba(255,255,255,.08);font-size:21px}.info span{color:var(--mut)}
#dlg{position:fixed;inset:0;z-index:9;display:none;place-items:center;background:rgba(0,0,0,.35)}#dlg.on{display:grid}
#dlg .box{width:820px;padding:48px;border-radius:40px;text-align:left}
#dlg .k{font-size:20px;font-weight:700;letter-spacing:.12em;color:var(--acc);text-transform:uppercase}
#dlg h2{margin:10px 0 8px;font-size:42px;font-weight:800;letter-spacing:-.02em}#dlg p{margin:0 0 32px;color:var(--mut);font-size:23px}
#dlg .row{display:flex;gap:16px}
.pill{display:inline-flex;align-items:center;gap:12px;height:68px;padding:0 30px;border-radius:999px;border:1px solid var(--edge);background:var(--glass2);color:#fff;font:700 23px "Vazirmatn",Roboto,sans-serif;cursor:pointer;transition:transform .35s var(--spring),background .2s}
.pill svg{width:26px;height:26px}.pill.f,.pill:hover{background:#fff;color:#0b0b10;transform:scale(1.06)}
#toast{position:fixed;top:56px;left:50%;z-index:10;transform:translate(-50%,-40px) scale(.95);opacity:0;padding:16px 28px;border-radius:999px;font-size:22px;font-weight:600;transition:all .45s var(--spring)}
#toast.on{opacity:1;transform:translate(-50%,0) scale(1)}
.subs{position:fixed;transition:bottom .45s var(--ease)}
body:not(.hide) .subs{bottom:30% !important}
</style></head><body class=hide>
<video id=v playsinline></video><div id=dim></div>
<div id=shade></div>
<div id=subs class=subs></div>
<div id=big class=glass></div><div id=seekhint class=glass></div>
<div id=spin class=glass><div class=r></div><p id=spinp>Loading</p><small id=spins></small></div>
<div id=pause><div class=pl>Paused</div><h2 id=ptitle></h2><p id=pinfo></p></div>
<button id=closep class=glass title="Close the player (Esc)" onclick="event.stopPropagation();leave()">&#10005;<span>Close</span></button>
<div id=top class=ov><div class=lg><svg viewBox="0 0 64 64"><path d="M32 13v21M23.5 25.5 32 34l8.5-8.5" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/><path d="M19 38.5a13 13 0 0 0 26 0" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round"/></svg></div>
 <div class=ti><h1 id=title>__TITLE__</h1><div class=meta id=chips></div></div><div id=clock><b id=clk></b><span id=ends></span></div></div>
<div id=bot class=ov>
 <div class=tl><span id=cur>0:00</span><div class=track id=track><i id=ready></i><i id=played></i><b id=knob></b>
  <div id=prev class=glass><div class="img" id=pimg><div class=nd>Not downloaded yet<br>dlus will fetch this part first</div></div><div class=pt><b id=ptime>0:00</b><span id=pdelta></span></div></div></div><span id=rem>-0:00</span></div>
 <div class="dock glass" id=ctl></div>
</div>
<div id=sheet class=glass onclick="event.stopPropagation()"></div>
<div id=dlg><div class="box glass" onclick="event.stopPropagation()"><div class=k>Welcome back</div><h2 id=dlgt></h2><p id=dlgp></p><div class=row id=dlgb></div></div></div>
<div id=toast class=glass></div>
<script>
/*SUBJS*/
var C = __CFG__;
var $ = function (i) { return document.getElementById(i) }, v = $("v");
function svg(p, fill) { return '<svg viewBox="0 0 24 24" fill="' + (fill ? "currentColor" : "none") + '" stroke="' + (fill ? "none" : "currentColor") + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>' }
var IC = {
  play: svg('<path d="M8 5.2v13.6a1 1 0 0 0 1.5.86l11-6.8a1 1 0 0 0 0-1.72l-11-6.8A1 1 0 0 0 8 5.2z"/>', 1),
  pause: svg('<rect x="6" y="4.5" width="4.4" height="15" rx="1.6"/><rect x="13.6" y="4.5" width="4.4" height="15" rx="1.6"/>', 1),
  restart: svg('<path d="M4 4v6h6"/><path d="M5.2 15a8 8 0 1 0 1.6-8.4L4 10"/><path d="M11 9.5v5l4-2.5z" fill="currentColor" stroke="none"/>'),
  back: svg('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><text x="12" y="15.3" font-size="6.5" font-weight="800" text-anchor="middle" fill="currentColor" stroke="none">10</text>'),
  fwd: svg('<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/><text x="12" y="15.3" font-size="6.5" font-weight="800" text-anchor="middle" fill="currentColor" stroke="none">10</text>'),
  cc: svg('<rect x="2.5" y="4.5" width="19" height="15" rx="4"/><path d="M10 10.3a2.5 2.5 0 1 0 0 3.4M17 10.3a2.5 2.5 0 1 0 0 3.4"/>'),
  snd: svg('<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>'),
  hd: svg('<path d="M12 3l2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 15.4l-4.8 2.5.9-5.4-3.9-3.8 5.4-.8z"/>'),
  info: svg('<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5h.01"/>')
};
var S = { started: false, t0: 0, CH: 6, Q: "high", auto: true, errs: 0, stalls: [], live: false, liveAt: 0, stallT: null, waitT: null, switchAt: 0, ends: [], seekTo: -1, seekTimer: null, hideT: null, area: "none", ci: 1, sheet: null, si: 0, ready: 0, backT: 0, seq: 0, thumbT: null };
var KEY = "dlus-pos-" + C.num, SKEY = "dlus-subs-" + C.num, QKEY = "dlus-q-" + C.num;  // quality is kept per film
var QORDER = ["orig", "high", "saver", "1080", "1080low"], QN = { orig: "Original", high: "4K High", saver: "4K Saver", "1080": "1080p", "1080low": "1080p Low", enhance: "Enhanced 1080p" };
var LOWRES = C.width && C.width < 1280;  // under 720p: 'Enhance' is offered; good videos are never touched
// qualities this file can have: 4K -> all; 1080p -> original + lower bitrates; 720p -> original; smaller -> original + enhanced
QORDER = !C.width || C.width >= 3800 ? ["orig", "high", "saver", "1080", "1080low"] : C.width >= 1900 ? ["orig", "1080", "1080low"] : C.width >= 1280 ? ["orig"] : ["orig", "enhance"];
function modeName(m) {  // hdrCinemaBright -> HDR Cinema Home, dolbyHdrVivid -> Dolby Vision Vivid, normal -> Standard
  var n = { normal: "Standard", hdrCinemaBright: "HDR Cinema Home", dolbyHdrCinemaBright: "Dolby Vision Cinema Home", cinemaBright: "Cinema Home", filmMaker: "Filmmaker",
            hdrFilmMaker: "HDR Filmmaker", dolbyHdrFilmMaker: "Dolby Vision Filmmaker", expert1: "Expert (bright)", expert2: "Expert (dark)" }[m];
  if (n) return n;
  return String(m || "").replace(/^dolbyHdr/, "Dolby Vision ").replace(/^hdr/, "HDR ").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/^./, function (c) { return c.toUpperCase() });
}
function tvPicture(open) {
  var x = new XMLHttpRequest(); x.open("GET", C.num + "-picture" + (open ? "?open=1" : ""), true);
  x.onload = function () {
    var r = {}; try { r = JSON.parse(x.responseText) } catch (e) {}
    if (r.pictureMode) { S.pic = r.pictureMode; render() }
    if (open) toast(r.error ? "TV picture: " + r.error : "TV picture settings \u2014 choose the mode, BACK returns to the film");
  };
  x.send();
}
function toLG() {  // the TV's own player: full HDR picture processing, untouched sound, same position and subtitle
  toast("Opening in LG's player at " + fmt(now()) + "...");
  v.pause();
  var x = new XMLHttpRequest(); x.open("GET", C.num + "-native?t=" + now().toFixed(1), true);
  x.onload = function () { var r = {}; try { r = JSON.parse(x.responseText) } catch (e) {} if (r.error) toast("LG player: " + r.error) };
  x.send();
}
function badge(big, small) { return "<span class=bt><b>" + big + "</b><small>" + small + "</small></span>" }
function resName(q) { if (q == "enhance") return "1080p"; return q == "1080" || q == "1080low" || C.width < 3800 ? (C.width >= 1900 || q == "1080" || q == "1080low" ? "1080p" : "HD") : "4K" }
function qBadge() { return badge(resName(S.Q), S.Q == "enhance" ? "ENHANCED" : S.Q == "orig" ? (S.conv ? "CONVERTED" : "ORIGINAL") : C.hdr ? "HDR" : "") }
function sndOut(m) {  // what the TV is actually receiving
  var multi = C.ach > 2;
  return m == "original" ? (multi ? ["5.1", "DD+", "Dolby Digital Plus 5.1 \u00b7 1536 kbps"] : ["2.0", "DD+", "Dolby Digital Plus stereo"]) :
    m == "surround" ? (multi ? ["5.1", "AAC", "AAC 5.1 \u00b7 448 kbps"] : ["2.0", "AAC", "AAC stereo"]) :
    m == "night" ? ["2.0", "NIGHT", "Stereo, even volume (AAC)"] : ["2.0", "VOICE", "Stereo, clear dialogue (AAC)"];
}
function sBadge() { var o = sndOut(SUBS.snd || "original"); return badge(o[0], o[1]) }
function srcAudio() { var a = (C.audio || [])[0] || {}; return a.title || ((a.codec || "").toUpperCase() + (a.ch ? " " + (a.ch == 8 ? "7.1" : a.ch == 6 ? "5.1" : a.ch == 2 ? "2.0" : a.ch + "ch") : "")) }
var SN = { original: "Original sound", dialog: "Clear dialogue", night: "Night mode", surround: "5.1 surround" };
function fmt(s) { s = Math.max(0, Math.floor(s || 0)); var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return (h ? h + ":" + (m < 10 ? "0" : "") + m : m) + ":" + (x < 10 ? "0" : "") + x }
function hm(d) { return (d.getHours() < 10 ? "0" : "") + d.getHours() + ":" + (d.getMinutes() < 10 ? "0" : "") + d.getMinutes() }
function now() { return S.t0 + (v.currentTime || 0) - (S.off || 0) }
function toast(t) { var e = $("toast"); e.textContent = t; e.className = "glass on"; clearTimeout(toast.t); toast.t = setTimeout(function () { e.className = "glass" }, 2600) }
function big(ic) { var b = $("big"); b.innerHTML = IC[ic]; b.className = "glass on"; clearTimeout(big.t); big.t = setTimeout(function () { b.className = "glass" }, 650) }
function spin(on, t, sub) { $("spin").style.display = on ? "block" : "none"; if (t) $("spinp").textContent = t; $("spins").textContent = sub || "" }
// ---------- overlay / focus
function showUI(focusCtl) {
  document.body.classList.remove("hide"); clearTimeout(S.hideT);
  if (focusCtl && S.area == "none") { S.area = "ctl"; S.ci = MAINI }
  render(); S.hideT = setTimeout(hideUI, 5000);
}
function hideUI() { if (!S.started || v.paused || S.sheet || S.area == "track") { S.hideT = setTimeout(hideUI, 5000); return } document.body.classList.add("hide"); S.area = "none"; render() }
var ONTV = /Web0S|webOS|NetCast|LGE|SmartTV|Tizen/i.test(navigator.userAgent);
var BTNS = [["restart", "Start over", function () { if (!S.started) return start(0); S.seekTo = -1; load(0); toast("Playing from the beginning") }], ["back", "Back 10 s", function () { seekBy(-10) }], ["main", "", function () { toggle() }], ["fwd", "Forward 10 s", function () { seekBy(10) }], ["sep"],
  ["lg", "LG player \u00b7 best picture", function () { toLG() }], ["pic", "TV picture mode", function () { tvPicture(true) }], ["cc", "Subtitles", function () { openSheet("subs") }], ["snd", "Sound", function () { openSheet("snd") }], ["hd", "Quality", function () { openSheet("q") }], ["info", "Info", function () { openSheet("info") }]];
if (!ONTV) { BTNS = BTNS.filter(function (b) { return b[0] != "lg" && b[0] != "pic" }); document.body.classList.add("mac") }
var MAINI = 0; for (var bi = 0; bi < BTNS.length; bi++) if (BTNS[bi][0] == "main") MAINI = bi;  // the play button
function render() {
  var h = "";
  BTNS.forEach(function (b, i) {
    if (b[0] == "sep") { h += "<span class=sep></span>"; return }
    var f = S.area == "ctl" && S.ci == i ? " f" : "", val = "";
    if (b[0] == "cc") val = SUBS.sid ? "On" : "Off";
    if (b[0] == "snd") val = SN[SUBS.snd || "original"];
    if (b[0] == "pic") val = S.pic ? modeName(S.pic) + " \u00b7 OK to change" : "OK to open";
    if (b[0] == "hd") val = (S.auto ? "Auto · " : "") + QN[S.Q] + (S.Q == "orig" ? " (untouched)" : "");
    var ic = b[0] == "main" ? (v.paused || !S.started ? IC.play : IC.pause) : b[0] == "hd" ? qBadge() : b[0] == "snd" ? sBadge() : b[0] == "lg" ? badge("LG", "BEST") :
      b[0] == "pic" ? badge(/^(hdr|dolby)/i.test(S.pic || "") ? (/^dolby/i.test(S.pic) ? "DV" : "HDR") : "SDR", (modeName(S.pic).replace(/^(HDR|Dolby Vision) /, "").split(" ")[0] || "MODE").toUpperCase().slice(0, 8)) : IC[b[0]];
    var lb = b[0] == "main" ? (v.paused || !S.started ? "Play" : "Pause") : b[1];
    h += "<button class='b" + (b[0] == "main" ? " main" : "") + f + "' data-i=" + i + ">" + ic + (b[0] == "cc" && SUBS.sid ? "<i class=dot></i>" : "") +
      "<span class=lb>" + lb + (val ? "<em>" + val + "</em>" : "") + "</span></button>";
  });
  $("ctl").innerHTML = h;
  $("track").className = "track" + (S.area == "track" ? " f" : "");
  document.body.classList.toggle("paused", S.started && v.paused);
}
function dupOk() { return Date.now() - (S.kOk || 0) < 450 }  // this click is the same press as the OK key just handled
$("ctl").addEventListener("click", function (e) {
  var t = e.target; while (t && !(t.getAttribute && t.getAttribute("data-i"))) t = t.parentNode;
  if (!t) return;
  e.stopPropagation();
  if (dupOk()) return;
  S.area = "ctl"; S.ci = +t.getAttribute("data-i"); BTNS[S.ci][2](); render();
});
$("ctl").addEventListener("mouseover", function (e) {  // pointer and OK always mean the same button
  var t = e.target; while (t && !(t.getAttribute && t.getAttribute("data-i"))) t = t.parentNode;
  if (t && (S.area != "ctl" || S.ci != +t.getAttribute("data-i"))) { S.area = "ctl"; S.ci = +t.getAttribute("data-i"); render() }
});
// ---------- playback
var HLSOK = !!v.canPlayType("application/vnd.apple.mpegurl") && !/[?&]hls=0/.test(location.search);
function qs(t) { return "t=" + t.toFixed(1) + "&ch=" + S.CH + "&snd=" + (SUBS.snd || "original") + "&q=" + S.Q }
function src(t) { return C.num + ".mp4?" + qs(t) }
function load(t) {
  if (C.dur) t = Math.max(0, Math.min(t, C.dur - 3));
  S.t0 = Math.max(0, t);
  S.live = false; clearTimeout(S.stallT); clearInterval(S.waitT);  // a fresh stream: its first buffering is not a stall
  spin(true, "Loading", "");
  var go = function (u) { v.src = u; var p = v.play(); if (p && p["catch"]) p["catch"](function () {}) };
  S.off = 0;
  if (!HLSOK) return go(src(S.t0));
  var my = S.loadId = (S.loadId || 0) + 1, x = new XMLHttpRequest();
  x.open("GET", C.num + "-hls?" + qs(S.t0), true);
  x.onload = function () {
    if (my != S.loadId) return;  // a newer seek replaced this one
    var r = {}; try { r = JSON.parse(x.responseText) } catch (e) {}
    if (r.url) { S.off = r.off || 0; S.conv = !!r.converted; if (r.t0 != null) S.t0 = r.t0; render(); go(r.url) } else spin(true, "The Mac could not start the stream", r.error || "Press OK to try again");
  };
  x.onerror = function () { if (my == S.loadId) spin(true, "Cannot reach the Mac", "Is dlus running? Press OK to try again") };
  x.send();
}
function isFull() { return !!(document.fullscreenElement || document.webkitFullscreenElement || document.webkitCurrentFullScreenElement || document.webkitIsFullScreen) }
function goFull() {  // the browser only allows this right after a key press or click
  if (isFull()) return;
  var el = document.documentElement, f = el.requestFullscreen || el.webkitRequestFullscreen || el.webkitRequestFullScreen || el.mozRequestFullScreen;
  if (!f) return;
  var quiet = function (r) { if (r && r["catch"]) r["catch"](function () {}) };
  try { var r = f.call(el, { navigationUI: "hide" }); if (r && r["catch"]) r["catch"](function () { try { quiet(f.call(el)) } catch (e) {} }) } catch (e) { try { quiet(f.call(el)) } catch (e2) {} }
}
document.addEventListener("keyup", goFull);
document.addEventListener("mousedown", goFull);
// TV browsers collapse their address bar when the page scrolls: give the page 1 px to scroll and use it
document.documentElement.style.minHeight = "calc(100% + 2px)";
function hideBar() { try { window.scrollTo(0, 1) } catch (e) {} }
setTimeout(hideBar, 300); setTimeout(hideBar, 1500); document.addEventListener("keydown", hideBar);
function start(t) { if (S.started && $("dlg").className != "on") return; S.started = true; $("dlg").className = ""; goFull(); load(t); showUI(true) }
function toggle() { if (!S.started) return start(resumeAt()); if (v.paused) { v.play(); big("play") } else { v.pause(); big("pause") } render(); showUI() }
function thumb(t) {  // seek preview: a real frame from the movie, rendered by the Mac
  var img = $("pimg"), ok = !C.dur || t / C.dur <= S.ready - 0.003;
  img.className = "img" + (ok ? " load" : " nodl"); img.style.backgroundImage = "";
  clearTimeout(S.thumbT); if (!ok) return;
  S.thumbT = setTimeout(function () {
    var u = C.num + "-thumb?t=" + Math.round(t / 10) * 10, im = new Image();
    im.onload = function () { if (Math.abs((S.seekTo >= 0 ? S.seekTo : now()) - t) < 300) { img.style.backgroundImage = "url('" + u + "')"; img.className = "img" } };
    im.onerror = function () { img.className = "img" }; im.src = u;
  }, 250);
}
function seekBy(d) {
  if (!S.started) return;
  if (S.seekTo < 0) S.seekTo = now();
  S.seekTo = Math.max(0, Math.min(C.dur ? C.dur - 3 : 1e9, S.seekTo + d));
  var h = $("seekhint"); h.innerHTML = (d > 0 ? IC.fwd : IC.back) + fmt(S.seekTo); document.body.classList.add("seeking");
  var p = $("prev"); p.className = "glass on"; p.style.left = (C.dur ? Math.min(92, Math.max(8, S.seekTo / C.dur * 100)) : 0) + "%";
  $("ptime").textContent = fmt(S.seekTo); var dd = S.seekTo - now(); $("pdelta").textContent = (dd >= 0 ? "+" : "−") + fmt(Math.abs(dd));
  thumb(S.seekTo);
  clearTimeout(S.seekTimer); S.seekTimer = setTimeout(function () { var t = S.seekTo; S.seekTo = -1; document.body.classList.remove("seeking"); p.className = "glass"; load(t) }, 1100);
  showUI();
}
function resumeAt() { var r = parseFloat(localStorage.getItem(KEY) || "0"); return r > 60 && (!C.dur || r < C.dur - 120) ? r - 5 : 0 }
function setQ(q, auto) { S.Q = q; S.auto = !!auto; localStorage.setItem(QKEY, auto ? "auto" : q); if (S.started) load(now()); render() }
v.addEventListener("playing", function () { spin(false); S.errs = 0; S.live = true; S.liveAt = Date.now(); clearTimeout(S.stallT); render() });
function atEdge() { return C.dur && S.ready < 1 && now() / C.dur > S.ready - 0.005 }  // playing where the download has not got to yet
v.addEventListener("pause", render); v.addEventListener("play", render);
var EVQ = [], EV0 = Date.now();
["loadstart", "loadedmetadata", "canplay", "playing", "waiting", "stalled", "ended", "error", "emptied", "abort", "seeking", "suspend"].forEach(function (n) {
  v.addEventListener(n, function () {
    var b = []; for (var i = 0; i < v.buffered.length; i++) b.push(v.buffered.start(i).toFixed(1) + "-" + v.buffered.end(i).toFixed(1));
    EVQ.push(((Date.now() - EV0) / 1000).toFixed(1) + " " + n + " ct=" + v.currentTime.toFixed(1) + " rs=" + v.readyState + " ns=" + v.networkState + " buf=" + b.join(",") +
      (n == "error" && v.error ? " code=" + v.error.code + ":" + (v.error.message || "") : "") + (n == "loadstart" ? " t0=" + S.t0.toFixed(0) + " q=" + S.Q : ""));
    if (EVQ.length > 40) EVQ.shift();
  });
});
v.addEventListener("waiting", function () {
  if (!S.started) return;
  if (atEdge()) { spin(true, "Waiting for the download", "It plays on as soon as more of the movie is downloaded"); return }
  spin(true, "Buffering", "");
  // only a real stall counts: the stream was already playing for 5 s and it is still stuck 3 s later
  if (!S.live || Date.now() - S.liveAt < 5000) return;
  clearTimeout(S.stallT);
  S.stallT = setTimeout(function () {
    if (!v.paused && v.readyState >= 3) return;
    var t = Date.now(); S.stalls = S.stalls.filter(function (x) { return t - x < 180000 }); S.stalls.push(t);
    var steps = QORDER.filter(function (x) { return x != "enhance" });
    if (S.auto && S.stalls.length >= 3 && t - (S.switchAt || 0) > 180000 && steps.indexOf(S.Q) >= 0 && steps.indexOf(S.Q) < steps.length - 1) {
      var nq = steps[steps.indexOf(S.Q) + 1]; S.stalls = []; S.switchAt = t;
      toast("Your Wi-Fi is slow - switching to " + QN[nq] + " (choose a fixed quality in Quality)");
      S.Q = nq; load(now()); render();
    }
  }, 3000);
});
v.addEventListener("ended", function () {
  if (!C.dur || now() >= C.dur - 10) return;
  if (atEdge()) {  // the stream stopped at the end of what is downloaded: wait for more, do not hammer the Mac
    spin(true, "Waiting for the download", "It plays on as soon as more of the movie is downloaded");
    clearInterval(S.waitT); var at = now();
    S.waitT = setInterval(function () { if (S.ready >= 1 || S.ready * C.dur > at + 60) { clearInterval(S.waitT); load(at) } }, 3000);
    return;
  }
  var t = Date.now(); S.ends = (S.ends || []).filter(function (x) { return t - x < 60000 }); S.ends.push(t);
  if (S.ends.length > 3) return spin(true, "The stream keeps stopping", "Check that dlus is running on the Mac, then press OK to try again");
  setTimeout(function () { load(now()) }, 1000);
});
v.addEventListener("error", function () {
  S.errs++;
  if (S.errs <= 2 && S.CH > 2) { S.CH = 2; load(now()); return }
  if (S.errs <= 3 && S.Q == "orig") { S.Q = "saver"; S.auto = true; toast("Switching to a TV-friendly quality"); load(now()); return }
  spin(true, "This TV cannot play this video", "Try Quality → 1080p, or the TV's own player from dlus on the Mac");
});
// ---------- side sheets
function head(icon, title, sub) { return "<div class=sh><i>" + IC[icon] + "</i><h2>" + title + "</h2></div><div class=sub>" + sub + "</div>" }
function openSheet(name) {
  S.sheet = name; S.si = 0; var sh = $("sheet");
  if (name == "subs") {
    sh.innerHTML = head("cc", "Subtitles", "Font, colour, size and sync — ▲▼ choose, ◀▶ change") + "<div id=sprev><span id=sprevt dir=auto>سلام، این زیرنویس شماست</span></div><div id=smenu class=smenu></div>";
    SUBM.el = $("smenu"); SUBM.row = 0; subMenuRender(); sprev();
  } else if (name == "find") {
    sh.innerHTML = head("cc", "Find subtitles", "Searching subf2m and OpenSubtitles...") + "<div id=opts></div>";
    var x = new XMLHttpRequest(); x.open("GET", C.num + "-subsearch?lang=fa", true);
    x.onload = function () { var r = []; try { r = JSON.parse(x.responseText).results || [] } catch (e) {} S.found = r.slice(0, 12);
      sh.querySelector(".sub").textContent = r.length ? "Best match first — OK adds and shows it" : "Nothing found"; listRender() };
    x.send();
  } else if (name == "snd") sh.innerHTML = head("snd", "Sound", "For TV speakers choose Clear dialogue") + "<div id=opts></div>";
  else if (name == "q") sh.innerHTML = head("hd", "Quality", "Lower is smoother on slow Wi-Fi. HDR is kept in every mode.") + "<div id=opts></div>";
  else if (name == "info") {
    var orig = S.Q == "orig", so = sndOut(SUBS.snd || "original");
    var rows = [["Picture", orig && S.conv ? (C.vcodec || "").toUpperCase() + " cannot play in the TV browser: converted near-lossless to HEVC \u00b7 " + resName("orig") + " (LG player plays it untouched)" : orig ? "Original, untouched \u00b7 " + resName("orig") + " " + (C.vcodec || "").toUpperCase() + " 10-bit" + (C.hdr ? " \u00b7 HDR10" : "")
                   : resName(S.Q) + " re-encoded by the Mac \u00b7 " + C.qual[S.Q] + (C.hdr ? " \u00b7 HDR10 kept" : "")],
      ["Quality setting", S.auto ? "Auto (steps down only if the Wi-Fi cannot keep up)" : "Fixed"],
      ["Sound", so[2] + (srcAudio() ? " \u00b7 from " + srcAudio() : "")],
      ["TV picture mode", S.pic ? modeName(S.pic) : "\u2014"], ["Sound mode", SN[SUBS.snd || "original"] + ((SUBS.snd || "original") == "original" ? " \u2014 the film's own mix, no remix or level changes" : "")],
      ["Subtitle", SUBS.sid ? (SUBS.tracks.filter(function (t) { return t.sid == SUBS.sid })[0] || {}).label || "on" : "off"],
      ["Downloaded", Math.round(S.ready * 100) + "% \u00b7 " + fmt(S.ready * C.dur) + " of " + fmt(C.dur)], ["From", "dlus on " + C.host]];
    sh.innerHTML = head("info", "Info", subEsc(C.title)) + "<div class=info>" + rows.map(function (r) { return "<div><span>" + r[0] + "</span><b dir=auto>" + subEsc(r[1]) + "</b></div>" }).join("") + "</div>";
  }
  sh.className = "glass on"; listRender(); showUI();
}
function sprev() { var p = $("sprevt"); if (!p) return; p.style.fontFamily = (SUBS.font && SUBS.font != "system" ? "'" + SUBS.font + "'," : "") + "sans-serif"; p.style.color = SUBS.color; p.style.fontWeight = SUBS.bold ? 700 : 400;
  p.style.fontSize = Math.round(30 * SUBS.size / 5.2) + "px"; p.style.background = SUBS.bg == "box" ? "rgba(0,0,0,.82)" : SUBS.bg == "soft" ? "rgba(0,0,0,.42)" : "none"; p.style.padding = "2px 10px"; p.style.borderRadius = "6px";
  p.style.textShadow = SUBS.bg == "shadow" ? "0 0 4px #000,0 0 6px #000" : "none" }
function opts() {
  if (S.sheet == "snd") return [["original", "Original sound", C.ach > 2 ? "the film's own mix, best quality (Dolby Digital Plus 5.1)" : "the film's own stereo, untouched mix"], ["dialog", "Clear dialogue", "voices louder — best for TV speakers"], ["night", "Night mode", "loud and quiet parts evened out"], ["surround", "5.1 surround", "for a soundbar or home theater"]].filter(function (o) { return C.ach > 2 || o[0] == "original" || o[0] == "night" }).map(function (o) { return o.concat([SUBS.snd == o[0]]) });
  if (S.sheet == "q") return [["auto", "Auto", "the original, stepping down only if the Wi-Fi cannot keep up"]].concat(QORDER.map(function (q) {
    if (q == "enhance") return ["enhance", "Enhanced", "low-resolution video: cleaned up and upscaled to 1080p by the Mac"]; return [q, QN[q], (((C.qual[q] || "").match(/\(([^)]*)\)/) || [])[1] || "").replace("~", "about ")] }))
    .map(function (o) { return o.concat([o[0] == "auto" ? S.auto : !S.auto && S.Q == o[0]]) });
  if (S.sheet == "find") return (S.found || []).map(function (r) { return [r, (r.best ? "★ " : "") + r.score + "% match · " + (r.by || r.src), (r.release || "") + " · " + r.src, false] });
  return [];
}
function listRender() {
  var e = $("opts"); if (!e) return;
  e.innerHTML = opts().map(function (o, i) { return "<div class='opt" + (i == S.si ? " f" : "") + (o[3] ? " sel" : "") + "' data-i=" + i + "><span class=chk></span><div><b>" + subEsc(o[1]) + "</b><span class=d dir=auto>" + subEsc(o[2]) + "</span></div></div>" }).join("");
}
function pick(i) {
  var o = opts()[i]; if (!o) return;
  if (S.sheet == "snd") { SUBS.snd = o[0]; SUBM.onSave(); var xs = new XMLHttpRequest(); xs.open("GET", C.num + "-subset?snd=" + o[0], true); xs.send();
    if (S.started) load(now()); toast("Sound — " + o[1]); listRender(); render(); return }
  if (S.sheet == "q") { if (o[0] == "auto") setQ("orig", true); else setQ(o[0], false); toast("Quality — " + o[1]); listRender(); return }
  if (S.sheet == "find") {
    var r = o[0]; toast("Adding subtitle...");
    var x = new XMLHttpRequest();
    x.open("GET", C.num + "-subget?path=" + encodeURIComponent(r.path) + "&id=" + encodeURIComponent(r.id) + "&name=" + encodeURIComponent(r.name || "") + "&label=" + encodeURIComponent("Persian · " + (r.by || r.src) + " · " + r.score + "%"), true);
    x.onload = function () { var j = {}; try { j = JSON.parse(x.responseText) } catch (e) {} if (j.subs) { subSetup(j.subs); SUBM.onSave(); toast("Subtitle added"); closeSheet() } else toast("Could not add: " + (j.error || "error")) };
    x.send();
  }
}
$("sheet").addEventListener("click", function (e) {
  var t = e.target; while (t && t != this && !(t.getAttribute && t.getAttribute("data-i")) && !(t.className && /smr/.test(t.className))) t = t.parentNode;
  if (!t || t == this) return;
  if (/smr/.test(t.className)) { subMenuClick(e); sprev(); return }
  S.si = +t.getAttribute("data-i"); pick(S.si); listRender();
});
function closeSheet() { S.sheet = null; $("sheet").className = "glass"; render(); showUI() }
// ---------- keys (remote), pointer, BACK
function key(k) {
  if ($("dlg").className == "on") { var bs = $("dlgb").children; if (k == 37 || k == 39) { S.di = 1 - (S.di || 0); bs[0].className = "pill" + (S.di ? "" : " f"); bs[1].className = "pill" + (S.di ? " f" : "") } else if (k == 13) S.dlgAct[S.di || 0](); return true }
  if (S.sheet) {
    if (S.sheet == "subs") { if (subMenuKey(k)) { sprev(); return true } return k == 13 }
    var n = opts().length;
    if (k == 38) S.si = Math.max(0, S.si - 1); else if (k == 40) S.si = Math.min(n - 1, S.si + 1); else if (k == 13) pick(S.si);
    else if (k == 37) { closeSheet(); return true } else return false;
    listRender(); return true;
  }
  if (k == 403 || k == 67) { openSheet("subs"); return true }
  if (k == 404) { subChange(2, 1); toast("Subtitle size " + Math.round(SUBS.size / 5.2 * 100) + "%"); return true }
  if (k == 405 || k == 406) { subChange(6, k == 405 ? -1 : 1); toast("Subtitle sync " + (SUBS.delay > 0 ? "+" : "") + SUBS.delay.toFixed(1) + " s"); return true }
  if (k == 415 || k == 19 || k == 179 || (k == 32 && !ONTV)) { toggle(); return true }
  if (k == 412) { seekBy(-30); return true } if (k == 417) { seekBy(30); return true }
  if (!S.started) { if (k == 13) start(resumeAt()); return k == 13 }
  if (S.area == "none") {
    if (k == 13) { toggle(); return true }
    if (k == 37 || k == 39) { seekBy(k == 37 ? -10 : 10); return true }
    if (k == 38 || k == 40) { showUI(true); return true }
    return false;
  }
  if (S.area == "track") {
    if (k == 37 || k == 39) { seekBy(k == 37 ? -30 : 30); return true }
    if (k == 40 || k == 13) { S.area = "ctl"; render(); showUI(); return true }
    if (k == 38) { hideNow(); return true }
    return false;
  }
  if (k == 37 || k == 39) { do { S.ci = (S.ci + (k == 39 ? 1 : -1) + BTNS.length) % BTNS.length } while (BTNS[S.ci][0] == "sep"); render(); showUI(); return true }
  if (k == 13) { BTNS[S.ci][2](); render(); return true }
  if (k == 38) { S.area = "track"; render(); showUI(); return true }
  if (k == 40) { hideNow(); return true }
  return false;
}
function hideNow() { S.area = "none"; document.body.classList.add("hide"); render() }
document.addEventListener("keydown", function (e) {
  var k = e.keyCode; goFull();
  if (k == 13) S.kOk = Date.now();
  if (k == 461 || k == 8 || k == 27) { e.preventDefault(); S.kb = Date.now(); return back() }
  if (key(k)) e.preventDefault(); else showUI();
});
function leave() {  // Mac: close the player window / tab (it was opened from dlus), else go back
  v.pause(); try { v.removeAttribute("src"); v.load() } catch (e) {}
  if (window.opener) { window.close(); return }
  history.length > 1 ? history.back() : (location.href = "about:blank");
}
function back() {
  if ($("dlg").className == "on") { $("dlg").className = ""; return }
  if (S.sheet) { closeSheet(); return }
  if (!ONTV) return leave();
  if (S.area != "none" && S.started) { hideNow(); return }
  if (Date.now() - S.backT < 2500) { history.go(-2); return }
  S.backT = Date.now(); toast("Press BACK again to leave the player");
}
history.pushState({ dlus: 1 }, ""); window.addEventListener("popstate", function () { history.pushState({ dlus: 1 }, ""); if (Date.now() - (S.kb || 0) > 400) back() });
document.addEventListener("mousemove", function () { document.body.classList.add("ptr"); showUI() });
document.addEventListener("click", function () { if (S.sheet) { closeSheet(); return } if ($("dlg").className == "on" || dupOk()) return; if (!S.started) return start(resumeAt()); if (document.body.classList.contains("hide")) showUI(true); else toggle() });
$("track").addEventListener("click", function (e) { e.stopPropagation(); var r = this.getBoundingClientRect(); if (C.dur) { S.seekTo = -1; seekBy((e.clientX - r.left) / r.width * C.dur - now()) } });
// ---------- subtitles, remembered on this TV
SUBS.el = $("subs"); SUBS.box = document.body; SUBS.urlFor = function (sid) { return C.num + "-" + sid + ".vtt" };
SUBS.note = function (t) { if (t) toast(t) }; SUBM.el = null;
SUBM.extra = "▲▼ choose &nbsp; ◀▶ change &nbsp; BACK close";
SUBM.onSave = function () {
  sprev();
  var look = JSON.stringify({ font: SUBS.font, size: SUBS.size, color: SUBS.color, bg: SUBS.bg, pos: SUBS.pos, bold: SUBS.bold });
  if (look != S.subLook) {  // the look is one preset for all movies, kept on the Mac
    S.subLook = look;
    var xl = new XMLHttpRequest(); xl.open("GET", C.num + "-subset?style=" + encodeURIComponent(look), true); xl.send();
  }
  if (SUBS.sid !== S.subSid || SUBS.delay !== S.subDelay || (SUBS.scale || 1) !== S.subScale) {  // track / sync belong to the movie: save them on the Mac
    S.subSid = SUBS.sid; S.subDelay = SUBS.delay; S.subScale = SUBS.scale || 1;
    var x = new XMLHttpRequest(); x.open("GET", C.num + "-subset?sid=" + encodeURIComponent(SUBS.sid || "") + "&delay=" + SUBS.delay + "&scale=" + (SUBS.scale || 1), true); x.send();
  }
};
SUBS.sounds = null; SUBS.snd = C.snd || "original"; SUBS.onFind = function () { openSheet("find") };
SUBS.onAutoSync = function () { subAutoSync(C.num + "-autosync?sid=" + encodeURIComponent(SUBS.sid || "")) };
(function () {
  var st = C.subs || {}, saved = null;
  try { saved = JSON.parse(localStorage.getItem(SKEY) || "null") } catch (e) {}
  subSetup(st);  // look: the preset on the Mac; track and sync: as saved on the Mac for this movie
  S.subSid = SUBS.sid; S.subDelay = SUBS.delay; S.subScale = SUBS.scale || 1;
  S.subLook = JSON.stringify({ font: SUBS.font, size: SUBS.size, color: SUBS.color, bg: SUBS.bg, pos: SUBS.pos, bold: SUBS.bold });
  var q = localStorage.getItem(QKEY) || "auto"; if (q != "auto" && QORDER.indexOf(q) < 0) q = "auto"; S.auto = q == "auto"; S.Q = S.auto ? "orig" : q;
})();
window.addEventListener("resize", subStyle);
// ---------- loops
(function loop() { subRender(now()); (window.requestAnimationFrame || function (f) { setTimeout(f, 100) })(loop) })();
setInterval(function () {
  var c = now(), D = C.dur || 0, d = new Date();
  $("cur").textContent = fmt(S.seekTo >= 0 ? S.seekTo : c); $("rem").textContent = "−" + fmt(D - c);
  $("played").style.width = (D ? Math.min(100, c / D * 100) : 0) + "%"; $("knob").style.left = (D ? Math.min(100, (S.seekTo >= 0 ? S.seekTo : c) / D * 100) : 0) + "%";
  if (S.started && !v.paused && c > 5) localStorage.setItem(KEY, String(c));
  $("clk").textContent = hm(d);
  var ends = D ? new Date(d.getTime() + (D - c) * 1000) : null;
  $("ends").textContent = ends && S.started ? "ends at " + hm(ends) : "";
  $("pinfo").innerHTML = "<b>" + fmt(D - c) + "</b> left" + (ends ? " · ends at <b>" + hm(ends) + "</b>" : "");
}, 400);
function poll() {
  var x = new XMLHttpRequest(); x.open("GET", C.num + ".json?" + (+new Date()), true);
  x.onload = function () { try { var i = JSON.parse(x.responseText); S.ready = i.size ? (i.status == "done" ? 1 : i.prefix / i.size) : 0 } catch (e) {} $("ready").style.width = (S.ready * 100).toFixed(2) + "%" };
  x.send();
}
poll(); setInterval(poll, 5000);
function cmds() {
  var x = new XMLHttpRequest();
  var ev = EVQ.length ? "&ev=" + encodeURIComponent(EVQ.join(" | ")) : ""; EVQ = [];
  x.open("GET", "cmd?since=" + S.seq + "&num=" + C.num + "&t=" + now().toFixed(1) + "&dur=" + (C.dur || 0) + "&p=" + (S.started && !v.paused ? 1 : 0) + "&q=" + S.Q + "&ss=" + encodeURIComponent(SUBS.sid || "") + "&sd=" + SUBS.delay + "&fs=" + (isFull() ? 1 : 0) + "&vh=" + window.innerHeight + "x" + screen.height + ev, true);
  x.onload = function () {
    var r = {}; try { r = JSON.parse(x.responseText) } catch (e) {}
    if (S.seq === 0) { S.seq = r.seq || 0; return }
    (r.cmds || []).forEach(function (c) {
      S.seq = Math.max(S.seq, c.seq);
      if (c.seek) return seekBy(c.seek);
      var map = { UP: 38, DOWN: 40, LEFT: 37, RIGHT: 39, ENTER: 13, BACK: 461, RED: 403, GREEN: 404, YELLOW: 405, BLUE: 406, PLAY: 415, PAUSE: 19, REWIND: 412, FASTFORWARD: 417, STOP: 19, INFO: 457 };
      var k = map[c.key]; if (k == 461) back(); else if (k == 457) openSheet("info"); else if (k) { if (!key(k)) showUI() }
    });
    S.seq = Math.max(S.seq, r.seq || 0);
  };
  x.send();
}
setInterval(cmds, 700); cmds();
if (ONTV) { tvPicture(false); setInterval(function () { tvPicture(false) }, 20000) }
// ---------- title, chips, start
$("title").textContent = C.pretty || C.title; $("ptitle").textContent = C.pretty || C.title;
var ch = [];
if (C.width >= 3800) ch.push(["4K", ""]); else if (C.width >= 1900) ch.push(["1080p", ""]);
if (C.hdr) ch.push(["HDR10", " hdr"]); if (srcAudio()) ch.push([subEsc(srcAudio()), ""]); if (SUBS.sid) ch.push(["FA", ""]);
$("chips").innerHTML = ch.map(function (c) { return "<span class='chip" + c[1] + "'>" + c[0] + "</span>" }).join("") + (C.year ? "<span class='chip dim'>" + C.year + "</span>" : "") + (C.dur ? "<span class='chip dim'>" + fmt(C.dur) + "</span>" : "");
render();
var r0 = resumeAt(), qt = (location.search.match(/[?&]t=([0-9.]+)/) || [])[1];
if (C.ffmpeg && C.dur) v.poster = C.num + "-thumb?t=" + Math.round(qt ? +qt : r0 || Math.min(600, C.dur * 0.1));  // a real frame while it loads
function ask() {
  $("dlgt").textContent = C.pretty || C.title; $("dlgp").textContent = "You stopped at " + fmt(r0 + 5) + " · " + fmt(C.dur - r0) + " left";
  $("dlgb").innerHTML = "<button class='pill f'>" + IC.play + "<span>Continue at " + fmt(r0) + "</span></button><button class=pill>" + IC.restart + "<span>Start over</span></button>";
  var bs = $("dlgb").children;
  var pickD = function (i) { S.di = i; bs[0].className = "pill" + (i ? "" : " f"); bs[1].className = "pill" + (i ? " f" : "") };
  S.dlgAct = [function () { start(r0) }, function () { start(0) }];  // OK runs these directly; pointer clicks too
  bs[0].onclick = function (e) { e.stopPropagation(); if (!dupOk()) S.dlgAct[0]() };
  bs[1].onclick = function (e) { e.stopPropagation(); if (!dupOk()) S.dlgAct[1]() };
  bs[0].onmouseover = function () { pickD(0) }; bs[1].onmouseover = function () { pickD(1) };
  S.di = 0; $("dlg").className = "on";
}
if (/[?&]autoplay=1/.test(location.search)) { if (qt) start(+qt); else if (r0) ask(); else start(0) }
else if (r0) ask(); else { showUI(true); spin(false) }
</script></body></html>
"""


def soap(action, svc, args):
    inner = "".join("<%s>%s</%s>" % (k, xesc(str(v)), k) for k, v in args)
    return ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%sResponse '
            'xmlns:u="urn:schemas-upnp-org:service:%s:1">%s</u:%sResponse></s:Body></s:Envelope>' % (action, svc, inner, action))


DIDL = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">%s</DIDL-Lite>')


def didl_item(x):
    dur = x.get("dur") or 0
    attrs = (' size="%d"' % x["size"] if x["size"] else "") + (' duration="%d:%02d:%06.3f"' % (dur // 3600, dur % 3600 // 60, dur % 60) if dur else "")
    return ('<item id="%s" parentID="0" restricted="1"><dc:title>%s</dc:title><upnp:class>%s</upnp:class>'
            '<res protocolInfo="http-get:*:%s:%s"%s>%s</res></item>'
            % (xesc(x["id"]), xesc(x["title"]), x["cls"], x["mime"], x["flags"], attrs, xesc(x["url"])))


def dlna_parent(oid):
    """container that holds a DLNA object id"""
    if oid in ("dl",) or re.match(r"^f\d+$", oid):
        return "0"
    mm = re.match(r"^(f\d+):(.*)$", oid)
    if mm:
        rel = urllib.parse.unquote(mm.group(2))
        return mm.group(1) if "/" not in rel else "%s:%s" % (mm.group(1), urllib.parse.quote(rel.rsplit("/", 1)[0]))
    return "dl"  # downloads (and their -c / -t variants)


def didl_any(x, parent):
    if x.get("container"):
        return ('<container id="%s" parentID="%s" restricted="1" childCount="%d"><dc:title>%s</dc:title>'
                '<upnp:class>object.container.storageFolder</upnp:class></container>' % (xesc(x["id"]), xesc(parent), x.get("count", 0), xesc(x["title"])))
    return didl_item(x).replace('parentID="0"', 'parentID="%s"' % xesc(parent), 1)


def dlna_cds(m, action, body):
    def arg(tag):
        mm = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), body, re.S)
        return mm.group(1).strip() if mm else ""

    if action == "Browse":
        oid, flag = arg("ObjectID") or "0", arg("BrowseFlag")
        start, count = int(arg("StartingIndex") or 0), int(arg("RequestedCount") or 0)
        if flag == "BrowseMetadata":
            if oid == "0":
                res, n, total = ('<container id="0" parentID="-1" restricted="1" childCount="%d"><dc:title>%s</dc:title>'
                                 '<upnp:class>object.container.storageFolder</upnp:class></container>'
                                 % (len(m.dlna_folder("0")), xesc(m.friendly()))), 1, 1
            else:
                parent = dlna_parent(oid)
                x = next((i for i in m.dlna_folder(parent) if i["id"] == oid), None)
                res, n, total = (didl_any(x, parent), 1, 1) if x else ("", 0, 0)
        else:
            items = m.dlna_folder(oid)
            sel = items[start:start + count] if count else items[start:]
            res, n, total = "".join(didl_any(x, oid) for x in sel), len(sel), len(items)
        return soap("Browse", "ContentDirectory", [("Result", DIDL % res), ("NumberReturned", n),
                                                    ("TotalMatches", total), ("UpdateID", m.dlna_update)])
    if action == "GetSystemUpdateID":
        return soap(action, "ContentDirectory", [("Id", m.dlna_update)])
    if action == "GetSearchCapabilities":
        return soap(action, "ContentDirectory", [("SearchCaps", "")])
    if action == "GetSortCapabilities":
        return soap(action, "ContentDirectory", [("SortCaps", "")])
    return None


def dlna_cms(action):
    if action == "GetProtocolInfo":
        src = ",".join("http-get:*:%s:*" % t for t in sorted(set(MIME.values())))
        return soap(action, "ConnectionManager", [("Source", src), ("Sink", "")])
    if action == "GetCurrentConnectionIDs":
        return soap(action, "ConnectionManager", [("ConnectionIDs", "0")])
    if action == "GetCurrentConnectionInfo":
        return soap(action, "ConnectionManager", [("RcsID", "-1"), ("AVTransportID", "-1"), ("ProtocolInfo", ""),
                                                   ("PeerConnectionManager", ""), ("PeerConnectionID", "-1"),
                                                   ("Direction", "Output"), ("Status", "OK")])
    return None


def dlna_desc(m):
    svc = ('<service><serviceType>urn:schemas-upnp-org:service:%s:1</serviceType><serviceId>urn:upnp-org:serviceId:%s</serviceId>'
           '<SCPDURL>/dlna/%s.xml</SCPDURL><controlURL>/dlna/%s/ctl</controlURL><eventSubURL>/dlna/%s/evt</eventSubURL></service>')
    return ('<?xml version="1.0" encoding="utf-8"?><root xmlns="urn:schemas-upnp-org:device-1-0" '
            'xmlns:dlna="urn:schemas-dlna-org:device-1-0"><specVersion><major>1</major><minor>0</minor></specVersion><device>'
            '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType><friendlyName>%s</friendlyName>'
            '<manufacturer>dlus</manufacturer><modelName>dlus</modelName><modelNumber>%s</modelNumber><UDN>uuid:%s</UDN>'
            '<dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC><serviceList>%s%s</serviceList></device></root>'
            % (xesc(m.friendly()), VERSION, m.settings["dlna_uuid"],
               svc % (("ContentDirectory",) * 2 + ("cds",) * 3), svc % (("ConnectionManager",) * 2 + ("cms",) * 3)))


def scpd(actions, variables):
    acts = "".join("<action><name>%s</name><argumentList>%s</argumentList></action>" % (
        n, "".join("<argument><name>%s</name><direction>%s</direction><relatedStateVariable>%s</relatedStateVariable></argument>"
                   % a for a in args)) for n, args in actions)
    var = "".join('<stateVariable sendEvents="%s"><name>%s</name><dataType>%s</dataType></stateVariable>' % v for v in variables)
    return ('<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0"><specVersion><major>1</major>'
            '<minor>0</minor></specVersion><actionList>%s</actionList><serviceStateTable>%s</serviceStateTable></scpd>' % (acts, var))


CDS_SCPD = scpd(
    [("Browse", [("ObjectID", "in", "A_ARG_TYPE_ObjectID"), ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
                 ("Filter", "in", "A_ARG_TYPE_Filter"), ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                 ("RequestedCount", "in", "A_ARG_TYPE_Count"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                 ("Result", "out", "A_ARG_TYPE_Result"), ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                 ("TotalMatches", "out", "A_ARG_TYPE_Count"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID")]),
     ("GetSearchCapabilities", [("SearchCaps", "out", "SearchCapabilities")]),
     ("GetSortCapabilities", [("SortCaps", "out", "SortCapabilities")]),
     ("GetSystemUpdateID", [("Id", "out", "SystemUpdateID")])],
    [("no", "A_ARG_TYPE_ObjectID", "string"), ("no", "A_ARG_TYPE_BrowseFlag", "string"), ("no", "A_ARG_TYPE_Filter", "string"),
     ("no", "A_ARG_TYPE_Index", "ui4"), ("no", "A_ARG_TYPE_Count", "ui4"), ("no", "A_ARG_TYPE_SortCriteria", "string"),
     ("no", "A_ARG_TYPE_Result", "string"), ("no", "A_ARG_TYPE_UpdateID", "ui4"), ("no", "SearchCapabilities", "string"),
     ("no", "SortCapabilities", "string"), ("yes", "SystemUpdateID", "ui4")])
CMS_SCPD = scpd(
    [("GetProtocolInfo", [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")]),
     ("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "CurrentConnectionIDs")])],
    [("yes", "SourceProtocolInfo", "string"), ("yes", "SinkProtocolInfo", "string"), ("yes", "CurrentConnectionIDs", "string")])


def make_lan_handler(m):
    class L(Base):
        """local network: only stream links (with the secret code) and DLNA - never the control API"""

        def ok_client(self):
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                ip = None
            if ip and (ip.is_private or ip.is_loopback or ip.is_link_local):
                return True
            self.out(403, b"", "text/plain")
            return False

        def gate(self, parts):
            """True = go on. A request with the code must come from a device the user allowed; a wrong code counts as a failed try."""
            ip, tok = self.client_address[0], m.settings.get("lan_token", "")
            fails = [t for t in m.pin_fail.get(ip, []) if time.time() - t < 600]
            if parts[0] and tok and secrets.compare_digest(parts[0].encode(), tok.encode()):
                if len(fails) >= 10:
                    self.out(429, b"too many wrong codes - wait 10 minutes", "text/plain")
                    return False
                what = {"tv": "opened the TV home page", "files": "browsed the shared folders", "fplay": "played a shared file",
                        "proxy.pac": "loaded the proxy settings", "netsetup": "opened the VPN setup page", "netcheck": "VPN check",
                        "cmd": "TV player"}.get(parts[1] if len(parts) > 1 else "", "opened a film or stream")
                if m.lan_access(ip, self.headers.get("User-Agent", ""), what):
                    return True
                if self.command == "GET" and "text/html" in (self.headers.get("Accept") or ""):
                    self.out(403, WAIT_PAGE.encode(), "text/html; charset=utf-8")
                else:
                    self.out(403, {"error": "waiting for approval in dlus on the Mac"})
                return False
            if re.match(r"^[a-km-np-z2-9]{6}$", parts[0] or ""):  # looks like a code, but the wrong one
                m.pin_fail[ip] = fails + [time.time()]
                if len(fails) + 1 == 10:
                    m.audit("10 wrong codes in 10 minutes - blocked for 10 minutes", ip, level="warn")
                self.out(429 if len(fails) >= 10 else 404, b"", "text/plain")
                return False
            return True

        def do_GET(self):
            if not self.ok_client():
                return
            parts = [urllib.parse.unquote(x) for x in self.path.split("?")[0].strip("/").split("/")]
            if not self.gate(parts):
                return
            ff = safe_join(fonts_dir(), parts[1]) if len(parts) == 2 and parts[0] == "fonts" and re.match(r"^[\w\-]+\.(woff2|woff|ttf|otf)$", parts[1]) else None
            if ff and os.path.isfile(ff):
                with open(ff, "rb") as f:
                    return self.out(200, f.read(), FONT_MIME[parts[1].rsplit(".", 1)[1]], {"Cache-Control": "max-age=31536000"})
            if parts[0] == "dlna" and m.ssdp and len(parts) == 2:
                doc = {"desc.xml": dlna_desc(m), "cds.xml": CDS_SCPD, "cms.xml": CMS_SCPD}.get(parts[1])
                if doc:
                    return self.out(200, doc.encode(), 'text/xml; charset="utf-8"')
            if app_icon(self, "/" + "/".join(parts), "/"):
                return
            if parts == ["favicon.ico"]:
                return self.out(200, LOGO.encode(), "image/svg+xml")
            if parts == [""]:  # http://<mac>:8643/ -> enter the code shown in dlus
                return self.out(200, PIN_PAGE.encode(), "text/html; charset=utf-8")
            if parts == ["pin"]:
                ip = self.client_address[0]
                fails = [t for t in m.pin_fail.get(ip, []) if time.time() - t < 600]
                code = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("c") or [""])[0].strip().lower()
                if len(fails) >= 10:
                    return self.out(429, {"ok": False, "error": "too many tries - wait 10 minutes"})
                if code and secrets.compare_digest(code, m.settings.get("lan_token", "")):
                    m.sessions.saw(self, "connected with the code")
                    m.audit("entered the right code", ip)
                    m.lan_access(ip, self.headers.get("User-Agent", ""), "entered the code")
                    return self.out(200, {"ok": True, "home": "/%s/tv" % m.settings["lan_token"]})
                m.pin_fail[ip] = fails + [time.time()]
                if len(fails) + 1 == 10:
                    m.audit("10 wrong codes in 10 minutes - blocked for 10 minutes", ip, level="warn")
                return self.out(403, {"ok": False, "error": "wrong code"})
            if parts[0] == m.settings.get("lan_token") and parts[1:] == ["cmd"]:
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                if q.get("num") and self.client_address[0] not in ("127.0.0.1", "::1"):
                    m.tv_now = {"num": int(q["num"]), "t": float(q.get("t") or 0), "dur": float(q.get("dur") or 0), "playing": q.get("p") == "1",
                                "q": q.get("q", ""), "ip": self.client_address[0], "at": time.time()}
                if q.get("ev"):
                    log.info("player %s: %s", self.client_address[0], logsafe(q["ev"], 1500))
                subst = (q.get("ss", ""), q.get("sd", ""), q.get("fs", ""), q.get("vh", ""))
                if q.get("num") and (subst != getattr(m, "tv_sub_seen", None)):
                    m.tv_sub_seen = subst
                    log.info("player %s subtitle: %s sync %s s, fullscreen=%s, viewport/screen=%s", self.client_address[0], logsafe(subst[0] or "off", 80), logsafe(subst[1], 20), logsafe(subst[2], 20), logsafe(subst[3], 40))
                since = int(q.get("since") or 0)
                fresh = [c for c in list(m.tv_cmds) if c["seq"] > since and time.time() - c["at"] < 10]
                return self.out(200, {"seq": m.tv_seq, "cmds": fresh})
            if parts[0] == m.settings.get("lan_token") and parts[1:] == ["tv"]:
                m.sessions.saw(self, "opened dlus on the TV")
                return self.out(200, tv_home(m).encode(), "text/html; charset=utf-8")
            if parts[0] == m.settings.get("lan_token") and len(parts) >= 4 and parts[1] == "fplay" and parts[2].isdigit():
                full = m.shared_path(int(parts[2]), "/".join(parts[3:]))
                if not full or not os.path.isfile(full):
                    return self.out(404, b"not found", "text/plain")
                d = m.import_local(full)
                self.send_response(302)
                self.send_header("Location", "/%s/%d?autoplay=1" % (m.settings["lan_token"], d.r["num"]))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parts[0] == m.settings.get("lan_token") and len(parts) >= 4 and parts[1] == "f" and parts[2].isdigit():
                full = m.shared_path(int(parts[2]), "/".join(parts[3:]))
                if full and os.path.isfile(full):
                    m.sessions.saw(self, "plays a shared file: " + os.path.basename(full))
                    return send_file(self, full)
                return self.out(404, b"not found", "text/plain")
            if parts[0] == m.settings.get("lan_token") and len(parts) == 4 and parts[1] == "hls":
                return (m.hls or HLS(m)).serve(self, parts[2], parts[3]) if m.hls else self.out(404, b"gone", "text/plain")
            if parts[0] == m.settings.get("lan_token") and len(parts) == 2 and re.match(r"^\d+-picture$", parts[1]):
                tv = next((x for x in m.settings.get("tvs") or [] if x["ip"] == self.client_address[0]), None)
                if not tv:
                    return self.out(200, {"error": "not a known TV"})
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                try:
                    return self.out(200, m.tv_action(tv["id"], "picture", {"open": q.get("open") == "1"}))
                except (ValueError, KeyError) as e:
                    return self.out(200, {"error": str(e)})
            if parts[0] == m.settings.get("lan_token") and len(parts) == 2 and re.match(r"^\d+-native$", parts[1]):
                d = m.by_num(int(parts[1].split("-")[0]))
                tv = next((x for x in m.settings.get("tvs") or [] if x["ip"] == self.client_address[0]), None)
                if not d or not tv:
                    return self.out(404, {"error": "this TV is not known to dlus - press Find TVs on the Mac"})
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                try:
                    m.cast(d, tv["id"], "tv", float(q.get("t") or 0))  # sound as Dolby Digital Plus: LG's own player has no DTS-HD
                except ValueError as e:
                    return self.out(500, {"error": str(e)})
                return self.out(200, {"ok": True})
            if parts[0] == m.settings.get("lan_token") and len(parts) == 2 and re.match(r"^\d+-autosync$", parts[1]):
                d = m.by_num(int(parts[1].split("-")[0]))
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                try:
                    return self.out(200, m.subs_action(d, "autosync", {"sid": q.get("sid", "")}))
                except (ValueError, KeyError, AttributeError) as e:
                    return self.out(200, {"error": str(e)})
            if parts[0] == m.settings.get("lan_token") and len(parts) == 2 and re.match(r"^\d+-subset$", parts[1]):
                d = m.by_num(int(parts[1].split("-")[0]))
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                if not d:
                    return self.out(404, {"error": "no such video"})
                if q.get("style"):  # subtitle look changed on the TV: it is the preset for every movie
                    try:
                        m.set_settings({"sub_style": json.loads(q["style"])})
                    except (ValueError, TypeError) as e:
                        return self.out(400, {"error": str(e)})
                    return self.out(200, {"ok": True})
                if q.get("snd") in AUDIO_MODES:  # sound mode chosen in the player: kept for this film only
                    d.r["tv_audio"] = q["snd"]
                    m.save()
                    return self.out(200, {"ok": True})
                try:
                    m.subs_action(d, "set", dict({"sid": q.get("sid", ""), "delay": float(q.get("delay") or 0)},
                                                 **({"scale": float(q["scale"])} if q.get("scale") else {})))
                except (ValueError, KeyError) as e:
                    return self.out(400, {"error": str(e)})
                log.info("subtitle for %s set from %s: %s, sync %+.2f s", d.r["num"], self.client_address[0], q.get("sid") or "off", float(q.get("delay") or 0))
                return self.out(200, {"ok": True})
            if parts[0] == m.settings.get("lan_token") and len(parts) == 2 and re.match(r"^\d+-hls$", parts[1]):
                d = m.by_num(int(parts[1].split("-")[0]))
                if not d:
                    return self.out(404, {"error": "no such video"})
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                if not m.hls:
                    m.hls = HLS(m)
                try:
                    sid = m.hls.start(d, self.client_address[0], float(q.get("t") or 0), int(float(q.get("a") or 0)), int(float(q.get("ch") or 0)),
                                      q.get("snd"), q.get("q"))
                except ValueError as e:
                    log.warning("hls: %s", e)
                    return self.out(500, {"error": str(e)})
                x = m.hls.sess.get(sid, {})
                return self.out(200, {"url": "hls/%s/index.m3u8" % sid, "off": x.get("off", 0), "t0": x.get("t", 0),
                                      "converted": not tv_copy_ok(probe(m, d))})
            if parts[0] == m.settings.get("lan_token") and len(parts) == 2 and re.match(r"^\d+-thumb$", parts[1]):
                d = m.by_num(int(parts[1].split("-")[0]))
                try:
                    tq = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("t") or ["0"])[0]
                    with open(m.thumb(d, float(tq)), "rb") as f:
                        return self.out(200, f.read(), "image/jpeg", {"Cache-Control": "max-age=86400"})
                except (ValueError, AttributeError, OSError) as e:
                    return self.out(404, str(e).encode(), "text/plain")
            if parts[0] == m.settings.get("lan_token") and parts[1:] == ["vtest"]:  # TEMP: stream variant test page
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                page = ("<!doctype html><html><body style='margin:0;background:#000'><video id=v autoplay playsinline style='width:100vw;height:100vh' src='%s'></video>"
                        "<div id=o style='position:fixed;left:20px;top:20px;color:#0f0;font:28px monospace'></div><script>var v=document.getElementById('v'),Q=[],T=Date.now(),tag=%s;"
                        "['loadedmetadata','canplay','playing','waiting','stalled','ended','error','emptied'].forEach(function(n){v.addEventListener(n,function(){var b=[];"
                        "for(var i=0;i<v.buffered.length;i++)b.push(v.buffered.start(i).toFixed(1)+'-'+v.buffered.end(i).toFixed(1));"
                        "Q.push(((Date.now()-T)/1000).toFixed(1)+' '+n+' ct='+v.currentTime.toFixed(1)+' buf='+b.join(',')+' dur='+v.duration+(v.error?' code='+v.error.code:''))})});"
                        "setInterval(function(){document.getElementById('o').textContent=tag+' ct='+v.currentTime.toFixed(1);var x=new XMLHttpRequest();"
                        "x.open('GET','cmd?ev='+encodeURIComponent(tag+' :: '+(Q.length?Q.join(' | '):'ct='+v.currentTime.toFixed(1))),true);Q=[];x.send()},2000);</script></body></html>"
                        ) % (xesc(q.get("src", "")), json.dumps(q.get("tag", "")))
                return self.out(200, page.encode(), "text/html; charset=utf-8")
            if parts[0] == m.settings.get("lan_token") and parts[1:] == ["netsetup"]:
                m.sessions.saw(self, "opened the VPN setup page")
                return self.out(200, net_setup_page(m, self.client_address[0]).encode(), "text/html; charset=utf-8")
            if parts[0] == m.settings.get("lan_token") and parts[1:] == ["netcheck"]:
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                ip = self.client_address[0]
                if "ok" in q:
                    m.gw_tvnet[ip] = {"ok": q.get("ok") == "1", "ms": int(float(q.get("ms") or 0)), "at": time.time()}
                return self.out(200, m.gw_check_ip(ip))
            if parts[0] == m.settings.get("lan_token") and len(parts) == 2:
                mq = re.match(r"^(\d+)-sub(search|get)$", parts[1])
                d = mq and m.by_num(int(mq.group(1)))
                if d:  # subtitle search / add from the TV's menu
                    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                    try:
                        if mq.group(2) == "search":
                            return self.out(200, m.subs_search(d, q.get("lang") or "fa"))
                        r = m.subs_action(d, "get", {"path": q.get("path", ""), "id": q.get("id", ""), "name": q.get("name", ""), "label": q.get("label", "")})
                        return self.out(200, dict(r, subs=m.subs_info(d)))
                    except (ValueError, KeyError) as e:
                        return self.out(400, {"error": str(e)})
            if parts[0] == m.settings.get("lan_token") and len(parts) >= 2 and parts[1] == "files":
                if not m.settings.get("files_on") and not (len(parts) > 2 and parts[2] == "sdir"):
                    return self.out(404, b"File sharing is off", "text/plain")
                if len(parts) > 3 and parts[2] == "sdir" and parts[3].isdigit():  # shared folder (TV home tiles)
                    full = m.shared_path(int(parts[3]), "/".join(parts[4:]))
                    if not full:
                        return self.out(404, b"not found", "text/plain")
                    if os.path.isfile(full):
                        return send_file(self, full)
                    m.sessions.saw(self, "browses the shared folder " + os.path.basename(full))
                    return self.out(200, tv_folder(m, int(parts[3]), "/".join(parts[4:])).encode(), "text/html; charset=utf-8")
                    base_i = dict(m.share_dirs())[int(parts[3])]
                    rows = []
                    for n in sorted(os.listdir(full), key=str.lower):
                        if n.startswith("."):
                            continue
                        rp = os.path.relpath(os.path.join(full, n), base_i)
                        isd = os.path.isdir(os.path.join(full, n))
                        rows.append("<tr><td>%s</td><td><a href='/%s/files/sdir/%s/%s%s'>%s</a></td><td>%s</td></tr>" % (
                            "&#128193;" if isd else "&#127916;" if category(n) == "Video" else "&#128196;", m.settings["lan_token"], parts[3],
                            urllib.parse.quote(rp), "/" if isd else "", xesc(n), "" if isd else "%.1f MB" % (fsize(os.path.join(full, n)) / 2 ** 20)))
                    t = xesc(os.path.basename(full))
                    return self.out(200, (FILES_HTML % (t, t, "<table>%s</table>" % "".join(rows))).encode(), "text/html; charset=utf-8")
                rel = "/".join(parts[3:]) if len(parts) > 2 and parts[2] == "dir" else ""
                if len(parts) > 2 and parts[2] == "dir" and not rel:
                    rel = "."
                t, body = files_page(m, rel)
                if t is None:
                    return self.out(404, b"not found", "text/plain")
                if t == "file":
                    m.sessions.saw(self, "downloaded a shared file: " + os.path.basename(body))
                    return send_file(self, body)
                m.sessions.saw(self, "browsed the shared files")
                return self.out(200, (FILES_HTML % (t, t, body)).encode(), "text/html; charset=utf-8")
            if parts[0] == m.settings.get("lan_token") and parts[1:] == ["proxy.pac"] and m.proxy:
                pac = ('function FindProxyForURL(url, host) {\n  if (isPlainHostName(host) || shExpMatch(host, "*.local") ||\n'
                       '      isInNet(host, "10.0.0.0", "255.0.0.0") || isInNet(host, "172.16.0.0", "255.240.0.0") ||\n'
                       '      isInNet(host, "192.168.0.0", "255.255.0.0") || isInNet(host, "127.0.0.0", "255.0.0.0"))\n'
                       '    return "DIRECT";\n  return "PROXY %s:%d";\n}\n' % (m.lan_ip(), int(m.settings["proxy_port"])))
                return self.out(200, pac.encode(), "application/x-ns-proxy-autoconfig")
            if parts[0] == m.settings.get("lan_token") and len(parts) == 2:
                ms = re.match(r"^(\d+)-(subs\.json|\w+\.vtt)$", parts[1])
                d = ms and m.by_num(int(ms.group(1)))
                if d:
                    if ms.group(2) == "subs.json":
                        return self.out(200, m.subs_info(d))
                    try:
                        path = m.sub_path(d, ms.group(2)[:-4])
                    except KeyError:
                        return self.out(404, b"", "text/plain")
                    if not path:
                        return self.out(202, {"pending": True})
                    with open(path, "rb") as f:
                        return self.out(200, f.read(), "text/vtt; charset=utf-8")
                mm = re.match(r"^(\d+)([st]?)(\.\w+)?$", parts[1])
                d = mm and m.by_num(int(mm.group(1)))
                if d and mm.group(3) not in (None, ".json", ".mp4") and self.browser_nav():
                    return self.to_page(d)
                if d:
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    num = lambda k, dflt: float((q.get(k) or [dflt])[0] or dflt)
                    if not mm.group(3) and not mm.group(2):
                        m.sessions.saw(self, "opened the player page: " + d.r["name"])
                        return self.out(200, tv_page(m, d).encode(), "text/html; charset=utf-8")
                    if mm.group(3) == ".json":
                        return self.out(200, {"prefix": d.snap.get("prefix", 0), "size": d.r.get("size") or 0,
                                              "status": d.r["status"], "now": d.snap.get("now", 0)})
                    if mm.group(3) == ".mp4":
                        return send_compat(self, m, d, "tv", num("t", 0), int(num("a", 0)), int(num("ch", 0)), (q.get("snd") or [None])[0],
                                           quality=(q.get("q") or [None])[0], mkv=(q.get("c") or [""])[0] == "mkv")
                    if mm.group(2):
                        log.debug("TV request %s %s | %s", self.command, logsafe(self.path), logsafe(" | ".join("%s: %s" % kv for kv in self.headers.items()), 2000))
                        ts = re.match(r"npt=([\d.:]+)-", self.headers.get("TimeSeekRange.dlna.org", ""))
                        t0 = num("t", 0)
                        if ts:  # TV seek bar: npt=SS.sss- or npt=HH:MM:SS.sss-
                            v = ts.group(1).split(":")
                            t0 = sum(float(x) * 60 ** i for i, x in enumerate(reversed(v)))
                        return send_compat(self, m, d, "mkv" if mm.group(2) == "s" else "mkvsub", t0, seek_hdr=bool(ts), quality=(q.get("q") or [None])[0])
                    log.info("media %s raw %s range=%s ua=%s", self.client_address[0], d.r["num"], logsafe(self.headers.get("Range", "-")), logsafe(self.headers.get("User-Agent") or "", 60))
                    return send_stream(self, d, lan=True)
            if parts[0] == m.settings.get("lan_token") and len(parts) >= 2:
                d = m.find(parts[1])
                if d and self.browser_nav():
                    return self.to_page(d)
                if d:
                    if len(parts) > 2 and parts[2] == "c":
                        return send_compat(self, m, d, "mkv")
                    log.info("media %s raw %s range=%s ua=%s", self.client_address[0], d.r["num"], logsafe(self.headers.get("Range", "-")), logsafe(self.headers.get("User-Agent") or "", 60))
                    return send_stream(self, d, lan=True)
            self.out(404, b"", "text/plain")

        def browser_nav(self):
            """a web browser opening the link as a page (not a <video> or a player app)"""
            return self.command == "GET" and "text/html" in (self.headers.get("Accept") or "")

        def to_page(self, d):
            self.send_response(302)
            self.send_header("Location", "/%s/%d" % (m.settings["lan_token"], d.r["num"]))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if not self.ok_client():
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "ignore")
            action = (self.headers.get("SOAPACTION") or "").strip().strip('"').split("#")[-1]
            xml = None
            if m.ssdp and self.path.startswith("/dlna/") and not m.lan_access(self.client_address[0], self.headers.get("User-Agent", ""), "browsed the library (DLNA)"):
                return self.out(403, b"", "text/plain")
            if m.ssdp and self.path.startswith("/dlna/cds/"):
                m.sessions.saw(self, "browsed the dlus library (DLNA)")
                xml = dlna_cds(m, action, body)
            elif m.ssdp and self.path.startswith("/dlna/cms/"):
                xml = dlna_cms(action)
            if xml is None:
                return self.out(500, b"", "text/plain")
            self.out(200, xml.encode(), 'text/xml; charset="utf-8"', {"EXT": ""})

        def do_SUBSCRIBE(self):
            self.send_response(200)
            self.send_header("SID", "uuid:%s-events" % m.settings["dlna_uuid"])
            self.send_header("TIMEOUT", "Second-1800")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_UNSUBSCRIBE = do_SUBSCRIBE

    return L


WAIT_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=3><title>dlus - waiting for approval</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d0f1a;color:#eef;font:20px/1.5 -apple-system,system-ui,sans-serif;text-align:center}
div{max-width:640px;padding:32px}b{color:#9d92ff}small{color:#99a}</style></head><body><div>
<h1>Waiting for approval</h1><p>Open <b>dlus on the Mac</b> and press <b>Allow</b> for this device.</p>
<small>This page continues by itself once you are allowed.</small></div></body></html>"""

LOCAL_PLAYER_PORT = 8644


class LanServer:
    # security scan: home-network server, only when the user turns sharing on; private addresses only
    def __init__(self, m, host="0.0.0.0", port=None):  # nosec B104
        """host 0.0.0.0 = the home network (only when the user allowed sharing); 127.0.0.1 = the Mac's own player"""
        self.srv = http.server.ThreadingHTTPServer((host, int(port or m.settings["lan_port"])), make_lan_handler(m))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True, name="lan").start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class SSDP:
    """announces dlus as a DLNA media server so TVs list it under media sources"""
    GRP = ("239.255.255.250", 1900)
    TYPES = ["upnp:rootdevice", "urn:schemas-upnp-org:device:MediaServer:1",
             "urn:schemas-upnp-org:service:ContentDirectory:1", "urn:schemas-upnp-org:service:ConnectionManager:1"]

    def __init__(self, m):
        self.m = m
        self.uuid = "uuid:" + m.settings["dlna_uuid"]
        self.stop_ev = threading.Event()
        threading.Thread(target=self.run, daemon=True, name="ssdp").start()

    def stop(self):
        self.stop_ev.set()
        try:
            self.notify("ssdp:byebye")
        except OSError:
            pass

    def usn(self, nt):
        return self.uuid if nt == self.uuid else "%s::%s" % (self.uuid, nt)

    def loc(self):
        return "http://%s:%d/dlna/desc.xml" % (self.m.lan_ip(), int(self.m.settings["lan_port"]))

    def sender(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        ip = self.m.lan_ip()
        if ip != "127.0.0.1":
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        return s

    def notify(self, nts):
        s = self.sender()
        for nt in self.TYPES + [self.uuid]:
            msg = ("NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nCACHE-CONTROL: max-age=1800\r\nLOCATION: %s\r\n"
                   "NT: %s\r\nNTS: %s\r\nSERVER: macOS UPnP/1.0 dlus/%s\r\nUSN: %s\r\n\r\n"
                   % (self.loc(), nt, nts, VERSION, self.usn(nt)))
            s.sendto(msg.encode(), self.GRP)
        s.close()

    def run(self):
        try:
            r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            r.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                r.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            r.bind(("", 1900))
            r.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         # security scan: SSDP multicast membership, not a bind
                         struct.pack("4s4s", socket.inet_aton(self.GRP[0]), socket.inet_aton("0.0.0.0")))  # nosec B104
            r.settimeout(1)
        except OSError:
            log.exception("ssdp: cannot listen on UDP 1900")
            return
        last = 0
        while not self.stop_ev.is_set():
            try:
                if time.time() - last > 60:
                    self.notify("ssdp:alive")
                    last = time.time()
                data, addr = r.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(2)
                continue
            txt = data.decode("utf-8", "ignore")
            if not txt.startswith("M-SEARCH"):
                continue
            mm = re.search(r"^ST:\s*(.+?)\s*$", txt, re.M | re.I)
            st = mm.group(1) if mm else ""
            targets = self.TYPES + [self.uuid] if st == "ssdp:all" else [st] if st in self.TYPES or st == self.uuid else []
            s = self.sender()
            for t in targets:
                msg = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nEXT:\r\nLOCATION: %s\r\n"
                       "SERVER: macOS UPnP/1.0 dlus/%s\r\nST: %s\r\nUSN: %s\r\n\r\n" % (self.loc(), VERSION, t, self.usn(t)))
                try:
                    s.sendto(msg.encode(), addr)
                except OSError:
                    pass
            s.close()
        r.close()


# ---------------------------------------------------------------- web server
def make_handler(m, port):
    hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class H(Base):
        def guard(self):
            if self.headers.get("Host") not in hosts:
                self.out(403, {"error": "bad host"})
                return False
            path, _, query = self.path.partition("?")
            if path in UI_OPEN or path.startswith("/fonts/"):
                return True
            if path.startswith(("/stream/", "/s/", "/sub/")):  # media only: ffmpeg, players, the <video> element
                ik = (urllib.parse.parse_qs(query).get("ik") or [""])[0]
                if ik and secrets.compare_digest(ik, m.media_key):
                    return True
            key = m.ui_key
            ck = re.search(r"(?:^|;\s*)dlus_k=([\w\-]+)", self.headers.get("Cookie") or "")
            if ck and secrets.compare_digest(ck.group(1), key):
                return True
            qk = (urllib.parse.parse_qs(query).get("k") or [""])[0]
            if qk and secrets.compare_digest(qk, key):  # opened by the app: remember it in a cookie, drop the key from the address
                rest = urllib.parse.urlencode([(a, b) for a, b in urllib.parse.parse_qsl(query) if a != "k"])
                self.send_response(302)
                if not path.startswith("/") or path.startswith("//") or "\\" in path:
                    path = "/"  # only addresses on this page: never another site
                path = path.replace("\r", "").replace("\n", "")
                self.send_header("Location", path + ("?" + rest if rest else ""))
                self.send_header("Set-Cookie", "dlus_k=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict" % key)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            if path == "/":
                self.out(401, LOCKED_PAGE.encode(), "text/html; charset=utf-8")
            else:
                self.out(401, {"error": "open dlus from the dlus app"})
            return False

        def do_GET(self):
            if not self.guard():
                return
            p = self.path.split("?")[0]
            if p == "/":
                return self.out(200, PAGE.replace("/*SUBJS*/", fonts_js(m) + SUBJS).replace("/*SUBCSS*/", font_css(m) + SUBCSS).encode(), "text/html; charset=utf-8")
            if p == "/api/state":
                return self.out(200, m.state())
            mf = re.match(r"^/fonts/([\w\-]+\.(woff2|woff|ttf|otf))$", p)
            ff = safe_join(fonts_dir(), mf.group(1)) if mf else None
            if ff and os.path.isfile(ff):
                with open(ff, "rb") as f:
                    return self.out(200, f.read(), FONT_MIME[mf.group(2)], {"Cache-Control": "max-age=31536000"})
            if p == "/qr.svg":
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("d", [""])[0]
                try:
                    return self.out(200, qr_svg(q).encode(), "image/svg+xml")
                except ValueError as e:
                    return self.out(400, {"error": str(e)})
            if app_icon(self, p, "/"):
                return
            if p in ("/favicon.svg", "/favicon.ico", "/logo.svg"):
                return self.out(200, LOGO.encode(), "image/svg+xml")
            if p == "/api/health":
                return self.out(200, {"ok": True, "threads": threading.active_count(), "downloads": len(m.dls)})
            if p.startswith("/api/d/"):
                try:
                    return self.out(200, m.detail(p[7:]))
                except KeyError:
                    return self.out(404, {"error": "no such download"})
            if p.startswith("/api/subs/"):
                d = m.dls.get(p[10:].split("/")[0])
                if not d:
                    return self.out(404, {"error": "no such download"})
                if p.endswith("/autosync"):  # GET so the shared player code can poll it like the TV does
                    sid = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("sid") or [""])[0]
                    try:
                        return self.out(200, m.subs_action(d, "autosync", {"sid": sid}))
                    except (ValueError, KeyError) as e:
                        return self.out(200, {"error": str(e)})
                return self.out(200, m.subs_info(d))
            mm = re.match(r"^/sub/(\w+)/(\w+)\.vtt$", p)
            if mm:
                d = m.dls.get(mm.group(1))
                try:
                    path = d and m.sub_path(d, mm.group(2))
                except KeyError:
                    path = None
                    d = None
                if not d:
                    return self.out(404, {"error": "no such subtitle"})
                if not path:
                    return self.out(202, {"pending": True})
                with open(path, "rb") as f:
                    return self.out(200, f.read(), "text/vtt; charset=utf-8")
            if p == "/api/browse":
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
                full = m.shared_path(int(q.get("i", -1)), q.get("rel", ""))
                if not full or not os.path.isdir(full):
                    return self.out(404, {"error": "not found"})
                base_i = dict(m.share_dirs())[int(q["i"])]
                ents = []
                for n in sorted(os.listdir(full), key=str.lower):
                    if n.startswith(".") or n.endswith((".parts", ".part", ".assembling")):
                        continue
                    fp = os.path.join(full, n)
                    ents.append({"name": n, "rel": os.path.relpath(fp, base_i), "dir": os.path.isdir(fp), "cat": category(n), "size": 0 if os.path.isdir(fp) else fsize(fp)})
                return self.out(200, {"rel": os.path.relpath(full, base_i), "items": ents[:500]})
            if p.startswith("/api/probe/"):
                d = m.dls.get(p[11:])
                if not d:
                    return self.out(404, {"error": "no such download"})
                return self.out(200, dict(probe(m, d), ffmpeg=bool(shutil.which("ffmpeg"))))
            mm = re.match(r"^/s/(\d+)([st]?)(\.\w+)?$", p)
            if mm:
                d = m.by_num(int(mm.group(1)))
                if not d:
                    return self.out(404, {"error": "no such download"})
                if mm.group(2):
                    return send_compat(self, m, d, "mkv" if mm.group(2) == "s" else "mkvsub")
                return send_stream(self, d)
            if p.startswith("/stream/"):
                d = m.dls.get(p.split("/")[2])
                if not d:
                    return self.out(404, {"error": "no such download"})
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if q.get("compat"):
                    return send_compat(self, m, d, "mp4", float(q.get("t", ["0"])[0] or 0), int(q.get("a", ["0"])[0] or 0))
                return send_stream(self, d)
            self.out(404, {"error": "not found"})

        def do_POST(self):
            if not self.guard():
                return
            o = self.headers.get("Origin")
            if o and o.split("//")[-1] not in hosts:
                return self.out(403, {"error": "bad origin"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                parts = self.path.strip("/").split("/")
                if parts == ["api", "add"]:
                    urls = list(dict.fromkeys(re.findall(r"(?:https?|ftp)://[^\s\"'<>]+", body.get("urls", ""))))
                    if not urls:
                        return self.out(400, {"error": "no URL found"})
                    ids, skipped = [], []
                    for u in urls:
                        try:
                            ids.append(m.add(u, body.get("folder", ""), body.get("conns", 0), body.get("start", True),
                                             body.get("checksum", "") if len(urls) == 1 else ""))
                        except ValueError as e:
                            skipped.append(str(e))
                    return self.out(200 if ids else 400, {"ids": ids, "skipped": skipped, "error": "; ".join(skipped)})
                if parts == ["api", "settings"]:
                    m.set_settings(body)
                    return self.out(200, {"ok": True})
                if parts == ["api", "import"]:  # a file in a shared folder -> playable library entry
                    full = m.shared_path(int(body.get("folder", -1)), str(body.get("rel") or ""))
                    if not full or not os.path.isfile(full):
                        return self.out(404, {"error": "not found"})
                    return self.out(200, {"id": m.import_local(full).r["id"]})
                if parts == ["api", "fonts"]:
                    return self.out(200, m.fonts_set(body))
                if parts == ["api", "access"]:
                    return self.out(200, m.lan_decide(body))
                if parts == ["api", "gw"]:
                    return self.out(200, m.gw_set(body))
                if parts == ["api", "gw", "dismiss"]:
                    m.gw.last_result = None
                    return self.out(200, {"ok": True})
                if parts == ["api", "gw", "admin"]:
                    return self.out(200, m.gw_admin(body))
                if parts == ["api", "gw", "full"]:
                    return self.out(200, m.gw_full(body))
                if parts == ["api", "gw", "diag"]:
                    return self.out(200, m.gw_diag())
                if parts == ["api", "gw", "wizard"]:
                    return self.out(200, m.gw_wizard(body))
                if len(parts) == 4 and parts[:2] == ["api", "tv"]:
                    return self.out(200, m.tv_action(parts[2], parts[3], body))
                if parts == ["api", "tvs", "find"]:
                    return self.out(200, {"tvs": m.find_tvs()})
                if parts == ["api", "tvs", "cast"]:
                    d = m.dls.get(body.get("id", ""))
                    if not d:
                        return self.out(404, {"error": "no such download"})
                    return self.out(200, m.cast(d, body.get("tv", ""), body.get("sound", "tv"), float(body.get("at") or 0)))
                if len(parts) == 4 and parts[:3] == ["api", "tvs", "ctl"]:
                    return self.out(200, m.cast_ctl(parts[3]))
                if len(parts) == 4 and parts[:2] == ["api", "subs"]:
                    d = m.dls.get(parts[2])
                    if not d:
                        return self.out(404, {"error": "no such download"})
                    return self.out(200, m.subs_action(d, parts[3], body))
                if len(parts) == 4 and parts[:2] == ["api", "d"]:
                    return self.out(200, m.action(parts[2], parts[3], body))
                self.out(404, {"error": "not found"})
            except (KeyError, ValueError) as e:
                self.out(400, {"error": str(e).strip("'\"")})

        def log_message(self, *a):
            pass

    return H


def cli_add(args):
    body = json.dumps({"urls": " ".join(args.urls), "folder": args.folder or "", "conns": args.conns or 0,
                       "start": not args.paused}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/api/add" % args.port, body, {"Content-Type": "application/json", "Cookie": "dlus_k=" + ui_key()})
    try:
        # security scan: fixed http://127.0.0.1 URL
        print(urllib.request.urlopen(req, timeout=10).read().decode())  # nosec B310
    except urllib.error.HTTPError as e:
        sys.exit(e.read().decode())
    except OSError as e:
        sys.exit("dlus is not running (start it with: python3 dlus.py) - %s" % e)


def main():
    ap = argparse.ArgumentParser(description="dlus - downloader for us")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add", help="add URLs to a running dlm")
    a.add_argument("urls", nargs="+")
    a.add_argument("-f", "--folder", help="folder name (under the download dir) or full path")
    a.add_argument("-c", "--conns", type=int)
    a.add_argument("--paused", action="store_true")
    a.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    if args.cmd == "add":
        return cli_add(args)
    if not shutil.which("curl"):
        sys.exit("curl is required")
    os.makedirs(STATE_DIR, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)  # TV code, TV pairing keys, UI key: this user only
    h = logging.handlers.RotatingFileHandler(os.path.join(STATE_DIR, "dlus.log"), maxBytes=2 << 20, backupCount=3)
    h.setFormatter(OneLineFormatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[h, logging.StreamHandler()])
    url = "http://127.0.0.1:%d/?k=%s" % (args.port, ui_key())
    lockf = open(os.path.join(STATE_DIR, "lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("dlus is already running:", url)
        if not args.no_open and sys.platform == "darwin":
            subprocess.Popen(["open", url])
        return
    m = Manager(args.port)
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(m, args.port))
    except OSError as e:
        sys.exit("port %d is busy (%s)" % (args.port, e))
    srv.daemon_threads = True

    def bye(*_):
        log.info("stopping - progress is kept")
        m.shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    threading.Thread(target=m.net.loop, daemon=True, name="net").start()
    threading.Thread(target=m.loop, daemon=True, name="manager").start()
    threading.Thread(target=fetch_fonts, daemon=True, name="fonts").start()
    threading.Thread(target=m.gw.loop, daemon=True, name="gateway").start()
    try:
        m.apply_lan()
        try:  # the player on this Mac uses the TV player too, reachable only from this Mac
            m.local_player = LanServer(m, "127.0.0.1", LOCAL_PLAYER_PORT)
        except OSError as e:
            log.error("local player port %s busy: %s", LOCAL_PLAYER_PORT, e)
        m.apply_proxy()
    except OSError as e:
        log.error("local network sharing could not start: %s", e)
    log.info("dlus %s running: http://127.0.0.1:%d/", VERSION, args.port)
    if not args.no_open and sys.platform == "darwin":
        subprocess.Popen(["open", url])
    srv.serve_forever()


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>dlus</title>
<link rel=icon id=fav href="/favicon.svg"><meta name=color-scheme content="dark light">
<style>
:root{--bg:#09090b;--panel:#111114;--panel2:#16161a;--elev:#1c1c21;--hover:rgba(255,255,255,.045);--line:rgba(255,255,255,.07);--line2:rgba(255,255,255,.12);
--txt:#ededf0;--mut:#8e8e9a;--dim:#5f5f6b;--acc:#6d5efc;--acc2:#8f84ff;--accs:rgba(109,94,252,.14);--acct:#fff;
--green:#30c47b;--amber:#f5a524;--red:#f2555a;--blue:#3b82f6;--cyan:#22c3ee;--shadow:0 1px 0 rgba(255,255,255,.04) inset,0 8px 30px rgba(0,0,0,.35);--r:14px}
[data-theme=light]{--bg:#f6f6f8;--panel:#ffffff;--panel2:#f3f3f6;--elev:#ffffff;--hover:rgba(0,0,0,.04);--line:rgba(0,0,0,.075);--line2:rgba(0,0,0,.13);
--txt:#141418;--mut:#6c6c78;--dim:#a0a0ab;--acc:#5b4cf0;--acc2:#5b4cf0;--accs:rgba(91,76,240,.1);--shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.06)}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font:13.5px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",sans-serif;color:var(--txt);background:var(--bg);-webkit-font-smoothing:antialiased;letter-spacing:-.003em}
button,input,select,textarea{font:inherit;color:inherit}
::selection{background:var(--accs)}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--line2);border-radius:10px;border:3px solid transparent;background-clip:padding-box}
svg{flex:none}
.app{display:grid;grid-template-columns:248px 1fr;min-height:100vh}
/* sidebar */
.sb{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;padding:14px 10px;border-right:1px solid var(--line);background:var(--panel)}
.brand{display:flex;align-items:center;gap:10px;padding:6px 8px 14px;cursor:pointer;user-select:none}
.brand img{width:30px;height:30px;border-radius:9px;transition:transform .25s}.brand:hover img{transform:translateY(2px)}
.brand b{font-size:17px;font-weight:750;letter-spacing:-.03em}.brand span{margin-left:auto;font-size:10.5px;color:var(--mut);border:1px solid var(--line2);padding:1px 6px;border-radius:99px}
.new{display:flex;align-items:center;gap:8px;width:100%;padding:9px 12px;border-radius:10px;border:0;background:var(--acc);color:#fff;font-weight:620;cursor:pointer;box-shadow:0 1px 0 rgba(255,255,255,.18) inset,0 6px 18px -6px var(--acc);transition:filter .15s,transform .1s}
.new:hover{filter:brightness(1.08)}.new:active{transform:scale(.98)}.new kbd{margin-left:auto;background:rgba(255,255,255,.18);border-color:transparent;color:#fff}
nav{flex:1;overflow:auto;margin-top:12px;padding-right:2px}
nav h6{margin:12px 10px 3px;font-size:11px;font-weight:600;color:var(--dim)}
nav a{display:flex;align-items:center;gap:10px;padding:4.5px 10px;border-radius:8px;color:var(--mut);cursor:pointer;font-weight:520;transition:background .12s,color .12s}
nav a svg{width:16px;height:16px;opacity:.9}nav a:hover{background:var(--hover);color:var(--txt)}
nav a.on{background:var(--accs);color:var(--txt)}nav a.on svg{color:var(--acc2)}
nav a em{margin-left:auto;font-style:normal;font-size:11.5px;color:var(--dim);font-variant-numeric:tabular-nums}
.foot{border-top:1px solid var(--line);padding:10px 4px 2px;display:flex;flex-direction:column;gap:6px}
.chip{display:flex;align-items:center;gap:8px;padding:7px 9px;border-radius:9px;color:var(--mut);font-size:12.5px;cursor:pointer;transition:background .12s}
.chip:hover{background:var(--hover);color:var(--txt)}.chip svg{width:15px;height:15px}.chip b{color:var(--txt);font-weight:560;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip .r{margin-left:auto;display:flex;align-items:center;gap:6px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px rgba(48,196,123,.15)}.dot.off{background:var(--red);box-shadow:0 0 0 3px rgba(242,85,90,.15)}.dot.warn{background:var(--amber);box-shadow:0 0 0 3px rgba(245,165,36,.15)}.dot.idle{background:var(--dim);box-shadow:none}
.mini{height:4px;border-radius:9px;background:var(--line2);overflow:hidden;margin:0 9px 4px}.mini i{display:block;height:100%;background:var(--acc)}
/* main */
main{min-width:0;padding:0 28px 60px}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:14px 0;background:linear-gradient(var(--bg) 70%,transparent)}
.search{flex:1;max-width:620px;display:flex;align-items:center;gap:9px;padding:0 12px;height:38px;border-radius:11px;border:1px solid var(--line2);background:var(--panel);transition:border-color .15s,box-shadow .15s}
.search:focus-within{border-color:var(--acc);box-shadow:0 0 0 4px var(--accs)}.search svg{width:16px;height:16px;color:var(--dim)}
.search input{flex:1;border:0;background:transparent;outline:none;height:100%}
.speed{display:flex;align-items:center;gap:10px;height:38px;padding:0 12px;border-radius:11px;border:1px solid var(--line);background:var(--panel);margin-left:auto}
.speed canvas{width:90px;height:24px}.speed b{font-size:15px;font-variant-numeric:tabular-nums;font-weight:650;letter-spacing:-.02em}.speed span{color:var(--mut);font-size:11.5px}
.ibtn{width:38px;height:38px;display:grid;place-items:center;border-radius:11px;border:1px solid var(--line);background:var(--panel);cursor:pointer;color:var(--mut);transition:all .12s}
.ibtn:hover{color:var(--txt);border-color:var(--line2)}.ibtn svg{width:17px;height:17px}
kbd{display:inline-block;padding:0 6px;min-width:20px;text-align:center;border-radius:6px;border:1px solid var(--line2);background:var(--panel2);font:600 11px/18px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--mut)}
.head{display:flex;align-items:center;gap:12px;margin:10px 0 14px}
.head h1{margin:0;font-size:22px;font-weight:720;letter-spacing:-.03em}.head .cnt{color:var(--mut);font-variant-numeric:tabular-nums}
.head .tools{margin-left:auto;display:flex;gap:6px}
.btn{display:inline-flex;align-items:center;gap:7px;height:32px;padding:0 12px;border-radius:9px;border:1px solid var(--line2);background:var(--panel);cursor:pointer;font-weight:560;font-size:12.5px;transition:all .12s;white-space:nowrap}
.btn:hover{background:var(--panel2)}.btn svg{width:14px;height:14px}
.btn.pri{background:var(--acc);border-color:transparent;color:#fff}.btn.pri:hover{filter:brightness(1.08);background:var(--acc)}
.btn.danger{background:var(--red);border-color:transparent;color:#fff}
/* notifications */
.bellw{position:relative}.ibtn#bell{position:relative}
.belln{position:absolute;top:-5px;right:-5px;min-width:18px;height:18px;padding:0 5px;border-radius:99px;background:var(--red);color:#fff;font:700 10.5px/18px -apple-system,sans-serif;text-align:center;display:none;box-shadow:0 0 0 2px var(--bg)}
.belln.on{display:block}.belln.ok{background:var(--acc)}
.ring svg{animation:ring 1.1s ease-in-out;transform-origin:50% 8%}
@keyframes ring{0%,100%{transform:rotate(0)}10%{transform:rotate(18deg)}25%{transform:rotate(-16deg)}40%{transform:rotate(12deg)}55%{transform:rotate(-8deg)}70%{transform:rotate(4deg)}}
.npanel{position:absolute;right:0;top:46px;z-index:15;width:400px;max-height:70vh;overflow:auto;display:none;border-radius:16px;background:var(--elev);border:1px solid var(--line2);box-shadow:0 24px 70px rgba(0,0,0,.4);animation:pop .15s}
.npanel.on{display:block}.nph{display:flex;align-items:center;padding:12px 14px;border-bottom:1px solid var(--line)}.nph .btn{margin-left:auto;height:26px}
.nitem{display:flex;gap:11px;padding:12px 14px;border-bottom:1px solid var(--line);position:relative}.nitem:last-child{border:0}
.nitem .ni{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;flex:none}.nitem .ni svg{width:16px;height:16px}
.nitem.bad .ni{background:rgba(242,85,90,.14);color:var(--red)}.nitem.warn .ni{background:rgba(245,165,36,.14);color:var(--amber)}
.nitem.info .ni{background:var(--accs);color:var(--acc2)}.nitem.ok .ni{background:rgba(48,196,123,.14);color:var(--green)}
.nitem .nt{flex:1;min-width:0;font-size:12.8px}.nitem .nt b{display:block;font-size:13px;margin-bottom:2px}.nitem .nt small{color:var(--dim);display:block;margin-top:4px}
.nitem.unread:before{content:"";position:absolute;left:5px;top:22px;width:6px;height:6px;border-radius:50%;background:var(--acc)}
.nitem .nx{border:0;background:transparent;color:var(--dim);cursor:pointer;font-size:16px;height:24px;align-self:flex-start}.nitem .nx:hover{color:var(--txt)}
.nempty{padding:26px;text-align:center;color:var(--mut);font-size:12.8px}
/* banners */
.ban{display:flex;gap:10px;align-items:flex-start;padding:10px 13px;border-radius:12px;margin-bottom:8px;font-size:12.8px;border:1px solid}
.ban svg{width:16px;height:16px;margin-top:1px}
.ban.bad{background:rgba(242,85,90,.07);border-color:rgba(242,85,90,.28)}.ban.bad svg{color:var(--red)}
.ban.warn{background:rgba(245,165,36,.07);border-color:rgba(245,165,36,.28)}.ban.warn svg{color:var(--amber)}
.ban.info{background:var(--accs);border-color:rgba(109,94,252,.3)}.ban.info svg{color:var(--acc2)}
/* bento */
.bento{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:10px;margin-bottom:16px}
.tile{padding:14px 16px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel);box-shadow:var(--shadow);position:relative;overflow:hidden}
.tile small{display:block;color:var(--mut);font-size:11.5px;font-weight:550;margin-bottom:6px}
.tile b{font-size:24px;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums}.tile b em{font-style:normal;font-size:13px;color:var(--mut);font-weight:560;margin-left:4px}
.tile .sub{color:var(--mut);font-size:12px;margin-top:2px}
.tile canvas{position:absolute;left:0;right:0;bottom:0;width:100%;height:56px;opacity:.95}
/* list */
.item{border:1px solid var(--line);background:var(--panel);border-radius:var(--r);margin-bottom:8px;transition:border-color .15s,box-shadow .15s;box-shadow:var(--shadow)}
.item.fresh{animation:in .35s cubic-bezier(.2,.8,.2,1) both}@keyframes in{from{opacity:0;transform:translateY(6px) scale(.995)}}
.item:hover{border-color:var(--line2)}
.row{display:grid;grid-template-columns:40px 1fr auto;gap:14px;align-items:center;padding:12px 14px;cursor:pointer}
.fi{width:40px;height:40px;border-radius:11px;display:grid;place-items:center;background:color-mix(in srgb,var(--c) 15%,transparent);color:var(--c)}
.fi svg{width:19px;height:19px}
.mid{min-width:0}
.t1{display:flex;align-items:center;gap:8px}.nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:-.01em}
.t2{color:var(--mut);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.prog{height:4px;border-radius:9px;background:var(--line2);margin-top:9px;position:relative;overflow:hidden}
.prog i{position:absolute;left:0;top:0;bottom:0;border-radius:9px}.prog .pre{background:color-mix(in srgb,var(--acc) 30%,transparent)}
.prog .bar{background:var(--acc)}.st-done .prog .bar{background:var(--green)}.st-error .prog .bar{background:var(--red)}.st-paused .prog .bar,.st-waiting .prog .bar{background:var(--amber)}.st-queued .prog .bar{background:var(--dim)}
.st-downloading .prog .bar:after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);animation:sh 1.6s infinite;transform:translateX(-100%)}@keyframes sh{to{transform:translateX(100%)}}
.st-done .prog{display:none}
.right{display:flex;align-items:center;gap:14px}
.stat{text-align:right;min-width:120px;font-variant-numeric:tabular-nums}.stat b{display:block;font-weight:620}.stat span{color:var(--mut);font-size:12px}
.quick{display:flex;gap:6px}.qb{display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 11px;border-radius:9px;border:1px solid var(--line2);background:var(--panel);color:var(--txt);cursor:pointer;font-weight:600;font-size:12.5px;white-space:nowrap;transition:all .12s}
.qb svg{width:14px;height:14px}.qb:hover{background:var(--panel2)}.qb.pri{background:var(--acc);border-color:transparent;color:#fff}.qb.pri:hover{filter:brightness(1.1)}
.acts{display:flex;gap:2px;opacity:.0;transition:opacity .15s}.item:hover .acts,.item.open .acts{opacity:1}
.ab{width:30px;height:30px;display:grid;place-items:center;border-radius:8px;border:0;background:transparent;color:var(--mut);cursor:pointer;transition:all .12s}
.ab:hover{background:var(--hover);color:var(--txt)}.ab svg{width:16px;height:16px}.ab.del:hover{color:var(--red)}.ab.hl{color:var(--acc2)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:0 8px;height:20px;border-radius:99px;font-size:11px;font-weight:600;flex:none;background:var(--panel2);color:var(--mut);border:1px solid var(--line)}
.pill i{width:6px;height:6px;border-radius:50%;background:currentColor}
.p-downloading{color:var(--green);background:rgba(48,196,123,.1);border-color:rgba(48,196,123,.25)}.p-downloading i{animation:pl 1.4s infinite}
.p-paused,.p-waiting{color:var(--amber);background:rgba(245,165,36,.1);border-color:rgba(245,165,36,.25)}
.p-error{color:var(--red);background:rgba(242,85,90,.1);border-color:rgba(242,85,90,.25)}
.p-joining,.p-verifying{color:var(--acc2);background:var(--accs);border-color:rgba(109,94,252,.3)}
.p-done{color:var(--green);background:transparent;border-color:transparent;padding:0}
.p-missing{color:var(--red);background:transparent;border-color:transparent;padding:0}
@keyframes pl{50%{opacity:.35}}
.msg{color:var(--amber)}
/* details */
.det{display:none;border-top:1px solid var(--line);padding:16px;gap:16px;grid-template-columns:1.25fr 1fr}
.item.open .det{display:grid}
.box{border:1px solid var(--line);border-radius:12px;padding:12px;background:var(--panel2)}
.box+.box{margin-top:12px}
.box h4{margin:0 0 10px;font-size:12px;font-weight:600;color:var(--mut);display:flex;justify-content:space-between}.box h4 span{color:var(--txt);font-weight:560}
.dcv{width:100%;height:120px;display:block}
.conn{display:grid;grid-template-columns:64px 1fr 36px 80px;gap:10px;align-items:center;padding:4px 0;font-size:12px;font-variant-numeric:tabular-nums}
.mb{height:4px;border-radius:9px;background:var(--line2);overflow:hidden}.mb i{display:block;height:100%;background:var(--acc);transition:width .5s linear}
.conn .s{text-align:right;color:var(--mut)}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(10px,1fr));gap:2px}
.g div{aspect-ratio:1;border-radius:2.5px;background:var(--line2);position:relative;overflow:hidden}
.g div i{position:absolute;left:0;right:0;bottom:0;background:var(--acc)}
.g .o{background:var(--green)}.g .k{background:var(--amber);animation:pl 1s infinite}.g .b{background:var(--red)}
.lg{display:flex;flex-wrap:wrap;gap:12px;color:var(--mut);font-size:11px;margin-top:9px}.lg span:before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;background:var(--c)}
.kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.kpi div{padding:8px;border-radius:10px;background:var(--panel);border:1px solid var(--line);text-align:center}.kpi b{display:block;font-size:18px;font-weight:700}.kpi small{color:var(--mut);font-size:11px}
.vfy{margin:10px 0;font-size:12px;display:flex;flex-direction:column;gap:6px}.vrow2{display:flex;gap:6px}.vrow2 .in{flex:1;min-width:0;font-size:12px}
.ev{max-height:220px;overflow:auto;display:flex;flex-direction:column;gap:3px;font-size:12px}
.ev div{display:flex;gap:9px;padding:4px 6px;border-radius:7px}.ev div:hover{background:var(--hover)}
.ev time{color:var(--dim);flex:none;font-variant-numeric:tabular-nums}.ev .f span{color:var(--red)}.ev .ok span{color:var(--green)}.ev .w span{color:var(--amber)}
.ev div:before{content:"";width:6px;height:6px;border-radius:50%;margin-top:6px;flex:none;background:var(--dim)}.ev .ok:before{background:var(--green)}.ev .f:before{background:var(--red)}.ev .w:before{background:var(--amber)}.ev .r:before{background:var(--acc)}
.info{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:12px}.info span:nth-child(odd){color:var(--mut)}.info span:nth-child(even){word-break:break-all}
.badge{display:inline-flex;align-items:center;padding:0 7px;height:18px;border-radius:99px;font-size:10.5px;font-weight:650}
.b-ok{background:rgba(48,196,123,.13);color:var(--green)}.b-bad{background:rgba(242,85,90,.13);color:var(--red)}.b-warn{background:rgba(245,165,36,.13);color:var(--amber)}.b-off{background:var(--panel2);color:var(--mut);border:1px solid var(--line)}.b-acc{background:var(--accs);color:var(--acc2)}
/* library */
.cats{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin-bottom:22px}
.cat{padding:14px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel);cursor:pointer;transition:all .15s;box-shadow:var(--shadow)}
.cat:hover{border-color:var(--line2);transform:translateY(-1px)}
.cat .fi{margin-bottom:12px}.cat b{display:block;font-weight:650}.cat span{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
.cat .mini{margin:10px 0 0}.cat .mini i{background:var(--c)}
.cat.zero{opacity:.45}
h2.sec{font-size:14px;margin:6px 0 10px;font-weight:650}
/* network page */
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.card{padding:16px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel);box-shadow:var(--shadow)}
.card h3{margin:0 0 12px;font-size:13px;font-weight:650;display:flex;align-items:center;gap:8px}.card h3 svg{width:16px;height:16px;color:var(--acc2)}.card h3 .badge{margin-left:auto}
.kv{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px dashed var(--line);font-size:12.8px}.kv:last-child{border:0}
.kv span:first-child{color:var(--mut)}.kv span:last-child{text-align:right;font-variant-numeric:tabular-nums;word-break:break-all}
.vrow{display:flex;align-items:center;gap:9px;padding:7px 0;border-bottom:1px dashed var(--line);font-size:12.8px}.vrow:last-child{border:0}
.vrow .vn{color:var(--mut);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stack{display:flex;height:10px;border-radius:9px;background:color-mix(in srgb,var(--green) 30%,transparent);overflow:hidden;margin:4px 0 12px}
.stack i{display:block;height:100%}.stack .s1{background:var(--dim)}.stack .s2{background:var(--acc)}
.sw{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:7px}
.bars{display:flex;align-items:flex-end;gap:5px;height:120px;margin-top:6px}
.bars div{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;justify-content:flex-end}
.bars i{width:100%;border-radius:5px 5px 2px 2px;background:var(--acc);min-height:2px;opacity:.85}.bars div:last-child i{opacity:1}
.bars span{font-size:9.5px;color:var(--dim)}
.note{color:var(--mut);font-size:11.8px;margin-top:6px}
/* empty */
.empty{display:grid;place-items:center;text-align:center;padding:70px 20px;color:var(--mut)}
.empty img{width:64px;height:64px;border-radius:18px;margin-bottom:16px;box-shadow:0 14px 40px -10px var(--acc)}
.empty h2{margin:0 0 6px;color:var(--txt);font-size:19px;letter-spacing:-.02em}
.tips{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap;justify-content:center}
.tips div{padding:10px 14px;border-radius:12px;border:1px solid var(--line);background:var(--panel);font-size:12.5px}
/*SUBCSS*/
.pwrap{position:relative;min-height:380px}.pwrap .smenu{font-size:14px;min-width:380px}
.ssearch{position:absolute;inset:4% 3%;z-index:7;display:none;flex-direction:column;border-radius:16px;background:rgba(16,16,22,.96);border:1px solid rgba(255,255,255,.12);color:#eee;box-shadow:0 24px 70px rgba(0,0,0,.6);overflow:hidden}
.ssearch.on{display:flex}.ssh{display:flex;gap:8px;align-items:center;padding:12px;border-bottom:1px solid rgba(255,255,255,.1)}
.ssh b{margin-right:6px;white-space:nowrap}.ssh .in{background:#1b1b22;border-color:#2c2c36;color:#eee;width:auto}.ssh #ssq{flex:1}
.ssnote{padding:8px 14px;color:#a0a0ad;font-size:12.5px}.sslist{flex:1;overflow:auto;padding:0 10px 10px}
.ssr{display:grid;grid-template-columns:56px 1fr auto;gap:12px;align-items:center;padding:10px;border-radius:12px;border:1px solid rgba(255,255,255,.07);margin-bottom:7px;background:rgba(255,255,255,.025)}
.ssr.best{border-color:#6d5efc;background:rgba(109,94,252,.12)}
.ssr .sc{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:14px;background:conic-gradient(var(--g) calc(var(--p)*1%),rgba(255,255,255,.08) 0)}
.ssr .sc span{width:42px;height:42px;border-radius:50%;background:#15151b;display:grid;place-items:center}
.ssr .rl{font:12.5px ui-monospace,Menlo,monospace;word-break:break-all;color:#fff}.ssr .mt{color:#9a9aa6;font-size:12px;margin-top:3px}
.ssr .mt b{color:#c9c4ff;font-weight:600}.ssr .nt{direction:rtl;text-align:right;color:#b8b8c4;font-size:12px;margin-top:3px}
.ssr .tag{display:inline-block;padding:0 7px;border-radius:99px;background:#6d5efc;color:#fff;font-size:11px;font-weight:700;margin-right:6px}
.pwrap:fullscreen .smenu{font-size:20px}
.qrbox{display:flex;gap:16px;align-items:center;margin:10px 0;padding:12px;border-radius:13px;border:1px solid var(--line2);background:var(--panel2)}
.qrbox img{width:156px;height:156px;border-radius:10px;background:#fff;padding:0;flex:none}
.qrbox div{font-size:12.5px;color:var(--mut);word-break:break-all}.qrbox b{color:var(--txt);font-size:14px}
.tvbox{border:1px solid var(--line2);border-radius:13px;padding:12px;margin:6px 0 10px;background:var(--panel2)}
.tvh{display:flex;align-items:center;gap:10px}.tvh .btn{margin-left:auto}
.tvr{display:flex;align-items:center;gap:10px;padding:9px 4px;border-top:1px dashed var(--line);margin-top:8px}
.tvr .ti{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;background:var(--accs);color:var(--acc2)}.tvr .ti svg{width:18px;height:18px}
.tvr .tn{flex:1;min-width:0}.tvr .tn b{display:block}.tvr .tn span{color:var(--mut);font-size:12px}
.tvr.off{opacity:.5}
.castbar{display:flex;align-items:center;gap:8px;margin-top:10px;padding:9px 11px;border-radius:11px;background:var(--accs)}
.castbar span{flex:1}
.gwhero{display:flex;gap:14px;align-items:center;padding:16px 18px;margin-bottom:12px}.gwhero h3{margin:0 0 4px;font-size:15px}
.gwhero .gwi{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;background:var(--panel2);color:var(--mut);flex:none}.gwhero .gwi svg{width:22px;height:22px}
.gwhero.ok .gwi{background:var(--accs);color:var(--acc2)}.gwhero.bad .gwi{background:rgba(242,85,90,.14);color:var(--red)}.gwhero.warn .gwi{background:rgba(245,165,36,.14);color:var(--amber)}
.gwhero.ok{border-color:rgba(109,94,252,.4);background:linear-gradient(135deg,var(--accs),transparent 60%),var(--panel)}
.tg{position:relative;width:46px;height:26px;flex:none}.tg input{opacity:0;width:0;height:0}.tg span{position:absolute;inset:0;border-radius:99px;background:var(--line2);transition:.2s;cursor:pointer}
.tg span:before{content:"";position:absolute;left:3px;top:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.tg input:checked+span{background:var(--acc)}.tg input:checked+span:before{transform:translateX(20px)}.tg input:disabled+span{opacity:.4;cursor:not-allowed}
.seg{display:inline-flex;padding:3px;border-radius:11px;background:var(--panel2);border:1px solid var(--line);gap:2px;flex:none}
.seg button{border:0;background:transparent;padding:6px 11px;border-radius:8px;cursor:pointer;color:var(--mut);font-weight:560;font-size:12.5px;white-space:nowrap}
.seg button:hover{color:var(--txt)}.seg button.on{background:var(--elev);color:var(--txt);box-shadow:0 1px 3px rgba(0,0,0,.2)}
.seg button.on.tunnel{background:var(--acc);color:#fff}.seg button.on.direct{background:var(--panel);color:var(--txt);border:1px solid var(--line2)}
.gwd{display:flex;align-items:center;gap:14px;padding:12px 6px;border-bottom:1px solid var(--line)}.gwd:last-child{border:0}
.gwd.new{background:linear-gradient(90deg,var(--accs),transparent);border-radius:12px}.gwn{flex:1;min-width:0}.gwn b{cursor:pointer}.gwn b:hover{text-decoration:underline dotted}
.lnk{color:var(--acc2);cursor:pointer;margin-left:6px}.cmd{display:flex;gap:8px;align-items:center;padding:10px 12px;border-radius:11px;background:#0d0d10;margin:6px 0}
.cmd code{flex:1;background:transparent;border:0;color:#e5e5ea;font-size:13px}
.steps{margin:6px 0 12px;padding-left:20px;color:var(--mut);font-size:12.8px}.steps li{margin:3px 0}
.gwvals{display:flex;flex-direction:column;gap:8px}.gwvals>div:not(.note){display:flex;justify-content:space-between;padding:10px 12px;border-radius:11px;background:var(--panel2);border:1px solid var(--line)}
.gres{margin-top:12px;border-color:rgba(34,197,94,.45);background:linear-gradient(180deg,rgba(34,197,94,.08),transparent 120px),var(--panel)}.gres.bad{border-color:rgba(239,68,68,.5);background:linear-gradient(180deg,rgba(239,68,68,.08),transparent 120px),var(--panel)}
.gresh{display:flex;gap:14px;align-items:center}.gresi{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;font-weight:800;font-size:19px;color:#fff;background:var(--green);box-shadow:0 6px 20px rgba(34,197,94,.35)}
.gres.bad .gresi{background:var(--red);box-shadow:0 6px 20px rgba(239,68,68,.35)}
.gchips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.gchip{padding:6px 12px;border-radius:999px;font-size:12.5px;font-weight:600;background:var(--panel2);border:1px solid var(--line2)}
.gchip.ok{color:var(--green);border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.08)}.gchip.acc{color:var(--acc2);border-color:var(--acc);background:var(--accs)}.gchip code{font-size:11.5px}
.gresg{display:grid;grid-template-columns:1fr 1.2fr;gap:18px}@media(max-width:900px){.gresg{grid-template-columns:1fr}}.gsub{display:block;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin-bottom:8px}
.gline{display:flex;gap:9px;padding:5px 0;font:12.5px/1.45 ui-monospace,Menlo,monospace;color:var(--mut)}.gline i{width:18px;height:18px;flex:none;border-radius:50%;display:grid;place-items:center;font-style:normal;font-size:10.5px;background:var(--line2);color:var(--txt)}
.gline.ok{color:var(--txt)}.gline.ok i{background:var(--green);color:#fff}.gline.bad{color:var(--red)}.gline.bad i{background:var(--red);color:#fff}
.grt{width:100%;border-collapse:collapse;font:12.5px ui-monospace,Menlo,monospace}.grt th{text-align:left;font:600 11px -apple-system,system-ui,sans-serif;color:var(--mut);padding:4px 8px;border-bottom:1px solid var(--line)}
.grt td{padding:5px 8px;border-bottom:1px solid var(--line)}.grt td:first-child{width:14px;font-weight:800}.grt tr.added td{color:var(--green);background:rgba(34,197,94,.07)}.grt tr.removed td{color:var(--red);text-decoration:line-through;background:rgba(239,68,68,.06)}
.gwpre{margin:12px 0 0;padding:12px 14px;max-height:420px;overflow:auto;border-radius:12px;background:#0d0d10;color:#d4d4d8;border:1px solid var(--line);font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap}
.gwvals span{color:var(--mut)}.gwvals b{font:600 14px ui-monospace,Menlo,monospace}
.tvr.sel{background:var(--accs);border-radius:12px;padding-left:8px;padding-right:8px}.tvr{cursor:pointer}
.remote{display:flex;flex-direction:column;gap:10px;align-items:center}.rrow{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.rk{min-width:62px;height:42px;padding:0 12px;border-radius:12px;border:1px solid var(--line2);background:var(--panel2);color:var(--txt);cursor:pointer;font-weight:650;font-size:13px;transition:all .1s}
.rk:hover{background:var(--hover);border-color:var(--acc)}.rk:active{transform:scale(.94)}
.dpad{display:grid;grid-template-columns:repeat(3,62px);grid-template-rows:repeat(3,52px);gap:6px;grid-template-areas:". u ." "l ok r" ". d ."}
.dpad .u{grid-area:u}.dpad .l{grid-area:l}.dpad .r{grid-area:r}.dpad .d{grid-area:d}.dpad .rk{height:auto;width:100%}
.dpad .ok{grid-area:ok;border-radius:50%;background:var(--acc);color:#fff;border-color:transparent}
.rk.red{background:#e53935;min-width:50px}.rk.green{background:#43a047;min-width:50px}.rk.yellow{background:#fdd835;min-width:50px}.rk.blue{background:#1e88e5;min-width:50px}.rk.red2{color:var(--red)}
.rwrap{display:grid;grid-template-columns:300px 1fr;gap:22px;align-items:start}
@media(max-width:900px){.rwrap{grid-template-columns:1fr}}
.mr{position:relative;width:206px;margin:0 auto;padding:22px 0 30px;border-radius:44px;background:linear-gradient(90deg,#262628,#38383b 18%,#3a3a3d 50%,#353538 82%,#252527);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12),inset 0 -2px 0 rgba(0,0,0,.4),0 30px 70px rgba(0,0,0,.4),0 3px 8px rgba(0,0,0,.35);display:flex;flex-direction:column;align-items:center;color:#f1f1f3;user-select:none;-webkit-user-select:none}
.mr .g{display:grid;grid-template-columns:44px 44px 44px;column-gap:16px;justify-content:center;align-items:start}
.mk{position:relative;width:42px;height:42px;border-radius:50%;border:0;display:grid;place-items:center;cursor:pointer;color:#f4f4f6;padding:0;
  background:radial-gradient(circle at 50% 35%,#323235,#232325 72%);box-shadow:inset 0 1px 0 rgba(255,255,255,.1),0 2px 4px rgba(0,0,0,.5);transition:transform .1s,filter .2s}
.mk svg{width:20px;height:20px}.mk:hover{filter:brightness(1.35)}.mk:active,.mk.hit{transform:scale(.9)}
.mk.pw{background:radial-gradient(circle at 50% 35%,#ff3b3b,#d4121a 75%);color:#fff;box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 2px 6px rgba(212,18,26,.45)}
.mk .lb{position:absolute;top:44px;left:50%;transform:translateX(-50%);font:600 7.5px/1 -apple-system,system-ui,sans-serif;letter-spacing:.06em;color:#c9c9cf;white-space:nowrap;pointer-events:none}
.mk.ai{margin-top:21px}.mk.ai .lb{font-size:8.5px;font-weight:700}
.mk.num{font:800 10px/1 -apple-system,system-ui,sans-serif;letter-spacing:.06em}.mk.num small{display:block;font-size:9px;letter-spacing:.1em;margin-top:-2px}
.mr .mic{display:flex;gap:3px;align-items:center;margin-top:-4px}.mr .mic i{width:2.5px;height:2.5px;border-radius:50%;background:#18181a}.mr .mic b{width:4px;height:4px;border-radius:50%;background:#161618;margin-left:9px}
.mring{position:relative;width:132px;height:132px;border-radius:50%;margin:20px 0 14px;background:radial-gradient(circle at 50% 40%,#262628,#18181a 70%);
  box-shadow:inset 0 2px 3px rgba(0,0,0,.7),inset 0 -1px 0 rgba(255,255,255,.08),0 1px 0 rgba(255,255,255,.06)}
.mring button{position:absolute;border:0;background:none;padding:0;cursor:pointer;color:transparent;display:grid;place-items:center;transition:color .15s,background .15s}
.mring button svg{width:14px;height:14px}.mring:hover button{color:rgba(255,255,255,.35)}.mring button:hover{color:#fff;background:rgba(255,255,255,.05)}.mring button:active,.mring button.hit{background:rgba(139,123,255,.3);color:#fff}
.mring .u{left:36px;top:0;width:60px;height:36px;border-radius:60px 60px 0 0}.mring .d{left:36px;bottom:0;width:60px;height:36px;border-radius:0 0 60px 60px}
.mring .l{left:0;top:36px;width:36px;height:60px;border-radius:60px 0 0 60px}.mring .r{right:0;top:36px;width:36px;height:60px;border-radius:0 60px 60px 0}
.mring .ok{left:50%;top:50%;width:44px;height:54px;margin:-27px 0 0 -22px;border-radius:16px;background:linear-gradient(180deg,#3a3a3e,#29292c);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 2px 5px rgba(0,0,0,.6)}
.mring .ok:after{content:"";position:absolute;left:12px;right:12px;top:9px;bottom:9px;border-radius:8px;background:repeating-linear-gradient(180deg,#1b1b1d 0 2px,#3c3c40 2px 4px);box-shadow:inset 0 0 4px rgba(0,0,0,.7)}
.mring .ok:hover{background:linear-gradient(180deg,#8b7bff,#5b4be0)}
.nump{display:grid;grid-template-columns:repeat(3,36px);gap:7px;justify-content:center;margin:18px 0 12px;padding:10px;border-radius:24px;background:#1c1c1e}
.nump .mk{width:36px;height:30px;border-radius:10px;font:700 13px/1 -apple-system,system-ui,sans-serif}
.rck{display:flex;gap:2px;margin:6px 0 2px}.rck .bar{display:flex;flex-direction:column;align-items:center;width:62px}
.rck .bar .t,.rck .bar .b{height:24px;width:62px;border:0;background:none;color:#f1f1f3;cursor:pointer;display:grid;place-items:center;padding:0}
.rck .bar .t svg,.rck .bar .b svg{width:13px;height:13px}.rck .bar .t:hover,.rck .bar .b:hover{color:#b9b0ff}
.rck .bar .t:active,.rck .bar .b:active,.rck .bar .hit{color:#8b7bff}
.rck .bar .pill{width:58px;height:16px;border-radius:8px;background:linear-gradient(180deg,#36363a,#1f1f22);box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 2px 4px rgba(0,0,0,.6)}
.rck .bar .b{display:flex;gap:6px;justify-content:center;align-items:center}.rck .bar .b .mu{opacity:.9}
.aps{display:flex;flex-direction:column;gap:9px;margin-top:10px}
.aps .ap{display:flex;width:122px;height:23px;border-radius:12px;background:linear-gradient(180deg,#2e2e31,#212124);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 2px 4px rgba(0,0,0,.5);overflow:hidden}
.aps .ap button{flex:1;border:0;background:none;color:#f1f1f3;cursor:pointer;padding:0;font:700 9px/1 -apple-system,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;gap:3px;transition:background .15s}
.aps .ap button:hover{background:rgba(255,255,255,.08)}.aps .ap button:active,.aps .ap button.hit{background:rgba(139,123,255,.3)}
.aps .ap i{width:1px;margin:6px 0;background:#5a5a60}
.aps .ap .nf{color:#e50914;font:900 9.5px/1 "Arial Narrow",Arial,sans-serif;letter-spacing:.02em;transform:scaleY(1.15)}
.aps .pv{flex-direction:column;gap:0;font:700 8.5px/1 -apple-system,system-ui,sans-serif;letter-spacing:-.01em}.aps .pv:after{content:"";width:26px;height:4px;margin-top:-1px;border-bottom:1.5px solid #fff;border-radius:0 0 14px 14px}
.aps .dp{font:italic 800 9.5px/1 Georgia,serif}.aps .lgc{font-weight:500;font-size:8.5px}
.aps .ap .yt b{width:12px;height:8.5px;border-radius:2.5px;background:#ff0033;display:grid;place-items:center}.aps .yt b:after{content:"";border-left:3.5px solid #fff;border-top:2.2px solid transparent;border-bottom:2.2px solid transparent;margin-left:1px}
.aps .dl b{width:10px;height:10px;border-radius:3px;background:linear-gradient(135deg,#9b8cff,#3f6bff)}
.mr .logo{margin-top:auto;padding-top:92px;font:800 12px/1 -apple-system,system-ui,sans-serif;letter-spacing:.08em;color:#8d8d95;display:flex;align-items:center;gap:5px}
.mr .logo b{width:18px;height:18px;border-radius:50%;background:#8d8d95;color:#2f2f32;display:grid;place-items:center;font-size:8px}
.heart{display:flex;gap:8px;align-items:center}
.mr.keys{box-shadow:0 0 0 3px var(--acc),0 30px 70px rgba(0,0,0,.4)}.mr.keys:after{content:"Keyboard \2192  TV";position:absolute;top:-30px;left:50%;transform:translateX(-50%);white-space:nowrap;
  padding:4px 10px;border-radius:999px;background:var(--acc);color:#fff;font:700 11px -apple-system,system-ui,sans-serif}
.rside{display:flex;flex-direction:column;gap:14px}
.rnow{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:14px;background:var(--panel2);border:1px solid var(--line)}
.rnow .vol{flex:1;display:flex;gap:10px;align-items:center}.rnow input[type=range]{flex:1;accent-color:var(--acc)}
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:10px;max-height:300px;overflow:auto;padding:2px}
.agrid button{display:flex;flex-direction:column;align-items:center;gap:6px;padding:10px 6px;border-radius:14px;border:1px solid var(--line);background:var(--panel2);color:var(--txt);cursor:pointer;font-size:11.5px;transition:all .12s}
.agrid button:hover{border-color:var(--acc);transform:translateY(-2px)}.agrid img{width:56px;height:56px;border-radius:12px;object-fit:cover;background:#222}
.chips{display:flex;flex-wrap:wrap;gap:8px}.chips button{padding:8px 14px;border-radius:999px;border:1px solid var(--line2);background:var(--panel2);color:var(--txt);cursor:pointer;font-size:12.5px}
.chips button:hover{border-color:var(--acc)}.chips button.dim{opacity:.55}
.kbd{display:flex;gap:8px}.kbd input{flex:1}
.gww{display:grid;grid-template-columns:1.2fr 1fr;gap:18px;margin-top:14px}@media(max-width:760px){.gww{grid-template-columns:1fr}}
.gck{display:flex;gap:11px;align-items:flex-start;padding:9px 4px;border-bottom:1px solid var(--line);font-size:13.5px}.gck:last-child{border:0}
.gck i{width:22px;height:22px;border-radius:50%;flex:none;display:grid;place-items:center;font-style:normal;font-size:12px;font-weight:800;color:#fff;background:var(--line2)}
.gck.ok i{background:var(--green)}.gck.no{color:var(--mut)}.gck.no i{background:none;border:2px solid var(--line2);box-sizing:border-box;animation:gpulse 1.4s infinite}
@keyframes gpulse{50%{border-color:var(--acc)}}
.gdone{display:flex;gap:12px;align-items:center;padding:14px 16px;border-radius:14px;background:linear-gradient(90deg,rgba(34,197,94,.18),transparent);border:1px solid rgba(34,197,94,.4);font-weight:650;margin-top:12px}
.bigcode{font:700 22px ui-monospace,Menlo,monospace;letter-spacing:.18em;padding:2px 10px;border-radius:8px;background:var(--accs);color:var(--acc2)}
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.fcard{padding:16px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel);box-shadow:var(--shadow)}.fcard.off{opacity:.65}.fcard.cur{border-color:var(--acc)}
.fh{display:flex;gap:8px;align-items:center}.fh b{font-size:15px;margin-right:auto}
.fprev{font-size:27px;line-height:1.5;margin:12px 0 14px;min-height:42px;color:var(--txt)}.fb{display:flex;gap:8px}
.htab{width:100%;border-collapse:collapse;font-size:12.5px}.htab th{text-align:left;color:var(--mut);font-weight:560;padding:9px 12px;border-bottom:1px solid var(--line);background:var(--panel2)}
.htab td{padding:8px 12px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}.htab td.ht{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* modals, palette */
.modal{position:fixed;inset:0;z-index:20;display:none;place-items:center;padding:20px;background:rgba(0,0,0,.45);backdrop-filter:blur(4px)}
.modal.on{display:grid;animation:fade .15s}@keyframes fade{from{opacity:0}}
.dlg{width:min(600px,100%);max-height:90vh;overflow:auto;padding:20px;border-radius:16px;background:var(--elev);border:1px solid var(--line2);box-shadow:0 30px 80px rgba(0,0,0,.45);animation:pop .18s cubic-bezier(.2,.8,.2,1)}
@keyframes pop{from{transform:translateY(6px) scale(.98);opacity:.5}}
.dlg.wide{width:min(1000px,100%)}
.dlg h2{margin:0 0 4px;font-size:17px;letter-spacing:-.02em}.dlg .lead{color:var(--mut);font-size:12.8px;margin-bottom:12px}
label{display:block;color:var(--mut);font-size:12px;font-weight:560;margin:12px 0 5px}
.in{width:100%;padding:8px 11px;border-radius:9px;border:1px solid var(--line2);background:var(--panel);outline:none;transition:border-color .15s,box-shadow .15s}
.in:focus{border-color:var(--acc);box-shadow:0 0 0 4px var(--accs)}
textarea.in{min-height:110px;resize:vertical;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.ft{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:18px;flex-wrap:wrap}.ft .l{margin-right:auto;color:var(--mut);font-size:12px}
.ck{display:flex;align-items:center;gap:8px;color:var(--txt);font-weight:500;margin-top:12px;cursor:pointer}
.ck input{width:16px;height:16px;accent-color:var(--acc)}
.hint{color:var(--mut);font-size:11.8px;margin-top:5px}
code{font:12px ui-monospace,Menlo,monospace;background:var(--panel2);border:1px solid var(--line);padding:0 5px;border-radius:5px}
.cmdk{position:fixed;inset:0;z-index:30;display:none;justify-content:center;align-items:flex-start;padding-top:12vh;background:rgba(0,0,0,.35);backdrop-filter:blur(3px)}
.cmdk.on{display:flex}
.cmdbox{width:min(620px,92vw);border-radius:16px;background:var(--elev);border:1px solid var(--line2);box-shadow:0 30px 90px rgba(0,0,0,.5);overflow:hidden;animation:pop .15s}
.cmdbox input{width:100%;padding:16px 18px;border:0;border-bottom:1px solid var(--line);background:transparent;outline:none;font-size:15px}
#cmdl{max-height:52vh;overflow:auto;padding:6px}
#cmdl h6{margin:8px 10px 4px;font-size:11px;color:var(--dim);font-weight:600}
#cmdl a{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:9px;cursor:pointer}
#cmdl a svg{width:16px;height:16px;color:var(--mut)}#cmdl a.sel{background:var(--accs)}#cmdl a kbd{margin-left:auto}#cmdl a small{margin-left:auto;color:var(--dim)}
video,audio{width:100%;max-height:62vh;background:#000;display:block;outline:none}
.pwrap{border-radius:12px;overflow:hidden;background:#000}.pwrap:fullscreen video{max-height:calc(100vh - 52px);height:calc(100vh - 52px)}
.pctl{display:none;align-items:center;gap:10px;padding:8px 12px;background:#0d0d10;color:#e5e5ea}
.pctl #ptime{font-variant-numeric:tabular-nums;font-size:12px;min-width:104px}.pctl input[type=range]{flex:1;accent-color:var(--acc)}
.pctl select{width:auto;max-width:230px;padding:5px 8px;background:#18181c;border-color:#2a2a30;color:#e5e5ea}
.pctl .ab{color:#e5e5ea}
.toast{position:fixed;left:50%;bottom:22px;z-index:40;transform:translate(-50%,20px);opacity:0;pointer-events:none;display:flex;align-items:center;gap:9px;padding:10px 14px;border-radius:12px;background:var(--txt);color:var(--bg);font-weight:560;box-shadow:0 12px 30px rgba(0,0,0,.3);transition:all .22s cubic-bezier(.2,.8,.2,1)}
.toast.on{opacity:1;transform:translate(-50%,0)}
.off{position:fixed;top:0;left:0;right:0;z-index:50;display:none;padding:7px;text-align:center;background:var(--red);color:#fff;font-weight:600}
.drop{position:fixed;inset:10px;z-index:45;display:none;place-items:center;border:2px dashed var(--acc);border-radius:20px;background:var(--accs);font-size:20px;font-weight:650;backdrop-filter:blur(3px)}
.hide{display:none!important}
@media(max-width:1100px){.bento{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}}
@media(max-width:1180px){.quick .qb{padding:0 8px}.quick .qb:not(.pri) svg{display:none}}
@media(max-width:860px){.app{grid-template-columns:1fr}.sb{position:static;height:auto}.det{grid-template-columns:1fr}.stat{display:none}.acts{opacity:1}}
</style></head><body>
<div class=off id=off>dlus is not running &mdash; start it again; downloads continue where they stopped</div>
<div class=drop id=drop>Drop links to download</div>
<div class=app>
<aside class=sb>
 <div class=brand onclick="openAbout()" title="About dlus"><img src="/logo.svg" alt=""><b>dlus</b><span id=ver></span></div>
 <button class=new onclick="openAdd()"><svg width=15 height=15 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2.6 stroke-linecap=round><path d="M12 5v14M5 12h14"/></svg>New download<kbd>&#8997;N</kbd></button>
 <nav id=nav></nav>
 <div class=foot id=sbfoot></div>
</aside>
<main>
 <div class=top>
  <label class=search><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round><circle cx=11 cy=11 r=7 /><path d="m20 20-3.5-3.5"/></svg>
   <input id=q placeholder="Search downloads, or paste a link to add it" autocomplete=off><kbd>&#8984;K</kbd></label>
  <div class=speed title="Total speed"><canvas id=gcv width=180 height=48></canvas><b id=gnow>0.00</b><span>MB/s</span></div>
  <div class=bellw><button class=ibtn id=bell title="Notifications" onclick="noteToggle(event)"><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg><span id=belln class=belln></span></button>
   <div id=npanel class=npanel onclick="event.stopPropagation()"><div class=nph><b>Notifications</b><button class=btn onclick="noteClear()">Clear all</button></div><div id=nlist></div></div></div>
  <button class=ibtn id=thbtn title="Theme" onclick="cycleTheme()"></button>
  <button class=ibtn title="Settings (,)" onclick="openSet()"><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round><circle cx=12 cy=12 r=3 /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></button>
 </div>
 <div id=accessban></div>
 <div id=banners></div>
 <section id=v-list>
  <div class=head><h1 id=vtitle>All downloads</h1><span class=cnt id=vcount></span><div class=tools id=vtools></div></div>
  <div class=bento id=bento></div>
  <div id=list></div>
 </section>
 <section id=v-lib class=hide>
  <div class=head><h1>Library</h1><span class=cnt id=libsum></span></div>
  <div class=cats id=cats></div>
  <h2 class=sec>Recently finished</h2>
  <div id=recent></div>
 </section>
 <section id=v-dev class=hide>
  <div class=head><h1>Devices &amp; viewers</h1><span class=cnt id=devsum></span></div>
  <div class=bento id=devbento style="grid-template-columns:2fr 1fr 1fr 1fr"></div>
  <h2 class=sec>Watching now</h2><div id=devlive></div>
  <h2 class=sec>Seen in the last hour</h2><div id=devseen></div>
  <h2 class=sec>Play history</h2><div class=card style="padding:0;overflow:hidden"><table class=htab id=devhist></table></div>
  <h2 class=sec>Allowed devices</h2><div class=note style="margin-top:0">Only these devices can open the TV home page, streams, shared folders, the library (DLNA) and the proxy. A new device must be allowed here first, even with the right code.</div><div id=devallowed></div>
  <h2 class=sec>Activity log</h2><div class=card style="padding:0;overflow:hidden;max-height:520px;overflow-y:auto"><table class=htab id=devaudit></table></div>
 </section>
 <section id=v-fonts class=hide>
  <div class=head><h1>Fonts</h1><span class=cnt>for subtitles in the dlus players (Mac and TV)</span><div class=tools><button class="btn pri" onclick="$('fontfile').click()">Add your own font&hellip;</button></div></div>
  <input type=file id=fontfile accept=".ttf,.otf,.woff,.woff2" style="display:none">
  <div class="ban info" style="margin-bottom:14px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg><div>These fonts are used by the <b>dlus player</b> (on the Mac and on the TV) &mdash; pick one in the subtitle menu (TV: RED &rarr; Font). The TV's <b>own</b> player has only LG's built-in fonts (there, Font 1 is the one with Persian letters); LG does not allow adding fonts to it.</div></div>
  <div class=fgrid id=fgrid></div>
 </section>
 <section id=v-tv class=hide>
  <div class=head><h1>TV &amp; remote</h1><span class=cnt>play on your TV, control it, connect it with a code</span></div>
  <div id=tvpage></div>
 </section>
 <section id=v-gw class=hide>
  <div class=head><h1>VPN router</h1><span class=cnt>choose per device: through the tunnel or direct</span></div>
  <div id=gwbody></div>
 </section>
 <section id=v-share class=hide>
  <div class=head><h1>Sharing</h1><span class=cnt>with other devices on this network</span></div>
  <div class=grid id=sharegrid></div>
 </section>
 <section id=v-net class=hide>
  <div class=head><h1>Network &amp; storage</h1></div>
  <div class=grid id=netgrid></div>
 </section>
 <div class="empty hide" id=empty><img src="/logo.svg" alt=""><h2 id=etitle>Welcome to dlus</h2><div>Downloads over many connections, checks every piece, never starts over.</div>
  <div class=tips><div><kbd>&#8984;</kbd> <kbd>V</kbd> paste a link anywhere</div><div><kbd>&#8997;</kbd> <kbd>N</kbd> new download</div><div><kbd>&#8984;</kbd> <kbd>K</kbd> command menu</div><div>drop links on the page</div></div>
  <div style="margin-top:20px"><button class="btn pri" onclick="openAdd()">New download</button></div></div>
</main>
</div>

<div class=cmdk id=cmdk onclick="if(event.target==this)closeCmd()"><div class=cmdbox><input id=cmdq placeholder="Type a command or search downloads..." autocomplete=off><div id=cmdl></div></div></div>

<div class=modal id=madd onclick="if(event.target==this)closeM()"><div class=dlg>
 <h2>New download</h2><div class=lead>Paste one or more links &mdash; any text works, links are detected.</div>
 <textarea class=in id=aurls placeholder="https://example.com/file.mkv"></textarea>
 <div style="display:flex;gap:8px;margin-top:8px;align-items:center"><button class=btn onclick="pasteClip()">Paste from clipboard</button><span class=hint id=acount style="margin:0"></span></div>
 <div class=two>
  <div><label>Folder</label><input class=in id=afolder list=folders placeholder="Automatic, by file type"><datalist id=folders></datalist>
   <div class=hint>A name creates a folder in your download folder; <code>~/Desktop</code> or <code>/path</code> is used as is.</div></div>
  <div><label>Connections per file</label><select class=in id=aconns><option>1</option><option>2</option><option>4</option><option selected>8</option><option>12</option><option>16</option></select></div>
 </div>
 <label>Checksum <span style="color:var(--dim)">(optional, single link: md5 / sha1 / sha256)</span></label>
 <input class=in id=acs placeholder="e.g. sha256:ab12...">
 <label class=ck><input type=checkbox id=astart checked> Start now</label>
 <div class=ft><span class=l id=adir></span><button class=btn onclick="closeM()">Cancel</button><button class="btn pri" onclick="doAdd()">Add <kbd style="background:rgba(255,255,255,.2);border:0;color:#fff">&#8984;&#9166;</kbd></button></div>
</div></div>

<div class=modal id=mset onclick="if(event.target==this)closeM()"><div class=dlg>
 <h2>Settings</h2><div class=lead>Applies right away. Running downloads keep their data.</div>
 <label>Download folder</label><input class=in id=sdir>
 <label>Open dlus in</label><select class=in id=suiopen><option value=app>its own window (default)</option><option value=browser>my web browser</option></select>
 <div class=hint>For the dlus app in Applications. Hold <kbd>&#8997;</kbd> while opening dlus to get the window once anyway.</div>
 <div class=two>
  <div><label>Downloads at the same time</label><input class=in id=smax type=number min=1 max=20></div>
  <div><label>Connections per file (default)</label><input class=in id=sconns type=number min=1 max=32></div>
 </div>
 <div class=two><div><label>Total speed limit, MB/s (0 = unlimited)</label><input class=in id=slim type=number min=0 step=0.1></div>
  <div><label>While a TV or device watches: limit to MB/s</label><input class=in id=swatch type=number min=0 step=0.5></div></div>
 <div class=hint>Streaming has priority: while anything plays on your TV/phone over the network, downloads drop to this speed (0 = no limit), then go back to full speed.</div>
 <label class=ck><input type=checkbox id=son> Only download between</label>
 <div class=two style="margin-top:6px"><input class=in id=sfrom type=time><input class=in id=sto type=time></div>
 <div class=hint>e.g. 02:00&ndash;08:00 for cheaper night internet. &ldquo;Start now&rdquo; ignores the schedule.</div>
 <div class=two>
  <div><label>When the queue is finished</label><select class=in id=sdone><option value=none>Do nothing</option><option value=sleep>Put the Mac to sleep</option></select></div>
  <div><label>&nbsp;</label><label class=ck style="margin-top:4px"><input type=checkbox id=snot> macOS notifications</label></div>
 </div>
 <div class=ft><button class=btn onclick="closeM()">Cancel</button><button class="btn pri" onclick="doSet()">Save</button></div>
</div></div>

<div class=modal id=mdel onclick="if(event.target==this)closeM()"><div class=dlg>
 <h2>Delete download?</h2><div class=lead id=delname style="word-break:break-all"></div>
 <div class=ft><button class=btn onclick="closeM()">Cancel</button><button class=btn onclick="doDel(false)">Remove from list</button><button class="btn danger" onclick="doDel(true)">Delete with files</button></div>
</div></div>

<div class=modal id=mref onclick="if(event.target==this)closeM()"><div class=dlg>
 <h2>Refresh link</h2><div class=lead>Paste a new link for the same file (e.g. the old one expired). Size and ETag are checked, so two different files can never be mixed.</div>
 <input class=in id=rurl placeholder="https://...">
 <div class=ft><button class=btn onclick="closeM()">Cancel</button><button class="btn pri" onclick="doRef()">Use this link</button></div>
</div></div>

<div class=modal id=mplay onclick="if(event.target==this)closePlay()"><div class="dlg wide">
 <h2 id=ptitle>Play</h2><div class=lead id=phint></div>
 <div class=pwrap id=pwrap><div id=pbox></div><div id=psubs class=subs></div><div id=psmenu class=smenu></div>
  <div id=ssearch class=ssearch><div class=ssh><b>Find subtitles</b><select class=in id=sslang><option value=fa>Persian</option><option value=en>English</option><option value=ar>Arabic</option><option value=tr>Turkish</option><option value=fr>French</option><option value=es>Spanish</option><option value=de>German</option></select>
   <input class=in id=ssq placeholder="title year"><button class="btn pri" onclick="subSearch()">Search</button><button class=btn onclick="subSearchClose()">&times;</button></div>
   <div class=ssnote id=ssnote></div><div id=sslist class=sslist></div></div>
  <div class=pctl id=pctl><button class=ab id=ppbtn onclick="pToggle()"></button><span id=ptime>0:00 / 0:00</span>
   <input type=range id=pseek min=0 max=1000 value=0><select class=in id=ptrack title="Audio track"></select>
   <button class=ab title=Fullscreen onclick="pFull()"><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button></div></div>
 <div class=ft><span class=l id=pavail></span><button class="btn pri" onclick="pSubMenu()" title="Subtitles (C)">CC Subtitles</button><span id=pmode></span><span id=papps></span><button class=btn onclick="copyStream()">Copy stream link</button><button class=btn onclick="closePlay()">Close</button></div>
</div></div>

<div class=modal id=mshare onclick="if(event.target==this)closeM()"><div class=dlg>
 <h2>Watch on TV or another device</h2><div class=lead id=shint></div>
 <div class=tvbox><div class=tvh><b>Play on a TV</b><span class=hint id=tvstat style="margin:0"></span><button class=btn id=tvfind onclick="tvFind()">Find TVs</button></div><div id=tvlist></div><div id=tvctl></div></div>
 <label class=ck><input type=checkbox id=slan onchange="lanSet()"> Share on my local network (same Wi-Fi)</label>
 <label class=ck><input type=checkbox id=sdlna onchange="lanSet()"> Show &ldquo;dlus&rdquo; on TVs automatically (DLNA)</label>
 <div id=qrbox class="qrbox hide"><img id=qrimg alt="QR code"><div id=qrcap></div></div>
 <div id=surls></div>
 <div class=ft><button class=btn onclick="lanRotate()" title="Old links stop working">New secret link</button><button class="btn pri" onclick="closeM()">Done</button></div>
</div></div>

<div class=modal id=mbrowse onclick="if(event.target==this)closeM()"><div class="dlg wide">
 <h2>Shared folder</h2><div class=lead id=brpath></div><div id=brlist style="max-height:62vh;overflow:auto"></div>
 <div class=ft><span class=l>&ldquo;On TV&rdquo; opens it in the dlus player on your TV &mdash; with subtitles, quality and sound modes.</span><button class="btn pri" onclick="closeM()">Close</button></div>
</div></div>
<div class=modal id=mgw onclick="if(event.target==this)closeM()"><div class=dlg style="max-width:820px">
 <h2>Connect a device to the VPN</h2><div class=lead>dlus sets up this Mac, helps on the TV and checks everything live.</div>
 <div style="display:flex;gap:10px;align-items:center"><select class=in id=gwwdev style="flex:1"></select><button class="btn pri" id=gwwgo onclick="gwwStart()">Start</button></div>
 <div id=gwwbody></div>
 <div class=ft><button class=btn onclick="closeM()">Close</button></div>
</div></div>
<div class=modal id=mtv onclick="if(event.target==this)closeM()"><div class=dlg>
 <h2 id=tvtitle>Play on TV</h2><div class=lead>dlus finds TVs on your network. <b>dlus player</b> opens on the TV with seeking, a downloaded-so-far bar, subtitles, fonts and sound modes. <b>TV's own player</b> uses the TV's built-in player.</div>
 <div class=tvh style="margin-bottom:6px"><span class=hint id=tvpstat style="margin:0"></span><button class=btn style="margin-left:auto" id=tvpfind onclick="tvpFind()">Find TVs</button></div>
 <div id=tvplist></div>
 <div class=note id=tvpnote style="margin-top:10px"></div>
 <div class=ft><button class="btn pri" onclick="closeM()">Close</button></div>
</div></div>
<div class=modal id=mabout onclick="if(event.target==this)closeM()"><div class=dlg>
 <div style="display:flex;gap:14px;align-items:center;margin-bottom:14px"><img src="/logo.svg" style="width:52px;height:52px;border-radius:14px"><div><h2 style="margin:0">dlus <span id=aver style="font-size:12px;color:var(--mut);font-weight:560"></span></h2><div class=lead style="margin:0">downloads, streams and controls your LG TV</div></div></div>
 <div class=info style="font-size:12.8px;gap:8px 18px">
  <span><kbd>&#8984;K</kbd></span><span>Command menu &mdash; every action and download</span>
  <span><kbd>&#8997;N</kbd></span><span>New download (in the app also <kbd>&#8984;N</kbd>)</span>
  <span><kbd>&#8984;V</kbd></span><span>Paste a link anywhere to add it</span>
  <span><kbd>&#8997;F</kbd></span><span>Search (in the app also <kbd>&#8984;F</kbd>)</span>
  <span><kbd>&#8997;1</kbd>&ndash;<kbd>&#8997;0</kbd></span><span>All &middot; Active &middot; Queue &middot; Finished &middot; Library &middot; Network &middot; Devices &middot; Sharing &middot; VPN router &middot; TV</span>
  <span><kbd>&#8997;T</kbd></span><span>Switch theme</span>
  <span><kbd>&#8997;,</kbd></span><span>Settings (in the app <kbd>&#8984;,</kbd>)</span>
 </div>
 <div class=hint style="margin-top:14px">Every 256 MB chunk is checked against the server at 3 points; a resumed chunk is checked where it continues; a bad chunk is re-downloaded alone; cut connections keep their data. Data folder: <code id=adir2></code></div>
 <div class=ft><button class="btn pri" onclick="closeM()">Close</button></div>
</div></div>
<div class=toast id=toast></div>

<input type=file id=subfile accept=".srt,.ass,.ssa,.vtt,.sub,.zip" style="display:none">
<script>
/*SUBJS*/
var ST = null, OPEN = {}, DET = {}, D = {}, VIEW = "all", CUR = null, G = 0, PLAYING = null, PL = null, SEEKING = false, SHARE = null;
var BROWSER_OK = { mp4: 1, m4v: 1, webm: 1, mov: 1, mp3: 1, m4a: 1, aac: 1, wav: 1, ogg: 1, opus: 1, flac: 1 };
function sv(p, extra) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"' + (extra || "") + '>' + p + '</svg>' }
var I = {
  all: sv('<path d="M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5"/><path d="M5 19h14"/>'),
  active: sv('<path d="M3 12h4l3-8 4 16 3-8h4"/>'),
  queue: sv('<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>'),
  paused: sv('<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>'),
  done: sv('<path d="M20 6 9 17l-5-5"/>'),
  error: sv('<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>'),
  lib: sv('<path d="M4 5a1 1 0 0 1 1-1h4l2 2h8a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"/>'),
  net: sv('<path d="M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M2 9a15 15 0 0 1 20 0"/><path d="M12 19.5h.01"/>'),
  play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15l13-7.5z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  tv: sv('<rect x="3" y="5" width="18" height="13" rx="2"/><path d="m10 9 5 2.5-5 2.5z"/>'),
  cast: sv('<path d="M3 8V6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6M3 12a8 8 0 0 1 8 8M3 16a4 4 0 0 1 4 4M3 20h.01"/>'),
  folder: sv('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  link: sv('<path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>'),
  del: sv('<path d="M4 7h16M10 11v6M14 11v6M9 7V4h6v3M6 7l1 13h10l1-13"/>'),
  up: sv('<path d="m6 15 6-6 6 6"/>'), down: sv('<path d="m6 9 6 6 6-6"/>'),
  now: sv('<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>'),
  sun: sv('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  moon: sv('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'),
  auto: sv('<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>'),
  plus: sv('<path d="M12 5v14M5 12h14"/>'), gear: sv('<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>'),
  vpn: sv('<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/>'), disk: sv('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14h18M7 17h.01"/>'),
  chart: sv('<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'), help: sv('<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17h.01"/>'),
  warn: sv('<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17h.01"/>'), info: sv('<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>')
};
var CAT = {
  Video: ["#8b5cf6", '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 9.5h4M3 14.5h4M17 9.5h4M17 14.5h4"/>'],
  Audio: ["#ec4899", '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>'],
  Images: ["#14b8a6", '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>'],
  Documents: ["#3b82f6", '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>'],
  Ebooks: ["#0ea5e9", '<path d="M4 19.5V5a2 2 0 0 1 2-2h14v15H6.5A2.5 2.5 0 0 0 4 20.5 2.5 2.5 0 0 0 6.5 23H20v-5"/>'],
  Archives: ["#f59e0b", '<path d="M21 8v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8M1 3h22v5H1zM10 12h4"/>'],
  "Disk images": ["#f97316", '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>'],
  Programs: ["#22c55e", '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>'],
  Subtitles: ["#a3a3a3", '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M6 13h4M14 13h4M6 16h8"/>'],
  Torrents: ["#ef4444", '<path d="M6 3v8a6 6 0 0 0 12 0V3M6 7h4M14 7h4"/>'],
  Fonts: ["#eab308", '<path d="M4 20 10 4h1l6 16M6.5 14h8"/>'],
  Design: ["#d946ef", '<path d="m12 19 7-7 3 3-7 7zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18z"/><circle cx="11" cy="11" r="2"/>'],
  Other: ["#64748b", '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>']
};
function catIcon(c) { var x = CAT[c] || CAT.Other; return "<div class=fi style='--c:" + x[0] + "'>" + sv(x[1]) + "</div>" }
function $(i) { return document.getElementById(i) }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";" }) }
function mbs(x) { return (x / 1048576).toFixed(2) }
function hs(x) { if (!x) return "0 B"; var u = ["B", "KB", "MB", "GB", "TB"], i = Math.min(4, Math.floor(Math.log(x) / Math.log(1024))); return (x / Math.pow(1024, i)).toFixed(i > 1 ? 2 : 0) + " " + u[i] }
function rate(x) { return x >= 1048576 ? mbs(x) + " MB/s" : (x / 1024).toFixed(0) + " KB/s" }
function dur(s) { if (!isFinite(s) || s <= 0) return ""; var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? d + "d " + h + "h" : h ? h + "h " + String(m).padStart(2, "0") + "m" : m + "m " + String(Math.floor(s % 60)).padStart(2, "0") + "s" }
function ext(n) { return (n.split(".").pop() || "").toLowerCase() }
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim() }
function tilde(p) { return ST && ST.home && p && p.indexOf(ST.home) == 0 ? "~" + p.slice(ST.home.length) : p }
function toast(t) { var e = $("toast"); e.textContent = t; e.classList.add("on"); clearTimeout(e._t); e._t = setTimeout(function () { e.classList.remove("on") }, 2600) }
function api(p, body) {
  var o = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  return fetch(p, o).then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j }) })
    .catch(function (e) {
      if (e.message != "NEED_LAN") throw e;
      // the network port is closed: ask once, open it, then do what the user asked for
      if (!confirm("Let your TV and other devices on this network reach dlus?\n\ndlus opens its network port (" + ((ST && ST.settings.lan_port) || 8643) + ") so the TV can play from this Mac. It is protected by a secret code. You can close it again under Sharing.")) throw new Error("Not shared - nothing was opened on your network");
      return fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lan_on: true }) })
        .then(function () { return api(p, body) });
    });
}
function act(id, a, body) { return api("/api/d/" + id + "/" + a, body || {}).then(poll).catch(function (e) { toast("Error: " + e.message) }) }
function poll() { return api("/api/state").then(function (s) { ST = s; $("off").style.display = "none"; render() }).catch(function () { $("off").style.display = "block" }) }
function pollDet() { Object.keys(OPEN).forEach(function (id) { api("/api/d/" + id).then(function (d) { DET[id] = d; renderDet(id) }).catch(function () {}) }) }
setInterval(poll, 500); setInterval(pollDet, 700); poll();
// theme
function applyTheme() { var t = localStorage.getItem("dlus-theme") || "auto", dark = t == "dark" || (t == "auto" && matchMedia("(prefers-color-scheme: dark)").matches); document.documentElement.dataset.theme = dark ? "dark" : "light"; $("thbtn").innerHTML = I[t == "auto" ? "auto" : dark ? "moon" : "sun"]; $("thbtn").title = "Theme: " + t }
function cycleTheme() { var o = ["auto", "dark", "light"], t = localStorage.getItem("dlus-theme") || "auto"; localStorage.setItem("dlus-theme", o[(o.indexOf(t) + 1) % 3]); applyTheme(); toast("Theme: " + localStorage.getItem("dlus-theme")) }
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme); applyTheme();
(function () { var h = location.hash.slice(1).split("/"); if (h[0]) VIEW = decodeURIComponent(h[0]); if (h[1]) document.documentElement.dataset.theme = h[1]; if (h[2] == "bell") setTimeout(function () { $("npanel").classList.add("on") }, 1500) })();
window.addEventListener("hashchange", function () { var h = location.hash.slice(1).split("/")[0]; if (h) setView(decodeURIComponent(h)) });
// views
var VIEWS = [["all", "All downloads", "all"], ["active", "Active", "active"], ["queued", "Queue", "queue"], ["paused", "Paused", "paused"], ["done", "Finished", "done"], ["error", "Errors", "error"]];
function inView(it, v) {
  if (v == "all") return true;
  if (v == "active") return ["downloading", "waiting", "joining", "verifying"].indexOf(it.status) >= 0;
  if (v.indexOf("cat:") == 0) return it.cat == v.slice(4);
  return it.status == v;
}
function setView(v) { VIEW = v; window.scrollTo(0, 0); render() }
function render() {
  if (!ST) return;
  renderAccess();
  $("ver").textContent = "v" + ST.version;
  var items = ST.items, q = $("q").value.trim().toLowerCase();
  if (/^(https?|ftp):\/\//.test(q)) q = "";
  var nav = "<h6>Downloads</h6>" + VIEWS.map(function (v) {
    var n = items.filter(function (it) { return inView(it, v[0]) }).length;
    return "<a class='" + (VIEW == v[0] ? "on" : "") + "' onclick=\"setView('" + v[0] + "')\">" + I[v[2]] + v[1] + "<em>" + (n || "") + "</em></a>";
  }).join("") + "<h6>Library</h6><a class='" + (VIEW == "lib" ? "on" : "") + "' onclick=\"setView('lib')\">" + I.lib + "All files<em>" + (items.filter(function (it) { return it.status == "done" }).length || "") + "</em></a>" +
    (ST.cats || []).map(function (c) {
      var n = items.filter(function (it) { return it.cat == c }).length;
      return n ? "<a class='" + (VIEW == "cat:" + c ? "on" : "") + "' onclick=\"setView('cat:" + c + "')\"><span style='color:" + CAT[c][0] + ";display:flex'>" + sv(CAT[c][1]) + "</span>" + c + "<em>" + n + "</em></a>" : "";
    }).join("") + "<h6>System</h6><a class='" + (VIEW == "fonts" ? "on" : "") + "' onclick=\"setView('fonts')\">" + sv('<path d="M4 20 10 4h1l6 16M6.5 14h8"/>') + "Fonts<em>" + ((ST.fonts || []).filter(function (f) { return f.installed }).length || "") + "</em></a>" +
    "<a class='" + (VIEW == "tv" ? "on" : "") + "' onclick=\"setView('tv')\">" + I.tv + "TV &amp; remote<em>" + ((ST.tvs || []).length || "") + "</em></a>" +
    "<a class='" + (VIEW == "gw" ? "on" : "") + "' onclick=\"setView('gw')\">" + I.vpn + "VPN router<em>" + (ST.gw && ST.gw.active ? "on" : "") + "</em></a>" +
    "<a class='" + (VIEW == "dev" ? "on" : "") + "' onclick=\"setView('dev')\">" + I.tv + "Devices &amp; viewers<em>" + (((ST.devices || {}).live || []).length || "") + "</em></a>" +
    "<a class='" + (VIEW == "share" ? "on" : "") + "' onclick=\"setView('share')\">" + I.cast + "Sharing<em>" + (ST.share && (ST.share.proxy.running || ST.share.files.on) ? "on" : "") + "</em></a>" +
    "<a class='" + (VIEW == "net" ? "on" : "") + "' onclick=\"setView('net')\">" + I.net + "Network &amp; storage</a>";
  if ($("nav")._h != nav) { $("nav").innerHTML = nav; $("nav")._h = nav }
  renderFoot(); renderBanners();
  $("v-list").classList.toggle("hide", ["lib", "net", "dev", "share", "gw", "tv", "fonts"].indexOf(VIEW) >= 0);
  $("v-dev").classList.toggle("hide", VIEW != "dev");
  $("v-share").classList.toggle("hide", VIEW != "share");
  $("v-gw").classList.toggle("hide", VIEW != "gw");
  $("v-tv").classList.toggle("hide", VIEW != "tv");
  $("v-fonts").classList.toggle("hide", VIEW != "fonts");
  if (VIEW == "fonts") { ["v-list", "v-lib", "v-net", "v-dev", "v-share", "v-gw", "v-tv", "empty"].forEach(function (x) { $(x).classList.add("hide") }); renderFonts(); return }
  if (VIEW == "tv") { ["v-list", "v-lib", "v-net", "v-dev", "v-share", "v-gw", "empty"].forEach(function (x) { $(x).classList.add("hide") }); renderTvPage(); return }
  if (VIEW == "gw") { ["v-list", "v-lib", "v-net", "v-dev", "v-share", "empty"].forEach(function (x) { $(x).classList.add("hide") }); renderGw(); return }
  if (VIEW == "share") { $("v-list").classList.add("hide"); $("v-lib").classList.add("hide"); $("v-net").classList.add("hide"); $("empty").classList.add("hide"); renderShareSet(); return }
  if (VIEW == "dev") { renderDevices(); $("empty").classList.add("hide"); $("v-lib").classList.add("hide"); $("v-net").classList.add("hide"); return }
  $("v-lib").classList.toggle("hide", VIEW != "lib");
  $("v-net").classList.toggle("hide", VIEW != "net");
  if (VIEW == "lib") { renderLib(); $("empty").classList.add("hide"); return }
  if (VIEW == "net") { renderNetPage(); $("empty").classList.add("hide"); return }
  var vis = items.filter(function (it) { return inView(it, VIEW) && (!q || (it.name + " " + it.dir + " " + it.url).toLowerCase().indexOf(q) >= 0) });
  if (VIEW != "queued") {  // active first, then queue order, then finished newest first
    var rank = { downloading: 0, joining: 0, verifying: 0, waiting: 1, error: 2, queued: 3, paused: 4, done: 5 };
    vis = vis.map(function (it, i) { return [it, i] }).sort(function (a, b) {
      var d = (rank[a[0].status] || 0) - (rank[b[0].status] || 0);
      return d || (a[0].status == "done" ? (b[0].finished || 0) - (a[0].finished || 0) : a[1] - b[1]);
    }).map(function (x) { return x[0] });
  }
  var title = VIEW.indexOf("cat:") == 0 ? VIEW.slice(4) : VIEWS.filter(function (v) { return v[0] == VIEW })[0][1];
  $("vtitle").textContent = title;
  $("vcount").textContent = vis.length ? vis.length + "" : "";
  var tools = "<button class=btn onclick=\"act('all','pause')\">" + I.pause + "Pause all</button><button class=btn onclick=\"act('all','resume')\">" + I.play + "Resume all</button>" +
    (VIEW == "done" && vis.length ? "<button class=btn onclick=\"act('all','clear')\">Clear list</button>" : "");
  if ($("vtools")._h != tools) { $("vtools").innerHTML = tools; $("vtools")._h = tools }
  $("bento").classList.toggle("hide", VIEW != "all" || !items.length);
  if (VIEW == "all" && items.length) renderBento();
  placeRows($("list"), vis);
  $("empty").classList.toggle("hide", vis.length > 0);
  $("etitle").textContent = items.length ? "Nothing in " + title.toLowerCase() : "Welcome to dlus";
  var fo = ST.folders.map(function (f) { return "<option value='" + esc(f) + "'>" }).join("");
  if ($("folders")._h != fo) { $("folders").innerHTML = fo; $("folders")._h = fo }
  if (SHARE && $("mshare").classList.contains("on")) renderShare();
}
function placeRows(list, vis) {
  var seen = {};
  vis.forEach(function (it, k) {
    seen[it.id] = 1;
    var el = list.querySelector("#d-" + it.id) || mk(it);
    upd(el, it);
    if (list.children[k] !== el) list.insertBefore(el, list.children[k] || null);
  });
  Array.prototype.slice.call(list.children).forEach(function (el) { if (!seen[el.id.slice(2)]) el.remove() });
}
function renderBento() {
  var it = ST.items, act = it.filter(function (x) { return x.status == "downloading" }).length, qd = it.filter(function (x) { return x.status == "queued" }).length;
  var left = it.reduce(function (a, x) { return a + (["done", "error"].indexOf(x.status) < 0 && x.size ? x.size - x.done : 0) }, 0);
  var dk = ST.disk || {};
  if (!$("bento")._b) {
    $("bento").innerHTML = "<div class=tile><small>Speed</small><b id=bsp>0.00<em>MB/s</em></b><div class=sub id=bsub></div><canvas id=bcv width=900 height=112></canvas></div>" +
      "<div class=tile><small>Active</small><b id=bact></b><div class=sub id=bq></div></div><div class=tile><small>Left to download</small><b id=bleft></b><div class=sub id=bleft2></div></div>" +
      "<div class=tile><small>Traffic today</small><b id=btoday></b><div class=sub id=bdisk></div></div>";
    $("bento")._b = 1;
  }
  $("bact").textContent = act; $("bq").textContent = qd ? qd + " waiting in queue" : "queue empty";
  $("bleft").textContent = hs(left); $("bleft2").textContent = ST.now > 1024 && left ? "~" + dur(left / ST.now) + " at this speed" : "";
  $("btoday").textContent = hs(ST.stats.today); $("bdisk").textContent = dk.free ? hs(dk.free) + " free on disk" : "";
  var n = ST.net; $("bsub").textContent = (n.kind || "") + (n.public_ip ? " · " + n.public_ip : "") + (n.full_tunnel ? " · via VPN" : "");
}
function renderFoot() {
  var n = ST.net, dk = ST.disk || {}, conn = (n.vpns || []).filter(function (v) { return v.status == "Connected" });
  var vpnTxt = n.full_tunnel ? "VPN: all traffic" : conn.length ? "VPN: " + conn.map(function (v) { return v.app }).join(", ") : n.vpn ? "VPN tunnel" : "No VPN";
  var h = "<div class=chip onclick=\"setView('net')\">" + I.net + "<b>" + esc(n.kind || "Network") + (n.local_ip ? " · " + esc(n.local_ip) : "") + "</b><span class=r><span class='dot" + (n.online ? "" : " off") + "'></span></span></div>" +
    "<div class=chip onclick=\"setView('net')\">" + I.vpn + "<b>" + esc(vpnTxt) + "</b><span class=r><span class='dot " + (n.full_tunnel ? "warn" : conn.length || n.vpn ? "" : "idle") + "'></span></span></div>" +
    (dk.total ? "<div class=chip onclick=\"setView('net')\">" + I.disk + "<b>" + hs(dk.free) + " free</b><span class=r style='color:var(--dim)'>" + Math.round(dk.used * 100 / dk.total) + "%</span></div><div class=mini><i style='width:" + (dk.used * 100 / dk.total).toFixed(1) + "%'></i></div>" : "");
  if ($("sbfoot")._h != h) { $("sbfoot").innerHTML = h; $("sbfoot")._h = h }
}
// notifications: warnings (live conditions) + events, behind the bell
var NOTE = { seen: {}, gone: {}, ev: [], last: {}, unread: 0, first: true };
try { NOTE.seen = JSON.parse(localStorage.getItem("dlus-nseen") || "{}"); NOTE.gone = JSON.parse(localStorage.getItem("dlus-ngone") || "{}"); NOTE.ev = JSON.parse(localStorage.getItem("dlus-nev") || "[]") } catch (e) {}
function noteSave() { localStorage.setItem("dlus-nseen", JSON.stringify(NOTE.seen)); localStorage.setItem("dlus-ngone", JSON.stringify(NOTE.gone)); localStorage.setItem("dlus-nev", JSON.stringify(NOTE.ev.slice(0, 40))) }
function noteConditions() {
  var b = [], n = ST.net;
  if (!n.online) b.push(["offline", "bad", "No internet connection", "Downloads wait and continue by themselves when the network is back."]);
  ST.items.forEach(function (it) {
    if (it.bound_ip && n.public_ip && it.bound_ip != n.public_ip && it.status != "done") {
      var refused = /HTTP 40[34]|HTTP 410/.test(it.msg || "");
      b.push(["bound-" + it.id + "-" + n.public_ip + (refused ? "-r" : ""), refused ? "bad" : "warn", refused ? "Link refused: IP changed" : "Link issued for another IP",
        "<b>" + esc(it.name) + "</b> &mdash; the link was issued for IP <b>" + it.bound_ip + "</b>, yours is now <b>" + esc(n.public_ip) + "</b>" + (n.full_tunnel ? " (VPN on)" : "") + ". " +
        (refused ? "The server refuses it: turn the VPN off / go back to that network, or use <b>Refresh link</b>." : "The server still accepts it &mdash; nothing to do unless it starts failing.")]);
    }
    if (it.status == "error") b.push(["err-" + it.id + "-" + (it.msg || ""), "bad", "Download stopped", "<b>" + esc(it.name) + "</b>: " + esc(it.msg || "error")]);
  });
  if (n.full_tunnel) b.push(["vpn-" + ((n.tunnels[0] || {}).if || ""), "warn", "VPN carries all traffic", "Downloads go through the VPN &mdash; slower, and they may use VPN quota."]);
  ((ST.gw && ST.gw.devices) || []).forEach(function (d) {
    if (d.new) b.push(["gwnew-" + d.mac, "info", "New device: " + esc(d.name), esc(d.ip) + " joined your network. Its internet:<div style='display:flex;gap:6px;margin-top:8px'>" +
      "<button class='btn pri' onclick=\"gwPol('" + d.mac + "','tunnel')\">\ud83d\udd12 Tunnel</button><button class=btn onclick=\"gwPol('" + d.mac + "','direct')\">\u2197 Direct</button><button class=btn onclick=\"setView('gw')\">Open</button></div>"]);
  });
  if (ST.throttled) b.push(["thr-" + ST.throttled, "info", "Streaming has priority", "<b>" + esc(ST.throttled) + "</b> is watching &mdash; downloads are limited to " + ST.settings.watch_limit_mb + " MB/s so the video does not stutter. Full speed resumes when it stops."]);
  if (ST.sched_wait) b.push(["sched-" + ST.settings.sched_from, "info", "Waiting for the scheduler", "Downloads run between " + ST.settings.sched_from + " and " + ST.settings.sched_to + ". Use &ldquo;Start now&rdquo; to override."]);
  var need = ST.items.reduce(function (a, it) { return a + (["done", "error"].indexOf(it.status) < 0 && it.size ? it.size - it.done : 0) }, 0);
  if (ST.disk && ST.disk.free && need > ST.disk.free) b.push(["disk", "bad", "Not enough disk space", hs(need) + " still to download, only " + hs(ST.disk.free) + " free."]);
  return b;
}
function noteEvents() {
  ST.items.forEach(function (it) {
    var p = NOTE.last[it.id];
    if (!NOTE.first && p && p != it.status) {
      if (it.status == "done") NOTE.ev.unshift({ id: "ev-done-" + it.id, lvl: "ok", title: "Download finished", html: "<b>" + esc(it.name) + "</b> &middot; " + hs(it.size), t: Date.now() });
      if (it.status == "error") NOTE.ev.unshift({ id: "ev-err-" + it.id + Date.now(), lvl: "bad", title: "Download failed", html: "<b>" + esc(it.name) + "</b>: " + esc(it.msg || ""), t: Date.now() });
    }
    NOTE.last[it.id] = it.status;
  });
  NOTE.first = false;
}
function renderBanners() {
  noteEvents();
  var cond = noteConditions().filter(function (c) { return !NOTE.gone[c[0]] }), list = [];
  cond.forEach(function (c) { list.push({ id: c[0], lvl: c[1], title: c[2], html: c[3], live: true }) });
  NOTE.ev.forEach(function (e) { if (!NOTE.gone[e.id]) list.push(e) });
  var unread = list.filter(function (x) { return !NOTE.seen[x.id] }).length;
  var bn = $("belln"); bn.textContent = unread > 9 ? "9+" : unread; bn.className = "belln" + (unread ? " on" : "") + (unread && !list.some(function (x) { return !NOTE.seen[x.id] && (x.lvl == "bad" || x.lvl == "warn") }) ? " ok" : "");
  if (unread > NOTE.unread) { var b = $("bell"); b.classList.remove("ring"); void b.offsetWidth; b.classList.add("ring") }
  NOTE.unread = unread; NOTE.list = list;
  var icon = { bad: I.warn, warn: I.warn, info: I.info, ok: I.done };
  var h = list.length ? list.map(function (x) {
    return "<div class='nitem " + x.lvl + (NOTE.seen[x.id] ? "" : " unread") + "'><div class=ni>" + icon[x.lvl] + "</div><div class=nt><b>" + x.title + "</b>" + x.html +
      "<small>" + (x.live ? "now" : ago(x.t / 1000)) + "</small></div><button class=nx title=Dismiss data-n='" + esc(x.id) + "'>&times;</button></div>";
  }).join("") : "<div class=nempty>All clear &mdash; no notifications.</div>";
  if ($("nlist")._h != h) { $("nlist").innerHTML = h; $("nlist")._h = h; $("nlist").querySelectorAll(".nx").forEach(function (b) { b.onclick = function () { NOTE.gone[b.dataset.n] = 1; NOTE.ev = NOTE.ev.filter(function (e) { return e.id != b.dataset.n }); noteSave(); renderBanners() } }) }
  // only "no internet" also stays visible on the page
  var off = cond.filter(function (c) { return c[0] == "offline" }), bh = off.map(function (c) { return "<div class='ban bad'>" + I.warn + "<div><b>" + c[2] + ".</b> " + c[3] + "</div></div>" }).join("");
  if ($("banners")._h != bh) { $("banners").innerHTML = bh; $("banners")._h = bh }
}
function noteToggle(e) {
  e.stopPropagation(); var p = $("npanel"), on = !p.classList.contains("on"); p.classList.toggle("on", on);
  if (on) setTimeout(function () { (NOTE.list || []).forEach(function (x) { NOTE.seen[x.id] = 1 }); noteSave(); renderBanners(); $("nlist")._h = null }, 1500);
}
function noteClear() { (NOTE.list || []).forEach(function (x) { NOTE.gone[x.id] = 1; NOTE.seen[x.id] = 1 }); NOTE.ev = []; noteSave(); renderBanners() }
document.addEventListener("click", function () { $("npanel").classList.remove("on") });
function renderLib() {
  var done = ST.items.filter(function (it) { return it.status == "done" }), tot = done.reduce(function (a, it) { return a + (it.size || 0) }, 0);
  $("libsum").textContent = done.length + " files · " + hs(tot);
  var h = (ST.cats || []).map(function (c) {
    var f = done.filter(function (it) { return it.cat == c }), sz = f.reduce(function (a, it) { return a + (it.size || 0) }, 0);
    return "<div class='cat" + (f.length ? "" : " zero") + "' style='--c:" + CAT[c][0] + "' onclick=\"setView('cat:" + c + "')\">" + catIcon(c) + "<b>" + c + "</b><span>" + f.length + " file" + (f.length == 1 ? "" : "s") + " · " + hs(sz) + "</span><div class=mini><i style='width:" + (tot ? sz * 100 / tot : 0).toFixed(1) + "%'></i></div></div>";
  }).join("");
  if ($("cats")._h != h) { $("cats").innerHTML = h; $("cats")._h = h }
  var rec = done.slice().sort(function (a, b) { return (b.finished || 0) - (a.finished || 0) }).slice(0, 10);
  placeRows($("recent"), rec);
  if (!rec.length) $("recent").innerHTML = "<div class=note>Finished downloads appear here, sorted into the categories above.</div>";
}
function renderNetPage() {
  var n = ST.net, s = ST.stats, dk = ST.disk || {}, dl = ST.now, other = Math.max(0, n.rx - dl);
  var vp = (n.vpns || []).slice().sort(function (a, b) { return (b.status == "Connected") - (a.status == "Connected") });
  var listed = vp.map(function (v) { return v.app }), apps = (n.apps || []).filter(function (a) { return listed.indexOf(a) < 0 });
  var px = n.proxy || {}, pk = Object.keys(px), ep = Object.keys(n.env_proxy || {});
  var vstate = n.full_tunnel ? "<span class='badge b-warn'>all traffic</span>" : vp.some(function (v) { return v.status == "Connected" }) || n.vpn ? "<span class='badge b-acc'>connected</span>" : "<span class='badge b-off'>off</span>";
  var mx = Math.max.apply(null, s.days.map(function (d) { return d[1] }).concat([1]));
  var h =
    "<div class=card><h3>" + I.net + "Connection <span class='badge " + (n.online ? "b-ok'>online" : "b-bad'>offline") + "</span></h3>" +
    kv("Type", esc(n.kind || "-") + (n.ssid && n.ssid != "<redacted>" ? " · " + esc(n.ssid) : "") + (n.iface ? " (" + esc(n.iface) + ")" : "") + (/^172\.20\.10\./.test(n.local_ip || "") ? " · iPhone hotspot" : "")) +
    kv("Local IP", esc(n.local_ip || "-")) + kv("Gateway", esc(n.gateway || "-")) + kv("Public IP", esc(n.public_ip || "checking...")) + "</div>" +
    "<div class=card><h3>" + I.active + "Live traffic</h3>" + kv("Download / upload", rate(n.rx) + " / " + rate(n.tx)) + kv("dlus", rate(dl)) + kv("Other apps", rate(other)) +
    "<div class=stack style='margin-top:10px;background:var(--line2)'><i class=s2 style='width:" + (n.rx ? Math.min(100, dl * 100 / n.rx) : 0) + "%'></i><i class=s1 style='width:" + (n.rx ? Math.min(100, other * 100 / n.rx) : 0) + "%'></i></div><div class=note>Share of your incoming traffic used by dlus.</div></div>" +
    "<div class=card><h3>" + I.vpn + "VPN &amp; proxy " + vstate + "</h3>" +
    (vp.map(function (v) { var on = v.status == "Connected"; return "<div class=vrow><span class='dot " + (on ? "" : "idle") + "'></span><b>" + esc(v.app) + "</b><span class=vn>" + esc(v.name) + (v.ip ? " · " + esc(v.ip) : "") + "</span><span class='badge " + (on ? "b-ok" : "b-off") + "'>" + (on ? "connected" : v.status.toLowerCase()) + "</span></div>" }).join("") || "<div class=note>No VPN profiles found.</div>") +
    (n.tunnels || []).filter(function (t) { return t.kind != "NetBird" }).map(function (t) { return "<div class=vrow><span class=dot></span><b>" + esc(t.if) + "</b><span class=vn>" + esc(t.ip) + " · " + esc(t.kind) + "</span></div>" }).join("") +
    (pk.length ? pk.map(function (k) { return "<div class=vrow><span class=dot></span><b>" + k + " proxy</b><span class=vn>" + esc(px[k]) + "</span><span class='badge b-acc'>system</span></div>" }).join("") + "<div class=note>Browsers use the system proxy; dlus downloads go direct (curl ignores it).</div>" : "<div class=vrow><span class='dot idle'></span><b>System proxy</b><span class=vn>none</span></div>") +
    (ep.length ? "<div class=note>dlus uses " + esc(ep[0]) + " = " + esc(n.env_proxy[ep[0]]) + "</div>" : "") +
    (apps.length ? "<div class=note>Running but not connected: " + esc(apps.join(", ")) + "</div>" : "") + "</div>" +
    "<div class=card><h3>" + I.chart + "Traffic used by dlus</h3>" + kv("Today", hs(s.today)) + kv("This month", hs(s.month)) + kv("All time", hs(s.total)) +
    "<div class=bars>" + s.days.map(function (d) { return "<div title='" + d[0] + ": " + hs(d[1]) + "'><i style='height:" + (d[1] * 100 / mx).toFixed(1) + "%'></i><span>" + d[0].slice(3) + "</span></div>" }).join("") + "</div></div>" +
    (dk.total ? "<div class=card style='grid-column:1/-1'><h3>" + I.disk + "System storage <span class='badge b-off'>" + hs(dk.total) + "</span></h3>" +
      "<div class=stack><i class=s1 style='width:" + ((dk.used - dk.mine) * 100 / dk.total).toFixed(2) + "%'></i><i class=s2 style='width:" + (dk.mine * 100 / dk.total).toFixed(2) + "%'></i></div>" +
      "<div class=two style='margin:0'><div>" + kv("<span class=sw style='background:var(--dim)'></span>Used by other files", hs(Math.max(0, dk.used - dk.mine))) + kv("<span class=sw style='background:var(--acc)'></span>dlus downloads", hs(dk.mine)) + "</div><div>" +
      kv("<span class=sw style='background:color-mix(in srgb,var(--green) 40%,transparent)'></span>Free", hs(dk.free)) + kv("Download folder", esc(tilde(ST.settings.base_dir))) + "</div></div>" +
      (dk.dl_vol ? "<div class=note>The download folder is on another disk: " + hs(dk.dl_vol.free) + " free of " + hs(dk.dl_vol.total) + ".</div>" : "") + "</div>" : "");
  setHtml($("netgrid"), h);
}
var DEVICON = { tv: I.tv, phone: sv('<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>'), player: I.play,
  computer: sv('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M2 20h20"/>') };
function ago(t) { var s = Date.now() / 1000 - t; return s < 60 ? Math.round(s) + " s ago" : s < 3600 ? Math.round(s / 60) + " min ago" : s < 86400 ? Math.round(s / 3600) + " h ago" : new Date(t * 1000).toLocaleDateString() }
function mbit(x) { return (x * 8 / 1e6).toFixed(1) + " Mbit/s" }
function renderAccess() {
  var A = ST.access || { pending: [], allowed: [], log: [] };
  var h = A.pending.map(function (p) {
    return "<div class='card' style='display:flex;gap:14px;align-items:center;border:1px solid var(--amber);margin-bottom:10px'><div class=fi style='--c:var(--amber)'>" + I.warn + "</div>" +
      "<div style='flex:1;min-width:0'><b>" + esc(p.name) + "</b> asks to use dlus<div class=t2>" + esc(p.ip) + (p.host && p.host != p.name ? " \u00b7 " + esc(p.host) : "") + (p.mac ? " \u00b7 " + esc(p.mac) : "") + " \u00b7 " + esc(p.what) + " \u00b7 " + ago(p.at) + "</div>" +
      "<div class=t2 style='color:var(--dim)'>" + esc(p.ua || "") + "</div><div class=t2>Allow it only if you know this device. It then can watch your films, open shared folders and use the proxy.</div></div>" +
      "<button class='btn pri' data-ip='" + esc(p.ip) + "' data-a=allow>Allow</button><button class=btn data-ip='" + esc(p.ip) + "' data-a=deny>Deny</button></div>";
  }).join("");
  if ($("accessban")._h != h) {
    $("accessban").innerHTML = h; $("accessban")._h = h;
    $("accessban").querySelectorAll("button").forEach(function (b) { b.onclick = function () { accessDo(b.dataset.ip, b.dataset.a) } });
  }
}
function accessDo(ip, act) {
  api("/api/access", { ip: ip, act: act }).then(function () { toast(act == "allow" ? "Allowed" : act == "deny" ? "Denied for 1 hour" : "Access removed"); return poll() }).catch(function (e) { toast("Error: " + e.message) });
}
function renderDevices() {
  var D = ST.devices || { live: [], hist: [], seen: [], rates: [] }, now = D.rates.length ? D.rates[D.rates.length - 1] : 0;
  var ips = {}; D.live.concat(D.seen).forEach(function (x) { ips[x.ip] = 1 });
  $("devsum").textContent = D.live.length + " watching \u00b7 " + Object.keys(ips).length + " device" + (Object.keys(ips).length == 1 ? "" : "s");
  if (!$("devbento")._b) { $("devbento").innerHTML = "<div class=tile><small>Streaming to devices</small><b id=dvr></b><canvas id=dvcv width=900 height=112></canvas></div><div class=tile><small>Watching now</small><b id=dvn></b></div><div class=tile><small>Sent this session</small><b id=dvb></b></div><div class=tile><small>Plays in history</small><b id=dvh></b></div>"; $("devbento")._b = 1 }
  $("dvr").innerHTML = mbit(now).replace(" ", "<em>") + "</em>"; $("dvn").textContent = D.live.length;
  $("dvb").textContent = hs(D.live.reduce(function (a, x) { return a + x.bytes }, 0)); $("dvh").textContent = D.hist.length;
  chart($("dvcv"), D.rates, 0, false);
  var lv = D.live.map(function (x) {
    return "<div class='item' style='padding:14px 16px'><div style='display:flex;gap:14px;align-items:center'><div class=fi style='--c:var(--acc)'>" + (DEVICON[x.kind] || I.tv) + "</div>" +
      "<div style='flex:1;min-width:0'><div class=t1><span class=nm>" + esc(x.device) + "</span><span class='pill p-downloading'><i></i><span>" + (x.active > 0 ? "streaming" : "paused / buffered") + "</span></span></div>" +
      "<div class=t2>" + esc(x.ip) + (x.host ? " \u00b7 " + esc(x.host) : "") + (x.mac ? " \u00b7 " + esc(x.mac) : "") + "</div>" +
      "<div class=t2>Watching <b style='color:var(--txt)'>" + esc(x.title) + "</b> \u00b7 " + esc(x.variant) + " \u00b7 at ~" + Math.round(x.frac * 100) + "%</div></div>" +
      "<div class=stat><b>" + mbit(x.rate) + "</b><span>" + hs(x.bytes) + " sent \u00b7 since " + new Date(x.start * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + "</span></div>" +
      "<canvas class=dcv data-ip='" + esc(x.ip) + "' width=360 height=90 style='width:180px;height:45px'></canvas></div><div class=t2 style='margin-top:6px;color:var(--dim)'>" + esc(x.ua || "") + "</div></div>";
  }).join("") || "<div class=note>Nobody is watching right now. Devices appear here as soon as they open a stream link, the browser player or the dlus library on a TV.</div>";
  $("devlive").innerHTML = lv;
  var cv = $("devlive").querySelectorAll("canvas"); D.live.forEach(function (x, i) { if (cv[i]) chart(cv[i], x.rates, 0, false) });
  var se = D.seen.map(function (x) { return "<div class=vrow><span class=dot></span><b>" + esc(x.device) + "</b><span class=vn>" + esc(x.ip) + (x.host ? " \u00b7 " + esc(x.host) : "") + (x.mac ? " \u00b7 " + esc(x.mac) : "") + " \u00b7 " + esc(x.what) + "</span><span class='badge b-off'>" + ago(x.last) + "</span></div>" }).join("");
  $("devseen").innerHTML = se ? "<div class=card>" + se + "</div>" : "<div class=note>No devices in the last hour.</div>";
  $("devhist").innerHTML = "<tr><th>When</th><th>Device</th><th>IP</th><th>Watched</th><th>How</th><th>Time</th><th>Data</th></tr>" + (D.hist.map(function (x) {
    return "<tr><td>" + ago(x.start) + "</td><td>" + esc(x.device) + "</td><td>" + esc(x.ip) + "</td><td class=ht>" + esc(x.title) + "</td><td>" + esc(x.variant) + "</td><td>" + dur(x.last - x.start) + "</td><td>" + hs(x.bytes) + "</td></tr>";
  }).join("") || "<tr><td colspan=7 class=note style='padding:14px'>No plays yet.</td></tr>");
  var A = ST.access || { allowed: [], log: [] };
  var al = A.allowed.map(function (a) {
    return "<div class=vrow><span class=dot></span><b>" + esc(a.name) + "</b><span class=vn>" + esc(a.ip) + (a.mac ? " \u00b7 " + esc(a.mac) : "") + " \u00b7 " + esc(a.why || "") + " \u00b7 last seen " + ago(a.last || a.at) + "</span>" +
      "<button class='btn' style='margin-left:auto' onclick=\"if(confirm('Remove access for this device? It must be allowed again to use dlus.'))accessDo('" + esc(a.ip) + "','remove')\">Remove</button></div>";
  }).join("");
  $("devallowed").innerHTML = al ? "<div class=card>" + al + "</div>" : "<div class=note>No devices allowed yet.</div>";
  var lg = "<tr><th>When</th><th>Device</th><th>IP</th><th>What</th></tr>" + (A.log.map(function (x) {
    return "<tr" + (x.level == "warn" ? " style='color:var(--amber)'" : "") + "><td title='" + new Date(x.t * 1000).toLocaleString() + "'>" + ago(x.t) + "</td><td>" + esc(x.device || "") + "</td><td>" + esc(x.ip || "this Mac") + "</td><td class=ht>" + esc(x.event) + "</td></tr>";
  }).join("") || "<tr><td colspan=4 class=note style='padding:14px'>Nothing yet.</td></tr>");
  if ($("devaudit")._h != lg) { $("devaudit").innerHTML = lg; $("devaudit")._h = lg }
}
function gwSet(o) { api("/api/gw", o).then(poll).catch(function (e) { toast("Error: " + e.message) }) }
function gwPol(mac, v) { var p = {}; p[mac] = v; gwSet({ policy: p }); toast(v == "tunnel" ? "Tunnel — this device's internet now goes through the VPN" : v == "direct" ? "Direct — this device skips the VPN" : "Uses the default") }
function gwName(mac, old) { var n = prompt("Name for this device", old); if (n) { var o = {}; o[mac] = n; gwSet({ names: o }) } }
function renderGw() {
  var G = ST.gw, L = G.lan || {}, tun = G.tun != "-";
  var seg = function (mac, cur) {
    return "<div class=seg>" + [["tunnel", "🔒 Tunnel"], ["direct", "↗ Direct"], ["default", "Default"]].map(function (o) {
      return "<button class='" + (cur == o[0] ? "on " + o[0] : "") + "' onclick=\"gwPol('" + mac + "','" + o[0] + "')\">" + o[1] + "</button>" }).join("") + "</div>" };
  var h = "";
  var hv = G.helper_have, hw = G.helper_want;
  if (!G.installed || hv < hw) h += "<div class='card gwhero warn'><div class=gwi>" + I.warn + "</div><div style='flex:1'><h3>" + (hv && hv < hw ? "Update the router helper (v" + hv + " &rarr; v" + hw + ")" : "One-time setup: install the router helper") + "</h3>" +
    "<div class=note style='margin:0 0 10px'>Routing other devices needs macOS's firewall (pf) and IP forwarding, which only an administrator can change. dlus installs a tiny helper that can do <b>only</b> these steps. macOS asks for your password once.</div>" +
    "<div style='display:flex;gap:8px;flex-wrap:wrap'><button class='btn pri' onclick=\"gwAdmin('install')\">" + (hv ? "Update helper" : "Install helper") + "</button><a class=lnk onclick=\"$('gwwhat').classList.toggle('hide')\">what exactly runs as administrator?</a></div>" +
    "<div id=gwwhat class='hide note'>" + gwWhat() + "</div></div></div>";
  h += "<div class='card gwhero " + (G.active ? (tun ? "ok" : (G.killswitch ? "bad" : "warn")) : "") + "'><div class=gwi>" + I.vpn + "</div><div style='flex:1'>" +
    "<h3>" + (G.active ? (tun ? "Router on · tunnel " + esc(G.tun_name || G.tun) + " (" + esc(G.tun) + ") is up" : "Router on · no VPN tunnel " + (G.killswitch ? "— tunnel devices are blocked (kill switch)" : "— tunnel devices go direct")) : G.on ? "Starting..." : "Router off") + "</h3>" +
    "<div class=note style='margin:0'>" + (G.error ? "<span style='color:var(--red)'>" + esc(G.error) + "</span>" : L.ip ? "This Mac is the gateway at <b>" + esc(L.ip) + "</b> on " + esc(L.if) + " (" + esc(L.cidr) + ")" : "No network") + "</div></div>" +
    "<label class=tg><input type=checkbox " + (G.on ? "checked" : "") + (G.installed ? "" : " disabled") + " onchange=\"gwSet({on:this.checked})\"><span></span></label></div>";
  h += "<div class=card style='margin-top:12px;display:flex;align-items:center;gap:14px'><div style='flex:1'><h3 style='margin:0 0 4px'>" + I.tv + "Connect a device to the VPN</h3><div class=note style='margin:0'>Pick your TV (or any device): dlus sets up this Mac, shows the values on the TV and checks live that its traffic really goes through the VPN.</div></div><button class='btn pri' onclick='gwwOpen()'>Start</button></div>";
  if (G.result) h += gwResultHtml(G.result);
  var cur = G.tunnels.filter(function (t) { return t.if == G.via })[0];
  h += "<div class=grid style='margin-top:12px'><div class=card><h3>" + I.vpn + "VPN</h3>" +
    (cur ? "<div class=gwvals><div><span>This Mac's internet goes through</span><b>" + esc(cur.name) + " &middot; " + esc(cur.if) + "</b></div></div>" +
      (G.full == cur.if ? "<div class=note>Routed by dlus (all traffic into " + esc(cur.if) + "). <a class=lnk onclick=\"gwFull(false)\">undo</a></div>" : "")
      : "<div class=note style='margin:0;color:var(--orange,#f5a524)'>No VPN carries this Mac's internet right now (it goes through " + esc(G.via || "-") + "). Connect a VPN in <b>full-tunnel</b> mode: V2Box TUN / system mode, WireGuard with AllowedIPs 0.0.0.0/0, OpenVPN redirect-gateway, Windscribe, AnyConnect without split tunnelling...</div>") +
    G.split.map(function (t) { return "<div class=note style='margin-top:8px'><b>" + esc(t.name) + "</b> (" + esc(t.if) + ") is up but only carries some networks (split tunnel). " +
      "<button class=btn onclick=\"gwFull(true,'" + esc(t.if) + "')\">Route everything through it</button></div>" }).join("") +
    "<div class=note>Devices that use this Mac follow the same route as the Mac itself, so any VPN works that takes all of the Mac's traffic. Devices you want <b>Direct</b> keep their network settings on Automatic. Examples: V2Box in <b>TUN / system-wide mode</b>, WireGuard, FortiClient, IKEv2... The devices' DNS also goes through the tunnel, so filtered DNS cannot block YouTube.</div>" +
    "<label class=ck><input type=checkbox " + (G.killswitch ? "checked" : "") + " onchange=\"gwSet({killswitch:this.checked})\"> Kill switch: if the VPN drops, tunnel devices get no internet (nothing leaks)</label></div>" +
    "<div class=card><h3>" + I.tv + "New devices</h3><div class=note style='margin:0 0 8px'>When a device joins your network the bell rings and you choose. Until then it uses:</div>" + seg("", G.default).replace(/gwPol\('','(\w+)'\)/g, "gwSet({default:'$1'})").replace(/<button class='[^']*' onclick="gwSet\(\{default:'default'\}\)">Default<\/button>/, "") + "</div></div>";
  var dv = G.devices.map(function (d) {
    var eff = d.effective;
    return "<div class='gwd" + (d.new ? " new" : "") + "'><div class=fi style='--c:" + (eff == "tunnel" ? "var(--acc)" : "var(--dim)") + "'>" + (DEVICON[d.kind] || I.tv) + "</div>" +
      "<div class=gwn><b onclick=\"gwName('" + d.mac + "','" + esc(d.name).replace(/'/g, "") + "')\" title='Rename'>" + esc(d.name) + "</b>" + (d.new ? " <span class='badge b-acc'>new</span>" : "") +
      "<div class=t2>" + esc(d.ip) + " · " + esc(d.mac) + (d.host ? " · " + esc(d.host) : "") + "</div>" +
      (d.usage ? "<div class=t2>Traffic through this Mac: <b>" + hs(d.usage.today) + "</b> today &middot; <b>" + hs(d.usage.month) + "</b> this month &middot; " + hs(d.usage.total) +
        " since " + new Date(d.usage.since * 1000).toLocaleDateString() + (d.usage.tunnel ? " (" + hs(d.usage.tunnel) + " through the VPN)" : "") +
        " <a class=lnk onclick=\"if(confirm('Reset the traffic counter of this device?'))gwSet({reset_usage:'" + d.mac + "'})\">reset</a></div>" : "") +
      "<div class=t2>" + (d.routed ? "<span class='badge " + (eff == "tunnel" ? "b-acc'>🔒 through the VPN" : "b-ok'>↗ direct via this Mac") + "</span> " + d.conns + " connections · " + mbit(d.rate) + " now" : "<span class='badge b-off'>not using this Mac yet</span> <a class=lnk onclick=\"gwwOpen('" + d.mac + "')\">connect it (guided)</a>") + "</div></div>" +
      seg(d.mac, d.policy) + "</div>";
  }).join("");
  h += "<h2 class=sec style='margin-top:18px'>Devices on this network</h2><div class=card style='padding:6px 10px'>" + (dv || "<div class=note style='padding:12px'>No other devices seen yet.</div>") + "</div>";
  h += "<div class=card style='margin-top:12px'><h3>" + I.info + "Check routes <button class=btn style='margin-left:auto' onclick='gwDiag()'>Check now</button>" +
    (G.installed ? "<button class=btn onclick=\"gwAdmin('uninstall')\">Remove helper</button>" : "") + "</h3><div class=note style='margin:0'>Shows the Mac's routes, VPN tunnels and the router's firewall state &mdash; useful when something does not work.</div><pre id=gwout class='hide gwpre'></pre></div>";
  h += "<div class=card id=gwguide style='margin-top:12px'><h3>" + I.info + "Point a device at this Mac (once)</h3>" + gwGuideHtml(GWIP || (G.devices[0] || {}).ip || "") + "</div>";
  setHtml($("gwbody"), h);
}
var GWIP = "";
function gwWhat() {
  return "<ol class=steps><li>copies <code>dlus-gw</code> to <code>/Library/PrivilegedHelperTools/dlus-gw</code>, a root-only folder (neither dlus nor any other program can change it)</li>" +
    "<li>adds <code>/etc/sudoers.d/dlus-gw</code>: your user may run <b>only that file</b> as root, nothing else</li>" +
    "<li>checks its firewall rules (<code>dlus-gw selftest</code>)</li></ol>The helper can only: turn IP forwarding on/off, load dlus's own firewall anchor (<code>com.apple/dlus</code>, your other rules stay untouched), and add/remove the two &ldquo;route everything&rdquo; routes. Command: <code>" + esc(ST.gw.install) + "</code>";
}
function gwResultHtml(r) {
  var ago = Math.max(0, Math.round((Date.now() / 1000 - r.at) / 60));
  var chip = function (cls, t) { return "<span class='gchip " + cls + "'>" + t + "</span>" };
  var chips = [];
  if (r.kind == "admin" && r.act == "install") chips.push(chip("ok", "Helper " + (r.v_from ? "v" + r.v_from + " &rarr; " : "") + "v" + (r.v_to || "?")));
  if (r.kind == "admin" && r.act == "install") chips.push(chip("ok", "Only <code>/Library/PrivilegedHelperTools/dlus-gw</code> may run as root"));
  if (r.checks) chips.push(chip("ok", r.checks + "/3 firewall checks passed"));
  chips.push(chip(r.added || r.removed ? "acc" : "", r.added || r.removed ? (r.added ? "+" + r.added + " route" + (r.added > 1 ? "s" : "") : "") + (r.added && r.removed ? " &middot; " : "") + (r.removed ? "&minus;" + r.removed + " removed" : "") : "Your routes did not change"));
  var lines = r.lines.map(function (l) { var good = /^(ok|installed|done|removed)|: ok$/i.test(l), bad = /error|fail|refus|could not|no internet/i.test(l);
    return "<div class='gline" + (good ? " ok" : bad ? " bad" : "") + "'><i>" + (good ? "&#10003;" : bad ? "!" : "&middot;") + "</i><span>" + esc(l) + "</span></div>" }).join("");
  var rows = r.routes.map(function (x) { return "<tr class='" + x.st + "'><td>" + (x.st == "added" ? "+" : x.st == "removed" ? "&minus;" : "") + "</td><td>" + esc(x.dest) + "</td><td>" + esc(x.via) + "</td><td>" + esc(x.if) + "</td></tr>" }).join("");
  return "<div class='card gres " + (r.ok ? "ok" : "bad") + "'><div class=gresh><div class=gresi>" + (r.ok ? "&#10003;" : "!") + "</div><div style='flex:1'><h3 style='margin:0'>" + esc(r.title) + "</h3>" +
    "<div class=note style='margin:2px 0 0'>" + (ago ? ago + " min ago" : "just now") + (r.command ? " &middot; <code>" + esc(r.command) + "</code>" : "") + "</div></div>" +
    "<button class=btn onclick=\"api('/api/gw/dismiss',{}).then(poll)\">Close</button></div>" +
    "<div class=gchips>" + chips.join("") + "</div>" +
    "<div class=gresg><div><b class=gsub>What ran</b>" + (lines || "<div class=note>no output</div>") + "</div>" +
    "<div><b class=gsub>Your Mac's routes" + (r.added || r.removed ? " &mdash; changes marked" : "") + "</b><table class=grt><tr><th></th><th>Destination</th><th>Via</th><th>Interface</th></tr>" + rows + "</table></div></div></div>";
}
function gwShow(title, text, ok) { var o = $("gwout"); if (!o) return; o.classList.remove("hide"); o.textContent = "== " + title + "\n" + (text || "(no output)"); o.style.borderColor = ok === false ? "var(--red)" : ""; o.scrollIntoView({ behavior: "smooth", block: "center" }) }
function gwAdmin(act) {
  toast(act == "install" ? "macOS will ask for your password..." : "macOS will ask for your password to remove the helper...");
  api("/api/gw/admin", { act: act }).then(function (r) { if (r.cancelled) toast("Cancelled - nothing was changed"); else { $("gwbody")._h = null; poll().then(function () { var e = document.querySelector(".gres"); if (e) e.scrollIntoView({ behavior: "smooth", block: "center" }) }) } })
    .catch(function (e) { gwShow("Error", e.message, false) });
}
function gwFull(on, tun) {
  if (on && !confirm("Send ALL of this Mac's internet traffic through " + tun + "?\n\ndlus adds two routes (0.0.0.0/1 and 128.0.0.0/1). If the internet stops working within a few seconds they are removed again by themselves. They are also removed when you turn the router off, restart the Mac, or press undo.")) return;
  toast(on ? "Routing everything through " + tun + "..." : "Removing the routes...");
  api("/api/gw/full", { on: on, tun: tun || "" }).then(function () { $("gwbody")._h = null; poll().then(function () { var e = document.querySelector(".gres"); if (e) e.scrollIntoView({ behavior: "smooth", block: "center" }) }) }).catch(function (e) { gwShow("Error", e.message, false) });
}
function gwDiag() { gwShow("Checking...", ""); api("/api/gw/diag", {}).then(function (r) { gwShow("Check routes", r.text, true) }).catch(function (e) { gwShow("Error", e.message, false) }) }
function gwGuide(ip) { GWIP = ip; $("gwbody")._h = null; renderGw(); $("gwguide").scrollIntoView({ behavior: "smooth" }) }
function gwGuideHtml(ip) {
  var L = ST.gw.lan || {};
  return "<div class=two style='margin:0'><div><b>LG TV (webOS)</b><ol class=steps><li>Settings &rarr; General &rarr; Network &rarr; Wi-Fi Connection</li><li>Advanced Wi-Fi Settings &rarr; <b>Edit</b></li><li>Untick <b>Set Automatically</b> and enter the values on the right</li><li>Connect. Then choose Tunnel or Direct for it here.</li></ol>" +
    "<b>iPhone / Android</b><ol class=steps><li>Wi-Fi &rarr; your network &rarr; (i) / Edit</li><li>Configure IP &rarr; <b>Manual</b> (Android: IP settings &rarr; Static)</li><li>Enter the values on the right</li></ol></div>" +
    "<div class=gwvals>" + [["IP address", ip || "(the device's current IP)"], ["Subnet mask", L.mask || "-"], ["Router / Gateway", L.ip || "-"], ["DNS", "1.1.1.1"]].map(function (r) { return "<div><span>" + r[0] + "</span><b>" + esc(r[1]) + "</b></div>" }).join("") +
    "<div class=note>Keep the device's current IP so it does not clash. If this Mac reconnects and gets another IP, update the gateway on the device.</div></div></div>";
}
// fonts
var FPREV = "سلام، این یک زیرنویس فارسی است ۱۲۳";
function renderFonts() {
  var cur = (ST.settings.sub_style || {}).font;
  var h = (ST.fonts || []).map(function (f) {
    return "<div class='fcard" + (f.installed ? "" : " off") + (cur == f.name ? " cur" : "") + "'><div class=fh><b>" + esc(f.name) + "</b><span class='badge " + (f.user ? "b-acc" : "b-off") + "'>" + esc(f.kind) + "</span>" + (cur == f.name ? "<span class='badge b-ok'>in use</span>" : "") + "</div>" +
      "<div class=fprev dir=rtl style=\"font-family:'" + esc(f.name) + "',serif\">" + (f.installed ? FPREV : "<span class=note>not installed</span>") + "</div><div class=fb>" +
      (f.installed ? "<button class='btn pri' onclick=\"fontUse('" + esc(f.name) + "')\">Use for subtitles</button>" + (f.builtin ? "" : "<button class=btn onclick=\"fontAct({remove:'" + esc(f.name) + "'})\">Remove</button>")
        : "<button class='btn pri' onclick=\"this.disabled=true;this.textContent='Installing...';fontAct({install:'" + esc(f.name) + "'})\">Install</button>") + "</div></div>";
  }).join("");
  setHtml($("fgrid"), h);
}
function fontAct(o) { api("/api/fonts", o).then(function () { toast(o.install ? o.install + " installed — reload the player on the TV to use it" : "Font removed"); return poll() }).then(function () { $("fgrid")._h = null; renderFonts() }).catch(function (e) { toast("Error: " + e.message); $("fgrid")._h = null; renderFonts() }) }
function fontUse(n) { var st = Object.assign({}, ST.settings.sub_style || {}, { font: n }); api("/api/settings", { sub_style: st }).then(poll).then(function () { $("fgrid")._h = null; renderFonts(); toast(n + " is the subtitle font now (Mac player; on the TV choose it in RED → Font)") }) }
$("fontfile").addEventListener("change", function () {
  var f = this.files[0]; if (!f) return; var rd2 = new FileReader();
  rd2.onload = function () { var b = new Uint8Array(rd2.result), s = ""; for (var i = 0; i < b.length; i += 32768) s += String.fromCharCode.apply(null, b.subarray(i, i + 32768));
    var name = prompt("Name for this font", f.name.replace(/\.[^.]+$/, "")); if (!name) return;
    api("/api/fonts", { upload: { name: name, filename: f.name, b64: btoa(s) } }).then(function () { toast(name + " added"); return poll() }).then(function () { location.reload() }).catch(function (e) { toast("Error: " + e.message) }) };
  rd2.readAsArrayBuffer(f); this.value = "";
});
// shared folder browser with "Show on TV"
var BR = { i: 0, rel: "" };
function pairedTv() { var t = (ST.tvs || []).filter(function (x) { return x.paired })[0]; if (!t) toast("Connect your TV first: TV & remote → Connect"); return t }
function tvOpenFolder(i, rel) { var t = pairedTv(); if (!t) return; toast("Opening on the TV...");
  api("/api/tv/" + t.id + "/open", { folder: i, rel: rel || "" }).then(function () { toast("Open on the TV — use the remote") }).catch(function (e) { toast("Error: " + e.message) }) }
function brOpen(i, rel) { BR = { i: i, rel: rel || "" }; show("mbrowse"); brLoad() }
function brLoad() {
  api("/api/browse?i=" + BR.i + "&rel=" + encodeURIComponent(BR.rel)).then(function (r) {
    BR.rel = r.rel == "." ? "" : r.rel;
    $("brpath").textContent = ((ST.share.dirs || []).filter(function (d) { return d.i == BR.i })[0] || {}).name + (BR.rel ? " / " + BR.rel : "");
    $("brlist").innerHTML = (BR.rel ? "<div class=vrow style='cursor:pointer' onclick=\"BR.rel=BR.rel.split('/').slice(0,-1).join('/');brLoad()\"><b>&#11013; Back</b></div>" : "") + r.items.map(function (x) {
      var media = x.cat == "Video" || x.cat == "Audio", rr = esc(x.rel).replace(/'/g, "");
      return "<div class=vrow>" + (x.dir ? "<b style='cursor:pointer' onclick=\"BR.rel='" + rr + "';brLoad()\">&#128193; " + esc(x.name) + "</b><span class=vn></span><button class=btn style='height:28px' onclick=\"tvOpenFolder(" + BR.i + ",'" + rr + "')\">" + I.tv + "Show on TV</button>"
        : "<b>" + esc(x.name) + "</b><span class=vn>" + hs(x.size) + "</span>" + (media ? "<button class=btn style='height:28px' onclick=\"brPlay('" + rr + "')\">" + I.play + "Play</button><button class='btn pri' style='height:28px' onclick=\"tvOpenFolder(" + BR.i + ",'" + rr + "')\">" + I.tv + "On TV</button>" : "")) + "</div>";
    }).join("") || "<div class=note>Empty folder.</div>";
  }).catch(function (e) { toast("Error: " + e.message) });
}
function brPlay(rel) { api("/api/import", { folder: BR.i, rel: rel }).then(function (r) { return poll().then(function () { closeM(); openPlay(r.id) }) }).catch(function (e) { toast("Error: " + e.message) }) }
function shDir(a, i) {
  var cur = (ST.share.dirs || []).map(function (d) { return d.path });
  if (a == "add") { var v = $("shdir").value.trim(); if (!v) return; cur.push(v) } else cur.splice(+i, 1);
  api("/api/settings", { share_dirs: cur }).then(function () { toast(a == "add" ? "Folder shared" : "Folder removed"); $("sharegrid")._h = null; return poll() }).catch(function (e) { toast("Error: " + e.message) });
}
function shSet(o) { api("/api/settings", o).then(poll).catch(function (e) { toast("Error: " + e.message); poll() }) }
function renderShareSet() {
  var S = ST.share, P = S.proxy, F = S.files, L = ST.lan, n = ST.net, ip = n.local_ip || "?";
  var hot = /^172\.20\.10\./.test(ip) ? " (your iPhone hotspot)" : "";
  var ups = [["auto", "Automatic" + (P.local.length ? " (" + P.local[0].app + ")" : " (this Mac's connection)")], ["direct", "This Mac's connection" + (n.full_tunnel ? " (goes through the VPN)" : "")]]
    .concat(P.local.map(function (x) { return [x.url, x.app + " \u00b7 " + x.kind.toUpperCase() + " " + x.port] }));
  if (P.upstream != "auto" && P.upstream != "direct" && !P.local.some(function (x) { return x.url == P.upstream })) ups.push([P.upstream, P.upstream]);
  var clients = (P.clients || []).map(function (c) {
    return "<tr><td><b>" + esc(c.device || "device") + "</b><br><span class=note style='margin:0'>" + esc(c.ip) + (c.host ? " \u00b7 " + esc(c.host) : "") + (c.mac ? " \u00b7 " + esc(c.mac) : "") + "</span></td><td>" + (c.conns || 0) + " open</td><td>&darr; " + hs(c.down) + "<br>&uarr; " + hs(c.up) + "</td><td>" + mbit(c.rate) + "</td><td class=ht>" + esc(c.host || "") + "</td></tr>";
  }).join("");
  var h =
    "<div class=card><h3>" + I.lib + "File sharing <span class='badge " + (F.on ? "b-ok'>on" : "b-off'>off") + "</span></h3>" +
    "<div class=note style='margin:0 0 10px'>Devices on this network" + hot + " can browse, download and play your dlus files in a web browser. Read-only; only dlus downloads and your download folder.</div>" +
    "<label class=ck><input type=checkbox " + (F.on ? "checked" : "") + " onchange=\"shSet({files_on:this.checked})\"> Share my downloads on this network</label>" +
    (F.on ? "<div class=qrbox><img src='/qr.svg?d=" + encodeURIComponent(F.url) + "'><div><b>Open on the other device</b><br>" + esc(F.url) + "<br><br><button class=btn data-u='" + esc(F.url) + "' onclick='copyText(this.dataset.u)'>Copy link</button></div></div>" : "") + "</div>" +
    "<div class=card><h3>" + I.vpn + "Proxy sharing <span class='badge " + (P.running ? "b-ok'>on" : "b-off'>off") + "</span></h3>" +
    "<div class=note style='margin:0 0 10px'>Other devices on this network" + hot + " can use this Mac's internet " + (P.local.length || n.full_tunnel ? "<b>including its VPN tunnel</b>" : "connection") + " through an HTTP/HTTPS proxy (like Privoxy).</div>" +
    "<label class=ck><input type=checkbox " + (P.on ? "checked" : "") + " onchange=\"shSet({proxy_on:this.checked})\"> Share this Mac's connection as a proxy</label>" +
    "<label>Send the traffic through</label><select class=in onchange=\"shSet({proxy_upstream:this.value})\">" + ups.map(function (u) { return "<option value='" + esc(u[0]) + "'" + (u[0] == P.upstream ? " selected" : "") + ">" + esc(u[1]) + "</option>" }).join("") + "</select>" +
    "<div class=note>" + (P.local.length ? "Found on this Mac: " + P.local.map(function (x) { return esc(x.app) + " (" + x.kind + " " + x.port + ")" }).join(", ") + "." : "No VPN app in proxy mode found &mdash; traffic leaves through this Mac's own connection" + (n.full_tunnel ? " (which goes through the VPN)." : ".")) + (P.running ? " Now using: <b>" + esc(P.using) + "</b>" : "") + "</div>" +
    "<label class=ck><input type=checkbox " + (P.auth ? "checked" : "") + " onchange=\"shSet({proxy_auth:this.checked})\"> Require a password (recommended)</label>" +
    (P.running ? "<div class=qrbox><img src='/qr.svg?d=" + encodeURIComponent(P.pac) + "'><div><b>Set up the other device</b><br>" +
      "<b>Automatic:</b> Wi-Fi &rarr; your network &rarr; Proxy &rarr; Automatic, URL:<br><code>" + esc(P.pac) + "</code><br>" +
      "<b>Manual:</b> server <code>" + esc(P.ip) + "</code> port <code>" + P.port + "</code>" + (P.auth ? "<br>user <code>" + esc(P.user) + "</code> password <code>" + esc(P.pass) + "</code> <button class=btn style='height:22px' onclick=\"shSet({proxy_newpass:true})\">new</button>" : "") +
      "</div></div>" : "") + "</div>" +
    "<div class=card style='grid-column:1/-1'><h3>" + I.active + "Devices using the proxy <span class='badge b-off'>" + mbit((P.rates || []).slice(-1)[0] || 0) + "</span></h3>" +
    (P.running ? "<canvas id=pxcv width=1600 height=110 style='width:100%;height:55px'></canvas>" : "") +
    (clients ? "<table class=htab><tr><th>Device</th><th>Connections</th><th>Data</th><th>Speed</th><th>Last site</th></tr>" + clients + "</table>" : "<div class=note>" + (P.running ? "No device has used the proxy yet." : "Turn on proxy sharing above.") + "</div>") + "</div>" +
    "<div class=card style='grid-column:1/-1'><h3>" + I.lib + "Shared folders <span class='badge b-off'>on TVs (DLNA), the TV home and the file page</span></h3>" +
    ((S.dirs || []).map(function (d) { return "<div class=vrow><span class=dot></span><b>" + esc(d.name) + "</b><span class=vn>" + esc(d.path) + "</span><button class=btn style='height:26px' onclick=\"brOpen(" + d.i + ")\">Browse</button><button class='btn pri' style='height:26px' onclick=\"tvOpenFolder(" + d.i + ")\">" + I.tv + "Show on TV</button><button class=btn style='height:26px' onclick=\"shDir('del','" + d.i + "')\">Remove</button></div>" }).join("") || "<div class=note>No folders shared yet. Your dlus downloads are always shared.</div>") +
    "<div style='display:flex;gap:8px;margin-top:10px'><input class=in id=shdir placeholder='Folder to share, e.g. ~/Movies'><button class='btn pri' onclick=\"shDir('add')\">Share folder</button></div>" +
    "<div class=note>Videos, music and photos in these folders show up on the TV under <b>" + esc(L.name || "dlus") + "</b> (turn on &ldquo;Show dlus on TVs&rdquo; in a video's share window) and in the TV home page.</div></div>" +
    "<div class=card style='grid-column:1/-1'><h3>" + I.tv + "Media sharing</h3>" + kv("Stream links &amp; browser player", L.running ? "<span class='badge b-ok'>on</span>" : "<span class='badge b-off'>off</span>") + kv("TVs see dlus (DLNA)", L.dlna ? "<span class='badge b-ok'>on</span>" : "<span class='badge b-off'>off</span>") + "<div class=note>Use the cast button on a video to share it, play it on a TV, or get its QR code.</div></div>";
  setHtml($("sharegrid"), h);
  if ($("pxcv")) chart($("pxcv"), P.rates || [], 0, false);
}
function kv(a, b) { return "<div class=kv><span>" + a + "</span><span>" + b + "</span></div>" }
// rows
function mk(it) {
  var el = document.createElement("div");
  el.className = "item fresh"; el.id = "d-" + it.id; el._new = Date.now();
  el.innerHTML = "<div class=row><span class=ic></span><div class=mid><div class=t1><span class=nm></span><span class=pill><i></i><span></span></span></div><div class=t2></div><div class=prog><i class=pre></i><i class=bar></i></div></div>" +
    "<div class=right><div class=quick><button class='qb pri' data-a=play title='Play here'>" + I.play + "Play</button><button class=qb data-a=tvplay title='Play on your TV'>" + I.tv + "On TV</button><button class=qb data-a=qr title='QR code to open it on a phone or TV'>QR</button></div><div class=acts>" +
    "<button class=ab data-a=play title='Play (also while downloading)'>" + I.tv + "</button><button class=ab data-a=share title='Watch on TV'>" + I.cast + "</button>" +
    "<button class=ab data-a=pp></button><button class=ab data-a=up title='Move up'>" + I.up + "</button><button class=ab data-a=down title='Move down'>" + I.down + "</button>" +
    "<button class='ab hl' data-a=start title='Start now (ignore queue and schedule)'>" + I.now + "</button><button class=ab data-a=open title='Show in Finder'>" + I.folder + "</button>" +
    "<button class=ab data-a=ref title='Refresh link'>" + I.link + "</button><button class='ab del' data-a=del title=Delete>" + I.del + "</button></div><div class=stat><b class=sp></b><span class=eta></span></div></div></div>" +
    "<div class=det><div><div class=box><h4>Speed <span class=dpk></span></h4><canvas class=dcv width=1000 height=240></canvas></div>" +
    "<div class=box><h4>Chunks <span class=dcm></span></h4><div class=g></div><div class=lg><span style='--c:var(--green)'>checked</span><span style='--c:var(--acc)'>downloading</span><span style='--c:var(--amber)'>checking</span><span style='--c:var(--red)'>failed, re-downloading alone</span><span style='--c:var(--line2)'>waiting</span></div></div>" +
    "<div class=box><h4>Details</h4><div class=info></div></div></div>" +
    "<div><div class=box><h4>Connections <span class=dcn></span></h4><div class=conns></div></div>" +
    "<div class=box><h4>Integrity</h4><div class=kpi><div><b class=kok style='color:var(--green)'>0</b><small>chunks verified</small></div><div><b class=kbad style='color:var(--red)'>0</b><small>fixed</small></div><div><b class=kcs>-</b><small>checksum</small></div></div>" +
    "<div class=vfy><div class=ksha></div><div class=vrow2><input class=in placeholder='Paste the publisher&#39;s checksum (SHA-256, SHA-512, SHA-1, MD5) or a link to their checksum file'><button class=btn data-v=1>Verify</button></div><div class=vres></div></div>" +
    "<div class=ev></div></div></div></div>";
  el.querySelector(".vfy button").onclick = function (e) {
    e.stopPropagation();
    var cs = el.querySelector(".vfy input").value.trim();
    api("/api/d/" + it.id + "/verify", { checksum: cs }).then(function () { toast("Checking the file..."); poll() }).catch(function (e) { toast("Error: " + e.message) });
  };
  el.querySelector(".row").onclick = function (e) {
    var b = e.target.closest("button");
    if (b) { e.stopPropagation(); return btn(it.id, b.dataset.a) }
    toggleOpen(it.id);
  };
  return el;
}
function toggleOpen(id) {
  var els = document.querySelectorAll("#d-" + id);
  if (OPEN[id]) delete OPEN[id]; else { OPEN[id] = 1; pollDet() }
  els.forEach(function (el) { el.classList.toggle("open", !!OPEN[id]) });
}
function byId(id) { return ST.items.filter(function (x) { return x.id == id })[0] }
function btn(id, a) {
  var it = byId(id); if (!it) return;
  if (a == "pp") return act(id, ["paused", "error", "queued"].indexOf(it.status) >= 0 ? "resume" : "pause");
  if (a == "start" || a == "up" || a == "down" || a == "open") return act(id, a);
  if (a == "play") return openPlay(id);
  if (a == "share") return openShare(id);
  if (a == "tvplay") return openTvPlay(id);
  if (a == "qr") { openShare(id); if (!ST.lan.running) lanSet2(true); return }
  if (a == "del") { CUR = id; $("delname").textContent = it.name; return show("mdel") }
  if (a == "ref") { CUR = id; $("rurl").value = ""; show("mref"); return setTimeout(function () { $("rurl").focus() }, 50) }
}
function upd(el, it) {
  var cls = "item st-" + it.status + (OPEN[it.id] ? " open" : "") + (Date.now() - (el._new || 0) < 450 ? " fresh" : "");
  if (el.className != cls) el.className = cls;
  var q = el.querySelector.bind(el);
  if (el._cat != it.cat) { q(".ic").innerHTML = catIcon(it.cat); el._cat = it.cat }
  q(".nm").textContent = it.name; q(".nm").title = it.name;
  var p = q(".pill"), st = it.missing ? "missing" : it.status;
  p.className = "pill p-" + st;
  p.lastChild.textContent = it.missing ? "file moved or deleted" : it.qpos ? "queued #" + it.qpos : it.status == "done" ? "" : it.status;
  p.firstChild.style.display = it.status == "done" && !it.missing ? "none" : "";
  var parts = [tilde(it.dir)];
  if (it.size) parts.push(hs(it.size));
  if (it.status != "done") parts.push(it.ranges === false ? "1 connection" : it.conns + " connections");
  if (it.msg) parts.push("<span class=msg>" + esc(it.msg) + "</span>");
  var t2 = parts.map(function (x, i) { return i == parts.length - 1 && it.msg ? x : esc(x) }).join(" · ");
  if (q(".t2")._h != t2) { q(".t2").innerHTML = t2; q(".t2")._h = t2 }
  var pp = q("[data-a=pp]"), paused = ["paused", "error", "queued"].indexOf(it.status) >= 0, done = it.status == "done";
  pp.style.display = done ? "none" : ""; pp.title = paused ? "Resume" : "Pause"; pp.innerHTML = paused ? I.play : I.pause;
  var queued = it.status == "queued" || it.status == "paused";
  q("[data-a=up]").style.display = q("[data-a=down]").style.display = q("[data-a=start]").style.display = queued ? "" : "none";
  q("[data-a=ref]").style.display = done ? "none" : "";
  var media = (it.cat == "Video" || it.cat == "Audio") && (it.prefix > 0 || done) && !it.missing;
  q("[data-a=play]").style.display = q("[data-a=share]").style.display = media ? "" : "none";
  q(".quick").style.display = media ? "" : "none";
}
function renderDet(id) {
  var d = DET[id], it = ST && byId(id); if (!d || !it) return;
  document.querySelectorAll("#d-" + id).forEach(function (el) {
    var q = el.querySelector.bind(el);
    q(".conns").innerHTML = d.conn.map(function (c) { return "<div class=conn><span>chunk " + c[0] + "</span><div class=mb><i style='width:" + c[1] + "%'></i></div><span>" + c[1] + "%</span><span class=s>" + rate(c[2]) + "</span></div>" }).join("") ||
      "<div class=note style='margin:0'>" + (it.status == "done" ? "Finished." : "No open connections.") + "</div>";
    q(".dcn").textContent = d.conn.length ? d.conn.length + " open" : "";
    var g = "", N = d.chunks.length;
    for (var i = 0; i < N; i++) { var c = d.chunks[i]; g += "<div class='" + c + "' title='chunk " + i + "'>" + (c == "d" ? "<i style='height:" + d.pc[i] + "%'></i>" : "") + "</div>" }
    q(".g").innerHTML = g || "<div class=note style='margin:0'>" + (it.status == "done" ? "All chunks verified and joined." : it.ranges === false ? "Single-piece download." : "Starts after the first contact with the server.") + "</div>";
    q(".dcm").textContent = N ? d.ok + " / " + N : "";
    q(".kok").textContent = d.ok; q(".kbad").textContent = d.fails;
    q(".kcs").textContent = !it.checksum ? "-" : it.checksum_ok === true ? "OK" : it.checksum_ok === false ? "FAIL" : "...";
    q(".kcs").style.color = it.checksum_ok === true ? "var(--green)" : it.checksum_ok === false ? "var(--red)" : "";
    q(".vfy").style.display = it.status == "done" ? "" : "none";
    q(".ksha").innerHTML = it.sha256 ? "SHA-256 <code style='user-select:all;word-break:break-all'>" + esc(it.sha256) + "</code>" + (it.checksum_src ? "<br><small>checked against " + esc(it.checksum_src) + "</small>" : "") : "";
    var v = it.verify;
    q(".vres").innerHTML = !v ? "" : v.state == "running" ? "Checking... " + esc(it.msg || "") :
      "<span style='color:" + (v.state == "ok" ? "var(--green)" : v.state == "bad" ? "var(--red)" : "inherit") + "'>" + (v.state == "ok" ? "✓ " : v.state == "bad" ? "✗ " : "") + esc(v.text || "") + "</span>";
    q(".ev").innerHTML = d.ev.map(function (l) {
      var c = /FAILED|ERROR/.test(l) ? "f" : /OK|done/.test(l) ? "ok" : /reusing|started|adopt|resumed|refreshed|jumped/.test(l) ? "r" : /error|HTTP|paused|retrying|schedule/.test(l) ? "w" : "";
      return "<div class=" + c + "><time>" + esc(l.slice(0, 8)) + "</time><span>" + esc(l.slice(9)) + "</span></div>";
    }).join("");
    var n = it.net || {}, bi = it.bound_ip, pub = ST.net.public_ip;
    q(".info").innerHTML = [["Link", esc(it.url)], ["Folder", esc(tilde(it.dir))], ["Server", esc(n.server || "-") + (n.http ? " · HTTP/" + esc(n.http) : "") + (n.connect_ms != null ? " · " + n.connect_ms + " ms" : "")],
      ["Your side", esc(n.local || "-") + " via " + esc(ST.net.kind || "?") + (ST.net.full_tunnel ? " (VPN)" : "")],
      ["Link bound to IP", bi ? esc(bi) + (pub ? (bi == pub ? " <span class='badge b-ok'>matches</span>" : " <span class='badge b-warn'>yours: " + esc(pub) + "</span>") : "") : "no"],
      ["Playable from start", it.prefix ? hs(it.prefix) : "-"], ["Added", new Date(it.added * 1000).toLocaleString()]].map(function (r) { return "<span>" + r[0] + "</span><span>" + r[1] + "</span>" }).join("");
    q(".dpk").textContent = d.hist.length ? "peak " + rate(Math.max.apply(null, d.hist)) : "";
    chart(q(".dcv"), d.hist, it.avg, true);
  });
}
function chart(cv, hist, avg, axes) {
  var x = cv.getContext("2d"), W = cv.width, H = cv.height, n = 600, pb = axes ? 24 : 0, h = H - pb;
  var acc = css("--acc"), mut = css("--dim"), line = css("--line2");
  x.clearRect(0, 0, W, H);
  var max = Math.max.apply(null, hist.concat([avg || 0, 262144])) * 1.2;
  if (axes) {
    x.font = "19px -apple-system,sans-serif";
    for (var k = 1; k <= 3; k++) { var y = h - h * k / 3; x.strokeStyle = line; x.lineWidth = 1; x.setLineDash([4, 6]); x.beginPath(); x.moveTo(0, y); x.lineTo(W, y); x.stroke(); x.setLineDash([]); x.fillStyle = mut; x.fillText(mbs(max * k / 3) + " MB/s", 6, y + 20) }
    x.fillStyle = mut; x.fillText("5 min ago", 6, H - 4); x.fillText("now", W - 40, H - 4);
  }
  if (hist.length > 1) {
    var pts = hist.map(function (v, i) { return [W - (hist.length - 1 - i) * W / n, h - 2 - v / max * (h - 4)] });
    var path = function () { x.moveTo(pts[0][0], pts[0][1]); for (var i = 1; i < pts.length - 1; i++) x.quadraticCurveTo(pts[i][0], pts[i][1], (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2); x.lineTo(pts[pts.length - 1][0], pts[pts.length - 1][1]) };
    var gr = x.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, acc + "55"); gr.addColorStop(1, acc + "00");
    x.beginPath(); path(); x.lineTo(pts[pts.length - 1][0], h); x.lineTo(pts[0][0], h); x.closePath(); x.fillStyle = gr; x.fill();
    x.beginPath(); path(); x.strokeStyle = acc; x.lineWidth = axes ? 3 : 2.5; x.stroke();
  }
  if (axes && avg) { var ay = h - avg / max * h; x.setLineDash([10, 8]); x.strokeStyle = css("--green"); x.lineWidth = 2; x.beginPath(); x.moveTo(0, ay); x.lineTo(W, ay); x.stroke(); x.setLineDash([]); x.fillStyle = css("--green"); x.fillText("avg " + mbs(avg) + " MB/s", W - 200, ay - 7) }
}
// animation loop
var FAVK = null, LOGOIMG = new Image(); LOGOIMG.src = "/logo.svg";
function favicon(p) {
  var key = p < 0 ? "idle" : Math.round(p * 200);
  if (key === FAVK || !LOGOIMG.complete) return; FAVK = key;
  if (p < 0) { $("fav").href = "/favicon.svg"; return }
  var c = document.createElement("canvas"); c.width = c.height = 64; var x = c.getContext("2d");
  x.drawImage(LOGOIMG, 8, 8, 48, 48); x.lineWidth = 6; x.lineCap = "round";
  x.strokeStyle = "rgba(127,127,127,.35)"; x.beginPath(); x.arc(32, 32, 29, 0, 7); x.stroke();
  x.strokeStyle = "#30c47b"; x.beginPath(); x.arc(32, 32, 29, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * p); x.stroke();
  $("fav").href = c.toDataURL();
}
function frame() {
  if (ST) {
    G += (ST.now - G) * 0.1;
    $("gnow").textContent = mbs(G);
    ST.items.forEach(function (it) {
      var s = D[it.id] || (D[it.id] = { done: it.done, now: it.now });
      s.done += (it.done - s.done) * 0.12; s.now += (it.now - s.now) * 0.12;
      var pct = it.size ? s.done * 100 / it.size : 0, run = it.status == "downloading";
      document.querySelectorAll("#d-" + it.id).forEach(function (el) {
        var q = el.querySelector.bind(el);
        q(".prog .bar").style.width = pct.toFixed(3) + "%";
        q(".prog .pre").style.width = (it.size && (it.cat == "Video" || it.cat == "Audio") ? it.prefix * 100 / it.size : 0).toFixed(3) + "%";
        var left = it.avg > 1024 && it.size ? (it.size - s.done) / it.avg : 0;
        q(".sp").textContent = it.status == "done" ? hs(it.size) : run ? rate(s.now) : it.size ? pct.toFixed(1) + "%" : "";
        q(".eta").textContent = it.status == "done" ? (it.finished ? new Date(it.finished * 1000).toLocaleDateString([], { month: "short", day: "numeric" }) : "") : run ? pct.toFixed(1) + "%" + (left ? " · " + dur(left) : "") : it.size ? hs(s.done) + " of " + hs(it.size) : "";
      });
    });
    var run = ST.items.filter(function (it) { return it.status == "downloading" && it.size });
    var tp = run.length ? run.reduce(function (a, it) { return a + it.done }, 0) / run.reduce(function (a, it) { return a + it.size }, 0) : -1;
    document.title = (run.length ? (tp * 100).toFixed(1) + "% · " + mbs(G) + " MB/s — " : "") + "dlus";
    favicon(tp);
    chart($("gcv"), ST.hist, 0, false);
    if ($("bsp")) { $("bsp").innerHTML = mbs(G) + "<em>MB/s</em>"; chart($("bcv"), ST.hist, 0, false) }
    pTick(); if (PL) subRender(pNow());
    if (PLAYING) { var p = byId(PLAYING); if (p) $("pavail").textContent = p.status == "done" ? "Fully downloaded" : "Playable now: " + hs(p.prefix) + " (" + (p.size ? (p.prefix * 100 / p.size).toFixed(1) : 0) + "%)" }
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
// player
function shortPath(it, snd) { return it.num + (snd ? "s.mkv" : "." + (ext(it.name) || "bin")) }
function streamUrl(it) { return location.origin + "/s/" + shortPath(it) + "?ik=" + ST.mk }
function tfmt(s) { s = Math.max(0, Math.floor(s || 0)); var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0") }
function openPlay(id) {
  var it = byId(id);
  if (it && ST.local_player && !window.__oldPlayer) {  // the TV player in its own window (app) or tab (browser)
    var w = window.open(ST.local_player + it.num + "?autoplay=1", "_blank");
    if (!w) location.href = ST.local_player + it.num + "?autoplay=1";
    return;
  }
  return openPlayOld(id);
}
function openPlayOld(id) {
  var it = byId(id); PLAYING = id;
  PL = { id: id, it: it, t0: 0, dur: 0, compat: false, canCompat: false };
  $("ptitle").textContent = it.name; $("phint").textContent = "Reading the file..."; $("pbox").innerHTML = ""; $("pctl").style.display = "none"; $("pmode").innerHTML = "";
  var ps = ST.players || [];
  $("papps").innerHTML = ps.length ? ps.map(function (p) { return "<button class=btn onclick=\"act('" + id + "','play',{app:'" + p + "'});toast('Opening in " + p + "')\">" + esc(p) + "</button>" }).join(" ") : "";
  show("mplay"); pSubSetup(id);
  api("/api/probe/" + id).then(function (info) {
    if (!PL || PL.id != id) return;
    PL.dur = info.duration || 0;
    var e = ext(it.name), au = info.audio || [], ac = au.length ? au[0].codec : "";
    var okAudio = !ac || ["aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le", "alac"].indexOf(ac) >= 0, native = BROWSER_OK[e] && okAudio;
    PL.canCompat = !!info.ffmpeg && it.cat == "Video" && !native; PL.compat = PL.canCompat;
    $("ptrack").innerHTML = au.map(function (a) { return "<option value=" + a.i + ">" + esc((a.lang || "track " + (a.i + 1)).toUpperCase() + " · " + a.codec + (a.ch ? " " + a.ch + "ch" : "") + (a.title ? " · " + a.title : "")) + "</option>" }).join("");
    var hint = PL.compat ? "With sound: the video plays untouched (bit-identical); the audio (" + esc(ac.toUpperCase()) + ") is converted to lossless FLAC on the fly, all channels kept. " :
      !native ? "<b style='color:var(--amber)'>The browser cannot play " + (okAudio ? "this format" : "the sound of this file (" + esc((ac || e).toUpperCase()) + ")") + ".</b> " + (info.ffmpeg ? "" : "Install ffmpeg (<code>brew install ffmpeg</code>) for sound here, or use IINA / VLC. ") : "";
    $("phint").innerHTML = hint + "Plays what is downloaded so far; jumping ahead downloads that part first.";
    pModeBtn(); pStart(0);
  }).catch(function () { if (PL) pStart(0) });
}
// subtitles in the Mac player
function pSubSetup(id) {
  SUBS.el = $("psubs"); SUBS.box = $("pwrap"); SUBM.el = $("psmenu"); SUBM.open = false; SUBM.el.className = "smenu";
  SUBS.urlFor = function (sid) { return "/sub/" + id + "/" + sid + ".vtt?ik=" + ST.mk };
  SUBS.onAutoSync = function () { subAutoSync("/api/subs/" + id + "/autosync?sid=" + encodeURIComponent(SUBS.sid || "")) };
  SUBS.note = function (t) { if (t) toast(t) };
  SUBM.extra = "<button class='btn pri' data-x=search>Search online&hellip;</button> <button class=btn data-x=file>Add file&hellip;</button> <button class=btn data-x=url>From a link&hellip;</button><div style='margin-top:6px'>.srt .ass .vtt or a .zip &mdash; Persian Windows-1256 files are fine. Also shown on the TV.</div>";
  SUBM.onSave = function (k) {
    if (k == "sid" || k == "delay") api("/api/subs/" + id + "/set", { sid: SUBS.sid, delay: SUBS.delay, scale: SUBS.scale || 1 });
    else api("/api/settings", { sub_style: { font: SUBS.font, size: SUBS.size, color: SUBS.color, bg: SUBS.bg, pos: SUBS.pos, bold: SUBS.bold } });
  };
  return api("/api/subs/" + id).then(function (st) { subSetup(st); subMenuRender() });
}
function pSubMenu() { SUBM.open = !SUBM.open; SUBM.el.className = "smenu" + (SUBM.open ? " on" : ""); subMenuRender() }
$("psmenu").addEventListener("click", function (e) {
  var x = e.target.getAttribute && e.target.getAttribute("data-x");
  if (x == "search") { subSearchOpen(); return e.stopPropagation() }
  if (x == "file") { $("subfile").click(); return e.stopPropagation() }
  if (x == "url") { var u = prompt("Link to the subtitle (.srt / .zip ...)"); if (u) subUpload({ url: u }); return e.stopPropagation() }
  subMenuClick(e);
});
$("subfile").addEventListener("change", function () {
  var f = this.files[0]; if (!f) return; var rd = new FileReader();
  rd.onload = function () { var b = new Uint8Array(rd.result), s = ""; for (var i = 0; i < b.length; i += 32768) s += String.fromCharCode.apply(null, b.subarray(i, i + 32768)); subUpload({ name: f.name, b64: btoa(s) }) };
  rd.readAsArrayBuffer(f); this.value = "";
});
// online subtitle search: best match for this exact release first
function subSearchOpen() {
  SUBM.open = false; SUBM.el.className = "smenu"; $("ssearch").classList.add("on");
  if (!$("sslist").innerHTML) subSearch();
}
function subSearchClose() { $("ssearch").classList.remove("on") }
function subSearch() {
  $("ssnote").textContent = "Searching subf2m and OpenSubtitles for this exact release..."; $("sslist").innerHTML = "";
  api("/api/subs/" + PL.id + "/search", { lang: $("sslang").value, query: $("ssq").value }).then(function (r) {
    if (!$("ssq").value) $("ssq").value = r.query;
    $("ssnote").innerHTML = r.results.length ? r.results.length + " subtitles for &ldquo;" + esc(r.query) + "&rdquo; &mdash; sorted by how well they match <b>" + esc(PL.it.name) + "</b>" : "Nothing found for &ldquo;" + esc(r.query) + "&rdquo;. Try another title.";
    $("sslist").innerHTML = r.results.map(function (x, i) {
      var col = x.score >= 85 ? "#30c47b" : x.score >= 60 ? "#f5a524" : "#8e8e9a";
      return "<div class='ssr" + (x.best ? " best" : "") + "'><div class=sc style='--p:" + x.score + ";--g:" + col + "'><span>" + x.score + "%</span></div><div><div class=rl>" + (x.best ? "<span class=tag>BEST MATCH</span>" : "") + esc(x.release || "-") + "</div>" +
        "<div class=mt><span class=tag style='background:" + (x.src == "opensubtitles" ? "#1f6feb" : "#2e9d4a") + "'>" + (x.src == "opensubtitles" ? "OpenSubtitles" : "subf2m") + "</span>by <b>" + esc(x.by || "?") + "</b>" + (x.rated ? " &middot; " + esc(x.rated) : "") + " &middot; " + x.releases.length + " release" + (x.releases.length == 1 ? "" : "s") + "</div>" + (x.note ? "<div class=nt dir=auto>" + esc(x.note) + "</div>" : "") + "</div>" +
        "<button class='btn" + (x.best ? " pri" : "") + "' data-i=" + i + ">" + (x.added ? "Added &#10003;" : "Add") + "</button></div>";
    }).join("");
    $("sslist").querySelectorAll("button[data-i]").forEach(function (b) { b.onclick = function () { subGet(r.results[+b.dataset.i], b) } });
  }).catch(function (e) { $("ssnote").textContent = "Error: " + e.message });
}
function subGet(x, b) {
  b.disabled = true; b.textContent = "Adding...";
  var lang = $("sslang").options[$("sslang").selectedIndex].text;
  api("/api/subs/" + PL.id + "/get", { path: x.path, id: x.id, name: x.name, label: lang + " \u00b7 " + (x.by || "subtitle") + " \u00b7 " + x.score + "% match" }).then(function () {
    b.textContent = "Added \u2713"; toast("Subtitle added and selected \u2014 checking its timing against the movie");
    return pSubSetup(PL.id);
  }).then(function () { setTimeout(function () { pSubSetup(PL.id) }, 8000) }).catch(function (e) { b.disabled = false; b.textContent = "Add"; toast("Error: " + e.message) });
}
function subUpload(body) {
  toast("Adding subtitle...");
  api("/api/subs/" + PL.id + "/add", body).then(function () { return pSubSetup(PL.id) }).then(function () { SUBM.open = true; SUBM.el.className = "smenu on"; subMenuRender(); toast("Subtitle added") })
    .catch(function (e) { toast("Error: " + e.message) });
}
document.addEventListener("fullscreenchange", function () { setTimeout(subStyle, 50) }); window.addEventListener("resize", function () { if (PL) subStyle() });
function pModeBtn() { $("pmode").innerHTML = PL.canCompat ? "<button class=btn onclick='pMode()'>" + (PL.compat ? "Original sound" : "With sound (convert)") + "</button>" : "" }
function pSrc(t) { return "/stream/" + PL.id + "/x?ik=" + ST.mk + (PL.compat ? "&compat=mp4&t=" + t.toFixed(2) + "&a=" + ($("ptrack").value || 0) : "") }
function pStart(t) {
  var tag = PL.it.cat == "Audio" ? "audio" : "video", v = $("pv");
  if (!v) { $("pbox").innerHTML = "<" + tag + " id=pv autoplay playsinline></" + tag + ">"; v = $("pv"); v.addEventListener("click", function () { if (PL && PL.compat) pToggle() }) }
  PL.t0 = PL.compat ? t : 0; v.controls = !PL.compat;
  $("pctl").style.display = PL.compat ? "flex" : "none";
  $("ptrack").style.display = PL.compat && $("ptrack").options.length > 1 ? "" : "none";
  v.src = pSrc(t);
  if (!PL.compat && t) v.addEventListener("loadedmetadata", function f() { v.currentTime = t; v.removeEventListener("loadedmetadata", f) });
  v.play().catch(function () {});
}
function pNow() { var v = $("pv"); return v ? PL.t0 + (v.currentTime || 0) : 0 }
function pMode() { var t = pNow(); PL.compat = !PL.compat; pModeBtn(); pStart(t) }
function pToggle() { var v = $("pv"); if (v) v.paused ? v.play() : v.pause() }
function pFull() { document.fullscreenElement ? document.exitFullscreen() : $("pwrap").requestFullscreen() }
$("ptrack").addEventListener("change", function () { pStart(pNow()) });
$("pseek").addEventListener("input", function () { SEEKING = true; $("ptime").textContent = tfmt(this.value / 1000 * PL.dur) + " / " + tfmt(PL.dur) });
$("pseek").addEventListener("change", function () { SEEKING = false; pStart(this.value / 1000 * PL.dur) });
function pTick() {
  if (!PL || !PL.compat) return; var v = $("pv"); if (!v) return;
  $("ppbtn").innerHTML = v.paused ? I.play : I.pause;
  if (!SEEKING) { $("ptime").textContent = tfmt(pNow()) + " / " + tfmt(PL.dur); if (PL.dur) $("pseek").value = Math.min(1000, pNow() / PL.dur * 1000) }
}
function closePlay() { SUBS.cues = []; SUBS.url = null; if (SUBS.el) SUBS.el.innerHTML = ""; SUBM.open = false; var v = $("pv"); if (v) { v.pause(); v.removeAttribute("src"); v.load() } $("pbox").innerHTML = ""; PLAYING = null; PL = null; if (document.fullscreenElement) document.exitFullscreen(); closeM() }
function copyStream() { var it = byId(PLAYING); navigator.clipboard.writeText(streamUrl(it)).then(function () { toast("Stream link copied") }) }
// share / TV
function openShare(id) { SHARE = id; show("mshare"); renderShare(); tvRender(); if (!(ST.tvs || []).length) tvFind() }
function urlRow(t, u) { return "<label>" + t + "</label><div style='display:flex;gap:8px'><input class=in readonly value='" + esc(u) + "' onclick='this.select()'><button class=btn data-u='" + esc(u) + "' onclick='copyText(this.dataset.u)'>Copy</button><button class=btn title='Show as QR code' data-u='" + esc(u) + "' data-t='" + esc(t.split(" &mdash;")[0]) + "' onclick='qrShow(this.dataset.u, this.dataset.t)'>QR</button></div>" }
function qrShow(u, t) { $("qrimg").src = "/qr.svg?d=" + encodeURIComponent(u); $("qrcap").innerHTML = "<b>" + t + "</b><br><span>" + esc(u) + "</span>"; $("qrbox").classList.remove("hide") }
function copyText(u) { navigator.clipboard.writeText(u).then(function () { toast("Link copied") }) }
function renderShare() {
  var it = byId(SHARE); if (!it) return;
  var L = ST.lan, n = ST.net;
  $("slan").checked = L.on_lan; $("sdlna").checked = L.dlna;
  $("shint").innerHTML = "This Mac on your network: <b>" + esc(n.local_ip || "?") + "</b>" + (/^172\.20\.10\./.test(n.local_ip || "") ? " &mdash; <b style='color:var(--amber)'>you are on an iPhone hotspot, the TV must join the same hotspot</b>" : "");
  var h;
  if (!L.running) { h = "<div class=note style='margin-top:14px'>Turn on sharing to get a link for your TV, phone or another computer.</div>"; $("qrbox").classList.add("hide"); $("qrbox")._u = null }
  else {
    var raw = L.base + "/" + shortPath(it), comp = L.base + "/" + shortPath(it, true);
    var page = L.base + "/" + it.num;
    setTimeout(function () { if (!$("qrbox")._u || $("qrbox")._u.indexOf(L.base) != 0) { qrShow(page, "Browser player"); $("qrbox")._u = page } }, 0);
    h = (it.cat == "Video" && L.ffmpeg ? urlRow("Browser player &mdash; open in the TV's web browser (LG, Samsung, Android TV) or any phone / computer browser; subtitles included", L.base + "/" + it.num) : "") +
      urlRow("Original quality &mdash; VLC, Kodi, Infuse, Plex or the TV's player (can seek)", raw);
    if (it.cat == "Video" && L.ffmpeg) h += urlRow("Original sound + chosen subtitle (sync applied) &mdash; for players without external subtitles", L.base + "/" + shortPath(it).replace(/^(\d+)\.\w+$/, "$1t.mkv"));
    if (it.cat == "Video") h += L.ffmpeg ? urlRow("TV-compatible sound (AAC 5.1) + chosen subtitle &mdash; if the TV shows the picture but has no sound. Picture stays identical", comp) : "<div class=note style='margin-top:10px'>TV has picture but no sound (DTS)? Install ffmpeg (<code>brew install ffmpeg</code>) for a TV-compatible link.</div>";
    if (L.dlna) h += "<div class='ban info' style='margin-top:14px'>" + I.info + "<div><b>On the TV:</b> open Media / Sources / Home network and pick <b>" + esc(L.name) + "</b>.</div></div>";
    h += "<div class=note style='margin-top:10px'>VLC on a TV: Browse &rarr; Network stream &rarr; enter the link. Only your local network can open it; the secret code keeps others from guessing it. The control panel stays private to this Mac.</div>";
  }
  setHtml($("surls"), h);
}
// TVs on the network
function tvRender() {
  var tvs = (ST && ST.tvs) || [], it = byId(SHARE);
  $("tvlist").innerHTML = tvs.length ? tvs.map(function (t) {
    var on = t.online !== false;
    return "<div class='tvr" + (on ? "" : " off") + "'><span class=ti>" + I.tv + "</span><div class=tn><b>" + esc(t.name) + "</b><span>" + esc([t.maker, t.model, t.ip].filter(Boolean).join(" \u00b7 ")) + "</span></div>" +
      (it && it.cat == "Video" && ST.lan.ffmpeg ? "<button class='btn pri' onclick=\"tvCast('" + t.id + "','tv')\">" + I.play + "Play</button><button class=btn title='Original DTS sound (only if the TV supports DTS)' onclick=\"tvCast('" + t.id + "','orig')\">Original sound</button>"
        : "<button class='btn pri' onclick=\"tvCast('" + t.id + "','orig')\">" + I.play + "Play</button>") + "</div>";
  }).join("") : "<div class=note>Press <b>Find TVs</b>. The TV must be on and on the same network (on LG: Settings &rarr; General &rarr; Devices &rarr; &ldquo;LG Connect Apps&rdquo; / &ldquo;Mobile TV On&rdquo; on).</div>";
  var c = ST && ST.casting;
  var am = ST.audio_modes || {}, cur = ST.settings.tv_audio || "original";
  $("tvctl").innerHTML = "<div class=two style='margin-top:10px'><div><label style='margin-top:0'>TV sound</label><select class=in onchange=\"shSet({tv_audio:this.value})\">" +
    Object.keys(am).map(function (k) { return "<option value=" + k + (k == cur ? " selected" : "") + ">" + esc(am[k]) + "</option>" }).join("") + "</select></div>" +
    "<div><label style='margin-top:0'>While watching, limit downloads to (MB/s)</label><input class=in type=number min=0 step=0.5 value='" + (ST.settings.watch_limit_mb || 0) + "' onchange=\"shSet({watch_limit_mb:this.value})\"></div></div>" +
    "<div class=note>Video needs about 60 Mbit/s over your Wi-Fi; a running download on the same Wi-Fi (e.g. an iPhone hotspot) can make it stutter. 0 = no limit. Sound changes apply the next time you press Play.</div>" + (c ? "<div class=castbar>" + I.cast + "<span>Playing <b>" + esc(c.title) + "</b> on <b>" + esc(c.name) + "</b></span><button class=btn onclick=\"tvCtl('pause')\">" + I.pause + "</button><button class=btn onclick=\"tvCtl('play')\">" + I.play + "</button><button class=btn onclick=\"tvCtl('stop')\">Stop</button></div>" : "");
}
function tvFind() {
  $("tvfind").disabled = true; $("tvstat").textContent = "searching the network...";
  api("/api/tvs/find", {}).then(function (r) { ST.tvs = r.tvs; var n = r.tvs.filter(function (t) { return t.online }).length; $("tvstat").textContent = n ? n + " found" : "no TV answered"; tvRender() })
    .catch(function (e) { $("tvstat").textContent = "Error: " + e.message }).then(function () { $("tvfind").disabled = false });
}
function tvCast(tv, sound) {
  toast("Sending to the TV...");
  api("/api/tvs/cast", { id: SHARE, tv: tv, sound: sound }).then(function (c) { toast("Playing on " + c.name); return poll() }).then(function () { renderShare(); tvRender() })
    .catch(function (e) { toast("Error: " + e.message) });
}
function tvCtl(a) { api("/api/tvs/ctl/" + a, {}).then(poll).then(tvRender).catch(function (e) { toast("Error: " + e.message) }) }
// play on a TV: dlus player (webOS remote API) or the TV's own player (DLNA)
var TVP = null;
function lanSet2(on) { return api("/api/settings", { lan_on: on }).then(poll) }
function openTvPlay(id) { TVP = id; var it = byId(id); $("tvtitle").textContent = "Play “" + it.name.replace(/\.[^.]+$/, "").replace(/[._]/g, " ").slice(0, 60) + "” on TV"; show("mtv"); tvpRender(); if (!(ST.tvs || []).length) tvpFind() }
function tvpFind() { $("tvpfind").disabled = true; $("tvpstat").textContent = "searching your network...";
  api("/api/tvs/find", {}).then(function (r) { ST.tvs = r.tvs; $("tvpstat").textContent = r.tvs.filter(function (t) { return t.online }).length ? "" : "no TV answered - is it on and on this network?"; tvpRender() })
    .catch(function (e) { $("tvpstat").textContent = "Error: " + e.message }).then(function () { $("tvpfind").disabled = false }) }
function tvpRender() {
  var tvs = ST.tvs || [];
  $("tvplist").innerHTML = tvs.length ? tvs.map(function (t) {
    return "<div class=tvr><span class=ti>" + I.tv + "</span><div class=tn><b>" + esc(t.name) + "</b><span>" + esc([t.model, t.ip].filter(Boolean).join(" · ")) + (t.paired ? " · <span style='color:var(--green)'>connected</span>" : "") + "</span></div>" +
      "<button class='btn pri' title='The TV plays the original file itself: full 4K HDR picture processing, original sound, subtitle included' onclick=\"tvpNative('" + t.id + "')\">" + I.play + "LG player &middot; best picture</button>" +
      "<button class=btn title='dlus controls and subtitle styles; the TV browser shows a weaker picture' onclick=\"tvpOpen('" + t.id + "')\">dlus player</button>" +
      (t.paired ? "<button class=btn title='Remove the pairing: the TV asks to Allow dlus again next time' onclick=\"if(confirm('Forget this TV? Next time the TV asks to allow dlus again.'))api('/api/tv/" + t.id + "/forget',{}).then(function(){toast('TV forgotten');poll()})\">Forget</button>" : "") + "</div>";
  }).join("") : "<div class=note>No TV found yet.</div>";
  var dq = ST.settings.dlna_quality || "saver", ql = ST.qualities || {};
  $("tvpnote").innerHTML = "<div style='display:flex;gap:10px;align-items:center;margin-bottom:8px'><span>Quality for the TV's own player:</span><select class=in style='width:auto' onchange=\"api('/api/settings',{dlna_quality:this.value}).then(poll)\">" +
    Object.keys(ql).map(function (k) { return "<option value=" + k + (k == dq ? " selected" : "") + ">" + esc(ql[k]) + "</option>" }).join("") + "</select></div>" +
    "<b>LG player</b> (recommended): the TV plays the original file itself &mdash; full 4K HDR picture with the TV's own processing, the original sound, and the subtitle you chose. " +
    "<div style='display:flex;gap:10px;align-items:center;margin:8px 0'><span>Sound for LG player:</span><select class=in style='width:auto' id=tvpsnd><option value=tv>Dolby Digital Plus 5.1 &mdash; the film's own mix (works on every LG TV)</option><option value=orig>Untouched original (DTS / TrueHD only if your TV or soundbar decodes it)</option></select></div>" +
    "<b>dlus player</b>: dlus controls, styled subtitles and the remote on the Mac; the TV's browser shows a weaker picture (smaller, less HDR). First time: the TV asks to allow a remote-control app &mdash; choose <b>Allow</b>.";
}
function tvpOpen(tv) {
  var t = (ST.tvs || []).filter(function (x) { return x.id == tv })[0];
  toast(t && t.paired ? "Opening dlus on the TV..." : "Look at the TV and press Allow");
  api("/api/tv/" + tv + "/open", { id: TVP }).then(function () { toast("Playing on the TV — use the remote: OK, ◀▶ seek, RED for subtitles"); poll() })
    .catch(function (e) { toast("Error: " + e.message) });
}
function tvpNative(tv) { toast("Sending to LG's player...");
  api("/api/tvs/cast", { id: TVP, tv: tv, sound: $("tvpsnd") && $("tvpsnd").value == "orig" ? "orig" : "tv" }).then(function (c) { toast("Playing on " + c.name); poll() }).catch(function (e) { toast("Error: " + e.message) }) }
var RI = (function () {
  function sv(p) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>' }
  return {
    power: sv('<path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/>'), input: sv('<path d="M14.5 4.5h-6a5 5 0 0 0-5 5v5a5 5 0 0 0 5 5h7a5 5 0 0 0 5-5v-1"/><path d="M12 12l8.5-8.5M15 3.5h5.5V9"/><circle cx="12" cy="12" r="2.2"/>'),
    hub: sv('<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v9.5h13V10"/><circle cx="12" cy="14.5" r="2"/>'), a11y: sv('<circle cx="12" cy="4.5" r="1.8"/><path d="M5 8.5l7 1.5 7-1.5M12 10v5l-3 6M12 15l3 6"/>'),
    mic: sv('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>'), gear: sv('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
    plus: sv('<path d="M12 5v14M5 12h14"/>'), minus: sv('<path d="M5 12h14"/>'), up: sv('<path d="m6 15 6-6 6 6"/>'), down: sv('<path d="m6 9 6 6 6-6"/>'),
    left: sv('<path d="m15 6-6 6 6 6"/>'), right: sv('<path d="m9 6 6 6-6 6"/>'), mute: sv('<path d="M11 5 6 9H3v6h3l5 4z"/><path d="m22 9-6 6M16 9l6 6"/>'),
    back: sv('<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>'), home: sv('<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10"/>'),
    guide: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="5" width="16" height="2" rx="1"/><rect x="4" y="9" width="4" height="2" rx="1"/><rect x="10" y="9" width="10" height="2" rx="1"/><rect x="4" y="13" width="4" height="2" rx="1"/><rect x="10" y="13" width="10" height="2" rx="1"/><text x="12" y="22.5" font-size="5.6" font-weight="800" text-anchor="middle" font-family="-apple-system,system-ui,sans-serif">GUIDE</text></svg>', rew: sv('<path d="M11 19 2 12l9-7zM22 19l-9-7 9-7z"/>'),
    playp: sv('<path d="M5 4v16l9-8z"/><path d="M17 5v14M21 5v14"/>'), ff: sv('<path d="m13 19 9-7-9-7zM2 19l9-7-9-7z"/>'), stop: sv('<rect x="6" y="6" width="12" height="12" rx="1.5"/>'),
    heart: sv('<path d="M12 20s-7.5-4.6-9.2-9.3C1.7 7.4 3.8 4.5 7 4.5c2 0 3.6 1.1 5 3 1.4-1.9 3-3 5-3 3.2 0 5.3 2.9 4.2 6.2C19.5 15.4 12 20 12 20z"/>'),
    ai: sv('<path d="M12 3.5c2.2 0 3.6 1.6 3.6 3.6M8.4 7.1c0-2 1.4-3.6 3.6-3.6M20.5 12c0 2.2-1.6 3.6-3.6 3.6M16.9 8.4c2 0 3.6 1.4 3.6 3.6M12 20.5c-2.2 0-3.6-1.6-3.6-3.6M15.6 16.9c0 2-1.4 3.6-3.6 3.6M3.5 12c0-2.2 1.6-3.6 3.6-3.6M7.1 15.6c-2 0-3.6-1.4-3.6-3.6"/><path d="M8.4 7.1 16.9 15.6M15.6 7.1 7.1 15.6" opacity=".9"/>'),
    dots: sv('<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>'), screen: sv('<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M4 4l16 13"/>')
  };
})();
var R123 = false, RAPPS = null, RINS = null, RSTAT = null, RSTT = 0, RLONG = null, RLONGED = false;
var HEARTS = { MUTE: "Mute", SCREENOFF: "Screen off / on", INPUTS: "Inputs list", DLUS: "Open dlus", A11Y: "Accessibility", HOMEHUB: "Home Hub", QMENU: "Quick settings" };
function heart() { return localStorage.getItem("dlus-heart") || "MUTE" }
function rHit(el) { if (!el) return; el.classList.add("hit"); setTimeout(function () { el.classList.remove("hit") }, 160) }
function rSend(n) {
  if (!RTV) return toast("Pick a TV first");
  if (n == "HEART") n = heart();
  if (n == "SCREENOFF") { RSTAT = !RSTAT; return tvAct("screen", { on: !RSTAT }).then(function (r) { if (r) toast(RSTAT ? "Screen off - sound keeps playing" : "Screen on") }) }
  if (n == "INPUTS") return rLoadInputs(true);
  if (n == "DLUS") return rApp(null, "dlus");
  api("/api/tv/" + RTV + "/button", { name: n }).then(function (r) { if (r.via == "app") toast("Opened on the TV"); setTimeout(rStatus, 700) }).catch(function (e) { toast(e.message) });
}
function rPress(el, n) { rHit(el); rSend(n) }
// press = first function, hold 0.6 s = the function printed under the key
function rDown(el, n, long) { RLONGED = false; clearTimeout(RLONG); if (long) RLONG = setTimeout(function () { RLONGED = true; rHit(el); rSend(long); toast(long == "MUTE" ? "Mute" : "Long press: " + long) }, 600) }
function rUp(el, n) { clearTimeout(RLONG); if (!RLONGED) rPress(el, n) }
function rApp(el, id) {
  rHit(el); if (!RTV) return toast("Pick a TV first");
  if (id == "dlus") return tvAct("open").then(function (r) { if (r) toast("dlus is open on the TV") });
  api("/api/tv/" + RTV + "/launch", { app: id }).then(function () { setTimeout(rStatus, 1500) }).catch(function (e) { toast(e.message) });
}
function rk(n, ic, cls, title, label, long) {
  return "<button class='mk " + (cls || "") + "' title='" + title + "' onmousedown=\"rDown(this,'" + n + "','" + (long || "") + "')\" onmouseup=\"rUp(this,'" + n + "')\" onmouseleave='clearTimeout(RLONG)'>" +
    ic + (label ? "<span class=lb>" + label + "</span>" : "") + "</button>";
}
function remoteHtml() {
  var rb = function (n, cls, ic) { return "<button class=" + cls + " onclick=\"rPress(this,'" + n + "')\">" + ic + "</button>" };
  var core = R123 ? "<div class=nump>" + ["1", "2", "3", "4", "5", "6", "7", "8", "9", "LIST", "0", "DASH"].map(function (k) {
      return "<button class=mk onclick=\"rPress(this,'" + k + "')\">" + (k == "LIST" ? "<span style='font-size:9px'>LIST</span>" : k == "DASH" ? "&ndash;" : k) + "</button>" }).join("") + "</div>"
    : "<div class=mring onwheel=\"event.preventDefault();rWheel(event)\">" + rb("UP", "u", RI.up) + rb("LEFT", "l", RI.left) + rb("RIGHT", "r", RI.right) + rb("DOWN", "d", RI.down) +
      "<button class=ok title='OK (scroll = up / down)' onclick=\"rPress(this,'ENTER')\"></button></div>";
  var bar = function (up, down, iu, idn, mute) {
    return "<div class=bar><button class=t onclick=\"rPress(this,'" + up + "')\">" + iu + "</button><div class=pill></div>" +
      "<button class=b onmousedown=\"rDown(this,'" + down + "','" + (mute ? "MUTE" : "") + "')\" onmouseup=\"rUp(this,'" + down + "')\" onmouseleave='clearTimeout(RLONG)'>" + (mute ? "<span class=mu>" + RI.mute.replace("<svg", "<svg style='width:11px;height:11px'") + "</span>" : "") + idn + "</button></div>";
  };
  var ap = function (a, b) { return "<div class=ap>" + a + "<i></i>" + b + "</div>" };
  var ab = function (id, cls, inner) { return "<button class=" + cls + " onclick=\"rApp(this,'" + id + "')\">" + inner + "</button>" };
  return "<div class=mr>" +
    "<div class=g style='align-items:center'>" + rk("POWER", RI.power, "pw", "Power off") + "<div class=mic style='justify-self:center'><i></i><i></i><i></i><b></b></div>" +
      rk("HEART", RI.heart, "", "Heart key: " + HEARTS[heart()] + " (change it on the right)") + "</div>" +
    "<div class=g style='margin-top:12px'>" + rk("INPUTS", RI.input, "", "Inputs", "INPUT") + rk("AI", RI.ai, "ai", "AI", "AI", "MIC") + rk("GUIDE", RI.guide, "num", "Guide - hold: channel list", "LIST", "LIST") + "</div>" +
    "<div class=g style='margin-top:-4px'>" + rk("HOME", RI.home, "", "Home - hold: recent apps", "", "RECENT") + "<span></span>" +
      "<button class='mk num' title='Number keys - hold: audio description' onmousedown=\"rDown(this,'NUM','SAP')\" onmouseup=\"clearTimeout(RLONG);if(!RLONGED){R123=!R123;$('tvpage')._h=null;renderTvPage()}\" onmouseleave='clearTimeout(RLONG)'>" +
      (R123 ? "OK" : "123<small>&bull;&bull;&bull;</small>") + "<span class=lb>AD/SAP</span></button></div>" +
    core +
    "<div class=g>" + rk("BACK", RI.back, "", "Back - hold: exit", "", "EXIT") + "<span></span>" + rk("SETTINGS", RI.gear, "", "Settings - hold: all settings", "", "MENU") + "</div>" +
    "<div class=rck>" + bar("VOLUMEUP", "VOLUMEDOWN", RI.plus, RI.minus, true) + bar("CHANNELUP", "CHANNELDOWN", RI.up, RI.down, false) + "</div>" +
    "<div class=aps>" + ap(ab("netflix", "nf", "NETFLIX"), ab("amazon", "pv", "prime video")) + ap(ab("com.disney.disneyplus-prod", "dp", "Disney+"), ab("com.webos.app.lgchannels", "lgc", "LG Channels")) +
      ap(ab("dlus", "dl", "<b></b>dlus"), ab("youtube.leanback.v4", "yt", "<b></b>YouTube")) + "</div>" +
    "<div class=logo><b>dl</b>dlus</div></div>";
}
function rWheel(e) { var t = Date.now(); if (t - (rWheel.t || 0) < 180) return; rWheel.t = t; rSend(e.deltaY > 0 ? "DOWN" : "UP") }
function rsideHtml() {
  return "<div class=rside><div class=rnow id=rnow><span class=note style='margin:0'>Connect the TV to see what is on it</span></div>" +
    "<div class=heart><label style='margin:0;white-space:nowrap'>" + RI.heart.replace("<svg", "<svg style='width:14px;height:14px;vertical-align:-2px'") + " Heart key does</label><select class=in style='width:auto' onchange=\"localStorage.setItem('dlus-heart',this.value);$('tvpage')._h=null;renderTvPage()\">" +
      Object.keys(HEARTS).map(function (k) { return "<option value=" + k + (k == heart() ? " selected" : "") + ">" + HEARTS[k] + "</option>" }).join("") + "</select><span class=note style='margin:0'>like on the real remote</span></div>" +
    "<div><label style='margin-top:0'>Type on the TV <span style='color:var(--dim)'>(search boxes, YouTube, browser &mdash; instead of the mic)</span></label><div class=kbd><input class=in id=rtext placeholder='Type and press Enter' onkeydown=\"if(event.key=='Enter'){rType();event.preventDefault()}\"><button class=btn onclick='rType()'>Send</button><button class=btn title='Delete one letter' onclick=\"tvAct('text',{delete:1})\">&#9003;</button></div></div>" +
    "<div><label>Inputs <a class=lnk onclick='rLoadInputs(false)'>refresh</a></label><div class=chips id=rins><span class=note style='margin:0'>Press refresh</span></div></div>" +
    "<div><label>Apps on the TV <a class=lnk onclick='rLoadApps()'>load</a></label><div class=agrid id=rapps><span class=note style='margin:0'>Press load</span></div></div>" +
    "<div><label style='margin-top:0'>On-screen keys <span style='color:var(--dim)'>(the real remote shows these after 123 &bull;&bull;&bull;)</span></label><div class=chips>" +
      [["RED", "#e53935"], ["GREEN", "#43a047"], ["YELLOW", "#fdd835"], ["BLUE", "#1e88e5"]].map(function (c) { return "<button onclick=\"rPress(this,'" + c[0] + "')\"><span style='display:inline-block;width:10px;height:10px;border-radius:3px;background:" + c[1] + ";margin-right:6px'></span>" + c[0].toLowerCase() + "</button>" }).join("") +
      [["REWIND", "&#9194;"], ["PLAY", "&#9654;"], ["PAUSE", "&#10074;&#10074;"], ["FASTFORWARD", "&#9193;"], ["STOP", "&#9632;"], ["INFO", "info"], ["QMENU", "quick settings"], ["A11Y", "accessibility"], ["HOMEHUB", "Home Hub"]].map(function (c) { return "<button onclick=\"rPress(this,'" + c[0] + "')\">" + c[1] + "</button>" }).join("") + "</div></div>" +
    "<div class=chips><button onclick=\"tvAct('screen',{on:false}).then(function(r){if(r)toast('Screen off - sound keeps playing')})\">Screen off</button><button onclick=\"tvAct('screen',{on:true})\">Screen on</button>" +
    "<button onclick=\"toast('Waking the TV (up to 20 s)...');tvAct('wake').then(function(r){if(r)toast('The TV is on')})\">Wake TV</button><button onclick=\"tvAct('toast',{message:'Hello from dlus'})\">Test message</button></div>" +
    "<div class=note>Hold a key for the function printed under it (INPUT, LIST, AD/SAP; hold &minus; = mute, hold Home = recent apps). Scroll on the wheel = up / down. <b>Click the remote</b> to drive the TV with your keyboard: arrows, Enter = OK, &#9003; = Back, H = Home, + / &minus; = volume, M = mute; Esc or a click elsewhere stops it. Inside the dlus player on the TV every button works; in other apps LG only lets remote apps use some buttons &mdash; dlus opens the right screen instead when it can.</div></div>";
}
function rType() { var v = $("rtext").value; if (!v) return tvAct("text", { enter: true }); tvAct("text", { text: v }).then(function (r) { if (r) { $("rtext").value = ""; toast("Typed on the TV") } }) }
function rLoadApps() { if (!RTV) return toast("Pick a TV first"); $("rapps").innerHTML = "<span class=note style='margin:0'>loading...</span>";
  api("/api/tv/" + RTV + "/apps", {}).then(function (r) { RAPPS = r.apps; $("rapps").innerHTML = r.apps.map(function (a) {
    return "<button onclick=\"rApp(this,'" + esc(a.id) + "')\" title='" + esc(a.id) + "'><img src='" + esc(a.icon) + "' alt='' onerror=\"this.style.visibility='hidden'\">" + esc(a.title) + "</button>" }).join("") })
    .catch(function (e) { $("rapps").innerHTML = "<span class=note style='margin:0'>" + esc(e.message) + "</span>" }) }
function rLoadInputs(open) { if (!RTV) return toast("Pick a TV first");
  api("/api/tv/" + RTV + "/inputs", {}).then(function (r) { RINS = r.inputs; $("rins").innerHTML = r.inputs.map(function (x) {
    return "<button class='" + (x.connected ? "" : "dim") + "' onclick=\"tvAct('input',{input:'" + esc(x.id) + "'}).then(function(r){if(r)toast('Switched to " + esc(x.label) + "')})\">" + esc(x.label) + "</button>" }).join("") || "<span class=note style='margin:0'>no inputs</span>";
    if (open) $("rins").scrollIntoView({ behavior: "smooth", block: "center" }) })
    .catch(function (e) { $("rins").innerHTML = "<span class=note style='margin:0'>" + esc(e.message) + "</span>"; if (open) toast(e.message) }) }
function rStatus() {
  var t = (ST.tvs || []).filter(function (x) { return x.id == RTV })[0];
  if (!t || !t.paired || VIEW != "tv" || !$("rnow")) return;
  RSTT = Date.now();
  api("/api/tv/" + RTV + "/status", {}).then(function (r) {
    var app = r.player ? "dlus player" : (RAPPS || []).filter(function (a) { return a.id == r.app })[0];
    app = typeof app == "string" ? app : app ? app.title : (r.app || "").replace(/^com\.(webos|palm)\.app\./, "") || "TV";
    $("rnow").innerHTML = "<span class=ti>" + I.tv + "</span><div style='min-width:120px'><b>" + esc(app) + "</b><div class=t2>on the TV now</div></div>" +
      "<div class=vol>" + RI.mute.replace("<svg", "<svg style='width:18px;height:18px;cursor:pointer' onclick=\"rPress(null,'MUTE')\"") +
      "<input type=range min=0 max=100 value='" + (r.volume || 0) + "' onchange=\"tvAct('setvol',{level:this.value})\"><b style='width:46px;text-align:right'>" + (r.muted ? "muted" : (r.volume != null ? r.volume : "-")) + "</b></div>";
  }).catch(function () {});
}
setInterval(function () { if (VIEW == "tv" && Date.now() - RSTT > 5000) rStatus() }, 1000);
var RKEYS = false;  // the Mac keyboard drives the TV only after you click the remote
function rKeys(on) { RKEYS = on; var m = document.querySelector(".mr"); if (m) m.classList.toggle("keys", on) }
document.addEventListener("mousedown", function (e) { if (VIEW == "tv") rKeys(!!(e.target.closest && e.target.closest(".mr"))) }, true);
document.addEventListener("keydown", function (e) {
  if (!RKEYS || VIEW != "tv" || anyModal() || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key == "Escape" && !e.shiftKey) { rKeys(false); e.preventDefault(); e.stopImmediatePropagation(); return }
  var k = { ArrowUp: "UP", ArrowDown: "DOWN", ArrowLeft: "LEFT", ArrowRight: "RIGHT", Enter: "ENTER", Backspace: "BACK", h: "HOME", H: "HOME", "+": "VOLUMEUP", "=": "VOLUMEUP", "-": "VOLUMEDOWN", m: "MUTE", M: "MUTE" }[e.key];
  if (k && RTV) { e.preventDefault(); e.stopImmediatePropagation(); rPress(null, k) }
}, true);
// ---------- VPN assistant: pick a device, dlus sets up the Mac, helps on the TV and checks everything live
var GWW = { mac: "", t: null, r: null };
function gwwOpen(mac) {
  var ds = (ST.gw.devices || []).slice();
  (ST.tvs || []).forEach(function (t) { if (t.mac && !ds.some(function (d) { return d.mac == t.mac || d.ip == t.ip })) ds.push({ mac: t.mac, ip: t.ip, name: t.name + " (off right now)", kind: "tv" }) });
  ds.sort(function (a, b) { return (b.kind == "tv") - (a.kind == "tv") }); GWW.ds = ds;
  if (!ds.length) return toast("No other devices seen on this network yet");
  GWW.mac = mac || (ds.filter(function (d) { return d.kind == "tv" })[0] || ds[0]).mac;
  $("gwwdev").innerHTML = ds.map(function (d) { return "<option value='" + d.mac + "'" + (d.mac == GWW.mac ? " selected" : "") + ">" + esc(d.name) + " · " + esc(d.ip) + "</option>" }).join("");
  $("gwwbody").innerHTML = "<div class=note>Press Start: dlus turns the router on and sends this device through the VPN.</div>";
  $("gwwgo").style.display = ""; show("mgw");
}
function gwwStart() {
  GWW.mac = $("gwwdev").value; $("gwwgo").style.display = "none";
  GWW.ip = (GWW.ds.filter(function (d) { return d.mac == GWW.mac })[0] || {}).ip;
  api("/api/gw/wizard", { mac: GWW.mac, ip: GWW.ip, act: "start" }).then(function (r) { gwwRender(r); clearInterval(GWW.t); GWW.t = setInterval(gwwCheck, 3000); poll() })
    .catch(function (e) { $("gwwgo").style.display = ""; toast("Error: " + e.message) });
}
function gwwCheck() {
  if (!$("mgw").classList.contains("on")) { clearInterval(GWW.t); return }
  api("/api/gw/wizard", { mac: GWW.mac, ip: GWW.ip }).then(gwwRender).catch(function () {});
}
function gwwTv(a, body) { var tv = GWW.r && GWW.r.tv; if (!tv) return;
  toast(GWW.r.paired ? "Sending to the TV..." : "Look at the TV and press Allow");
  return api("/api/tv/" + tv + "/" + a, body || {}).then(function () { toast("Done - look at the TV") }).catch(function (e) { toast("Error: " + e.message) }) }
function gwwRender(r) {
  GWW.r = r; var v = r.values, first = r.checks.filter(function (c) { return !c.ok })[0];
  var ck = r.checks.map(function (c) { return "<div class='gck " + (c.ok ? "ok" : "no") + "'><i>" + (c.ok ? "&#10003;" : "") + "</i><div>" + esc(c.ok ? c.t : c.bad) + "</div></div>" }).join("");
  var vals = "<div class=gwvals>" + [["IP address", v.ip], ["Subnet mask", v.mask], ["Router / Gateway", v.gw], ["DNS", v.dns]].map(function (x) {
    return "<div><span>" + x[0] + "</span><b>" + esc(x[1]) + "</b></div>" }).join("") + "</div>";
  var tvb = r.tv ? "<div class=chips style='margin-top:12px'><button onclick=\"gwwTv('open',{page:'netsetup'})\">1 &middot; Show the steps on the TV</button>" +
      "<button onclick=\"gwwTv('netsettings')\">2 &middot; Open the TV's network settings</button>" +
      "<button onclick=\"gwwTv('toast',{message:'dlus VPN · IP " + esc(v.ip) + " · Mask " + esc(v.mask) + " · Gateway " + esc(v.gw) + " · DNS " + esc(v.dns) + "'})\">Values as a TV message</button></div>" +
      "<div class=note>LG does not let any app change the TV's network settings, so you type the 4 values once (Settings &rarr; General &rarr; Network &rarr; Wi-Fi Connection &rarr; Advanced Wi-Fi Settings &rarr; Edit, untick <b>Set Automatically</b>). While the settings are open dlus keeps the values on the TV screen.</div>"
    : "<div class=note style='margin-top:10px'>On the device: Wi-Fi &rarr; this network &rarr; Configure IP <b>Manual</b> / Static, enter the values.</div>";
  var inst = !r.checks[0].ok ? "<div class=cmd><code>" + esc(ST.gw.install) + "</code><button class=btn data-u='" + esc(ST.gw.install) + "' onclick='copyText(this.dataset.u)'>Copy</button></div>" : "";
  $("gwwbody").innerHTML = (r.done ? "<div class=gdone>" + I.vpn + "Working: " + esc(r.name) + " goes through the VPN" + (r.rate ? " &middot; " + mbit(r.rate) : "") + "</div>" : "") +
    "<div class=gww><div><b style='font-size:13px'>Live check</b>" + ck + inst + "</div><div><b style='font-size:13px'>Values for " + esc(r.name) + "</b>" + vals + tvb + "</div></div>";
}
// re-render a page section only when it changed - and never while the user is typing or choosing in it
function setHtml(el, h) {
  if (el._h == h) return;
  var a = document.activeElement;
  if (a && el.contains(a) && (/INPUT|TEXTAREA|SELECT/.test(a.tagName) || a.isContentEditable)) return;  // tried again on the next update
  el.innerHTML = h; el._h = h;
}
// TV & remote page
var RTV = "";
function tvAct(a, body) { if (!RTV) return toast("Pick a TV first"); return api("/api/tv/" + RTV + "/" + a, body || {}).then(function (r) { poll(); return r }).catch(function (e) { toast("Error: " + e.message) }) }
function tvBtn(n) { tvAct("button", { name: n }) }
function tvVpn(ip) { var d = (ST.gw.devices || []).filter(function (x) { return x.ip == ip })[0] || (ST.tvs || []).filter(function (x) { return x.ip == ip })[0] || {}; gwwOpen(d.mac) }
function renderTvPage() {
  var tvs = ST.tvs || [], L = ST.lan, c = ST.casting;
  if (!RTV && tvs.length) RTV = (tvs.filter(function (t) { return t.paired })[0] || tvs[0]).id;
  var pad = function (n, label, cls) { return "<button class='rk " + (cls || "") + "' onclick=\"tvBtn('" + n + "')\">" + label + "</button>" };
  var h = "<div class=grid><div class=card><h3>" + I.tv + "TVs on your network <button class=btn style='margin-left:auto' onclick=\"tvpFind();setTimeout(renderTvPage,4000)\">Find TVs</button></h3>" +
    (tvs.length ? tvs.map(function (t) {
      return "<div class='tvr" + (t.id == RTV ? " sel" : "") + "' onclick=\"RTV='" + t.id + "';$('tvpage')._h=null;renderTvPage()\"><span class=ti>" + I.tv + "</span><div class=tn><b>" + esc(t.name) + "</b><span>" + esc(t.ip) + " · " + (t.paired ? "<span style='color:var(--green)'>remote connected</span>" : "not connected") + "</span></div>" +
        (t.paired ? "" : "<button class=btn onclick=\"event.stopPropagation();RTV='" + t.id + "';toast('Press Allow on the TV');tvAct('pair')\">Connect</button>") +
        "<button class=btn title='Send this TV through the VPN of this Mac' onclick=\"event.stopPropagation();tvVpn('" + t.ip + "')\">" + I.vpn + "VPN</button></div>";
    }).join("") : "<div class=note>No TV found yet. Turn the TV on and press Find TVs.</div>") +
    "<div class=ft style='justify-content:flex-start'><button class='btn pri' onclick=\"tvAct('open').then(function(){toast('dlus is open on the TV')})\">" + I.tv + "Open dlus on the TV</button></div></div>" +
    "</div><div class=card style='margin-top:12px'><h3>" + I.tv + "Remote <span class=cnt style='margin-left:6px;font-weight:500'>" + esc(((tvs.filter(function (t) { return t.id == RTV })[0]) || {}).name || "pick a TV") + "</span></h3><div class=rwrap>" + remoteHtml() + rsideHtml() + "</div></div>" +
    "<div class=grid style='margin-top:12px'>" +
    "<div class=card style='grid-column:1/-1'><h3>" + I.info + "Connect a TV yourself (no Mac needed)</h3><div class=two style='margin:0'><div>" +
      "<ol class=steps><li>On the TV open the <b>Web Browser</b></li><li>Go to <code style='font-size:15px'>" + esc((L.base || "").replace(/\/[^\/]+$/, "")) + "</code></li><li>Type this code with the remote: <b class=bigcode>" + esc((L.base || "").split("/").pop()) + "</b></li><li>Bookmark it &mdash; next time it opens dlus straight away</li></ol>" +
      "<div class=note>Phones and laptops: scan the QR code. &ldquo;New secret link&rdquo; in the Sharing window changes the code; TVs then have to enter the new one.</div></div>" +
      "<div class=qrbox style='margin:0'><img src='/qr.svg?d=" + encodeURIComponent((L.base || "") + "/tv") + "'><div><b>dlus home for TV &amp; phones</b><br>" + esc((L.base || "") + "/tv") + "</div></div></div></div>" +
    "<div class=card style='grid-column:1/-1'><h3>" + I.active + "Streaming priority <span class='badge " + (ST.throttled ? "b-acc'>active \u00b7 " + esc(ST.throttled) + " is watching" : "b-off'>nobody watching") + "</span></h3>" +
      "<div style='display:flex;gap:10px;align-items:center'><span>While a TV or device watches, limit downloads to</span><input class=in type=number min=0 step=0.5 style='width:110px' value='" + (ST.settings.watch_limit_mb != null ? ST.settings.watch_limit_mb : 1) + "' onchange=\"shSet({watch_limit_mb:this.value})\"><span>MB/s</span></div>" +
      "<div class=note>0 = no limit. Downloads go back to full speed when nothing is playing. Right now: <b id=tvnowspd></b> MB/s.</div></div>" +
    (c ? "<div class=card style='grid-column:1/-1'><h3>" + I.cast + "Playing in the TV's own player</h3><div class=castbar>" + I.cast + "<span><b>" + esc(c.title) + "</b> on <b>" + esc(c.name) + "</b></span><button class=btn onclick=\"tvCtl('pause')\">" + I.pause + "</button><button class=btn onclick=\"tvCtl('play')\">" + I.play + "</button><button class=btn onclick=\"tvCtl('stop')\">Stop</button></div></div>" : "") + "</div>";
  setHtml($("tvpage"), h);
  var sp = $("tvnowspd"); if (sp) sp.textContent = mbs(ST.items.reduce(function (a, it) { return a + (it.now || 0) }, 0));
}
function lanSet() { api("/api/settings", { lan_on: $("slan").checked, dlna_on: $("sdlna").checked }).then(poll).then(renderShare).catch(function (e) { toast("Error: " + e.message); poll().then(renderShare) }) }
function lanRotate() { api("/api/settings", { rotate_token: true }).then(poll).then(function () { $("qrbox")._u = null; renderShare(); toast("New secret link \u2014 old links stop working") }) }
// modals
function show(id) { $(id).classList.add("on") }
function closeM() { document.querySelectorAll(".modal").forEach(function (m) { m.classList.remove("on") }) }
function anyModal() { return document.querySelector(".modal.on") || $("cmdk").classList.contains("on") }
function countUrls() { var n = ($("aurls").value.match(/(https?|ftp):\/\/\S+/g) || []).length; $("acount").textContent = n ? n + " link" + (n > 1 ? "s" : "") + " found" : "" }
function openAdd(text) {
  closeCmd();
  $("aurls").value = text || ""; $("acs").value = ""; $("afolder").value = ""; countUrls();
  $("aconns").value = String(ST ? ST.settings.conns : 8);
  if (![].some.call($("aconns").options, function (o) { return o.value == $("aconns").value })) $("aconns").value = "8";
  $("adir").textContent = ST ? "Saves to " + tilde(ST.settings.base_dir) : "";
  show("madd"); setTimeout(function () { $("aurls").focus() }, 50);
  if (!text && navigator.clipboard && navigator.clipboard.readText) navigator.clipboard.readText().then(function (t) { if (/(https?|ftp):\/\//.test(t) && !$("aurls").value) { $("aurls").value = t.trim(); countUrls() } }).catch(function () {});
}
$("aurls").addEventListener("input", countUrls);
$("aurls").addEventListener("keydown", function (e) { if (e.key == "Enter" && (e.metaKey || e.ctrlKey)) doAdd() });
function pasteClip() { navigator.clipboard.readText().then(function (t) { $("aurls").value += ($("aurls").value ? "\n" : "") + t.trim(); countUrls() }).catch(function () { toast("Clipboard not allowed — paste with ⌘V") }) }
function doAdd() {
  api("/api/add", { urls: $("aurls").value, folder: $("afolder").value, conns: +$("aconns").value, checksum: $("acs").value, start: $("astart").checked })
    .then(function (r) { closeM(); toast(r.ids.length + " added" + (r.skipped.length ? ", " + r.skipped.length + " already in the list" : "")); poll() })
    .catch(function (e) { toast("Error: " + e.message) });
}
function openSet() {
  closeCmd(); var s = ST.settings; $("sdir").value = s.base_dir; $("smax").value = s.max_active; $("sconns").value = s.conns; $("slim").value = s.limit_mb || 0;
  $("swatch").value = s.watch_limit_mb != null ? s.watch_limit_mb : 1; $("suiopen").value = s.ui_open || "app";
  $("son").checked = !!s.sched_on; $("sfrom").value = s.sched_from; $("sto").value = s.sched_to; $("sdone").value = s.on_done || "none"; $("snot").checked = s.notify !== false;
  show("mset");
}
function doSet() {
  api("/api/settings", { base_dir: $("sdir").value, max_active: $("smax").value, conns: $("sconns").value, limit_mb: $("slim").value, watch_limit_mb: $("swatch").value,
    sched_on: $("son").checked, sched_from: $("sfrom").value, sched_to: $("sto").value, on_done: $("sdone").value, notify: $("snot").checked, ui_open: $("suiopen").value })
    .then(function () { closeM(); toast("Settings saved"); poll() }).catch(function (e) { toast("Error: " + e.message) });
}
function doDel(files) { var id = CUR; closeM(); act(id, "delete", { files: files }).then(function () { delete OPEN[id]; toast(files ? "Deleted with files" : "Removed from list") }) }
function doRef() { var u = $("rurl").value.trim(); if (!u) return; closeM(); act(CUR, "refresh", { url: u }).then(function () { toast("Link updated") }) }
function openAbout() { closeCmd(); $("aver").textContent = "v" + (ST ? ST.version : ""); $("adir2").textContent = ST ? tilde(ST.state_dir) : ""; show("mabout") }
// search box: paste a link -> add
$("q").addEventListener("input", function () { if (/^(https?|ftp):\/\/\S+/.test(this.value.trim())) { var t = this.value.trim(); this.value = ""; openAdd(t); countUrls() } else render() });
// command palette
var CMDS = [], CSEL = 0;
function openCmd() { closeM(); $("cmdk").classList.add("on"); $("cmdq").value = ""; buildCmd(); setTimeout(function () { $("cmdq").focus() }, 20) }
function closeCmd() { $("cmdk").classList.remove("on") }
function buildCmd() {
  var q = $("cmdq").value.trim().toLowerCase(), base = [
    ["New download", I.plus, openAdd, "N"], ["Pause all", I.pause, function () { act("all", "pause") }], ["Resume all", I.play, function () { act("all", "resume") }],
    ["Go to All downloads", I.all, function () { setView("all") }, "1"], ["Go to Active", I.active, function () { setView("active") }, "2"], ["Go to Queue", I.queue, function () { setView("queued") }, "3"],
    ["Go to Finished", I.done, function () { setView("done") }, "4"], ["Go to Library", I.lib, function () { setView("lib") }, "5"], ["Go to Network & storage", I.net, function () { setView("net") }, "6"], ["Go to Devices & viewers", I.tv, function () { setView("dev") }, "7"], ["Go to Sharing (files, proxy)", I.cast, function () { setView("share") }, "8"], ["Go to VPN router (per-device tunnel)", I.vpn, function () { setView("gw") }, "9"], ["Go to TV & remote", I.tv, function () { setView("tv") }, "0"],
    ["Settings", I.gear, openSet, ","], ["Switch theme", I.auto, cycleTheme, "T"], ["Keyboard shortcuts & about", I.help, openAbout, "?"]];
  var acts = base.filter(function (c) { return !q || c[0].toLowerCase().indexOf(q) >= 0 });
  var dls = (ST ? ST.items : []).filter(function (it) { return q && it.name.toLowerCase().indexOf(q) >= 0 }).slice(0, 8).map(function (it) {
    return [it.name, catIcon(it.cat).replace("class=fi", "class=fi style='width:20px;height:20px;border-radius:6px'"), function () { setView("all"); if (!OPEN[it.id]) toggleOpen(it.id); setTimeout(function () { var e = $("d-" + it.id); if (e) e.scrollIntoView({ behavior: "smooth", block: "center" }) }, 60) }, "", it.status];
  });
  CMDS = acts.concat(dls); CSEL = 0;
  var h = (acts.length ? "<h6>Actions</h6>" : "") + acts.map(function (c, i) { return "<a data-i=" + i + ">" + c[1] + esc(c[0]) + (c[3] ? "<kbd>" + c[3] + "</kbd>" : "") + "</a>" }).join("") +
    (dls.length ? "<h6>Downloads</h6>" + dls.map(function (c, i) { return "<a data-i=" + (acts.length + i) + ">" + c[1] + esc(c[0]) + "<small>" + c[4] + "</small></a>" }).join("") : "") +
    (!CMDS.length ? "<div class=note style='padding:10px'>No results</div>" : "");
  $("cmdl").innerHTML = h; hlCmd();
  $("cmdl").querySelectorAll("a").forEach(function (a) { a.onclick = function () { runCmd(+a.dataset.i) }; a.onmousemove = function () { CSEL = +a.dataset.i; hlCmd() } });
}
function hlCmd() { $("cmdl").querySelectorAll("a").forEach(function (a) { a.classList.toggle("sel", +a.dataset.i == CSEL) }); var s = $("cmdl").querySelector("a.sel"); if (s) s.scrollIntoView({ block: "nearest" }) }
function runCmd(i) { var c = CMDS[i]; if (!c) return; closeCmd(); c[2]() }
$("cmdq").addEventListener("input", buildCmd);
$("cmdq").addEventListener("keydown", function (e) {
  if (e.key == "ArrowDown") { e.preventDefault(); CSEL = Math.min(CMDS.length - 1, CSEL + 1); hlCmd() }
  else if (e.key == "ArrowUp") { e.preventDefault(); CSEL = Math.max(0, CSEL - 1); hlCmd() }
  else if (e.key == "Enter") { e.preventDefault(); runCmd(CSEL) }
  else if (e.key == "Escape") closeCmd();
});
// keyboard
document.addEventListener("keydown", function (e) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() == "k") { e.preventDefault(); $("cmdk").classList.contains("on") ? closeCmd() : openCmd(); return }
  if (e.key == "Escape") { if (PLAYING && $("ssearch").classList.contains("on")) { subSearchClose(); return } if (PLAYING && SUBM.open) { pSubMenu(); return } if (PLAYING) closePlay(); else { closeM(); closeCmd() } return }
  if (e.key == "Enter" && document.activeElement && document.activeElement.id == "ssq") { subSearch(); return }
  if (PLAYING && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    if (e.key == "c" || e.key == "C") { pSubMenu(); e.preventDefault(); return }
    if (SUBM.open && subMenuKey(e.keyCode)) { e.preventDefault(); return }
  }
  // shortcuts are Option (alt) combos so they never fire while typing and do not clash with macOS / browser keys
  if (!e.altKey || e.metaKey || e.ctrlKey || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || document.activeElement.isContentEditable || anyModal()) return;
  var c = e.code, views = ["all", "active", "queued", "done", "lib", "net", "dev", "share", "gw", "tv"];
  if (c == "KeyN") openAdd();
  else if (c == "KeyF") $("q").focus();
  else if (c == "Slash") openAbout();
  else if (c == "Comma") openSet();
  else if (c == "KeyT") cycleTheme();
  else if (/^Digit\d$/.test(c)) setView(views[(+c.slice(5) + 9) % 10]);
  else return;
  e.preventDefault();
});
document.addEventListener("paste", function (e) {
  if (anyModal() || /INPUT|TEXTAREA/.test(document.activeElement.tagName)) return;
  var t = (e.clipboardData || {}).getData ? e.clipboardData.getData("text") : "";
  if (/(https?|ftp):\/\//.test(t)) { e.preventDefault(); openAdd(t.trim()); countUrls() }
});
var dc = 0;
document.addEventListener("dragenter", function (e) { e.preventDefault(); dc++; $("drop").style.display = "grid" });
document.addEventListener("dragleave", function () { if (--dc <= 0) { dc = 0; $("drop").style.display = "none" } });
document.addEventListener("dragover", function (e) { e.preventDefault() });
document.addEventListener("drop", function (e) {
  e.preventDefault(); dc = 0; $("drop").style.display = "none";
  var t = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text");
  if (/(https?|ftp):\/\//.test(t)) { openAdd(t.trim()); countUrls() } else toast("No link found in the dropped item");
});
</script></body></html>
"""

SUBJS = '\nvar SUBS = { tracks: [], sid: "", font: "Vazirmatn", size: 5.2, color: "#ffffff", bg: "shadow", pos: 7, bold: true, delay: 0, cues: [], el: null, box: null, url: null, urlFor: null, note: null };\nvar SUB_FONTS = \'-apple-system,"SF Arabic","Geeza Pro","Noto Sans Arabic","LG Smart UI","Roboto",Tahoma,sans-serif\';\nvar SUB_OPT = { size: [3.2, 4.2, 5.2, 6.2, 7.5, 9], pos: [3, 7, 12, 18, 26],\n  font: (typeof DLUS_FONTS != "undefined" && DLUS_FONTS.length) ? DLUS_FONTS : [["Vazirmatn", "Vazirmatn"], ["system", "System font"]],\n  color: [["#ffffff", "White"], ["#ffe14d", "Yellow"], ["#7fe8ff", "Cyan"], ["#9dff8a", "Green"], ["#ffb3d9", "Pink"]],\n  bg: [["shadow", "Outline"], ["box", "Black box"], ["soft", "Soft box"], ["none", "None"]] };\nfunction subPad(x) { return (x + "00").slice(0, 3) }\nfunction subSecs(h, m, s, ms) { return (+h || 0) * 3600 + (+m) * 60 + (+s) + (+subPad(ms)) / 1000 }\nfunction subParse(t) {\n  var out = [], bl = t.replace(/\\r/g, "").split(/\\n{2,}/), i, j;\n  for (i = 0; i < bl.length; i++) {\n    var L = bl[i].split("\\n");\n    for (j = 0; j < L.length; j++) if (L[j].indexOf("-->") >= 0) break;\n    if (j >= L.length) continue;\n    var m = L[j].match(/(?:(\\d+):)?(\\d{1,2}):(\\d{2})[.,](\\d{1,3})\\s*-->\\s*(?:(\\d+):)?(\\d{1,2}):(\\d{2})[.,](\\d{1,3})/);\n    if (!m) continue;\n    var txt = L.slice(j + 1).join("\\n").replace(/\\{\\\\[^}]*\\}/g, "").replace(/^\\s+|\\s+$/g, "");\n    if (txt) out.push([subSecs(m[1], m[2], m[3], m[4]), subSecs(m[5], m[6], m[7], m[8]), txt]);\n  }\n  out.sort(function (a, b) { return a[0] - b[0] });\n  return out;\n}\nfunction subEsc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") }\nfunction subHtml(t) {\n  var L = t.split("\\n"), o = [];\n  for (var i = 0; i < L.length; i++) o.push("<span class=sl dir=auto>" + subEsc(L[i]).replace(/&lt;(\\/?)(i|b|u)&gt;/gi, "<$1$2>").replace(/&lt;[^&]*?&gt;/g, "") + "</span>");\n  return o.join("<br>");\n}\nvar subLast = null;\nfunction subNote(t) { if (SUBS.note) SUBS.note(t) }\nfunction subLoad(url) {\n  SUBS.cues = []; SUBS.url = url; subLast = null; if (SUBS.el) SUBS.el.innerHTML = "";\n  if (!url) return;\n  var x = new XMLHttpRequest(); x.open("GET", url, true);\n  x.onload = function () {\n    if (SUBS.url != url) return;\n    if (x.status == 202) { subNote("Preparing subtitles from the movie..."); setTimeout(function () { if (SUBS.url == url) subLoad(url) }, 4000); return }\n    SUBS.cues = x.status == 200 ? subParse(x.responseText) : []; subNote(x.status == 200 ? "" : "Subtitle not available");\n  };\n  x.send();\n}\nfunction subPick(sid) { SUBS.sid = sid || ""; subLoad(SUBS.sid && SUBS.urlFor ? SUBS.urlFor(SUBS.sid) : null) }\nfunction subRender(time) {\n  if (!SUBS.el) return;\n  var c = SUBS.cues, t = (time - SUBS.delay) / (SUBS.scale || 1), lo = 0, hi = c.length, on = [];\n  while (lo < hi) { var mid = (lo + hi) >> 1; if (c[mid][0] <= t) lo = mid + 1; else hi = mid }\n  for (var i = Math.max(0, lo - 10); i < lo; i++) if (c[i][1] > t) on.push(c[i][2]);\n  var h = on.length ? subHtml(on.join("\\n")) : "";\n  if (h !== subLast) { SUBS.el.innerHTML = h; subLast = h }\n}\nfunction subStyle() {\n  var e = SUBS.el; if (!e) return;\n  var H = (SUBS.box && SUBS.box.clientHeight) || window.innerHeight;\n  e.style.fontSize = (H * SUBS.size / 100).toFixed(1) + "px"; e.style.bottom = SUBS.pos + "%";\n  e.style.color = SUBS.color; e.style.fontWeight = SUBS.bold ? "700" : "400";\n  e.style.fontFamily = (SUBS.font && SUBS.font != "system" ? "\'" + SUBS.font + "\'," : "") + SUB_FONTS;\n  e.className = "subs sb-" + SUBS.bg; subLast = null;\n}\n// menu (remote keys, mouse, Magic Remote pointer)\nvar SUBM = { open: false, row: 0, el: null, onSave: null, extra: "" };\nfunction subRows() {\n  var tr = [["", "Off"]];\n  for (var i = 0; i < SUBS.tracks.length; i++) if (SUBS.tracks[i].ok) tr.push([SUBS.tracks[i].sid, SUBS.tracks[i].label]);\n  var sz = [], ps = [], pn = ["Lowest", "Low", "Middle", "Higher", "High"];\n  for (i = 0; i < SUB_OPT.size.length; i++) sz.push([SUB_OPT.size[i], Math.round(SUB_OPT.size[i] / 5.2 * 100) + "%"]);\n  for (i = 0; i < SUB_OPT.pos.length; i++) ps.push([SUB_OPT.pos[i], pn[i]]);\n  var rows = [["Subtitle", tr, "sid"], ["Font", SUB_OPT.font, "font"], ["Size", sz, "size"], ["Color", SUB_OPT.color, "color"], ["Background", SUB_OPT.bg, "bg"],\n          ["Position", ps, "pos"], ["Sync", null, "delay"], ["Bold", [[true, "On"], [false, "Off"]], "bold"]];\n  if (SUBS.sounds) rows.push(["Sound", SUBS.sounds, "snd"]);\n  if (SUBS.onAutoSync) rows.push(["Auto sync", null, "autosync"]);\n  if (SUBS.onFind) rows.push(["Find online", null, "find"]);\n  return rows;\n}\nfunction subMenuRender() {\n  if (!SUBM.el) return;\n  var rows = subRows(), h = "<div class=smh>Subtitles</div>";\n  for (var i = 0; i < rows.length; i++) {\n    var r = rows[i], v = SUBS[r[2]], lab = "?";\n    if (r[2] == "delay") lab = (v > 0 ? "+" : "") + v.toFixed(1) + " s" + (v ? "" : " (in sync)");\n    else if (r[2] == "find") lab = "OK to search subtitles";\n    else if (r[2] == "autosync") lab = SUBS.syncNote || (SUBS.scale && SUBS.scale != 1 ? "speed fixed \\u00d7" + SUBS.scale.toFixed(4) : "OK to match the subtitle to the speech");\n    else for (var k = 0; k < r[1].length; k++) if (r[1][k][0] === v) lab = r[1][k][1];\n    var sw = r[2] == "color" ? "<i class=sw style=\'background:" + v + "\'></i>" : "";\n    h += "<div class=\'smr" + (i == SUBM.row ? " on" : "") + "\' data-r=" + i + "><span>" + r[0] + "</span><b data-d=-1>&#9664;</b><em dir=auto>" + sw + subEsc(lab) + "</em><b data-d=1>&#9654;</b></div>";\n  }\n  SUBM.el.innerHTML = h + "<div class=smf>" + SUBM.extra + "</div>";\n}\nfunction subChange(i, d) {\n  var r = subRows()[i], k = r[2];\n  if (k == "find") { if (SUBS.onFind) SUBS.onFind(); return }\n  if (k == "autosync") { if (SUBS.onAutoSync) SUBS.onAutoSync(); return }\n  if (k == "delay") SUBS.delay = Math.round((SUBS.delay + d * 0.5) * 10) / 10;\n  else { var o = r[1], j = 0; for (var x = 0; x < o.length; x++) if (o[x][0] === SUBS[k]) j = x; SUBS[k] = o[(j + d + o.length) % o.length][0] }\n  if (k == "sid") subPick(SUBS.sid);\n  if (k == "snd" && SUBS.onSound) SUBS.onSound();\n  SUBM.row = i; subStyle(); subMenuRender(); if (SUBM.onSave) SUBM.onSave(k);\n}\nfunction subMenuKey(k) {\n  var n = subRows().length;\n  if (k == 38) SUBM.row = (SUBM.row + n - 1) % n;\n  else if (k == 40) SUBM.row = (SUBM.row + 1) % n;\n  else if (k == 37) { subChange(SUBM.row, -1); return true }\n  else if (k == 39 || k == 13) { subChange(SUBM.row, 1); return true }\n  else return false;\n  subMenuRender(); return true;\n}\nfunction subMenuClick(e) {\n  var t = e.target, d = null, r = null;\n  while (t && t != SUBM.el) { if (d === null && t.getAttribute && t.getAttribute("data-d")) d = +t.getAttribute("data-d"); if (t.getAttribute && t.getAttribute("data-r") !== null) { r = +t.getAttribute("data-r"); break } t = t.parentNode }\n  if (r !== null) subChange(r, d === null ? 1 : d);\n  if (e.stopPropagation) e.stopPropagation();\n}\nfunction subSetup(st) {\n  if (!st) return;\n  SUBS.tracks = st.tracks || []; var y = st.style || {};\n  if (y.font) SUBS.font = y.font; if (y.size) SUBS.size = y.size; if (y.color) SUBS.color = y.color; if (y.bg) SUBS.bg = y.bg; if (y.pos) SUBS.pos = y.pos; if (y.bold !== undefined) SUBS.bold = y.bold;\n  SUBS.delay = st.delay || 0; SUBS.scale = st.scale || 1; subStyle(); subPick(st.sid);\n}\n\nfunction subAutoSync(url) {\n  if (!SUBS.sid) { SUBS.syncNote = "choose a subtitle first"; subMenuRender(); return }\n  SUBS.syncNote = "listening to the film..."; subMenuRender();\n  var x = new XMLHttpRequest(); x.open("GET", url, true);\n  x.onload = function () {\n    var r = {}; try { r = JSON.parse(x.responseText) } catch (e) {}\n    if (r.pending) { SUBS.syncNote = "analysing the film\'s speech (about " + (r.eta || 60) + " s, once per film)..."; subMenuRender(); setTimeout(function () { subAutoSync(url) }, 5000); return }\n    if (r.error) { SUBS.syncNote = r.error; subMenuRender(); return }\n    if (r.verdict == "sections") { SUBS.delay = 0; SUBS.scale = 1; SUBS.syncNote = "fitted to this cut \\u00b7 " + (r.note || ""); subPick(SUBS.sid, true) }\n    else {\n      SUBS.delay = r.delay; SUBS.scale = r.scale || 1;\n      SUBS.syncNote = (r.applied ? "synced " : "suggested ") + (r.offset > 0 ? "+" : "") + r.offset.toFixed(2) + " s" + (r.scale != 1 ? ", speed \\u00d7" + r.scale.toFixed(4) : "") + " \\u00b7 " + r.confidence_pct + "% sure";\n    }\n    if (SUBS.note) SUBS.note("Subtitle " + SUBS.syncNote);\n    subMenuRender(); if (SUBM.onSave) SUBM.onSave("delay");\n  };\n  x.onerror = function () { SUBS.syncNote = "could not reach the Mac"; subMenuRender() };\n  x.send();\n}\n'
SUBCSS = '\n.subs{position:absolute;left:4%;right:4%;text-align:center;pointer-events:none;line-height:1.38;z-index:4;font-family:-apple-system,"SF Arabic","Vazirmatn","Geeza Pro","Noto Sans Arabic","LG Smart UI","Roboto",Tahoma,sans-serif}\n.subs .sl{display:inline-block;padding:.02em .35em;border-radius:.16em;unicode-bidi:plaintext}\n.sb-shadow .sl{text-shadow:0 0 .1em #000,0 0 .14em #000,.06em .06em .08em #000,-.06em -.06em .08em #000}\n.sb-box .sl{background:rgba(0,0,0,.82)}.sb-soft .sl{background:rgba(0,0,0,.42);text-shadow:0 0 .12em #000}\n.smenu{position:absolute;right:2.5%;top:6%;z-index:6;min-width:430px;max-width:62%;padding:14px;border-radius:18px;background:rgba(18,18,24,.92);border:1px solid rgba(255,255,255,.12);box-shadow:0 20px 60px rgba(0,0,0,.6);color:#fff;font-family:-apple-system,"LG Smart UI",Roboto,sans-serif;display:none}\n.smenu.on{display:block}.smh{font-size:1.05em;font-weight:700;padding:4px 8px 10px;color:#c9c4ff}\n.smr{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:11px;cursor:pointer}\n.smr span{flex:1;color:#b7b7c4}.smr em{font-style:normal;font-weight:600;min-width:150px;max-width:300px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.smr b{opacity:.45;padding:0 6px;font-size:.9em}.smr.on{background:#6d5efc}.smr.on span{color:#fff}.smr.on b{opacity:1}\n.smr .sw{display:inline-block;width:.8em;height:.8em;border-radius:50%;margin-right:8px;vertical-align:-.05em;border:1px solid rgba(255,255,255,.4)}\n.smf{padding:10px 8px 2px;color:#9a9aa6;font-size:.82em}\n'

if __name__ == "__main__":
    main()
