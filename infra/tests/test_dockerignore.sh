#!/usr/bin/env bash
# Checks what infra/lambda/Dockerfile.dockerignore lets into the Lambda build context, using BuildKit itself.
#
# It lays out a fake repo (allowed packages plus data, docs, secrets and caches that must stay out), puts the real
# ignore file next to a throwaway `FROM scratch` Dockerfile at infra/lambda/Dockerfile, and exports the context
# with `--output type=local`. No image is pulled or built; nothing is pushed.
#
# Needs a running Docker daemon with buildx. Without one it prints SKIP and exits 0.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DOCKER="${DOCKER:-docker}"

echo "infra/tests/test_dockerignore.sh"
if ! command -v "$DOCKER" >/dev/null 2>&1 || ! "$DOCKER" buildx version >/dev/null 2>&1 || ! "$DOCKER" info >/dev/null 2>&1; then
  echo "  SKIP no running Docker daemon with buildx"
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/lookout-dockerignore.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
CTX="$WORK/repo"

put() { mkdir -p "$(dirname "$CTX/$1")"; printf 'x\n' >"$CTX/$1"; }

# Must reach the builder.
want="
viewpoints/manifests/rainier.paradise-mountain.json
viewpoints/schema/viewpoint.schema.json
workers/capture/README.md
workers/capture/pyproject.toml
workers/capture/src/lookout_capture/__init__.py
workers/capture/src/lookout_capture/lambda_handler.py
workers/capture/uv.lock
workers/common/README.md
workers/common/pyproject.toml
workers/common/src/lookout_common/__init__.py
workers/common/src/lookout_common/py.typed
workers/common/src/lookout_common/schema.json
workers/common/uv.lock
workers/forecast/pyproject.toml
workers/forecast/src/lookout_forecast/lambda_handler.py
workers/forecast/uv.lock
"
# Must not.
unwanted="
.env
.git/config
CLAUDE.md
PRD.md
docs/research/forecast.md
eval/pyproject.toml
renderer/package.json
app/core/Cargo.toml
infra/terraform/main.tf
viewpoints/plates/rainier/base.webp
viewpoints/notes.md
workers/.DS_Store
workers/other/pyproject.toml
workers/common/.venv/lib/python3.13/site-packages/x.py
workers/common/.ruff_cache/CACHEDIR.TAG
workers/common/.pytest_cache/v/cache/nodeids
workers/common/src/lookout_common/__pycache__/x.cpython-313.pyc
workers/common/tests/test_storage.py
workers/common/tests/fixtures/sample.json
workers/forecast/tests/helpers.py
workers/capture/src/lookout_capture/test_poll.py
workers/capture/src/lookout_capture/conftest.py
workers/capture/dist/lookout_capture-0.1.0-py3-none-any.whl
workers/capture/.env
workers/capture/.env.local
workers/capture/frame.jpg
workers/forecast/cache/hrrr.t12z.wrfsfcf00.grib2
workers/forecast/cache/hrrr.t12z.wrfsfcf00.grib2.idx
workers/forecast/forecast_log.jsonl
viewpoints/manifests/.DS_Store
"
for f in $want $unwanted; do put "$f"; done

mkdir -p "$CTX/infra/lambda"
printf 'FROM scratch\nCOPY . /\n' >"$CTX/infra/lambda/Dockerfile"
cp "$REPO/infra/lambda/Dockerfile.dockerignore" "$CTX/infra/lambda/Dockerfile.dockerignore"

BUILDX_GIT_INFO=0 "$DOCKER" buildx build --quiet --file "$CTX/infra/lambda/Dockerfile" --output "type=local,dest=$WORK/out" "$CTX" >/dev/null

got="$(cd "$WORK/out" && find . -type f | sed 's|^\./||' | LC_ALL=C sort)"
expected="$(printf '%s\n' "$want" | sed '/^$/d' | LC_ALL=C sort)"

if [ "$got" != "$expected" ]; then
  echo "  FAIL build context differs from the allow-list"
  diff <(printf '%s\n' "$expected") <(printf '%s\n' "$got") | sed 's/^/    /' || true
  echo "    (< expected only, > sent to the builder only)"
  exit 1
fi
echo "  ok   context holds exactly the $(printf '%s\n' "$got" | wc -l | tr -d ' ') allowed files; $(printf '%s\n' "$unwanted" | sed '/^$/d' | wc -l | tr -d ' ') decoys kept out"
