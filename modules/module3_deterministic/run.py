import argparse
import json
from datetime import datetime
from pathlib import Path

from schema import DDxItem

from .graph import OUTPUT_DIR, module3_app
from ..backends import MAX_CONCURRENCY
from .llm import get_langfuse_handler

# Input: a PT## patient id, resolved against the newest Module 1 and Module 2 run directories for
# that patient (--limit caps how many merged diagnoses are verified, for cheap trial runs). Output:
# prints the refined DDx list, the diagnoses the record ruled out (written separately to
# decline_ddx_list.json), the ones dropped for naming a drug the record never mentions
# (medication_filtered_ddx_list.json), the workup gaps, how much of the EMR each diagnosis was
# actually checked against, and the run directory each stage's JSON went to.
# Algorithm: locate the latest modules/module{1,2}_deterministic/output/<PID>_*/ run, load their
# final DDx JSON back into DDxItem, create a fresh output/<PID>_<timestamp>/ directory, and invoke
# module3_app with a Langfuse callback; --limit is applied to the merged list by truncating
# prelim_ddx_list before fan-out, so the verification work stays proportional to it.

# How many branches may run at once — the active backend's own capacity (modules/backends.py): 12
# on gpu200, 2 on infer:11239, which refuses a third request in flight with 429. Fan-out here is
# wider than either on its own — five specialties, one search per plan, one subgraph per diagnosis
# — so without a cap the branches reject each other, and a branch that dies on 429 is
# indistinguishable in the output from one the record could not settle.

MODULES_DIR = Path(__file__).resolve().parents[1]
MODULE1_OUTPUT = MODULES_DIR / "module1_deterministic" / "output"
MODULE2_OUTPUT = MODULES_DIR / "module2_deterministic" / "output"


def _latest_output(output_dir: Path, patient_id: str, filename: str) -> list[DDxItem] | None:
    runs = sorted(output_dir.glob(f"{patient_id}_*"))
    for run_dir in reversed(runs):
        path = run_dir / filename
        if path.exists():
            return [DDxItem(**d) for d in json.loads(path.read_text(encoding="utf-8"))]
    return None


def run(patient_id: str, limit: int | None = None) -> dict:
    module1_ddx = _latest_output(MODULE1_OUTPUT, patient_id, "05_final_ddx_list.json")
    module2_ddx = _latest_output(MODULE2_OUTPUT, patient_id, "04_final_ddx_list.json")
    if module1_ddx is None and module2_ddx is None:
        raise SystemExit(f"No Module 1 or Module 2 output found for {patient_id}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / f"{patient_id}_{timestamp}"
    run_dir.mkdir(parents=True)

    return module3_app.invoke(
        {
            "patient_id": patient_id,
            "run_dir": str(run_dir),
            "module1_ddx": module1_ddx,
            "module2_ddx": module2_ddx,
            "limit": limit,
        },
        config={"callbacks": [get_langfuse_handler()], "max_concurrency": MAX_CONCURRENCY},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Module 3 (deterministic) on one patient.")
    parser.add_argument("patient", help="Patient id, e.g. PT09")
    parser.add_argument("--limit", type=int, default=None, help="Verify only the first N diagnoses")
    args = parser.parse_args()

    result = run(args.patient, args.limit)
    print(json.dumps([i.model_dump() for i in result["final_ddx_list"]], ensure_ascii=False, indent=2))
    declined = result.get("declined_ddx_list") or []
    print(f"\nDeclined ({len(declined)}, in decline_ddx_list.json): "
          f"{', '.join(i.diagnosis_name for i in declined) or 'none'}")

    # Removed for want of the drug they are named after, which is a different claim from declined:
    # no criterion was weighed, so the reason is printed with each one.
    drug_filtered = result.get("medication_filtered_ddx_list") or []
    reasons = {d["diagnosis_name"]: d["substance"] for d in result.get("medication_filter") or []}
    print(f"Filtered on medication ({len(drug_filtered)}, in medication_filtered_ddx_list.json): "
          f"{', '.join(f'{i.diagnosis_name} [{reasons.get(i.diagnosis_name)}]' for i in drug_filtered) or 'none'}")
    print(f"Workup gaps: {len(result['final_gap_list'])}")

    # How hard each branch actually looked. A diagnosis judged off two lookups was not really
    # checked against the record, and nothing else printed here would say so.
    print("\nEMR consulted per diagnosis (full trail in 06_verification_log.json):")
    for entry in result.get("verification_log") or []:
        print(
            f"  {entry['diagnosis_name']}: {len(entry['documents_supplied'])} documents supplied, "
            f"{entry['lookups']} further lookups reading {len(entry['documents_read'])}"
        )

    print(f"\nRun directory: {result['run_dir']}")


if __name__ == "__main__":
    main()
