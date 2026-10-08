# CLAUDE.md

## Project

MAP diagnostic-assist pipeline: patient vignette → refined DDx list with EMR evidence + workup gaps.
LangGraph + deepagents; only the deterministic variants exist.

- **Module 1** — 5 specialist personas → Top-3 DDx each → normalize + union.
- **Module 2** — MeSH terms → PubMed case reports → new candidate DDx (parallel to Module 1).
- **Module 3** — merge M1+M2 → per-DDx criteria (Merck) → EMR verification → status + workup gap.
- **Collapse** (`modules/module3_deterministic/collapse.py`, v3) — exact-label MONDO/SNOMED codes (no LLM),
  decline inheritance on four evidence checks, then the LLM organizes the patient's whole list in context
  (same hypothesis / subtype / not a diagnosis); ontology tags flag agreement or conflict. Nothing is deleted.

`spec_docs/` is the source of truth (`module3_collapse.md` for collapse). Implement against it;
flag code↔spec discrepancies instead of silently resolving them.

## Layout

- `schema.py` — shared models (`Evidence`, `DDxItem`, `WorkupGap`)
- `modules/moduleN_deterministic/` — `graph.py`, `run.py` (CLI), `llm.py` (backend + Langfuse)
- `modules/*/output/<PID>_<ts>/` — numbered run JSON; Module 3 reads the newest M1/M2 run
- `modules/module3_deterministic/snomed.py` — SNOMED CT exact-term lookup + is_a ancestors
- `modules/module3_deterministic/report_collapse.py` — local HTML review page (`reports/`, patient data)
- `modules/module3_deterministic/eval/` — collapse gold set + `eval_collapse.py` scores against it
- `data_prep/` — MONDO and SNOMED CT extraction (see its README)
- `embeddings/` — BioLORD caches (Module 1/2 normalization), rebuilt locally

## Running

- `uv` (Python ≥3.12), from the project root: `uv run python -m modules.<module>.run PT09`
  or `./run_module{1,2,3}.sh PT09`; collapse: `... .collapse PT09 [--run-dir DIR] [--retag]`.
- LLM: Qwen3.8 on the host named by `QWEN_SERVER` (`GPU200` default, `INFER` = A5000; URL/model/key
  are `QWEN38_<server>_*` in `.env`). GPU200 and INFER judged a 60-item bench equally well; DGX scored
  lower (narrower-synonym matches) and stays out of judgement work until its setup is checked.
  Thinking on, T=0.6 / top_p=0.95, streamed. **Accuracy over speed** —
  a single call can take 20+ minutes; fix slowness with timeouts/error isolation, not by
  switching model or turning thinking off.
- Collapse changes the list only where **two independent runs agree**; a split never changes it and marks `needs_review`.
- Batch scripts: `run_batch_full_qwen38.sh` (full chain) and `run_batch_collapse_split.sh` (collapse only)
  alternate patients between GPU200 and INFER queues; concurrency per host is in `llm.py` (GPU200 8,
  INFER/DGX 2 per key). Check host RAM before running batches side by side (Modules 1–3 load BioLORD).

## Data Security

- `pending_diag/data/` is **real clinical data** — never send outside, never echo into logs,
  commits or chat beyond what the task needs. Module outputs, `batch_logs/`, `eval/`, `reports/`
  and `data_prep/module*_PT*.json` are patient-derived: local only.
- SNOMED CT is licensed content — the release zip, `data_prep/snomed_*.csv` and any SNOMED embeddings
  are never committed.
- **NEVER access anything above this directory** (`project/`) — no exceptions.

## Conventions

1. **Plan first**; state assumptions; ask when the request or spec is ambiguous.
2. **Simple, readable code** — no unrequested features, abstractions or impossible-case handling.
3. **Self-contained dirs**; follow each directory's style.
4. **Header comment** below the imports: 3–5 sentences on input, output, algorithm.
5. **Surgical changes**; mention dead code rather than deleting it.
6. **Verify on a sample** after implementing — pick one that exercises every changed path, run it,
   report what was checked; report failures with output.
7. **LLM calls** — reuse `get_llm()`; explicit timeout/retry; batch scripts log per-item progress.
8. **Clinical judgments** (gold labels, ambiguous matches) — follow the clinician's prior rulings;
   ask the user rather than deciding a borderline medical relation alone.
9. **Git** — commit only when asked, on `hahnyi/work`; check no patient-derived or licensed file is staged.
10. **Language** — reply in Korean; code, comments and commit messages in English.
