# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""Direct tests of the logic behind the features: the sync algorithm, device names (mDNS), QR codes,
the public-IP check, the crash guard, the DLNA announcer. No internet, no real network changes."""
import importlib.util
import io
import os
import random
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_edges", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)


def manager():
    dlus.STATE_DIR = tempfile.mkdtemp(prefix="dlus-m-")
    m = dlus.Manager(0)
    m.notify = lambda msg: None
    m.settings["base_dir"] = os.path.join(dlus.STATE_DIR, "dl")
    return m


def speech_for(times, seconds, length=0.8):
    sp = [0] * int(seconds * 10)
    for t in times:
        for i in range(int(t * 10), min(len(sp), int((t + length) * 10))):
            if i >= 0:
                sp[i] = 1
    return sp


def lines(seed, n, gap=(1.5, 3.5), start=5.0):
    rnd, out, t = random.Random(seed), [], start
    for _ in range(n):
        out.append(round(t, 2))
        t += rnd.uniform(*gap)
    return out


class SyncAlgorithm(unittest.TestCase):
    """speech_sync with many lines (all 12 sections vote) - the full-length film case"""

    def test_one_shift_for_the_whole_film(self):
        subs = lines(1, 400)
        r = dlus.speech_sync([(t, t + 0.8) for t in subs], speech_for([t + 3.4 for t in subs], subs[-1] + 20))
        self.assertEqual(r["verdict"], "fits")
        self.assertAlmostEqual(r["offset"], 3.4, delta=0.15)

    def test_another_cut_is_fixed_per_part(self):
        subs = lines(2, 400)
        mid = subs[200]
        speech = speech_for([t + 1.0 if t < mid else t + 13.0 for t in subs], subs[-1] + 30)
        r = dlus.speech_sync([(t, t + 0.8) for t in subs], speech)
        self.assertEqual(r["verdict"], "sections", {k: r[k] for k in ("verdict", "offset", "confidence")})
        self.assertTrue(any(abs(x - 1.0) < 0.3 for x in r["shifts"]) and any(abs(x - 13.0) < 0.3 for x in r["shifts"]), r["shifts"])

    def test_another_film_does_not_match(self):
        subs = lines(3, 400)
        r = dlus.speech_sync([(t, t + 0.8) for t in subs], speech_for(lines(77, 400), subs[-1] + 20))
        self.assertEqual(r["verdict"], "nomatch")

    def test_reference_track_check(self):
        ref = lines(4, 60)
        r = dlus.sync_check([t - 1.5 for t in ref], ref)
        self.assertAlmostEqual(r["offset"], 1.5, delta=0.1)
        self.assertGreater(r["match"], 90)
        self.assertIsNone(dlus.sync_check([1, 2], ref))
        self.assertLess(dlus.sync_check(lines(9, 60), ref)["match"], 60)

    def test_vtt_starts(self):
        p = os.path.join(tempfile.mkdtemp(), "a.vtt")
        with open(p, "w") as f:
            f.write("WEBVTT\n\n00:00:01.500 --> 00:00:02.000\na\n\n01:00:00.000 --> 01:00:01.000\nb\n")
        self.assertEqual(dlus.vtt_starts(p), [1.5, 3600.0])


def dns_answer(name):
    """an mDNS / DNS answer: question for 4.3.2.1.in-addr.arpa, one PTR answer with a compressed name"""
    def label(s):
        return b"".join(bytes([len(x)]) + x.encode() for x in s.split(".")) + b"\0"
    q = label("4.3.2.1.in-addr.arpa")
    head = struct.pack(">HHHHHH", 0x1d1d, 0x8400, 1, 1, 0, 0)
    rdata = bytes([len(name)]) + name.encode() + b"\xc0" + bytes([12 + len(q) - 6])  # "<name>" + pointer into ".arpa" (just a test target)
    ans = b"\xc0\x0c" + struct.pack(">HHIH", 12, 1, 120, len(rdata)) + rdata
    return head + q + struct.pack(">HH", 12, 1) + ans


class DeviceNames(unittest.TestCase):
    def test_ptr_answer_with_compression(self):
        name = dlus._ptr_answer(dns_answer("PS5-38121B"))
        self.assertTrue(name.startswith("PS5-38121B."), name)
        self.assertEqual(dlus._ptr_answer(struct.pack(">HHHHHH", 1, 0x8400, 0, 0, 0, 0)), "")
        q = dlus._ptr_query("4.3.2.1.in-addr.arpa", unicast=True)
        self.assertEqual(dlus._dns_name(q, 12)[0], "4.3.2.1.in-addr.arpa")

    def test_lan_name_asks_the_device(self):
        reply = dns_answer("iPad")

        class FakeSock:
            def __init__(self, *a):
                pass

            def settimeout(self, t):
                pass

            def setsockopt(self, *a):
                pass

            def sendto(self, data, addr):
                pass

            def recvfrom(self, n):
                return reply, ("1.2.3.4", 5353)

            def close(self):
                pass
        with mock.patch.object(dlus.socket, "socket", FakeSock):
            self.assertTrue(dlus.lan_name("1.2.3.4", "1.2.3.254").startswith("iPad"))

        class Silent(FakeSock):
            def recvfrom(self, n):
                raise socket.timeout()
        with mock.patch.object(dlus.socket, "socket", Silent):
            self.assertIn(dlus.lan_name("1.2.3.4", ""), ("", "1.2.3.4"))


class QRCodes(unittest.TestCase):
    def test_sizes(self):
        n = 1
        while True:  # every QR size up to the largest dlus supports, then a clear refusal
            try:
                svg = dlus.qr_svg("http://192.168.1.10:8643/abcdef/" + "x" * n)
            except ValueError as e:
                self.assertIn("too long", str(e))
                break
            self.assertIn("<svg", svg)
            n += 15
        self.assertGreater(n, 60)
        m = dlus.qr_matrix("HELLO")
        self.assertEqual(len(m), len(m[0]))


class PublicIP(unittest.TestCase):
    def test_public_ip_with_and_without_answers(self):
        net = dlus.Net()

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(dlus.urllib.request, "urlopen", lambda *a, **k: R(b"203.0.113.7\n")):
            net.pubip()
        self.assertEqual(net.info["public_ip"], "203.0.113.7")

        def down(*a, **k):
            raise OSError("offline")
        with mock.patch.object(dlus.urllib.request, "urlopen", down):
            net.pubip()
        self.assertEqual(net.info["public_ip"], "")
        with mock.patch.object(dlus.urllib.request, "urlopen", lambda *a, **k: R(b"<html>not an address</html>")):
            net.pubip()
        self.assertEqual(net.info["public_ip"], "")


class CrashGuard(unittest.TestCase):
    def test_router_stays_off_after_a_crash(self):
        m = manager()
        g = m.gw
        g.MARK = os.path.join(dlus.STATE_DIR, "gw-active")
        with open(g.MARK, "w") as f:
            f.write("1")
        os.utime(g.MARK, (time.time() - 100, time.time() - 100))
        panic = os.path.join(tempfile.mkdtemp(), "panic-full-2026.panic")
        open(panic, "w").close()
        m.settings["gw_on"] = True
        with mock.patch.object(dlus.glob, "glob", lambda pattern: [panic]):
            g.crash_guard()
        self.assertFalse(m.settings["gw_on"])
        self.assertIn("crash", g.st["error"])
        g.crash_guard()  # no marker: nothing to do


class Announcer(unittest.TestCase):
    def test_dlna_announcements(self):
        m = manager()
        listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listen.bind(("127.0.0.1", 0))
        listen.settimeout(5)
        with mock.patch.object(dlus.SSDP, "GRP", ("127.0.0.1", listen.getsockname()[1])):  # announce to this test, not the home network
            with mock.patch.object(dlus.SSDP, "run", lambda self: None):
                ssdp = dlus.SSDP(m)
            ssdp.notify("ssdp:alive")
            data, _ = listen.recvfrom(4096)
            self.assertIn(b"NOTIFY", data)
            self.assertIn(b"MediaServer", data + listen.recvfrom(4096)[0] + listen.recvfrom(4096)[0])
            self.assertTrue(ssdp.usn(ssdp.uuid).startswith("uuid:"))
            ssdp.stop()
        listen.close()


import threading  # noqa: E402
import zlib  # noqa: E402


class ProxyHandler(unittest.TestCase):
    """the proxy's connection handler, called directly over a socket pair"""

    def setUp(self):
        self.m = manager()
        self.m.settings.update(proxy_auth=False, proxy_upstream="direct")
        self.px = dlus.ProxyServer.__new__(dlus.ProxyServer)  # no listening socket needed
        self.px.m, self.px.clients, self.px.lock = self.m, {}, threading.Lock()
        self.px.rates, self.px.prev = __import__("collections").deque(maxlen=600), 0

    def talk(self, ip, data, close=False):
        a, b = socket.socketpair()
        t = threading.Thread(target=self.px.handle, args=(b, ip), daemon=True)
        t.start()
        if data:
            a.sendall(data)
        if close:
            a.shutdown(socket.SHUT_WR)
        t.join(10)
        b.close()  # as the real server does when the handler returns
        a.settimeout(5)
        out = b""
        try:
            while True:
                c = a.recv(4096)
                if not c:
                    break
                out += c
        except (socket.timeout, OSError):
            pass
        a.close()
        return out

    def test_refusals(self):
        self.assertEqual(self.talk("8.8.8.8", b"GET http://x/ HTTP/1.1\r\n\r\n"), b"")  # not from the home network
        self.assertEqual(self.talk("not-an-ip", b""), b"")
        self.assertEqual(self.talk("127.0.0.1", b"", close=True), b"")  # closed at once
        self.assertEqual(self.talk("127.0.0.1", b"GARBAGE\r\n\r\n"), b"")
        out = self.talk("192.168.40.40", b"GET http://example.com/ HTTP/1.1\r\n\r\n")  # a device nobody allowed
        self.assertTrue(out.startswith(b"HTTP/1.1 403"), out[:40])
        self.assertTrue(self.talk("127.0.0.1", b"CONNECT bad$host:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400"))
        self.assertTrue(self.talk("127.0.0.1", b"CONNECT localhost:8642 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403"))
        self.px.own_ips = lambda: set()
        self.px.local_dest = lambda a: False
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        out = self.talk("127.0.0.1", b"GET http://127.0.0.1:%d/ HTTP/1.1\r\nHost: x\r\n\r\nbody" % port)
        self.assertTrue(out.startswith(b"HTTP/1.1 502"), out[:40])


class TVProtocol(unittest.TestCase):
    """the WebSocket client against tiny scripted TVs: pings, close frames, big frames, refusals"""

    def tv(self, script):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def run():
            c, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            script(c)
            time.sleep(0.5)
            c.close()
            srv.close()
        threading.Thread(target=run, daemon=True).start()
        return srv.getsockname()[1]

    @staticmethod
    def frame(op, data, big=False):
        n = len(data)
        if big:
            return bytes([0x80 | op, 127]) + struct.pack(">Q", n) + data
        return bytes([0x80 | op, n]) + data if n < 126 else bytes([0x80 | op, 126]) + struct.pack(">H", n) + data

    def client(self, port):
        c = dlus.WebOSTV("127.0.0.1")
        c.sock, c.rest = c._ws("127.0.0.1", port, False)
        return c

    def test_ping_big_frames_and_close(self):
        msg = b'{"type": "response", "id": "x", "payload": {"a": "%s"}}' % (b"y" * 70000)

        def script(c):
            c.sendall(self.frame(9, b"ping") + self.frame(1, msg, big=True) + self.frame(8, b""))
        c = self.client(self.tv(script))
        self.assertEqual(c._recv(5)["id"], "x")  # the ping was answered on the way
        with self.assertRaises(OSError):
            c._recv(5)

    def test_tv_says_no(self):
        port = self.tv(lambda c: c.sendall(self.frame(1, b'{"type": "error", "error": "403 user denied"}')))
        c = dlus.WebOSTV("127.0.0.1", "oldkey")
        real_ws = dlus.WebOSTV._ws
        with mock.patch.object(c, "_ws", lambda h, p, tls, path="/": real_ws(c, "127.0.0.1", port, False)):
            with self.assertRaises(OSError) as e:
                c.connect(wait=3)
        self.assertIn("denied", str(e.exception))

    def test_unreachable_tv(self):
        c = dlus.WebOSTV("127.0.0.1")
        with mock.patch.object(c, "_ws", side_effect=OSError("refused")):
            with self.assertRaises(OSError) as e:
                c.connect(wait=1)
        self.assertIn("cannot reach", str(e.exception))


class RouterLogic(unittest.TestCase):
    def test_new_devices_policies_and_missing_helper(self):
        m = manager()
        g = m.gw
        g.lan = lambda: {"if": "en0", "ip": "192.168.1.2", "cidr": "192.168.1.0/24", "gw": "192.168.1.1"}
        g.scan = lambda lan: {"aa:aa:aa:aa:aa:01": {"ip": "192.168.1.50", "name": "LG TV"}, "aa:aa:aa:aa:aa:02": {"ip": "192.168.1.60", "name": "PS5"}}
        notes = []
        m.notify = notes.append
        m.settings.update(gw_on=True, gw_policy={"aa:aa:aa:aa:aa:01": "tunnel"})
        with mock.patch.dict(os.environ, {"DLUS_GW_HELPER": "/nonexistent/dlus-gw"}):
            g.sync()
            self.assertEqual(g.st.get("error"), "helper not installed")
        self.assertTrue(any("PS5" in x for x in notes))  # a new device on the network is announced


class Subtitles(unittest.TestCase):
    def test_archives_and_garbage(self):
        m = manager()
        p = os.path.join(m.settings["base_dir"], "Video", "film.mkv")
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(b"\x1aE\xdf\xa3" + os.urandom(1000))
        with open(p[:-4] + ".en.srt", "w") as f:  # a subtitle next to the film is picked up
            f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        d = m.import_local(p)
        m.scan_sidecars(d)
        self.assertTrue(d.r.get("subs"))
        good = zlib.compress(b"1\n00:00:05,000 --> 00:00:06,000\nGz\n", 9, wbits=31)
        self.assertTrue(m.subs_add(d, "a.srt.gz", good))
        with self.assertRaises(ValueError):
            m.subs_add(d, "b.srt.gz", b"\x1f\x8b" + b"not gzip at all")
        bomb = zlib.compress(b"0" * (40 << 20), 9, wbits=31)
        with self.assertRaises(ValueError):
            m.subs_add(d, "c.srt.gz", bomb)
        with self.assertRaises(ValueError):
            m.subs_add(d, "d.srt", os.urandom(3000))


class SharedFolderBlocked(unittest.TestCase):
    def test_macos_privacy_block(self):
        m = manager()
        share = os.path.realpath(tempfile.mkdtemp(prefix=".dlus-test-share-", dir=os.path.expanduser("~")))
        try:
            os.makedirs(os.path.join(share, "Season 1"))
            m.settings["share_dirs"] = [share]
            self.assertIn("Season 1", dlus.tv_folder(m, 0, ""))
            with mock.patch.object(dlus, "list_dir", side_effect=PermissionError("TCC")):
                self.assertIn("Privacy", dlus.tv_folder(m, 0, ""))
            with mock.patch.object(dlus, "list_dir", side_effect=OSError("gone")):
                self.assertIn("Cannot open", dlus.tv_folder(m, 0, ""))
        finally:
            __import__("shutil").rmtree(share, ignore_errors=True)


class FullTunnel(unittest.TestCase):
    def test_route_everything_and_undo(self):
        os.environ["DLUS_GW_HELPER"] = os.path.join(tempfile.mkdtemp(), "gw")
        with open(os.environ["DLUS_GW_HELPER"], "w") as f:
            f.write('#!/bin/sh\necho "ok $*"\n')
        os.chmod(os.environ["DLUS_GW_HELPER"], 0o700)
        m = manager()
        m.gw.tunnels = lambda: [{"if": "utun9", "ip": "10.0.0.2", "name": "Test VPN"}]
        real_sh = dlus.sh
        for answer, ok in (("204", True), ("000", False)):  # internet through the tunnel works / does not (then undone)
            with mock.patch.object(dlus, "sh", lambda cmd, t=5, a=answer: a if "gstatic" in " ".join(cmd) else real_sh(cmd, t)):
                r = m.gw_full({"on": True, "tun": "utun9"})
            self.assertEqual(r["ok"], ok, r)
        m.gw_full({"on": False})
        with mock.patch.object(dlus, "sh", lambda cmd, t=5: "204 0.12"):
            self.assertTrue(m.gw_vpn_ok("utun9")["ok"])
            self.assertTrue(m.gw_vpn_ok("utun9")["ok"])  # cached


class AnnouncerAnswers(unittest.TestCase):
    def test_answers_a_tv_search(self):
        m = manager()
        listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listen.bind(("127.0.0.1", 0))
        with mock.patch.object(dlus.SSDP, "GRP", ("127.0.0.1", listen.getsockname()[1])):
            ssdp = dlus.SSDP(m)  # listens on UDP 1900 like a real DLNA server
            time.sleep(0.5)
            tv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tv.settimeout(3)
            for st in ("ssdp:all", "urn:schemas-upnp-org:device:MediaServer:1", "urn:other"):
                tv.sendto(("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nST: %s\r\n\r\n" % st).encode(),
                          ("127.0.0.1", 1900))
            tv.sendto(b"NOTIFY * HTTP/1.1\r\n\r\n", ("127.0.0.1", 1900))
            try:
                data, _ = tv.recvfrom(4096)
                self.assertIn(b"LOCATION:", data)
            except socket.timeout:
                pass  # another DLNA server on this Mac took the packet: nothing to check here
            ssdp.stop()
            tv.close()
        listen.close()


class Discovery(unittest.TestCase):
    def test_parses_what_a_tv_answers(self):
        import http.server
        desc = (b'<root><device><friendlyName>Living Room TV</friendlyName><manufacturer>LG</manufacturer><modelName>OLED</modelName>'
                b'<UDN>uuid:tv-0001</UDN><serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>'
                b'<controlURL>/upnp/control/AVTransport1</controlURL></service></serviceList></device></root>')

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(desc)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        answers = [(b"HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1:%d/desc.xml\r\n\r\n" % port, ("127.0.0.1", 1900)),
                   (b"HTTP/1.1 200 OK\r\nLOCATION: file:///etc/passwd\r\n\r\n", ("127.0.0.1", 1900)),  # refused: not http
                   (b"HTTP/1.1 200 OK\r\nLOCATION: http://10.9.9.9/desc.xml\r\n\r\n", ("127.0.0.1", 1900))]  # refused: another host

        class Sock:
            def __init__(self, *a):
                pass

            def setsockopt(self, *a):
                pass

            def bind(self, a):
                pass

            def sendto(self, *a):
                pass

            def settimeout(self, t):
                pass

            def recvfrom(self, n):
                if answers:
                    return answers.pop(0)
                raise socket.timeout()

            def close(self):
                pass
        m = manager()
        try:
            real = socket.socket

            def only_udp(*a, **k):  # the search socket is scripted; the HTTP fetch of the description is real
                return Sock() if len(a) > 1 and a[1] == socket.SOCK_DGRAM else real(*a, **k)
            with mock.patch.object(dlus.socket, "socket", only_udp):
                found = m.find_tvs(wait=1.0)
            tv = [t for t in found if t["name"] == "Living Room TV"]
            self.assertEqual(len(tv), 1)
            self.assertEqual(tv[0]["ctl"], "http://127.0.0.1:%d/upnp/control/AVTransport1" % port)
            self.assertEqual(len(found), 1)  # the file:// and other-host answers were ignored
        finally:
            srv.shutdown()


class FontDownloads(unittest.TestCase):
    def test_failures_are_logged_not_fatal(self):
        m = manager()

        def down(*a, **k):
            raise OSError("offline")
        with mock.patch.object(dlus.urllib.request, "urlopen", down):
            dlus.fetch_fonts()
            name = next(n for n, f in dlus.FONT_CATALOG.items() if not f.get("default"))
            with self.assertRaises(ValueError):
                m.fonts_set({"install": name})

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(dlus.urllib.request, "urlopen", lambda *a, **k: R(b"<html>not a font</html>")):
            dlus.fetch_fonts()
            with self.assertRaises(ValueError):
                m.fonts_set({"install": name})
        with mock.patch.object(dlus.urllib.request, "urlopen", lambda *a, **k: R(b"wOF2" + b"0" * 2000)):
            dlus.fetch_fonts()  # the default fonts arrive


class Actions(unittest.TestCase):
    def test_start_now_players_and_bad_links(self):
        m = manager()
        p = os.path.join(m.settings["base_dir"], "Video", "a.mkv")
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(b"\x1aE\xdf\xa3" + b"0" * 100)
        d = m.import_local(p)
        with self.assertRaises(ValueError):
            m.action(d.r["id"], "play", {"app": "NoSuchPlayer"})
        with self.assertRaises(ValueError):
            m.action(d.r["id"], "refresh", {"url": "file:///etc/passwd"})
        self.assertIsInstance(m.players(), list)


class PartialFiles(unittest.TestCase):
    def test_what_can_be_played_while_downloading(self):
        m = manager()
        r = {"id": "partial01", "url": "http://127.0.0.1:9/p.mkv", "name": "p.mkv", "dir": os.path.join(m.settings["base_dir"], "Video"),
             "conns": 1, "status": "paused", "added": time.time(), "size": 3 * dlus.C, "done": 0, "msg": "", "checksum": "", "named": True,
             "fails": 0, "bound_ip": "", "num": 1, "ranges": True}
        os.makedirs(r["dir"], exist_ok=True)
        d = dlus.DL(m, r)
        m.dls[r["id"]] = d
        os.makedirs(d.parts, exist_ok=True)
        with open(d.cpath(0), "wb") as f:
            f.write(b"\x1aE\xdf\xa3" + b"0" * (dlus.C - 4))
        self.assertEqual(m.local_prefix(d), [d.cpath(0)])
        self.assertIsNotNone(d.avail(10))
        self.assertIsNone(d.avail(2 * dlus.C + 5))
        r["ranges"] = False
        with open(d.out + ".part", "wb") as f:
            f.write(b"\x1aE\xdf\xa3" + b"0" * 100)
        self.assertEqual(m.local_prefix(d), [d.out + ".part"])


if __name__ == "__main__":
    unittest.main()
