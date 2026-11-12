# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""The proxy end to end, the VPN router logic with a fake root helper, and the network monitor."""
import base64
import importlib.util
import os
import socket
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _testserver import FileServer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
GW_DIR = tempfile.mkdtemp(prefix="dlus-gw-")
FAKE_GW = os.path.join(GW_DIR, "dlus-gw")
with open(FAKE_GW, "w") as f:  # answers like the root helper (status with per-connection byte counters), changes nothing
    f.write('#!/bin/sh\necho "$@" >> "%s/calls"\ncase "$1" in\n'
            'version) echo "dlus-gw 3";;\n'
            'status) echo "dlus-gw 3 forwarding=1 redirect=0 full=-"; echo "Status: Enabled"; echo "rules=loaded";\n'
            '  echo "== tunnel 192.168.1.50"; echo "== direct 192.168.1.60"; echo "== states";\n'
            '  echo "all tcp 192.168.1.50:51000 -> 142.250.1.1:443 ESTABLISHED:ESTABLISHED | [1000:2000 pkts, 120000:3400000 bytes]";\n'
            '  echo "all udp 192.168.1.60:5353 -> 8.8.8.8:53 | [3:3 pkts, 300:600 bytes]";;\n'
            'full) echo "ok all traffic via $3";;\n'
            'enable) echo "ok lan=$2 tun=$5 peer=10.0.0.1 killswitch=$6";;\n'
            '*) echo ok;;\nesac\n' % GW_DIR)
os.chmod(FAKE_GW, stat.S_IRWXU)
os.environ["DLUS_GW_HELPER"] = FAKE_GW
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_net", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def manager():
    dlus.STATE_DIR = tempfile.mkdtemp(prefix="dlus-m-")
    m = dlus.Manager(0)
    m.notify = lambda msg: None
    m.settings["base_dir"] = os.path.join(dlus.STATE_DIR, "dl")
    return m


class Proxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.mkdtemp()
        cls.fs = FileServer(cls.files)
        cls.fs.put("page.html", b"<h1>hello through the proxy</h1>")
        m = cls.m = manager()
        m.settings["proxy_upstream"] = "direct"
        m.set_settings({"proxy_on": True, "proxy_port": free_port(), "proxy_auth": True})
        cls.port = m.settings["proxy_port"]
        # the test server is on 127.0.0.1, which the proxy (rightly) refuses: allow it for these tests only
        m.proxy.own_ips = lambda: {"0.0.0.0"}
        m.proxy.local_dest = lambda a: False
        cls.auth = "Basic " + base64.b64encode(("%s:%s" % (m.settings["proxy_user"], m.settings["proxy_pass"])).encode()).decode()

    @classmethod
    def tearDownClass(cls):
        cls.m.set_settings({"proxy_on": False})
        cls.fs.stop()

    def raw(self, data):
        s = socket.create_connection(("127.0.0.1", self.port), 10)
        s.sendall(data)
        out = b""
        s.settimeout(10)
        try:
            while True:
                c = s.recv(65536)
                if not c:
                    break
                out += c
                if b"hello through the proxy" in out or out.startswith((b"HTTP/1.1 4", b"HTTP/1.1 5")):
                    break
        except socket.timeout:
            pass
        s.close()
        return out

    def test_password_is_required(self):
        out = self.raw(b"GET %s HTTP/1.1\r\nHost: x\r\n\r\n" % self.fs.url("page.html").encode())
        self.assertTrue(out.startswith(b"HTTP/1.1 407"), out[:80])

    def test_plain_http_through_the_proxy(self):
        out = self.raw(b"GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nProxy-Authorization: %s\r\nConnection: close\r\n\r\n"
                       % (self.fs.url("page.html").encode(), self.auth.encode()))
        self.assertIn(b"hello through the proxy", out)

    def test_connect_tunnel(self):
        s = socket.create_connection(("127.0.0.1", self.port), 10)
        s.sendall(b"CONNECT 127.0.0.1:%d HTTP/1.1\r\nProxy-Authorization: %s\r\n\r\n" % (self.fs.port, self.auth.encode()))
        head = s.recv(4096)
        self.assertIn(b"200", head.split(b"\r\n")[0])
        s.sendall(b"GET /page.html HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        out = b""
        s.settimeout(10)
        while b"hello" not in out:
            c = s.recv(65536)
            if not c:
                break
            out += c
        s.close()
        self.assertIn(b"hello through the proxy", out)
        time.sleep(0.5)
        self.m.proxy.tick()
        self.assertTrue(self.m.share_state())

    def test_bad_requests(self):
        for req in (b"BREW / HTCPCP/1.0\r\n\r\n", b"GET ftp://x/ HTTP/1.1\r\nProxy-Authorization: %s\r\n\r\n" % self.auth.encode(),
                    b"CONNECT bad host:99999 HTTP/1.1\r\nProxy-Authorization: %s\r\n\r\n" % self.auth.encode()):
            self.raw(req)

    def test_malformed_connect_gets_400(self):  # 7.28: a non-numeric port crashed the connection handler
        out = self.raw(b"CONNECT example.com:abc HTTP/1.1\r\nProxy-Authorization: %s\r\n\r\n" % self.auth.encode())
        self.assertTrue(out.startswith(b"HTTP/1.1 400"), out[:60])

    def test_upstream_choices(self):
        for kind in ("auto", "direct"):
            self.m.settings["proxy_upstream"] = kind
            self.m.upstream()
        self.m.local_proxies()


class Router(unittest.TestCase):
    def setUp(self):
        self.m = manager()
        self.m.net.slow()

    def test_status_counts_traffic_per_device(self):
        g = self.m.gw
        ok, out = g.call("status")
        self.assertTrue(ok)
        g.parse_status(out)
        time.sleep(0.2)
        g.parse_status(out.replace("3400000", "3500000"))
        self.assertIn("192.168.1.50", g.traffic)
        self.assertGreater(g.traffic["192.168.1.50"]["total"], 3000000)
        g.save_usage(force=True)

    def test_router_on_off_and_policies(self):
        g = self.m.gw
        self.m.gw_set({"on": True, "default": "tunnel", "killswitch": True})
        g.sync()
        self.m.gw_set({"policy": {"aa:bb:cc:dd:ee:ff": "direct"}})
        with self.assertRaises(ValueError):
            self.m.gw_set({"policy": {"not a mac": "direct"}})
        g.sync()
        st = g.state()
        self.assertIn("on", st)
        self.m.gw_set({"on": False})
        g.sync()
        self.assertTrue(os.path.exists(os.path.join(GW_DIR, "calls")))

    def test_full_tunnel_and_wizard_and_diag(self):
        with self.assertRaises(ValueError):
            self.m.gw_full({"on": True, "tun": "utun99"})
        self.m.gw_full({"on": False})
        with self.assertRaises(ValueError):
            self.m.gw_wizard({"act": "start", "ip": "192.168.254.254"})
        with self.assertRaises(ValueError):
            self.m.gw_wizard({})
        self.assertTrue(self.m.gw_diag())
        self.m.gw.helper_versions()
        self.m.gw.crash_guard()

    def test_admin_refuses_unknown_actions(self):  # never runs the real password prompt in tests
        with self.assertRaises(ValueError):
            self.m.gw_admin({"act": "format-the-disk"})


class Monitor(unittest.TestCase):
    def test_network_info(self):
        m = manager()
        m.net.slow()
        info = m.net.info
        self.assertIn("iface", info)
        m.net.usage()
        self.assertIsInstance(dlus.own_ips(), (set, list, tuple))


if __name__ == "__main__":
    unittest.main()
