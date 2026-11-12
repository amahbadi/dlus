# Contributing to dlus

Thanks for helping! Issues and pull requests are welcome.

## Issues

Bugs and ideas: open an [issue](https://github.com/amahbadi/dlus/issues) (templates included). Please remove personal data (IP addresses, codes, file paths) from logs before you post them.

## Pull requests

All changes go through a pull request and are reviewed by the maintainer; releases are published by the maintainer only.

1. Fork the repository and create a branch.
2. Keep changes focused; follow the style of the code around you (dlus is one Python file using only the standard library).
3. Run the checks before you push - CI runs the same:
   ```sh
   python3 -m py_compile dlus.py
   python3 macos/check_names.py dlus.py
   python3 macos/check_js.py dlus.py
   swiftc -O -target arm64-apple-macos12.0 -o /tmp/dlus macos/main.swift
   ```
4. Update the docs when behaviour changes (`README.md`, `docs/USER-GUIDE.md`).
5. Never commit personal data: IP addresses, keys, codes, hostnames, file paths, or screenshots that show them.

## Security

Please report security problems privately - see [SECURITY.md](SECURITY.md).
