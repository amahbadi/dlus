"""Fuzz: checksum text from users or servers never crashes and never yields a wrong-size hash."""
import sys

import atheris

with atheris.instrument_imports():
    import _dlus
    dlus = _dlus.load()


def test(data):
    fdp = atheris.FuzzedDataProvider(data)
    strict = fdp.ConsumeBool()
    name = fdp.ConsumeUnicodeNoSurrogates(40)
    text = fdp.ConsumeUnicodeNoSurrogates(4000)
    got = dlus.parse_checksum(text, (name,), strict=strict)
    if got is not None:
        algo, hx = got
        assert dlus.HASH_LEN[len(hx)] == algo, got
        assert all(c in "0123456789abcdef" for c in hx), got


atheris.Setup(sys.argv, test)
atheris.Fuzz()
