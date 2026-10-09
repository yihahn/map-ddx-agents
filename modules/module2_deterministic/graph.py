import json
import operator
import re
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
# spec_docs/module2_deterministic.md — have the model write CORE_MESH_TERMS searches from the
# vignette, each a core term the case turns on plus the non-specific terms it is searched
# against (-> 01),
# fan out one PubMed case-report search per plan, AND'ing the core term against its companions
# OR'ed together so a hit is about that term and touches the rest of the picture (every query,
# count, title count and candidate logged -> 02), extract candidate diagnosis names from the
# top-100 (by relevance) titles per search (capped at MAX_CANDIDATES_PER_TERM), snapshot the
# merged pre-dedup candidates (-> 03), then dedup by BioLORD embedding similarity and save the
# final list (-> 04).

# How many searches the vignette is turned into: one core term each, with the terms it is
# searched against. The model writes the whole plan rather than a flat term list that code then
# groups, because which findings belong beside a core term is the same judgement as which term is
# core, and splitting it left the grouping to a rule that did not know the case. The size of each
# group is the model's too: it decides how wide a search is more than the core term does, measured
# against PubMed on PT09's terms —
#
#   core term        no OR group    OR 3    OR 5     OR 9
#   Poisoning             49,639   3,266   4,286   15,065
#   Shock                 34,139   4,452   6,692    8,632
#   Cardiomyopathy        33,341   2,604   2,903    3,583
#
# so a plan that names many companions is asking a broad question on purpose, and the 0-result
# ladder in search_case_reports covers the opposite case.
CORE_MESH_TERMS = 3
TOP_N_TITLES = 100
# Raised from 5 because there are now 3 searches rather than 10, and each returns titles spread
# across several terms rather than titles about one. Three searches at 10 is 30 candidates
# against the previous ten at 5.
MAX_CANDIDATES_PER_TERM = 10

OUTPUT_DIR = Path(__file__).parent / "output"


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class SearchPlan(BaseModel):
    core_term: str  # the term this case is about, which every hit of this search must be about
    related_terms: list[str]  # the non-specific terms it is searched against, OR'ed


class SearchPlanList(BaseModel):
    # Who the patient is, for the step that reads the search results, never for the search itself:
    # an adult's age group or sex OR'ed into a search matched nearly every case report and left
    # 70-86% of the core term's hits in place, against 10-22% for groups without them.
    age_group: str
    sex: str
    # What the record rules out, listed before the searches so code can strike any term that names
    # one. Told only not to use them, the model kept 'Myocardial Infarction' in all three of PT09's
    # searches from "no ECG findings suggesting acute myocardial infarction", three runs out of three.
    ruled_out: list[str]
    searches: list[SearchPlan]  # one per CORE_MESH_TERMS


class CandidateDiagnoses(BaseModel):
    diagnosis_names: list[str]


class Module2State(dict):
    vignette: str
    patient_id: str
    run_dir: str
    search_plans: list[dict]
    patient_description: str
    search_log: Annotated[list[dict], operator.add]
    new_ddx: Annotated[list[DDxItem], operator.add]
    final_ddx_list: list[DDxItem]
    output_path: str


def plan_searches(state: Module2State) -> dict:
    """Turn the vignette into CORE_MESH_TERMS PubMed searches, each a core term and its companions.

    The model writes the plans directly instead of listing terms that code then groups. Listing
    first cost this module its own answer: asked for ten terms by clinical priority, it collapsed
    PT09's 'Abdominal Pain', 'Diarrhea' and 'Vomiting' into one 'Gastrointestinal Diseases' and
    spent a slot on 'Adult', and the two searches that had found this patient's colchicine
    poisoning were the ones that disappeared. A flat list has to serve every search at once, and
    grouping it afterwards is a rule guessing at what the case is about; a plan is written knowing
    which search it is for.

    How many companions a search gets is the model's call too — a core term whose picture is one
    syndrome needs fewer than one that has to be pinned down by the company it keeps.

    The terms describe the patient rather than a diagnosis. Left free, the model reached for the
    diagnosis the vignette already named — 'Colchicine', 'Neurocysticercosis' — and a search built
    on a diagnosis returns reports of that diagnosis, which is confirmation of what Module 1
    already has rather than the new candidates this module exists to find. Observations are what
    a case report about an unnamed diagnosis still has in common with this patient.

    Only two things are enforced here, both structural: a search is dropped if it has no core term,
    and the core term is removed from its own companion list, because `A AND (A OR B)` is just `A`
    and the group would then do nothing.
    """
    result: SearchPlanList = get_llm().with_structured_output(SearchPlanList).invoke(
        "You are turning a clinical vignette into PubMed case-report searches.\n"
        f"Write exactly {CORE_MESH_TERMS} searches. Each one has:\n"
        "- core_term: the single MeSH term most central to this patient — what this case is "
        "really about, and what a case report describing this patient would have to be about.\n"
        "- related_terms: the non-specific MeSH terms that go together with that core term in "
        "this patient, so that a case report about the core term and any one of them would be "
        "describing this patient. Use as many or as few as the case calls for.\n"
        "Describe the patient, not a diagnosis. Avoid disease, syndrome and diagnosis names "
        "wherever the same ground can be covered another way — these searches exist to find "
        "diagnoses nobody has named yet, and a search built on a diagnosis only returns reports "
        "of the diagnosis already in hand. Build the terms instead from what is observed: "
        "symptoms and physical signs, laboratory and imaging findings including the direction "
        "they are abnormal in, and drugs and substances the patient was exposed to. Add a "
        "demographic or background term (age group, sex, pregnancy, immunosuppression, "
        "occupation, travel) only where it actually changes the differential for this patient — "
        "a neonate, a pregnancy, a transplant recipient, an exposure at work — and not by "
        "default: an adult's age group or a patient's sex rarely narrows anything. Use a disease "
        "name only where the case truly turns on an established diagnosis the patient already "
        "carries.\n"
        "Use only what this patient has. First fill ruled_out with every diagnosis and finding "
        "the record rules out, finds no evidence of, or argues against — 'no ECG findings of "
        "acute myocardial infarction' puts 'Myocardial Infarction' there — and every working "
        "diagnosis that was tried and abandoned. Nothing in ruled_out may appear as a term. "
        "Past history is a term only if it bears on the present illness — kidney failure on "
        "dialysis does, a resolved or unrelated old condition does not. Every term here is also "
        "passed on as a finding this patient has, so a wrong one misleads twice.\n"
        "One concept per term and no boolean operators anywhere; the search is assembled from "
        "these fields. Name each symptom and finding specifically, never by the category that "
        "contains it — 'Vomiting' and 'Diarrhea', not 'Gastrointestinal Diseases', because a "
        "category matches everything its members match and narrows nothing.\n"
        "The three searches should ask different questions rather than rewordings of one; "
        "related_terms may overlap between them.\n"
        "Separately, give the patient's age_group (e.g. 'Infant, Newborn', 'Child', 'Adult', "
        "'Middle Aged', 'Aged') and sex. These are not added to the searches; they tell the "
        "reader of the results who the patient is.\n\n"
        f"Vignette:\n{state['vignette']}"
    )

    ruled_out = [item.strip() for item in result.ruled_out if item.strip()]
    struck: list[str] = []

    def is_ruled_out(term: str) -> bool:
        # Same words, or the ruled-out name with only a severity/onset qualifier added, so
        # 'Acute Myocardial Infarction' strikes 'Myocardial Infarction' while 'Bacterial
        # Meningitis' leaves 'Meningitis' alone — a ruled-out subtype is not the finding itself.
        words = set(re.findall(r"[a-z0-9]+", term.lower()))
        for item in ruled_out:
            other = set(re.findall(r"[a-z0-9]+", item.lower()))
            if words and words <= other and other - words <= {"acute", "chronic", "severe"}:
                struck.append(term)
                return True
        return False

    plans, seen = [], set()
    for plan in result.searches:
        core = plan.core_term.strip()
        if not core or core.lower() in seen or is_ruled_out(core):
            continue
        seen.add(core.lower())
        related = [
            term.strip()
            for term in dict.fromkeys(plan.related_terms)
            if term.strip() and term.strip().lower() != core.lower() and not is_ruled_out(term)
        ]
        plans.append({"core_term": core, "related_terms": related})
        if len(plans) == CORE_MESH_TERMS:
            break

    run_dir = Path(state["run_dir"])
    _write_json(run_dir / "01_search_plans.json", plans)
    _write_json(run_dir / "01_ruled_out.json", {"ruled_out": ruled_out, "struck_terms": struck})
    # A flat list of every term the plans use, core terms first: reports/build_report.py reads this
    # file as the module's term list and renders it as chips.
    flat = list(dict.fromkeys([p["core_term"] for p in plans] + [t for p in plans for t in p["related_terms"]]))
    _write_json(run_dir / "01_mesh_terms.json", flat)

    # The whole patient as the candidate step sees it: every plan's terms, not just its own search's.
    # A search's query alone OR's its terms, so it says "one of these" where the patient has all of
    # them, and it holds one plan's findings — PT10's myocardial-dysfunction search had 'Neonates'
    # as one OR member among many and came back with postpartum and cocaine cardiomyopathy. Built
    # by code from fields the model already wrote, so it adds no call and states nothing new.
    who = ", ".join(x.strip() for x in (result.age_group, result.sex) if x.strip()) or "not stated"
    patient = f"Patient: {who}. Findings present together in this patient: {', '.join(flat)}."
    return {"search_plans": plans, "patient_description": patient}


def route_to_search(state: Module2State) -> list[Send]:
    """Fan out one search per plan, each carrying its own core term and companions."""
    return [
        Send(
            "search_case_reports",
            {
                "term": plan["core_term"],
                "other_terms": plan["related_terms"],
                "patient": state["patient_description"],
            },
        )
        for plan in state["search_plans"]
    ]


def search_case_reports(state: dict) -> dict:
    """
    - query = f'({term}) AND ({other OR other ...}) AND "case reports"[Publication Type]'
      The core term is AND'ed so every hit is about it, and the other terms are OR'ed so a hit has
      to touch the rest of the picture too. The core term is deliberately kept out of its own OR
      group: `A AND (A OR B OR C)` is just `A`, and the OR group would do nothing.
    - count==0 -> fall back to the core term alone, then give up; a query narrow enough to match
      nothing is the one failure this form makes likelier, and dropping the term outright would
      lose a third of the module's searches rather than a tenth
    - a large count is logged but not acted on: the OR group already narrows what the term
      alone used to widen, and the titles are taken by relevance regardless. This drops the
      old >500 rule, which AND'ed in one more term — the OR group subsumes it
    - top100 pmid/title/year/journal via esearch(sort=relevance)+esummary (no abstract)
    - LLM extracts candidate diagnosis names from titles only, given the patient description built
      in plan_searches so it can drop the ones that do not fit this patient
    - returns {"new_ddx": [DDxItem, ...], "search_log": [dict]} (one log entry per core term,
      merged into the run's 02_search_log.json by the aggregate node)
    """
    term = state["term"]
    other_terms = state["other_terms"]
    terms_used = [term, *other_terms]

    def build(others: list[str]) -> str:
        # Each OR member is parenthesised: a multi-word term sitting bare between ORs
        # ("Acute Kidney Injury OR Leukopenia") leaves PubMed to decide where the term ends.
        grouped = f' AND ({" OR ".join(f"({t})" for t in others)})' if others else ""
        return f'({term}){grouped} AND "case reports"[Publication Type]'

    query = build(other_terms)
    count = esearch_count(query)
    attempts = [{"query": query, "count": count}]

    if count == 0 and other_terms:
        terms_used = [term]
        query = build([])
        count = esearch_count(query)
        attempts.append({"query": query, "count": count})

    if count == 0:
        return {
            "new_ddx": [],
            "search_log": [
                {"term": term, "query": query, "count": 0, "attempts": attempts,
                 "titles_found": 0, "candidate_diagnoses": []}
            ],
        }

    pmids = esearch_pmids(query, retmax=TOP_N_TITLES, sort="relevance")
    records = esummary(pmids)
    titles = [r["title"] for r in records if r["title"]]

    if not titles:
        log_entry = {
            "term": term, "query": query, "count": count, "attempts": attempts,
            "titles_found": 0, "candidate_diagnoses": [],
        }
        return {"new_ddx": [], "search_log": [log_entry]}

    llm = get_llm().with_structured_output(CandidateDiagnoses)
    candidates: CandidateDiagnoses = llm.invoke(
        "These are PubMed case-report titles found by the search below. Of the diagnosis/disease "
        f"names mentioned in these titles, pick at most the {MAX_CANDIDATES_PER_TERM} most "
        "clinically plausible candidate differential diagnoses to add for this patient. Leave out "
        "any diagnosis that does not fit this patient's age, sex or findings as described below "
        "— a disease of infancy for an adult, say. Return diagnosis names only, no "
        "duplicates.\n\n"
        f"{state['patient']}\n\n"
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
        "term": term, "query": query, "count": count, "attempts": attempts,
        "titles_found": len(titles), "patient": state["patient"],
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
    _write_json(run_dir / "02_search_log.json", state.get("search_log") or [])
    _write_json(run_dir / "03_new_ddx_raw.json", [i.model_dump() for i in state.get("new_ddx") or []])

    merged = dedup_ddx_items(state.get("new_ddx") or [])

    output_path = run_dir / "04_final_ddx_list.json"
    _write_json(output_path, [item.model_dump() for item in merged])

    return {"final_ddx_list": merged, "output_path": str(output_path)}


graph = StateGraph(Module2State)
graph.add_node("plan_searches", plan_searches)
graph.add_node("search_case_reports", search_case_reports)
graph.add_node("aggregate", aggregate)

graph.add_edge(START, "plan_searches")
graph.add_conditional_edges("plan_searches", route_to_search, ["search_case_reports"])
graph.add_edge("search_case_reports", "aggregate")
graph.add_edge("aggregate", END)

module2_app = graph.compile()
