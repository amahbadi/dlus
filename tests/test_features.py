# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""Subtitle search and sync, fonts, settings and download actions."""
import base64
import importlib.util
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_feat", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)
FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def manager():
    dlus.STATE_DIR = tempfile.mkdtemp(prefix="dlus-m-")
    m = dlus.Manager(0)
    m.notify = lambda msg: None
    m.settings["base_dir"] = os.path.join(dlus.STATE_DIR, "dl")
    return m


def local_file(m, name, data):
    p = os.path.join(m.settings["base_dir"], name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return m.import_local(p)


def srt(times, length=0.8):
    def ts(x):
        ms = round(x * 1000)
        return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
    return "".join("%d\n%s --> %s\nLine %d\n\n" % (i + 1, ts(t), ts(t + length), i + 1) for i, t in enumerate(times))


SEARCH = '<a href="/subtitles/the-test-film">The Test Film (2026)</a><a href="/subtitles/other">Other (1999)</a>'
LIST = ("<li class='item'><a href='/subtitles/the-test-film/farsi_persian/111'>x</a><ul><li>The.Test.Film.2026.2160p.BluRay.x265-GRP</li></ul>"
        "<a href=\"/u/ali\"> Ali </a><p>Synced for <b>2160p</b></p><span class='rate positive'></span></li>"
        "<li class='item'><a href='/subtitles/the-test-film/farsi_persian/222'>x</a><ul><li>The.Test.Film.2026.720p.WEB</li></ul></li>")


class Subtitles(unittest.TestCase):
    def setUp(self):
        self.m = manager()
        self.d = local_file(self.m, "Video/The.Test.Film.2026.2160p.BluRay.x265-GRP.mkv", b"\x1aE\xdf\xa3" + os.urandom(5000))
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as z:
            z.writestr("The.Test.Film.fa.srt", srt([1, 3, 5]).encode("cp1256", "ignore"))
            z.writestr("__MACOSX/._x.srt", b"junk")
        os_results = [{"SubDownloadLink": "https://dl.opensubtitles.org/x/1", "SubFormat": "srt", "MovieReleaseName": "The.Test.Film.2026.1080p.WEB",
                       "SubFileName": "The.Test.Film.srt", "IDSubtitleFile": 9, "SubRating": "9", "SubDownloadsCnt": 5}]
        pages = {"searchbytitle": SEARCH.encode(), "/subtitles/the-test-film/farsi_persian/111": b"<a href='/subtitles/the-test-film/download/111'>d</a>",
                 "/download/": zbuf.getvalue(), "/subtitles/the-test-film/farsi_persian": LIST.encode(),
                 "rest.opensubtitles.org": json.dumps(os_results).encode(), "dl.opensubtitles.org": srt([2, 4, 6]).encode()}

        def web(url, timeout=25, limit=4 << 20, referer=None, headers=None):
            for k, v in pages.items():
                if k in url:
                    return v
            raise OSError("not in the test pages: " + url)
        self.m.web = web

    def test_search_ranks_the_exact_release_first(self):
        r = self.m.subs_action(self.d, "search", {"lang": "fa"})
        self.assertGreaterEqual(len(r["results"]), 3)
        self.assertEqual(r["results"][0]["id"], "111")
        self.assertEqual(r["results"][0]["score"], 100)
        self.assertTrue(r["results"][0]["best"])

    def test_get_from_both_sites(self):
        self.assertTrue(self.m.subs_action(self.d, "get", {"path": "/subtitles/the-test-film/farsi_persian/111", "id": "111"})["added"])
        self.assertTrue(self.m.subs_action(self.d, "get", {"path": "https://dl.opensubtitles.org/x/1", "id": "os9", "name": "a.srt"})["added"])
        info = self.m.subs_info(self.d)
        self.assertGreaterEqual(len(info["tracks"]), 2)
        # the same subtitle again is recognised
        self.m.subs_action(self.d, "get", {"path": "/subtitles/the-test-film/farsi_persian/111", "id": "111"})
        self.assertEqual(len(self.m.subs_info(self.d)["tracks"]), len(info["tracks"]))

    def test_subtitle_formats(self):
        for name, raw in (("a.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"), ("b.srt", b"\xef\xbb\xbf" + srt([1, 2]).encode()),
                          ("c.srt", "﻿".encode("utf-16-le") + srt([3]).encode("utf-16-le"))):
            self.assertTrue(self.m.subs_add(self.d, name, raw))
        with self.assertRaises(ValueError):
            self.m.subs_add(self.d, "x.zip", b"PK\x05\x06" + b"\0" * 18)
        self.assertEqual(dlus.guess_lang("film.fa.srt", ""), "fa")
        self.assertEqual(dlus.guess_lang("x.srt", "سلام گچپژ"), "fa")
        self.assertEqual(dlus.guess_lang("x.srt", "hello"), "en")


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class AutoSync(unittest.TestCase):
    def test_finds_a_two_second_shift(self):
        m = manager()
        rnd = random.Random(7)
        times, t = [], 3.0
        while t < 80:
            times.append(round(t, 2))
            t += rnd.uniform(1.3, 3.5)
        speech = [x + 2.0 for x in times]  # the film speaks 2.0 s after each subtitle line
        expr = "+".join("between(t,%.2f,%.2f)" % (s, s + 0.8) for s in speech)
        vid = os.path.join(m.settings["base_dir"], "Video", "Sync.Test.mkv")
        os.makedirs(os.path.dirname(vid), exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x90:d=90:r=10",
                        "-f", "lavfi", "-i", "aevalsrc='(%s)*sin(2*PI*700*t)':s=16000:d=90" % expr,
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "2", vid], check=True, timeout=180)
        d = m.import_local(vid)
        m.port = 0
        sid = m.subs_add(d, "sync.srt", srt(times).encode())[0]["sid"]
        end, res = time.time() + 180, None
        while time.time() < end:
            res = m.subs_action(d, "autosync", {"sid": sid})
            if res and res.get("state") not in ("running", "pending", "listening") and not res.get("pending"):
                break
            time.sleep(1)
        delay = float(d.r.get("sub_delay") or 0)
        self.assertAlmostEqual(delay, 2.0, delta=0.3, msg=res)


class Fonts(unittest.TestCase):
    def test_install_upload_and_remove(self):
        m = manager()
        dlus.STATE_DIR = dlus.STATE_DIR
        fake = b"\x00\x01\x00\x00" + os.urandom(3000)

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        real = dlus.urllib.request.urlopen
        dlus.urllib.request.urlopen = lambda req, timeout=30: R(b"wOF2" + os.urandom(3000) if "woff2" in str(getattr(req, "full_url", req)) else fake)
        try:
            dlus.fetch_fonts()
            name = next(n for n, f in dlus.FONT_CATALOG.items() if not f.get("default"))
            m.fonts_set({"install": name})
            self.assertTrue(dlus.font_installed(name))
            m.fonts_set({"remove": name})
            m.fonts_set({"upload": {"name": "My Font", "filename": "my.ttf", "b64": base64.b64encode(fake).decode()}})
            self.assertTrue(any(x["name"] == "My Font" for x in dlus.user_fonts(m)))
            with self.assertRaises(ValueError):
                m.fonts_set({"upload": {"name": "Bad", "filename": "x.ttf", "b64": base64.b64encode(b"not a font").decode()}})
            self.assertIn("font-face", dlus.font_css(m))
            dlus.fonts_js(m)
        finally:
            dlus.urllib.request.urlopen = real


class Settings(unittest.TestCase):
    def test_every_setting(self):
        m = manager()
        tok = m.settings["lan_token"]
        m.set_settings({"sched_on": True, "sched_from": "01:00", "sched_to": "02:00", "limit_mb": 2, "watch_limit_mb": 1, "conns": 6,
                        "max_active": 2, "on_done": "none", "dlna_quality": "1080", "notify": False, "tv_audio": "dialog",
                        "sub_style": {"size": 6, "color": "#ffee00", "bg": "box", "pos": 5, "bold": False, "font": "Vazirmatn"},
                        "rotate_token": True, "proxy_newpass": True, "proxy_upstream": "direct", "gw_names": {"aa:bb:cc:dd:ee:ff": "PS5"}})
        self.assertNotEqual(m.settings["lan_token"], tok)
        m.tick()
        self.assertTrue(m.sched_wait is False or m.sched_wait is True)
        m.set_settings({"sched_on": False, "limit_mb": 0})
        for bad in ({"share_dirs": ["/"]}, {"share_dirs": [os.path.expanduser("~")]}, {"share_dirs": ["/no/such/folder"]}):
            with self.assertRaises(ValueError):
                m.set_settings(bad)
        self.assertEqual(dlus.in_window("00:00", "23:59"), True)
        m.conn_limit()


class Actions(unittest.TestCase):
    def test_move_and_actions(self):
        m = manager()
        ids = [local_file(m, "Video/f%d.mkv" % i, b"\x1aE\xdf\xa3" + os.urandom(1000)).r["id"] for i in range(3)]
        for where in ("top", "up", "down", "bottom"):
            m.move(ids[1], where)
        for act in ("pause", "resume", "start"):
            m.action(ids[0], act, {})
        st = m.state()
        self.assertEqual(len(st["items"]), 3)
        m.action(ids[2], "delete", {})
        self.assertNotIn(ids[2], m.dls)
        m.shutdown()


if __name__ == "__main__":
    unittest.main()
