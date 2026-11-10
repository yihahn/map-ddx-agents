#!/usr/bin/env bash
# Run the Narrative step on several patients, PARALLEL at a time, all on the Qwen host named by QWEN_SERVER
# (default GPU200, which admits 8 concurrent requests: each patient uses at most 2 at once). Logs go to
# batch_logs/narrative_<PID>.log; outputs to modules/narrative/output/<PID>_<ts>/. With RESUME=1 each patient
# continues its newest run directory instead (collapse files and valid reason runs there are reused).
# ARGS passes extra flags to every run (e.g. ARGS="--reason-only").
# Usage: ./run_batch_narrative.sh PT01 PT03 PT04 ...   (PARALLEL=3 by default; RESUME=1 to continue)
set -u
cd "$(dirname "$0")"
mkdir -p batch_logs
PARALLEL="${PARALLEL:-3}"
RESUME="${RESUME:-0}"
ARGS="${ARGS:-}"
one() {
  pid="$1"; args=($ARGS)
  if [ "$RESUME" = "1" ]; then
    dir=$(ls -d "modules/narrative/output/${pid}_"* 2>/dev/null | sort | tail -1)
    [ -n "$dir" ] && args+=(--run-dir "$dir")
  fi
  echo "[$(date +%H:%M:%S)] start $pid ${args[*]:-}"
  uv run python -m modules.narrative.run "$pid" "${args[@]}" > "batch_logs/narrative_${pid}.log" 2>&1
  echo "[$(date +%H:%M:%S)] done $pid (exit $?)"
}
export -f one
export RESUME ARGS
printf '%s\n' "$@" | xargs -P "$PARALLEL" -I{} bash -c 'one {}'
