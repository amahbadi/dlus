"""Fuzz: file names from servers can never escape the download folder or hide their real type."""
import re
import sys

import atheris

with atheris.instrument_imports():
    import _dlus
    dlus = _dlus.load()

BAD = re.compile(r"[\x00-\x1f\x7f‎‏‪-‮⁦-⁩/\\:*?\"<>|]")


def test(data):
    fdp = atheris.FuzzedDataProvider(data)
    s = fdp.ConsumeUnicodeNoSurrogates(400)
    n = dlus.safe_name(s)
    assert n and len(n) <= 200, n
    assert not BAD.search(n), repr(n)
    assert not n.startswith("."), repr(n)
    assert n not in (".", "..")
    u = dlus.name_from_url("https://example.com/" + fdp.ConsumeUnicodeNoSurrogates(200))
    assert u and not BAD.search(u) and not u.startswith("."), repr(u)


atheris.Setup(sys.argv, test)
atheris.Fuzz()
