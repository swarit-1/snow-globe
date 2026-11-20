# Lookout: one entry point for tests, benchmarks, the backtest, scoring and the worker commands
# (PRD §12: one Make target each for test, bench, backtest and score; CLAUDE.md "Commands").
#
# `make help` lists every target with one line each.
#
# `make test` runs every suite in turn and prints a summary. It never calls NPS, NOAA, AWS or a
# paid service, and never opens a window, the microphone or a permission prompt:
#   - Python tests marked `network` are deselected, and pytest runs with every proxy variable
#     pointing at a closed local port (OFFLINE_ENV), so an unmarked test that tries a real
#     HTTP(S) call fails at once instead of reaching a real host;
#   - Terraform runs `init -backend=false`, `validate` and `terraform test` with mocked
#     providers, and never plans against AWS or applies;
#   - the renderer runs headless Chromium (SwiftShader), with every page request served locally.
# Network use: the first runs download dependencies (PyPI, crates.io, the GitHub oculus-core pin,
# the npm registry, Playwright's Chromium, the Terraform registry); none is a paid service. After
# that every suite runs offline except test-infra: infra/test.sh's `terraform init` asks
# registry.terraform.io for the provider's versions on every run, even with the lock file and a
# warm plugin cache, so offline it fails at once (measured 19 Nov under a no-network sandbox).
# Offline, leave it out: make test TEST_SUITES='common capture forecast eval renderer rust docs'
#
# Targets marked NETWORK in `make help` do contact NPS or NOAA. They are never part of `make test`.
# The NPS ones (capture-*, archive-check) must never run while another NPS fetcher runs, such as
# the lead's `lookout-capture loop`: every lookout-capture command except `status` takes a
# host-wide lock and exits with status 3 ("refused, another fetcher is active") before sending
# anything (CLAUDE.md "Polite collection", D-021, D-023).
#
# Written for GNU Make 3.81 (the macOS default) and bash 3.2.

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV ?= uv
CARGO ?= cargo
PNPM ?= pnpm
TERRAFORM ?= terraform
# The docs checks use the standard library only, so any python3 >= 3.9 runs them in isolated mode.
PYTHON ?= python3

# Rust build output stays out of the iCloud-synced repo (CLAUDE.md "Rust"). Exported, so every
# cargo call made from here (including maturin building oculus-core for eval) uses it.
export CARGO_TARGET_DIR ?= $(or $(LOOKOUT_DATA_DIR),$(HOME)/lookout-data)/cache/cargo-target

# iCloud sets the `hidden` flag on the venvs' .pth files and Python 3.13 skips hidden .pth files,
# so every Python command imports the sources through PYTHONPATH (CLAUDE.md "Python").
DIR_common := workers/common
DIR_capture := workers/capture
DIR_forecast := workers/forecast
DIR_eval := eval
PYPATH_common := src
PYPATH_capture := src:../common/src
PYPATH_forecast := src:../common/src
PYPATH_eval := src:../workers/common/src

PYTEST_ARGS ?= -q -m "not network"
# Clear it to run network tests on purpose, e.g.
#   make test-forecast PYTEST_ARGS='-m network -s' OFFLINE_ENV=
DEAD_PROXY := http://127.0.0.1:9
OFFLINE_ENV ?= HTTP_PROXY=$(DEAD_PROXY) HTTPS_PROXY=$(DEAD_PROXY) ALL_PROXY=$(DEAD_PROXY) \
	http_proxy=$(DEAD_PROXY) https_proxy=$(DEAD_PROXY) all_proxy=$(DEAD_PROXY) \
	NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1

# The suites `make test` runs, in order; each is `make test-<name>`. Override it to run fewer, for
# example offline without infra (see the header).
TEST_SUITES ?= common capture forecast eval renderer rust infra docs

# On battery this laptop idle-sleeps every few minutes (pmset log, 19 Nov), which froze test runs
# for 2-15 minutes and made their wall times meaningless. `caffeinate -i` holds off idle sleep
# only while the wrapped command runs; it is macOS only, so elsewhere commands run as they are.
KEEP_AWAKE := $(shell command -v caffeinate >/dev/null 2>&1 && echo caffeinate -i)

# PYTHONPATH can't reach a child started with `python -I` (some tests start one to hold a lock in
# another process), so on macOS the venv's .pth files are also un-hidden just before pytest runs.
# This changes only the file flag that iCloud sets; iCloud may set it again later.
UNHIDE_PTH = if [ "$$(uname -s)" = Darwin ]; then chflags nohidden .venv/lib/python3*/site-packages/*.pth 2>/dev/null || true; fi

# $(call py_suite,<name>[,<extra check>]): uv sync --locked, ruff check, pytest with network tests
# deselected, then the optional extra check, run in the suite's directory in the same shell. A
# failed sync stops the suite; any other failure still lets the later steps run, and the suite
# then fails, so one broken step never hides another.
define py_suite
	@echo "== $(DIR_$(1)): uv sync --locked, ruff check, pytest $(PYTEST_ARGS)"
	@cd $(DIR_$(1)) || exit 1; \
	$(UV) sync --locked || exit 1; \
	$(UNHIDE_PTH); \
	rc=0; \
	PYTHONPATH=$(PYPATH_$(1)) $(UV) run --no-sync ruff check . || rc=1; \
	$(OFFLINE_ENV) PYTHONPATH=$(PYPATH_$(1)) $(UV) run --no-sync pytest $(PYTEST_ARGS) || rc=1; \
	$(if $(2),$(2) || rc=1;) \
	exit $$rc
endef

# Run from workers/common: the viewpoint manifests against the JSON schema.
VIEWPOINTS_CHECK = echo "== viewpoints: manifests against the schema" && \
	PYTHONPATH=$(PYPATH_common) $(UV) run --no-sync lookout-viewpoints-check ../../viewpoints/manifests

# Worker commands (NETWORK unless the target says otherwise).
CAPTURE_RUN = cd $(DIR_capture) && PYTHONPATH=$(PYPATH_capture) $(UV) run --locked lookout-capture
CAPTURE_ARGS ?=
ARCHIVE_CHECK_ARGS ?=

# The light forecast profile: 7 fields, f00-f02, at most 40 MB of GRIB per run (about 21 MB
# measured for these fields and hours, workers/forecast/README.md). Each can be overridden from
# the environment or the command line. Without them the CLI refuses a whole run (exit 4).
LOOKOUT_FORECAST_FIELDS ?= VIS:surface,HGT:cloud ceiling,HGT:cloud base,LCDC:low cloud layer,MCDC:middle cloud layer,HCDC:high cloud layer,HGT:surface
LOOKOUT_FORECAST_MAX_FH ?= 2
LOOKOUT_FORECAST_MAX_MB ?= 40
FORECAST_PROFILE = LOOKOUT_FORECAST_FIELDS='$(LOOKOUT_FORECAST_FIELDS)' \
	LOOKOUT_FORECAST_MAX_FH='$(LOOKOUT_FORECAST_MAX_FH)' LOOKOUT_FORECAST_MAX_MB='$(LOOKOUT_FORECAST_MAX_MB)'
FORECAST_RUN = cd $(DIR_forecast) && $(FORECAST_PROFILE) PYTHONPATH=$(PYPATH_forecast) $(UV) run --locked
FORECAST_ARGS ?=

# The backtest pair (eval/README.md): the forecast group brings lookout-forecast for --forecast auto.
EVAL_RUN = cd $(DIR_eval) && PYTHONPATH=$(PYPATH_eval):../workers/forecast/src $(UV) run --locked
BACKTEST_ARGS ?=
EPHEM_ARGS ?=
BENCH_ROUNDS ?=

.PHONY: help test test-py test-common viewpoints-check test-capture test-forecast test-eval \
	test-renderer test-rust test-infra test-docs docs-check lint fmt-check \
	bench bench-parse bench-renderer backtest score ephem-check \
	capture-status capture-once capture-loop archive-check \
	forecast-status forecast-latest forecast-tick

##@ General

help: ## List every target with one line each
	@echo "Lookout make targets. NETWORK = contacts NPS or NOAA; never part of make test."
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@ / {printf "\n%s\n", substr($$0, 5); next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  %-17s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Tests (no calls to NPS, NOAA, AWS or paid services; no microphone, no credentials; test-infra needs registry.terraform.io)

test: ## Every suite in turn (kept awake on macOS), then a summary; non-zero exit if any failed (TEST_SUITES picks)
	@failed=""; report=""; t0=$$(date +%s); \
	for s in $(TEST_SUITES); do \
		start=$$(date +%s); \
		if $(KEEP_AWAKE) $(MAKE) --no-print-directory test-$$s; then st=passed; else st=FAILED; failed="$$failed $$s"; fi; \
		report="$$report$$(printf '\n  %-9s %-7s %5ss' "$$s" "$$st" "$$(( $$(date +%s) - start ))")"; \
	done; \
	printf '\n== make test summary (suite, result, wall time)%s\n  total %ss\n' "$$report" "$$(( $$(date +%s) - t0 ))"; \
	if [ -n "$$failed" ]; then echo "make test: FAILED:$$failed"; exit 1; fi; \
	echo "make test: all $(words $(TEST_SUITES)) suites passed"

test-common: ## workers/common: uv sync --locked, ruff check, pytest, then viewpoints-check (each runs)
	$(call py_suite,common,$(VIEWPOINTS_CHECK))

viewpoints-check: ## Validate viewpoints/manifests against viewpoints/schema (JSON Schema 2020-12)
	@cd $(DIR_common) && $(UV) sync --locked && $(VIEWPOINTS_CHECK)

test-capture: ## workers/capture: uv sync --locked, ruff check, pytest (network tests deselected)
	$(call py_suite,capture)

test-forecast: ## workers/forecast: uv sync --locked, ruff check, pytest (network tests deselected)
	$(call py_suite,forecast)

test-eval: ## eval: uv sync --locked (builds oculus-core), ruff check, pytest; Node 24 runs the renderer contract
	$(call py_suite,eval)

test-py: test-common test-capture test-forecast test-eval ## The four Python suites (stops at the first failure)

test-renderer: ## renderer: pnpm install --frozen-lockfile, then tsc + vitest (unit + headless SwiftShader)
	@echo "== renderer: pnpm install --frozen-lockfile, pnpm test"
	@cd renderer && $(PNPM) install --frozen-lockfile --prefer-offline && $(PNPM) test

test-rust: ## app: cargo test --workspace, clippy -D warnings, fmt --check (target dir outside iCloud)
	@echo "== app: cargo test, clippy -D warnings, fmt --check (CARGO_TARGET_DIR=$(CARGO_TARGET_DIR))"
	@cd app || exit 1; rc=0; \
	$(CARGO) test --workspace || rc=1; \
	$(CARGO) clippy --workspace --all-targets -- -D warnings || rc=1; \
	$(CARGO) fmt --all --check || rc=1; \
	exit $$rc

test-infra: ## infra/test.sh: terraform fmt, validate, mocked test, static and script checks; needs registry.terraform.io
	@echo "== infra: infra/test.sh"
	@bash infra/test.sh

docs-check: ## docs/checks: invariants of the planning docs (append-only DECISIONS, links, dates); stdlib only
	@echo "== docs/checks: $(PYTHON) -I -B -m unittest"
	@$(PYTHON) -I -B -m unittest discover -s docs/checks -t docs/checks -p 'test_*.py'

test-docs: docs-check ## Same as docs-check (the name `make test` uses)

##@ Benchmarks, backtest and scores

bench: bench-parse ## Benchmarks: the parser only until the M1 replay harness exists (TODO(M1), PRD §10.6)

bench-parse: ## Parse latency, PRD §10.1 (cargo run --release --example parse_bench [BENCH_ROUNDS])
	cd app && $(KEEP_AWAKE) $(CARGO) run --release --example parse_bench $(BENCH_ROUNDS)

bench-renderer: ## Renderer frame times, headless: SwiftShader and, if available, Chromium's GPU path (pnpm bench)
	cd renderer && $(PNPM) install --frozen-lockfile --prefer-offline && $(KEEP_AWAKE) $(PNPM) bench

backtest: ## One v-1 backtest pair (lookout-eval backtest-pair BACKTEST_ARGS); NETWORK (NOAA) with --forecast auto
ifeq ($(strip $(BACKTEST_ARGS)),)
	@echo "make backtest: set BACKTEST_ARGS to the lookout-eval backtest-pair options (eval/README.md). Required:" >&2
	@echo "  --viewpoint <id> --valid <ISO time> --lead <hours> --truth <archived frame>" >&2
	@echo "  --forecast <forecast/hrrr/... run JSON, or auto> --landmark-box x0,y0,x1,y1" >&2
	@echo "plus clear-day references (--reference or --references-file). Archived frames must be dated before" >&2
	@echo "1 Jun 2026, never from the sealed window and never from ~/lookout-data/quarantine/ (CLAUDE.md, D-019," >&2
	@echo "D-022). The record goes to \$$LOOKOUT_DATA_DIR/scores/pairs/. All options: make backtest BACKTEST_ARGS=--help" >&2
	@exit 2
else
	$(EVAL_RUN) --group forecast lookout-eval backtest-pair $(BACKTEST_ARGS)
endif

score: ## Score issued predictions (PRD §13.5): not built yet, TODO(M1.5); says so and exits 2
	@echo "make score: not built yet, so nothing was scored. TODO(M1.5): the daily scoring batch" >&2
	@echo "(PRD §11.2, §13.4-13.7) arrives with the walking skeleton. One pair today: make backtest" >&2
	@exit 2

ephem-check: ## Offline: oculus-core vs NPS SolarElevationAngle on a saved archive search (reads ~/lookout-data/samples)
	$(EVAL_RUN) lookout-eval ephem-check $(EPHEM_ARGS)

##@ Capture: NPS. Never part of make test; never run a NETWORK one while the lead's `lookout-capture loop` (or any NPS fetcher) runs: exit 3 = refused by the one-fetcher lock

capture-status: ## No network: persisted capture state per camera and today's event counts (lookout-capture status)
	$(CAPTURE_RUN) status

capture-once: ## NETWORK (nps.gov): one polite pass over the cameras that are due; exit 3 = another fetcher is active
	$(CAPTURE_RUN) once $(CAPTURE_ARGS)

capture-loop: ## NETWORK (nps.gov): a capture pass every 300 s plus jitter until Ctrl-C; exit 3 = refused by the lock
	$(CAPTURE_RUN) loop $(CAPTURE_ARGS)

archive-check: ## NETWORK (nps.gov archive): the M0.5 archive check, one request at a time >= 5 s apart; exit 3 = refused
	$(CAPTURE_RUN) archive-check $(ARCHIVE_CHECK_ARGS)

##@ Forecast: NOAA HRRR, light profile by default (LOOKOUT_FORECAST_FIELDS, _MAX_FH=2, _MAX_MB=40)

forecast-status: ## No network: forecast files written, the lag model and the cache size (lookout-forecast status --offline)
	$(FORECAST_RUN) lookout-forecast status --offline

forecast-latest: ## NETWORK (NOAA S3): extract the newest complete HRRR run with the light profile (about 21 MB)
	$(FORECAST_RUN) lookout-forecast latest $(FORECAST_ARGS)

# The same call the lead's local stand-in for the forecast Lambda makes every 5 minutes. The CLI has
# no `lookout-forecast tick` yet (checked 19 Nov); switch to it once it lands.
forecast-tick: ## NETWORK (NOAA S3): one call of the forecast Lambda handler with the light profile, locally
	$(FORECAST_RUN) python -c 'import json; from lookout_forecast.lambda_handler import handler; print(json.dumps(handler({}, None), indent=2, default=str))'

##@ Lint and format (no files are changed)

lint: ## ruff check, cargo clippy -D warnings, renderer tsc, terraform fmt -check, actionlint if installed
	@rc=0; \
	for d in $(DIR_common) $(DIR_capture) $(DIR_forecast) $(DIR_eval); do \
		echo "== $$d: ruff check"; (cd $$d && $(UV) run --locked ruff check .) || rc=1; \
	done; \
	echo "== app: cargo clippy -D warnings"; (cd app && $(CARGO) clippy --workspace --all-targets -- -D warnings) || rc=1; \
	echo "== renderer: tsc"; (cd renderer && $(PNPM) run typecheck) || rc=1; \
	echo "== infra: terraform fmt -check"; $(TERRAFORM) -chdir=infra/terraform fmt -check -recursive || rc=1; \
	if command -v actionlint >/dev/null 2>&1; then echo "== actionlint"; actionlint .github/workflows/ci.yml || rc=1; \
	else echo "-- actionlint not installed, skipped"; fi; \
	exit $$rc

fmt-check: ## ruff format --check, cargo fmt --check and terraform fmt -check
	@rc=0; \
	for d in $(DIR_common) $(DIR_capture) $(DIR_forecast) $(DIR_eval); do \
		echo "== $$d: ruff format --check"; (cd $$d && $(UV) run --locked ruff format --check .) || rc=1; \
	done; \
	echo "== app: cargo fmt --check"; (cd app && $(CARGO) fmt --all --check) || rc=1; \
	echo "== infra: terraform fmt -check"; $(TERRAFORM) -chdir=infra/terraform fmt -check -recursive || rc=1; \
	exit $$rc
