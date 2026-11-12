# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""Subtitle sync on real (generated) films: against the film's own subtitle track, a corrected copy for another cut,
and the HLS pacing that keeps ffmpeg just ahead of the TV. Skipped without ffmpeg."""
import importlib.util
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DLM_HOME", tempfile.mkdtemp(prefix="dlus-test-"))
sys.argv = ["dlus"]
spec = importlib.util.spec_from_file_location("dlus_syncm", os.path.join(ROOT, "dlus.py"))
dlus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dlus)
FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def manager():
    dlus.STATE_DIR = tempfile.mkdtemp(prefix="dlus-m-")
    m = dlus.Manager(0)
    m.notify = lambda msg: None
    m.settings["base_dir"] = os.path.join(dlus.STATE_DIR, "dl")
    return m


def srt(times, length=0.8):
    def ts(x):
        ms = round(x * 1000)
        return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
    return "".join("%d\n%s --> %s\nLine %d\n\n" % (i + 1, ts(t), ts(t + length), i + 1) for i, t in enumerate(times))


def lines(seed, end, start=4.0):
    rnd, out, t = random.Random(seed), [], start
    while t < end:
        out.append(round(t, 2))
        t += rnd.uniform(1.3, 3.2)
    return out


def tone_wav(speech, dur, rate=8000):
    """a WAV file with a 700 Hz tone wherever the film "speaks" (made in Python: any number of lines)"""
    import array
    import math
    import wave
    on = bytearray(int(dur * rate))
    for x in speech:
        a, b = int(x * rate), min(len(on), int((x + 0.8) * rate))
        on[a:b] = b"\x01" * max(0, b - a)
    tone = [int(12000 * math.sin(2 * math.pi * 700 * i / rate)) for i in range(rate)]
    pcm = array.array("h", (tone[i % rate] if on[i] else 0 for i in range(len(on))))
    p = os.path.join(tempfile.mkdtemp(), "speech.wav")
    with wave.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return p


def film(m, name, speech, dur, embedded=None):
    vid = os.path.join(m.settings["base_dir"], "Video", name)
    os.makedirs(os.path.dirname(vid), exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x90:d=%d:r=10" % dur, "-i", tone_wav(speech, dur)]
    if embedded:
        p = os.path.join(tempfile.mkdtemp(), "e.srt")
        with open(p, "w") as f:
            f.write(embedded)
        cmd += ["-i", p, "-map", "0", "-map", "1", "-map", "2", "-c:s", "srt"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "2", vid]
    subprocess.run(cmd, check=True, timeout=300)
    return m.import_local(vid)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class SyncOnFilms(unittest.TestCase):
    def test_new_subtitle_is_timed_against_the_films_own_track(self):
        m = manager()
        ref = lines(5, 110)
        d = film(m, "With.Own.Track.mkv", ref, 120, embedded=srt(ref))
        m.port = free_port()
        ui = dlus.http.server.ThreadingHTTPServer(("127.0.0.1", m.port), dlus.make_handler(m, m.port))
        __import__("threading").Thread(target=ui.serve_forever, daemon=True).start()
        try:
            new = m.subs_add(d, "late.srt", srt([t - 1.5 for t in ref]).encode())[0]  # shows 1.5 s too early
            m.subs_action(d, "set", {"sid": new["sid"]})
            m.auto_sync(d, new["sid"])
            t = next(x for x in d.r["subs"] if x["sid"] == new["sid"])
            self.assertAlmostEqual(t["sync"]["offset"], 1.5, delta=0.1)
            self.assertGreater(t["sync"]["match"], 80)
        finally:
            ui.shutdown()

    def test_corrected_copy_for_another_cut(self):
        m = manager()
        subs = lines(6, 290)
        mid = subs[len(subs) // 2]
        d = film(m, "Directors.Cut.mkv", [t + 1.0 if t < mid else t + 9.0 for t in subs], 310)
        sid = m.subs_add(d, "cinema-cut.srt", srt(subs).encode())[0]["sid"]
        end, res = time.time() + 240, {}
        while time.time() < end:
            res = m.subs_action(d, "autosync", {"sid": sid}) or {}
            if res.get("verdict"):
                break
            time.sleep(1)
        self.assertEqual(res.get("verdict"), "sections", res)
        self.assertTrue(res["applied"])
        t = next(x for x in d.r["subs"] if x["sid"] == sid)
        self.assertEqual(t.get("fixed"), "sections")
        self.assertTrue(os.path.exists(t["file"] + ".orig"))  # the original is kept
        fixed = dlus.speech_sync(dlus.speech_starts(t["file"]), m.speech_map(d))  # the corrected copy itself fits the film
        self.assertEqual(fixed["verdict"], "fits")
        self.assertAlmostEqual(fixed["offset"], 0, delta=0.2)
        before = open(t["file"]).read()
        self.assertEqual(m.subs_action(d, "autosync", {"sid": sid}).get("verdict"), "sections")  # always fitted from the original:
        self.assertEqual(open(t["file"]).read(), before)  # running it again changes nothing


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class HLSPacing(unittest.TestCase):
    def test_pause_resume_and_idle_stop(self):
        m = manager()
        d = film(m, "Long.mkv", [], 1200)  # 20 minutes: the conversion takes long enough to be paced
        m.port = free_port()
        ui = dlus.http.server.ThreadingHTTPServer(("127.0.0.1", m.port), dlus.make_handler(m, m.port))
        __import__("threading").Thread(target=ui.serve_forever, daemon=True).start()
        try:
            m.hls = dlus.HLS(m)
            m.hls.AHEAD, m.hls.KEEP = 2, 0  # tiny limits: ffmpeg must be paused and resumed while the test watches
            sid = m.hls.start(d, "127.0.0.1", 0, 0, 0, None, "high")
            x = m.hls.sess[sid]
            end = time.time() + 60
            while time.time() < end and not x["stopped"]:
                time.sleep(0.5)
            self.assertTrue(x["stopped"], "ffmpeg was not paused when far ahead")
            x["want"] = 999
            end = time.time() + 30
            while time.time() < end and x["stopped"]:
                time.sleep(0.5)
            self.assertFalse(x["stopped"], "ffmpeg was not resumed")
            x["seen"] = time.time() - m.hls.IDLE - 5
            end = time.time() + 15
            while time.time() < end and sid in m.hls.sess:
                time.sleep(0.5)
            self.assertNotIn(sid, m.hls.sess)  # idle: stopped and cleaned up
        finally:
            m.hls.shutdown()
            ui.shutdown()


if __name__ == "__main__":
    unittest.main()
