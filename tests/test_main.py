# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""Starting dlus (main), the "dlus add" command line and the single-instance lock - on free ports, own data, no internet."""
import contextlib
import importlib.util
import io
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _testserver import FileServer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_main", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Main(unittest.TestCase):
    def test_start_add_and_single_instance(self):
        dlus.STATE_DIR = tempfile.mkdtemp(prefix="dlus-main-")
        port = free_port()
        fs = FileServer(tempfile.mkdtemp())
        fs.put("cli.txt", b"added from the command line")
        patches = [mock.patch.object(dlus.signal, "signal", lambda *a: None),  # signal hooks only work in the main thread
                   mock.patch.object(dlus, "fetch_fonts", lambda: None),  # no internet in tests
                   mock.patch.object(dlus.Net, "pubip", lambda self: None),
                   mock.patch.object(dlus, "LOCAL_PLAYER_PORT", free_port())]
        for p in patches:
            p.start()
        try:
            with mock.patch.object(sys, "argv", ["dlus", "--port", str(port), "--no-open"]):
                threading.Thread(target=dlus.main, daemon=True, name="test-main").start()
                end = time.time() + 30
                while time.time() < end:
                    try:
                        # security scan: fixed http://127.0.0.1 address of the test instance
                        if urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2).status == 200:  # nosec B310
                            break
                    except OSError:
                        time.sleep(0.3)
                else:
                    self.fail("dlus did not start")
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["dlus", "add", fs.url("cli.txt"), "-f", "cli", "--port", str(port)]), contextlib.redirect_stdout(out):
                dlus.main()
            self.assertIn("ids", out.getvalue())
            out = io.StringIO()  # a second start finds the first one (lock) and does not start again
            with mock.patch.object(sys, "argv", ["dlus", "--port", str(free_port()), "--no-open"]), contextlib.redirect_stdout(out):
                dlus.main()
            self.assertIn("already running", out.getvalue())
            self.assertEqual(os.stat(dlus.STATE_DIR).st_mode & 0o777, 0o700)
        finally:
            for p in patches:
                p.stop()
            fs.stop()


if __name__ == "__main__":
    unittest.main()
