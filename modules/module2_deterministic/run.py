import argparse
import json
from pathlib import Path

from .graph import module2_app
from .llm import get_langfuse_handler

# Input: a PT## patient id (positional arg), resolved to pending_diag/ddx_vignette/<PID>_ddx_vignette_v1.md
# (main vignette only, not the *_appendix.md QA file). Output: prints the final_ddx_list and
# the saved JSON path (also written by the aggregate node itself). Algorithm: read the vignette
# markdown as plain text, invoke module2_app with a Langfuse callback attached for tracing, then
# report the result.

VIGNETTE_DIR = Path(__file__).resolve().parents[2] / "pending_diag" / "ddx_vignette"


def run(patient_id: str) -> dict:
    vignette_path = VIGNETTE_DIR / f"{patient_id}_ddx_vignette_v1.md"
    vignette = vignette_path.read_text(encoding="utf-8")

    result = module2_app.invoke(
        {"vignette": vignette, "patient_id": patient_id},
        config={"callbacks": [get_langfuse_handler()]},
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Module 2 (deterministic) on one patient.")
    parser.add_argument("patient", help="Patient id, e.g. PT09")
    args = parser.parse_args()

    result = run(args.patient)
    print(json.dumps([item.model_dump() for item in result["final_ddx_list"]], ensure_ascii=False, indent=2))
    print(f"\nSaved to: {result['output_path']}")


if __name__ == "__main__":
    main()
