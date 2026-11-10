import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import httpx

from modules.module3_deterministic.collapse import collapse

from . import passes
from .preprocess import build_input, input_text, known_ids, load_report, to_ddx_items

# Input: a PT## patient id, resolved to reports/report_v7_1031/<PID>_pipeline_report_v7_1031.html (or --report).
# Output: modules/narrative/output/<PID>_<ts>/ with the collapse files (collapse/07_*.json), the Narrative input
# (01_narrative_input.json/.txt), every reason run (02_reason_runN.json), the DDx Narrative (03_narrative.md)
# and 04_checks.json (agreement, unknown ids, writer checks, token count, the patient reference standard for
# post-hoc review only). Algorithm: parse the report, write its DDx items as a Module 3 run so collapse() can
# group same-hypothesis names and inherit declines unchanged, build the input, run the reason pass twice, write
# up the first valid run, append the cited evidence. --no-collapse keeps every name apart; --input-only stops
# after the input so preprocessing can be checked on every patient without an LLM call; --run-dir continues an
# earlier run directory, reusing its collapse files and its reason runs when all of them are valid, so a later
# stage can be rerun without repeating the earlier ones; --reason-only stops after the reason pass.

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports" / "report_v7_1031"


def _count_tokens(text: str) -> int | None:
    """Token count from the configured Qwen host's /tokenize endpoint; None when it is unavailable."""
    from modules.module3_deterministic.llm import BACKENDS, DEFAULT_BACKEND
    url, model, key_env = BACKENDS[DEFAULT_BACKEND]
    base = url.rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    try:
        r = httpx.post(base + "/tokenize", headers={"Authorization": f"Bearer {os.environ[key_env]}"},
                       json={"model": model, "prompt": text}, timeout=60)
        return r.json()["count"] if r.status_code == 200 else None
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_collapse(cdir: Path) -> dict:
    """The dict collapse() returned, read back from the four files it wrote."""
    files = {"groups": "07_grouped_ddx_list.json", "declined": "07_decline_ddx_list.json",
             "excluded": "07_excluded_ddx_list.json", "log": "07_collapse_log.json"}
    return {key: json.loads((cdir / name).read_text(encoding="utf-8")) for key, name in files.items()}


def run(patient_id: str, report_path: Path, use_collapse: bool, input_only: bool,
        run_dir: Path | None = None, reason_only: bool = False) -> Path:
    report = load_report(report_path)
    if run_dir is None:
        run_dir = OUTPUT_DIR / f"{patient_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run_dir.mkdir(parents=True)
    print(f"Run directory: {run_dir}", flush=True)

    groups, declined_extra = None, None
    if use_collapse:
        cdir = run_dir / "collapse"
        if (cdir / "07_collapse_log.json").exists():
            result = _load_collapse(cdir)
            print("collapse: reusing the files already in the run directory", flush=True)
        else:
            cdir.mkdir()
            _dump(cdir / "03_final_ddx_list.json", to_ddx_items(report["final"]))
            _dump(cdir / "decline_ddx_list.json", to_ddx_items(report["declined"]))
            result = collapse(cdir)
        groups, declined_extra = result["groups"], result["declined"]
        print(f"collapse: {len(report['final'])} names -> {len(groups)} groups, "
              f"{sum(1 for d in declined_extra if d.get('declined_via'))} inherited declines, "
              f"{len(result['excluded'])} non-diagnoses, {len(result['log']['review'])} review flags", flush=True)

    inp = build_input(report, groups, declined_extra)
    text = input_text(inp)
    _dump(run_dir / "01_narrative_input.json", inp)
    (run_dir / "01_narrative_input.txt").write_text(text, encoding="utf-8")
    tokens = _count_tokens(text)
    checks = {"pid": patient_id, "report": str(report_path), "collapse": use_collapse, "input_tokens": tokens,
              "candidates": len(inp["candidates"]), "declined": len(inp["declined"]),
              "reference_standard": report.get("truth")}
    print(f"input: {len(inp['candidates'])} candidates, {len(inp['declined'])} declined, "
          f"{len(text)} chars, {tokens} tokens", flush=True)
    if input_only:
        _dump(run_dir / "04_checks.json", checks)
        return run_dir

    saved = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("02_reason_run*.json"))]
    if saved and all("reasoning" in r for r in saved):
        reasoned = passes.summarize_runs(saved)
        print(f"reason: reusing {len(saved)} saved runs", flush=True)
    else:
        reasoned = passes.reason(text)
    for i, r in enumerate(reasoned["runs"], 1):
        _dump(run_dir / f"02_reason_run{i}.json", r)
    valid = [r["reasoning"] for r in reasoned["runs"] if "reasoning" in r]
    checks.update({"reason_agreement": reasoned["agreement"], "top_sets": reasoned["top_sets"],
                   "reason_errors": [r["error"] for r in reasoned["runs"] if "error" in r]})
    if not valid:
        _dump(run_dir / "04_checks.json", checks)
        raise SystemExit("reason pass: no valid run")
    if reason_only:
        _dump(run_dir / "04_checks.json", checks)
        return run_dir
    reasoning = valid[0]
    ids = known_ids(inp)
    checks["unknown_ids"] = [i for i in passes.cited_ids(reasoning) if i not in ids]

    written = passes.write(reasoning, inp)
    checks["write_attempts"] = written["attempts"]
    header = (f"# {patient_id} 감별진단 서술 (DDx Narrative)\n\n"
              f"출처: {report_path.name} · 생성: {datetime.now():%Y-%m-%d %H:%M} · "
              f"추론 패스 {len(valid)}/{passes.REASON_RUNS}회 유효, 상위 후보 일치: {'예' if reasoned['agreement'] else '아니오 (검토 필요)'}\n\n")
    (run_dir / "03_narrative.md").write_text(header + written["text"] + "\n\n" + passes.appendix(reasoning, inp) + "\n",
                                             encoding="utf-8")
    _dump(run_dir / "04_checks.json", checks)
    print(f"narrative: {len(written['text'])} chars -> {run_dir / '03_narrative.md'}", flush=True)
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the DDx Narrative of one patient from a pipeline report.")
    parser.add_argument("patient", help="Patient id, e.g. PT07")
    parser.add_argument("--report", type=Path, default=None, help="Report HTML (default: reports/report_v7_1031/<PID>_pipeline_report_v7_1031.html)")
    parser.add_argument("--no-collapse", action="store_true", help="Keep every diagnosis name as its own candidate")
    parser.add_argument("--input-only", action="store_true", help="Build the Narrative input and stop (no LLM calls unless collapse runs)")
    parser.add_argument("--run-dir", type=Path, default=None, help="Continue this run directory, reusing its collapse files")
    parser.add_argument("--reason-only", action="store_true", help="Stop after the reason pass (the write pass can be run later with --run-dir)")
    args = parser.parse_args()
    report_path = args.report or REPORTS_DIR / f"{args.patient}_pipeline_report_v7_1031.html"
    if not report_path.exists():
        raise SystemExit(f"Report not found: {report_path}")
    if args.run_dir is not None and not args.run_dir.is_dir():
        raise SystemExit(f"Run directory not found: {args.run_dir}")
    run(args.patient, report_path, use_collapse=not args.no_collapse, input_only=args.input_only,
        run_dir=args.run_dir, reason_only=args.reason_only)


if __name__ == "__main__":
    main()
