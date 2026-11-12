"""Fuzz: no path from the network gets out of a shared folder or into hidden files."""
import os
import sys

import atheris

with atheris.instrument_imports():
    import _dlus
    dlus = _dlus.load()

BASE = "/srv/media"


def test(data):
    fdp = atheris.FuzzedDataProvider(data)
    rel = fdp.ConsumeUnicodeNoSurrogates(300).replace("\x00", "")
    full = os.path.normpath(os.path.join(BASE, rel))
    inside = full == BASE or full.startswith(BASE + os.sep)
    hidden = dlus.hidden_inside(BASE, full) if inside else None
    if inside and not hidden:
        assert not any(p.startswith(".") for p in os.path.relpath(full, BASE).split(os.sep) if p != "."), full
    dlus.share_ok("/" + rel.lstrip("/"))
    j = dlus.safe_join(BASE, rel)
    assert j is None or j == os.path.realpath(BASE) or j.startswith(os.path.realpath(BASE) + os.sep), j


atheris.Setup(sys.argv, test)
atheris.Fuzz()
