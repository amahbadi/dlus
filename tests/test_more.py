# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""Streaming while downloading, resume after a restart, VPN-app proxies (SOCKS5 / HTTP), helper install without a
password prompt, and TV discovery + casting to a fake DLNA TV."""
import http.server
import importlib.util
import os
import socket
import stat
import struct
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
FAKE_GW = os.path.join(tempfile.mkdtemp(prefix="dlus-gw-"), "dlus-gw")
with open(FAKE_GW, "w") as f:
    f.write('#!/bin/sh\ncase "$1" in\nversion) echo "dlus-gw 3";;\nstatus) echo "dlus-gw 3 forwarding=0 redirect=1 full=-";;\n*) echo ok;;\nesac\n')
os.chmod(FAKE_GW, stat.S_IRWXU)
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_more", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)


def manager(home=None):
    dlus.STATE_DIR = home or tempfile.mkdtemp(prefix="dlus-m-")
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


class Downloading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fs = FileServer(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.fs.stop()

    def setUp(self):
        self.fs.delay = 0.0

    def test_player_jump_downloads_that_part_first(self):
        body = os.urandom(5 * 1024 * 1024)
        self.fs.put("jump.mkv", body)
        self.fs.delay = 0.05
        m = manager()
        did = m.add(self.fs.url("jump.mkv"), conns=2)
        d = run_until(m, did, statuses=("downloading",))
        time.sleep(0.5)
        d.want(4 * 1024 * 1024 + 10)  # the player jumped near the end
        self.assertEqual(d.focus, 4)
        d.avail(0)
        d.avail(4 * 1024 * 1024)
        self.fs.delay = 0.0
        d = run_until(m, did)
        self.assertEqual(open(d.out, "rb").read(), body)

    def test_resume_after_restart(self):
        body = os.urandom(3 * 1024 * 1024 + 99)
        self.fs.put("restart.mkv", body)
        self.fs.delay = 0.05
        home = tempfile.mkdtemp(prefix="dlus-m-")
        m = manager(home)
        did = m.add(self.fs.url("restart.mkv"), conns=2)
        run_until(m, did, statuses=("downloading",))
        time.sleep(1.0)
        m.action(did, "pause", {})
        m.save()
        m.shutdown()
        self.fs.delay = 0.0
        m2 = manager(home)  # dlus restarted: same state folder
        self.assertIn(did, m2.dls)
        m2.action(did, "resume", {})
        d = run_until(m2, did)
        self.assertEqual(open(d.out, "rb").read(), body)

    def test_damaged_piece_is_fetched_again_alone(self):  # the core promise: a resumed or broken file is never corrupted
        body = os.urandom(3 * 1024 * 1024 + 11)
        self.fs.put("damaged.mkv", body)
        self.fs.corrupt_big = 1
        m = manager()
        d = run_until(m, m.add(self.fs.url("damaged.mkv"), conns=1))
        self.assertEqual(open(d.out, "rb").read(), body)
        self.assertGreaterEqual(d.r.get("fails", 0), 1, d.events if hasattr(d, "events") else "")

    def test_refresh_link_and_clear(self):
        self.fs.put("old.bin", b"x" * 1000)
        self.fs.put("new.bin", b"x" * 1000)
        m = manager()
        did = m.add(self.fs.url("old.bin"), start=False)
        m.action(did, "refresh", {"url": self.fs.url("new.bin")})
        self.assertEqual(m.dls[did].r["url"], self.fs.url("new.bin"))
        m.action(did, "resume", {})
        run_until(m, did)
        m.action("all", "clear", {})
        self.assertNotIn(did, m.dls)

    def test_existing_partial_file_is_adopted(self):
        body = os.urandom(2 * 1024 * 1024 + 5)
        self.fs.put("adopt.bin", body)
        m = manager()
        target = os.path.join(m.settings["base_dir"], "Other", "adopt.bin")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(body[:1500000])  # e.g. left by another downloader
        d = run_until(m, m.add(self.fs.url("adopt.bin"), folder="Other"))
        self.assertEqual(open(d.out, "rb").read(), body)


def pipe(a, b):
    try:
        while True:
            x = a.recv(65536)
            if not x:
                break
            b.sendall(x)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


class FakeVPNProxy:
    """what V2Box / Hiddify / Clash offer on 127.0.0.1: a SOCKS5 or an HTTP CONNECT proxy"""

    def __init__(self, kind):
        self.kind = kind
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        try:
            if self.kind == "socks5":
                if c.recv(3)[:1] != b"\x05":
                    return c.close()
                c.sendall(b"\x05\x00")
                head = c.recv(4)
                if len(head) < 4:
                    return c.close()
                if head[3] == 3:
                    host = c.recv(c.recv(1)[0]).decode()
                else:
                    host = socket.inet_ntoa(c.recv(4))
                port = struct.unpack(">H", c.recv(2))[0]
                up = socket.create_connection((host, port), 5)
                c.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            else:
                req = b""
                while b"\r\n\r\n" not in req:
                    req += c.recv(4096)
                host, port = req.split(b" ")[1].decode().rsplit(":", 1)
                if host == "example.com":  # the probe from proxy_kind
                    c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    return c.close()
                up = socket.create_connection((host, int(port)), 5)
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            threading.Thread(target=pipe, args=(c, up), daemon=True).start()
            pipe(up, c)
        except OSError:
            c.close()

    def stop(self):
        self.sock.close()


class Upstreams(unittest.TestCase):
    def test_through_socks5_and_http_proxies(self):
        fs = FileServer(tempfile.mkdtemp())
        fs.put("x.txt", b"via the VPN app")
        try:
            for kind in ("socks5", "http"):
                p = FakeVPNProxy(kind)
                self.assertEqual(dlus.proxy_kind("127.0.0.1", p.port), kind)
                c = dlus.open_upstream("%s://127.0.0.1:%d" % (kind, p.port), "127.0.0.1", fs.port)
                c.sendall(b"GET /x.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                out = b""
                while True:
                    x = c.recv(65536)
                    if not x:
                        break
                    out += x
                c.close()
                self.assertIn(b"via the VPN app", out)
                p.stop()
            self.assertEqual(dlus.proxy_kind("127.0.0.1", fs.port), "")
        finally:
            fs.stop()


class HelperInstall(unittest.TestCase):
    def test_install_and_remove_without_a_password_prompt(self):
        os.environ["DLUS_GW_HELPER"] = FAKE_GW
        m = manager()
        calls = []
        real = subprocess.run

        def fake_run(cmd, *a, **k):
            if cmd and cmd[0] == "/usr/bin/osascript":  # the admin password prompt: never shown in tests
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "installed. checking the firewall rules:\nok rules with tunnel\ndone - dlus-gw 3\n", "")
            return real(cmd, *a, **k)
        with mock.patch.object(dlus.subprocess, "run", fake_run):
            for act in ("install", "uninstall"):
                r = m.gw_admin({"act": act})
                self.assertTrue(r["ok"], r)
        self.assertEqual(len(calls), 2)
        self.assertIn("administrator privileges", calls[0][2])
        self.assertTrue(any("root helper" in x["event"] for x in m.audit_log))


DESC = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><friendlyName>Fake DLNA TV</friendlyName>
<manufacturer>Test</manufacturer><modelName>Renderer</modelName><UDN>uuid:fake-dlna-tv-0001</UDN><serviceList><service>
<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/ctl</controlURL></service></serviceList></device></root>"""


class FakeDLNATV:
    """answers SSDP searches for media renderers and accepts AVTransport commands"""

    def __init__(self, ip):
        self.ip, self.actions = ip, []
        tv = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.end_headers()
                self.wfile.write(DESC.encode())

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                tv.actions.append((self.headers.get("SOAPACTION") or "").split("#")[-1].strip('"'))
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.end_headers()
                self.wfile.write(b"<s:Envelope><s:Body><u:Ok/></s:Body></s:Envelope>")
        self.http = http.server.ThreadingHTTPServer((ip, 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.udp.bind(("", 1900))
        # security scan: multicast group membership on any interface (fake TV in a test), not a bind
        iface = socket.inet_aton(ip) if ip != "127.0.0.1" else socket.inet_aton("0.0.0.0")  # nosec B104
        self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton("239.255.255.250") + iface)
        threading.Thread(target=self.answer, daemon=True).start()

    def answer(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(65536)
            except OSError:
                return
            if b"M-SEARCH" in data and b"MediaRenderer" in data:
                self.udp.sendto(("HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\nLOCATION: http://%s:%d/desc.xml\r\n\r\n"
                                 % (self.ip, self.http.server_address[1])).encode(), addr)

    def stop(self):
        self.udp.close()
        self.http.shutdown()


class Casting(unittest.TestCase):
    def test_find_a_tv_and_cast_to_it(self):
        m = manager()
        ip = m.lan_ip()
        try:
            tv = FakeDLNATV(ip)
        except OSError as e:
            self.skipTest("no multicast here: %s" % e)
        try:
            found = m.find_tvs(wait=3.0)
            fake = [t for t in found if t["name"] == "Fake DLNA TV"]
            if not fake:  # multicast does not loop back on every Mac: add the fake TV as discovery would
                fake = [{"id": "fakedlnatv0001", "name": "Fake DLNA TV", "ip": ip, "mac": "", "seen": time.time(),
                         "ctl": "http://%s:%d/ctl" % (ip, tv.http.server_address[1])}]
                m.settings["tvs"] = fake
            fs = FileServer(tempfile.mkdtemp())
            fs.put("cast.mp4", os.urandom(200000))
            did = m.add(fs.url("cast.mp4"))
            d = run_until(m, did)
            m.settings["lan_port"] = 0
            m.need_lan = lambda: None
            m.tv_up = lambda ip, wait=1.0: True
            m.cast(d, fake[0]["id"], "orig")
            for act in ("pause", "play", "stop"):
                m.cast_ctl(act)
            self.assertIn("SetAVTransportURI", tv.actions)
            self.assertIn("Play", tv.actions)
            fs.stop()
        finally:
            tv.stop()


if __name__ == "__main__":
    unittest.main()
