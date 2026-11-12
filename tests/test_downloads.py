# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""The download engine against a local test server: chunks, checks, resume, checksums, errors."""
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _testserver import FileServer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DLM_CHUNK_MB"] = "1"  # 1 MB chunks: several chunks with small test files
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_dl", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)


def manager():
    home = tempfile.mkdtemp(prefix="dlus-m-")
    dlus.STATE_DIR = home
    m = dlus.Manager(0)
    m.settings["base_dir"] = os.path.join(home, "dl")
    m.notify = lambda msg: None
    return m


def run_until(m, did, statuses=("done", "error"), timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        m.tick()
        d = m.dls.get(did)
        if d and d.r["status"] in statuses:
            return d
        time.sleep(0.1)
    raise AssertionError("download %s still %s" % (did, m.dls[did].r["status"]))


class Downloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.mkdtemp()
        cls.fs = FileServer(cls.files)

    @classmethod
    def tearDownClass(cls):
        cls.fs.stop()

    def setUp(self):
        self.fs.ranges, self.fs.delay, self.fs.fail_ranges, self.fs.names = True, 0.0, 0, {}
        self.m = manager()

    def data(self, n):
        return os.urandom(n)

    def test_chunked_download_is_exact_and_checked(self):
        body = self.data(3 * 1024 * 1024 + 12345)  # 4 chunks of 1 MB
        self.fs.put("films/movie.mkv", body)
        want = hashlib.sha256(body).hexdigest()
        did = self.m.add(self.fs.url("films/movie.mkv"), conns=3, checksum="sha256:" + want)
        d = run_until(self.m, did)
        self.assertEqual(d.r["status"], "done", d.r.get("msg"))
        self.assertEqual(open(d.out, "rb").read(), body)
        self.assertTrue(d.r["checksum_ok"])
        self.assertEqual(d.r["sha256"], want)
        self.assertEqual(os.path.basename(os.path.dirname(d.out)), "Video")  # sorted by type
        q = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.quarantine", d.out], capture_output=True, text=True)
        self.assertTrue(q.stdout.startswith("0083;"), q.stdout + q.stderr)
        self.assertFalse(os.path.exists(d.parts))
        st = self.m.state()
        self.assertTrue(any(i["id"] == did and i["status"] == "done" for i in st["items"]))
        self.m.detail(did)

    def test_server_without_ranges_downloads_in_one_piece(self):
        self.fs.ranges = False
        body = self.data(700000)
        self.fs.put("noranges.zip", body)
        d = run_until(self.m, self.m.add(self.fs.url("noranges.zip")))
        self.assertEqual(d.r["status"], "done", d.r.get("msg"))
        self.assertEqual(open(d.out, "rb").read(), body)

    def test_failed_ranges_are_retried(self):
        body = self.data(2 * 1024 * 1024 + 7)
        self.fs.put("retry.bin", body)
        self.fs.fail_ranges = 2
        d = run_until(self.m, self.m.add(self.fs.url("retry.bin"), conns=2), timeout=90)
        self.assertEqual(d.r["status"], "done", d.r.get("msg"))
        self.assertEqual(open(d.out, "rb").read(), body)

    def test_missing_file_waits_for_a_new_link(self):  # 404/403: the link may have expired - keep the download, wait
        did = self.m.add(self.fs.url("nothing-here.mkv"))
        d = run_until(self.m, did, statuses=("waiting", "error"), timeout=60)
        self.assertEqual(d.r["status"], "waiting")
        self.assertIn("link expired", d.r["msg"])
        self.m.action(did, "pause", {})

    def test_wrong_checksum_sets_the_file_aside(self):
        self.fs.put("bad.iso", self.data(300000))
        d = run_until(self.m, self.m.add(self.fs.url("bad.iso"), checksum="0" * 64))
        self.assertEqual(d.r["status"], "error")
        self.assertFalse(d.r["checksum_ok"])
        self.assertFalse(os.path.exists(d.out))
        self.assertTrue(os.path.exists(d.out + ".checksum-failed"))

    def test_publisher_checksum_file_is_found(self):
        body = self.data(400000)
        self.fs.put("dist/tool.iso", body)
        self.fs.put("dist/SHA256SUMS", ("%s  other.iso\n%s *tool.iso\n" % ("ab" * 32, hashlib.sha256(body).hexdigest())).encode())
        d = run_until(self.m, self.m.add(self.fs.url("dist/tool.iso")))
        self.assertEqual(d.r["status"], "done", d.r.get("msg"))
        self.assertTrue(d.r["checksum_ok"])
        self.assertTrue(d.r["checksum_src"].endswith("SHA256SUMS"))

    def test_checksum_file_link(self):
        body = self.data(200000)
        self.fs.put("pkg/app.dmg", body)
        self.fs.put("pkg/app.dmg.sha512", (hashlib.sha512(body).hexdigest() + "  app.dmg\n").encode())
        d = run_until(self.m, self.m.add(self.fs.url("pkg/app.dmg"), checksum=self.fs.url("pkg/app.dmg.sha512")))
        self.assertTrue(d.r["checksum_ok"], d.r.get("msg"))

    def test_server_file_name_is_used_and_cleaned(self):
        self.fs.put("dl.php", self.data(1000))
        self.fs.names["/dl.php"] = "../../Report\u202egpj.pdf"
        d = run_until(self.m, self.m.add(self.fs.url("dl.php?id=7")))
        self.assertEqual(d.r["status"], "done", d.r.get("msg"))
        self.assertEqual(d.r["name"], "_.._Reportgpj.pdf")
        self.assertTrue(d.out.startswith(self.m.settings["base_dir"]))

    def test_same_name_gets_a_new_name(self):
        self.fs.put("a/same.txt", b"one")
        self.fs.put("b/same.txt", b"two")
        d1 = run_until(self.m, self.m.add(self.fs.url("a/same.txt")))
        d2 = run_until(self.m, self.m.add(self.fs.url("b/same.txt")))
        self.assertNotEqual(d1.out, d2.out)
        self.assertEqual(open(d2.out, "rb").read(), b"two")

    def test_pause_resume_and_delete(self):
        body = self.data(3 * 1024 * 1024)
        self.fs.put("slow.mkv", body)
        self.fs.delay = 0.1
        did = self.m.add(self.fs.url("slow.mkv"), conns=2)
        run_until(self.m, did, statuses=("downloading",))
        time.sleep(0.5)
        self.m.action(did, "pause", {})
        self.assertEqual(self.m.dls[did].r["status"], "paused")
        self.fs.delay = 0.0
        self.m.action(did, "resume", {})
        d = run_until(self.m, did)
        self.assertEqual(open(d.out, "rb").read(), body)  # resumed, not corrupted
        self.m.action(did, "delete", {"files": True})
        self.assertNotIn(did, self.m.dls)
        self.assertFalse(os.path.exists(d.out))

    def test_verify_after_download(self):
        body = self.data(150000)
        self.fs.put("v.iso", body)
        d = run_until(self.m, self.m.add(self.fs.url("v.iso")))
        self.m.action(d.r["id"], "verify", {"checksum": hashlib.md5(body, usedforsecurity=False).hexdigest()})
        for _ in range(50):
            if (d.r.get("verify") or {}).get("state") in ("ok", "bad"):
                break
            time.sleep(0.1)
        self.assertEqual(d.r["verify"]["state"], "ok")

    def test_bad_links_and_checksums_are_refused(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp//x"):
            with self.assertRaises(ValueError):
                self.m.add(bad)
        with self.assertRaises(ValueError):
            self.m.add(self.fs.url("x.iso"), checksum="not a checksum")
        self.fs.put("dup.txt", b"x")
        self.m.add(self.fs.url("dup.txt"))
        with self.assertRaises(ValueError):
            self.m.add(self.fs.url("dup.txt"))

    def test_queue_order_and_limits(self):
        self.m.settings["max_active"] = 1
        ids = []
        for i in range(3):
            self.fs.put("q%d.bin" % i, self.data(50000))
            ids.append(self.m.add(self.fs.url("q%d.bin" % i), start=False))
        self.m.move(ids[2], "top")
        for i in ids:
            self.m.action(i, "resume", {})
        for i in ids:
            self.assertEqual(run_until(self.m, i).r["status"], "done")
        self.m.action("all", "pause", {})


if __name__ == "__main__":
    unittest.main()
