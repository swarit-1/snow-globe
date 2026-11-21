#!/usr/bin/env bash
# Build the Snow Globe Lambda image for linux/arm64 (PRD §11.3). Context: the repo root, filtered by
# infra/lambda/Dockerfile.dockerignore.
#
# Usage:
#   infra/lambda/build.sh                  build and load into the local Docker as snow-globe-lambda:<tag>
#   infra/lambda/build.sh --push REPO_URL  build and push REPO_URL:<tag> (log Docker in to ECR first;
#                                          infra/deploy.sh does that)
#   infra/lambda/build.sh --print-tag      print the tag this tree would get, and exit
#   infra/lambda/build.sh --check          check the build inputs and Docker, build nothing
#   infra/lambda/build.sh --tag TAG ...    use TAG instead of the git-derived one
#
# The tag is the 12-character git commit, plus "-dirty-<UTC time>" when anything that goes into the image
# (workers/, viewpoints/, infra/lambda/) has uncommitted changes. Every build therefore gets a new tag: the ECR
# repository is IMMUTABLE, and Lambda pins the image digest at deploy time.
#
# The last line on stdout is always "image_tag=<tag>". Progress goes to stderr.
#
# Exit codes: 0 ok, 2 usage error, 3 the worker packages, their Lambda handlers or the manifests are missing,
# 4 Docker is unavailable. The Dockerfile also imports both handlers, so a handler that cannot load fails the build.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKERFILE="infra/lambda/Dockerfile"
LOCAL_NAME="snow-globe-lambda"
PLATFORM="linux/arm64"
DOCKER="${DOCKER:-docker}"

log() { printf 'build.sh: %s\n' "$*" >&2; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}" >&2; }

push_repo=""
tag=""
print_tag=0
check_only=0
while [ $# -gt 0 ]; do
  case "$1" in
    --push)
      [ $# -ge 2 ] || { log "--push needs the ECR repository URL"; exit 2; }
      push_repo="$2"; shift 2 ;;
    --tag)
      [ $# -ge 2 ] || { log "--tag needs a value"; exit 2; }
      tag="$2"; shift 2 ;;
    --print-tag) print_tag=1; shift ;;
    --check) check_only=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) log "unknown argument: $1"; usage; exit 2 ;;
  esac
done

# Tag: <commit> or <commit>-dirty-<UTC time>. Only the paths that end up in the image decide "dirty".
derive_tag() {
  local sha dirty
  if ! sha="$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null)"; then
    sha="nogit"
  fi
  dirty="$(git -C "$ROOT" status --porcelain --untracked-files=normal -- workers viewpoints infra/lambda 2>/dev/null || true)"
  if [ -n "$dirty" ] || [ "$sha" = "nogit" ]; then
    printf '%s-dirty-%s\n' "$sha" "$(date -u +%Y%m%dT%H%M%SZ)"
  else
    printf '%s\n' "$sha"
  fi
}

if [ -z "$tag" ]; then
  tag="$(derive_tag)"
fi
case "$tag" in
  *[!A-Za-z0-9._-]* | "" | [.-]*)
    log "invalid tag '$tag': use letters, digits, '.', '_' or '-', not starting with '.' or '-'"; exit 2 ;;
esac
if [ "${#tag}" -gt 128 ]; then
  log "tag is longer than 128 characters"; exit 2
fi

if [ "$print_tag" -eq 1 ]; then
  printf 'image_tag=%s\n' "$tag"
  exit 0
fi

# The image needs all three worker packages, the two lock files, at least one manifest, and the two handler
# modules Terraform points the functions at (infra/terraform/main.tf, local.jobs): without one, every run of that
# function would fail with Runtime.ImportModuleError.
missing=""
for f in workers/common/pyproject.toml \
         workers/capture/pyproject.toml workers/capture/uv.lock \
         workers/capture/src/snow_globe_capture/lambda_handler.py \
         workers/forecast/pyproject.toml workers/forecast/uv.lock \
         workers/forecast/src/snow_globe_forecast/lambda_handler.py \
         viewpoints/schema/viewpoint.schema.json; do
  [ -f "$ROOT/$f" ] || missing="$missing $f"
done
if ! ls "$ROOT"/viewpoints/manifests/*.json >/dev/null 2>&1; then
  missing="$missing viewpoints/manifests/*.json"
fi
if [ -n "$missing" ]; then
  log "cannot build yet; missing:$missing"
  log "the worker packages, their Lambda handlers and the manifests must exist first (the integration step builds the image)."
  exit 3
fi

if ! command -v "$DOCKER" >/dev/null 2>&1; then
  log "docker not found"; exit 4
fi
if ! "$DOCKER" buildx version >/dev/null 2>&1; then
  log "docker buildx is not available"; exit 4
fi
if ! "$DOCKER" info >/dev/null 2>&1; then
  log "the Docker daemon is not running (start Docker Desktop)"; exit 4
fi

if [ "$check_only" -eq 1 ]; then
  log "build inputs and Docker are ready"
  printf 'image_tag=%s\n' "$tag"
  exit 0
fi

if [ -n "$push_repo" ]; then
  image="$push_repo:$tag"
  output="--push"
else
  image="$LOCAL_NAME:$tag"
  output="--load"
fi

log "building $image for $PLATFORM from $ROOT"
cd "$ROOT"
"$DOCKER" buildx build \
  --platform "$PLATFORM" \
  --provenance=false \
  --sbom=false \
  --file "$DOCKERFILE" \
  --tag "$image" \
  "$output" \
  . >&2

log "done: $image"
printf 'image_tag=%s\n' "$tag"
