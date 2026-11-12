# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""The local API (the dlus window) and the home-network server (TV, players, streams, shared folders, DLNA)."""
import base64
import http.client
import importlib.util
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _testserver import FileServer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
FAKE_GW = os.path.join(tempfile.mkdtemp(prefix="dlus-gw-"), "dlus-gw")
with open(FAKE_GW, "w") as f:  # a stand-in for the root helper: answers like it, changes nothing
    f.write('#!/bin/sh\ncase "$1" in\nversion) echo "dlus-gw 3";;\nstatus) echo "dlus-gw 3 forwarding=0 redirect=1 full=-"; echo "rules=none";;\n'
            'full) echo "ok all traffic via $3";;\nenable) echo "ok lan=$2 tun=$5";;\n*) echo ok;;\nesac\n')
os.chmod(FAKE_GW, stat.S_IRWXU)
os.environ["DLUS_GW_HELPER"] = FAKE_GW
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_srv", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class Servers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = tempfile.mkdtemp(prefix="dlus-m-")
        dlus.STATE_DIR = home
        cls.home = home
        m = cls.m = dlus.Manager(0)
        m.notify = lambda msg: None
        m.settings["base_dir"] = os.path.join(home, "dl")
        m.settings["lan_port"] = free_port()
        # a download that is done, and a shared folder with a film, a text file and a hidden file
        cls.files = tempfile.mkdtemp()
        cls.fs = FileServer(cls.files)
        cls.body = os.urandom(300000)
        cls.fs.put("film.mp4", cls.body)
        did = m.add(cls.fs.url("film.mp4"))
        end = time.time() + 60
        while m.dls[did].r["status"] != "done" and time.time() < end:
            m.tick()
            time.sleep(0.1)
        cls.d = m.dls[did]
        cls.share = tempfile.mkdtemp(prefix=".dlus-test-share-", dir=os.path.expanduser("~"))  # /private (temp) cannot be shared
        cls.share = os.path.realpath(cls.share)
        os.makedirs(os.path.join(cls.share, "Series", ".hidden"))
        with open(os.path.join(cls.share, "Series", "ep1.mkv"), "wb") as f:
            f.write(b"\x1aE\xdf\xa3" + os.urandom(20000))
        with open(os.path.join(cls.share, "Series", ".hidden", "key"), "w") as f:
            f.write("secret")
        m.set_settings({"share_dirs": [cls.share], "files_on": True, "lan_on": True})
        # the local API server
        cls.port = free_port()
        m.port = cls.port
        cls.ui = dlus.http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), dlus.make_handler(m, cls.port))
        threading.Thread(target=cls.ui.serve_forever, daemon=True).start()
        # the home-network server, here on 127.0.0.1 (this Mac counts as allowed)
        cls.lan = dlus.LanServer(m, host="127.0.0.1", port=m.settings["lan_port"])
        cls.tok = m.settings["lan_token"]

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.share, ignore_errors=True)
        cls.ui.shutdown()
        cls.lan.stop()
        cls.fs.stop()

    def req(self, method, path, body=None, port=None, headers=None, key=True):
        port = port or self.port
        h = {"Host": "127.0.0.1:%d" % port}
        if key and port == self.port:
            h["Cookie"] = "dlus_k=" + self.m.ui_key
        if body is not None:
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request(method, path, body, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def api(self, path, body=None):
        st, data, _ = self.req("POST" if body is not None else "GET", path, body)
        return st, (json.loads(data) if data[:1] in (b"{", b"[") else data)

    # ------------------------------------------------------------ local API
    def test_page_and_state(self):
        st, data, _ = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"dlus", data)
        st, s = self.api("/api/state")
        self.assertEqual(st, 200)
        for k in ("items", "settings", "net", "devices", "access", "gw", "share", "lan", "fonts"):
            self.assertIn(k, s)
        self.assertEqual(self.api("/api/health")[0], 200)

    def test_icons_qr_and_fonts(self):
        for p in ("/favicon.svg", "/logo.svg", "/qr.svg?d=http%3A%2F%2Fexample%2F", "/icon-192.png", "/manifest.webmanifest"):
            self.assertEqual(self.req("GET", p)[0], 200, p)
        self.assertIn(b"<svg", self.req("GET", "/qr.svg?d=hello")[1])

    def test_detail_and_stream_with_ranges(self):
        did = self.d.r["id"]
        st, det = self.api("/api/d/" + did)
        self.assertEqual(st, 200)
        st, data, r = self.req("GET", "/stream/%s/x?ik=%s" % (did, self.m.media_key), headers={"Range": "bytes=100-199"}, key=False)
        self.assertEqual(st, 206)
        self.assertEqual(data, self.body[100:200])
        self.assertEqual(self.req("GET", "/stream/%s/x" % did, key=False)[0], 401)

    def test_settings_roundtrip(self):
        st, _ = self.api("/api/settings", {"limit_mb": 3, "max_active": 2, "sched_on": False, "notify": False})
        self.assertEqual(st, 200)
        s = self.api("/api/state")[1]["settings"]
        self.assertEqual((s["limit_mb"], s["max_active"]), (3, 2))
        self.api("/api/settings", {"limit_mb": 0})
        self.assertEqual(self.api("/api/settings", {"share_dirs": ["/etc"]})[0], 400)

    def test_add_through_the_api(self):
        self.fs.put("api.txt", b"hello")
        st, r = self.api("/api/add", {"urls": "look: %s and nothing else" % self.fs.url("api.txt")})
        self.assertEqual(st, 200)
        self.assertEqual(len(r["ids"]), 1)
        self.assertEqual(self.api("/api/add", {"urls": "no links here"})[0], 400)
        self.api("/api/d/%s/delete" % r["ids"][0], {})

    def test_browse_and_import_shared_files(self):
        st, r = self.api("/api/browse?i=0&rel=Series")
        self.assertEqual(st, 200)
        names = json.dumps(r)
        self.assertIn("ep1.mkv", names)
        self.assertNotIn(".hidden", names)
        st, r = self.api("/api/import", {"folder": 0, "rel": "Series/ep1.mkv"})
        self.assertEqual(st, 200)
        self.assertEqual(self.api("/api/import", {"folder": 0, "rel": "../../etc/passwd"})[0], 404)

    def test_subtitles_add_set_delete(self):
        did = self.d.r["id"]
        srt = "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\nWorld\n"
        st, r = self.api("/api/subs/%s/add" % did, {"name": "film.en.srt", "b64": base64.b64encode(srt.encode()).decode()})
        self.assertEqual(st, 200, r)
        sid = r["added"][0]
        self.assertEqual(self.api("/api/subs/%s/set" % did, {"sid": sid, "delay": 0.5})[0], 200)
        st, info = self.api("/api/subs/" + did)
        self.assertEqual(st, 200)
        st, data, _ = self.req("GET", "/sub/%s/%s.vtt?ik=%s" % (did, sid, self.m.media_key), key=False)
        self.assertIn(st, (200, 404))
        self.assertEqual(self.api("/api/subs/%s/del" % did, {"sid": sid})[0], 200)

    def test_access_api(self):
        self.m.lan_access("192.168.200.9", "Mozilla/5.0 (iPad)", "opened the TV home page")
        st, s = self.api("/api/state")
        self.assertIn("192.168.200.9", [p["ip"] for p in s["access"]["pending"]])
        self.assertEqual(self.api("/api/access", {"ip": "192.168.200.9", "act": "deny"})[0], 200)
        self.assertEqual(self.api("/api/access", {"ip": "192.168.200.9", "act": "allow"})[0], 400)

    def test_router_with_a_fake_helper(self):
        for body in ({"on": True}, {"killswitch": False}, {"default": "direct"}, {"on": False}):
            self.assertEqual(self.api("/api/gw", body)[0], 200, body)
        st, r = self.api("/api/gw/diag", {})
        self.assertEqual(st, 200)
        self.assertEqual(self.api("/api/gw/dismiss", {})[0], 200)
        self.api("/api/gw/wizard", {"step": "start"})
        self.m.gw.state()

    def test_fonts_api(self):
        st, r = self.api("/api/fonts", {"remove": "Vazirmatn"})
        self.assertEqual(st, 400)  # a built-in font cannot be removed

    def test_actions_on_everything(self):
        self.assertEqual(self.api("/api/d/all/pause", {})[0], 200)
        self.assertEqual(self.api("/api/d/nope/pause", {})[0], 400)

    def test_wrong_host_and_origin(self):
        self.assertEqual(self.req("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/settings", {}, headers={"Origin": "http://evil.example"})[0], 403)

    # ------------------------------------------------------------ home network
    def lget(self, path, headers=None):
        return self.req("GET", path, port=self.m.settings["lan_port"], headers=headers)

    def test_code_page_and_wrong_codes(self):
        self.assertEqual(self.lget("/")[0], 200)
        st, data, _ = self.lget("/pin?c=" + self.tok)
        self.assertEqual(json.loads(data)["ok"], True)
        self.assertEqual(self.lget("/pin?c=zzzzzz")[0], 403)
        self.assertEqual(self.lget("/abcdef/tv")[0], 404)
        self.m.pin_fail.clear()

    def test_tv_pages(self):
        num = self.d.r["num"]
        for p in ("/%s/tv", "/%s/%d" % ("%s", num), "/%s/netsetup", "/%s/files/", "/%s/files/dir/"):
            st, data, _ = self.lget(p % self.tok, {"Accept": "text/html"})
            self.assertEqual(st, 200, p)
        self.assertEqual(self.lget("/%s/cmd?since=0" % self.tok)[0], 200)
        self.assertEqual(self.lget("/%s/cmd?num=%d&t=1.5&dur=10&p=1&ev=play" % (self.tok, num))[0], 200)

    def test_stream_on_the_network(self):
        num = self.d.r["num"]
        st, data, _ = self.lget("/%s/%d.raw" % (self.tok, num), {"Range": "bytes=0-99"})  # <num>.mp4 is the converted stream
        self.assertEqual(st, 206)
        self.assertEqual(data, self.body[:100])
        st, data, r = self.lget("/%s/%d" % (self.tok, num), {"Accept": "text/html"})
        self.assertEqual(st, 200)

    def test_shared_folder_on_the_network(self):
        st, data, _ = self.lget("/%s/files/0/Series/" % self.tok, {"Accept": "text/html"})
        self.assertIn(st, (200, 404))
        st, data, _ = self.lget("/%s/f/0/Series/ep1.mkv" % self.tok, {"Range": "bytes=0-3"})
        self.assertIn(st, (200, 206))
        self.assertEqual(self.lget("/%s/f/0/Series/.hidden/key" % self.tok)[0], 404)
        self.assertEqual(self.lget("/%s/f/0/../../etc/passwd" % self.tok)[0], 404)

    def test_dlna(self):
        self.m.ssdp = True
        try:
            st, data, _ = self.lget("/dlna/desc.xml")
            self.assertEqual(st, 200)
            self.assertIn(b"MediaServer", data)
            body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1"><ObjectID>%s</ObjectID>'
                    '<BrowseFlag>BrowseDirectChildren</BrowseFlag><StartingIndex>0</StartingIndex><RequestedCount>50</RequestedCount>'
                    '</u:Browse></s:Body></s:Envelope>')
            for oid in ("0", "dl", "f0", "f0:Series"):
                c = http.client.HTTPConnection("127.0.0.1", self.m.settings["lan_port"], timeout=30)
                c.request("POST", "/dlna/cds/ctl", (body % oid).encode(), {"SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
                                                                           "Content-Type": 'text/xml; charset="utf-8"'})
                r = c.getresponse()
                self.assertEqual(r.status, 200, oid)
                r.read()
        finally:
            self.m.ssdp = None

    def test_proxy_settings_file(self):
        self.m.set_settings({"proxy_on": True, "proxy_port": free_port()})
        try:
            st, data, _ = self.lget("/%s/proxy.pac" % self.tok)
            self.assertIn(st, (200, 404))
        finally:
            self.m.set_settings({"proxy_on": False})


if __name__ == "__main__":
    unittest.main()
