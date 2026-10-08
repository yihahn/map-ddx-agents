#!/usr/bin/env bash
# Collapse (v3) on existing Module 3 runs, split across two Qwen hosts: patients alternate between a GPU200
# queue (3 patients at a time — each whole-list judgement holds 2 of its ~8 decode streams) and an INFER
# (A5000) queue (1 at a time — 2 requests per key). QWEN_SERVER sets the host and concurrency (llm.py).
# Usage: ./run_batch_collapse_split.sh [PT01 PT03 ...] (default: all 9). One log per patient.
cd "$(dirname "$0")"
mkdir -p batch_logs
TS=$(date +%Y%m%d_%H%M%S)
(( $# )) || set -- PT01 PT03 PT04 PT05 PT06 PT07 PT09 PT10 PT11

run_one() {  # $1 = QWEN_SERVER, $2 = patient
  {
    echo "=== $2 on $1 collapse $(date +%H:%M:%S) ==="
    QWEN_SERVER=$1 uv run python -m modules.module3_deterministic.collapse "$2" || echo "FAILED $2 collapse"
    echo "=== DONE $2 $(date +%H:%M:%S) ==="
  } >"batch_logs/collapse_v3_${2}_${TS}.log" 2>&1
}
export -f run_one
export TS

GPU200=(); INFER=()
for i in $(seq 1 $#); do
  if (( i % 2 )); then GPU200+=("${!i}"); else INFER+=("${!i}"); fi
done
echo "GPU200: ${GPU200[*]}"
echo "INFER:  ${INFER[*]}"
printf '%s\n' "${GPU200[@]}" | xargs -P 3 -I{} bash -c 'run_one GPU200 {}' &
printf '%s\n' "${INFER[@]}" | xargs -P 1 -I{} bash -c 'run_one INFER {}' &
wait
