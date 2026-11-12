# dlus developer tasks.  make setup  ->  make test  ->  make run
PYTHON ?= python3

.PHONY: setup test lint run dmg install coverage clean

setup:            ## install the tools for development and tests (Homebrew)
	brew bundle --file Brewfile
	xcode-select -p >/dev/null || xcode-select --install

test:             ## every check CI runs (except the macOS pf one, which needs sudo)
	$(PYTHON) -m py_compile dlus.py
	$(PYTHON) macos/check_names.py dlus.py
	$(PYTHON) macos/check_js.py dlus.py
	$(PYTHON) -m unittest discover -s tests -v
	shellcheck -S warning gateway/dlus-gw gateway/*.sh macos/*.sh macos/uninstall.command

lint:             ## the coding standard: ruff (pyproject.toml), swift format (.swift-format), ShellCheck
	ruff check .
	swift format lint --strict macos/main.swift
	shellcheck -S warning gateway/dlus-gw gateway/*.sh macos/*.sh macos/uninstall.command

coverage:         ## test coverage of dlus.py
	$(PYTHON) -m coverage run --include=dlus.py -m unittest discover -s tests && $(PYTHON) -m coverage report

run:              ## run dlus from the source folder (http://127.0.0.1:8642/)
	$(PYTHON) dlus.py

dmg:              ## build dist/dlus.app and dist/dlus-<version>.dmg
	bash macos/build_dmg.sh

install:          ## build and install into /Applications (development)
	bash macos/deploy_local.sh

clean:
	rm -rf dist work .coverage
