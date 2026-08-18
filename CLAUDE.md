# CLAUDE.md

## Project

MAP (Multidisciplinary Assessment & Planning) diagnostic-assist AI agent pipeline.
Modules 1~3 are being implemented as LangGraph+deepagents graphs, each in two variants: deterministic and self-directed. Currently in the design stage (see `spec_docs`).

## Current Module

The module currently being implemented is **Module 2 (Case Report 기반 신규 DDx 발굴, deterministic)**, specified in [spec_docs/module2_deterministic.md](spec_docs/module2_deterministic.md). Treat that file as the source of truth for the graph structure, state schema, and node behavior — implement against it, and flag any discrepancy between the code and the spec instead of silently resolving it.

## Directory Structure

- `spec_docs/` — Design docs for Module 1~3, overall workflow, schema examples
- `schema.py` — Shared Pydantic models (Evidence, DDxItem, WorkupGap)
- `data_prep/` — Mondo disease ontology prep (`mondo.json` → `mondo_diseases.csv`)
- `normalization/` — Diagnosis-name normalization (BioLORD embedding matching, Mondo N-hop graph)
- `pending_diag/` — Patient data. `data/PT##/<date>/` contains **real clinical data** (admission notes, labs, medications, CXR reports). `ddx_vignette/` contains per-patient vignette markdown files.

**Caution:** `pending_diag/data/` contains sensitive patient information — never expose it via external transmission, logs, or commit messages.

## Workflow Rules

1. **Plan first**: Before writing or running code, briefly state the approach.
2. **Each directory is self-contained**: put new scripts in the relevant directory (`data_prep/`, `normalization/`, etc.) and follow that directory's existing style (README, file naming).
3. **Document input/output/algorithm**: for every script, write a comment block right below the imports with a concise 3-5 sentence summary covering its input, output, and core algorithm — omit nothing essential, but keep it tight.

---

## Behavioral Guidelines

Baseline principles to reduce unnecessary changes. Use judgment for trivial tasks.

### 1. Think Before Coding
- State assumptions explicitly; if uncertain, ask first.
- If multiple interpretations exist, present them instead of silently picking one.
- If a simpler approach exists, say so. Push back when warranted.

### 2. Simplicity First
- Don't add features, abstractions, or config options beyond what was requested.
- Don't add error handling for scenarios that can't happen.
- Ask: "Would a senior engineer call this overcomplicated?"

### 3. Surgical Changes
- Don't touch code, comments, or formatting outside the scope of the change.
- Match existing style.
- If you notice unrelated dead code, mention it — don't delete it.
- Only clean up imports/variables that your own change made unused.

### 4. Goal-Driven Execution
- Turn tasks into verifiable goals (e.g. "fix the bug" → "write a reproducing test, then make it pass").
- For multi-step tasks, state a short `step → verification` plan.
