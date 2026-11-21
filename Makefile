# Snow Globe: one entry point for tests, benchmarks, the backtest, scoring and the worker commands
# (PRD §12: one Make target each for test, bench, backtest and score; CLAUDE.md "Commands").
#
# `make help` lists every target with one line each.
#
# `make test` runs every suite in turn and prints a summary. It never calls NPS, NOAA, AWS or a
# paid service, and never opens a window, the microphone or a permission prompt:
#   - every Python suite runs `ruff check`, `ruff format --check` and pytest. Tests marked
#     `network` are deselected, and pytest runs with every proxy variable pointing at a closed
#     local port (OFFLINE_ENV), so an unmarked test that tries a real HTTP(S) call fails at once
#     instead of reaching a real host;
#   - Rust runs cargo test, clippy -D warnings and `cargo fmt --check`;
#   - Terraform runs `fmt -check`, `init -backend=false`, `validate` and `terraform test` with
#     mocked providers, and never plans against AWS or applies;
#   - the renderer runs tsc, then vitest on headless Chromium (SwiftShader), with every page
#     request served locally. It has no formatter configured, so there is no format check there;
#   - app/speech-macos runs `swift test` (macOS only). Its tests put a trap behind the recognizer
#     for the whole test process, so they never reach the Speech framework, the microphone or a
#     permission prompt. Then `scripts/build.sh --no-test` builds the release archive and runs the
#     signing check, the symbol deny list and reviewed baseline, the checker's mutation self-test
#     and the C-host link check, none of which calls the recognizer either (D-075);
#   - docs/checks runs the docs invariants with the standard library, then ruff on its own code.
# CI (.github/workflows/ci.yml) runs the same `make test-<suite>` targets, except the suites in
# LAPTOP_ONLY_SUITES; docs/checks fails if the two lists drift apart (its `ci-suites` check).
# Network use: the first runs download dependencies (PyPI, crates.io, the GitHub oculus-core pin,
# the npm registry, Playwright's Chromium, the Terraform registry); none is a paid service. After
# that every suite runs offline, test-infra included. infra/tf_providers.sh installs the AWS
# provider from the plugin cache with `terraform init -plugin-dir` once the cache holds the locked
# provider for this platform with a matching h1: hash. It falls back to the registry only when
# the cache can't serve (TF_INIT=auto, the default; CI's linux_amd64 always falls back, since the
# lock files hold darwin_arm64 hashes only). TF_INIT=offline never contacts the registry and fails
# at once if the cache can't serve; `make infra-providers` says whether it can, reading files only.
# Measured 20 Nov: with the dependencies cached, the whole of `make test` (all 10 suites, 223 s)
# passed under a macOS sandbox (sandbox-exec) that denied every outbound IP connection except to
# loopback, and DNS lookups too (the mDNSResponder socket). So no suite needs the network once its
# dependencies are cached. It does not prove that none tries: the sandbox's denials were not
# logged, and other local IPC (other Unix sockets, Mach services) stayed open.
#
# Targets marked NETWORK in `make help` do contact NPS or NOAA. They are never part of `make test`.
# The NPS ones (capture-*, archive-check) must never run while another NPS fetcher runs, such as
# the lead's `snow-globe-capture loop`: every snow-globe-capture command except `status` takes a
# host-wide lock and exits with status 3 ("refused, another fetcher is active") before sending
# anything (CLAUDE.md "Polite collection", D-021, D-023). The NOAA ones (forecast-latest,
# forecast-tick) must never run while the lead's local forecast stand-in runs: until the AWS jobs
# are live it is the only writer of forecast/hrrr/ and forecast_log/ (D-030), so they refuse
# while it runs (FORECAST_GUARD below).
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

# The data dir, resolved as the workers resolve it (workers/common's paths.data_dir, D-080):
# $SNOW_GLOBE_DATA_DIR, else the deprecated $LOOKOUT_DATA_DIR (blank counts as unset), else
# ~/snow-globe-data if it exists, else ~/lookout-data if it exists (the folder's name before the
# rename), else ~/snow-globe-data. Symlinks are followed (`test -e`; $(wildcard) would also match
# a dangling one) and nothing is created. While both folders exist and are different folders
# (something made ~/snow-globe-data before the rename's live switch), every target except `help`
# stops with an error, as the workers refuse then. The build caches below live under it.
SG_DATA_DIR_NEW := $(HOME)/snow-globe-data
SG_DATA_DIR_OLD := $(HOME)/lookout-data
SG_DATA_DIR := $(or $(strip $(SNOW_GLOBE_DATA_DIR)),$(strip $(LOOKOUT_DATA_DIR)),$(shell \
	new='$(SG_DATA_DIR_NEW)' old='$(SG_DATA_DIR_OLD)'; \
	if [ -e "$$new" ] && [ -e "$$old" ] && ! [ "$$new" -ef "$$old" ]; then echo AMBIGUOUS; \
	elif [ -e "$$new" ] || [ ! -e "$$old" ]; then echo "$$new"; else echo "$$old"; fi))
ifeq ($(SG_DATA_DIR),AMBIGUOUS)
ifneq ($(filter-out help,$(or $(MAKECMDGOALS),help)),)
$(error both $(SG_DATA_DIR_NEW) and $(SG_DATA_DIR_OLD) exist and are different folders, so the data dir is ambiguous (D-080): until the rename's live switch the data is in $(SG_DATA_DIR_OLD), and after it $(SG_DATA_DIR_OLD) is a symlink to $(SG_DATA_DIR_NEW). Set SNOW_GLOBE_DATA_DIR to the folder you mean, and tell the lead which one is stray)
endif
endif

# Rust build output stays out of the iCloud-synced repo (CLAUDE.md "Rust"). Exported, so every
# cargo call made from here (including maturin building oculus-core for eval) uses it.
export CARGO_TARGET_DIR ?= $(SG_DATA_DIR)/cache/cargo-target

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
# Its -m replaces a project's own -m in addopts. So capture's `slow` schedule tests (3 days each of
# its four schedules on a fake clock), which its pyproject deselects, run in make test and in CI on
# purpose: they guard the schedule behaviour behind the polite rule (D-065, D-075).
# Clear it to run network tests on purpose, e.g.
#   make test-forecast PYTEST_ARGS='-m network -s' OFFLINE_ENV=
DEAD_PROXY := http://127.0.0.1:9
OFFLINE_ENV ?= HTTP_PROXY=$(DEAD_PROXY) HTTPS_PROXY=$(DEAD_PROXY) ALL_PROXY=$(DEAD_PROXY) \
	http_proxy=$(DEAD_PROXY) https_proxy=$(DEAD_PROXY) all_proxy=$(DEAD_PROXY) \
	NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1

# The suites `make test` runs, in order; each is `make test-<name>`. Override it to run fewer,
# e.g. make test TEST_SUITES='docs bench'. CI runs each as its own `make test-<name>`, except the
# LAPTOP_ONLY_SUITES: speech needs macOS 26 and Xcode 26.2, and CI has no macOS job (ci.yml).
TEST_SUITES ?= common capture forecast eval renderer rust speech infra docs bench
LAPTOP_ONLY_SUITES := speech

# How infra/test.sh installs the Terraform provider (SNOW_GLOBE_TF_INIT, infra/tf_providers.sh): auto
# (offline from the plugin cache when it matches the lock file, else the registry), offline (never
# the registry; fails at once if the cache can't serve) or registry (always the registry).
TF_INIT ?= auto

# On battery this laptop idle-sleeps every few minutes (pmset log, 19 Nov), which froze test runs
# for 2-15 minutes and made their wall times meaningless. `caffeinate -i` holds off idle sleep
# only while the wrapped command runs; it is macOS only, so elsewhere commands run as they are.
KEEP_AWAKE := $(shell command -v caffeinate >/dev/null 2>&1 && echo caffeinate -i)

# PYTHONPATH can't reach a child started with `python -I` (some tests start one to hold a lock in
# another process), so on macOS the venv's .pth files are also un-hidden just before pytest runs.
# This changes only the file flag that iCloud sets; iCloud may set it again later.
UNHIDE_PTH = if [ "$$(uname -s)" = Darwin ]; then chflags nohidden .venv/lib/python3*/site-packages/*.pth 2>/dev/null || true; fi

# $(call py_suite,<name>[,<extra check>]): uv sync --locked, ruff check, ruff format --check,
# pytest with network tests deselected, then the optional extra check, run in the suite's
# directory in the same shell. Each project's own locked ruff runs, so the format check follows
# that project's pin. A failed sync stops the suite; any other failure still lets the later steps
# run, and the suite then fails, so one broken step never hides another.
define py_suite
	@echo "== $(DIR_$(1)): uv sync --locked, ruff check, ruff format --check, pytest $(PYTEST_ARGS)"
	@cd $(DIR_$(1)) || exit 1; \
	$(UV) sync --locked || exit 1; \
	$(UNHIDE_PTH); \
	rc=0; \
	PYTHONPATH=$(PYPATH_$(1)) $(UV) run --no-sync ruff check . || rc=1; \
	$(UV) run --no-sync ruff format --check . || rc=1; \
	$(OFFLINE_ENV) PYTHONPATH=$(PYPATH_$(1)) $(UV) run --no-sync pytest $(PYTEST_ARGS) || rc=1; \
	$(if $(2),$(2) || rc=1;) \
	exit $$rc
endef

# app/speech-macos (Swift 6.2, macOS 26): `swift test`, with the build output outside the
# iCloud-synced repo, where the package's README puts it, then `scripts/build.sh --no-test` (D-075;
# about 10 s on top of an existing build). build.sh always builds under <data dir>/cache/speech-macos
# (the data dir resolved as SG_DATA_DIR is, by build.sh itself), whatever SPEECH_SCRATCH says, and
# deletes the temporary folders it makes under $TMPDIR. Both steps run, and either failing fails
# the suite. Off macOS, or without the Xcode tools (checked with `xcode-select -p`, because the
# /usr/bin/swift shim would offer to install them in a window), the suite prints SKIP and passes.
SWIFT ?= swift
SPEECH_SCRATCH ?= $(SG_DATA_DIR)/cache/speech-macos
# Extra `swift test` options, such as --filter (none by default).
SWIFT_TEST_ARGS ?=

# The docs checks' own code is linted and format-checked with workers/common's locked ruff, which
# reads docs/checks/ruff.toml (Python 3.9 rules, since the checks run on any python3 >= 3.9).
DOCS_CHECKS_DIR := ../../docs/checks
DOCS_CHECK = $(PYTHON) -I -B -m unittest discover -s docs/checks -t docs/checks -p 'test_*.py'

# bench/ is standard-library Python with no project of its own (bench/README.md). It runs on
# workers/common's interpreter (uv's Python 3.13; check.py needs 3.11+) and that project's locked
# ruff, which reads bench/ruff.toml. --no-cache keeps a .ruff_cache out of the iCloud-synced tree.
BENCH_DIR := ../../bench
BENCH_CHECKS = rc=0; \
	$(UV) run --no-sync python -I -B -m unittest discover -s $(BENCH_DIR)/utterances \
		-t $(BENCH_DIR)/utterances -p 'test_*.py' || rc=1; \
	$(UV) run --no-sync python -I -B $(BENCH_DIR)/utterances/check.py --no-parser || rc=1; \
	$(UV) run --no-sync ruff check --no-cache $(BENCH_DIR) || rc=1; \
	$(UV) run --no-sync ruff format --check --no-cache $(BENCH_DIR) || rc=1; \
	exit $$rc

# Run from workers/common: the viewpoint manifests against the JSON schema.
VIEWPOINTS_CHECK = echo "== viewpoints: manifests against the schema" && \
	PYTHONPATH=$(PYPATH_common) $(UV) run --no-sync snow-globe-viewpoints-check ../../viewpoints/manifests

# Worker commands (NETWORK unless the target says otherwise).
CAPTURE_RUN = cd $(DIR_capture) && PYTHONPATH=$(PYPATH_capture) $(UV) run --locked snow-globe-capture
CAPTURE_ARGS ?=
ARCHIVE_CHECK_ARGS ?=

# The light forecast profile, the same as the lead's local stand-in for the forecast Lambda
# (<data dir>/cache/runtime/bin/forecast_loop.sh, D-030) since 02:39 UTC on 20 Nov: 8 fields
# (the 7 of 19 Nov plus PRES:cloud base, D-026), f00-f02, at most 40 MB of GRIB per run. Measured:
# 23.8-25.0 MB of GRIB for each of the 19 runs the stand-in extracted with it on 20 Nov (its
# forecast_log run events, up to 08:57 UTC). Keep it the stand-in's: with fewer fields, a run that
# a target here extracts first is extracted again by the stand-in; with more, the target extracts
# again what the stand-in wrote. Either way that run is downloaded twice and its run and lag events
# are logged twice (workers/forecast/README.md, "Field-set changes"). Each value can be overridden
# from the environment or the command line, under the SNOW_GLOBE_ name or, as before the rename,
# the LOOKOUT_ one (D-080). Without them the CLI refuses a whole run (exit 4).
FORECAST_FIELDS_DEFAULT := VIS:surface,HGT:cloud ceiling,HGT:cloud base,PRES:cloud base,LCDC:low cloud layer,MCDC:middle cloud layer,HCDC:high cloud layer,HGT:surface
SNOW_GLOBE_FORECAST_FIELDS ?= $(if $(LOOKOUT_FORECAST_FIELDS),$(LOOKOUT_FORECAST_FIELDS),$(FORECAST_FIELDS_DEFAULT))
SNOW_GLOBE_FORECAST_MAX_FH ?= $(or $(LOOKOUT_FORECAST_MAX_FH),2)
SNOW_GLOBE_FORECAST_MAX_MB ?= $(or $(LOOKOUT_FORECAST_MAX_MB),40)
FORECAST_PROFILE = SNOW_GLOBE_FORECAST_FIELDS='$(SNOW_GLOBE_FORECAST_FIELDS)' \
	SNOW_GLOBE_FORECAST_MAX_FH='$(SNOW_GLOBE_FORECAST_MAX_FH)' SNOW_GLOBE_FORECAST_MAX_MB='$(SNOW_GLOBE_FORECAST_MAX_MB)'
FORECAST_RUN = cd $(DIR_forecast) && $(FORECAST_PROFILE) PYTHONPATH=$(PYPATH_forecast) $(UV) run --locked
FORECAST_ARGS ?=
# `snow-globe-forecast tick` options: --json, or --uncapped (refused by default without a per-run cap).
FORECAST_TICK_ARGS ?=
# Until the AWS jobs are live, the lead's local stand-in (D-030) is the only writer of forecast/hrrr/
# and forecast_log/. forecast-latest and forecast-tick write both too (files, run and lag events),
# so they refuse (Error 5), before running anything, while a process whose command line matches
# FORECAST_STANDIN_RE runs (pgrep -f). This guards against that one known job; it is not a lock.
# The brackets keep the pattern from matching the recipe's own shell, so keep one in any
# replacement; FORECAST_STANDIN_RE= skips the check (once the stand-in is retired). `make backtest`
# with --forecast auto also appends a run event to forecast_log (eval's pair_forecast.py,
# TODO(M1.5) there) and has no such guard.
FORECAST_STANDIN_RE ?= cache/runtime/bin/[f]orecast_loop[.]sh
FORECAST_GUARD = if [ -n '$(FORECAST_STANDIN_RE)' ] && pgrep -f '$(FORECAST_STANDIN_RE)' >/dev/null 2>&1; then \
	echo "make $@: refused, nothing was run: the lead's local forecast stand-in is running" >&2; \
	echo "  (pgrep -f '$(FORECAST_STANDIN_RE)'). Until the AWS jobs are live it is the only writer of" >&2; \
	echo "  forecast/hrrr/ and forecast_log/ (D-030). FORECAST_STANDIN_RE= skips this check." >&2; \
	exit 5; fi

# The backtest pair (eval/README.md): the forecast group brings snow-globe-forecast for --forecast auto.
EVAL_RUN = cd $(DIR_eval) && PYTHONPATH=$(PYPATH_eval):../workers/forecast/src $(UV) run --locked
BACKTEST_ARGS ?=
EPHEM_ARGS ?=
# `snow-globe-eval data-report` options: --since/--until ISO times, --json [PATH], the gap thresholds.
DATA_REPORT_ARGS ?=
BENCH_ROUNDS ?=

.PHONY: help test test-py test-common viewpoints-check test-capture test-forecast test-eval \
	test-renderer test-rust test-speech test-infra infra-providers test-docs docs-check test-bench bench-script \
	lint fmt-check \
	bench bench-parse bench-renderer backtest score ephem-check data-report \
	capture-status capture-once capture-loop archive-check \
	forecast-status forecast-latest forecast-tick

##@ General

help: ## List every target with one line each
	@echo "Snow Globe make targets. NETWORK = contacts NPS or NOAA; never part of make test."
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@ / {printf "\n%s\n", substr($$0, 5); next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  %-17s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Tests (no calls to NPS, NOAA, AWS or paid services; no microphone, no credentials; offline once dependencies are cached)

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

test-common: ## workers/common: uv sync --locked, ruff check + format --check, pytest, viewpoints-check (each runs)
	$(call py_suite,common,$(VIEWPOINTS_CHECK))

viewpoints-check: ## Validate viewpoints/manifests against viewpoints/schema (JSON Schema 2020-12)
	@cd $(DIR_common) && $(UV) sync --locked && $(VIEWPOINTS_CHECK)

test-capture: ## workers/capture: uv sync --locked, ruff check + format --check, pytest (network tests deselected)
	$(call py_suite,capture)

test-forecast: ## workers/forecast: uv sync --locked, ruff check + format --check, pytest (network tests deselected)
	$(call py_suite,forecast)

test-eval: ## eval: uv sync --locked (builds oculus-core), ruff check + format --check, pytest; Node 24 for the contract
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

test-speech: ## app/speech-macos: swift test, then scripts/build.sh --no-test (symbols, signing, link); macOS + Xcode, else SKIP
	@if [ "$$(uname -s)" != Darwin ] || ! xcode-select -p >/dev/null 2>&1; then \
		echo "-- app/speech-macos: SKIP (needs macOS with the Xcode tools)"; exit 0; fi; \
	cd app/speech-macos || exit 1; rc=0; \
	echo "== app/speech-macos: swift test --scratch-path $(SPEECH_SCRATCH) $(SWIFT_TEST_ARGS)"; \
	$(SWIFT) test --scratch-path "$(SPEECH_SCRATCH)" $(SWIFT_TEST_ARGS) || rc=1; \
	echo "== app/speech-macos: scripts/build.sh --no-test (release build, signing, symbol baseline and self-test, link check)"; \
	scripts/build.sh --no-test || rc=1; \
	exit $$rc

# infra/tests/test_cost.sh runs ruff on infra/cost with whichever `ruff` comes first on PATH, and
# skips that step when there is none. So test-infra puts a directory first on PATH that holds only
# a symlink to workers/common's locked ruff (refreshed on every run, outside the repo): the laptop
# and CI then judge infra/cost with the pinned ruff, as `make lint` and `make fmt-check` do, and
# nothing else on PATH changes (python3 stays the same).
RUFF_ONLY_DIR ?= $(SG_DATA_DIR)/cache/make-tools/ruff-only

test-infra: ## infra/test.sh: terraform fmt, validate, mocked test, static and script checks; TF_INIT=auto|offline|registry
	@echo "== infra: infra/test.sh (SNOW_GLOBE_TF_INIT=$(TF_INIT); ruff of $(DIR_common))"
	@(cd $(DIR_common) && $(UV) sync --locked) || exit 1; \
	mkdir -p "$(RUFF_ONLY_DIR)" && ln -sfn "$(CURDIR)/$(DIR_common)/.venv/bin/ruff" "$(RUFF_ONLY_DIR)/ruff" || exit 1; \
	PATH="$(RUFF_ONLY_DIR):$$PATH" SNOW_GLOBE_TF_INIT='$(TF_INIT)' bash infra/test.sh

infra-providers: ## No network: can test-infra's terraform init run offline here? (infra/tf_providers.sh status)
	@bash infra/tf_providers.sh status

docs-check: ## docs/checks: docs invariants (append-only DECISIONS, links, dates, notices, CI suites); stdlib only
	@echo "== docs/checks: $(PYTHON) -I -B -m unittest"
	@$(DOCS_CHECK)

test-docs: ## docs-check, then ruff check + format --check of docs/checks (workers/common's ruff); each step runs
	@echo "== docs: docs/checks with $(PYTHON) -I -B -m unittest, then ruff check, ruff format --check (ruff of $(DIR_common))"
	@rc=0; $(DOCS_CHECK) || rc=1; \
	cd $(DIR_common) || exit 1; \
	$(UV) sync --locked || exit 1; \
	$(UV) run --no-sync ruff check --no-cache $(DOCS_CHECKS_DIR) || rc=1; \
	$(UV) run --no-sync ruff format --check --no-cache $(DOCS_CHECKS_DIR) || rc=1; \
	exit $$rc

test-bench: ## bench/: utterance-script tests, check.py --no-parser, ruff check + format --check (stdlib, no audio)
	@echo "== bench: unittest, check.py --no-parser, ruff check, ruff format --check (Python and ruff of workers/common)"
	@cd $(DIR_common) || exit 1; \
	$(UV) sync --locked || exit 1; \
	$(BENCH_CHECKS)

# The whole script check against the real parser (bench/README.md, D-075): build snow-globe-parse in
# release mode, offline and locked, into the target dir outside iCloud, then run check.py on that
# binary (--bin, so $SNOW_GLOBE_PARSE can't point it elsewhere). check.py needs Python 3.11 or later,
# so it runs on workers/common's interpreter (uv's 3.13), as test-bench does. Exit 1: the parser's
# output has drifted from scripted.tsv, or a stored verdict is stale; exit 2: the binary is missing
# or older than its sources. Not part of make test or CI: CI's bench job sets up no Rust toolchain
# (the rust job is separate), and --offline needs the crates cached. Run it after any change to
# app/core's parser or time rules, to the manifests, or to the script.
BENCH_PARSE_TARGET_DIR = $(abspath $(CARGO_TARGET_DIR))

bench-script: ## Offline: build snow-globe-parse (cargo --release --offline --locked), then check.py with the real parser
	@echo "== app: cargo build --release --offline --locked -p snow-globe-core --bin snow-globe-parse (CARGO_TARGET_DIR=$(BENCH_PARSE_TARGET_DIR))"
	@cd app && CARGO_TARGET_DIR="$(BENCH_PARSE_TARGET_DIR)" $(CARGO) build --release --offline --locked -p snow-globe-core --bin snow-globe-parse
	@echo "== bench: check.py --bin $(BENCH_PARSE_TARGET_DIR)/release/snow-globe-parse (Python of $(DIR_common))"
	@cd $(DIR_common) || exit 1; \
	$(UV) sync --locked || exit 1; \
	$(UV) run --no-sync python -I -B $(BENCH_DIR)/utterances/check.py --bin "$(BENCH_PARSE_TARGET_DIR)/release/snow-globe-parse"

##@ Benchmarks, backtest and scores

bench: bench-parse ## Benchmarks: the parser only until the M1 replay harness exists (TODO(M1), PRD §10.6)

bench-parse: ## Parse latency, PRD §10.1 (cargo run --release --example parse_bench [BENCH_ROUNDS])
	cd app && $(KEEP_AWAKE) $(CARGO) run --release --example parse_bench $(BENCH_ROUNDS)

bench-renderer: ## Renderer frame times, headless: SwiftShader and, if available, Chromium's GPU path (pnpm bench)
	cd renderer && $(PNPM) install --frozen-lockfile --prefer-offline && $(KEEP_AWAKE) $(PNPM) bench

backtest: ## One v-1 backtest pair (snow-globe-eval backtest-pair); render to <data dir>/renders/pairs/; NETWORK with --forecast auto
ifeq ($(strip $(BACKTEST_ARGS)),)
	@echo "make backtest: set BACKTEST_ARGS to the snow-globe-eval backtest-pair options (eval/README.md). Required:" >&2
	@echo "  --viewpoint <id> --valid <ISO time> --lead <hours> --truth <archived frame>" >&2
	@echo "  --forecast <forecast/hrrr/... run JSON, or auto (NOAA, network)>" >&2
	@echo "  --boxes v-1 (the committed by-eye landmark boxes), or one box: --landmark-box x0,y0,x1,y1" >&2
	@echo "plus clear-day references (--reference or --references-file). Archived frames must be dated before" >&2
	@echo "1 Jun 2026, never from the sealed window and never from <data dir>/quarantine/ (CLAUDE.md, D-019," >&2
	@echo "D-022, D-038); the pair refuses them, and 'snow-globe-eval heldout --check FRAME' vets one beforehand." >&2
	@echo "The record and scene go to <data dir>/scores/pairs/<viewpoint>/, the render, its metadata and the" >&2
	@echo "renderer log to <data dir>/renders/pairs/<viewpoint>/. The data dir here is $(SG_DATA_DIR)" >&2
	@echo "(~/lookout-data until the rename's live switch, D-080). All options: make backtest BACKTEST_ARGS=--help" >&2
	@exit 2
else
	$(EVAL_RUN) --group forecast snow-globe-eval backtest-pair $(BACKTEST_ARGS)
endif

score: ## Score issued predictions (PRD §13.5): not built yet, TODO(M1.5); says so and exits 2
	@echo "make score: not built yet, so nothing was scored. TODO(M1.5): the daily scoring batch" >&2
	@echo "(PRD §11.2, §13.4-13.7) arrives with the walking skeleton. One pair today: make backtest" >&2
	@exit 2

ephem-check: ## Offline: oculus-core vs NPS SolarElevationAngle on a saved archive search (reads <data dir>/samples)
	$(EVAL_RUN) snow-globe-eval ephem-check $(EPHEM_ARGS)

# Reads the real logs, so it is not part of make test (its unit tests are, in test-eval). It never
# opens an image, archive/ or quarantine/, and writes a file only with --json PATH.
data-report: ## Offline, read-only: M0.5 gate 1's 72 h log evidence per viewpoint (snow-globe-eval data-report DATA_REPORT_ARGS)
	$(EVAL_RUN) snow-globe-eval data-report $(DATA_REPORT_ARGS)

##@ Capture: NPS. Never part of make test; never run a NETWORK one while the lead's `snow-globe-capture loop` (or any NPS fetcher) runs: exit 3 = refused by the one-fetcher lock

capture-status: ## No network: persisted capture state per camera and today's event counts (snow-globe-capture status)
	$(CAPTURE_RUN) status

capture-once: ## NETWORK (nps.gov): one polite pass over the cameras that are due; exit 3 = another fetcher is active
	$(CAPTURE_RUN) once $(CAPTURE_ARGS)

capture-loop: ## NETWORK (nps.gov): a capture pass every 300 s plus jitter until Ctrl-C; exit 3 = refused by the lock
	$(CAPTURE_RUN) loop $(CAPTURE_ARGS)

archive-check: ## NETWORK (nps.gov archive): the M0.5 archive check, one request at a time >= 5 s apart; exit 3 = refused
	$(CAPTURE_RUN) archive-check $(ARCHIVE_CHECK_ARGS)

##@ Forecast: NOAA HRRR, the stand-in's light profile (8 fields, f00-f02, at most 40 MB a run). NETWORK ones refuse while the lead's local forecast stand-in runs, the only forecast_log writer until AWS is live (D-030): Error 5

forecast-status: ## No network: forecast files written, the lag model and the cache size (snow-globe-forecast status --offline)
	$(FORECAST_RUN) snow-globe-forecast status --offline

# `snow-globe-forecast latest`: extracts the newest complete run and appends its run and lag events to
# forecast_log, so it refuses while the stand-in runs (FORECAST_GUARD).
forecast-latest: ## NETWORK (NOAA S3): extract the newest complete HRRR run, light profile (about 24 MB); refused while the stand-in runs
	@$(FORECAST_GUARD)
	$(FORECAST_RUN) snow-globe-forecast latest $(FORECAST_ARGS)

# `snow-globe-forecast tick`: one call of the handler the scheduled Lambda runs, which the lead's local
# stand-in also calls every 5 minutes. It writes forecast/hrrr/ files and forecast_log/ events as
# the stand-in does, so it never runs beside it: it refuses while the stand-in runs
# (FORECAST_GUARD). The light profile's SNOW_GLOBE_FORECAST_MAX_MB caps each run; without a cap, tick
# refuses (exit 1) unless FORECAST_TICK_ARGS=--uncapped. Exit 1 if a run failed in the call, 4 if
# every failure was a cap refusal.
forecast-tick: ## NETWORK (NOAA S3): snow-globe-forecast tick, one call of the forecast Lambda handler; refused while the stand-in runs
	@$(FORECAST_GUARD)
	$(FORECAST_RUN) snow-globe-forecast tick $(FORECAST_TICK_ARGS)

##@ Lint and format (no files are changed)

# bench/, infra/cost/ and docs/checks/ have no project of their own: workers/common's locked ruff
# checks them, each with its own ruff.toml (--no-cache: nothing written into the iCloud-synced tree).
RUFF_EXTRA_DIRS := bench infra/cost docs/checks

lint: ## ruff check (+ bench/, infra/cost/, docs/checks/), cargo clippy -D warnings, renderer tsc, terraform fmt -check, actionlint
	@rc=0; \
	for d in $(DIR_common) $(DIR_capture) $(DIR_forecast) $(DIR_eval); do \
		echo "== $$d: ruff check"; (cd $$d && $(UV) run --locked ruff check .) || rc=1; \
	done; \
	for d in $(RUFF_EXTRA_DIRS); do \
		echo "== $$d: ruff check ($(DIR_common)'s ruff)"; \
		(cd $(DIR_common) && $(UV) run --locked ruff check --no-cache "$(CURDIR)/$$d") || rc=1; \
	done; \
	echo "== app: cargo clippy -D warnings"; (cd app && $(CARGO) clippy --workspace --all-targets -- -D warnings) || rc=1; \
	echo "== renderer: tsc"; (cd renderer && $(PNPM) run typecheck) || rc=1; \
	echo "== infra: terraform fmt -check"; $(TERRAFORM) -chdir=infra/terraform fmt -check -recursive || rc=1; \
	if command -v actionlint >/dev/null 2>&1; then echo "== actionlint"; actionlint .github/workflows/ci.yml || rc=1; \
	else echo "-- actionlint not installed, skipped"; fi; \
	exit $$rc

fmt-check: ## ruff format --check (+ bench/, infra/cost/, docs/checks/), cargo fmt --check, terraform fmt -check; renderer has none
	@rc=0; \
	for d in $(DIR_common) $(DIR_capture) $(DIR_forecast) $(DIR_eval); do \
		echo "== $$d: ruff format --check"; (cd $$d && $(UV) run --locked ruff format --check .) || rc=1; \
	done; \
	for d in $(RUFF_EXTRA_DIRS); do \
		echo "== $$d: ruff format --check ($(DIR_common)'s ruff)"; \
		(cd $(DIR_common) && $(UV) run --locked ruff format --check --no-cache "$(CURDIR)/$$d") || rc=1; \
	done; \
	echo "== app: cargo fmt --check"; (cd app && $(CARGO) fmt --all --check) || rc=1; \
	echo "== infra: terraform fmt -check"; $(TERRAFORM) -chdir=infra/terraform fmt -check -recursive || rc=1; \
	exit $$rc
