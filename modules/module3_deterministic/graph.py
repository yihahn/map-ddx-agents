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

from .criteria import fetch_reference_text
from .embed import group_by_similarity
from .emr import TOP_K, attribute_doc, load_document, rank_documents
from .llm import DEFAULT_BACKEND, get_structured_llm

# Input: Module 1 and Module 2 DDx lists plus the patient id whose EMR backs the verification, under
# state keys "module1_ddx", "module2_ddx", "patient_id", "run_dir". Output: final_ddx_list (DDxItems
# with status re-decided and EMR-sourced evidence) and final_gap_list (WorkupGaps), with each stage
# written under run_dir for auditing. Algorithm: mirrors spec_docs/module3_deterministic.md — merge
# the two lists by exact diagnosis name (-> 01), then Send one verification subgraph per diagnosis
# that scrapes reference criteria (Merck, else StatPearls), checks each criterion against the EMR by
# walking that criterion's ranked documents, and splits the outcome into a deterministic status
# rule and a workup-gap record; aggregate collects both fan-ins, sending the diagnoses the record
# ruled out to their own decline_ddx_list.json (-> 02, 03, 04, 05, 06). Every
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
OUTPUT_DIR = Path(__file__).parent / "output"


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
    judgement: Literal["supported", "refuted", "unconfirmed"]


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
    final_ddx_list: list[DDxItem]
    declined_ddx_list: list[DDxItem]
    final_gap_list: list[WorkupGap]


class DDxVerificationState(dict):
    """Branch-local state for one diagnosis, kept out of Module3State to avoid channel conflicts."""

    verify_patient_id: str
    verify_run_dir: str
    ddx_item: DDxItem
    criteria: Optional[list[Criterion]] = None
    verification_result: Optional[VerificationRecord] = None
    criteria_log: Annotated[list[dict], operator.add]
    verification_log: Annotated[list[dict], operator.add]
    branch_errors: Annotated[list[dict], operator.add]
    refined_ddx: Annotated[list[DDxItem], operator.add]
    workup_gaps: Annotated[list[WorkupGap], operator.add]


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
    """
    ddx_item: DDxItem = state["ddx_item"]
    raw_text, source_doc, detail = fetch_reference_text(ddx_item.diagnosis_name)

    if raw_text is None:
        log = {
            "diagnosis_name": ddx_item.diagnosis_name, "source_doc": None,
            "detail": detail, "criteria_count": 0,
        }
        _write_branch(state["verify_run_dir"], ddx_item.diagnosis_name, {"criteria": log})
        return {"criteria": [], "criteria_log": [log]}

    parser = get_structured_llm(ParsedCriteria)
    prompt = (
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
    try:
        parsed: ParsedCriteria = parser.invoke(prompt)
    except Exception as exc:
        # One unparseable or rate-limited reply must not take down the whole fan-out: the
        # diagnosis goes on with no criteria (as when no reference page exists), flagged.
        log = {
            "diagnosis_name": ddx_item.diagnosis_name, "source_doc": source_doc,
            "detail": detail, "criteria_count": 0, "error": f"{type(exc).__name__}: {exc}"[:300],
        }
        _write_branch(state["verify_run_dir"], ddx_item.diagnosis_name, {"criteria": log})
        return {"criteria": [], "criteria_log": [log],
                "branch_errors": [{"diagnosis_name": ddx_item.diagnosis_name,
                                   "error": f"criteria extraction failed: {log['error']}"}]}
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
    judgement: Literal["supported", "refuted", "unconfirmed"]
    quote: str  # copied from the record, empty when unconfirmed


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
    listed = "\n".join(f"{i}. {c.text}" for i, c in enumerate(criteria, 1))
    try:
        return get_structured_llm(DocJudgements, backend=VERIFIER_BACKEND).invoke(
            f"Patient record {doc_id}:\n{doc_text[:MAX_DOCUMENT_CHARS]}\n\n"
            f"Judge each numbered criterion for '{diagnosis_name}' against this record and nothing "
            f"else.\n\n{listed}\n\n"
            "For each number return 'supported' if this record confirms the criterion, 'refuted' if "
            "this record states something that cannot be true at the same time as it — a measured "
            "value outside the range it names, or a finding it rules out — and 'unconfirmed' if "
            "this record simply does not say. A record that is silent is 'unconfirmed', never "
            "'refuted'.\n"
            "quote must be text copied from the record above, and only for supported or refuted. "
            "Leave quote empty for unconfirmed. Never write a value the record does not contain.\n"
            "Return exactly one entry per criterion number listed above."
        )
    except Exception:
        return None


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
            for entry in judged.judgements if judged else []:
                index = entry.criterion_number - 1
                if not 0 <= index < len(group):
                    continue
                criterion = group[index]
                if criterion.text not in pending or entry.judgement == "unconfirmed":
                    continue
                quote = _clean_quote(entry.quote)
                # The quote has to be in the one document the model was shown. Nothing else can
                # vouch for it, and a judgement quoting text the record does not contain is the
                # failure this whole node exists to prevent.
                if attribute_doc([(doc_id, doc_text)], quote) is None:
                    continue
                settled[criterion.text] = AttributedVerification(
                    criterion=criterion.text,
                    content=quote,
                    judgement=entry.judgement,
                    source=source,
                    date=date,
                    history=list(read.get(criterion.text, [])),
                )
                pending.pop(criterion.text)
                resolved += 1
            history.append({
                "step": len(history) + 1,
                "round": round_index + 1,
                "tool": "load_emr_doc",
                "input": doc_id,
                "doc_ids": [doc_id] if doc_text else [],
                "criteria_checked": len(group),
                "criteria_settled": resolved,
                "outcome": "judged" if judged else ("call_failed" if doc_text else "load_failed"),
            })

    verifications = [
        settled.get(
            c.text,
            AttributedVerification(
                criterion=c.text,
                content="",
                judgement="unconfirmed",
                source=None,
                date=None,
                history=list(read.get(c.text, [])),
            ),
        )
        for c in criteria
    ]
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
    - status then follows from the judgements that survive: any refutation -> "declined",
      all supported -> "supported", anything left unconfirmed -> "pending"
    - Module 1/2 evidence is kept and the EMR findings are appended to it, per schema_examples.md,
      whose merged example keeps the vignette quote and the PubMed term alongside the EMR one.
      Replacing the list erased the entire clinical picture of a diagnosis whose criteria all came
      back unconfirmed (measured on PT09: colchicine poisoning, the correct answer, lost 10 of 10)
    - date and source_doc name the document the judgement was made against, which verify_with_emr
      recorded when it made it, so the evidence and the verification log cannot disagree
    - a diagnosis with no criteria found stays "pending" and keeps its Module 1/2 evidence
    - returns {"refined_ddx": [DDxItem]}
    """
    ddx_item: DDxItem = state["ddx_item"]
    result: VerificationRecord = state["verification_result"]
    if not result.verifications:
        carried = ddx_item.model_copy(deep=True)
        _write_branch(
            state["verify_run_dir"], ddx_item.diagnosis_name, {"refined": carried.model_dump()}
        )
        return {"refined_ddx": [carried]}

    # An untraceable judgement is demoted to unconfirmed, so status and evidence cannot disagree: a
    # diagnosis can no longer come back "supported" off judgements that quote no record.
    judgements = [v.judgement if v.source else "unconfirmed" for v in result.verifications]

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
    if any(not e.supports for e in evidence):
        status = "declined"
    elif all(j == "supported" for j in judgements):
        status = "supported"
    else:
        status = "pending"  # some criterion is still unconfirmed

    refined = ddx_item.model_copy(
        deep=True, update={"status": status, "evidence": list(ddx_item.evidence) + evidence}
    )
    _write_branch(state["verify_run_dir"], ddx_item.diagnosis_name, {"refined": refined.model_dump()})
    return {"refined_ddx": [refined]}


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
    unconfirmed = [v.criterion for v in result.verifications if v.judgement == "unconfirmed"]
    if not unconfirmed:
        return {"workup_gaps": []}

    listed = "\n".join(f"{i}. {c}" for i, c in enumerate(unconfirmed, 1))
    try:
        plan = get_structured_llm(WorkupPlan).invoke(
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


def aggregate(state: Module3State) -> dict:
    """Collect both fan-ins and write the run's criteria log, refined DDx list, gap list, and
    verification log — the last being how each judgement was reached rather than what it was.

    Diagnoses the record ruled out go to decline_ddx_list.json instead of 03_final_ddx_list.json.
    They are a different kind of answer from the ones still open: nothing about them is left to
    work up, and mixing them into the list a clinician reads for what to do next buries the
    diagnoses that still need something. Both files are written on every run, empty if need be.
    """
    run_dir = Path(state["run_dir"])
    refined = state.get("refined_ddx") or []
    gaps = state.get("workup_gaps") or []
    declined = [item for item in refined if item.status == "declined"]
    remaining = [item for item in refined if item.status != "declined"]

    _write_json(run_dir / "02_criteria_log.json", state.get("criteria_log") or [])
    _write_json(run_dir / "03_final_ddx_list.json", [i.model_dump() for i in remaining])
    _write_json(run_dir / "decline_ddx_list.json", [i.model_dump() for i in declined])
    _write_json(run_dir / "04_workup_gaps.json", [g.model_dump() for g in gaps])
    _write_json(run_dir / "05_branch_errors.json", state.get("branch_errors") or [])
    _write_json(run_dir / "06_verification_log.json", state.get("verification_log") or [])

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
graph.add_node("aggregate", aggregate)

graph.add_edge(START, "merge_prelim_ddx")
graph.add_conditional_edges("merge_prelim_ddx", route_to_verification, ["verify_ddx"])
graph.add_edge("verify_ddx", "aggregate")
graph.add_edge("aggregate", END)

module3_app = graph.compile()
