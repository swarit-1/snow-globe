#!/usr/bin/env bash
# Tests for infra/tf_providers.sh, the offline provider path of infra/test.sh. No network, no real provider.
#
# A fake provider (a shell script that only exits) stands in for hashicorp/aws, in a fake plugin cache with the
# filesystem-mirror layout. Terraform itself is the oracle for the h1: hash (`terraform providers lock -fs-mirror`
# reads only that directory), and a real offline `terraform init -plugin-dir` must accept the package. Every real
# Terraform call runs with HTTP(S)_PROXY and ALL_PROXY pointing at a closed local port, so any attempt to reach the
# network fails the test. A stub terraform on PATH checks which way tfp_init goes in each mode, running no Terraform.
#
# Run: infra/tests/test_tf_providers.sh   (bash 3.2 compatible; exits non-zero on the first failure)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="$(cd "$HERE/.." && pwd)"

# shellcheck disable=SC1091 # infra/tf_providers.sh is shellchecked on its own by infra/test.sh
. "$INFRA/tf_providers.sh"

PASSED=0
CURRENT=""
fail() {
  printf '  FAIL %s: %s\n' "$CURRENT" "$*"
  exit 1
}
ok() {
  printf '  ok   %s\n' "$CURRENT"
  PASSED=$((PASSED + 1))
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/snow-globe-tfp-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Hermetic Terraform: no user CLI config, no version check, nothing reachable but a closed port.
: >"$WORK/empty.tfrc"
export TF_CLI_CONFIG_FILE="$WORK/empty.tfrc" CHECKPOINT_DISABLE=1 TF_IN_AUTOMATION=1 TF_REGISTRY_DISCOVERY_RETRY=0
export HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 all_proxy=http://127.0.0.1:9 NO_PROXY='' no_proxy=''
unset TF_PLUGIN_CACHE_DIR SNOW_GLOBE_TF_INIT LOOKOUT_TF_INIT

echo "infra/tests/test_tf_providers.sh"
command -v terraform >/dev/null 2>&1 || { echo "  SKIP terraform not installed"; exit 0; }
tfp_platform || { echo "  FAIL cannot read the platform from terraform version -json"; exit 1; }
PLATFORM="$TFP_PLATFORM"

# new_package CACHE_DIR [PLATFORM]: a fake provider package. File names check the byte order of slash paths, which
# locale collation breaks ("Zeta" sorts before "sub.b", and "sub.b" before "sub/a.txt"), and a name with a space.
new_package() {
  local pkg="$1/example.com/snow-globe/fake/1.0.0/${2:-$PLATFORM}"
  mkdir -p "$pkg/sub"
  printf '#!/bin/sh\necho "fake provider: it only exits" >&2\nexit 1\n' >"$pkg/terraform-provider-fake_v1.0.0_x5"
  chmod +x "$pkg/terraform-provider-fake_v1.0.0_x5"
  printf 'fake licence\n' >"$pkg/LICENSE.txt"
  printf 'z\n' >"$pkg/Zeta"
  printf 'b\n' >"$pkg/sub.b"
  printf 'a\n' >"$pkg/sub/a.txt"
  printf 'space\n' >"$pkg/with space.txt"
}

# new_module DIR: a module that requires the fake provider.
new_module() {
  mkdir -p "$1"
  cat >"$1/main.tf" <<'EOF'
terraform {
  required_providers {
    fake = {
      source  = "example.com/snow-globe/fake"
      version = "1.0.0"
    }
  }
}
EOF
}

CACHE="$WORK/cache"
new_package "$CACHE"
MOD="$WORK/module"
new_module "$MOD"

# ---- the h1: hash ---------------------------------------------------------------------------------------------------

CURRENT="h1_hash_matches_terraforms_own"
(cd "$MOD" && terraform providers lock -fs-mirror="$CACHE" -platform="$PLATFORM" -no-color >"$WORK/lock.log" 2>&1) ||
  fail "terraform providers lock -fs-mirror failed: $(cat "$WORK/lock.log")"
oracle="$(grep -o '"h1:[^"]*"' "$MOD/.terraform.lock.hcl" | tr -d '"')"
[ "$(printf '%s\n' "$oracle" | wc -l | tr -d ' ')" = 1 ] || fail "expected one h1: hash from Terraform, got: $oracle"
mine="$(tfp_h1_of_dir "$CACHE/example.com/snow-globe/fake/1.0.0/$PLATFORM")"
[ "$mine" = "$oracle" ] || fail "tfp_h1_of_dir gave $mine, Terraform $oracle"
cp "$MOD/.terraform.lock.hcl" "$WORK/lock.orig"
ok

# ---- lock-file parsing --------------------------------------------------------------------------------------------------

CURRENT="lock_file_parsing"
cat >"$WORK/two.lock.hcl" <<'EOF'
# This file is maintained automatically by "terraform init".

provider "registry.terraform.io/hashicorp/aws" {
  version     = "6.73.0"
  constraints = "~> 6.73"
  hashes = [
    "h1:AAAA=",
    "h1:BBBB=",
    "zh:0f7403264fbfbcad526a5ddb9553be6ab12dba14e5901d0c2ccabc38d9ccf9a0",
  ]
}

provider "example.com/snow-globe/fake" {
  version = "1.0.0"
  hashes = [
    "zh:abcd",
  ]
}
EOF
got="$(tfp_locked_providers "$WORK/two.lock.hcl")"
want="$(printf 'registry.terraform.io/hashicorp/aws\t6.73.0\th1:AAAA=,h1:BBBB=\nexample.com/snow-globe/fake\t1.0.0\t')"
[ "$got" = "$want" ] || fail "got: $got"
# The committed lock files parse as well: hashicorp/aws, its version, at least one h1: hash.
for lock in "$INFRA/terraform/.terraform.lock.hcl" "$INFRA/terraform/bootstrap/.terraform.lock.hcl"; do
  tfp_locked_providers "$lock" | grep -Eq '^registry\.terraform\.io/hashicorp/aws	[0-9]+\.[0-9]+\.[0-9]+	h1:' ||
    fail "${lock#"$INFRA"/}: no hashicorp/aws with a version and an h1: hash"
done
ok

# ---- when the cache can serve --------------------------------------------------------------------------------------------

CURRENT="ready_when_the_cache_matches_the_lock_file"
_TFP_MEMO_DIR=""
tfp_offline_ready "$MOD" "$CACHE" || fail "not ready: $TFP_REASON"
case "$TFP_REASON" in *"example.com/snow-globe/fake 1.0.0 $PLATFORM"*) ;; *) fail "reason: $TFP_REASON" ;; esac
ok

CURRENT="not_ready_when_the_provider_is_not_cached"
mkdir -p "$WORK/empty-cache"
if tfp_offline_ready "$MOD" "$WORK/empty-cache"; then fail "ready with an empty cache"; fi
case "$TFP_REASON" in *"is not in the plugin cache"*) ;; *) fail "reason: $TFP_REASON" ;; esac
ok

# A cache restored from another machine, or a damaged one: same path, other bytes (CI's linux_amd64 case).
CURRENT="not_ready_when_the_cached_package_differs_from_the_lock_file"
new_package "$WORK/other-cache"
printf 'another build\n' >>"$WORK/other-cache/example.com/snow-globe/fake/1.0.0/$PLATFORM/terraform-provider-fake_v1.0.0_x5"
if tfp_offline_ready "$MOD" "$WORK/other-cache"; then fail "ready with a package whose hash is not locked"; fi
case "$TFP_REASON" in *"matches no h1: hash"*) ;; *) fail "reason: $TFP_REASON" ;; esac
ok

CURRENT="not_ready_when_only_another_platform_is_cached"
new_package "$WORK/foreign-cache" "plan9_mips"
if tfp_offline_ready "$MOD" "$WORK/foreign-cache"; then fail "ready with only plan9_mips cached"; fi
case "$TFP_REASON" in *"for $PLATFORM is not in the plugin cache"*) ;; *) fail "reason: $TFP_REASON" ;; esac
ok

CURRENT="not_ready_without_a_lock_file"
new_module "$WORK/unlocked"
if tfp_offline_ready "$WORK/unlocked" "$CACHE"; then fail "ready without a lock file"; fi
case "$TFP_REASON" in *"no .terraform.lock.hcl"*) ;; *) fail "reason: $TFP_REASON" ;; esac
ok

# ---- a real offline init ----------------------------------------------------------------------------------------------

CURRENT="offline_init_installs_from_the_cache_without_network"
_TFP_MEMO_DIR=""
out="$(TF_PLUGIN_CACHE_DIR="$CACHE" SNOW_GLOBE_TF_INIT=offline tfp_init "$MOD" "$WORK/init.log")" ||
  fail "tfp_init failed: $out $(cat "$WORK/init.log")"
case "$out" in *"offline from the plugin cache"*) ;; *) fail "output: $out" ;; esac
installed="$MOD/.terraform/providers/example.com/snow-globe/fake/1.0.0/$PLATFORM"
[ -e "$installed/terraform-provider-fake_v1.0.0_x5" ] || fail "provider not installed at $installed: $(cat "$WORK/init.log")"
[ -L "$installed" ] || fail "expected .terraform/ to hold a symlink into the cache, not a copy"
cmp -s "$MOD/.terraform.lock.hcl" "$WORK/lock.orig" || fail "the offline init changed the lock file"
grep -q 'successfully initialized' "$WORK/init.log" || fail "init log: $(cat "$WORK/init.log")"
[ ! -e "$MOD/.terraform/plugin_path" ] || fail ".terraform/plugin_path left behind: $(cat "$MOD/.terraform/plugin_path")"
ok

# With plugin_path gone, Terraform still resolves the installed package from .terraform/providers. validate then
# starts the fake provider, which only exits, so the check is that Terraform found it and tried to start it, rather
# than "there is no package ... cached in .terraform/providers". Terraform wraps its messages, so whitespace is
# flattened first; and go-plugin reports a provider that exits at once in one of two ways, either of which will do.
CURRENT="terraform_finds_the_installed_provider_without_plugin_path"
out="$(tfp_tf -chdir="$MOD" validate -no-color 2>&1 || true)"
flat="$(printf '%s\n' "$out" | tr -s '\n\t ' '   ')"
case "$flat" in *"there is no package"*) fail "provider not found after the offline init: $out" ;; esac
case "$flat" in
  *'instantiate provider "example.com/snow-globe/fake"'* | *"Path: .terraform/providers/example.com/snow-globe/fake/1.0.0/"*) ;;
  *) fail "Terraform did not try to start the installed provider: $out" ;;
esac
ok

# ---- which way tfp_init goes, with a stub terraform -------------------------------------------------------------------

mkdir -p "$WORK/bin"
cat >"$WORK/bin/terraform" <<'EOF'
#!/usr/bin/env bash
# Stub: logs each call with the plugin cache it saw, answers `version -json`, and fails an init that uses
# -plugin-dir when STUB_FAIL_PLUGIN_DIR=1.
printf '%s|cache=%s\n' "$*" "${TF_PLUGIN_CACHE_DIR-<unset>}" >>"$STUB_LOG"
case "$*" in
  "version -json") printf '{\n  "terraform_version": "1.16.1",\n  "platform": "%s"\n}\n' "$STUB_PLATFORM" ;;
  *-plugin-dir=*) if [ "${STUB_FAIL_PLUGIN_DIR:-0}" = 1 ]; then echo "Error: stub offline failure"; exit 1; fi ;;
esac
exit 0
EOF
chmod +x "$WORK/bin/terraform"
export STUB_PLATFORM="$PLATFORM" STUB_LOG="$WORK/stub.log"
REAL_PATH="$PATH"

# stub_init MODE CACHE [FAIL_OFFLINE]: run tfp_init against the stub; sets OUT, RC and CALLS (the stub's init calls).
stub_init() {
  : >"$STUB_LOG"
  _TFP_MEMO_DIR=""
  TFP_PLATFORM=""
  RC=0
  OUT="$(PATH="$WORK/bin:$REAL_PATH" TF_PLUGIN_CACHE_DIR="$2" SNOW_GLOBE_TF_INIT="$1" STUB_FAIL_PLUGIN_DIR="${3:-0}" \
    tfp_init "$MOD" "$WORK/stub-init.log")" || RC=$?
  CALLS="$(grep ' init ' "$STUB_LOG" || true)"
}
count() { if [ -z "$1" ]; then echo 0; else printf '%s\n' "$1" | wc -l | tr -d ' '; fi; }

CURRENT="auto_uses_the_cache_without_the_cache_variable"
stub_init auto "$CACHE"
[ "$RC" = 0 ] || fail "rc $RC: $OUT"
[ "$(count "$CALLS")" = 1 ] || fail "expected one init, got: $CALLS"
case "$CALLS" in *"-lockfile=readonly -plugin-dir=$CACHE|cache=<unset>") ;; *) fail "init call: $CALLS" ;; esac
ok

CURRENT="auto_falls_back_to_the_registry_when_the_cache_cannot_serve"
stub_init auto "$WORK/empty-cache"
[ "$RC" = 0 ] || fail "rc $RC: $OUT"
case "$OUT" in *"registry.terraform.io: "*"is not in the plugin cache"*) ;; *) fail "output: $OUT" ;; esac
[ "$(count "$CALLS")" = 1 ] || fail "expected one init, got: $CALLS"
case "$CALLS" in *-plugin-dir=*) fail "registry init used -plugin-dir: $CALLS" ;; esac
case "$CALLS" in *"|cache=$WORK/empty-cache") ;; *) fail "the registry init must fill the plugin cache: $CALLS" ;; esac
ok

CURRENT="auto_falls_back_to_the_registry_when_the_offline_init_fails"
stub_init auto "$CACHE" 1
[ "$RC" = 0 ] || fail "rc $RC: $OUT"
case "$OUT" in *"the offline init failed (Error: stub offline failure); trying the registry"*) ;; *) fail "output: $OUT" ;; esac
[ "$(count "$CALLS")" = 2 ] || fail "expected an offline then a registry init, got: $CALLS"
case "$(printf '%s\n' "$CALLS" | sed -n 2p)" in *-plugin-dir=*) fail "second init must use the registry: $CALLS" ;; esac
ok

CURRENT="offline_refuses_before_running_init_when_the_cache_cannot_serve"
stub_init offline "$WORK/empty-cache"
[ "$RC" = 1 ] || fail "rc $RC (want 1): $OUT"
[ "$(count "$CALLS")" = 0 ] || fail "offline mode ran init: $CALLS"
case "$OUT" in *"cannot init offline: "*"infra/tf_providers.sh fetch"*) ;; *) fail "output: $OUT" ;; esac
ok

CURRENT="offline_never_falls_back_to_the_registry"
stub_init offline "$CACHE" 1
[ "$RC" = 1 ] || fail "rc $RC (want 1): $OUT"
[ "$(count "$CALLS")" = 1 ] || fail "expected only the offline init, got: $CALLS"
ok

CURRENT="registry_mode_always_uses_the_registry"
stub_init registry "$CACHE"
[ "$RC" = 0 ] || fail "rc $RC: $OUT"
[ "$(count "$CALLS")" = 1 ] || fail "expected one init, got: $CALLS"
case "$CALLS" in *-plugin-dir=*) fail "registry mode used -plugin-dir: $CALLS" ;; esac
ok

# Standalone (`infra/tf_providers.sh fetch`), TF_PLUGIN_CACHE_DIR may be unset: the registry init must still put
# the provider in the default cache under SNOW_GLOBE_DATA_DIR, never unpack ~760 MB into .terraform/ inside iCloud.
CURRENT="registry_init_fills_the_default_cache_when_the_variable_is_unset"
: >"$STUB_LOG"
_TFP_MEMO_DIR=""
TFP_PLATFORM=""
RC=0
OUT="$(unset TF_PLUGIN_CACHE_DIR
  PATH="$WORK/bin:$REAL_PATH" SNOW_GLOBE_DATA_DIR="$WORK/data" SNOW_GLOBE_TF_INIT=registry tfp_init "$MOD" "$WORK/stub-init.log")" ||
  RC=$?
[ "$RC" = 0 ] || fail "rc $RC: $OUT"
case "$(grep ' init ' "$STUB_LOG")" in *"|cache=$WORK/data/cache/terraform-plugins") ;; *) fail "init call: $(cat "$STUB_LOG")" ;; esac
[ -d "$WORK/data/cache/terraform-plugins" ] || fail "the default cache was not created"
ok

CURRENT="an_unknown_mode_is_refused"
stub_init sometimes "$CACHE"
[ "$RC" = 2 ] || fail "rc $RC (want 2): $OUT"
[ "$(count "$CALLS")" = 0 ] || fail "ran init: $CALLS"
ok

CURRENT="status_reports_each_module_and_fails_when_one_needs_the_registry"
RC=0
OUT="$(TF_PLUGIN_CACHE_DIR="$WORK/empty-cache" "$INFRA/tf_providers.sh" status)" || RC=$?
[ "$RC" = 1 ] || fail "rc $RC (want 1): $OUT"
[ "$(printf '%s\n' "$OUT" | grep -c ': needs the registry: ')" = 2 ] || fail "output: $OUT"
ok

# ---- tflint's one network step, with a stub tflint ---------------------------------------------------------------

mkdir -p "$WORK/tflint-bin" "$WORK/no-tflint-bin" "$WORK/tf"
cat >"$WORK/tflint-bin/tflint" <<'EOF'
#!/usr/bin/env bash
# Stub: logs each call; `--recursive` fails when STUB_TFLINT_FAIL=1 (a ruleset that was never installed).
printf '%s\n' "$*" >>"$STUB_LOG"
case "$*" in *--recursive*) [ "${STUB_TFLINT_FAIL:-0}" = 1 ] && { echo "Failed to initialize plugins"; exit 1; } ;; esac
exit 0
EOF
chmod +x "$WORK/tflint-bin/tflint"

# stub_tflint MODE [FAIL]: run tfp_tflint against the stub; sets OUT, RC and CALLS (the stub's calls).
stub_tflint() {
  : >"$STUB_LOG"
  RC=0
  OUT="$(PATH="$WORK/tflint-bin:$REAL_PATH" SNOW_GLOBE_TF_INIT="$1" STUB_TFLINT_FAIL="${2:-0}" tfp_tflint "$WORK/tf" 2>&1)" ||
    RC=$?
  CALLS="$(cat "$STUB_LOG")"
}

CURRENT="tflint_inits_then_lints_in_auto_and_registry_modes"
for mode in auto registry; do
  stub_tflint "$mode"
  [ "$RC" = 0 ] || fail "$mode: rc $RC: $OUT"
  [ "$(printf '%s\n' "$CALLS" | sed -n 1p)" = "--init --config $WORK/tf/.tflint.hcl" ] || fail "$mode: calls: $CALLS"
  [ "$(printf '%s\n' "$CALLS" | sed -n 2p)" = "--recursive --config $WORK/tf/.tflint.hcl" ] || fail "$mode: calls: $CALLS"
  case "$OUT" in *ok) ;; *) fail "$mode: output: $OUT" ;; esac
done
ok

CURRENT="tflint_offline_never_runs_init"
stub_tflint offline
[ "$RC" = 0 ] || fail "rc $RC: $OUT"
[ "$CALLS" = "--recursive --config $WORK/tf/.tflint.hcl" ] || fail "calls: $CALLS"
ok

CURRENT="tflint_offline_names_the_one_time_command_when_a_ruleset_is_missing"
stub_tflint offline 1
[ "$RC" = 1 ] || fail "rc $RC (want 1): $OUT"
case "$CALLS" in *--init*) fail "offline mode ran tflint --init: $CALLS" ;; esac
case "$OUT" in *"skips tflint --init"*"tflint --init)"*) ;; *) fail "output: $OUT" ;; esac
ok

CURRENT="tflint_is_skipped_when_not_installed"
if PATH="$WORK/no-tflint-bin:/usr/bin:/bin" command -v tflint >/dev/null 2>&1; then
  echo "  SKIP $CURRENT (a tflint is in /usr/bin or /bin)"
else
  : >"$STUB_LOG"
  OUT="$(PATH="$WORK/no-tflint-bin:/usr/bin:/bin" tfp_tflint "$WORK/tf")" || fail "rc $?: $OUT"
  [ "$OUT" = "SKIP tflint not installed" ] || fail "output: $OUT"
  [ ! -s "$STUB_LOG" ] || fail "the stub ran: $(cat "$STUB_LOG")"
  ok
fi

# ---- the rename (D-080): the data dir and the mode under the new and the deprecated names ---------------------------

CURRENT="data_dir_and_mode_read_the_new_names_then_the_old_ones"
home="$WORK/home"
mkdir -p "$home"
dd() { env -u SNOW_GLOBE_DATA_DIR -u LOOKOUT_DATA_DIR -u TF_PLUGIN_CACHE_DIR HOME="$home" "$@" bash -c \
  ". '$INFRA/tf_providers.sh'; tfp_cache_dir"; }
[ "$(dd)" = "$home/snow-globe-data/cache/terraform-plugins" ] || fail "neither folder: $(dd)"
mkdir -p "$home/lookout-data"
[ "$(dd)" = "$home/lookout-data/cache/terraform-plugins" ] || fail "only the old folder: $(dd)"
mkdir -p "$home/snow-globe-data"
# Both folders exist apart (~/snow-globe-data made before the switch): refused, with nothing printed on stdout.
if out="$(dd 2>"$WORK/dd.err")"; then fail "both folders apart were accepted: $out"; fi
[ -z "$out" ] || fail "both folders apart printed: $out"
grep -q "ambiguous (D-080)" "$WORK/dd.err" || fail "both folders apart, stderr: $(cat "$WORK/dd.err")"
[ "$(dd SNOW_GLOBE_DATA_DIR="$home/lookout-data")" = "$home/lookout-data/cache/terraform-plugins" ] ||
  fail "both folders apart, the variable decides"
# After the switch: ~/lookout-data is a symlink to ~/snow-globe-data, so the new name wins.
rmdir "$home/lookout-data"
ln -s "$home/snow-globe-data" "$home/lookout-data"
[ "$(dd)" = "$home/snow-globe-data/cache/terraform-plugins" ] || fail "after the switch: $(dd)"
# A blank or whitespace-only variable counts as unset, and surrounding whitespace is dropped (as in Python).
[ "$(dd SNOW_GLOBE_DATA_DIR=' ' LOOKOUT_DATA_DIR=' /old ')" = "/old/cache/terraform-plugins" ] ||
  fail "whitespace: $(dd SNOW_GLOBE_DATA_DIR=' ' LOOKOUT_DATA_DIR=' /old ')"
# A dangling ~/snow-globe-data symlink does not exist (as in Python): the old folder is used.
rm "$home/lookout-data"
mv "$home/snow-globe-data" "$home/lookout-data"
ln -s "$home/nowhere" "$home/snow-globe-data"
[ "$(dd)" = "$home/lookout-data/cache/terraform-plugins" ] || fail "dangling new symlink: $(dd)"
rm "$home/snow-globe-data"
[ "$(dd LOOKOUT_DATA_DIR=/old)" = "/old/cache/terraform-plugins" ] || fail "LOOKOUT_DATA_DIR: $(dd LOOKOUT_DATA_DIR=/old)"
[ "$(dd LOOKOUT_DATA_DIR=/old SNOW_GLOBE_DATA_DIR=/new)" = "/new/cache/terraform-plugins" ] || fail "SNOW_GLOBE_DATA_DIR"
[ "$(dd TF_PLUGIN_CACHE_DIR=/c SNOW_GLOBE_DATA_DIR=/new)" = "/c" ] || fail "TF_PLUGIN_CACHE_DIR"
[ "$(env -u SNOW_GLOBE_TF_INIT -u LOOKOUT_TF_INIT bash -c ". '$INFRA/tf_providers.sh'; tfp_mode")" = auto ] || fail "mode default"
[ "$(env -u SNOW_GLOBE_TF_INIT LOOKOUT_TF_INIT=offline bash -c ". '$INFRA/tf_providers.sh'; tfp_mode")" = offline ] ||
  fail "LOOKOUT_TF_INIT"
[ "$(env SNOW_GLOBE_TF_INIT=registry LOOKOUT_TF_INIT=offline bash -c ". '$INFRA/tf_providers.sh'; tfp_mode")" = registry ] ||
  fail "SNOW_GLOBE_TF_INIT"
ok

CURRENT="every_shell_copy_of_the_data_dir_rule_is_tfp_data_dir"
# The speech scripts and the spike's bundler can't source this file (each runs on its own), so they carry copies of
# tfp_data_dir as data_dir(). The rule tested above holds for them only while the copies stay identical.
want="$(sed -n '/^tfp_data_dir() {/,/^}/p' "$INFRA/tf_providers.sh" | sed '1s/^tfp_data_dir() {$/data_dir() {/')"
[ -n "$want" ] || fail "no tfp_data_dir() in infra/tf_providers.sh"
for f in app/speech-macos/scripts/build.sh app/speech-macos/scripts/check-symbols.sh \
  app/speech-macos/scripts/check-symbols-selftest.sh app/speech-macos/scripts/link-check.sh \
  spikes/m1-platform/scripts/bundle-sck-helper.sh; do
  got="$(sed -n '/^data_dir() {/,/^}/p' "$INFRA/../$f")"
  [ "$got" = "$want" ] || fail "$f: its data_dir() is not infra/tf_providers.sh's tfp_data_dir()"
done
ok

echo "infra/tests/test_tf_providers.sh: $PASSED passed, 0 failed"
