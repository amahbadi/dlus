# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""What happens when things go wrong: damaged pieces on disk, a full disk, unexpected errors, refused TV keys,
an unknown device at the door, fonts, and the API's error answers."""
import errno
import http.client
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _testserver import FileServer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DLM_CHUNK_MB"] = "1"
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_fail", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)
FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def manager():
    dlus.STATE_DIR = tempfile.mkdtemp(prefix="dlus-m-")
    m = dlus.Manager(0)
    m.notify = lambda msg: None
    m.settings["base_dir"] = os.path.join(dlus.STATE_DIR, "dl")
    return m


def run_until(m, did, statuses=("done", "error"), timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        m.tick()
        if m.dls[did].r["status"] in statuses:
            return m.dls[did]
        time.sleep(0.1)
    raise AssertionError("still %s" % m.dls[did].r["status"])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class DiskAndDamage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fs = FileServer(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.fs.stop()

    def paused_half(self, name):
        body = os.urandom(3 * 1024 * 1024 + 77)
        self.fs.put(name, body)
        self.fs.delay = 0.05
        m = manager()
        did = m.add(self.fs.url(name), conns=1)
        run_until(m, did, statuses=("downloading",))
        end = time.time() + 30
        while time.time() < end and dlus.fsize(m.dls[did].cpath(0)) < 300000:
            m.tick()
            time.sleep(0.1)
        m.action(did, "pause", {})
        self.fs.delay = 0.0
        return m, did, body

    def test_piece_damaged_on_disk_while_paused(self):  # 7.29: a checked piece damaged before joining went into the file
        m, did, body = self.paused_half("damaged-on-disk.mkv")
        p = m.dls[did].cpath(0)
        with open(p, "r+b") as f:  # e.g. a disk error or another program wrote into it
            f.seek(max(0, dlus.fsize(p) - 1000))
            f.write(os.urandom(500))
        m.action(did, "resume", {})
        d = run_until(m, did)
        self.assertEqual(open(d.out, "rb").read(), body)
        self.assertTrue(any("CHANGED ON DISK" in e or "RESUME CHECK FAILED" in e for e in d.events), list(d.events)[-8:])

    def test_piece_too_big_on_disk(self):
        m, did, body = self.paused_half("too-big.mkv")
        with open(m.dls[did].cpath(0), "ab") as f:
            f.write(b"\0" * (dlus.C + 10))
        m.action(did, "resume", {})
        d = run_until(m, did)
        self.assertEqual(open(d.out, "rb").read(), body)

    def test_not_enough_disk_space(self):
        self.fs.put("big.iso", os.urandom(2 * 1024 * 1024))
        m = manager()
        Usage = type("U", (), {"free": 1000, "total": 10 ** 9, "used": 10 ** 9})
        with mock.patch.object(dlus.shutil, "disk_usage", lambda p: Usage):
            d = run_until(m, m.add(self.fs.url("big.iso")))
        self.assertEqual(d.r["status"], "error")
        self.assertIn("disk space", d.r["msg"])

    def test_disk_full_and_unexpected_errors_while_downloading(self):
        self.fs.put("full.mkv", os.urandom(2 * 1024 * 1024))
        for err, want in ((OSError(errno.ENOSPC, "No space left on device"), "disk is full"), (RuntimeError("surprise"), None)):
            m = manager()
            real = dlus.DL.chunk
            calls = []

            def chunk(self, *a, **k):
                if not calls:
                    calls.append(1)
                    raise err
                return real(self, *a, **k)
            with mock.patch.object(dlus.DL, "chunk", chunk), mock.patch.object(dlus.DL, "sleep", lambda self, s: self.stop_ev.is_set()):
                d = run_until(m, m.add(self.fs.url("full.mkv")))
            if want:
                self.assertIn(want, d.r["msg"])
            else:
                self.assertEqual(d.r["status"], "done")  # an unexpected error is retried, not fatal


class UnknownDeviceAtTheDoor(unittest.TestCase):
    def test_waiting_page_from_the_home_network(self):
        m = manager()
        m.net.slow()  # learn this Mac's home-network address
        ip = m.lan_ip()
        if ip == "127.0.0.1":
            self.skipTest("no home-network address on this machine")
        m.settings["lan_port"] = free_port()
        lan = dlus.LanServer(m, host=ip, port=m.settings["lan_port"])
        try:
            tok = m.settings["lan_token"]
            for accept, want in (("text/html", b"Waiting for approval"), ("application/json", b"waiting for approval")):
                c = http.client.HTTPConnection(ip, m.settings["lan_port"], timeout=10)
                c.request("GET", "/%s/tv" % tok, headers={"Accept": accept})
                r = c.getresponse()
                self.assertEqual(r.status, 403)
                self.assertIn(want, r.read())
            self.assertIn(ip, m.lan_pending)
        finally:
            lan.stop()


class Fonts(unittest.TestCase):
    def test_user_fonts(self):
        m = manager()
        ttf = b"\x00\x01\x00\x00" + os.urandom(2000)
        import base64
        up = {"name": "Mine", "filename": "mine.ttf", "b64": base64.b64encode(ttf).decode()}
        m.fonts_set({"upload": up})
        with self.assertRaises(ValueError):
            m.fonts_set({"upload": up})  # the same name twice
        m.settings["sub_style"]["font"] = "Mine"
        m.fonts_set({"remove": "Mine"})
        self.assertEqual(m.settings["sub_style"]["font"], "Vazirmatn")  # back to the default
        with self.assertRaises(KeyError):
            m.fonts_set({"nonsense": 1})


class ApiErrors(unittest.TestCase):
    def test_error_answers(self):
        m = manager()
        port = free_port()
        m.port = port
        srv = dlus.http.server.ThreadingHTTPServer(("127.0.0.1", port), dlus.make_handler(m, port))
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def post(path, body):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            c.request("POST", path, json.dumps(body).encode(), {"Host": "127.0.0.1:%d" % port, "Cookie": "dlus_k=" + m.ui_key,
                                                                "Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, r.read()
        try:
            st, data = post("/api/add", {"urls": "http://127.0.0.1:9/a.bin", "checksum": "not-a-checksum"})
            self.assertEqual(st, 400)
            with mock.patch.object(m, "find_tvs", lambda: []):
                self.assertEqual(post("/api/tvs/find", {})[0], 200)
            with mock.patch.object(m, "gw_admin", lambda b: {"ok": False, "cancelled": True}):
                self.assertEqual(post("/api/gw/admin", {"act": "install"})[0], 200)
            p = os.path.join(m.settings["base_dir"], "Video", "c.mkv")
            os.makedirs(os.path.dirname(p))
            with open(p, "wb") as f:
                f.write(b"\x1aE\xdf\xa3" + b"0" * 100)
            d = m.import_local(p)
            with mock.patch.object(m, "cast", lambda d, tv, sound, at: {"ok": True}):
                self.assertEqual(post("/api/tvs/cast", {"id": d.r["id"], "tv": "x"})[0], 200)
            self.assertEqual(post("/api/subs/nope/add", {})[0], 404)
            self.assertEqual(post("/api/d/%s/start" % d.r["id"], {})[0], 200)  # start now: ignores queue and schedule
            self.assertTrue(m.dls[d.r["id"]].r.get("force") or m.dls[d.r["id"]].r["status"] in ("done", "queued", "downloading"))
        finally:
            srv.shutdown()


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class ConvertedFormats(unittest.TestCase):
    def test_vp9_film_is_converted_for_the_tv(self):
        m = manager()
        vid = os.path.join(m.settings["base_dir"], "Video", "clip.webm")
        os.makedirs(os.path.dirname(vid))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=duration=4:size=320x180:rate=24",
                        "-f", "lavfi", "-i", "sine=duration=4", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-c:a", "libopus", vid],
                       check=True, timeout=120)
        d = m.import_local(vid)
        m.port = free_port()
        srv = dlus.http.server.ThreadingHTTPServer(("127.0.0.1", m.port), dlus.make_handler(m, m.port))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            srt = os.path.join(tempfile.mkdtemp(), "s.srt")
            with open(srt, "w") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
            sid = m.subs_add(d, "s.srt", open(srt, "rb").read())[0]["sid"]
            m.subs_action(d, "set", {"sid": sid, "delay": 0.3})
            cmd, info, _ = dlus.compat_cmd(m, d, "tv", 1.0)
            self.assertTrue(info.get("_converted"))  # VP9 cannot play in the TV browser: converted
            cmd, info, sub_in = dlus.compat_cmd(m, d, "mkvsub", 1.0)
            self.assertTrue(sub_in)  # the chosen subtitle goes into the TV player's stream, cut at the start time
            c = http.client.HTTPConnection("127.0.0.1", m.port, timeout=60)
            c.request("GET", "/s/%dt.mkv?ik=%s" % (d.r["num"], m.media_key), headers={"Host": "127.0.0.1:%d" % m.port})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertTrue(r.read(2048))
            c.close()
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
