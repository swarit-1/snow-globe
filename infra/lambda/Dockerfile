# Lookout Lambda image (PRD §11.2, §11.3): one arm64 image for every scheduled job.
#   capture  -> lookout_capture.lambda_handler.handler
#   forecast -> lookout_forecast.lambda_handler.handler    (the default CMD below)
# Terraform sets each function's handler with image_config.command (infra/terraform/lambda.tf), so the default CMD
# only matters for a local `docker run`. It is the forecast handler, which reads only NOAA's public bucket: a local
# run that forgets the command must never become a second NPS fetcher (CLAUDE.md, PRD §15.3).
# infra/lambda/smoke.sh tests the image locally without ever invoking the capture handler.
#
# Build context is the repo root. infra/lambda/Dockerfile.dockerignore (BuildKit reads it because it sits next to
# this file, and it overrides any root .dockerignore) lets in only workers/{common,capture,forecast} and
# viewpoints/{manifests,schema}: no data, no docs, no tests. Build with infra/lambda/build.sh, which runs
#   docker buildx build --platform linux/arm64 --provenance=false -f infra/lambda/Dockerfile .
# (Lambda rejects images built with provenance attestations.)

# Both bases are pinned by digest, so one commit always builds the same image. AWS republishes the python:3.13 tag
# often (two builds 12 minutes apart on 20 Nov 2026 got sha256:683d87bf... and then sha256:41cce764...). The digest
# below is the multi-platform index; its linux/arm64 image is Python 3.13.18. To take AWS's patches, bump it on
# purpose before a deploy (infra/README.md, "The Lambda image").
ARG PYTHON_BASE=public.ecr.aws/lambda/python:3.13@sha256:41cce7642ee5093340804000b66a79c91d5668ad66449c4a0ece277b4912a1ce
# Same uv release as the dev toolchain (uv 0.11.6), so `uv export` reads the lock files the same way.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754

FROM ${UV_IMAGE} AS uv

# ---- builder: resolve pinned dependencies into ${LAMBDA_TASK_ROOT} -------------------------------------------
FROM ${PYTHON_BASE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_INSTALLER_METADATA=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build
COPY workers/common workers/common
COPY workers/capture workers/capture
COPY workers/forecast workers/forecast

# 1. Third-party dependencies, exactly as pinned in the capture and forecast lock files. lookout-common reaches
#    them as a path dependency, so it is left out of the export and installed in step 2. If the two lock files
#    pin different versions of a package, the install fails here instead of picking one silently.
# 2. The three local packages, built as wheels (not editable), without re-resolving dependencies.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    for job in capture forecast; do \
      uv export --project "workers/${job}" --frozen --no-dev --no-editable \
        --no-emit-project --no-emit-package lookout-common \
        --output-file "/build/requirements-${job}.txt"; \
    done; \
    uv pip install --python python3.13 --target "${LAMBDA_TASK_ROOT}" \
      -r /build/requirements-capture.txt -r /build/requirements-forecast.txt; \
    uv pip install --python python3.13 --target "${LAMBDA_TASK_ROOT}" --no-deps \
      ./workers/common ./workers/capture ./workers/forecast

# ---- runtime -----------------------------------------------------------------------------------------------------
FROM ${PYTHON_BASE}

# Lambda's file system is read-only except /tmp. HOME points there for libraries that write under ~ (config,
# caches). The two LOOKOUT_ paths match what Terraform sets, so `docker run` with the Lambda runtime interface
# emulator behaves like the deployed functions.
ENV HOME=/tmp \
    LOOKOUT_DATA_DIR=/tmp/lookout-data \
    LOOKOUT_MANIFEST_DIR=/var/task/viewpoints/manifests

COPY --from=builder ${LAMBDA_TASK_ROOT} ${LAMBDA_TASK_ROOT}
COPY viewpoints/manifests ${LAMBDA_TASK_ROOT}/viewpoints/manifests
# The schema travels with the manifests in case the loader validates them at start-up.
COPY viewpoints/schema ${LAMBDA_TASK_ROOT}/viewpoints/schema

# Smoke test: import both handlers the way Lambda will (from LAMBDA_TASK_ROOT), so a missing module or a
# dependency that cannot load (eccodes, say) fails the build instead of every scheduled run
# (Runtime.ImportModuleError). Nothing runs: importing a handler module calls no handler.
# The base image ships its own boto3 and botocore in /var/lang (1.43.127 in the pinned base; the lock files pin
# 1.44.6). The runtime puts LAMBDA_TASK_ROOT first on sys.path, so the locked copies win. The check fails the build
# if boto3 or botocore ever drops out of the lock files, which would silently swap in the base image's version.
RUN PYTHONPATH="${LAMBDA_TASK_ROOT}" PYTHONDONTWRITEBYTECODE=1 python3 -c \
    "import boto3, botocore, lookout_capture.lambda_handler as c, lookout_forecast.lambda_handler as f; assert callable(c.handler) and callable(f.handler); assert all(m.__file__.startswith('$LAMBDA_TASK_ROOT/') for m in (boto3, botocore)), (boto3.__file__, botocore.__file__)"

CMD ["lookout_forecast.lambda_handler.handler"]
