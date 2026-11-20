#!/usr/bin/env bash
# Static check on liblookout_speech.a (PRD §15.1: speech on device only; §6.5: the C ABI).
#
# Fails when:
# - the archive refers to anything in scripts/forbidden-symbols.txt (networking, server-capable
#   recognition, the authorization prompt, model download, microphone or recording, file
#   writing or reading), as an undefined symbol or as an Objective-C selector name, or contains
#   some of those names as raw bytes;
# - the archive imports a symbol, or names a selector, that is not in the reviewed baseline,
#   scripts/archive-imports.txt (an allow list), or no longer does so for one that is listed.
#   Selectors count because an Objective-C call leaves only a selector string and a plain
#   _objc_msgSend import (NSData.write(toFile:) adds no symbol of its own);
# - a C ABI function is missing or not external;
# - the objects ask the linker for Network.framework.
# The deny list also runs in Tests/LookoutSpeechTests/StaticChecksTests.swift on the debug
# objects; scripts/check-symbols-selftest.sh checks that this script catches each category.
#
# usage: scripts/check-symbols.sh [path/to/liblookout_speech.a]
#        scripts/check-symbols.sh --update-baseline [path/to/liblookout_speech.a]
#          rewrites archive-imports.txt from the archive (only when every other check passes);
#          review the printed and git diffs first: each new line is a new dependency.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UPDATE=0
if [ "${1:-}" = "--update-baseline" ]; then
  UPDATE=1
  shift
fi
ARCHIVE="${1:-${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos/release/liblookout_speech.a}"
DENY="$HERE/forbidden-symbols.txt"
BASELINE="$HERE/archive-imports.txt"
if [ ! -f "$ARCHIVE" ]; then
  echo "check-symbols: no archive at $ARCHIVE (build first: scripts/build.sh)" >&2
  exit 2
fi
export LC_ALL=C

# Capture each tool's output once: `tool | grep -q` under pipefail can turn a match into a miss.
# nm -j prints a "member.o:" line before each member's symbols; drop only those, because
# Objective-C calls appear as `_objc_msgSend$selector:` stub symbols, which also end in a colon.
symbols() { nm "$@" -j "$ARCHIVE" 2>/dev/null | grep -v -E '\.o:$' | grep -v '^$' | sort -u || true; }
UNDEFINED="$(symbols -u)"
DEFINED_ANY="$(symbols -U)"
DEFINED="$(nm -gU -m "$ARCHIVE" 2>/dev/null)"
# What the archive needs from outside itself: undefined in some member and defined in none.
IMPORTS="$(comm -23 <(printf '%s\n' "$UNDEFINED") <(printf '%s\n' "$DEFINED_ANY") | grep -v '^$' || true)"
# Objective-C selector names (the __objc_methname C strings: the selectors the code sends, and
# the method names of the classes and protocols it declares).
SELECTORS="$(otool -v -s __TEXT __objc_methname "$ARCHIVE" 2>/dev/null | sed -nE 's/^[0-9a-f]{16}[[:space:]]+//p' | sort -u || true)"
# The baseline lists both, a selector as @selector(name).
REFERENCES="$( { printf '%s\n' "$IMPORTS"; grep -v '^$' <<<"$SELECTORS" | sed 's/^/@selector(/; s/$/)/' || true; } | grep -v '^$' | sort -u)"
fail=0

indent() { sed 's/^/        /'; }
demangle() { xcrun swift-demangle --compact | indent; }

check() { # what, extended regex over undefined symbols and selector names
  local hits
  hits="$( { grep -E -- "$2" <<<"$UNDEFINED" || true; grep -E -- "$2" <<<"$SELECTORS" | sed 's/^/@selector(/; s/$/)/' || true; } )"
  if [ -n "$hits" ]; then
    echo "FAIL  $1:"
    indent <<<"$hits"
    fail=1
  else
    echo "ok    no $1"
  fi
}

rules=0
while IFS=$'\t' read -r what pattern; do
  case "$what" in '' | '#'*) continue ;; esac
  if [ -z "${pattern:-}" ]; then
    echo "check-symbols: malformed rule in $DENY: $what" >&2
    exit 2
  fi
  check "$what" "$pattern"
  rules=$((rules + 1))
done <"$DENY"
if [ "$rules" -lt 10 ]; then
  echo "check-symbols: only $rules rules read from $DENY" >&2
  exit 2
fi

# Selector names and Swift names also live in the symbol string tables, which `strings -a`
# does not read; search the archive's raw bytes.
for s in recognitionTask requestAuthorization NSURLSession downloadAndInstall assetInstallationRequest AVAudioFile AVAudioEngine; do
  if grep -a -q -- "$s" "$ARCHIVE"; then
    echo "FAIL  the bytes '$s' are in the archive"
    fail=1
  fi
done

# The C ABI: every function defined and external (not private extern).
missing=""
for f in status start push_f32 finish cancel free version set_contextual_strings set_silence_pad_ms set_model_retention; do
  if ! grep -E -q -- " external( \[no dead strip\])? _lk_speech_${f}\$" <<<"$DEFINED"; then
    missing="$missing _lk_speech_$f"
  fi
done
if [ -n "$missing" ]; then
  echo "FAIL  C ABI not exported:$missing"
  fail=1
else
  echo "ok    C ABI exported: 10 _lk_speech_* functions"
fi

# What the objects ask the linker to add (LC_LINKER_OPTION autolink entries), for the record.
LINKS="$(otool -l "$ARCHIVE" | awk '/LC_LINKER_OPTION/{l=1; next} l && /string #1/{a=$3; next} l && /string #2/{print a" "$3; l=0; next} l && /cmd /{l=0}' | sort -u)"
LIBS="$(otool -l "$ARCHIVE" | awk '/LC_LINKER_OPTION/{l=1; next} l && /count 1/{c=1; next} l && c && /string #1/{print $3; l=0; c=0; next} l && /cmd /{l=0; c=0}' | sort -u)"
# These come from the Clang modules the Swift code imports, transitively: `import Foundation`
# alone lists CFNetwork, because Foundation's module declares it (Foundation links CFNetwork
# itself). An autolink entry is not a use; the symbol checks above are the test of use. A direct
# import of the Network framework would show up both here and as `7Network` symbols.
echo "info  autolinked frameworks: $(grep '^-framework' <<<"$LINKS" | awk '{print $2}' | tr '\n' ' ')"
echo "info  autolinked libraries:  $(tr '\n' ' ' <<<"$LIBS")"
if grep -E -q -- '-framework Network$' <<<"$LINKS"; then
  echo "FAIL  the archive autolinks Network.framework (a direct import of Network)"
  fail=1
fi

count() { grep -c . <<<"$1" || true; }

if [ "$UPDATE" -eq 1 ]; then
  if [ "$fail" -ne 0 ]; then
    echo "check-symbols: refusing to write a baseline for an archive that fails the checks above"
    exit 1
  fi
  {
    echo "# The reviewed list of external symbols liblookout_speech.a (release build) imports, and of the"
    echo "# Objective-C selector names it contains, as @selector(name). One per line: the name, a TAB,"
    echo "# then the demangled name for the reviewer."
    echo "#"
    echo "# scripts/check-symbols.sh fails when the archive imports a symbol that is not listed here, or"
    echo "# no longer imports one that is (an allow list: every new dependency needs a review, which the"
    echo "# deny list in forbidden-symbols.txt alone cannot guarantee)."
    echo "# Regenerate with \`scripts/check-symbols.sh --update-baseline\` after reviewing what it prints."
    echo "# Toolchain: $(xcrun swift --version 2>/dev/null | head -1 | sed 's/^.*Apple Swift/Apple Swift/'); SDK $(xcrun --sdk macosx --show-sdk-version)."
    # swift-demangle prints one line per input line, so the two columns stay aligned.
    paste <(printf '%s\n' "$REFERENCES") <(printf '%s\n' "$REFERENCES" | xcrun swift-demangle --compact)
  } >"$BASELINE.tmp"
  OLD="$( { grep -v '^#' "$BASELINE" 2>/dev/null || true; } | cut -f1 | grep -v '^$' | sort -u || true)"
  echo "info  added to the baseline:"
  comm -13 <(printf '%s\n' "$OLD") <(printf '%s\n' "$REFERENCES") | grep -v '^$' | demangle || true
  echo "info  removed from the baseline:"
  comm -23 <(printf '%s\n' "$OLD") <(printf '%s\n' "$REFERENCES") | grep -v '^$' | demangle || true
  mv "$BASELINE.tmp" "$BASELINE"
  echo "check-symbols: wrote $BASELINE ($(count "$IMPORTS") imports, $(count "$SELECTORS") selectors)"
  exit 0
fi

if [ ! -f "$BASELINE" ]; then
  echo "FAIL  no reviewed baseline at $BASELINE (scripts/check-symbols.sh --update-baseline)"
  fail=1
else
  BASE="$(grep -v '^#' "$BASELINE" | cut -f1 | grep -v '^$' | sort -u || true)"
  NEW="$(comm -13 <(printf '%s\n' "$BASE") <(printf '%s\n' "$REFERENCES") | grep -v '^$' || true)"
  GONE="$(comm -23 <(printf '%s\n' "$BASE") <(printf '%s\n' "$REFERENCES") | grep -v '^$' || true)"
  if [ -n "$NEW" ]; then
    echo "FAIL  $(count "$NEW") import(s) or selector(s) not in the reviewed baseline (scripts/archive-imports.txt); review, then --update-baseline:"
    demangle <<<"$NEW"
    fail=1
  else
    echo "ok    every import and selector is in the reviewed baseline ($(count "$IMPORTS") imports, $(count "$SELECTORS") selectors)"
  fi
  if [ -n "$GONE" ]; then
    echo "FAIL  $(count "$GONE") baseline entr(ies) the archive no longer imports; --update-baseline:"
    demangle <<<"$GONE"
    fail=1
  fi
fi

echo "info  archive: $ARCHIVE ($(stat -f %z "$ARCHIVE") bytes)"
if [ "$fail" -ne 0 ]; then
  echo "check-symbols: FAILED"
  exit 1
fi
echo "check-symbols: passed"
