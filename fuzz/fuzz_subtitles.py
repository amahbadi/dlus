# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""Fuzz: subtitle files and release names from the internet never crash the parsers."""
import sys

import atheris

with atheris.instrument_imports():
    import _dlus
    dlus = _dlus.load()


def test(data):
    fdp = atheris.FuzzedDataProvider(data)
    raw = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 8000))
    text = dlus.decode_sub(raw)
    assert isinstance(text, str)
    vtt = dlus.to_vtt(text, "x.srt")  # the SRT path is pure Python (no ffmpeg)
    assert vtt.startswith("WEBVTT")
    dlus.guess_lang(fdp.ConsumeUnicodeNoSurrogates(80), text)
    a, b = fdp.ConsumeUnicodeNoSurrogates(200), fdp.ConsumeUnicodeNoSurrogates(200)
    score = dlus.release_score(a, b)
    assert 0 <= score <= 100, score


atheris.Setup(sys.argv, test)
atheris.Fuzz()
