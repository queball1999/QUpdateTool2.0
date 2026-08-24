.PHONY: help venv recreate-venv install install-gui test test-pgp lint format check build build-gui brand clean

VENV_DIR := .venv

ifeq ($(OS),Windows_NT)
PYTHON := python
VENV_PYTHON := $(VENV_DIR)\Scripts\python.exe
BINARY := dist/updater.exe
else
PYTHON := python3
VENV_PYTHON := $(VENV_DIR)/bin/python3
BINARY := dist/updater
endif

# Override on the command line, e.g.
#   make brand BRAND=../QSnippet/updater-brand.yaml ICON=../QSnippet/assets/icons/QSnippet.ico
BRAND ?= examples/qsnippet-brand.yaml
ICON  ?=
KEY   ?=
OUT   ?=

help:
ifeq ($(OS),Windows_NT)
	@echo Available targets:
	@echo   make venv           - Create .venv and install dependencies
	@echo   make recreate-venv  - Delete and rebuild .venv from scratch
	@echo   make install        - Install runtime dependencies
	@echo   make install-gui    - Install runtime dependencies including PySide6
	@echo   make test           - Run the test suite
	@echo   make test-pgp       - Run the OpenPGP tests against real gpg keys
	@echo   make lint           - Run ruff
	@echo   make format         - Run ruff format
	@echo   make check          - Live --check-only run (set REPO=owner/name)
	@echo   make build          - Build an unbranded headless binary
	@echo   make build-gui      - Build an unbranded binary with GUI support
	@echo   make brand          - Build a branded binary (set BRAND=, ICON=, KEY=, OUT=)
	@echo   make clean          - Remove build artifacts
else
	@echo "Available targets:"
	@echo "  make venv           - Create .venv and install dependencies"
	@echo "  make recreate-venv  - Delete and rebuild .venv from scratch"
	@echo "  make install        - Install runtime dependencies"
	@echo "  make install-gui    - Install runtime dependencies including PySide6"
	@echo "  make test           - Run the test suite"
	@echo "  make test-pgp       - Run the OpenPGP tests against real gpg keys"
	@echo "  make lint           - Run ruff"
	@echo "  make format         - Run ruff format"
	@echo "  make check          - Live --check-only run (set REPO=owner/name)"
	@echo "  make build          - Build an unbranded headless binary"
	@echo "  make build-gui      - Build an unbranded binary with GUI support"
	@echo "  make brand          - Build a branded binary (set BRAND=, ICON=, KEY=, OUT=)"
	@echo "  make clean          - Remove build artifacts"
endif

venv:
ifeq ($(OS),Windows_NT)
	@if not exist "$(VENV_DIR)" $(PYTHON) -m venv $(VENV_DIR)
else
	@test -d "$(VENV_DIR)" || $(PYTHON) -m venv "$(VENV_DIR)"
endif
	@"$(VENV_PYTHON)" -m pip install --upgrade pip
	@"$(VENV_PYTHON)" -m pip install -r requirements-dev.txt

recreate-venv:
ifeq ($(OS),Windows_NT)
	@if exist "$(VENV_DIR)" rmdir /s /q "$(VENV_DIR)"
else
	@rm -rf "$(VENV_DIR)"
endif
	@$(MAKE) venv

install:
	$(PYTHON) -m pip install -r requirements.txt

install-gui:
	$(PYTHON) -m pip install -r requirements-gui.txt

test:
	$(PYTHON) -m pytest

test-pgp:
	$(PYTHON) -m pytest tests/test_openpgp.py -v

lint:
	$(PYTHON) -m ruff check qupdatetool tools tests

format:
	$(PYTHON) -m ruff format qupdatetool tools tests

# Live smoke test. Override REPO and KEY for your own project.
REPO ?= queball1999/QSnippet
check:
	$(PYTHON) -m qupdatetool --check-only --json \
		--provider github --repo $(REPO) \
		--app-name SmokeTest --current-version 0.0.0 \
		--log-file none

build:
	$(PYTHON) tools/build_branded.py --brand $(BRAND) --clean

build-gui:
	$(PYTHON) tools/build_branded.py --brand $(BRAND) --gui --clean

brand:
	$(PYTHON) tools/build_branded.py \
		--brand $(BRAND) \
		$(if $(ICON),--icon $(ICON),) \
		$(if $(KEY),--key $(KEY),) \
		$(if $(OUT),--out $(OUT),) \
		--clean

clean:
ifeq ($(OS),Windows_NT)
	@if exist build rmdir /s /q build
	@if exist dist rmdir /s /q dist
	@if exist qupdatetool\_brand_data.py del /q qupdatetool\_brand_data.py
	@for /d /r . %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
	@echo Cleaned build artifacts
else
	@rm -rf build dist
	@rm -f qupdatetool/_brand_data.py
	@find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned build artifacts"
endif
