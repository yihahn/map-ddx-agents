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
# asks for specific, checkable criteria only, each sorted into one diagnostic category
# (CATEGORIES). Algorithm: search PubMed through E-utilities for
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
        "or laboratory methods, about animals, single-patient reports and case series, and other "
        "original studies of a patient group that do not propose or validate diagnostic "
        "criteria. Return an empty list if "
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


def _focus(text: str, limit: int = MAX_SOURCE_CHARS) -> str:
    """At most limit characters, sections about diagnosis first, the rest in document order."""
    if len(text) <= limit:
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
        if size + len(chunk) > limit:
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
    diagnosis_name: str, extract: Callable[[str, str], list], guidelines_only: bool = False,
    trail: dict | None = None, label: str = "PubMed",
) -> tuple[list, str | None, str]:
    """(criteria, source_doc, detail) from the first ranked source extract() finds criteria in.

    extract(source_doc, text) is the caller's LLM call, so this module decides which source and the
    caller decides what counts as a criterion. Every source tried is listed in detail, with why it
    was passed over, so an empty result says where it looked. guidelines_only keeps to records
    PubMed types as a guideline or consensus statement. trail, when given, gets one entry per
    article this call opened to read criteria from, keyed ("PubMed", pmid), listing what every pass
    (label) made of it. Records never opened are not recorded.
    """
    sites = trail if trail is not None else {}
    try:
        record = _ranking(diagnosis_name)
    except RuntimeError as error:
        return [], None, str(error)
    records, tried, extractions = record["results"], [], 0

    def attempt(index: int, outcome: str, source_doc: str | None = None) -> None:
        hit = records[index]
        sites.setdefault(("PubMed", hit["pmid"]), {
            "search": "PubMed", "query": record["query"], "result": index + 1,
            "title": f"{hit['title']} ({hit['journal']}, {hit['year']})",
            "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{hit['pmcid']}/",
            "types": hit["types"], "attempts": [],
        })["attempts"].append(
            {"pass": label, "outcome": outcome, **({"source_doc": source_doc} if source_doc else {})}
        )

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
            attempt(index, f"not readable: {error}")
            continue
        if not text:
            tried.append(f"{hit['pmcid']} (no full text)")
            attempt(index, "not readable: no full text")
            continue
        source_doc = f"{hit['title']} ({hit['journal']}, {hit['year']})"
        criteria = extract(source_doc, text)
        extractions += 1
        attempt(index, "used" if criteria else "read, no criteria extracted", source_doc)
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
    """The extraction prompt for a source found this way: the key findings, one per test, each in
    one diagnostic category."""
    return (
        f"Below is the text of {source_doc}, chosen as the best available guideline or paper on "
        f"diagnosing '{diagnosis_name}'.\n\n"
        f"Read the whole text and work out which findings a clinician uses to establish "
        f"{diagnosis_name}: the key diagnostic findings that a test, an examination or the "
        "documented history can check, and that point to this diagnosis rather than merely being "
        "compatible with it — a confirmatory test result, a defined threshold, a characteristic "
        "imaging or histological finding, a required exposure, a required exclusion of other "
        "causes. Write them in your own words as a short list. Do not copy sentences from the "
        "source, but keep the strength of its wording: what the source requires stays required, "
        "and what it calls typical, suggestive or 'less likely' is not turned into a requirement "
        "('makes the diagnosis less likely' does not become 'absent').\n\n"
        "What the list must contain:\n"
        "- The reference-standard or confirmatory test, whenever the source names one (biopsy, "
        "culture, PCR, a definitive imaging sign), even if it is rarely done.\n"
        "- The route by which most patients are actually diagnosed, when that differs from the "
        "definitive one — for example compatible imaging lesions when the definitive sign is "
        "usually absent, or IgM alongside IgG serology.\n"
        "- When the source requires that other causes be ruled out — it calls the condition a "
        "diagnosis of exclusion or says other causes must be excluded — one item per test or "
        "record that does the excluding, naming what it must not show ('Viral hepatitis "
        "serology: negative for acute hepatitis A, B, C and E').\n\n"
        "How to write each item:\n"
        "- One item per test or examination. Each item names the test and the result that counts, "
        "with the source's threshold if it gives one: 'CSF cell count: eosinophils > 10%', "
        "'Cardiac MRI T2-weighted imaging or T2 mapping: increased myocardial T2 signal'. An "
        "item states the finding only — no comments about the source or about how sensitive or "
        "reliable the test is.\n"
        "- Every number, threshold or value in an item must be stated in the source text, with its "
        "conditions exactly as the source gives them. Do not add values from your own knowledge; "
        "without one in the source, describe the result without a number. When the source does "
        "give a number for a finding, the item must include it ('PT INR >= 1.5', 'ALT >= 5 x "
        "ULN') — never replace it with 'abnormal', 'elevated' or 'prolonged'.\n"
        "- Keep the full range of results the source accepts. Do not narrow a finding to its "
        "typical form when the source allows others (a rash 'typically salmon-pink, but other "
        "rashes may be consistent' is 'transient rash with the fever spikes', not 'salmon-pink "
        "rash'), and list all the patterns the source accepts for an imaging finding.\n"
        "- Do not turn a typical course, a usual time window or a reference figure into a required "
        "condition. 'Symptoms usually begin 10-24 h after ingestion' or 'toxic dose: 0.5 mg/kg' "
        "describe the condition; they are not thresholds a patient must meet.\n"
        "- When subtypes are diagnosed by different tests, keep one item per subtype and name the "
        "subtype in it. An item that belongs to one subtype, stage or certainty level (definite, "
        "probable, possible) only is sufficient, specific or nonspecific, never necessary or "
        "exclusion: a patient can have the condition without meeting it.\n"
        "- Keep what the source names as it names it — exposures, animals, drugs, organisms, "
        "sites. Do not replace a list with a broader term ('cattle, sheep, goats or parturient "
        "cats' does not become 'contact with animals'; 'busulfan or cyclophosphamide' does not "
        "become 'a causative agent').\n"
        "- When the source gives more than one version of a criteria set (different years or "
        "revisions), use only the most recent one.\n\n"
        "Combination criteria and alternatives:\n"
        "- When a criteria set is a scoring or combination rule ('2 of 3', 'at least four of six', "
        "'A and at least two of the following', 'any one of the following', major and minor "
        "criteria), list the findings it is built from, each as its own item, and leave the rule "
        "out. No item may refer to other items or to how many of them are needed.\n"
        "- Do not join alternatives into one item: when the source gives alternative findings or "
        "alternative ways to show a finding ('A or B', 'any one of the following'), each "
        "alternative is its own item ('BAL eosinophils >= 25%' and 'Lung biopsy: eosinophilic "
        "pneumonia' are two items). The one exception: when the source requires at least one of "
        "the alternatives for the condition as a whole, so that the diagnosis cannot be made "
        "without one of them, keep them in one item joined with 'or', and that item is "
        "necessary (drug-induced liver injury defined as 'any one of the following: ALT > 5 x "
        "ULN …; bilirubin > 2.5 mg/dl …; INR > 1.5 …' is one item).\n"
        "- A finding the source requires alongside a set of findings is necessary ('bilirubin "
        ">= 2 mg/dL and at least 2 of the following': the bilirubin; 'asthma, eosinophilia and "
        "at least two of the following': the asthma and the eosinophilia).\n"
        "- A separate alternative, or one component of an 'at least m of n' set, is specific or "
        "nonspecific — sufficient only if it alone establishes the diagnosis — and never "
        "necessary. When the source sorts its criteria into major and minor, a major criterion "
        "is specific and a minor criterion nonspecific, except that a criterion the source calls "
        "absolute or definitive is sufficient.\n"
        "- When the criteria set is a points score (each finding adds or subtracts points and a "
        "total decides, as in RegiSCAR or RUCAM), every finding in it is specific or nonspecific, "
        "never necessary or exclusion — including a scored item for other causes having been "
        "ruled out — and the points and cut-offs are left out.\n\n"
        "Do not include:\n"
        "- findings shared by many conditions (fever, fatigue, leukocytosis, a raised CRP) unless "
        "the source makes them part of a formal criterion or case definition;\n"
        "- descriptions of what is typical, common or possible ('most patients have', 'may show', "
        "percentages, sensitivities, predictive values);\n"
        "- epidemiology, risk factors, prognosis, treatment, and staging or severity grading;\n"
        "- a monitoring threshold or result from a single study rather than the source's own "
        "diagnostic approach;\n"
        "- findings that would point to another disease, except as the exclusion items above;\n"
        "- the test results of the patients a case report or case series describes, and the "
        "inclusion or exclusion criteria of a study's population ('HIV-negative' in a study of "
        "HIV-negative patients) — they describe those patients, not the condition;\n"
        "- instructions on when or how to perform a test, and descriptions of how a test or assay "
        "works — an item says what result in the patient counts toward the diagnosis.\n\n"
        "Never list the same finding twice. Leave the list empty if the source states no specific "
        "diagnostic findings.\n\n"
        "For every item, checkable_by names the record that would settle it — a lab analyte, an "
        "imaging study, a vital sign, a documented history detail. Name the record, not the "
        "finding. If several records could settle one item, name them all in one checkable_by "
        "separated by commas. Drop any item you cannot name a record for.\n\n"
        "For every item, category says what role it plays in making the diagnosis. The "
        "categories:\n"
        "- precondition: a history, exposure or patient state the patient must have for these "
        "criteria to apply at all; without it this set of criteria is not used — the suspected "
        "drug was taken, the parasite's host was eaten, haematopoietic stem cell "
        "transplantation, exposure to extreme heat or cold. Residence in or travel to an endemic "
        "area, or any other exposure, is a precondition only when the source says the criteria "
        "apply only to patients with it; an exposure the source counts as supporting the "
        "diagnosis without requiring it is specific.\n"
        "- sufficient: a finding that on its own establishes the diagnosis — histology on biopsy, "
        "direct demonstration of the pathogen (culture, PCR, larvae in CSF). The source signals "
        "it with words such as definite, establishes the diagnosis, diagnostic of, absolute "
        "criterion, reference standard, gold standard. Write it as the positive result that "
        "establishes the diagnosis. A test that confirms the diagnosis only in a patient who also "
        "has a compatible clinical syndrome or other findings (an antibody that is diagnostic 'in "
        "a patient with a typical demyelinating event', serology that confirms 'with compatible "
        "clinical features') is specific, not sufficient.\n"
        "- exclusion: a criterion that other conditions be absent, used only when the source "
        "requires that other causes be ruled out — the diagnosis is made only once they are "
        "(for drug-induced liver injury: viral hepatitis serology negative, autoimmune serology "
        "negative, ultrasound without biliary obstruction). One item per test that does the "
        "excluding. A statement that a finding makes the diagnosis less likely, that another "
        "cause is more likely, that a condition can mimic or overlap with it, or a list of "
        "differential diagnoses is not an exclusion criterion.\n"
        "- necessary: a finding the source requires, with words such as must, required, is "
        "needed, in the presence of, for the condition as a whole, so that the diagnosis cannot "
        "be made at all without it. Not necessary: a finding the source introduces with "
        "suspected when, e.g., such as, typically, usually, often or if available; a finding "
        "required only for one subtype, stage or certainty level; and a separate alternative or "
        "a component of a set (see combination criteria above).\n"
        "- specific: a finding that, when present, greatly raises the likelihood of this "
        "diagnosis because it is uncommon in the conditions this diagnosis is usually confused "
        "with — a disease-specific antibody, a characteristic imaging finding, improvement after "
        "the suspected drug is stopped, response to a disease-specific treatment.\n"
        "- nonspecific: a finding that the conditions this diagnosis is usually confused with "
        "commonly share — headache, fever, seizures.\n"
        "Decide each item's category by asking these questions in order and taking the first "
        "answered yes:\n"
        "1. Is it a precondition for applying this set of criteria at all? -> precondition\n"
        "2. Does meeting this one item alone, without any other finding, establish the diagnosis? "
        "-> sufficient\n"
        "3. Does the source require that other causes be ruled out, and does this item do the "
        "excluding? -> exclusion\n"
        "4. Does the source require it for the condition as a whole — not for one subtype, stage "
        "or certainty level, and not as an item of a points score — so that the diagnosis cannot "
        "be made without it? -> necessary\n"
        "5. Is it uncommon in the conditions this diagnosis is usually confused with? -> "
        "specific; otherwise -> nonspecific\n\n"
        f"Source text:\n{text}"
    )


# The roles a criterion can play, in the order the prompt's questions test them. They replace the
# per-item weights (summing to 1) used through 2026-10-12, which ranked a diagnosis by how much of
# it the record bore out but could not say which findings may rule it out: every refutation
# declined, so a negative test for a finding that only confirms (neurocysticercosis declined on an
# MRI without a scolex) removed the diagnosis as surely as a missing required finding.
CATEGORIES = ("precondition", "sufficient", "exclusion", "necessary", "specific", "nonspecific")


class CategorizedCriterion(BaseModel):
    text: str
    checkable_by: str
    category: Literal["precondition", "sufficient", "exclusion", "necessary", "specific",
                      "nonspecific"]


class CategorizedCriteria(BaseModel):
    criteria: list[CategorizedCriterion]
