#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/core-linux-embedded-binaries-build-pipeline.py "$@"
