#!/usr/bin/env bash
# Usage: ./run_module1.sh PT09
set -e
cd "$(dirname "$0")"
uv run python -m modules.module1_deterministic.run "$1"
