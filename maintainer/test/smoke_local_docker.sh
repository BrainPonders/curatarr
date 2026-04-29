#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_TAG="${PROJECT_SMOKE_IMAGE:-curatarr:smoke-local}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed."
  exit 1
fi

echo "== Build smoke image =="
docker build \
  --build-arg BUILD_NUMBER="smoke-local" \
  --build-arg VERSION="smoke-local" \
  -t "$IMAGE_TAG" \
  -f "$ROOT_DIR/maintainer/docker/Dockerfile" \
  "$ROOT_DIR"

echo "== Check version output =="
docker run --rm "$IMAGE_TAG" --version >/dev/null

echo "== Check help output =="
docker run --rm "$IMAGE_TAG" --help >/dev/null

echo "== Check missing config fails cleanly =="
docker run --rm "$IMAGE_TAG" >/dev/null 2>&1 && {
  echo "ERROR: container should require a config file."
  exit 1
}

echo "Smoke checks passed."
