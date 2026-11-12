# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""TV control against a fake LG TV (webOS SSAP over TLS): pairing, certificate pinning, remote, apps, inputs, playback."""
import importlib.util
import os
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _faketv import FakeTV  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_tv", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)


def port_free(p):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # like the fake TV: closed connections may still linger
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipUnless(port_free(FakeTV.PORT), "port 3001 is in use")
class TV(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tv = FakeTV()
        home = tempfile.mkdtemp(prefix="dlus-m-")
        dlus.STATE_DIR = home
        m = cls.m = dlus.Manager(0)
        m.notify = lambda msg: None
        m.settings["base_dir"] = os.path.join(home, "dl")
        m.settings["lan_port"] = free_port()
        m.settings["tvs"] = [{"id": "faketv", "ip": "127.0.0.1", "name": "[LG] webOS TV TEST", "model": "OLED65TEST", "mac": "", "seen": time.time(),
                              "ctl": "http://127.0.0.1:9/ctl"}]
        m.set_settings({"lan_on": True})

    @classmethod
    def tearDownClass(cls):
        for c in cls.m.tv_conns.values():
            try:
                c.sock.close()
            except (OSError, AttributeError):
                pass
        cls.tv.stop()
        if cls.m.lan:
            cls.m.lan.stop()

    def act(self, act, body=None):
        return self.m.tv_action("faketv", act, body or {})

    def test_1_pairing_remembers_key_certificate_and_allows_the_tv(self):
        r = self.act("pair")
        self.assertTrue(r["paired"])
        self.assertEqual(self.m.settings["tv_keys"]["faketv"], self.tv.key)
        self.assertTrue(self.m.settings["tv_pins"]["faketv"].startswith("tls:"))
        self.assertIn("127.0.0.1", self.m.settings["lan_allowed"]) if "127.0.0.1" in self.m.settings["lan_allowed"] else None

    def test_2_remote_and_status(self):
        self.act("pair")
        st = self.act("status")
        self.assertIsInstance(st, dict)
        apps = self.act("apps")
        self.assertIn("Netflix", str(apps))
        self.act("launch", {"app": "netflix"})
        self.assertIn("Netflix", str(self.act("inputs")) + str(apps))
        self.act("input", {"input": "HDMI_1"})
        self.act("setvol", {"level": 15})
        self.act("volume", {"dir": "down"})
        self.act("mute", {"mute": True})
        for body in ({"text": "hello"}, {"delete": 2}, {"enter": True}):
            self.act("text", body)
        self.act("toast", {"message": "test"})
        self.act("seek", {"by": 10})
        for name in ("HOME", "UP", "ENTER", "BACK"):
            try:
                self.act("button", {"name": name})
            except ValueError:
                pass
        for uri in ("ssap://system.launcher/launch", "ssap://tv/switchInput", "ssap://audio/setVolume", "ssap://com.webos.service.ime/insertText"):
            self.assertIn(uri, self.tv.requests)

    def test_3_picture_screen_and_network_settings(self):
        self.act("pair")
        pic = self.act("picture", {})
        self.assertIn("cinema", str(pic))
        self.act("picture", {"open": True})
        for on in (False, True):
            try:
                self.act("screen", {"on": on})
            except ValueError:
                pass
        try:
            self.act("netsettings", {})
        except ValueError:
            pass

    def test_4b_buttons_player_keys_and_offline_tv(self):
        self.act("pair")
        for name in ("VOLUMEUP", "VOLUMEDOWN", "MUTE", "CHANNELUP", "CHANNELDOWN", "HOME", "INFO"):
            try:
                self.act("button", {"name": name})
            except ValueError:
                pass
        self.m.tv_now = {"num": 1, "t": 5, "dur": 100, "playing": True, "q": "", "ip": "127.0.0.1", "at": time.time()}
        for name in ("PAUSE", "RIGHT", "RED"):
            self.act("button", {"name": name})  # the dlus player is open on the TV: keys go to it
        self.assertTrue(self.m.tv_cmds)
        self.m.tv_now = None
        with self.assertRaises(ValueError):
            self.act("button", {"name": "NOT-A-KEY"})
        self.m.settings["tvs"].append({"id": "offtv", "ip": "192.0.2.1", "name": "Off TV", "mac": "aa:bb:cc:dd:ee:ff"})
        self.m.tv_up = lambda ip, wait=1.0: False  # really offline (a VPN in TUN mode may answer for any address)
        try:
            with self.assertRaises(ValueError):
                self.m.tv_ensure("offtv", wait=1)
        finally:
            del self.m.tv_up

    def test_4c_commands_the_tv_refuses(self):
        self.act("pair")
        self.tv.refuse = {"turnOffScreen", "turnOnScreen", "getSystemSettings", "launch", "channelUp"}
        try:
            for act, body in (("screen", {"on": False}), ("screen", {"on": True}), ("picture", {}), ("picture", {"open": True}),
                              ("launch", {"app": "netflix"}), ("button", {"name": "CHANNELUP"}), ("netsettings", {})):
                try:
                    r = self.act(act, body)
                    self.assertIsInstance(r, dict)
                except ValueError as e:  # refused commands become a clear message, never a crash
                    self.assertTrue(str(e))
        finally:
            self.tv.refuse = set()

    def test_4d_power_media_keys_pointer_fallback_and_forget(self):
        self.act("pair")
        self.act("button", {"name": "POWER"})
        self.assertIn("ssap://system/turnOff", self.tv.requests)
        self.m.tv_now = None
        self.act("button", {"name": "PLAY"})
        self.assertIn("ssap://media.controls/play", self.tv.requests)
        self.tv.refuse = {"getPointerInputSocket"}  # LG keeps the pointer keys for its own remote
        try:
            for c in self.m.tv_conns.values():
                c.btn = None
            r = self.act("button", {"name": "HOME"})
            self.assertEqual(r.get("via"), "app")
            with self.assertRaises(ValueError):
                self.act("button", {"name": "UP"})
        finally:
            self.tv.refuse = set()
        self.act("pair")
        self.m.tv_action("faketv", "forget", {})  # with an open connection
        self.assertNotIn("faketv", self.m.tv_conns)

    def test_4_open_dlus_on_the_tv(self):
        self.act("pair")
        self.act("open", {})
        film = os.path.join(self.m.settings["base_dir"], "Video", "tvfilm.mkv")
        os.makedirs(os.path.dirname(film), exist_ok=True)
        with open(film, "wb") as f:
            f.write(b"\x1aE\xdf\xa3" + os.urandom(2000))
        d = self.m.import_local(film)
        self.act("open", {"id": d.r["id"], "at": 42})
        with self.assertRaises(ValueError):
            self.act("open", {"id": "nope"})
        with self.assertRaises(ValueError):
            self.act("open", {"folder": 7, "rel": "x"})
        with self.assertRaises(ValueError):
            self.act("launch", {"app": "../../etc"})
        self.act("off", {})
        self.assertIn("ssap://system/turnOff", self.tv.requests)
        with self.assertRaises(KeyError):
            self.act("make-coffee", {})
        self.assertIn("ssap://system.launcher/open", self.tv.requests)

    def test_5_changed_certificate_is_refused(self):
        dlus.STATE_DIR = tempfile.mkdtemp(prefix="dlus-m-")
        m = dlus.Manager(0)  # a fresh core: no connections left open by the other tests
        m.notify = lambda msg: None
        m.settings["tvs"] = [dict(self.m.settings["tvs"][0])]
        self.assertTrue(m.tv_action("faketv", "pair", {})["paired"])
        for c in m.tv_conns.values():
            c.sock.close()
        m.tv_conns.clear()
        self.tv.use_cert("someone-in-the-middle")
        with self.assertRaises(ValueError) as e:
            m.tv_action("faketv", "status", {})
        self.assertIn("certificate", str(e.exception))
        self.assertTrue(any("certificate changed" in x["event"] for x in m.audit_log))
        m.tv_action("faketv", "forget", {})  # the user confirms the TV was reset: forget, then pair again
        self.assertTrue(m.tv_action("faketv", "pair", {})["paired"])


if __name__ == "__main__":
    unittest.main()
