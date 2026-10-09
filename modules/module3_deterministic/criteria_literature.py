import hashlib
import json
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from .criteria import EUTILS, _ncbi_get
from .llm import get_llm

# Input: a diagnosis name, plus a function that extracts criteria from a source text. Output:
# find_criteria(name, extract) -> (criteria, source_doc, detail) from the best guideline or paper
# on diagnosing that condition that yields any, and criteria_prompt(), the extraction prompt that
# asks for specific, checkable criteria only. Algorithm: search PubMed through E-utilities for
# guidelines, consensus statements, criteria papers and reviews on diagnosing the condition that
# have PubMed Central full text; the LLM ranks them and marks each as about the condition as a
# whole or about one subtype of it, and code puts every whole-condition source first; full texts
# are then fetched in that order, the sections about diagnosis kept first when long, and the first
# source the extraction finds criteria in is used. The ranking and each full text are cached on
# disk, so a rerun reads the same sources; extraction is not, so it follows the run's own model.
# PubMed replaced a DuckDuckGo search ("diagnosis for <name>"), which has no search API and began
# answering this host with its bot-check page (HTTP 202) after about a dozen queries in 2026-10-07
# testing; E-utilities is NCBI's sanctioned route for automated search and retrieval.

SEARCH_RESULTS = 20
# The publication kinds a diagnostic criterion is stated in, and the restriction to articles whose
# full text PubMed Central can serve: a ranked result that cannot be read is no source at all.
SOURCE_FILTER = (
    ' AND (guideline[pt] OR "practice guideline"[pt] OR "consensus development conference"[pt]'
    ' OR review[pt] OR "diagnostic criteria"[tiab] OR "classification criteria"[tiab]'
    ' OR diagnos*[ti]) AND "pubmed pmc"[sb] AND english[la] NOT "case reports"[pt]'
)
# How far down the ranking to go. Many PubMed Central records serve no body through efetch (about a
# thousand characters of front matter only), and a source can state no criteria the extraction
# accepts, so the next one down has to be tried; extractions are capped separately because each is
# an LLM call on up to MAX_SOURCE_CHARS of text.
MAX_CANDIDATES = 8
MAX_EXTRACTIONS = 3
MIN_SOURCE_CHARS = 1500
MAX_SOURCE_CHARS = 24000
_DIAGNOSIS_HEADING = re.compile(
    r"diagnos|criteri|definition|evaluation|work-?up|laborator|imaging|histolog|patholog|finding",
    re.I,
)
_CACHE_DIR = Path(__file__).resolve().parents[2] / "embeddings" / "literature_criteria"

_cache_lock = threading.Lock()


class RankedSource(BaseModel):
    number: int
    # Whether the record covers the condition as named or one subtype, organ manifestation or
    # patient group of it. Asked only in prose to rank narrower sources lower, the model still put
    # abdominal angiostrongyliasis first for 'Angiostrongyliasis' on one of two runs, so the
    # judgement is a field and the ordering is code's.
    scope: Literal["whole", "subtype"]


class SourceRanking(BaseModel):
    ranked: list[RankedSource]  # best first; only sources of diagnostic criteria


def _queries(diagnosis_name: str) -> list[str]:
    """The diagnosis as a phrase, then as all of its words, then as PubMed would map it."""
    words = [w for w in re.findall(r"[A-Za-z0-9'-]+", diagnosis_name) if len(w) > 1]
    phrase = diagnosis_name.replace('"', "")
    out = [f'"{phrase}"[tiab]']
    if len(words) > 1:
        out.append("(" + " AND ".join(f"{w}[tiab]" for w in words) + ")")
    out.append(f"({phrase})")
    return [q + SOURCE_FILTER for q in out]


def _search(query: str) -> list[dict]:
    """[{pmid, title, journal, year, types, pmcid}] for one PubMed query, best match first."""
    resp = _ncbi_get(f"{EUTILS}/esearch.fcgi", {"db": "pubmed", "term": query, "retmode": "json",
                                                "retmax": SEARCH_RESULTS, "sort": "relevance"})
    if resp is None:
        raise RuntimeError("PubMed search request failed")
    ids = resp.json()["esearchresult"]["idlist"]
    if not ids:
        return []
    summary = _ncbi_get(f"{EUTILS}/esummary.fcgi", {"db": "pubmed", "id": ",".join(ids),
                                                    "retmode": "json"})
    if summary is None:
        raise RuntimeError("PubMed summary request failed")
    result = summary.json()["result"]
    records = []
    for pmid in ids:
        r = result.get(pmid) or {}
        pmcid = next((a["value"] for a in r.get("articleids", []) if a.get("idtype") == "pmc"), None)
        records.append({"pmid": pmid, "title": r.get("title", ""), "journal": r.get("fulljournalname", ""),
                        "year": (r.get("pubdate") or "")[:4], "types": r.get("pubtype", []),
                        "pmcid": pmcid})
    return records


def _rank(diagnosis_name: str, records: list[dict]) -> list[tuple[int, str]]:
    """[(record index, scope)], whole-condition sources first, the model's order kept within each."""
    listed = "\n".join(
        f"{i}. {r['title']}\n   {r['journal']}, {r['year']} — {', '.join(r['types'])}"
        for i, r in enumerate(records, 1)
    )
    reply: SourceRanking = get_llm().with_structured_output(SourceRanking).invoke(
        f"These are PubMed records found on diagnosing {diagnosis_name}.\n\n{listed}\n\n"
        "Rank the records that would be the best source of diagnostic criteria for this condition, "
        "best first. Prefer, in that order, a society or government clinical practice guideline or "
        "a consensus statement, then a paper that proposes or validates formal diagnostic "
        "criteria, then a review of how the condition is diagnosed. Leave out records about a "
        "different condition, about treatment or outcomes only, about a single test's performance "
        "or laboratory methods, about animals, and single-patient reports. Return an empty list if "
        "none qualify.\n"
        f"For each record give its number and its scope: 'whole' if it is about {diagnosis_name} "
        "as named, 'subtype' if it is about one subtype, organ manifestation or patient group of "
        "it — for 'Toxoplasmosis', a paper on ocular toxoplasmosis is 'subtype'; for "
        "'Drug-induced liver injury', one on drug-induced autoimmune-like hepatitis is 'subtype'."
    )
    seen, ranked = set(), []
    for item in reply.ranked:
        index = item.number - 1
        if 0 <= index < len(records) and index not in seen and records[index]["pmcid"]:
            seen.add(index)
            ranked.append((index, item.scope))
    return [r for r in ranked if r[1] == "whole"] + [r for r in ranked if r[1] != "whole"]


def _pmc_text(pmcid: str) -> str | None:
    """Full text of a PubMed Central article, section titles kept as ## lines."""
    resp = _ncbi_get(f"{EUTILS}/efetch.fcgi", {"db": "pmc", "id": pmcid, "retmode": "xml"})
    if resp is None:
        return None
    root = ET.fromstring(resp.content)
    text = lambda el: " ".join("".join(el.itertext()).split())
    lines = []
    title = root.find(".//article-title")
    if title is not None:
        lines.append("# " + text(title))
    abstract = root.find(".//abstract")
    if abstract is not None:
        lines += ["## Abstract", text(abstract)]
    for sec in root.findall(".//body//sec"):
        heading = sec.find("title")
        if heading is not None:
            lines.append("## " + text(heading))
        for child in sec:
            if child.tag == "table-wrap":
                for row in child.iter("tr"):
                    cells = [text(c) for c in row if c.tag in ("td", "th")]
                    if any(cells):
                        lines.append(" | ".join(cells))
            elif child.tag in ("p", "list", "disp-quote", "boxed-text"):
                if text(child):
                    lines.append(text(child))
    return "\n".join(lines) or None


def _focus(text: str) -> str:
    """At most MAX_SOURCE_CHARS, sections about diagnosis first, the rest in document order."""
    if len(text) <= MAX_SOURCE_CHARS:
        return text
    blocks, current = [], []
    for line in text.splitlines():
        if line.startswith("#") and current:
            blocks.append(current)
            current = []
        current.append(line)
    blocks.append(current)
    first = [b for b in blocks if _DIAGNOSIS_HEADING.search(b[0])]
    rest = [b for b in blocks if b not in first]
    out, size = [], 0
    for block in first + rest:
        chunk = "\n".join(block)
        if size + len(chunk) > MAX_SOURCE_CHARS:
            continue
        out.append(chunk)
        size += len(chunk) + 1
    return "\n".join(out)


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:16]


def _ranking(diagnosis_name: str) -> dict:
    """{query, results, ranked: [[index, scope]]}, from disk when this diagnosis was ranked before."""
    path = _CACHE_DIR / f"rank_{_key(diagnosis_name)}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    # Each query is broader than the one before; the next is tried when a query finds nothing or
    # nothing the ranking accepts — "Colchicine toxicity" as a phrase found only papers the model
    # rightly turned down. A failed request raises and is not cached.
    record = {"diagnosis_name": diagnosis_name, "query": None, "results": [], "ranked": []}
    for query in _queries(diagnosis_name):
        records = _search(query)
        ranked = _rank(diagnosis_name, records) if records else []
        record.update(query=query, results=records, ranked=[list(r) for r in ranked])
        if ranked:
            break
    with _cache_lock:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


def _source_text(pmcid: str) -> str:
    """The focused full text of one article ("" when PMC serves no usable body), cached on disk."""
    path = _CACHE_DIR / f"{pmcid}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    text = _pmc_text(pmcid)
    if text is None:
        raise RuntimeError(f"PMC request for {pmcid} failed")
    text = _focus(text) if len(text) >= MIN_SOURCE_CHARS else ""
    with _cache_lock:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


# PubMed publication types that mark a society or government guideline or a consensus statement.
GUIDELINE_TYPES = {"Guideline", "Practice Guideline", "Consensus Statement",
                   "Consensus Development Conference", "Consensus Development Conference, NIH"}


def find_criteria(
    diagnosis_name: str, extract: Callable[[str, str], list], guidelines_only: bool = False
) -> tuple[list, str | None, str]:
    """(criteria, source_doc, detail) from the first ranked source extract() finds criteria in.

    extract(source_doc, text) is the caller's LLM call, so this module decides which source and the
    caller decides what counts as a criterion. Every source tried is listed in detail, with why it
    was passed over, so an empty result says where it looked. guidelines_only keeps to records
    PubMed types as a guideline or consensus statement.
    """
    try:
        record = _ranking(diagnosis_name)
    except RuntimeError as error:
        return [], None, str(error)
    records, tried, extractions = record["results"], [], 0
    if not record["ranked"]:
        return [], None, f"no ranked guideline, criteria paper or review; query: {record['query']}"
    ranked = record["ranked"]
    if guidelines_only:
        ranked = [r for r in ranked if GUIDELINE_TYPES & set(records[r[0]]["types"])]
        if not ranked:
            return [], None, f"no guideline or consensus statement among {len(records)} records"
    for index, scope in ranked[:MAX_CANDIDATES]:
        hit = records[index]
        try:
            text = _source_text(hit["pmcid"])
        except RuntimeError as error:
            tried.append(f"{hit['pmcid']} ({error})")
            continue
        if not text:
            tried.append(f"{hit['pmcid']} (no full text)")
            continue
        source_doc = f"{hit['title']} ({hit['journal']}, {hit['year']})"
        criteria = extract(source_doc, text)
        extractions += 1
        if criteria:
            detail = (f"PMID {hit['pmid']} / {hit['pmcid']}, {scope} — result {index + 1} of "
                      f"{len(records)}; passed over: {'; '.join(tried) or 'none'}; "
                      f"query: {record['query']}")
            return criteria, source_doc, detail
        tried.append(f"{hit['pmcid']} (no specific criteria)")
        if extractions >= MAX_EXTRACTIONS:
            break
    return [], None, f"no source yielded criteria: {'; '.join(tried)}; query: {record['query']}"


def criteria_prompt(diagnosis_name: str, source_doc: str, text: str) -> str:
    """The extraction prompt for a source found this way: the key findings, one per test, weighted."""
    return (
        f"Below is the text of {source_doc}, chosen as the best available guideline or paper on "
        f"diagnosing '{diagnosis_name}'.\n\n"
        f"Read the whole text and work out which findings a clinician uses to establish "
        f"{diagnosis_name}: the key diagnostic findings that a test, an examination or the "
        "documented history can check, and that point to this diagnosis rather than merely being "
        "compatible with it — a confirmatory test result, a defined threshold, a characteristic "
        "imaging or histological finding, a required exposure, a required exclusion of other "
        "causes. Write them in your own words as a short list. Do not copy sentences from the "
        "source.\n\n"
        "What the list must contain:\n"
        "- The reference-standard or confirmatory test, whenever the source names one (biopsy, "
        "culture, PCR, a definitive imaging sign), even if it is rarely done.\n"
        "- The route by which most patients are actually diagnosed, when that differs from the "
        "definitive one — for example compatible imaging lesions when the definitive sign is "
        "usually absent, or IgM alongside IgG serology.\n"
        "- When the source calls the condition a diagnosis of exclusion or says other causes must "
        "be ruled out, one item per test or record that does the excluding, naming what it must "
        "not show ('Viral hepatitis serology: negative for acute hepatitis A, B, C and E').\n\n"
        "How to write each item:\n"
        "- One item per test or examination. Each item names the test and the result that counts, "
        "with the source's threshold if it gives one: 'CSF cell count: eosinophils > 10%', "
        "'Cardiac MRI T2-weighted imaging or T2 mapping: increased myocardial T2 signal'.\n"
        "- Every number, threshold or value in an item must be stated in the source text, with its "
        "conditions exactly as the source gives them. Do not add values from your own knowledge; "
        "without one in the source, describe the result without a number.\n"
        "- Keep the full range of results the source accepts. Do not narrow a finding to its "
        "typical form when the source allows others (a rash 'typically salmon-pink, but other "
        "rashes may be consistent' is 'transient rash with the fever spikes', not 'salmon-pink "
        "rash'), and list all the patterns the source accepts for an imaging finding.\n"
        "- Do not turn a typical course, a usual time window or a reference figure into a required "
        "condition. 'Symptoms usually begin 10-24 h after ingestion' or 'toxic dose: 0.5 mg/kg' "
        "describe the condition; they are not thresholds a patient must meet.\n"
        "- Say what a result does and does not establish when the source qualifies it: a test the "
        "source says shows only past exposure, or is insufficient on its own, is stated as such.\n"
        "- When a criteria set is a scoring or combination rule ('2 of 3', 'both A and B', 'meets "
        "the criteria for possible disease plus one of...'), list the findings it is built from, "
        "each as its own item, and leave the rule out. No item may refer to other items or to "
        "how many of them are needed.\n"
        "- When the source gives alternative ways to show the same finding, keep them in one item "
        "joined with 'or' ('BAL eosinophils >= 25% or eosinophilic pneumonia on lung biopsy'). "
        "When it describes mutually exclusive patterns or subtypes, give the finding they share "
        "rather than one item per pattern; when subtypes are diagnosed by different tests, keep "
        "one item per subtype and name the subtype in it.\n"
        "- When the source gives more than one version of a criteria set (different years or "
        "revisions), use only the most recent one.\n"
        "- Include a history or exposure item only when the diagnosis cannot be made without it "
        "(the suspected drug was taken, the parasite's host was eaten).\n\n"
        "Do not include:\n"
        "- findings shared by many conditions (fever, fatigue, leukocytosis, a raised CRP) unless "
        "the source makes them part of a formal criterion or case definition;\n"
        "- descriptions of what is typical, common or possible ('most patients have', 'may show', "
        "percentages, sensitivities, predictive values);\n"
        "- epidemiology, risk factors, prognosis, treatment, and staging or severity grading;\n"
        "- a monitoring threshold or result from a single study rather than the source's own "
        "diagnostic approach;\n"
        "- findings that would point to another disease, except as the exclusion items above;\n"
        "- instructions on when or how to perform a test, and descriptions of how a test or assay "
        "works — an item says what result in the patient counts toward the diagnosis.\n\n"
        "Never list the same finding twice. Leave the list empty if the source states no specific "
        "diagnostic findings.\n\n"
        "For every item, checkable_by names the record that would settle it — a lab analyte, an "
        "imaging study, a vital sign, a documented history detail. Name the record, not the "
        "finding. If several records could settle one item, name them all in one checkable_by "
        "separated by commas. Drop any item you cannot name a record for.\n\n"
        "For every item, weight is how much that finding counts toward establishing the diagnosis "
        "compared with the other items, as a number between 0 and 1; the weights of all items "
        "together must add up to 1. Give the most to findings that confirm the diagnosis on their "
        "own (the reference-standard test, a definitive sign), a middle share to required "
        "findings and exclusions, and the least to findings that only support it.\n\n"
        f"Source text:\n{text}"
    )


class WeightedCriterion(BaseModel):
    text: str
    checkable_by: str
    weight: float


class WeightedCriteria(BaseModel):
    criteria: list[WeightedCriterion]


def normalize_weights(criteria: list) -> list:
    """The same items with weights rescaled to add up to exactly 1 (equal shares when all are 0).

    The model is asked for weights summing to 1 but its arithmetic drifts, so the sum is enforced
    here rather than trusted; negative weights count as 0."""
    total = sum(max(c.weight, 0.0) for c in criteria)
    for c in criteria:
        c.weight = round(max(c.weight, 0.0) / total, 4) if total > 0 else round(1 / len(criteria), 4)
    return criteria
