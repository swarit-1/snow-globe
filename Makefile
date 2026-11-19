# Lookout: one entry point for tests, lint and the M0.5 worker commands (PRD §12; CLAUDE.md "Commands").
#
# Every target skips, with a message, any project whose folder does not exist yet.
# `make test` runs only checks that need no microphone, no credentials and no calls to external
# services (NPS, NOAA, AWS). First runs may still download dependencies (uv, pnpm, Terraform providers).
# Written for GNU Make 3.81 (the macOS default) and later.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PY_PROJECTS := workers/common workers/capture workers/forecast eval
PYTEST_ARGS ?= -q -m "not network"
UV ?= uv
CARGO ?= cargo
PNPM ?= pnpm
TERRAFORM ?= terraform
CLIPPY_FLAGS ?=
TF_DIR := infra/terraform
# Terraform's plugin socket path breaks on macOS when TMPDIR is long, and providers are cached outside
# the iCloud-synced repo (same convention as infra/test.sh).
TF_ENV = TMPDIR=/tmp TF_IN_AUTOMATION=1 CHECKPOINT_DISABLE=1 \
	TF_PLUGIN_CACHE_DIR="$${TF_PLUGIN_CACHE_DIR:-$${LOOKOUT_DATA_DIR:-$$HOME/lookout-data}/cache/terraform-plugins}"

# Worker commands. These call NPS or NOAA, so they are never part of `make test`.
# Override if a worker's CLI differs, e.g. `make capture-once CAPTURE_ONCE_CMD="uv run python -m x once"`.
CAPTURE_DIR := workers/capture
FORECAST_DIR := workers/forecast
CAPTURE_ONCE_CMD ?= $(UV) run lookout-capture once
ARCHIVE_CHECK_CMD ?= $(UV) run lookout-capture archive-check
FORECAST_LATEST_CMD ?= $(UV) run lookout-forecast latest

.PHONY: help test test-py viewpoints-check test-rust test-renderer test-infra lint lint-py \
	lint-rust lint-renderer lint-tf tf-validate capture-once forecast-latest archive-check

help: ## List the targets
	@echo "Lookout make targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  %-17s %s\n", $$1, $$2}'

test: test-py viewpoints-check test-rust test-renderer test-infra ## All offline checks: no network calls, mic or credentials

test-py: ## pytest -m "not network" in every uv project that exists
	@ran=0; for p in $(PY_PROJECTS); do \
		if [ -f "$$p/pyproject.toml" ]; then \
			echo "== $$p: pytest"; \
			( cd "$$p" && $(UV) run pytest $(PYTEST_ARGS) ) || { rc=$$?; \
				if [ $$rc -eq 5 ]; then echo "warning: $$p collected no tests"; else exit $$rc; fi; }; \
			ran=$$((ran + 1)); \
		else \
			echo "-- $$p: not created yet, skipped"; \
		fi; \
	done; echo "test-py: $$ran project(s) tested"

viewpoints-check: ## Validate viewpoints/manifests against viewpoints/schema (JSON Schema 2020-12)
	@if [ -f workers/common/pyproject.toml ] && [ -d viewpoints/manifests ]; then \
		echo "== viewpoints: schema check"; \
		cd workers/common && $(UV) run lookout-viewpoints-check ../../viewpoints/manifests; \
	else \
		echo "-- viewpoints or workers/common missing, skipped"; \
	fi

test-rust: ## cargo test --workspace in app/
	@if [ -f app/Cargo.toml ]; then \
		echo "== app: cargo test --workspace"; \
		cd app && $(CARGO) test --workspace; \
	else \
		echo "-- app: not created yet, skipped"; \
	fi

test-renderer: ## pnpm test in renderer/ (unit + headless Chromium/SwiftShader)
	@if [ -f renderer/package.json ]; then \
		echo "== renderer: pnpm test"; \
		cd renderer && $(PNPM) install --frozen-lockfile --prefer-offline && $(PNPM) test; \
	else \
		echo "-- renderer: not created yet, skipped"; \
	fi

test-infra: ## infra/test.sh if present (fmt, validate, mocked terraform test), else tf-validate
	@if [ -f infra/test.sh ]; then \
		echo "== infra: infra/test.sh"; \
		bash infra/test.sh; \
	elif [ -d $(TF_DIR) ]; then \
		$(MAKE) --no-print-directory tf-validate; \
	else \
		echo "-- infra: not created yet, skipped"; \
	fi

tf-validate: ## terraform fmt -check, init -backend=false, validate (no AWS credentials, never applies)
	@if [ -d $(TF_DIR) ]; then \
		echo "== $(TF_DIR): fmt, init -backend=false, validate"; \
		export $(TF_ENV); mkdir -p "$$TF_PLUGIN_CACHE_DIR" && \
		$(TERRAFORM) -chdir=$(TF_DIR) fmt -check -recursive && \
		$(TERRAFORM) -chdir=$(TF_DIR) init -backend=false -input=false && \
		$(TERRAFORM) -chdir=$(TF_DIR) validate; \
	else \
		echo "-- $(TF_DIR): not created yet, skipped"; \
	fi

lint: lint-py lint-rust lint-renderer lint-tf ## ruff, clippy, tsc and terraform fmt for whatever exists

lint-py: ## ruff check in every uv project that exists
	@for p in $(PY_PROJECTS); do \
		if [ -f "$$p/pyproject.toml" ]; then \
			echo "== $$p: ruff check"; \
			( cd "$$p" && $(UV) run ruff check . ) || exit $$?; \
		else \
			echo "-- $$p: not created yet, skipped"; \
		fi; \
	done

lint-rust: ## cargo clippy in app/ (set CLIPPY_FLAGS="-- -D warnings" to fail on warnings)
	@if [ -f app/Cargo.toml ]; then \
		echo "== app: cargo clippy"; \
		cd app && $(CARGO) clippy --workspace --all-targets $(CLIPPY_FLAGS); \
	else \
		echo "-- app: not created yet, skipped"; \
	fi

lint-renderer: ## pnpm typecheck in renderer/
	@if [ -f renderer/package.json ]; then \
		echo "== renderer: typecheck"; \
		cd renderer && $(PNPM) install --frozen-lockfile --prefer-offline && $(PNPM) run typecheck; \
	else \
		echo "-- renderer: not created yet, skipped"; \
	fi

lint-tf: ## terraform fmt -check -recursive
	@if [ -d infra ] && ls infra/terraform/*.tf >/dev/null 2>&1; then \
		echo "== infra: terraform fmt -check"; \
		$(TERRAFORM) -chdir=infra fmt -check -recursive; \
	else \
		echo "-- infra/terraform: not created yet, skipped"; \
	fi

capture-once: ## NETWORK: one polite live-capture cycle (never beside a running poller)
	@if [ -f $(CAPTURE_DIR)/pyproject.toml ]; then \
		cd $(CAPTURE_DIR) && $(CAPTURE_ONCE_CMD); \
	else \
		echo "-- $(CAPTURE_DIR): not created yet, skipped"; \
	fi

forecast-latest: ## NETWORK: extract the latest available HRRR run for every viewpoint
	@if [ -f $(FORECAST_DIR)/pyproject.toml ]; then \
		cd $(FORECAST_DIR) && $(FORECAST_LATEST_CMD); \
	else \
		echo "-- $(FORECAST_DIR): not created yet, skipped"; \
	fi

archive-check: ## NETWORK: NPS archive depth check (one request at a time, >= 5 s apart)
	@if [ -f $(CAPTURE_DIR)/pyproject.toml ]; then \
		cd $(CAPTURE_DIR) && $(ARCHIVE_CHECK_CMD); \
	else \
		echo "-- $(CAPTURE_DIR): not created yet, skipped"; \
	fi
