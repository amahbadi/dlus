# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""stop a deploy when the code uses a name that is defined nowhere (e.g. a helper lost while editing)"""
import ast, builtins, sys
src = open(sys.argv[1]).read()
tree = ast.parse(src)
known = set(dir(builtins)) | {"__file__", "__name__"}
for n in ast.walk(tree):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        known.add(n.name)
        for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs if not isinstance(n, ast.ClassDef) else []:
            known.add(a.arg)
        if not isinstance(n, ast.ClassDef):
            for a in (n.args.vararg, n.args.kwarg):
                if a:
                    known.add(a.arg)
    elif isinstance(n, ast.Lambda):
        for a in n.args.args:
            known.add(a.arg)
    elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
        known.add(n.id)
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            known.add((a.asname or a.name).split(".")[0])
    elif isinstance(n, ast.ExceptHandler) and n.name:
        known.add(n.name)
    elif isinstance(n, (ast.Global, ast.Nonlocal)):
        known.update(n.names)
missing = sorted({(n.id, n.lineno) for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known})
if missing:
    print("undefined names:", ", ".join("%s (line %d)" % m for m in missing))
    sys.exit(1)
print("names ok")
