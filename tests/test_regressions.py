# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""Regression tests: one test per bug fixed since the first public release (see CHANGELOG.md).
Each test fails on the old code and passes on the fix."""
import http.client
import importlib.util
import io
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_reg", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)
SRC = open(os.path.join(ROOT, "dlus.py"), encoding="utf-8").read()


def manager():
    """a fresh dlus core with its own state folder (no servers or background threads started)"""
    home = tempfile.mkdtemp(prefix="dlus-m-")
    dlus.STATE_DIR = home
    m = dlus.Manager(0)
    m.settings["base_dir"] = os.path.join(home, "dl")
    return m


def local_file(m, name, data):
    p = os.path.join(m.settings["base_dir"], name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return m.import_local(p)


class Subtitles(unittest.TestCase):
    def vtt(self, text):
        p = os.path.join(tempfile.mkdtemp(), "s.vtt")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_cut_carries_rounded_milliseconds_into_seconds(self):  # 7.27: 0.9996 s was written as 00:00:00,000
        out = open(dlus.cut_srt(self.vtt("WEBVTT\n\n00:00:10.000 --> 00:00:12.500\nhello\n"), 9.0004, 0)).read()
        self.assertIn("00:00:01,000 --> 00:00:03,500", out)

    def test_cut_starts_at_the_asked_time_with_sync(self):  # 7.18: the cut must use the asked start, plus the film's sync
        p = self.vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nbefore\n\n00:01:00.000 --> 00:01:02.000\nin\n")
        out = open(dlus.cut_srt(p, 59.0, 0.5)).read()
        self.assertNotIn("before", out)
        self.assertIn("00:00:01,500 --> 00:00:03,500", out)

    def test_archive_bomb_is_refused(self):  # 7.23: a subtitle .zip could unpack to gigabytes
        m = manager()
        d = local_file(m, "Video/film.mkv", b"\x1aE\xdf\xa3" + b"\0" * 5000)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("big.srt", b"0" * (40 << 20))
        with self.assertRaises(ValueError):
            m.subs_add(d, "subs.zip", buf.getvalue())


class Media(unittest.TestCase):
    def test_playlist_disguised_as_video_is_refused(self):  # 7.23: HLS playlist named .mkv could make ffmpeg read other files
        m = manager()
        evil = local_file(m, "Video/evil.mkv", b"#EXTM3U\n#EXTINF:10,\nfile:///etc/passwd\n" + b"\n" * 5000)
        real = local_file(m, "Video/real.mkv", b"\x1aE\xdf\xa3" + os.urandom(5000))
        self.assertFalse(evil.media_ok())
        self.assertTrue(real.media_ok())


class Network(unittest.TestCase):
    def test_unknown_device_waits_for_approval(self):  # 7.23: the code alone gave access
        m = manager()
        ip = "192.168.77.5"
        self.assertFalse(m.lan_access(ip, "LG Browser", "opened the TV home page"))
        self.assertIn(ip, m.lan_pending)
        m.lan_decide({"ip": ip, "act": "allow"})
        self.assertTrue(m.lan_access(ip, "LG Browser", "opened the TV home page"))
        m.lan_decide({"ip": ip, "act": "remove"})
        self.assertFalse(m.lan_access(ip, "LG Browser", "again"))

    def test_denied_device_stays_blocked(self):  # 7.23
        m = manager()
        m.lan_access("192.168.77.6", "", "x")
        m.lan_decide({"ip": "192.168.77.6", "act": "deny"})
        self.assertFalse(m.lan_access("192.168.77.6", "", "x"))
        self.assertNotIn("192.168.77.6", m.lan_pending)

    def test_proxy_never_reaches_this_mac(self):  # 7.23: proxy could be pointed at loopback addresses
        # security scan: addresses the proxy must refuse, not a bind
        for a in ("127.0.0.1", "127.8.8.8", "0.0.0.0", "::1", "224.0.0.251"):  # nosec B104
            self.assertTrue(dlus.ProxyServer.local_dest(a), a)
        self.assertFalse(dlus.ProxyServer.local_dest("93.184.216.34"))

    def test_tv_needs_tls_12(self):  # 7.25
        self.assertIn("ctx.minimum_version = ssl.TLSVersion.TLSv1_2", SRC)


class LocalServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = manager()
        cls.srv = dlus.http.server.ThreadingHTTPServer(("127.0.0.1", 0), dlus.make_handler(cls.m, 0))
        cls.port = cls.srv.server_address[1]
        cls.srv.RequestHandlerClass = dlus.make_handler(cls.m, cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers=dict({"Host": "127.0.0.1:%d" % self.port}, **(headers or {})))
        r = c.getresponse()
        r.read()
        return r

    def test_unlock_redirect_stays_on_the_page(self):  # 7.24: open redirect after the key
        for path in ("/?k=%s", "/x/?k=%s&a=1"):
            r = self.get(path % self.m.ui_key)
            loc = r.getheader("Location")
            self.assertEqual(r.status, 302)
            self.assertTrue(loc.startswith("/") and not loc.startswith("//"), loc)
            self.assertNotIn("k=", loc)

    def test_no_key_no_api(self):
        self.assertEqual(self.get("/api/state").status, 401)

    def test_foreign_host_refused(self):  # DNS rebinding
        self.assertEqual(self.get("/api/state", {"Host": "evil.example:%d" % self.port}).status, 403)


class Logs(unittest.TestCase):
    def test_one_line_per_entry(self):  # 7.24: values from the network could forge log lines
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "ua=%s", ("a\r\nFAKE ERROR",), None)
        out = dlus.OneLineFormatter("%(message)s").format(rec)
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertEqual(dlus.logsafe("a\nb\rc"), "a\\nb\\rc")


class Secrets(unittest.TestCase):
    def test_credentials_live_in_their_own_private_file(self):  # 7.26
        m = manager()
        m.settings["tv_keys"] = {"tv": "k"}
        m.save()
        home = dlus.STATE_DIR
        pub = json.load(open(os.path.join(home, "settings.json")))
        sec = json.load(open(os.path.join(home, "secrets.json")))
        for k in ("lan_token", "proxy_pass", "tv_keys"):
            self.assertNotIn(k, pub)
            self.assertIn(k, sec)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(home, "secrets.json")).st_mode), 0o600)


class Build(unittest.TestCase):
    def test_no_undefined_names(self):  # 7.19: a deleted helper (NameError) broke the TV player
        r = subprocess.run([sys.executable, os.path.join(ROOT, "macos", "check_names.py"), os.path.join(ROOT, "dlus.py")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
