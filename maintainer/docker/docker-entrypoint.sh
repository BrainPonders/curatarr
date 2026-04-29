#!/bin/sh
set -eu

mkdir -p "${APP_DATA_DIR:-/app/data}" 2>/dev/null || true

echo "========================================"
echo "  Curatarr Container"
echo "  Version: ${PROJECT_VERSION:-dev-local}"
echo "  Build: ${PROJECT_BUILD_NUMBER:-local}"
echo "  Config: ${CURATARR_CONFIG_FILE:-/app/config.yaml}"
echo "========================================"

case "${1:-}" in
  --help|-h)
    if command -v curatarr >/dev/null 2>&1; then
      exec curatarr --help
    fi
    echo "Curatarr command is not installed in this shell. In the container, this runs curatarr --help."
    exit 0
    ;;
  --version|-v)
    if command -v curatarr >/dev/null 2>&1; then
      exec curatarr --version
    fi
    echo "${PROJECT_VERSION:-dev-local}"
    exit 0
    ;;
  "")
    if ! command -v curatarr >/dev/null 2>&1; then
      echo "ERROR: curatarr command is not installed."
      exit 127
    fi
    exec curatarr --config "${CURATARR_CONFIG_FILE:-/app/config.yaml}" --check-config
    ;;
esac

exec "$@"
