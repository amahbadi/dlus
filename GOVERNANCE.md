<h1 align="center">🏛️ Governance</h1>

<p align="center">How dlus is run: who decides, how changes get in, and what happens if the maintainer can no longer continue.</p>

## 👥 Roles

| Role | Who | Can do |
|---|---|---|
| **Lead maintainer** | [@amahbadi](https://github.com/amahbadi) (Ali Akbar Mahbadi) | merge pull requests, publish releases (tags `v*`), manage settings, security fixes, triage issues, grant roles |
| **Maintainer** | [@Mohammadi8mf](https://github.com/Mohammadi8mf) (Mohammad Mohammadi Fath) | review and approve pull requests, merge, triage issues, security fixes |
| **Successor** | [@Mohammadi8mf](https://github.com/Mohammadi8mf), named in the lead maintainer's GitHub account (GitHub's [account successor](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-access-to-your-personal-repositories/maintaining-ownership-continuity-of-your-personal-accounts-repositories)) | takes over the repository if the lead maintainer cannot continue (see [Continuity](#-continuity)) |
| **Contributor** | anyone | open issues, discuss, send pull requests (signed off, see [DCO](#-developer-certificate-of-origin-dco)) |
| **Reviewer** | contributors invited by the maintainer after sustained good contributions | review and approve pull requests |

Responsibilities of the maintainers: keep `main` releasable, answer security reports within the times in [SECURITY.md](SECURITY.md), review pull requests, keep the documentation current, and enforce the [Code of Conduct](CODE_OF_CONDUCT.md).

## ⚖️ How decisions are made

dlus follows a **maintainer-led** model with two maintainers:

1. Ideas and problems are discussed in public [issues](https://github.com/amahbadi/dlus/issues).
2. Changes are proposed as pull requests. Every pull request must pass all automatic checks (CI, Security, CodeQL) and get the required reviews before it can be merged ([CONTRIBUTING.md](CONTRIBUTING.md)).
3. The maintainers decide together; when they disagree, the lead maintainer makes the final decision and explains it in the issue or pull request. The direction for the coming year is in the [roadmap](docs/ROADMAP.md).
4. Security-relevant changes (root helper, network servers, downloads) need an explicit security note and extra review.

When the project grows to several regular reviewers, decisions will move to lazy consensus among them; this file will be updated first.

## 📜 Developer Certificate of Origin (DCO)

Every commit in a pull request must be **signed off**, stating that you wrote it or have the right to submit it under the project's MIT license, as defined by the [Developer Certificate of Origin 1.1](https://developercertificate.org/):

```sh
git commit -s -m "your message"     # adds:  Signed-off-by: Your Name <you@example.com>
```

The **DCO** check on every pull request verifies that each commit carries a `Signed-off-by` line matching its author ([dco.yml](.github/workflows/dco.yml)). Forgot it? `git rebase --signoff main` and push again.

## 🔄 Continuity

dlus must be able to go on if any one person cannot:

- **Second maintainer:** [@Mohammadi8mf](https://github.com/Mohammadi8mf) has write access and can triage, review, merge and fix at any time.
- **Successor:** the lead maintainer has named @Mohammadi8mf as successor in GitHub's account settings. On confirmation that the maintainer cannot continue, GitHub transfers the repository to the successor, who can then triage issues, merge pull requests and publish releases.
- **Releases need no personal secrets:** releases are built, signed (Sigstore) and published by GitHub Actions from a tag. There are no signing keys or tokens held by one person.
- **Everything is in the repository:** code, build scripts, tests, the security pipeline and all documentation; [`make setup`](Makefile) prepares a new developer's Mac.

## 🔁 Changing this document

Changes to governance are made by pull request like any other change and announced in the [changelog](CHANGELOG.md).
