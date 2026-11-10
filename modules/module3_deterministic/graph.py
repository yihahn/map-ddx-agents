import json
import operator
import os
import re
from pathlib import Path
from typing import Annotated, Literal, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel

from schema import DDxItem, Evidence, WorkupGap

from . import criteria_ddx, criteria_extract, criteria_literature, criteria_web
from .criteria import fetch_reference_text
from .embed import group_by_similarity
from .emr import TOP_K, attribute_doc, load_document, rank_documents
from .llm import DEFAULT_BACKEND, get_llm
from .medication import find_exposure_mentions, load_medications

# Input: Module 1 and Module 2 DDx lists plus the patient id whose EMR backs the verification, under
# state keys "module1_ddx", "module2_ddx", "patient_id", "run_dir". Output: final_ddx_list (DDxItems
# with status re-decided and EMR-sourced evidence) and final_gap_list (WorkupGaps), with each stage
# written under run_dir for auditing. Algorithm: mirrors spec_docs/module3_deterministic.md — merge
# the two lists by exact diagnosis name (-> 01), drop the diagnoses named after a drug the record
# never mentions (-> 07), then Send one verification subgraph per remaining diagnosis
# that finds categorized diagnostic criteria (a guideline or reference found through DuckDuckGo or
# PubMed; Merck/StatPearls under MODULE3_CRITERIA_SOURCE=reference), checks each criterion against
# the EMR by walking that criterion's ranked documents (a refutation counts only if a second call
# reading the same record confirms it), and splits the outcome into a deterministic
# status rule by criterion category, a rank key and a workup-gap record; aggregate
# collects the fan-ins, sends the diagnoses the record ruled out to their own decline_ddx_list.json,
# and ranks the rest by rank key, then by an LLM reading of the vignette (-> 02-06, 08). Every
# judgement is recorded with the EMR document it traces back to and with the ordered list of lookups
# the loop made (-> 06), so a run says not just what it concluded but how much of the record it read
# to get there. The per-diagnosis fields live in a subgraph state, not Module3State, because
# parallel Send branches writing the same top-level channel raise InvalidUpdateError.

MAX_REFERENCE_CHARS = 20000

# The criteria list is deliberately uncapped: a criterion that genuinely decides a diagnosis cannot
# be dropped to save budget, so every one the reference page yields is verified. A cap of 12 used to
# sit here because Sepsis yielded 15 criteria, most of them SOFA sub-scores reading the same lab
# files, and verifying each separately pushed one branch past the model's context window. That
# failure is now bounded by handing one record to one judgement call instead of by discarding
# criteria, and by batching the criteria that share a record into that one call. Records are small
# (PT09's largest is under 2KB), so this only guards against an unexpectedly long CSV rather than
# trimming anything in practice.
MAX_DOCUMENT_CHARS = 8000

# How far a criterion may escalate: it is checked against its best-matching document, and each
# document that leaves it unsettled moves it to the next one down. Equal to TOP_K because that is
# how many documents the ranking offers — a criterion walks its whole list or stops earlier on a
# conclusive judgement. This is the "추가 EMR 문서 조회 상한" the spec describes, now a loop bound
# rather than a budget the model could ignore.
MAX_ESCALATION_ROUNDS = TOP_K
# Below this the criterion's own wording reached nothing much in the record, so _rank_for asks again
# with the diagnosis name attached. 0.30 sits just under the 10th percentile of best-document scores
# (p10 0.331, median 0.447) and fires for 8.2% of PT09's criteria. On those it is close to a wash —
# 2 ranked the right document higher, 2 lower, 3 unchanged — so it is kept for the case it was
# written for rather than for a measured gain: criteria that omit the thing they are about, like
# StatPearls stating colchicine's threshold as "a dose as low as 7 mg".
MIN_QUERY_SCORE = 0.30
# Which backend judges criteria against the EMR. This node makes the judgement the whole module
# turns on, so it is the one worth pointing at a different model; every other node just reads prose.
VERIFIER_BACKEND = os.environ.get("MODULE3_VERIFIER_BACKEND", DEFAULT_BACKEND)
# Where diagnostic criteria come from. "ddx" (the default since 2026-10-27) takes the open-access
# papers ddxcriteria_sources fetches from PubMed and Europe PMC and falls back to the DuckDuckGo
# search when it asks for one (criteria_ddx). "web" (the default from v3) searches DuckDuckGo and
# PubMed for a society guideline first, then the best other source (criteria_web /
# criteria_literature). Both extract the same categorized key findings. "reference" is the earlier
# Merck-then-StatPearls scrape with its verbatim-statement prompt, kept so the runs behind the
# v1/v2 reports can be reproduced; "web" is kept for the runs up to 2026-10-16.
CRITERIA_SOURCE = os.environ.get("MODULE3_CRITERIA_SOURCE", "ddx")
# The criterion categories whose refutation declines a diagnosis (see determine_status): without a
# precondition the criteria do not apply, and without a necessary finding the diagnosis cannot be
# made. A refuted exclusion criterion (another cause shown) no longer declines (2026-10-27): it keeps
# the diagnosis from "supported" and leaves it pending.
DECLINING_CATEGORIES = ("precondition", "necessary")
# The categories that must all be supported, where they apply to the patient, for "supported".
CORE_CATEGORIES = ("precondition", "necessary", "exclusion")
OUTPUT_DIR = Path(__file__).parent / "output"
VIGNETTE_DIR = Path(__file__).resolve().parents[2] / "pending_diag" / "ddx_vignette"


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_branch(run_dir: str, diagnosis_name: str, data: dict) -> None:
    """Record one diagnosis's progress as its own file, so a run that dies still leaves results."""
    branch_dir = Path(run_dir) / "branches"
    branch_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", diagnosis_name.lower()).strip("-")[:60]
    path = branch_dir / f"{slug}.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    _write_json(path, {**existing, **data})


class Criterion(BaseModel):
    text: str
    source_doc: str
    checkable_by: str  # the record that would settle this item, from ParsedCriterion
    # The role this item plays (criteria_literature.CATEGORIES), which decides what a refutation of
    # it does in determine_status. The Merck/StatPearls path has no categories; "necessary" keeps
    # its rule as it was, every refutation declining.
    category: str = "necessary"
    # The item's id in its extraction (criteria_extract), which that extraction's combo and score
    # rules refer to; empty on the Merck/StatPearls path, which has no rules.
    item_id: str = ""
    # The tests that must all have been completed, contradicting the item, before the record can
    # show it absent (criteria_extract); a refutation stands only once every one has (verify_with_emr).
    refute_requires: list[str] = []
    # "whole", or the subtype this item applies to (criteria_extract); its category is its role
    # within that scope, and determine_status judges each subtype apart from the condition as a whole.
    scope: str = "whole"


# What the parser returns. checkable_by is not documentation — it is the filter: a criterion whose
# evidence no record could supply has nothing to name here, and the prompt drops it on that basis.
# Asking for it in the schema is what makes "verifiable only" structural rather than only an
# instruction, which the prompt gives too ("Skip treatment, prognosis, and epidemiology") and did
# not get on its own: sepsis came back led by its own definition, colchicine by ingested-dose
# survival statistics. It is also the retrieval query's second half (see _rank_for).
class ParsedCriterion(BaseModel):
    text: str
    checkable_by: str


class ParsedCriteria(BaseModel):
    criteria: list[ParsedCriterion]


class CriterionVerification(BaseModel):
    criterion: str
    content: str
    # not_applicable: the criterion is for a subtype, pattern, drug or situation the record shows
    # this patient does not have (a cholestatic-injury criterion in hepatocellular injury, an HLA
    # allele tied to a drug the patient never took); it counts neither way and is not a workup gap.
    judgement: Literal["supported", "refuted", "unconfirmed", "not_applicable"]


class WorkupItem(BaseModel):
    criterion_number: int  # which listed criterion this item would settle
    missing_item: str
    recommended_action: str


class WorkupPlan(BaseModel):
    items: list[WorkupItem]


# CriterionVerification above is what a judgement looks like before code has sourced it, and it
# carries no source field on purpose: asked for the record a quote came from, the model filled it 0
# times out of 21 (an explicit doc_id field fared no better). The two below are filled by code from
# the document the judgement was actually made against, and they are what a run records.
class AttributedVerification(CriterionVerification):
    source: Optional[str] = None  # EMR form the quote was traced to, None when it traces to none
    date: Optional[str] = None    # that document's date, same None rule
    history: list[str] = []       # every EMR document this criterion was checked against, in order
    # Why an unconfirmed judgement was left so on purpose: "direction_only" (the criterion gives
    # only a direction — raised, reduced — and no number, so it neither supports nor rules out; it
    # has no power to exclude), or "partial_refutation: ..." (the record contradicted it, but not
    # with every test refute_requires names completed).
    flag: Optional[str] = None


class VerificationRecord(BaseModel):
    diagnosis_name: str
    verifications: list[AttributedVerification]
    history: list[dict]  # every EMR lookup this verification made, in call order


class Module3State(dict):
    patient_id: str
    run_dir: str
    limit: Optional[int] = None
    module1_ddx: Optional[list[DDxItem]] = None
    module2_ddx: Optional[list[DDxItem]] = None
    prelim_ddx_list: list[DDxItem]
    criteria_log: Annotated[list[dict], operator.add]
    verification_log: Annotated[list[dict], operator.add]
    branch_errors: Annotated[list[dict], operator.add]
    refined_ddx: Annotated[list[DDxItem], operator.add]
    workup_gaps: Annotated[list[WorkupGap], operator.add]
    scores: Annotated[list[dict], operator.add]
    medication_list: list[dict]
    medication_filter: list[dict]
    final_ddx_list: list[DDxItem]
    declined_ddx_list: list[DDxItem]
    medication_filtered_ddx_list: list[DDxItem]
    final_gap_list: list[WorkupGap]


class DDxVerificationState(dict):
    """Branch-local state for one diagnosis, kept out of Module3State to avoid channel conflicts."""

    verify_patient_id: str
    verify_run_dir: str
    ddx_item: DDxItem
    criteria: Optional[list[Criterion]] = None
    rules: Optional[list[dict]] = None  # the combo and score rules over the criteria's item ids
    verification_result: Optional[VerificationRecord] = None
    criteria_log: Annotated[list[dict], operator.add]
    verification_log: Annotated[list[dict], operator.add]
    branch_errors: Annotated[list[dict], operator.add]
    refined_ddx: Annotated[list[DDxItem], operator.add]
    workup_gaps: Annotated[list[WorkupGap], operator.add]
    scores: Annotated[list[dict], operator.add]


def merge_prelim_ddx(state: Module3State) -> dict:
    """
    - merges module1_ddx + module2_ddx by BioLORD similarity, not by exact name. Each module only
      dedups inside itself, so the same diagnosis arrives twice under two spellings — Module 1
      names it the way a specialist would and Module 2 the way a case report titled it. Matching on
      the string alone left both in the list ("Colchicine toxicity" beside "Colchicine poisoning"),
      which spends a verification budget twice on one diagnosis
    - grouping is Module 1's own union rule, so a name joins the best match above 0.85 rather than
      the first one that clears it
    - a group keeps its Module 1 name when it has one, that being the clinician-facing wording;
      otherwise the first Module 2 name, Module 2 ordering its own list by priority
    - source_detail joins within a module with ", " and across modules with " | ", per
      schema_examples.md; evidence is concatenated and repeated quotes dropped
    - either input may be missing; the other side is used on its own
    - diagnoses both modules named are ordered first, so a --limit run verifies the best-supported
      ones; 01_prelim_ddx_list.json always records the full merge, limit only caps what fans out
    - returns {"prelim_ddx_list": [...]}
    """
    module1 = list(state.get("module1_ddx") or [])
    module2 = list(state.get("module2_ddx") or [])
    tagged = [(1, item) for item in module1] + [(2, item) for item in module2]
    if not tagged:
        _write_json(Path(state["run_dir"]) / "01_prelim_ddx_list.json", [])
        return {"prelim_ddx_list": []}

    merged: list[DDxItem] = []
    both: list[bool] = []
    for group in group_by_similarity([item.diagnosis_name for _, item in tagged]):
        members = [tagged[index] for index in group]
        first1 = next((item for module, item in members if module == 1), None)
        primary = first1 or members[0][1]

        details = []
        for module in (1, 2):
            parts = [item.source_detail for source, item in members if source == module]
            if not parts:
                continue
            joined = ", ".join(dict.fromkeys(parts))
            details.append(joined if module == 1 else f"Module2: {joined}")

        evidence, seen = [], set()
        for _, item in members:
            for quote in item.evidence:
                if quote.content not in seen:
                    seen.add(quote.content)
                    evidence.append(quote)

        merged.append(
            primary.model_copy(
                deep=True, update={"source_detail": " | ".join(details), "evidence": evidence}
            )
        )
        both.append(len({module for module, _ in members}) == 2)

    order = sorted(range(len(merged)), key=lambda i: not both[i])
    merged = [merged[i] for i in order]

    _write_json(
        Path(state["run_dir"]) / "01_prelim_ddx_list.json", [i.model_dump() for i in merged]
    )
    limit = state.get("limit")
    return {"prelim_ddx_list": merged[:limit] if limit else merged}


def route_to_verification(state: Module3State) -> list[Send]:
    """Fan out one verification subgraph per merged diagnosis."""
    return [
        Send(
            "verify_ddx",
            {
                "ddx_item": item,
                "verify_patient_id": state["patient_id"],
                "verify_run_dir": state["run_dir"],
            },
        )
        for item in state["prelim_ddx_list"]
    ]


def fetch_criteria(state: dict) -> dict:
    """
    - pull reference text for the diagnosis: Merck Manual Professional first, StatPearls fallback
    - an LLM parses that text into ParsedCriteria (the site and URL are chosen by code, not the LLM)
    - criteria are taken as the page states them, one item per statement, and not split further.
      Splitting each statement into one item per condition was tried and reverted: it multiplied a
      diagnosis's criteria several times over (arsenic 12 -> 56) and made the output too long to
      read, which is the cost the split was never worth. A SOFA row stating five score bands stays
      one item, so a verifier reads it as one scored parameter rather than as five mutually
      exclusive criteria of which four must come back refuted. A sentence naming several findings
      at once stays one item for the same reason: split, "hypocalcemia" and "hypercalcemia" became
      two criteria of rhabdomyolysis that cannot both hold, so one of them was always refuted and
      the rule in determine_status declined the diagnosis on it
    - anything a patient record could not settle is dropped — disease definitions, epidemiology,
      ingested-dose statistics, prognosis, treatment. The criteria list is what determine_status
      requires every item of to be supported, so an unverifiable item makes that unreachable
    - three kinds of page content reach the criteria list only as noise and are named in the prompt
      so the model drops them. Bare table cells and list entries ("Anti-Jo-1", "Hypertension",
      "Anti-PL-7: Interstitial lung disease") say what to look at but not what would count, and
      were 47% of everything extracted. Instructions to run a test ("cultures should be obtained
      from blood, urine, the nasopharynx") state no finding at all, and are kept only when the page
      also says what the test shows, restated as that finding. Staging and grading tables — TNM
      bands, tumour size, Grade and Class — describe a diagnosis already made rather than establish
      one, and cost PT01's renal cell carcinoma its status outright: only one T band can hold, so
      the other eight came back refuted and declined a diagnosis whose tumour the record confirms
    - one item may name several records in its checkable_by. Asking for one record per item made
      the model repeat the item instead — 7.1% of all criteria were a duplicate of another, and one
      diagnosis came back with the same sentence nine times out of twelve
    - when neither source has the diagnosis, criteria is left empty rather than filled with
      whatever text came back
    - returns {"criteria": [...], "criteria_log": [dict]}

    This is the "reference" source. The default "web" source is _fetch_web_criteria below.
    """
    if CRITERIA_SOURCE in ("ddx", "web"):
        return _fetch_web_criteria(state)
    ddx_item: DDxItem = state["ddx_item"]
    raw_text, source_doc, detail = fetch_reference_text(ddx_item.diagnosis_name)

    if raw_text is None:
        log = {
            "diagnosis_name": ddx_item.diagnosis_name, "source_doc": None,
            "detail": detail, "criteria_count": 0,
        }
        _write_branch(state["verify_run_dir"], ddx_item.diagnosis_name, {"criteria": log})
        return {"criteria": [], "criteria_log": [log]}

    parser = get_llm().with_structured_output(ParsedCriteria)
    parsed: ParsedCriteria = parser.invoke(
        f"Below is the {source_doc} reference page for '{ddx_item.diagnosis_name}'.\n\n"
        "Extract only the diagnostic criteria — the findings, tests and thresholds used to make or "
        "exclude this diagnosis. Skip treatment, prognosis and epidemiology.\n\n"
        "Every item must be a complete statement of a finding: what is observed, and the value or "
        "quality that makes it count. Copy the page's wording, but when the page gives a finding as "
        "a table cell or a bare list entry, write out the statement it stands for.\n"
        "  'Anti-Jo-1'                             -> not an item\n"
        "  'Hypertension'                          -> not an item\n"
        "  'Anti-Jo-1: Interstitial lung disease'  -> not an item; a table of associations is not a "
        "criterion\n"
        "  'Serum creatine kinase is elevated'     -> an item\n\n"
        "Skip anything that only tells you to perform a test. Keep it only when the page also says "
        "what the test shows, and then state that finding:\n"
        "  'A chest radiograph should be done'     -> not an item\n"
        "  'Chest radiograph shows patchy asymmetrically progressive infiltrates' -> an item\n\n"
        "Skip staging, grading and severity classification — TNM stages, tumour-size bands, Grade "
        "and Class tables. They describe a diagnosis already made; they do not establish it.\n\n"
        "Do not break one of the page's statements into smaller ones, and never list the same "
        "statement twice. When one sentence of the page names several findings, keep them together "
        "as one item — 'Other laboratory features include rapidly rising creatinine, hyperkalemia, "
        "hypocalcemia and hypercalcemia' is one item. Splitting it makes findings that the page "
        "lists side by side contradict each other, since a patient cannot have both hypocalcemia "
        "and hypercalcemia at one moment, and the criterion then has to come back refuted.\n"
        "A row of a scoring table is one item, kept together with every score band and threshold it "
        "lists: SOFA creatinine with its five bands is one item, not five.\n\n"
        "Leave the list empty if the page states no diagnostic criteria.\n\n"
        "For every item, checkable_by names the record that would settle it — a lab analyte, an "
        "imaging study, a vital sign, a documented history detail. Name the record, not the "
        "finding. If several records could settle one item, name them all in one checkable_by "
        "separated by commas — never repeat the item in order to name another record. Drop any "
        "item you cannot name a record for.\n\n"
        f"Page text:\n{raw_text[:MAX_REFERENCE_CHARS]}"
    )
    criteria = [
        Criterion(text=c.text, source_doc=source_doc, checkable_by=c.checkable_by)
        for c in parsed.criteria
    ]
    log = {
        "diagnosis_name": ddx_item.diagnosis_name, "source_doc": source_doc,
        "detail": detail, "criteria_count": len(criteria),
        "criteria": [{"text": c.text, "checkable_by": c.checkable_by} for c in criteria],
    }
    _write_branch(state["verify_run_dir"], ddx_item.diagnosis_name, {"criteria": log})
    return {"criteria": criteria, "criteria_log": [log]}


def _fetch_web_criteria(state: dict) -> dict:
    """
    - find a source and its criteria with criteria_ddx.find_criteria ("ddx"): the open-access papers
      ddxcriteria_sources fetches, with the DuckDuckGo search where it asks for a web search; or
      with criteria_web.find_criteria_with_fallback ("web"): a society guideline from DuckDuckGo or
      PubMed first, then DuckDuckGo's best other result, then PubMed's
    - each source is read with criteria_extract.extract_best (the v3 prompt, two runs, the finer
      kept), which asks for the key findings in the model's own words, one per test, each in one
      category (precondition, sufficient, exclusion, necessary, specific, nonspecific) that
      determine_status reads, with the tests needed to rule each out (refute_requires) and the
      combination and points rules over them; on "ddx" a newer version of a criteria set the source
      names is merged in from the fetched paper that states it (criteria_ddx)
    - the prompt was settled over six rounds on nine diagnoses (2026-10-07/08); its rules are the
      fixes for what each round got wrong — combination rules listed as items, superseded criteria
      versions, findings narrowed to their typical form, missing exclusions and reference tests
    - a source that yields nothing moves the search on to the next; no source leaves criteria empty
    - returns {"criteria": [...], "criteria_log": [dict]}
    """
    ddx_item: DDxItem = state["ddx_item"]
    # Every source read, with what the extraction made of it; the rules of the one used are logged.
    extractions: dict[str, criteria_extract.Extraction] = {}

    def extract(source_doc: str, text: str) -> list:
        extractions[source_doc] = criteria_extract.extract_best(ddx_item.diagnosis_name, source_doc, text)
        return extractions[source_doc].items

    sites: dict = {}
    used = None
    try:
        if CRITERIA_SOURCE == "ddx":
            used, source_doc, detail, extractions = criteria_ddx.find_criteria(ddx_item.diagnosis_name, trail=sites)
            found = used.items if used else []
        else:
            found, source_doc, detail = criteria_web.find_criteria_with_fallback(
                ddx_item.diagnosis_name, extract, trail=sites)
            used = extractions.get(source_doc)
    except Exception as error:  # a failed search or call leaves this diagnosis unverified, not the run
        found, source_doc, detail = [], None, f"criteria search failed: {error}"
    # Every page opened to read criteria from, with what each pass made of it — the ones that
    # yielded nothing or could not be read included, so the log shows where the criteria were
    # looked for and not only where they were found.
    sources_checked = [
        {**site, "final_outcome": site["attempts"][-1]["outcome"]} for site in sites.values()
    ]
    criteria = [
        Criterion(
            text=c.text, source_doc=source_doc, checkable_by=c.checkable_by, category=c.category,
            item_id=c.id, refute_requires=c.refute_requires, scope=c.scope,
        )
        for c in found
    ]
    # Every node after this one keys a criterion by its text, and a points score repeats the same
    # finding in each subtype's table (RUCAM: "HAV excluded" under hepatocellular and under
    # cholestatic injury), so a repeated text would merge two criteria into one judgement and one
    # scope. Such a repeat is told apart by its subtype's name.
    seen_texts: dict[str, int] = {}
    for c in criteria:
        seen_texts[c.text] = seen_texts.get(c.text, 0) + 1
    for c in criteria:
        if seen_texts[c.text] > 1 and c.scope != "whole":
            c.text = f"{c.text} ({c.scope})"
    log = {
        "diagnosis_name": ddx_item.diagnosis_name, "source_doc": source_doc,
        "detail": detail, "criteria_count": len(criteria),
        "criteria": [
            {"id": c.item_id, "text": c.text, "checkable_by": c.checkable_by, "category": c.category,
             "refute_requires": c.refute_requires, "scope": c.scope}
            for c in criteria
        ],
        # The combination and points rules over those items (criteria_extract); determine_status
        # ranks by its combo rules (_threshold_key), and still decides the status by category.
        "rules": used.rules if used else [],
        "newer_versions": used.newer_versions if used else [],
        "merged": used.meta.get("merged", []) if used else [],
        "extractions": {doc: e.meta for doc, e in extractions.items()},
        "sources_checked": sources_checked,
    }
    _write_branch(state["verify_run_dir"], ddx_item.diagnosis_name, {"criteria": log})
    return {"criteria": criteria, "rules": log["rules"], "criteria_log": [log]}


def _rank_for(patient_id: str, diagnosis_name: str, criterion: Criterion) -> list[tuple]:
    """The documents to check this criterion against, best first.

    The query is the criterion as written plus the records checkable_by names, embedded whole. It
    used to be the medical keywords extracted from those same two strings, matched term by term
    against keywords extracted from each document, and the criterion side of that was too thin to
    rank with: over PT09's 321 criteria the median query held 2 keywords and 102 of them held
    exactly one, which saturated 54.5% of criteria at coverage 1.0 and left 68.2% tied at the top of
    their ranking. A tie is decided by a tiebreaker, so for two thirds of criteria the document the
    verifier actually read was not chosen by the match at all.

    The diagnosis name is kept out of the first query and used only as a fallback, for the reason it
    was removed: putting it in every query drowned the term the query was about, and a chest X-ray
    report came first for 8 of Sepsis's 11 criteria. But criteria do routinely omit the thing they
    are about — StatPearls states colchicine's threshold as "Toxicity can occur after ingesting a
    dose as low as 7 mg" — so when the criterion alone reaches nothing, asking again with the
    diagnosis name is better than reading the top of a ranking that means nothing.
    """
    query = f"{criterion.text} {criterion.checkable_by}".strip()
    ranked = rank_documents(patient_id, query, TOP_K)
    if ranked and ranked[0][1] >= MIN_QUERY_SCORE:
        return ranked
    return rank_documents(patient_id, f"{diagnosis_name}. {query}", TOP_K)


class DocJudgement(BaseModel):
    criterion_number: int
    judgement: Literal["supported", "refuted", "unconfirmed", "not_applicable"]
    quote: str  # copied from the record, empty when unconfirmed
    # The criterion states a measurement only as raised/reduced/abnormal, with no number: judged
    # unconfirmed whatever the record shows (2026-10-27).
    direction_only: bool = False
    # For a refutation: the numbers of the criterion's "tests needed to rule it out" that this
    # record shows completed with a result contradicting it.
    tests_completed: list[int] = []


class DocJudgements(BaseModel):
    judgements: list[DocJudgement]


def _judge_against_document(
    diagnosis_name: str, doc_id: str, doc_text: str, criteria: list[Criterion]
) -> DocJudgements | None:
    """Judge several criteria against one record in a single call. None means the call failed.

    Criteria are numbered rather than echoed back, so a reworded criterion cannot be mismatched to
    the wrong judgement. One document per call is the whole point: the model is never asked which
    record to consult, only what this record says, and the quote it returns can be checked against
    the one text it was shown.
    """
    listed = "\n".join(
        f"{i}. {c.text}" + ("\n   Tests needed to rule it out: " + "; ".join(
            f"({n}) {t}" for n, t in enumerate(c.refute_requires, 1)) if c.refute_requires else "")
        for i, c in enumerate(criteria, 1)
    )
    try:
        return get_llm(backend=VERIFIER_BACKEND).with_structured_output(DocJudgements).invoke(
            f"Patient record {doc_id}:\n{doc_text[:MAX_DOCUMENT_CHARS]}\n\n"
            f"Judge each numbered criterion for '{diagnosis_name}' against this record and nothing "
            f"else.\n\n{listed}\n\n"
            "For each number return 'supported' if this record confirms the criterion, 'refuted' if "
            "this record states something that cannot be true at the same time as it — a measured "
            "value outside the range it names, or a finding it rules out — and 'unconfirmed' if "
            "this record simply does not say. A record that is silent is 'unconfirmed', never "
            "'refuted'.\n"
            "- Compare every number and duration to the criterion's own threshold or time window. "
            "A value that falls short of the threshold does not support it; a duration inside the "
            "window supports it and never refutes it.\n"
            "- A test that was not done, is only planned or pending, was contraindicated, or was "
            "done in a different form than the criterion names (e.g. without contrast when it "
            "names contrast) is 'unconfirmed', not 'refuted'.\n"
            "- When the criterion lists alternatives ('A or B'), it is supported by any one of them "
            "and refuted only if the record rules out every one.\n"
            "- A finding the record shows is not refuted by how the record words it. When the "
            "record describes the finding the criterion names with a different qualifier — its "
            "distribution, extent or pattern ('confluent' where the criterion says 'scattered', "
            "'bilateral' where it says 'multifocal') — judge it supported or unconfirmed, never "
            "refuted: refute only when the record shows the finding absent or a value outside "
            "the criterion's threshold.\n"
            "- A negative result from one site or specimen does not refute a finding that could be "
            "shown at another, unless the criterion names that site: a skin biopsy without "
            "vasculitis does not refute vasculitis on a kidney biopsy, and a negative sputum "
            "culture does not refute a positive blood culture.\n"
            "- The quote must be about the same finding or test the criterion names. A different "
            "test, a different pathogen, a plan, or a statement of what cannot yet be concluded "
            "neither supports nor refutes it.\n"
            "- 'refuted' only when the record shows the test the criterion names properly completed "
            "with a result that contradicts it. When a criterion lists tests needed to rule it out, "
            "put in tests_completed the numbers of those tests this record shows completed with a "
            "result that contradicts the criterion; the criterion counts as refuted only once every "
            "listed test has been (they may be in different records), so still answer 'refuted' "
            "when this record completes only some of them. A 'refuted' answer for such a criterion "
            "never has an empty tests_completed: a refutation whose tests are not numbered is not "
            "counted.\n"
            "- 'not_applicable' only when the criterion itself names the drug, exposure, subtype, "
            "sex, age group or situation it applies to, and the record shows this patient does not "
            "match it — an HLA allele tied to flucloxacillin in a patient who never took it, a test "
            "stated 'in cholestatic liver damage' in a patient whose record shows hepatocellular "
            "injury (R > 5), a criterion stated 'when ALT is within the normal range' in a patient "
            "with a raised ALT, a paediatric threshold in an adult. Quote the record text that shows "
            "the patient does not match. A finding of the condition itself, a test that would "
            "exclude another cause, and any criterion whose stated condition this patient does "
            "match are never 'not_applicable': judge them supported, refuted or unconfirmed.\n"
            "- A test, examination or treatment that has not been done yet is 'unconfirmed', never "
            "'not_applicable': 'not_applicable' is for a criterion that cannot concern this patient, "
            "not for one whose answer the record does not hold yet.\n"
            "- direction_only: set it true when the criterion states a measurement only as raised, "
            "lowered, increased, decreased, elevated, reduced, low, high or abnormal, with no number "
            "or threshold. Such a criterion is 'unconfirmed' whatever the record shows: a direction "
            "alone neither confirms it nor rules it out.\n"
            "quote must be text copied from the record above, and only for supported, refuted or "
            "not_applicable. Leave quote empty for unconfirmed. Never write a value the record does "
            "not contain.\n"
            "Return exactly one entry per criterion number listed above."
        )
    except Exception:
        return None


class RefutationCheck(BaseModel):
    criterion_number: int
    holds: bool


class RefutationChecks(BaseModel):
    checks: list[RefutationCheck]


def _recheck_refutations(
    diagnosis_name: str, doc_id: str, doc_text: str, refuted: list[tuple[Criterion, str]]
) -> set[int] | None:
    """Ask a second time whether each refutation from one record really holds; None if the call fails.

    A refutation is the one judgement that ends a diagnosis outright, and on the v3 runs it was the
    one most often wrong in a way that mattered: anti-NMDAR encephalitis declined because symptoms
    "started 2 weeks ago" was read as contradicting "onset within 3 months", neurosarcoidosis and
    PACNS declined on a contrast MRI that was contraindicated and never done. The first call judges
    many criteria at once; this one sees only the refutations, each with the quote it rests on, and
    is asked to confirm rather than to judge, so a refutation stands only if two reads agree.
    Returns the 0-based indices into refuted whose refutation holds.
    """
    listed = "\n".join(
        f"{i}. Criterion: {c.text}\n   Quote: \"{quote}\"" for i, (c, quote) in enumerate(refuted, 1)
    )
    try:
        result = get_llm(backend=VERIFIER_BACKEND).with_structured_output(RefutationChecks).invoke(
            f"Patient record {doc_id}:\n{doc_text[:MAX_DOCUMENT_CHARS]}\n\n"
            f"Each numbered criterion for '{diagnosis_name}' below was judged refuted by this record, "
            f"on the quote shown.\n\n{listed}\n\n"
            "For each number, return holds=true only if the quote, read in this record, states a "
            "result that cannot be true at the same time as the criterion. Return holds=false if "
            "any of these apply:\n"
            "- the quote is about a different finding, test or pathogen than the criterion names;\n"
            "- the record shows the finding the criterion names, only described with a different "
            "qualifier (its distribution, extent or pattern);\n"
            "- the quote is a negative result from a different site or specimen than the criterion "
            "names, or than the criterion's finding could be shown in;\n"
            "- the test was not done, only planned or pending, contraindicated, or done in a "
            "different form than the criterion names;\n"
            "- a number or duration in the quote actually falls inside the criterion's threshold "
            "or time window;\n"
            "- the criterion lists alternatives and the record does not rule out every one;\n"
            "- the quote describes a change, an uncertainty, or something not yet concluded rather "
            "than a result that contradicts the criterion.\n"
            "Return exactly one entry per criterion number listed above."
        )
    except Exception:
        return None
    return {
        c.criterion_number - 1 for c in result.checks
        if c.holds and 0 < c.criterion_number <= len(refuted)
    }


def verify_with_emr(state: dict) -> dict:
    """Check every criterion against the EMR by a deterministic escalation, not by an agent's choice.

    Each criterion is ranked against the record by passage similarity (see chunks.py), then checked
    against its best-matching document; a criterion the document settles is done, and one it leaves
    open moves to the next-ranked document, up to MAX_ESCALATION_ROUNDS of them. Criteria that share
    a document at the current rank are judged together in one call, which is what makes this
    affordable: on PT09 the same few progress notes rank first for almost everything, so 151
    criteria across four diagnoses need 16 calls in the first round rather than 495 one at a time.

    This replaces a ReAct agent holding query_emr_index / load_emr_doc. The agent decided when to
    look, and measurably often did not: two of four criteria-bearing branches made zero lookups and
    judged everything from the prompt, inventing values that read like records ("EKG shows QTc of
    510 ms", traceable to nothing). No wording fixed it, which is the same lesson as the loop's
    other failures — the guarantee has to be structural. Here code performs every lookup, so a
    judgement is always made with one real document in front of the model, and the machinery that
    existed to survive a misbehaving loop (recursion limit, retry, lookup budget, repeat refusal,
    the tool-free closing call) is no longer needed.

    Variable depth, the reason the spec wanted this node self-directed, is kept: escalation happens
    only for criteria still unsettled. What changed is that the depth is driven by whether evidence
    was found rather than by the model's own sense of when to stop.

    - returns {"verification_result": VerificationRecord, "verification_log": [dict]}
    """
    ddx_item: DDxItem = state["ddx_item"]
    criteria: list[Criterion] = state["criteria"]
    patient_id = state["verify_patient_id"]

    if not criteria:
        return _verified(
            state,
            VerificationRecord(
                diagnosis_name=ddx_item.diagnosis_name, verifications=[], history=[]
            ),
        )

    ranked = {c.text: _rank_for(patient_id, ddx_item.diagnosis_name, c) for c in criteria}
    retrieval = [
        {
            "criterion": c.text,
            "documents": [
                {"doc_id": doc_id, "score": score, "passages": passages}
                for doc_id, score, passages in ranked[c.text]
            ],
        }
        for c in criteria
    ]

    pending = {c.text: c for c in criteria}
    settled: dict[str, AttributedVerification] = {}
    # Refutations of a criterion whose refute_requires names several tests, gathered across records
    # until every test is covered: {criterion: {"tests": {test numbers}, "quotes": [(quote, source)]}}
    partial: dict[str, dict] = {}
    history: list[dict] = []
    texts: dict[str, str] = {}
    read: dict[str, list[str]] = {}

    for round_index in range(MAX_ESCALATION_ROUNDS):
        groups: dict[str, list[Criterion]] = {}
        for text, criterion in pending.items():
            documents = ranked[text]
            if round_index < len(documents):
                groups.setdefault(documents[round_index][0], []).append(criterion)
        if not groups:
            break

        for doc_id, group in groups.items():
            if doc_id not in texts:
                texts[doc_id] = load_document(patient_id, doc_id)
            doc_text = texts[doc_id]
            if doc_text:
                for criterion in group:
                    read.setdefault(criterion.text, []).append(doc_id)
            judged = (
                _judge_against_document(ddx_item.diagnosis_name, doc_id, doc_text, group)
                if doc_text
                else None
            )
            date, source = _doc_ref(doc_id)
            resolved = 0
            traced: list[tuple[Criterion, str, str, list[int]]] = []
            for entry in judged.judgements if judged else []:
                index = entry.criterion_number - 1
                if not 0 <= index < len(group):
                    continue
                criterion = group[index]
                if criterion.text not in pending or any(t[0].text == criterion.text for t in traced):
                    continue
                # A criterion giving only a direction (raised, reduced) and no number is unconfirmed
                # whatever the record says, so it is settled as such and not looked for again.
                if entry.direction_only:
                    settled[criterion.text] = AttributedVerification(
                        criterion=criterion.text, content="", judgement="unconfirmed", source=None,
                        date=None, history=list(read.get(criterion.text, [])), flag="direction_only",
                    )
                    pending.pop(criterion.text)
                    resolved += 1
                    continue
                if entry.judgement == "unconfirmed":
                    continue
                quote = _clean_quote(entry.quote)
                # The quote has to be in the one document the model was shown. Nothing else can
                # vouch for it, and a judgement quoting text the record does not contain is the
                # failure this whole node exists to prevent.
                if attribute_doc([(doc_id, doc_text)], quote) is None:
                    continue
                traced.append((criterion, entry.judgement, quote, entry.tests_completed))
            # A refutation the second read does not confirm is withdrawn, and its criterion stays
            # pending so the next-ranked document can still settle it. If the second call fails the
            # first judgement stands, as it did before the re-check existed.
            refuted = [(c, q) for c, j, q, _ in traced if j == "refuted"]
            withdrawn: list[dict] = []
            if refuted:
                holds = _recheck_refutations(ddx_item.diagnosis_name, doc_id, doc_text, refuted)
                if holds is not None:
                    withdrawn = [
                        {"criterion": c.text, "quote": q}
                        for i, (c, q) in enumerate(refuted) if i not in holds
                    ]
            dropped = {w["criterion"] for w in withdrawn}
            for criterion, judgement, quote, tests in traced:
                if judgement == "refuted" and criterion.text in dropped:
                    continue
                # A refutation counts only once every test the criterion needs to be ruled out has
                # been completed against it (an NCC lesion: MRI and CT); until then the criterion
                # stays pending, and the next-ranked record may complete the set. With a single test
                # the refutation itself is that test completed — its quote is traced to this record
                # and survived the re-check — so it counts even when the model left the number out.
                if judgement == "refuted" and criterion.refute_requires:
                    need = set(range(1, len(criterion.refute_requires) + 1))
                    if len(need) == 1:
                        tests = [1]
                    got = partial.setdefault(criterion.text, {"tests": set(), "quotes": []})
                    got["tests"] |= {t for t in tests if t in need}
                    got["quotes"].append((quote, source))
                    if not need <= got["tests"]:
                        continue
                    quote = " / ".join(q for q, _ in got["quotes"])
                settled[criterion.text] = AttributedVerification(
                    criterion=criterion.text,
                    content=quote,
                    judgement=judgement,
                    source=source,
                    date=date,
                    history=list(read.get(criterion.text, [])),
                )
                pending.pop(criterion.text)
                resolved += 1
            step_extra = {}
            if refuted:
                step_extra["refutations_rechecked"] = len(refuted)
                step_extra["refutations_withdrawn"] = withdrawn
                if holds is None:
                    step_extra["recheck"] = "call_failed"
            history.append({
                "step": len(history) + 1,
                "round": round_index + 1,
                "tool": "load_emr_doc",
                "input": doc_id,
                "doc_ids": [doc_id] if doc_text else [],
                "criteria_checked": len(group),
                "criteria_settled": resolved,
                "outcome": "judged" if judged else ("call_failed" if doc_text else "load_failed"),
                **step_extra,
            })

    def left_open(c: Criterion) -> AttributedVerification:
        got = partial.get(c.text)
        flag = None
        if got:
            done = [c.refute_requires[t - 1] for t in sorted(got["tests"])]
            flag = (f"partial_refutation: {len(done)} of {len(c.refute_requires)} tests needed to rule it "
                    f"out completed ({'; '.join(done) or 'none named'}); quotes: "
                    + " / ".join(f"{q} ({src})" for q, src in got["quotes"]))
        return AttributedVerification(
            criterion=c.text, content="", judgement="unconfirmed", source=None, date=None,
            history=list(read.get(c.text, [])), flag=flag,
        )

    verifications = [settled.get(c.text) or left_open(c) for c in criteria]
    record = VerificationRecord(
        diagnosis_name=ddx_item.diagnosis_name, verifications=verifications, history=history
    )
    failed = [step for step in history if step["outcome"] != "judged"]
    extra = (
        {"branch_errors": [{
            "diagnosis_name": ddx_item.diagnosis_name,
            "error": f"{len(failed)} of {len(history)} document checks failed",
        }]}
        if failed
        else {}
    )
    return _verified(state, record, retrieval=retrieval, **extra)


_DOC_REF = re.compile(r"\(?\b\d{4}-?\d{2}-?\d{2}/[^\s:)]+\)?\s*:?\s*")
_TOOL_NOISE = re.compile(r"No EMR record matches[^;.]*[;.]?\s*|EMR lookup budget spent[^;.]*[;.]?\s*")


def _clean_quote(quoted: str) -> str:
    """Strip tool formatting out of a quote so the evidence reads as record text.

    Two things leak in. query_emr_index prefixes every hit with its doc_id and the verifier quotes
    that prefix, sometimes rewriting 20240118 as 2024-01-18 or trailing it in parentheses mid
    sentence; the document and date already live in Evidence.source_doc and Evidence.date. And the
    tools' own replies ("No EMR record matches 'temperature'") get quoted as if they were findings,
    which they are not — an absent record is the reason for a judgement, never evidence for one.
    """
    cleaned = _TOOL_NOISE.sub("", quoted)
    cleaned = _DOC_REF.sub(" ", cleaned)
    cleaned = re.sub(r"\s+([;,.])", r"\1", re.sub(r"\s{2,}", " ", cleaned))
    return cleaned.strip(" ;,")


def _doc_ref(doc_id: str | None) -> tuple[str | None, str | None]:
    """Split a doc_id into the (date, form name) pair the schema records a source as."""
    if not doc_id:
        return None, None
    day, _, filename = doc_id.partition("/")
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}", filename.rsplit(".", 1)[0]


def _log_entry(record: VerificationRecord, retrieval: list[dict] | None = None) -> dict:
    """One row per verification: how it was reached, not what it judged.

    The judgements themselves are in the branch file and are not repeated here — this row is the
    audit trail, and the two outputs are kept disjoint so neither has to be maintained against the
    other.

    documents_read and lookups are derived rather than counted by hand: a verification that judged
    12 criteria off 2 lookups did not check the record, and nothing else in the output says so.
    retrieval records which documents code offered for each criterion, so a criterion judged
    unconfirmed can be read two ways apart: nothing was offered, or something was and did not settle
    it. documents_supplied counts separately from documents_read because the records now reach the
    verifier through the prompt: counting only tool calls reported "0 documents read" on a branch
    that had been handed twelve, which is the opposite of what this log exists to show.
    """
    return {
        "diagnosis_name": record.diagnosis_name,
        "lookups": len(record.history),
        "documents_supplied": sorted(
            {doc["doc_id"] for entry in (retrieval or []) for doc in entry["documents"]}
        ),
        "documents_read": list(
            dict.fromkeys(doc_id for step in record.history for doc_id in step["doc_ids"])
        ),
        "retrieval": retrieval or [],
        "history": record.history,
    }


def _verified(
    state: dict,
    record: VerificationRecord,
    attempts=None,
    retrieval: list[dict] | None = None,
    **extra,
) -> dict:
    """Write one branch's verification to disk and hand it to both fan-in nodes."""
    entry = _log_entry(record, retrieval)
    # The two outputs split rather than overlap. The branch file holds the judgements; the run's
    # audit trail — lookups, documents_supplied, documents_read, retrieval, the loop's step-by-step
    # history — holds how they were reached and lives in 06_verification_log.json. Each judgement
    # carries the documents it was checked against, so the branch says what was read without the
    # trail, and the trail no longer repeats the judgements.
    branch = {"verification": {"verifications": [v.model_dump() for v in record.verifications]}}
    if attempts is not None:
        branch["verify_attempts"] = attempts
    _write_branch(state["verify_run_dir"], record.diagnosis_name, branch)
    return {"verification_result": record, "verification_log": [entry], **extra}


# The tiers of a rank_key (_threshold_key), highest first. A diagnosis with no criteria, or none the
# record bore out, sits in TIER_NO_EVIDENCE, which _rank_final hands to the model to order.
TIER_MET, TIER_APPROACHING, TIER_NO_EVIDENCE, TIER_UNREACHABLE = 3, 2, 1, 0
# The certainty a rule's label or a score's cut-off label gives (criteria_extract writes the
# source's own name for the grade or route). A label naming several grades ("Probable or confirmed
# encephalitis") is met at the lowest of them; a label naming none ("Classification criterion",
# Yamaguchi's one rule) is the diagnosis itself and counts as definite. A cut-off that rules the
# diagnosis out ("excluded", "unlikely" — which "likely" alone would read as probable) grades 0.
RULE_GRADES = [(0, re.compile(r"excluded|unlikely|\bnot\b|negative", re.I)),
               (1, re.compile(r"possible|suspect|indeterminate|ambiguous|low[- ]confidence", re.I)),
               (2, re.compile(r"probable|presumptive|likely", re.I)),
               (3, re.compile(r"definite|definitive|confirmed|certain|high[- ]confidence", re.I))]
# A rule the source states as a requirement shared by every route of its set ("at least one major
# neuroimaging criterion is always required", criteria_extract) rather than as a route of its own:
# it has to hold alongside whichever route is met.
REQUIREMENT_RULE = re.compile(r"\b(always|required?|requires|must|entry|additional)\b|exclusion of other", re.I)


def _rule_grade(label: str) -> int:
    return next((g for g, pattern in RULE_GRADES if pattern.search(label or "")), 3)


def _number(value) -> float | None:
    try:
        return float(str(value).replace("+", "").replace("−", "-").strip())
    except (TypeError, ValueError):
        return None


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _threshold_key(rows: list[dict], counted: list[dict], rules: list[dict], scopes: dict,
                   status: str) -> tuple[list[int], dict]:
    """How far the record has carried a diagnosis toward its source's diagnostic threshold.

    The threshold is the source's own: its combo and score rules where it gives them (Yamaguchi:
    five of the eight criteria with two of the four major; RUCAM: 6 points for "probable"), else
    the category rule — a sufficient criterion, or every applicable precondition and necessary
    criterion. A rule reads its items by id, so the role the extraction gave an item does not move
    it — on PT05 the same Yamaguchi text came back with fever and leukocytosis "specific" on 10-28
    and "nonspecific" on 10-29, and the old rank_key, which counted by category, dropped adult-onset
    Still's disease from 1st to 6th. Counting criteria met as a share of what a route requires, not
    as a raw number, also stops a source that lists many criteria from outranking one that lists
    few (PT05: Castleman disease's 26 above Still's 8).

    Exclusion criteria are left out of the category threshold and its share (2026-10-30): an
    exclusion the record has not settled is a workup still to do (build_workup_gap lists it), not
    evidence against the diagnosis, and requiring them all ranked the thorough sources last — on
    the 10-29 runs drug-induced liver injury (12 criteria supported, HEV and CMV serology not yet in
    the record) fell below dermatitis carried by pruritus and an allergy history. A refuted
    exclusion (another cause shown) still keeps the threshold from being met.

    rank_key, compared highest first:
    - TIER_MET: a sufficient criterion supported, or a route of grade probable or higher met, or,
      where no rule applies, every applicable precondition and necessary criterion supported with
      no exclusion refuted and at least one finding — a necessary, specific or sufficient
      criterion, not a precondition alone — supported (PT06: drug-reaction syndrome met on "exposure
      to a suspected drug", the one defining item its source gave) -> [3, the grade (3 definite, 2 probable), sufficient plus net specific
      criteria, supported exclusions, net nonspecific]
    - TIER_APPROACHING: something supported, not met, and still reachable -> [2, percent of the
      nearest reachable route's requirement reached, its gate items counted with its groups (of the
      precondition and necessary criteria, plus one place for the finding, where no rule applies),
      the rest as above]. A route of grade possible that is met counts as
      100 percent here, not as met (PT07: angiostrongylus meningoencephalitis, "Suspected" met,
      ranked above a diagnosis 86 percent of the way to its definite route)
    - TIER_NO_EVIDENCE: nothing supported, still reachable -> [1, 0, 0, 0, 0]
    - TIER_UNREACHABLE: every route has more refuted than it can spare, and no sufficient
      criterion is left open -> [0, percent as above, the rest as above]
    A refutation on its own declines nothing here; it lowers the share and the net counts.

    A score rule is read where its cut-offs grade the diagnosis and its points name items. Since
    2026-10-30 each points band of a scored finding is an item of its own, judged against the
    record like any other, and a finding scores the points of its supported band, else its
    default (the source's "no information" points); a finding counted over exclusion items
    (RUCAM's search for alternative causes) scores the highest band whose minimum counts of
    supported items are met, else its default. Bands are alternatives, so their items stay out of
    the specific and nonspecific counts: the bands a case does not fall in come back refuted. An
    earlier extraction keyed points by the finding's one item, with the conditions as text; there a
    supported item scores the condition worded most like it, and a points key that is not an item
    is only added to the reach. The reach adds, for each finding still open, its best points over
    what it scores now. A score still unsettled — below its
    probable cut-off but within reach of it — is not a route at all (2026-10-30), so it neither
    meets nor holds down the diagnosis: on a short record the category rule ranks it, on a long one
    (a full admission, the course after the drug was stopped) the score does.
    """
    judgement = {r["item_id"]: r["judgement"] for r in rows if r["item_id"]}
    text = {r["item_id"]: r["criterion"] for r in rows if r["item_id"]}
    # Rules of a declined or inapplicable subtype are left out, as its criteria are from `counted`.
    live = [r for r in rules if scopes.get(r.get("subtype") or "", "live") not in ("declined", "not_applicable")]
    band_ids = {str(b.get("item")) for r in live if r.get("type") == "score"
                for spec in (r.get("points") or {}).values() if isinstance(spec, dict)
                for b in spec.get("bands") or [] if isinstance(b, dict) and b.get("item")}
    findings = [r for r in counted if r["item_id"] not in band_ids]

    def count(judged: str, *cats: str) -> int:
        return sum(r["judgement"] == judged and r["category"] in cats for r in findings)

    specific = (count("supported", "sufficient", "specific") - count("refuted", "specific"))
    exclusions = count("supported", "exclusion")
    nonspecific = count("supported", "nonspecific") - count("refuted", "nonspecific")

    def evaluate(rule: dict) -> dict:
        """Met, reachable, and the count supported and required over the rule's groups; an item
        not_applicable to the patient counts neither way, one not in the record is unconfirmed."""
        met, reachable, filled, needed = True, True, 0, 0
        for group in rule.get("groups") or []:
            items = [judgement.get(str(i), "unconfirmed") for i in group.get("items") or []]
            m = max(int(_number(group.get("m")) or 1), 1)
            got, open_ = items.count("supported"), items.count("unconfirmed")
            met, reachable = met and got >= m, reachable and got + open_ >= m
            filled, needed = filled + min(got, m), needed + m
        # The gate is one more group, every applicable item of it required: the setting the criteria
        # are for. Unchecked, PT03's hepatic veno-occlusive disease met the Seattle criteria, which are
        # for after a stem-cell transplant, on bilirubin and hepatomegaly with no transplant on record.
        gate = [j for j in (judgement.get(str(i), "unconfirmed") for i in rule.get("gate") or []) if j != "not_applicable"]
        got, open_ = gate.count("supported"), gate.count("unconfirmed")
        met, reachable = met and got == len(gate), reachable and got + open_ == len(gate)
        return {"met": met, "reachable": reachable, "filled": filled + got, "needed": needed + len(gate)}

    def scored(key: str, spec) -> tuple[float, float, bool] | None:
        """A scored finding's points now and at best, and whether the record has any of its items."""
        if isinstance(spec, dict):
            default = _number(spec.get("default")) or 0.0
            bands = [b for b in spec.get("bands") or [] if isinstance(b, dict) and _number(b.get("points")) is not None]
            if isinstance(spec.get("count_of"), dict):
                lists = {str(n): [str(i) for i in ids or []] for n, ids in spec["count_of"].items()}
                got = {n: sum(judgement.get(i) == "supported" for i in ids) for n, ids in lists.items()}
                can = {n: got[n] + sum(judgement.get(i, "unconfirmed") == "unconfirmed" for i in ids)
                       for n, ids in lists.items()}

                def band(counts: dict) -> float:
                    reached = [_number(b["points"]) for b in bands if all(
                        counts.get(str(n), 0) >= (_number(m) or 0) for n, m in (b.get("min") or {}).items())]
                    return max(reached, default=default)
                return band(got), band(can), any(i in judgement for ids in lists.values() for i in ids)
            items = [(str(b.get("item")), _number(b["points"])) for b in bands]
            held = [p for i, p in items if judgement.get(i) == "supported"]
            now = max(held) if held else default
            open_ = [p for i, p in items if judgement.get(i, "unconfirmed") == "unconfirmed"]
            return now, (now if held else max([now] + open_)), any(i in judgement for i, _ in items)
        if isinstance(spec, list):  # an extraction before 2026-10-30
            bands = [(_number(c.get("points")), c.get("condition") or "") for c in spec
                     if isinstance(c, dict) and _number(c.get("points")) is not None]
            if not bands:
                return None
            state = judgement.get(key)
            if state == "supported":
                words = _words(text.get(key))
                now = max(bands, key=lambda b: len(words & _words(b[1])) / max(len(_words(b[1]) | words), 1))[0]
                return now, now, True
            best = max(0.0, max(p for p, _ in bands)) if state in (None, "unconfirmed") else 0.0
            return 0.0, best, state is not None
        return None

    def score(rule: dict) -> dict | None:
        """The rule as a route — the grade of the highest cut-off reached, met when that is
        probable or higher — or None when it cannot be read (no graded cut-off, no item named)."""
        cutoffs = [(c.get("label"), _rule_grade(c.get("label")), _number(c.get("min")))
                   for c in rule.get("cutoffs") or [] if isinstance(c, dict)]
        cutoffs = [(label, g, low) for label, g, low in cutoffs if g >= 1 and low is not None]
        parts = [p for p in (scored(str(k), v) for k, v in (rule.get("points") or {}).items()) if p]
        if not cutoffs or not any(seen for _, _, seen in parts):
            return None
        total = sum(now for now, _, _ in parts)
        reach = sum(max(best - now, 0.0) for now, best, _ in parts)
        gate = [judgement.get(str(i), "unconfirmed") for i in rule.get("gate") or []]
        reached = [(g, low) for _, g, low in cutoffs if total >= low]
        target = min([low for _, g, low in cutoffs if g >= 2] or [min(low for _, _, low in cutoffs)])
        # A score decides only once it is settled: reached with its gate held, or out of reach even
        # with every open item at its highest points. In between it is left out and the category
        # rule ranks the diagnosis — RUCAM's course after cessation (+3) cannot be in a record taken
        # before the diagnosis is made, and on PT03 the 2 points it had then held drug-induced liver
        # injury at 33 percent, 6th, under the diagnoses its own liver-injury definition outranks.
        # A gate item not applicable to the patient does not hold the score back: RUCAM gates on the
        # ALP being of hepatic origin, which a hepatocellular case with a normal ALP never settles.
        gate_held = all(j in ("supported", "not_applicable") for j in gate)
        if not (total >= target and gate_held) and total + reach >= target and "refuted" not in gate:
            return None
        grade = max([g for g, _ in reached] or [0])
        return {"rule": rule.get("id"), "label": rule.get("label"), "set": rule.get("set") or "",
                "grade": grade, "score": total,
                "met": grade >= 1 and gate_held,
                "reachable": total + reach >= target and "refuted" not in gate,
                "share": min(max(total, 0.0) / target, 1.0) if target > 0 else 1.0}

    combos = [r for r in live if r.get("type") == "combo" and r.get("groups")]
    routes = [route for route in (score(r) for r in live if r.get("type") == "score") if route]
    for name in dict.fromkeys(r.get("set") or "" for r in combos):
        in_set = [r for r in combos if (r.get("set") or "") == name]
        required = [r for r in in_set if REQUIREMENT_RULE.search(r.get("label") or "")]
        for rule in [r for r in in_set if r not in required] or required:
            parts = [evaluate(rule)] + [evaluate(r) for r in required if r is not rule]
            routes.append({
                "rule": rule.get("id"), "label": rule.get("label"), "set": name,
                "grade": _rule_grade(rule.get("label")),
                "met": all(p["met"] for p in parts), "reachable": all(p["reachable"] for p in parts),
                "share": sum(p["filled"] for p in parts) / max(sum(p["needed"] for p in parts), 1),
            })

    sufficient_open = any(r["category"] == "sufficient" and r["judgement"] == "unconfirmed" for r in findings)
    any_supported = any(r["judgement"] == "supported" for r in counted)
    # The category rule's defining criteria: the whole condition's, plus those of the one live subtype
    # nearest to holding — subtypes are alternatives, as in the status, which needs only one of them
    # (PT06: the consensus's pure, induced and mixed immune-checkpoint pneumonitis, all three counted
    # together, held it at 50 percent when one subtype is all a case can be).
    def defining_of(scope: str) -> list[dict]:
        return [r for r in counted if r["scope"] == scope and r["category"] in ("precondition", "necessary")
                and r["judgement"] != "not_applicable"]

    def held(group: list[dict]) -> float:
        return sum(r["judgement"] == "supported" for r in group) / max(len(group), 1)

    subtypes = [g for g in (defining_of(n) for n, st in scopes.items() if st not in ("declined", "not_applicable")) if g]
    defining = defining_of("whole") + max(subtypes, key=held, default=[])
    finding_held = bool(count("supported", "necessary", "specific", "sufficient"))
    category_met = finding_held and (
        all(r["judgement"] == "supported" for r in defining) and not count("refuted", "exclusion")
        if defining else status == "supported")
    met = [r for r in routes if r["met"] and r["grade"] >= 2]
    if count("supported", "sufficient") or met or (not routes and category_met):
        tier = TIER_MET
        second = max([r["grade"] for r in met] or [3])
    else:
        if routes:
            open_routes = [r for r in routes if r["reachable"] or r["met"]]
            reachable = bool(open_routes) or sufficient_open
            share = max(1.0 if r["met"] else r["share"] for r in (open_routes or routes))
        elif defining:
            # The finding the category rule also requires is one more place in the share, so a
            # precondition alone is half the way, not all of it (PT03: methotrexate toxicity at 100
            # percent on "exposure to methotrexate", above drug-induced liver injury at 80).
            reachable = True
            share = (sum(r["judgement"] == "supported" for r in defining) + finding_held) / (len(defining) + 1)
        else:
            judged = [r for r in counted if r["judgement"] != "not_applicable"]
            reachable = True
            share = sum(r["judgement"] == "supported" for r in judged) / max(len(judged), 1)
        second = round(100 * share)
        if not reachable:
            tier = TIER_UNREACHABLE
        elif any_supported:
            tier = TIER_APPROACHING
        else:
            tier, second, specific, exclusions, nonspecific = TIER_NO_EVIDENCE, 0, 0, 0, 0
    return [tier, second, specific, exclusions, nonspecific], {"routes": routes}


def determine_status(state: dict) -> dict:
    """
    - a judgement counts only if its quote traces to a document the verifier actually read;
      otherwise it is treated as "unconfirmed", whichever way it was decided. This applies to
      supported and refuted alike. A refutation quoting nothing cannot contradict anything — the
      verifier has marked a criterion refuted while its own quote said the opposite (measured on
      PT09: sepsis declined on 1 of 11 criteria, where the quote read "the patient has signs of
      systemic inflammation") and one such slip rules a diagnosis out outright. A support quoting
      nothing is the same defect pointing the other way, and it was the more common one: on the
      arsenic branch 26 of 167 judgements quoted the search tool's own scoring line rather than a
      record, three of them as "supported", on a branch that made no lookups at all
    - status then follows from the judgements that survive, by each criterion's category
      (criteria_literature.CATEGORIES) and scope (the whole condition or one subtype). Each subtype
      is judged first on its own criteria: declined by a refuted precondition or necessary
      criterion of its own, not concerning the patient when one of its preconditions or every
      criterion of it the record settled is not_applicable, supported by its own sufficient criterion or all its applicable
      core criteria, pending otherwise. Then the condition: a refuted precondition or necessary
      criterion of the whole condition, or every subtype that concerns the patient declined where
      the source gives two or more subtypes -> "declined"; otherwise a supported sufficient criterion (of the whole or a live subtype), or
      every applicable precondition, necessary and exclusion criterion of the whole supported and,
      where there are subtypes, one of them supported (with no subtypes, at least one such
      criterion is needed) -> "supported"; anything else -> "pending". A refuted exclusion
      criterion (another cause shown) declines nothing since 2026-10-27; it keeps the diagnosis
      pending. A refuted sufficient criterion declines nothing — a
      biopsy of an unaffected site, larvae not found, an MRI without a scolex are negative results
      of tests that confirm but cannot exclude, and treating them as exclusions declined
      neurocysticercosis on PT07, the correct answer. A refuted specific or nonspecific criterion
      declines nothing either; it lowers the rank instead (rank_key below)
    - Module 1/2 evidence is kept and the EMR findings are appended to it, per schema_examples.md,
      whose merged example keeps the vignette quote and the PubMed term alongside the EMR one.
      Replacing the list erased the entire clinical picture of a diagnosis whose criteria all came
      back unconfirmed (measured on PT09: colchicine poisoning, the correct answer, lost 10 of 10)
    - date and source_doc name the document the judgement was made against, which verify_with_emr
      recorded when it made it, so the evidence and the verification log cannot disagree
    - a diagnosis with no criteria found stays "pending" and keeps its Module 1/2 evidence
    - the diagnosis's rank_key, under the same traceability rule as the status, is how far the
      record has carried it toward its source's diagnostic threshold (_threshold_key); aggregate
      ranks the final list by it. It replaced (2026-10-30) a key counting supported criteria by
      category — sufficient, net specific, necessary and exclusion, net nonspecific — under which
      one supported criterion of any kind put a diagnosis above every one the record had not borne
      out, and a source listing many criteria above one listing few; that key had replaced a score
      summing per-criterion weights, which ranked a diagnosis carried by one generic finding above
      one the record had not yet been tested for
    - returns {"refined_ddx": [DDxItem], "scores": [dict]}
    """
    ddx_item: DDxItem = state["ddx_item"]
    result: VerificationRecord = state["verification_result"]
    if not result.verifications:
        carried = ddx_item.model_copy(deep=True)
        _write_branch(
            state["verify_run_dir"], ddx_item.diagnosis_name, {"refined": carried.model_dump()}
        )
        score = {"diagnosis_name": ddx_item.diagnosis_name, "has_criteria": False,
                 "rank_key": [TIER_NO_EVIDENCE, 0, 0, 0, 0], "supported": [], "refuted": []}
        return {"refined_ddx": [carried], "scores": [score]}

    # An untraceable judgement is demoted to unconfirmed, so status and evidence cannot disagree: a
    # diagnosis can no longer come back "supported" off judgements that quote no record.
    judgements = [v.judgement if v.source else "unconfirmed" for v in result.verifications]
    by_text = {c.text: c for c in state.get("criteria") or []}
    rows = [
        {"criterion": v.criterion, "judgement": j,
         "category": by_text[v.criterion].category if v.criterion in by_text else "necessary",
         "scope": by_text[v.criterion].scope if v.criterion in by_text else "whole",
         "item_id": by_text[v.criterion].item_id if v.criterion in by_text else ""}
        for v, j in zip(result.verifications, judgements)
    ]

    def core_ok(group: list[dict]) -> bool | None:
        """Whether every precondition, necessary and exclusion criterion of the group that applies
        is supported; None when the group has none."""
        core = [r for r in group if r["category"] in CORE_CATEGORIES and r["judgement"] != "not_applicable"]
        return all(r["judgement"] == "supported" for r in core) if core else None

    def has(group: list[dict], judgement: str, *cats: str) -> bool:
        return any(r["judgement"] == judgement and r["category"] in cats for r in group)

    # Each subtype is judged on its own (2026-10-27): a subtype is declined when one of its own
    # preconditions or necessary criteria is refuted, does not concern the patient when one of its
    # preconditions or every criterion of it the record settled is not_applicable, and is supported by its own sufficient
    # criterion or by all its applicable core criteria. A declined subtype rules out that subtype
    # only, never the condition: hepatocellular drug-induced liver injury is not declined because
    # the record refutes the pattern that defines cholestatic injury.
    scopes: dict[str, str] = {}
    for name in dict.fromkeys(r["scope"] for r in rows if r["scope"] != "whole"):
        group = [r for r in rows if r["scope"] == name]
        judged = [r["judgement"] for r in group if r["judgement"] != "unconfirmed"]
        if has(group, "refuted", *DECLINING_CATEGORIES):
            scopes[name] = "declined"
        # The precondition of a subtype is what decides that a case is that subtype (the pattern of
        # cholestatic injury), so when it does not apply the subtype does not either, whatever its
        # other criteria came back as.
        elif has(group, "not_applicable", "precondition") or (judged and all(j == "not_applicable" for j in judged)):
            scopes[name] = "not_applicable"
        elif has(group, "supported", "sufficient") or core_ok(group):
            scopes[name] = "supported"
        else:
            scopes[name] = "pending"
    live = [name for name, st in scopes.items() if st != "not_applicable"]
    whole = [r for r in rows if r["scope"] == "whole"]
    # What counts toward the status and the rank: the whole condition's criteria and those of the
    # subtypes still open; a declined or inapplicable subtype's criteria are left out.
    counted = [r for r in rows if scopes.get(r["scope"]) not in ("declined", "not_applicable")]
    entry = lambda r: {"criterion": r["criterion"], "category": r["category"], "scope": r["scope"]}
    supported = [entry(r) for r in counted if r["judgement"] == "supported"]
    refuted = [entry(r) for r in counted if r["judgement"] == "refuted"]
    not_applicable = [entry(r) for r in rows if r["judgement"] == "not_applicable"]

    def count(group: list[dict], *cats: str) -> int:
        return sum(s["category"] in cats for s in group)

    evidence, seen = [], set()
    for v in result.verifications:
        if v.judgement not in ("supported", "refuted") or v.source is None:
            continue
        key = (v.criterion, v.content, v.judgement)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            Evidence(
                content=v.content,
                supports=(v.judgement == "supported"),
                date=v.date,
                source_doc=v.source,
                criterion=v.criterion,
            )
        )
    whole_ok = core_ok(whole)
    # Declining every live subtype declines the condition only where the source split it into two or
    # more: a lone "subtype" does not cover the condition (PT07: "headache attributed to PRES",
    # ICHD-3's secondary-headache criteria, extracted as PRES's only subtype, declined PRES itself).
    if has(whole, "refuted", *DECLINING_CATEGORIES) or (
        len(scopes) > 1 and live and all(scopes[n] == "declined" for n in live)
    ):
        status = "declined"
    elif count(supported, "sufficient") or (
        whole_ok is not False
        and (any(scopes[n] == "supported" for n in live) if live else whole_ok is True)
    ):
        status = "supported"
    else:
        status = "pending"  # some criterion is still unconfirmed

    refined = ddx_item.model_copy(
        deep=True, update={"status": status, "evidence": list(ddx_item.evidence) + evidence}
    )
    rank_key, threshold = _threshold_key(rows, counted, state.get("rules") or [], scopes, status)
    score = {
        "diagnosis_name": ddx_item.diagnosis_name, "has_criteria": True,
        "rank_key": rank_key, "threshold": threshold,
        "criteria_count": len(result.verifications), "supported": supported, "refuted": refuted,
        "not_applicable": not_applicable, "subtypes": scopes,
    }
    _write_branch(
        state["verify_run_dir"], ddx_item.diagnosis_name,
        {"refined": refined.model_dump(), "score": score},
    )
    return {"refined_ddx": [refined], "scores": [score]}


def build_workup_gap(state: dict) -> dict:
    """Turn the criteria the EMR could not settle into orderable workup items.

    The criteria themselves are prose ("Toxicity can occur after ingesting a dose as low as 7 mg
    over 4 days..."), and copying them into missing_item produced a list no one can act on —
    schema_examples.md asks for the thing to order ("IL-6" / "IL-6 lab"). One LLM call per
    diagnosis names what is actually missing; unconfirmed criteria that need no new test (a history
    detail, an already-answered question) are dropped rather than padded into the list. If the call
    fails the criteria are still reported verbatim, since losing the gap entirely is worse.
    """
    ddx_item: DDxItem = state["ddx_item"]
    result: VerificationRecord = state["verification_result"]
    # A direction-only criterion (no number) is unconfirmed by rule, so no test would settle it; a
    # not_applicable one does not concern this patient. Neither is a gap.
    unconfirmed = [v.criterion for v in result.verifications
                   if v.judgement == "unconfirmed" and v.flag != "direction_only"]
    if not unconfirmed:
        return {"workup_gaps": []}

    listed = "\n".join(f"{i}. {c}" for i, c in enumerate(unconfirmed, 1))
    try:
        plan = get_llm().with_structured_output(WorkupPlan).invoke(
            f"These diagnostic criteria for '{ddx_item.diagnosis_name}' could not be confirmed or "
            f"refuted from the patient's record:\n{listed}\n\n"
            "For each one that a test, study, or documented observation would settle, name that "
            "single item and the action that obtains it. missing_item is the item itself — a lab "
            "analyte, an imaging study, a scored assessment, a history detail — not a sentence. "
            "recommended_action is how to obtain it. criterion_number is the number of the "
            "criterion above that the item would settle, so a reader can see which one each item "
            "answers. Skip any criterion no test could settle, and never list the same item twice."
        )
    except Exception:
        plan = None

    if plan is None or not plan.items:
        return {
            "workup_gaps": [
                WorkupGap(
                    diagnosis_name=ddx_item.diagnosis_name,
                    missing_item=criterion,
                    recommended_action=f"Obtain the test or record needed to confirm: {criterion}",
                    criterion=criterion,
                )
                for criterion in unconfirmed
            ]
        }

    return {
        "workup_gaps": [
            WorkupGap(
                diagnosis_name=ddx_item.diagnosis_name,
                missing_item=item.missing_item,
                recommended_action=item.recommended_action,
                criterion=(
                    unconfirmed[item.criterion_number - 1]
                    if 1 <= item.criterion_number <= len(unconfirmed)
                    else None
                ),
            )
            for item in plan.items
        ]
    }


class DrugInDiagnosis(BaseModel):
    diagnosis_number: int
    substance: str = ""  # the specific thing the diagnosis names, empty when it names none
    kind: Literal["drug", "toxin", "organism", "other"] = "other"
    medication_number: int = 0  # the listed medication that is that substance, 0 when none is


class DrugsInDiagnoses(BaseModel):
    items: list[DrugInDiagnosis]


def filter_by_medication(state: Module3State) -> dict:
    """Drop diagnoses named after a drug this patient has no documented exposure to.

    A diagnosis like 'Metronidazole-induced encephalopathy' only makes sense for a patient who took
    metronidazole, so the record can rule it out without any diagnostic criterion — which is what
    the criteria route misses, since a reference page on metronidazole toxicity never asks whether
    the drug was given.

    Exposure is decided in three steps rather than one, because the medication CSVs hold only what
    the hospital ordered. PT09's colchicine, the reference standard's answer, is in none of them:
    the patient took it before arriving and the drug appears only in a progress note. So a match
    against the medication list keeps the diagnosis, and a diagnosis with no such match is still
    only removed once medication.py has searched the whole record for the substance and found it
    named nowhere. Deletion is the dangerous direction here — Module 3 has already deleted a
    correct diagnosis's evidence once — so it is the one outcome that has to clear both gates.

    The model is asked to point at a medication rather than to judge exposure, and its answer is a
    line number checked against the list, so a drug it invents cannot keep a diagnosis alive and a
    failed call removes nothing.

    It runs on the merged list before any verification, because a diagnosis that needs a drug the
    patient never took is out regardless of what its criteria would say, and verifying it first
    spends a whole branch of LLM calls on an answer already known. The removed ones never fan out.
    """
    run_dir = Path(state["run_dir"])
    open_ddx = state.get("prelim_ddx_list") or []
    medications = load_medications(state["patient_id"])
    if not open_ddx:
        _write_json(run_dir / "07_medication_filter.json", [])
        _write_json(run_dir / "medication_filtered_ddx_list.json", [])
        return {"medication_list": medications, "medication_filter": []}

    listed_ddx = "\n".join(f"{i}. {d.diagnosis_name}" for i, d in enumerate(open_ddx, 1))
    listed_meds = "\n".join(
        f"{i}. {m['medication']} ({m['source']})" for i, m in enumerate(medications, 1)
    ) or "(no medication records)"
    try:
        reply = get_llm().with_structured_output(DrugsInDiagnoses).invoke(
            f"Differential diagnoses:\n{listed_ddx}\n\n"
            f"Medications recorded for this patient (Korean product name, ingredient in "
            f"parentheses):\n{listed_meds}\n\n"
            "Return one entry per numbered diagnosis. Set substance to the specific thing the "
            "diagnosis is named after, in English — 'Metronidazole-induced encephalopathy' gives "
            "'metronidazole'. Leave substance empty when the diagnosis names nothing in "
            "particular: a class or mechanism such as 'Drug-induced liver injury', 'DRESS "
            "syndrome' or 'Hypersensitivity reaction' names none.\n"
            "Set kind to what that thing is: 'drug' for a medication, 'toxin' for a poison, venom "
            "or chemical exposure, 'organism' for an infectious agent — a bacterium, virus, "
            "fungus or parasite, as in 'Pneumocystis pneumonia' or 'Leishmaniasis' — and 'other' "
            "for anything else. An organism is not something a patient is prescribed, so this "
            "distinction decides whether the medication list has any bearing on the diagnosis.\n"
            "Set medication_number to the number of the medication above that is that same "
            "substance, matching the Korean ingredient to the English name, and 0 when none of "
            "them is. Do not guess: only a medication that really is that substance counts."
        )
    except Exception as error:
        _write_json(run_dir / "07_medication_filter.json", [])
        _write_json(run_dir / "medication_filtered_ddx_list.json", [])
        return {
            "medication_list": medications,
            "medication_filter": [],
            "branch_errors": [{"node": "filter_by_medication", "error": str(error)}],
        }

    decisions = []
    for item in reply.items:
        index = item.diagnosis_number - 1
        substance = item.substance.strip()
        if not (0 <= index < len(open_ddx)) or not substance:
            continue
        # Only an exposure the patient could have been given is answerable from a medication list.
        # Asked for "the substance the diagnosis is named after", the model returned the organism
        # for 'Pneumocystis pneumonia' and 'Leishmaniasis', and both were deleted for not being
        # among PT03's prescriptions — a filter on drug exposure had removed two infections.
        # Naming the kind costs nothing in the same call and puts that decision in the log.
        if item.kind not in ("drug", "toxin"):
            continue
        decision = {
            "diagnosis_name": open_ddx[index].diagnosis_name,
            "substance": substance,
            "kind": item.kind,
        }

        if 1 <= item.medication_number <= len(medications):
            matched = medications[item.medication_number - 1]
            decisions.append(
                decision
                | {
                    "exposure": "prescribed",
                    "kept": True,
                    "matched_medication": matched["medication"],
                    "matched_source": matched["source"],
                }
            )
            continue

        mentions = find_exposure_mentions(state["patient_id"], substance)
        if mentions:
            decisions.append(decision | {"exposure": "mentioned", "kept": True, "mentions": mentions})
        else:
            # Recorded with the list that was checked, so a drug the model failed to recognise in
            # its Korean form is visible as a miss rather than hidden behind a bare verdict.
            decisions.append(
                decision
                | {
                    "exposure": "absent",
                    "kept": False,
                    "medications_checked": [m["medication"] for m in medications],
                }
            )

    # Written here rather than in aggregate, like 01, so a run that dies during verification still
    # records what was removed before it.
    filtered_names = {d["diagnosis_name"] for d in decisions if not d["kept"]}
    drug_filtered = [item for item in open_ddx if item.diagnosis_name in filtered_names]
    _write_json(run_dir / "07_medication_filter.json", decisions)
    _write_json(
        run_dir / "medication_filtered_ddx_list.json", [i.model_dump() for i in drug_filtered]
    )
    return {
        "medication_list": medications,
        "medication_filter": decisions,
        "medication_filtered_ddx_list": drug_filtered,
        "prelim_ddx_list": [i for i in open_ddx if i.diagnosis_name not in filtered_names],
    }


class LikelihoodOrder(BaseModel):
    order: list[int]  # the listed diagnoses' numbers, most likely first


def _rank_final(state: Module3State, remaining: list[DDxItem]) -> tuple[list[DDxItem], list[dict]]:
    """Order the final list by rank_key's tier, the no-evidence tier as an LLM ranks it from the vignette.

    A diagnosis's rank_key (_threshold_key) places it in a tier by how far the EMR has carried it
    toward its source's diagnostic threshold — met, approaching, no evidence, unreachable — and
    orders it within the tier by the rest of the key, highest first. The criteria decide every
    tier but one: a diagnosis with no criteria, or none the record bore out, cannot be told apart
    from another such by them, so the model is given the patient's clinical vignette and asked to
    order that tier by how likely each is. It sits below the approaching tier and above the
    unreachable one, whose criteria the record has refuted beyond what its rules can spare. The
    model's answer is a list of numbers checked against the list, and anything it leaves out or
    repeats keeps its merged order at the end, so a failed call loses no diagnosis. Ties keep the
    merged order, which puts diagnoses both modules named first.
    """
    scores = {s["diagnosis_name"]: s for s in state.get("scores") or []}

    def key(item: DDxItem) -> list[int]:
        return scores.get(item.diagnosis_name, {}).get("rank_key", [TIER_NO_EVIDENCE, 0, 0, 0, 0])

    scored = [i for i in remaining if key(i)[0] > TIER_NO_EVIDENCE]
    scored.sort(key=lambda i: [-k for k in key(i)])
    unreachable = [i for i in remaining if key(i)[0] < TIER_NO_EVIDENCE]
    unreachable.sort(key=lambda i: [-k for k in key(i)])
    unscored = [i for i in remaining if key(i)[0] == TIER_NO_EVIDENCE]

    order, error = list(range(len(unscored))), None
    vignette_path = VIGNETTE_DIR / f"{state['patient_id']}_ddx_vignette_v1.md"
    if len(unscored) > 1:
        listed = "\n".join(f"{n}. {i.diagnosis_name}" for n, i in enumerate(unscored, 1))
        try:
            vignette = vignette_path.read_text(encoding="utf-8")
            reply = get_llm().with_structured_output(LikelihoodOrder).invoke(
                f"Clinical vignette:\n{vignette}\n\n"
                f"Differential diagnoses still open for this patient:\n{listed}\n\n"
                "Rank these diagnoses by how likely each is to be this patient's diagnosis, given "
                "the vignette — most likely first. Return every number once, in that order."
            )
            picked = list(dict.fromkeys(n - 1 for n in reply.order if 1 <= n <= len(unscored)))
            order = picked + [n for n in range(len(unscored)) if n not in picked]
        except Exception as e:
            error = str(e)

    ranked = scored + [unscored[n] for n in order] + unreachable
    records = []
    for rank, item in enumerate(ranked, 1):
        s = scores.get(item.diagnosis_name, {})
        records.append({
            "rank": rank, "diagnosis_name": item.diagnosis_name, "status": item.status,
            "method": "llm" if item in unscored else "criteria",
            "rank_key": key(item), "threshold": s.get("threshold"),
            "has_criteria": s.get("has_criteria", False),
            "criteria_count": s.get("criteria_count", 0),
            "supported": s.get("supported", []), "refuted": s.get("refuted", []),
        })
    if error:
        records.append({"llm_ranking_error": error})
    return ranked, records


def aggregate(state: Module3State) -> dict:
    """Collect both fan-ins and write the run's criteria log, refined DDx list, gap list, and
    verification log — the last being how each judgement was reached rather than what it was.

    Diagnoses the record ruled out go to decline_ddx_list.json instead of 03_final_ddx_list.json.
    They are a different kind of answer from the ones still open: nothing about them is left to
    work up, and mixing them into the list a clinician reads for what to do next buries the
    diagnoses that still need something. Both files are written on every run, empty if need be.
    The ones filter_by_medication removed never reached verification, so they are not here; that
    node wrote their file already.

    03_final_ddx_list.json is written in rank order (see _rank_final), and 08_ranking.json records
    each diagnosis's rank, rank_key, the supported and refuted criteria it counts and whether the
    rank came from the criteria or from the model.
    """
    run_dir = Path(state["run_dir"])
    refined = state.get("refined_ddx") or []
    gaps = state.get("workup_gaps") or []
    declined = [item for item in refined if item.status == "declined"]
    # Fan-in order is whichever branch finished first; restore the merged order before ranking, so
    # ties and the model's fallback order are deterministic.
    merged_order = {i.diagnosis_name: n for n, i in enumerate(state.get("prelim_ddx_list") or [])}
    remaining = sorted(
        (item for item in refined if item.status != "declined"),
        key=lambda i: merged_order.get(i.diagnosis_name, len(merged_order)),
    )
    remaining, ranking = _rank_final(state, remaining)

    _write_json(run_dir / "02_criteria_log.json", state.get("criteria_log") or [])
    _write_json(run_dir / "03_final_ddx_list.json", [i.model_dump() for i in remaining])
    _write_json(run_dir / "decline_ddx_list.json", [i.model_dump() for i in declined])
    _write_json(run_dir / "04_workup_gaps.json", [g.model_dump() for g in gaps])
    _write_json(run_dir / "05_branch_errors.json", state.get("branch_errors") or [])
    _write_json(run_dir / "06_verification_log.json", state.get("verification_log") or [])
    _write_json(run_dir / "08_ranking.json", ranking)

    return {
        "final_ddx_list": remaining,
        "declined_ddx_list": declined,
        "final_gap_list": gaps,
    }


verification_graph = StateGraph(DDxVerificationState)
verification_graph.add_node("fetch_criteria", fetch_criteria)
verification_graph.add_node("verify_with_emr", verify_with_emr)
verification_graph.add_node("determine_status", determine_status)
verification_graph.add_node("build_workup_gap", build_workup_gap)

verification_graph.add_edge(START, "fetch_criteria")
verification_graph.add_edge("fetch_criteria", "verify_with_emr")
verification_graph.add_edge("verify_with_emr", "determine_status")
verification_graph.add_edge("verify_with_emr", "build_workup_gap")
verification_graph.add_edge("determine_status", END)
verification_graph.add_edge("build_workup_gap", END)

verify_ddx = verification_graph.compile()

graph = StateGraph(Module3State)
graph.add_node("merge_prelim_ddx", merge_prelim_ddx)
graph.add_node("verify_ddx", verify_ddx)
graph.add_node("filter_by_medication", filter_by_medication)
graph.add_node("aggregate", aggregate)

graph.add_edge(START, "merge_prelim_ddx")
# Between the merge and the fan-out: the filter needs the whole merged list in one call, and what
# it removes should never cost a verification branch.
graph.add_edge("merge_prelim_ddx", "filter_by_medication")
graph.add_conditional_edges("filter_by_medication", route_to_verification, ["verify_ddx"])
graph.add_edge("verify_ddx", "aggregate")
graph.add_edge("aggregate", END)

module3_app = graph.compile()
