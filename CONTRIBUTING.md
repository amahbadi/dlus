<h1 align="center">🤝 Contributing to dlus</h1>

<p align="center">Thanks for helping! Issues and pull requests are welcome.</p>

## 🐞 Issues

Bugs and ideas: open an [issue](https://github.com/amahbadi/dlus/issues) - templates are included.

> [!WARNING]
> Remove personal data (IP addresses, codes, file paths, device names) from logs and screenshots before you post them.

> [!CAUTION]
> **Security problems** are never reported in public issues - use **[Report a vulnerability](https://github.com/amahbadi/dlus/security/advisories/new)** instead. See [SECURITY.md](SECURITY.md).

## 🔀 Pull requests

```text
fork → branch → pull request → 🤖 checks comment on it → fix until ✅ → maintainer review → merge
```

1. **Fork** the repository and create a branch.
2. **Keep changes focused**; follow the style of the code around you (dlus is one Python file using only the standard library).
3. **Run the checks** before you push - CI runs the same:
   ```sh
   python3 -m py_compile dlus.py
   python3 macos/check_names.py dlus.py
   python3 macos/check_js.py dlus.py
   python3 -m unittest discover -s tests          # security tests
   swiftc -O -target arm64-apple-macos12.0 -o /tmp/dlus macos/main.swift
   ```
4. **Update the docs** when behaviour changes (`README.md`, `docs/USER-GUIDE.md`, `SECURITY.md`).
5. **Never commit personal data**: IP addresses, keys, codes, hostnames, file paths, or screenshots that show them.

### 🤖 What happens on your pull request

| Check | Looks for |
|---|---|
| **CI** | compile, embedded JavaScript, [security tests](tests/test_security.py), firewall rules in macOS `pf`, Swift build |
| **Security** | Bandit, Semgrep, Gitleaks (secrets, whole history), ShellCheck, [fuzzing](fuzz/) |
| **CodeQL** | GitHub's code analysis |

- ❌ If something fails, a bot comment lists **what to fix** and the PR gets the label `security: fix needed`.
- ✅ When everything is green, the label changes to `ready for review` and the maintainer reviews it.

> [!NOTE]
> Releases are published by the maintainer only. Changes touching the root helper (`gateway/`), the network servers or downloads get an extra careful review.
