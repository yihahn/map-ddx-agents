import argparse
import json
from collections import Counter
from pathlib import Path

from .collapse import MODULE3_OUTPUT

# Input: PT## patient ids; for each, the newest run under --output-root (default: Module 3 output) with a
# v3 collapse log (an "organize" key), scored against eval/gold_hierarchy.json.
# Output: per-stage counts, precision/recall and the wrong verdicts, printed to stdout. Algorithm: a gold
# subtype pair (both names in the patient's final forest) counts "yes" when the parent is an ancestor of the
# child in the forest (same/subtype alike) and undecided when the child's placement was disputed; gold
# non-diagnoses are checked against the excluded list; decline pairs against the logged decline verdicts.
# Codes are not scored: a MONDO code is a canonical-label match, correct by construction.

EVAL_DIR = Path(__file__).resolve().parent / "eval"


def _latest_log(patient_id: str, root: Path) -> tuple[Path, dict]:
    for run_dir in sorted(root.glob(f"{patient_id}_*"), reverse=True):
        path = run_dir / "07_collapse_log.json"
        if path.exists():
            log = json.loads(path.read_text(encoding="utf-8"))
            if "organize" in log:
                return run_dir, log
    raise SystemExit(f"No v3 collapse log found for {patient_id}")


def _binary(name: str, rows: list[tuple[str, object, object]]) -> None:
    """rows: (label, gold bool, predicted verdict 'yes'/'no'/other)."""
    c = Counter()
    wrong = []
    for label, gold, verdict in rows:
        if verdict not in ("yes", "no"):
            c["undecided"] += 1
            wrong.append(f"  undecided ({verdict}): {label}")
            continue
        pred = verdict == "yes"
        c[("tp" if gold else "fp") if pred else ("fn" if gold else "tn")] += 1
        if pred != gold:
            wrong.append(f"  {'false yes' if pred else 'false no'}: {label}")
    precision = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else float("nan")
    recall = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else float("nan")
    print(f"\n[{name}] yes→yes {c['tp']}, no→no {c['tn']}, false yes {c['fp']}, false no {c['fn']}, "
          f"undecided {c['undecided']} | precision {precision:.3f} recall {recall:.3f}")
    print("\n".join(wrong) if wrong else "  (no errors)")


def _ancestors_in_forest(roots: list[dict]) -> dict[str, set[str]]:
    """lower-cased name -> lower-cased names above it in the forest."""
    out: dict[str, set[str]] = {}

    def walk(nodes, above):
        for n in nodes:
            out[n["diagnosis_name"].lower()] = set(above)
            walk(n["children"], above + [n["diagnosis_name"].lower()])
    walk(roots, [])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Score v3 collapse results against the gold evaluation set.")
    parser.add_argument("patients", nargs="+", help="Patient ids, e.g. PT01 PT05")
    parser.add_argument("--output-root", type=Path, default=MODULE3_OUTPUT,
                        help="Directory holding the <PID>_<ts> run dirs (default: Module 3 output)")
    args = parser.parse_args()

    gold_h = json.loads((EVAL_DIR / "gold_hierarchy.json").read_text(encoding="utf-8"))
    gold_sub = {k: v for k, v in gold_h["subtype"].items() if v["status"] == "decided"}
    gold_dec = {(d["patient"], d["child"].lower(), d["declined_ancestor"].lower()): d for d in gold_h["decline"]}
    gold_nondx = {k.lower(): v for k, v in gold_h["nondx"].items() if v["status"] == "decided"}

    placement_rows, nondx_rows, decline_rows = [], [], []
    for patient in args.patients:
        run_dir, log = _latest_log(patient, args.output_root)
        print(f"{patient}: {run_dir.name} (organize {log['organize']['status']})")
        above = _ancestors_in_forest(json.loads((run_dir / "07_grouped_ddx_list.json").read_text(encoding="utf-8")))
        disputed = {d["name"].lower() for d in log["organize"]["disputed"]}
        nondx_disputed = {d["name"].lower() for d in log["organize"]["disputed"] if "not a diagnosis" in d["proposals"]}
        excluded = {x["name"].lower() for x in log["organize"]["excluded"]}
        for key, g in gold_sub.items():
            child, parent = key.split("||")
            if child in above and parent in above:
                verdict = "yes" if parent in above[child] else ("split" if child in disputed else "no")
                placement_rows.append((f"{patient} {g['child']} < {g['parent']}", g["is_subtype"], verdict))
        for name in list(above) + list(excluded):
            g = gold_nondx.get(name)
            if g:
                verdict = "yes" if name in excluded else ("split" if name in nondx_disputed else "no")
                nondx_rows.append((f"{patient} {name}", not g["is_diagnosis"], verdict))
        for e in log["decline"]:
            key = (patient, e["child"].lower(), e["declined_ancestor"].lower())
            if key in gold_dec:
                decline_rows.append((f"{patient} {e['child']} <- {e['declined_ancestor']}", gold_dec[key]["inherits"], e["verdict"]))

    _binary("placement (child under gold parent)", placement_rows)
    _binary("nondx (yes = not a diagnosis)", nondx_rows)
    _binary("decline inheritance", decline_rows)


if __name__ == "__main__":
    main()
