#!/usr/bin/env bash
# Usage: ./run_module3.sh PT09 [--limit 5]
set -e
cd "$(dirname "$0")"
uv run python -m modules.module3_deterministic.run "$@"
