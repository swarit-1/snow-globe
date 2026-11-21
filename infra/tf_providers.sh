#!/usr/bin/env bash
# Terraform provider installation for infra/test.sh, with an offline path (PRD §11.3; CLAUDE.md "make test").
#
# `terraform init` normally asks registry.terraform.io for the provider's versions on every run, even with the lock
# file and a warm plugin cache, so offline it fails. The plugin cache (TF_PLUGIN_CACHE_DIR, by default
# <data dir>/cache/terraform-plugins, outside iCloud; see tfp_data_dir) already has the unpacked layout of a Terraform
# filesystem mirror, <host>/<namespace>/<type>/<version>/<os>_<arch>/. So once it holds every provider a module's
# .terraform.lock.hcl locks, for this machine's platform, init can install from it with -plugin-dir and never
# touch the network. That is used only when the cached package's h1: hash (Go's dirhash, as Terraform records it)
# is in the lock file, which is the check Terraform then makes itself. A cache restored from another platform's run
# (CI's linux_amd64, whose h1: the committed lock files do not hold) therefore still goes to the registry.
#
# Modes, from SNOW_GLOBE_TF_INIT (or, as before the rename, LOOKOUT_TF_INIT; D-080):
#   auto      (default) offline when the cache matches the lock file; otherwise, or if the offline init fails, the
#             registry, which also fills the cache for next time
#   offline   never the registry: exit 1 with what is missing and the one-time command, before running init
#   registry  always the registry (the only behaviour before 20 Nov 2026)
#
# Standalone:
#   infra/tf_providers.sh status   for each module, can init run offline? Reads files only, no network; exit 1 if not
#   infra/tf_providers.sh fetch    one registry init per module (registry.terraform.io, releases.hashicorp.com), so
#                                  later runs work offline. Nothing is planned or applied.
# Sourced by infra/test.sh, it defines tfp_init DIR LOG, tfp_tflint DIR and the helpers below. bash 3.2 compatible.
#
# The same modes cover tflint's one network step: `tflint --init` downloads the AWS ruleset from GitHub. offline
# skips it and runs tflint on the rulesets already installed.
#
# The offline init runs without TF_PLUGIN_CACHE_DIR, because Terraform's docs forbid one directory being both the
# plugin cache and a filesystem mirror in the same run. .terraform/ then holds a symlink into the cache, as before.

TFP_MODULES="terraform terraform/bootstrap" # relative to infra/

tfp_log() { printf 'tf_providers: %s\n' "$*"; }

# tfp_tf ARGS...: terraform, with a short TMPDIR when needed. Terraform reaches its provider over a Unix socket
# created in TMPDIR, and socket paths are capped at 104 bytes on macOS (108 on Linux). With a longer TMPDIR every
# provider call fails with "Failed to read any lines from plugin's stdout", so Terraform alone gets /tmp then.
tfp_tf() {
  local tmp="${TMPDIR:-/tmp}"
  if [ "${#tmp}" -gt 60 ]; then
    TMPDIR=/tmp CHECKPOINT_DISABLE=1 terraform "$@"
  else
    CHECKPOINT_DISABLE=1 terraform "$@"
  fi
}

# tfp_data_dir: the data dir, resolved as the workers resolve it (D-080): $SNOW_GLOBE_DATA_DIR, else the deprecated
# $LOOKOUT_DATA_DIR (blank counts as unset; surrounding whitespace is dropped), else ~/snow-globe-data if it
# exists, else ~/lookout-data if it exists (the folder's name before the rename), else ~/snow-globe-data.
# Symlinks are followed and nothing is created. While both folders exist and are different folders (something
# made ~/snow-globe-data before the rename's live switch), it prints why on stderr and returns 1.
tfp_data_dir() {
  local v new="$HOME/snow-globe-data" old="$HOME/lookout-data"
  for v in "${SNOW_GLOBE_DATA_DIR:-}" "${LOOKOUT_DATA_DIR:-}"; do
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    if [ -n "$v" ]; then
      printf '%s\n' "$v"
      return 0
    fi
  done
  if [ -e "$new" ] && [ -e "$old" ] && ! [ "$new" -ef "$old" ]; then
    printf '%s\n' "both $new and $old exist and are different folders, so the data dir is ambiguous (D-080):" \
      "until the rename's live switch the data is in $old, and after it $old is a symlink to $new." \
      "Set SNOW_GLOBE_DATA_DIR to the folder you mean, and tell the lead which one is stray." >&2
    return 1
  elif [ -e "$new" ] || [ ! -e "$old" ]; then
    printf '%s\n' "$new"
  else
    printf '%s\n' "$old"
  fi
}

# tfp_cache_dir: the plugin cache, with infra/test.sh's default (returns 1 when tfp_data_dir does).
tfp_cache_dir() {
  local data
  if [ -n "${TF_PLUGIN_CACHE_DIR:-}" ]; then
    printf '%s\n' "$TF_PLUGIN_CACHE_DIR"
    return 0
  fi
  data="$(tfp_data_dir)" || return 1
  printf '%s\n' "$data/cache/terraform-plugins"
}

# tfp_mode: auto, offline or registry, from SNOW_GLOBE_TF_INIT, else the deprecated LOOKOUT_TF_INIT (D-080).
tfp_mode() {
  printf '%s\n' "${SNOW_GLOBE_TF_INIT:-${LOOKOUT_TF_INIT:-auto}}"
}

# tfp_platform: sets TFP_PLATFORM to this machine's Terraform platform, e.g. darwin_arm64 (once per shell).
# CHECKPOINT_DISABLE keeps `terraform version` from asking checkpoint-api.hashicorp.com about newer releases.
TFP_PLATFORM=""
tfp_platform() {
  if [ -z "$TFP_PLATFORM" ]; then
    TFP_PLATFORM="$(CHECKPOINT_DISABLE=1 terraform version -json 2>/dev/null |
      sed -n 's/^ *"platform": *"\([^"]*\)".*/\1/p' | head -n 1)"
  fi
  [ -n "$TFP_PLATFORM" ]
}

# tfp_locked_providers LOCKFILE: one line per provider, "<source><TAB><version><TAB><h1 hashes, comma-separated>".
tfp_locked_providers() {
  awk '
    /^provider "/ { match($0, /"[^"]+"/); src = substr($0, RSTART + 1, RLENGTH - 2); ver = ""; h1 = ""; next }
    src != "" && /^  version[[:space:]]*=/ { match($0, /"[^"]+"/); ver = substr($0, RSTART + 1, RLENGTH - 2); next }
    src != "" && /"h1:[^"]+"/ {
      match($0, /"h1:[^"]+"/); h1 = h1 (h1 == "" ? "" : ",") substr($0, RSTART + 1, RLENGTH - 2); next
    }
    src != "" && /^}/ { printf "%s\t%s\t%s\n", src, ver, h1; src = "" }
  ' "$1"
}

tfp_sha256_hex() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d ' ' -f 1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d ' ' -f 1
  else
    openssl dgst -sha256 -r "$1" | cut -d ' ' -f 1
  fi
}

# tfp_h1_of_dir DIR: print the "h1:" hash of an unpacked provider package as Terraform computes it
# (golang.org/x/mod/sumdb/dirhash Hash1): for every file, in byte order of its slash path, "<sha256 hex>  <path>\n";
# then the SHA-256 of those lines, base64. Prints nothing and fails if openssl is missing.
tfp_h1_of_dir() {
  command -v openssl >/dev/null 2>&1 || return 1
  (
    cd "$1" || exit 1
    find . ! -type d -print | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do
      printf '%s  %s\n' "$(tfp_sha256_hex "$f")" "$f"
    done
  ) | openssl dgst -sha256 -binary | openssl base64 -A | sed 's/^/h1:/'
}

# One-entry memo, so the two modules (both lock hashicorp/aws) hash the ~760 MB provider binary once per run.
_TFP_MEMO_DIR=""
_TFP_MEMO_H1=""

# tfp_offline_ready MODULE_DIR [CACHE_DIR]: return 0 if init can run offline from the cache. Sets TFP_REASON to
# what matched, or why not. (A variable rather than output, so that the memo survives between calls.)
TFP_REASON=""
tfp_offline_ready() {
  local dir="$1" cache="${2:-}" lock providers src ver hashes pkg found=""
  [ -n "$cache" ] || cache="$(tfp_cache_dir)" || return 1
  lock="$dir/.terraform.lock.hcl"
  if ! tfp_platform; then
    TFP_REASON="cannot tell this machine's Terraform platform (terraform version -json)"
    return 1
  fi
  if [ ! -f "$lock" ]; then
    TFP_REASON="no .terraform.lock.hcl in $dir"
    return 1
  fi
  providers="$(tfp_locked_providers "$lock")"
  if [ -z "$providers" ]; then
    TFP_REASON="$lock locks no provider"
    return 1
  fi
  while IFS="$(printf '\t')" read -r src ver hashes; do
    [ -n "$src" ] || continue
    pkg="$cache/$src/$ver/$TFP_PLATFORM"
    if [ ! -d "$pkg" ]; then
      TFP_REASON="$src $ver for $TFP_PLATFORM is not in the plugin cache ($cache)"
      return 1
    fi
    if [ -z "$hashes" ]; then
      TFP_REASON="the lock file holds no h1: hash for $src $ver"
      return 1
    fi
    if [ "$pkg" != "$_TFP_MEMO_DIR" ]; then
      _TFP_MEMO_H1="$(tfp_h1_of_dir "$pkg" || true)"
      _TFP_MEMO_DIR="$pkg"
    fi
    case ",$hashes," in
      *",$_TFP_MEMO_H1,"*) found="$found${found:+, }$src $ver $TFP_PLATFORM" ;;
      *)
        TFP_REASON="the cached $src $ver for $TFP_PLATFORM (${_TFP_MEMO_H1:-hash unavailable}) matches no h1: hash in $lock"
        return 1
        ;;
    esac
  done <<EOF
$providers
EOF
  TFP_REASON="$found, h1: in the lock file"
  return 0
}

# tfp_init MODULE_DIR LOG: `terraform init -backend=false` for one module, offline when possible (see the modes
# above). Prints one line saying which way it went; Terraform's own output goes to LOG. Returns init's status.
tfp_init() {
  local dir="$1" log="$2" mode cache
  mode="$(tfp_mode)"
  cache="$(tfp_cache_dir)" || return 1
  case "$mode" in
    auto | offline | registry) ;;
    *)
      tfp_log "SNOW_GLOBE_TF_INIT must be auto, offline or registry, not '$mode'"
      return 2
      ;;
  esac
  if [ "$mode" = registry ]; then
    tfp_log "registry.terraform.io (SNOW_GLOBE_TF_INIT=registry)"
  elif tfp_offline_ready "$dir" "$cache"; then
    tfp_log "offline from the plugin cache: $TFP_REASON"
    if (
      unset TF_PLUGIN_CACHE_DIR
      tfp_tf -chdir="$dir" init -backend=false -input=false -no-color -lockfile=readonly -plugin-dir="$cache"
    ) >"$log" 2>&1; then
      # init records -plugin-dir in .terraform/plugin_path, and later inits keep rewriting it. The providers are
      # installed by then, so drop it: the working directory is left as a registry init leaves it (deploy.sh
      # runs its own init there).
      rm -f "$dir/.terraform/plugin_path"
      return 0
    fi
    if [ "$mode" = offline ]; then
      tfp_log "the offline init failed, and SNOW_GLOBE_TF_INIT=offline allows no registry fallback"
      return 1
    fi
    tfp_log "the offline init failed ($(grep -m 1 -E '^Error' "$log" || echo 'see the log')); trying the registry"
  elif [ "$mode" = offline ]; then
    tfp_log "cannot init offline: $TFP_REASON"
    tfp_log "fetch the providers once, with network: infra/tf_providers.sh fetch"
    return 1
  else
    tfp_log "registry.terraform.io: $TFP_REASON"
  fi
  mkdir -p "$cache"
  TF_PLUGIN_CACHE_DIR="$cache" tfp_tf -chdir="$dir" init -backend=false -input=false -no-color >"$log" 2>&1
}

# tfp_tflint TF_DIR: tflint over TF_DIR and its modules (config TF_DIR/.tflint.hcl), when tflint is installed. In
# auto and registry modes it first runs `tflint --init`, which fetches the rulesets the config pins from GitHub
# when they are missing. SNOW_GLOBE_TF_INIT=offline never runs --init: tflint then uses only rulesets already installed,
# and if one is missing it fails, and this names the one-time command. Prints "ok" or "SKIP ..."; returns tflint's
# failure.
tfp_tflint() {
  local dir="$1" mode
  mode="$(tfp_mode)"
  if ! command -v tflint >/dev/null 2>&1; then
    echo "SKIP tflint not installed"
    return 0
  fi
  if [ "$mode" != offline ]; then
    (cd "$dir" && tflint --init --config "$dir/.tflint.hcl" >/dev/null) || return 1
  fi
  if ! (cd "$dir" && tflint --recursive --config "$dir/.tflint.hcl"); then
    if [ "$mode" = offline ]; then
      tfp_log "tflint failed with SNOW_GLOBE_TF_INIT=offline, which skips tflint --init; if a ruleset is missing," \
        "install it once, with network: (cd infra/terraform && tflint --init)"
    fi
    return 1
  fi
  echo "ok"
}

tfp_main() {
  local infra cmd="${1:-}" rc=0 dir log
  infra="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  case "$cmd" in
    status)
      for dir in $TFP_MODULES; do
        if tfp_offline_ready "$infra/$dir"; then
          echo "infra/$dir: offline ok: $TFP_REASON"
        else
          echo "infra/$dir: needs the registry: $TFP_REASON"
          rc=1
        fi
      done
      ;;
    fetch)
      log="$(mktemp "${TMPDIR:-/tmp}/snow-globe-tf-fetch.XXXXXX")"
      for dir in $TFP_MODULES; do
        if SNOW_GLOBE_TF_INIT=registry tfp_init "$infra/$dir" "$log"; then
          echo "infra/$dir: providers fetched into $(tfp_cache_dir)"
        else
          cat "$log"
          rc=1
        fi
      done
      rm -f "$log"
      if [ "$rc" -eq 0 ]; then
        tfp_main status || rc=1
      fi
      ;;
    *)
      awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}" >&2
      rc=2
      ;;
  esac
  return "$rc"
}

# Run as a script, not sourced: the status and fetch commands.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  tfp_main "$@"
fi
