#!/usr/bin/env bash
# Usage: ./run_module2.sh PT09
set -e
cd "$(dirname "$0")"
uv run python -m modules.module2_deterministic.run "$1"
