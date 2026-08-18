import json
import operator
from pathlib import Path
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel

from schema import DDxItem, Evidence

from .llm import get_llm
from .normalize import dedup_ddx_items
from .pubmed import esearch_count, esearch_pmids, esummary

# Input: a patient vignette (+ problem list) as free text, under state keys "vignette",
# "patient_id", "run_dir" (a pre-created output subdirectory). Output: final_ddx_list (list[DDxItem])
# of new candidate diagnoses surfaced from PubMed case-report titles, with each major stage's
# intermediate result written as its own JSON file under run_dir for auditing. Algorithm: mirrors
# spec_docs/module2_deterministic.md — extract up to 10 MeSH terms from the vignette (-> 01), fan
# out one PubMed case-report search per term (widening the query with a second related term if the
# term alone matches >500 results; every term's query/count/titles/candidates logged -> 02), extract
# candidate diagnosis names from the top-100 (by relevance) titles per term (capped at
# MAX_CANDIDATES_PER_TERM), snapshot the merged pre-dedup candidates (-> 03), then dedup by BioLORD
# embedding similarity and save the final list (-> 04).

MAX_MESH_TERMS = 10
CASE_REPORT_COUNT_THRESHOLD = 500
TOP_N_TITLES = 100
MAX_CANDIDATES_PER_TERM = 5

OUTPUT_DIR = Path(__file__).parent / "output"


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class MeshTermList(BaseModel):
    terms: list[str]  # priority order, max 10


class CandidateDiagnoses(BaseModel):
    diagnosis_names: list[str]


class RelatedTermPick(BaseModel):
    term: str


class Module2State(dict):
    vignette: str
    patient_id: str
    run_dir: str
    mesh_terms: list[str]
    search_log: Annotated[list[dict], operator.add]
    new_ddx: Annotated[list[DDxItem], operator.add]
    final_ddx_list: list[DDxItem]
    output_path: str


def extract_mesh_terms(state: Module2State) -> dict:
    """Extract up to MAX_MESH_TERMS single MeSH terms from the vignette, symptom/disease-name first."""
    llm = get_llm().with_structured_output(MeshTermList)
    result: MeshTermList = llm.invoke(
        "You are extracting PubMed MeSH search terms from a clinical vignette.\n"
        f"List up to {MAX_MESH_TERMS} single MeSH terms (one concept per term, no boolean "
        "combinations), ordered by clinical priority. Prefer symptom and disease/condition "
        "names over lab values or drug names.\n\n"
        f"Vignette:\n{state['vignette']}"
    )
    terms = result.terms[:MAX_MESH_TERMS]
    _write_json(Path(state["run_dir"]) / "01_mesh_terms.json", terms)
    return {"mesh_terms": terms}


def route_to_search(state: Module2State) -> list[Send]:
    """Fan out one search per MeSH term, carrying the full term list for the >500-result fallback."""
    return [
        Send("search_case_reports", {"term": term, "all_terms": state["mesh_terms"]})
        for term in state["mesh_terms"]
    ]


def search_case_reports(state: dict) -> dict:
    """
    - query = f'({term}) AND "case reports"[Publication Type]'
    - esearch(retmax=0) for count; count==0 -> skip (no DDxItem for this term)
    - count>500 -> LLM picks 1 related term from all_terms, AND'ed in (max 2 terms)
    - top100 pmid/title/year/journal via esearch(sort=relevance)+esummary (no abstract)
    - LLM extracts candidate diagnosis names from titles only
    - returns {"new_ddx": [DDxItem, ...], "search_log": [dict]} (one log entry per term, merged
      into the run's 02_search_log.json by the aggregate node)
    """
    term = state["term"]
    all_terms = state["all_terms"]
    terms_used = [term]
    query = f'({term}) AND "case reports"[Publication Type]'
    widened_with = None

    count = esearch_count(query)
    if count == 0:
        return {"new_ddx": [], "search_log": [{"term": term, "query": query, "count": 0}]}

    if count > CASE_REPORT_COUNT_THRESHOLD:
        other_terms = [t for t in all_terms if t != term]
        if other_terms:
            llm = get_llm().with_structured_output(RelatedTermPick)
            pick: RelatedTermPick = llm.invoke(
                "A PubMed case-report search for this MeSH term returned too many results:\n"
                f"Term: {term}\nOther extracted MeSH terms: {other_terms}\n\n"
                "Pick the single term from the list above that is most clinically related to "
                "the first term, to narrow the search."
            )
            if pick.term in other_terms:
                terms_used.append(pick.term)
                widened_with = pick.term
                query = f'({term}) AND ({pick.term}) AND "case reports"[Publication Type]'

    pmids = esearch_pmids(query, retmax=TOP_N_TITLES, sort="relevance")
    records = esummary(pmids)
    titles = [r["title"] for r in records if r["title"]]

    if not titles:
        log_entry = {
            "term": term, "query": query, "count": count,
            "widened_with": widened_with, "titles_found": 0, "candidate_diagnoses": [],
        }
        return {"new_ddx": [], "search_log": [log_entry]}

    llm = get_llm().with_structured_output(CandidateDiagnoses)
    candidates: CandidateDiagnoses = llm.invoke(
        "These are PubMed case-report titles found by the search below. Of the diagnosis/disease "
        f"names mentioned in these titles, pick at most the {MAX_CANDIDATES_PER_TERM} most "
        "clinically plausible candidate differential diagnoses to add for this patient (prefer "
        "diagnoses that recur across multiple titles). Return diagnosis names only, no duplicates.\n\n"
        f"Search: {query}\n\nTitles:\n" + "\n".join(f"- {t}" for t in titles)
    )
    diagnosis_names = candidates.diagnosis_names[:MAX_CANDIDATES_PER_TERM]

    evidence_content = ", ".join(terms_used)
    new_ddx = [
        DDxItem(
            diagnosis_name=name,
            source_detail=query,
            status="pending",
            evidence=[Evidence(content=evidence_content, supports=True)],
        )
        for name in diagnosis_names
    ]
    log_entry = {
        "term": term, "query": query, "count": count,
        "widened_with": widened_with, "titles_found": len(titles),
        "candidate_diagnoses": diagnosis_names,
    }
    return {"new_ddx": new_ddx, "search_log": [log_entry]}


def aggregate(state: Module2State) -> dict:
    """
    - writes 02_search_log.json (per-term search log, all terms) and 03_new_ddx_raw.json
      (pre-dedup candidates) for auditing
    - dedup new_ddx by diagnosis-name BioLORD cosine similarity (>0.85 -> merge)
    - on merge: evidence lists concatenated, source_detail joined with " | "
    - writes the deduped list to run_dir/04_final_ddx_list.json
    """
    run_dir = Path(state["run_dir"])
    _write_json(run_dir / "02_search_log.json", state["search_log"])
    _write_json(run_dir / "03_new_ddx_raw.json", [item.model_dump() for item in state["new_ddx"]])

    merged = dedup_ddx_items(state["new_ddx"])

    output_path = run_dir / "04_final_ddx_list.json"
    _write_json(output_path, [item.model_dump() for item in merged])

    return {"final_ddx_list": merged, "output_path": str(output_path)}


graph = StateGraph(Module2State)
graph.add_node("extract_mesh_terms", extract_mesh_terms)
graph.add_node("search_case_reports", search_case_reports)
graph.add_node("aggregate", aggregate)

graph.add_edge(START, "extract_mesh_terms")
graph.add_conditional_edges("extract_mesh_terms", route_to_search, ["search_case_reports"])
graph.add_edge("search_case_reports", "aggregate")
graph.add_edge("aggregate", END)

module2_app = graph.compile()
