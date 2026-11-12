# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""A fake LG webOS TV for the tests: the SSAP WebSocket API over TLS on 127.0.0.1:3001, with a self-signed certificate."""
import json
import os
import socket
import ssl
import struct
import subprocess
import tempfile
import threading


def make_cert(cn):
    d = tempfile.mkdtemp(prefix="faketv-")
    key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "2", "-subj", "/CN=" + cn],
                   check=True, capture_output=True, timeout=60)
    return crt, key


class FakeTV:
    PORT = 3001

    def __init__(self):
        self.requests = []  # every uri dlus asked for
        self.texts = []  # pointer socket messages (buttons)
        self.key = "FAKE-CLIENT-KEY"
        self.refuse = set()  # uris answered with an error (like LG refusing remote apps some commands)
        self.use_cert("lgtv-1")
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.PORT))
        self.sock.listen(8)
        self.running = True
        threading.Thread(target=self.accept, daemon=True).start()

    def use_cert(self, cn):
        """a new certificate: as if the TV was reset - or someone is in the middle"""
        crt, key = make_cert(cn)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        self.ctx = ctx

    def stop(self):
        self.running = False
        self.sock.close()

    def accept(self):
        while self.running:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.client, args=(c,), daemon=True).start()

    # ------------------------------------------------------------ WebSocket
    @staticmethod
    def read_exact(s, n):
        b = b""
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise OSError("closed")
            b += c
        return b

    def read_frame(self, s):
        b0, b1 = self.read_exact(s, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.read_exact(s, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.read_exact(s, 8))[0]
        mask = self.read_exact(s, 4) if b1 & 0x80 else b"\0\0\0\0"
        data = self.read_exact(s, n)
        return b0 & 0x0F, bytes(x ^ mask[i % 4] for i, x in enumerate(data))

    @staticmethod
    def send(s, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
        s.sendall(head + data)

    def client(self, raw):
        try:
            s = self.ctx.wrap_socket(raw, server_side=True)
            req = b""
            while b"\r\n\r\n" not in req:
                req += s.recv(4096)
            path = req.split(b" ")[1].decode()
            s.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: x\r\n\r\n")
            while True:
                op, data = self.read_frame(s)
                if op == 8:
                    return
                if path.startswith("/pointer"):
                    self.texts.append(data.decode())
                    continue
                self.answer(s, json.loads(data))
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            raw.close()

    def answer(self, s, msg):
        if msg.get("type") == "register":
            self.send(s, {"type": "registered", "id": msg.get("id"), "payload": {"client-key": self.key}})
            return
        uri = msg.get("uri", "")
        self.requests.append(uri)
        if any(uri.endswith(x) for x in self.refuse):
            self.send(s, {"type": "error", "id": msg.get("id"), "error": "401 insufficient permissions"})
            return
        p = {"returnValue": True}
        if uri.endswith("getForegroundAppInfo"):
            p.update(appId="com.webos.app.livetv", windowId="")
        elif uri.endswith("getStatus") or uri.endswith("getVolume"):
            p.update(volume=12, muted=False, volumeStatus={"volume": 12, "muteStatus": False})
        elif uri.endswith("listLaunchPoints"):
            p.update(launchPoints=[{"id": "netflix", "title": "Netflix", "icon": "http://127.0.0.1:9/n.png"},
                                   {"id": "com.webos.app.browser", "title": "Web Browser", "icon": ""}])
        elif uri.endswith("getExternalInputList"):
            p.update(devices=[{"id": "HDMI_1", "label": "HDMI 1", "connected": True, "appId": "com.webos.app.hdmi1"}])
        elif uri.endswith("getPointerInputSocket"):
            p.update(socketPath="wss://127.0.0.1:%d/pointer" % self.PORT)
        elif uri.endswith("getSystemSettings"):
            p.update(settings={"pictureMode": "cinema", "eyeComfortMode": "off"})
        elif uri.endswith("getSystemSettingDesc"):
            p.update(results=[{"values": {"arrayExt": [{"value": "cinema", "visible": True}, {"value": "vivid", "visible": True}]}}])
        self.send(s, {"type": "response", "id": msg.get("id"), "payload": p})
