import json
import operator
from pathlib import Path
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel

from schema import DDxItem, Evidence

from .llm import get_structured_llm
from .normalize import group_by_similarity, match_to_mondo

# Input: a patient vignette (+ problem list) as free text, under state keys "vignette",
# "patient_id", "run_dir" (a pre-created output subdirectory). Output: final_ddx_list (list[DDxItem])
# of preliminary differential diagnoses agreed across specialty personas, with each stage's
# intermediate result written as its own JSON file under run_dir for auditing. Algorithm: mirrors
# spec_docs/module1_deterministic.md — recruit NUM_DEPARTMENTS specialties from the vignette (-> 01),
# fan out one persona per specialty that returns its top DDX_PER_SPECIALIST diagnoses with verbatim
# vignette quotes as evidence (-> 02, 03), then take the union by grouping diagnosis names on BioLORD
# similarity (best match above threshold, as in Module 2 but picking the closest candidate rather
# than the first), merging evidence and joining the contributing specialties into source_detail
# (-> 05). Every name is also matched to its nearest MONDO entry and logged (-> 04) for auditing,
# but that match does not decide the union.

NUM_DEPARTMENTS = 5
DDX_PER_SPECIALIST = 3

OUTPUT_DIR = Path(__file__).parent / "output"


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class RecruitedDepartments(BaseModel):
    departments: list[str]  # exactly NUM_DEPARTMENTS


class EvidenceQuote(BaseModel):
    content: str  # verbatim quote from the vignette / problem list
    supports: bool


class SpecialistDDx(BaseModel):
    diagnosis_name: str
    evidence: list[EvidenceQuote]


class SpecialistTop3(BaseModel):
    ddx_list: list[SpecialistDDx]  # exactly DDX_PER_SPECIALIST


class Module1State(dict):
    vignette: str
    patient_id: str
    run_dir: str
    departments: list[str]
    specialist_log: Annotated[list[dict], operator.add]
    specialist_outputs: Annotated[list[DDxItem], operator.add]
    final_ddx_list: list[DDxItem]
    output_path: str


def recruit_specialists(state: Module1State) -> dict:
    """Pick NUM_DEPARTMENTS specialties to consult, with at least one rare/non-mainstream department."""
    llm = get_structured_llm(RecruitedDepartments)
    result: RecruitedDepartments = llm.invoke(
        "You are the orchestrator of a multidisciplinary case conference.\n"
        f"Read the clinical vignette below and decide exactly {NUM_DEPARTMENTS} medical specialties "
        "to convene for differential diagnosis.\n\n"
        "Constraints:\n"
        "- At least one must be a rare or non-mainstream specialty (e.g. Clinical Toxicology, "
        "Medical Genetics, Immunology) rather than a common one.\n"
        "- Do not include three or more specialties from the same family (e.g. not Cardiology + "
        "Cardiac Surgery + Interventional Cardiology together).\n"
        "- Return English specialty names only.\n\n"
        f"Vignette:\n{state['vignette']}"
    )
    departments = result.departments[:NUM_DEPARTMENTS]
    _write_json(Path(state["run_dir"]) / "01_departments.json", departments)
    return {"departments": departments}


def route_to_specialists(state: Module1State) -> list[Send]:
    """Fan out one persona call per recruited department (parallel, NUM_DEPARTMENTS branches)."""
    return [
        Send("specialist_ddx", {"department": department, "vignette": state["vignette"]})
        for department in state["departments"]
    ]


def specialist_ddx(state: dict) -> dict:
    """
    - persona-prompt one specialty for its top DDX_PER_SPECIALIST differential diagnoses
    - evidence.content must be a verbatim quote from the vignette / problem list
    - status and source_detail are written by code here, not by the LLM
    - returns {"specialist_outputs": [DDxItem, ...], "specialist_log": [dict]} (one log entry per
      department, merged into the run's 02_specialist_log.json by the aggregate node)
    """
    department = state["department"]

    llm = get_structured_llm(SpecialistTop3)
    result: SpecialistTop3 = llm.invoke(
        f"You are a senior attending physician in {department}, participating in a "
        "multidisciplinary case conference.\n"
        f"From your specialty's perspective, give the top {DDX_PER_SPECIALIST} differential "
        "diagnoses for the patient below. Reason from what your specialty would notice that "
        "others might miss — do not list generic diagnoses any department would name.\n\n"
        "Rules:\n"
        "- Every evidence.content must be a verbatim quote from the vignette or problem list "
        "below. Do not paraphrase and do not invent findings.\n"
        "- supports=true if the quote argues for the diagnosis, false if it argues against it.\n"
        "- Use full formal English disease names, never abbreviations "
        "(write \"Hemophagocytic Lymphohistiocytosis\", not \"HLH\") — the names are matched "
        "against the MONDO disease ontology downstream, which does not resolve acronyms.\n\n"
        f"Vignette:\n{state['vignette']}"
    )
    ddx_list = result.ddx_list[:DDX_PER_SPECIALIST]

    specialist_outputs = [
        DDxItem(
            diagnosis_name=ddx.diagnosis_name,
            source_detail=f"Module1: {department}",
            status="pending",
            evidence=[Evidence(content=e.content, supports=e.supports) for e in ddx.evidence],
        )
        for ddx in ddx_list
    ]
    log_entry = {
        "department": department,
        "diagnoses": [
            {"diagnosis_name": d.diagnosis_name, "evidence": [e.model_dump() for e in d.evidence]}
            for d in ddx_list
        ],
    }
    return {"specialist_outputs": specialist_outputs, "specialist_log": [log_entry]}


def aggregate_union(state: Module1State) -> dict:
    """
    - writes 02_specialist_log.json (per-department raw output) and 03_specialist_outputs_raw.json
      (pre-union DDxItems) for auditing
    - matches every diagnosis name to its nearest MONDO entry (forced match, no threshold) and
      writes 04_mondo_normalization.json with the query/mondo_id/mondo_name/score — recorded for
      auditing and the spec's threshold experiment only; it does not decide the union
    - unions by BioLORD name similarity (best match above threshold): the first-seen name is kept,
      evidence lists are concatenated, and contributing departments join as "Module1: A, B"
    - writes the union to run_dir/05_final_ddx_list.json
    """
    run_dir = Path(state["run_dir"])
    items: list[DDxItem] = state["specialist_outputs"]
    _write_json(run_dir / "02_specialist_log.json", state["specialist_log"])
    _write_json(run_dir / "03_specialist_outputs_raw.json", [i.model_dump() for i in items])

    names = [item.diagnosis_name for item in items]
    _write_json(
        run_dir / "04_mondo_normalization.json", [m.as_dict() for m in match_to_mondo(names)]
    )

    final_ddx_list = []
    for group in group_by_similarity(names):
        first = items[group[0]]
        departments = []
        evidence = []
        for index in group:
            department = items[index].source_detail.removeprefix("Module1: ")
            if department not in departments:
                departments.append(department)
            evidence.extend(items[index].evidence)
        final_ddx_list.append(
            DDxItem(
                diagnosis_name=first.diagnosis_name,
                source_detail=f"Module1: {', '.join(departments)}",
                status=first.status,
                evidence=evidence,
            )
        )

    output_path = run_dir / "05_final_ddx_list.json"
    _write_json(output_path, [item.model_dump() for item in final_ddx_list])

    return {"final_ddx_list": final_ddx_list, "output_path": str(output_path)}


graph = StateGraph(Module1State)
graph.add_node("recruit_specialists", recruit_specialists)
graph.add_node("specialist_ddx", specialist_ddx)
graph.add_node("aggregate_union", aggregate_union)

graph.add_edge(START, "recruit_specialists")
graph.add_conditional_edges("recruit_specialists", route_to_specialists, ["specialist_ddx"])
graph.add_edge("specialist_ddx", "aggregate_union")
graph.add_edge("aggregate_union", END)

module1_app = graph.compile()
