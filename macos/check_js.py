"""syntax-check the JavaScript embedded in dlus.py (Mac page, TV player, shared subtitle code) with node --check"""
import re, subprocess, sys, tempfile, os
s = open(sys.argv[1]).read()
a = s.index("SUBJS = ")
subjs = eval(s[a + 8:s.index("\n", a)])
bad = 0
for name in ("PAGE", "TV_PAGE"):
    a = s.index('\n%s = r"""' % name) + len('\n%s = r"""' % name)
    b = s.index('"""', a)
    js = "\n".join(re.findall(r"<script>(.*?)</script>", s[a:b], re.S)).replace("/*SUBJS*/", subjs).replace("__CFG__", "{}")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    os.unlink(f.name)
    print(name, "ok" if r.returncode == 0 else "FAILED\n" + r.stderr[:800])
    bad |= r.returncode
sys.exit(bad)
