# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""syntax-check the JavaScript embedded in dlus.py (Mac page, TV player, shared subtitle code) with node --check"""
import ast, subprocess, sys, tempfile, os
s = open(sys.argv[1]).read()
a = s.index("SUBJS = ")
subjs = ast.literal_eval(s[a + 8:s.index("\n", a)])
bad = 0


def scripts(page):
    """the code between <script> and </script> in dlus' own page templates (no regex: plain search)"""
    out, i = [], 0
    while True:
        i = page.find("<script>", i)
        if i < 0:
            return out
        j = page.find("</script>", i)
        out.append(page[i + 8:j])
        i = j + 9
for name in ("PAGE", "TV_PAGE"):
    a = s.index('\n%s = r"""' % name) + len('\n%s = r"""' % name)
    b = s.index('"""', a)
    js = "\n".join(scripts(s[a:b])).replace("/*SUBJS*/", subjs).replace("__CFG__", "{}")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    os.unlink(f.name)
    print(name, "ok" if r.returncode == 0 else "FAILED\n" + r.stderr[:800])
    bad |= r.returncode
sys.exit(bad)
