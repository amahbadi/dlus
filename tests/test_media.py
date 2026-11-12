# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""Media with ffmpeg: probing, thumbnails, converted streams, HLS for the TV, embedded subtitles, styles, auto sync.
Uses a short test video that the test makes itself; skipped when ffmpeg is not installed."""
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_media", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)
FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


SRT = "".join("%d\n00:%02d:%02d,000 --> 00:%02d:%02d,700\nLine %d\n\n" % (i + 1, (i + 1) // 60, (i + 1) % 60, (i + 1) // 60, (i + 1) % 60, i + 1)
              for i in range(50))  # 50 lines: enough for auto sync


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class Media(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = tempfile.mkdtemp(prefix="dlus-m-")
        dlus.STATE_DIR = home
        m = cls.m = dlus.Manager(0)
        m.notify = lambda msg: None
        m.settings["base_dir"] = os.path.join(home, "dl")
        m.settings["lan_port"] = free_port()
        work = tempfile.mkdtemp()
        srt = os.path.join(work, "s.srt")
        with open(srt, "w") as f:
            f.write(SRT)
        vid = os.path.join(m.settings["base_dir"], "Video", "Test.Film.2026.1080p.WEB.x264-GRP.mkv")
        os.makedirs(os.path.dirname(vid), exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=duration=60:size=320x180:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=300:duration=60", "-i", srt,
                        "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-ac", "2", "-c:s", "srt", "-metadata:s:s:0", "language=eng", vid], check=True, timeout=120)
        cls.d = m.import_local(vid)
        cls.port = free_port()
        m.port = cls.port
        cls.ui = dlus.http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), dlus.make_handler(m, cls.port))
        threading.Thread(target=cls.ui.serve_forever, daemon=True).start()
        cls.lan = dlus.LanServer(m, host="127.0.0.1", port=m.settings["lan_port"])
        cls.tok = m.settings["lan_token"]

    @classmethod
    def tearDownClass(cls):
        if cls.m.hls:
            for sid in list(cls.m.hls.sess):
                cls.m.hls.stop(sid)
        cls.ui.shutdown()
        cls.lan.stop()

    def get(self, path, lan=True, limit=None, headers=None, timeout=60):
        port = self.m.settings["lan_port"] if lan else self.port
        h = {"Host": "127.0.0.1:%d" % port}
        if not lan:
            h["Cookie"] = "dlus_k=" + self.m.ui_key
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path, headers=h)
        r = c.getresponse()
        data = r.read(limit) if limit else r.read()
        c.close()
        return r.status, data

    def test_probe(self):
        info = dlus.probe(self.m, self.d)
        self.assertEqual(info["video"], "h264")
        self.assertAlmostEqual(info["duration"], 60, delta=1)
        self.assertEqual(len(info["audio"]), 1)
        self.assertEqual(len(info["subs"]), 1)
        st, data = self.get("/api/probe/" + self.d.r["id"], lan=False)
        self.assertEqual(st, 200)

    def test_thumbnail(self):
        st, data = self.get("/%s/%d-thumb?t=3" % (self.tok, self.d.r["num"]))
        self.assertEqual(st, 200)
        self.assertEqual(data[:2], b"\xff\xd8")  # JPEG

    def test_converted_streams_start(self):
        num = self.d.r["num"]
        for path in ("/%s/%d.mp4?t=2" % (self.tok, num), "/%s/%ds.mkv" % (self.tok, num), "/%s/%dt.mkv" % (self.tok, num),
                     "/%s/%d.mp4?t=1&q=1080&snd=dialog" % (self.tok, num)):
            st, data = self.get(path, limit=4096)
            self.assertEqual(st, 200, path)
            self.assertGreater(len(data), 100, path)

    def test_hls_session(self):
        st, data = self.get("/%s/%d-hls?t=0" % (self.tok, self.d.r["num"]))
        self.assertEqual(st, 200, data)
        url = json.loads(data)["url"]
        end, pl = time.time() + 60, b""
        while time.time() < end:
            st, pl = self.get("/%s/%s" % (self.tok, url))
            if st == 200 and b".m4s" in pl:
                break
            time.sleep(0.5)
        self.assertIn(b"#EXTM3U", pl)
        seg = [x for x in pl.decode().splitlines() if x.endswith(".m4s")][0]
        base = url.rsplit("/", 1)[0]
        self.assertEqual(self.get("/%s/%s/init.mp4" % (self.tok, base))[0], 200)
        self.assertEqual(self.get("/%s/%s/%s" % (self.tok, base, seg))[0], 200)
        self.assertEqual(self.get("/%s/%s/../../x" % (self.tok, base))[0], 404)

    def test_embedded_subtitle_style_and_sync(self):
        info = self.m.subs_info(self.d)
        emb = [t for t in info["tracks"] if t["kind"] == "embedded"]
        self.assertTrue(emb, info)
        sid = emb[0]["sid"]
        path = None
        for _ in range(100):
            path = self.m.sub_path(self.d, sid)
            if path:
                break
            time.sleep(0.2)
        self.assertTrue(path and os.path.exists(path))
        ass, font = dlus.styled_ass(path, self.m.settings["sub_style"], 0.5)
        self.assertTrue(os.path.exists(ass))
        st, data = self.get("/%s/%d-subs.json" % (self.tok, self.d.r["num"]))
        self.assertEqual(st, 200)
        res = self.m.subs_action(self.d, "autosync", {"sid": sid})
        self.assertIsInstance(res, dict)
        for _ in range(150):  # the speech map is made in the background (a few seconds for 12 s of film)
            if self.m.speech_map(self.d) is not None:
                break
            time.sleep(0.2)
        self.m.subs_action(self.d, "autosync", {"sid": sid})

    def test_player_page_and_info(self):
        st, data = self.get("/%s/%d" % (self.tok, self.d.r["num"]), headers={"Accept": "text/html"})
        self.assertEqual(st, 200)
        st, data = self.get("/%s/%d.json" % (self.tok, self.d.r["num"]))
        self.assertEqual(json.loads(data)["status"], "done")
        self.assertEqual(self.get("/%s/%d-native" % (self.tok, self.d.r["num"]))[0], 404)  # only for a TV dlus knows
        self.assertEqual(self.get("/%s/%d-picture" % (self.tok, self.d.r["num"]))[0], 200)


if __name__ == "__main__":
    unittest.main()
