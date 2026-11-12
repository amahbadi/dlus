<h1 align="center">🤝 Contributing to dlus</h1>

<p align="center">Thanks for helping! Issues and pull requests are welcome.<br>Please follow the <a href="CODE_OF_CONDUCT.md">Code of Conduct</a>. How decisions are made: <a href="GOVERNANCE.md">GOVERNANCE.md</a>.</p>

> [!TIP]
> **Set up a development Mac in one step:** `make setup` (tools from the [Brewfile](Brewfile)), then `make test`, `make run`, `make dmg`. Design overview: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 🐞 Issues

Bugs and ideas: open an [issue](https://github.com/amahbadi/dlus/issues) - templates are included. New here? Start with an issue labelled **[good first issue](https://github.com/amahbadi/dlus/labels/good%20first%20issue)**.

> [!WARNING]
> Remove personal data (IP addresses, codes, file paths, device names) from logs and screenshots before you post them.

> [!CAUTION]
> **Security problems** are never reported in public issues - use **[Report a vulnerability](https://github.com/amahbadi/dlus/security/advisories/new)** instead. See [SECURITY.md](SECURITY.md).

## 🔀 Pull requests

```text
fork → branch → pull request → 🤖 checks comment on it → fix until ✅ → maintainer review → merge
```

1. **Fork** the repository and create a branch. **Sign off every commit** (`git commit -s`) - this is the [Developer Certificate of Origin](GOVERNANCE.md#-developer-certificate-of-origin-dco), checked on every pull request.
2. **Keep changes focused** and follow the code standard:
   - Python 3.9+, **standard library only** (no new dependencies), [PEP 8](https://peps.python.org/pep-0008/), lines up to 160 characters - checked by `ruff` ([pyproject.toml](pyproject.toml))
   - external programs only with argument lists (never `shell=True`), files only inside the folders dlus owns, input from the network always checked
   - shell scripts must pass `shellcheck -S warning`
   - Swift (the window, `macos/main.swift`) follows the [Swift API Design Guidelines](https://www.swift.org/documentation/api-design-guidelines/), formatted with `swift format` ([.swift-format](.swift-format))
3. **Run the checks** before you push - CI runs the same:
   ```sh
   python3 -m py_compile dlus.py
   python3 macos/check_names.py dlus.py
   python3 macos/check_js.py dlus.py
   python3 -m unittest discover -s tests          # security tests
   make lint                                       # coding style: ruff, swift format, ShellCheck
   swiftc -O -target arm64-apple-macos12.0 -o /tmp/dlus macos/main.swift
   ```
4. **Add tests**: new functionality, and every bug or security fix, comes with a test in [`tests/`](tests/) (standard `unittest`) - or a [fuzz target](fuzz/) for a new parser of outside input. Pull requests without tests for new behaviour are not merged.
5. **Update the docs** when behaviour changes (`README.md`, `docs/USER-GUIDE.md`, `SECURITY.md`).
6. **Never commit personal data**: IP addresses, keys, codes, hostnames, file paths, or screenshots that show them.

### 🤖 What happens on your pull request

| Check | Looks for |
|---|---|
| **CI** | compile, embedded JavaScript, [security tests](tests/test_security.py), firewall rules in macOS `pf`, Swift build |
| **Security** | Bandit, Semgrep, Gitleaks (secrets, whole history), ShellCheck, [fuzzing](fuzz/) |
| **CodeQL** | GitHub's code analysis |
| **DCO** | every commit signed off by its author |

- ❌ If something fails, a bot comment lists **what to fix** and the PR gets the label `security: fix needed`.
- ✅ When everything is green, the label changes to `ready for review` and the maintainer reviews it.

## 🔍 Code review

**How it is done:** every change to `main` is a pull request on GitHub. A reviewer reads the whole diff, runs it when it changes behaviour (`make test`, `make run`), and comments on the lines in GitHub. Questions and requested changes are answered in the pull request; the author pushes fixes to the same branch.

**What the reviewer checks**

| Area | Questions |
|---|---|
| ✅ Correctness | Does it do what the pull request says? Edge cases, errors, a download / stream / TV that is slow or offline? |
| 🧪 Tests | Is there a test for the new behaviour or the fixed bug ([test policy](#-pull-requests))? Do they fail without the change? |
| 🔒 Security | Input from the network, servers and files checked with an allow-list? No `shell=True`, no new root code without need, paths through `safe_join`, no secrets or personal data, nothing opened on the network by default? (see [SECURITY.md](SECURITY.md), [assurance case](docs/ASSURANCE-CASE.md)) |
| 🛡️ Root helper | Any change to `gateway/` keeps the fixed command set, checks every argument, and passes the `pf` selftest |
| 🧹 Code | Follows the [code standard](#-pull-requests); simple; no dead code; no new dependencies |
| 📖 Docs | README / user guide / SECURITY.md / changelog updated when behaviour changes |

**What is required to be accepted**

- every automatic check is green: CI, Security, CodeQL, DCO
- the required approvals (two for outside contributors, including the code owner) and all review threads resolved
- the branch is up to date with `main`, and every commit is signed off

> [!NOTE]
> Releases are published by the maintainer only. Changes touching the root helper (`gateway/`), the network servers or downloads get an extra careful review.
