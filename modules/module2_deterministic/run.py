import argparse
import json
from datetime import datetime
from pathlib import Path

from .graph import OUTPUT_DIR, module2_app
from .llm import get_langfuse_handler

# Input: a PT## patient id (positional arg), resolved to pending_diag/ddx_vignette/<PID>_ddx_vignette_v1.md
# (main vignette only, not the *_appendix.md QA file). Output: prints the final_ddx_list and the
# run directory each stage's intermediate JSON was written to. Algorithm: create a fresh
# output/<PID>_<YYYYMMDD_HHMMSS>/ run directory, read the vignette markdown as plain text, invoke
# module2_app (passing run_dir through state so each node writes its own stage file there) with a
# Langfuse callback attached for tracing, then report the result.

VIGNETTE_DIR = Path(__file__).resolve().parents[2] / "pending_diag" / "ddx_vignette"


def run(patient_id: str) -> dict:
    vignette_path = VIGNETTE_DIR / f"{patient_id}_ddx_vignette_v1.md"
    vignette = vignette_path.read_text(encoding="utf-8")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / f"{patient_id}_{timestamp}"
    run_dir.mkdir(parents=True)

    result = module2_app.invoke(
        {"vignette": vignette, "patient_id": patient_id, "run_dir": str(run_dir)},
        config={"callbacks": [get_langfuse_handler()]},
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Module 2 (deterministic) on one patient.")
    parser.add_argument("patient", help="Patient id, e.g. PT09")
    args = parser.parse_args()

    result = run(args.patient)
    print(json.dumps([item.model_dump() for item in result["final_ddx_list"]], ensure_ascii=False, indent=2))
    print(f"\nRun directory: {result['run_dir']}")


if __name__ == "__main__":
    main()
