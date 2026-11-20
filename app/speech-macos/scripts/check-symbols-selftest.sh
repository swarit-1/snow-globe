#!/usr/bin/env bash
# Mutation self-test of scripts/check-symbols.sh (PRD §15.1: the archive's "on device only, never
# writes audio" claim rests on that check, so the check itself is tested).
#
# Compiles small objects that call each forbidden kind of API, adds each one to a copy of the
# release archive, and requires check-symbols.sh to fail with that category's FAIL line. Also
# requires a failure for a new but harmless import (the reviewed baseline, an allow list), for
# forbidden names hidden in data, and for an archive missing the C ABI; and a pass for the
# unmodified archive. Nothing here is run: the objects are only compiled and inspected.
#
# usage: scripts/check-symbols-selftest.sh [path/to/liblookout_speech.a]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="${1:-${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos/release/liblookout_speech.a}"
if [ ! -f "$ARCHIVE" ]; then
  echo "check-symbols-selftest: no archive at $ARCHIVE (build first: scripts/build.sh)" >&2
  exit 2
fi
ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lk-symtest.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# name|expected FAIL line (fixed string)|source file (written below)
MUTANTS=(
  "data-write|FAIL  file writing|DataWrite.swift"
  "string-write|FAIL  file writing|StringWrite.swift"
  "nsdata-write|FAIL  file writing|NSDataWrite.swift"
  "file-manager|FAIL  file writing|FileManagerCreate.swift"
  "darwin-open|FAIL  file writing|DarwinOpen.swift"
  "posix-open-write|FAIL  file writing|posix_write.c"
  "stdio|FAIL  file writing|stdio_write.c"
  "cf-write-stream|FAIL  file writing|cf_stream.c"
  "data-contents-of|FAIL  file or URL reading|DataContents.swift"
  "url-session|FAIL  networking (Foundation URL loading)|URLSessionTask.swift"
  "url-request|FAIL  networking (Foundation URL loading)|URLRequestMake.swift"
  "network-framework|FAIL  networking (Network.framework)|NetworkConnection.swift"
  "sockets-dns|FAIL  networking (BSD sockets, DNS)|sockets.c"
  "recognition-task|FAIL  server-capable recognition|RecognitionTask.swift"
  "request-authorization|FAIL  authorization prompt|RequestAuthorization.swift"
  "objc-selector-only|FAIL  file writing|selector_only.m"
  "asset-download|FAIL  model download|AssetDownload.swift"
  "audio-engine|FAIL  microphone or recording|AudioEngine.swift"
  "bytes-in-data|FAIL  the bytes 'recognitionTask'|hidden_name.c"
  "new-harmless-import|not in the reviewed baseline|harmless.c"
)

cat >DataWrite.swift <<'EOF'
import Foundation
public func lkMutantDataWrite(_ d: Data) throws { try d.write(to: URL(fileURLWithPath: "/dev/null")) }
EOF
cat >StringWrite.swift <<'EOF'
import Foundation
public func lkMutantStringWrite(_ s: String) throws { try s.write(toFile: "/dev/null", atomically: false, encoding: .utf8) }
EOF
cat >NSDataWrite.swift <<'EOF'
import Foundation
public func lkMutantNSDataWrite(_ d: Data) -> Bool { (d as NSData).write(toFile: "/dev/null", atomically: false) }
EOF
cat >FileManagerCreate.swift <<'EOF'
import Foundation
public func lkMutantFileManager() -> Bool { FileManager.default.createFile(atPath: "/dev/null", contents: nil) }
EOF
cat >DarwinOpen.swift <<'EOF'
import Darwin
public func lkMutantDarwinOpen() -> Int32 { open("/dev/null", O_WRONLY) }
EOF
cat >posix_write.c <<'EOF'
#include <fcntl.h>
#include <unistd.h>
int lk_mutant_posix(void) { int fd = open("/dev/null", O_WRONLY); ssize_t n = write(fd, "x", 1); close(fd); return (int)n; }
EOF
cat >stdio_write.c <<'EOF'
#include <stdio.h>
int lk_mutant_stdio(void) { FILE *f = fopen("/dev/null", "w"); if (!f) return 1; fputs("x", f); return fclose(f); }
EOF
cat >cf_stream.c <<'EOF'
#include <CoreFoundation/CoreFoundation.h>
const void *lk_mutant_cf(CFURLRef u) { return CFWriteStreamCreateWithFile(NULL, u); }
EOF
cat >DataContents.swift <<'EOF'
import Foundation
public func lkMutantDataContents() throws -> Data { try Data(contentsOf: URL(string: "https://example.invalid/")!) }
EOF
cat >URLSessionTask.swift <<'EOF'
import Foundation
public func lkMutantURLSession() { URLSession.shared.dataTask(with: URL(string: "https://example.invalid/")!).resume() }
EOF
cat >URLRequestMake.swift <<'EOF'
import Foundation
public func lkMutantURLRequest() -> URLRequest { URLRequest(url: URL(string: "https://example.invalid/")!) }
EOF
cat >NetworkConnection.swift <<'EOF'
import Network
public func lkMutantNetwork() -> AnyObject { NWConnection(host: "example.invalid", port: 443, using: .tcp) }
EOF
cat >sockets.c <<'EOF'
#include <netdb.h>
#include <sys/socket.h>
int lk_mutant_socket(void) { struct addrinfo *ai = 0; getaddrinfo("example.invalid", "443", 0, &ai); return socket(AF_INET, SOCK_STREAM, 0); }
EOF
cat >RecognitionTask.swift <<'EOF'
import Speech
public func lkMutantRecognitionTask(_ r: SFSpeechRecognizer) -> AnyObject {
    r.recognitionTask(with: SFSpeechAudioBufferRecognitionRequest()) { _, _ in }
}
EOF
cat >RequestAuthorization.swift <<'EOF'
import Speech
public func lkMutantRequestAuthorization() { SFSpeechRecognizer.requestAuthorization { _ in } }
EOF
cat >AssetDownload.swift <<'EOF'
import Speech
public func lkMutantAssetDownload() async throws { _ = try await AssetInventory.assetInstallationRequest(supporting: []) }
EOF
cat >AudioEngine.swift <<'EOF'
import AVFoundation
public func lkMutantAudioEngine() -> AnyObject { AVAudioEngine() }
EOF
# An Objective-C call on a class the library already uses adds no symbol, only a selector name.
cat >selector_only.m <<'EOF'
#import <AVFAudio/AVFAudio.h>
BOOL lk_mutant_selector(AVAudioPCMBuffer *b) { return [(id)b writeToFile:@"/dev/null" atomically:NO]; }
EOF
cat >hidden_name.c <<'EOF'
const char lk_mutant_hidden[] = "recognitionTask";
EOF
cat >harmless.c <<'EOF'
#include <unistd.h>
int lk_mutant_harmless(void) { return (int)getppid(); }
EOF

TARGET="arm64-apple-macosx26.0"
SWIFT_SOURCES=(*.swift)
xcrun swiftc -c -O -parse-as-library -module-name LKCheckMutants -target "$TARGET" "${SWIFT_SOURCES[@]}" 2>"$WORK/swiftc.log" || {
  cat "$WORK/swiftc.log" >&2
  exit 2
}
for c in *.c; do
  xcrun clang -c -O1 -target "$TARGET" "$c" -o "${c%.c}.o"
done
for m in *.m; do
  xcrun clang -c -O1 -fobjc-arc -fno-objc-msgsend-selector-stubs -target "$TARGET" "$m" -o "${m%.m}.o"
done

fail=0
indent() { sed 's/^/        /'; }
run_check() { # archive -> output in $OUT, status in $RC
  set +e
  OUT="$("$HERE/check-symbols.sh" "$1" 2>&1)"
  RC=$?
  set -e
}

run_check "$ARCHIVE"
if [ "$RC" -ne 0 ]; then
  echo "FAIL  the unmodified archive does not pass check-symbols:"
  indent <<<"$OUT"
  fail=1
else
  echo "ok    unmodified archive passes"
fi

for m in "${MUTANTS[@]}"; do
  IFS='|' read -r name expect src <<<"$m"
  obj="${src%.*}.o"
  if [ ! -f "$obj" ]; then
    echo "FAIL  mutant $name: no object $obj"
    fail=1
    continue
  fi
  libtool -static -o "mutant-$name.a" "$ARCHIVE" "$obj" 2>/dev/null
  run_check "mutant-$name.a"
  if [ "$RC" -eq 1 ] && grep -q -F -- "$expect" <<<"$OUT"; then
    echo "ok    mutant $name: rejected ($expect)"
  else
    echo "FAIL  mutant $name: check-symbols exited $RC without '$expect'"
    indent <<<"$OUT"
    fail=1
  fi
done

# The harmless import must fail only the baseline, not a deny-list category.
libtool -static -o mutant-harmless-only.a "$ARCHIVE" harmless.o 2>/dev/null
run_check mutant-harmless-only.a
if grep -E -q '^FAIL  (networking|server-capable|authorization|model download|microphone|file|the bytes)' <<<"$OUT"; then
  echo "FAIL  a harmless new import tripped a deny-list category:"
  indent <<<"$OUT"
  fail=1
else
  echo "ok    a harmless new import fails the baseline only"
fi

# An archive without the C ABI's Swift object.
mkdir -p members
(cd members && ar -x "$ARCHIVE")
rm -f members/CABI.swift.o members/__.SYMDEF*
libtool -static -o mutant-no-abi.a members/*.o 2>/dev/null
run_check mutant-no-abi.a
if [ "$RC" -eq 1 ] && grep -q -F "FAIL  C ABI not exported" <<<"$OUT"; then
  echo "ok    mutant no-abi: rejected (C ABI not exported)"
else
  echo "FAIL  mutant no-abi: check-symbols exited $RC without 'C ABI not exported'"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "check-symbols-selftest: FAILED"
  exit 1
fi
echo "check-symbols-selftest: passed (${#MUTANTS[@]} mutants + no-abi + harmless-only)"
