<h1 align="center">🔁 Rebuild dlus yourself</h1>

<p align="center">Check that a release really comes from the published source: rebuild the app and compare it, file by file.</p>

dlus is reproducible: building `dlus.app` from the same source with the same toolchain gives **bit-for-bit the same files**. CI proves this on every change by building twice and comparing ([`macos/check_repeatable.sh`](../macos/check_repeatable.sh)).

Every release carries `dlus.app.sha256`: the SHA-256 of every file inside the app, plus the exact Swift compiler, macOS and SDK versions used.

## Steps

```sh
git clone https://github.com/amahbadi/dlus && cd dlus
git checkout v7.29                      # the release you want to check
swiftc --version                        # must match the "# Swift ..." line in dlus.app.sha256
bash macos/build_dmg.sh                 # builds dist/dlus.app
cd dist/dlus.app && find . -type f | LC_ALL=C sort | xargs shasum -a 256 > /tmp/mine.sha256
grep -v '^#' ~/Downloads/dlus.app.sha256 | diff - /tmp/mine.sha256 && echo "identical to the release"
```

> [!NOTE]
> The **app** is reproducible; the **DMG** around it is not bit-for-bit, because macOS writes the time it was made into every disk image. Compare the app (as above), or mount the release DMG and hash the `dlus.app` inside it the same way.

> [!TIP]
> The release is also signed: `gh attestation verify dlus-<version>.dmg --repo amahbadi/dlus` proves GitHub Actions built it from this repository ([SECURITY.md](../SECURITY.md#-check-a-download-of-dlus-yourself)).
