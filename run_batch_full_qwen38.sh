#!/usr/bin/env bash
# Full chain (module1 -> module2 -> module3 -> collapse) on Qwen3.8-27B, split across two hosts: patients
# alternate between a GPU200 queue and an INFER (A5000) queue, which run side by side; each queue takes
# its patients one at a time, and QWEN_SERVER sets the host and its request concurrency (llm.py). The two
# hosts judged the collapse bench equally well (2026-10-06); DGX is left out until its setup is checked.
# Usage: ./run_batch_full_qwen38.sh [PT01 PT03 ...] (default: all 9). One log per patient.
cd "$(dirname "$0")"
mkdir -p batch_logs
TS=$(date +%Y%m%d_%H%M%S)
(( $# )) || set -- PT01 PT03 PT04 PT05 PT06 PT07 PT09 PT10 PT11

run_queue() {  # $1 = QWEN_SERVER, then the patients for that host
  local server=$1; shift
  for P in "$@"; do
    (
      export QWEN_SERVER=$server
      echo "=== $P on $server module1 $(date +%H:%M:%S) ==="
      uv run python -m modules.module1_deterministic.run "$P" || { echo "FAILED $P module1"; exit 1; }
      echo "=== $P module2 $(date +%H:%M:%S) ==="
      uv run python -m modules.module2_deterministic.run "$P" || { echo "FAILED $P module2"; exit 1; }
      echo "=== $P module3 $(date +%H:%M:%S) ==="
      uv run python -m modules.module3_deterministic.run "$P" || { echo "FAILED $P module3"; exit 1; }
      echo "=== $P collapse $(date +%H:%M:%S) ==="
      uv run python -m modules.module3_deterministic.collapse "$P" || { echo "FAILED $P collapse"; exit 1; }
      echo "=== DONE $P $(date +%H:%M:%S) ==="
    ) >"batch_logs/full_qwen38_${P}_${TS}.log" 2>&1
  done
}

GPU200=(); INFER=()
for i in $(seq 1 $#); do
  if (( i % 2 )); then GPU200+=("${!i}"); else INFER+=("${!i}"); fi
done
echo "GPU200: ${GPU200[*]}"
echo "INFER:  ${INFER[*]}"
run_queue GPU200 "${GPU200[@]}" &
run_queue INFER "${INFER[@]}" &
wait
