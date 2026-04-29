#!/bin/sh
set -eu

mkdir -p "${APP_DATA_DIR:-/app/data}" 2>/dev/null || true

echo "========================================"
echo "  Curatarr Scaffold Container"
echo "  Version: ${PROJECT_VERSION:-dev-local}"
echo "  Build: ${PROJECT_BUILD_NUMBER:-local}"
echo "  Runtime: not implemented"
echo "========================================"

case "${1:-}" in
  --help|-h)
    echo "Curatarr runtime is not implemented yet. See documentation/System Architecture.md."
    exit 0
    ;;
  --version|-v)
    echo "${PROJECT_VERSION:-dev-local}"
    exit 0
    ;;
  "")
    echo "Curatarr runtime is not implemented yet. Container scaffold is healthy."
    exit 0
    ;;
esac

exec "$@"
