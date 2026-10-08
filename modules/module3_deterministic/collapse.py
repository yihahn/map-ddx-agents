import argparse
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

# Input: a PT## patient id, resolved to the newest Module 3 run directory with 03_final_ddx_list.json
# (or --run-dir), plus data_prep/mondo.json and the SNOMED CT tables. Output: 07_grouped_ddx_list.json (every
# kept DDx once, as a forest), 07_decline_ddx_list.json, 07_excluded_ddx_list.json and 07_collapse_log.json.
# Algorithm (spec_docs/module3_collapse.md, v3): A. attach MONDO (label) / SNOMED (term) codes by exact text (no LLM);
# C. inherit a declined ancestor's decline only when its refuting evidence passes four checks; D. give the
# LLM the patient's whole kept list with its evidence and apply only the "same hypothesis" / "subtype" /
# "not a diagnosis" placements two independent runs agree on; then tag each placement with how MONDO/SNOMED relate the
# pair (agrees / same / inverted, or a relation the LLM left out) — tags inform review, never move a node.

MODULES_DIR = Path(__file__).resolve().parents[1]
MODULE3_OUTPUT = MODULES_DIR / "module3_deterministic" / "output"
MONDO_JSON = Path(__file__).resolve().parents[2] / "data_prep" / "mondo.json"
UMBRELLA_MAX_DESCENDANTS = 200
VOTES = 2                    # independent runs whose verdicts must all agree
MAX_ATTEMPTS = 3             # calls allowed to collect the two decline-check votes
CALL_RETRIES = 2             # same-settings retries of one call on a truncated or unparseable reply
GATE_MAX_TOKENS = 16384      # decline checks (reasoning included)
ORGANIZE_MAX_TOKENS = 40960  # whole-list placement: ~25k tokens per call in the pilot


def _latest_run_dir(patient_id: str) -> Path:
    runs = sorted(MODULE3_OUTPUT.glob(f"{patient_id}_*"))
    for run_dir in reversed(runs):
        if (run_dir / "03_final_ddx_list.json").exists():
            return run_dir
    raise SystemExit(f"No Module 3 run with 03_final_ddx_list.json found for {patient_id}")


# ---------------------------------------------------------------------------------------------------
# MONDO
# ---------------------------------------------------------------------------------------------------

@dataclass
class Mondo:
    label2id: dict[str, str]          # lower-cased canonical label -> URI
    id2label: dict[str, str]
    parents: dict[str, set[str]]
    children: dict[str, set[str]]
    _descendants: dict[str, int] = field(default_factory=dict)

    def ancestors(self, uri: str) -> dict[str, int]:
        """Ancestor URIs mapped to their minimum is_a hop distance."""
        seen: dict[str, int] = {}
        frontier, depth = {uri}, 0
        while frontier:
            depth += 1
            frontier = {p for node in frontier for p in self.parents.get(node, ())} - seen.keys()
            for node in frontier:
                seen[node] = depth
        return seen

    def descendant_count(self, uri: str) -> int:
        if uri not in self._descendants:
            seen: set[str] = set()
            frontier = {uri}
            while frontier:
                frontier = {c for node in frontier for c in self.children.get(node, ())} - seen
                seen |= frontier
            self._descendants[uri] = len(seen)
        return self._descendants[uri]


def _load_mondo() -> Mondo:
    graph = json.loads(MONDO_JSON.read_text(encoding="utf-8"))["graphs"][0]
    label2id, id2label = {}, {}
    for node in graph["nodes"]:
        label = node.get("lbl")
        if not label:
            continue
        label2id[label.lower()] = node["id"]
        id2label[node["id"]] = label
    parents: dict[str, set[str]] = defaultdict(set)
    children: dict[str, set[str]] = defaultdict(set)
    for edge in graph["edges"]:
        if edge.get("pred") == "is_a" or edge.get("pred", "").endswith("subClassOf"):
            parents[edge["sub"]].add(edge["obj"])
            children[edge["obj"]].add(edge["sub"])
    return Mondo(label2id, id2label, parents, children)


# ---------------------------------------------------------------------------------------------------
# LLM calls and voting
# ---------------------------------------------------------------------------------------------------

# Field order is decoding order: every schema puts its reason before its verdict, so the verdict is
# conditioned on the reasoning.
class DeclineCheck(BaseModel):
    # Four separate checks instead of one "does it rule out" question: asked as one question, a marrow
    # biopsy's "no evidence of lymphoma" ruled out 8 nodal lymphoma subtypes (clinical review 2026-10-06).
    reason: str = Field(max_length=200, description="one short sentence; never more than 25 words")
    targeted_lesion: bool = Field(description="the test examined the very lesion or site where the subtype "
                                              "is suspected (e.g. the suspicious lymph node itself, not bone "
                                              "marrow when the suspected lesion is nodal)")
    adequate: bool = Field(description="the specimen and workup suffice to detect the subtype (e.g. excisional "
                                       "or adequate core biopsy with immunophenotyping, not cytology alone)")
    representative: bool = Field(description="the sample represents the suspected lesion (e.g. the whole "
                                             "suspicious node excised, not a random or peripheral sample)")
    scope_covers_subtype: bool = Field(description="the report's conclusion covers the subtype (e.g. 'no "
                                                   "evidence of lymphoma' in that tissue covers every lymphoma "
                                                   "subtype there; 'no marrow involvement' covers only the marrow)")


class Placement(BaseModel):
    reason: str = Field(max_length=300, description="one or two short sentences citing the deciding evidence or definition")
    entry: int = Field(description="number of the entry being placed under another")
    relation: Literal["same", "subtype"]
    target: int = Field(description="'same': the entry this one merges into (the representative); "
                                    "'subtype': the broader entry this one is a specific form of")


class NonDiagnosis(BaseModel):
    reason: str = Field(max_length=200, description="one short sentence")
    entry: int
    category: Literal["drug", "test_result", "symptom", "exposure", "procedure", "other"]


class Organized(BaseModel):
    placements: list[Placement] = Field(description="one per entry that is NOT top-level; unlisted entries stay top-level")
    non_diagnoses: list[NonDiagnosis] = Field(description="entries that are not diagnoses at all; usually empty")


def _structured(model_cls: type[BaseModel], max_tokens: int) -> Callable[[str], BaseModel]:
    """One structured verdict via vLLM guided decoding (response_format json_schema) on a plain
    chat.completions request — the langchain structured-output path does not engage the grammar on
    this server (measured). Truncated or unparseable output is retried with the same settings; a
    transport error has already been retried by the SDK and propagates."""
    from .llm import get_llm

    llm = get_llm(max_tokens=max_tokens)
    fmt = {"type": "json_schema",
           "json_schema": {"name": model_cls.__name__, "schema": model_cls.model_json_schema()}}

    def invoke(prompt: str) -> BaseModel:
        last_error: Exception | None = None
        for _ in range(1 + CALL_RETRIES):
            response = llm.root_client.chat.completions.create(
                model=llm.model_name, messages=[{"role": "user", "content": prompt}],
                max_tokens=llm.max_tokens, temperature=llm.temperature, top_p=llm.top_p,
                response_format=fmt)
            choice = response.choices[0]
            try:
                if choice.finish_reason == "length":
                    raise ValueError("finish_reason=length")
                return model_cls.model_validate_json(choice.message.content or "")
            except ValueError as exc:
                last_error = exc
                print(f"  {model_cls.__name__}: retry after {str(exc)[:60]} "
                      f"({response.usage.completion_tokens} tokens)", flush=True)
        raise last_error

    return invoke


def _vote(call: Callable[[], BaseModel], key: Callable[[BaseModel], object]) -> tuple[object, list[dict]]:
    """Collect VOTES valid votes in at most MAX_ATTEMPTS calls. The verdict is their shared key when they
    all agree, "split" when they disagree, "error" when too few calls succeed. A split never changes the
    list (the caller keeps its no-change default) and is surfaced for review."""
    votes: list[dict] = []
    keys: list[object] = []
    for _ in range(MAX_ATTEMPTS):
        if len(keys) == VOTES:
            break
        try:
            result = call()
            keys.append(key(result))
            votes.append({**result.model_dump(), "key": keys[-1]})
        except Exception as exc:
            votes.append({"error": str(exc)[:300]})
    if len(keys) < VOTES:
        return "error", votes
    return (keys[0] if all(k == keys[0] for k in keys) else "split"), votes


def _parallel(fn: Callable, items: list) -> list:
    """fn over items with up to MAX_CONCURRENT_REQUESTS LLM-bound calls in flight; results keep input order."""
    from .llm import MAX_CONCURRENT_REQUESTS

    with ThreadPoolExecutor(MAX_CONCURRENT_REQUESTS) as pool:
        return list(pool.map(fn, items))


# ---------------------------------------------------------------------------------------------------
# Stage A — codes by exact term (no LLM)
# ---------------------------------------------------------------------------------------------------

def assign_codes(names: list[str], mondo: Mondo, snomed) -> dict[str, dict]:
    """name -> {"mondo": URI | None, "sctid": str | None}. MONDO: the name is a canonical label. SNOMED: the
    name is a preferred/acceptable/FSN term exactly one concept carries. MONDO EXACT synonyms are not used: on
    the 9-patient set they mislabelled 10 names (e.g. Hashimoto encephalopathy -> hereditary elliptocytosis via
    the shared abbreviation HE). Codes label the output, propose stage C pairs and feed the ontology tags."""
    codes = {}
    for name in names:
        key = name.lower()
        uri = mondo.label2id.get(key)
        sctids = snomed.term2ids.get(key, set())
        codes[name] = {"mondo": uri, "sctid": next(iter(sctids)) if len(sctids) == 1 else None}
    return codes


# ---------------------------------------------------------------------------------------------------
# Stage C — decline inheritance
# ---------------------------------------------------------------------------------------------------

def _evidence_lines(item: dict, supports: bool) -> list[str]:
    """Up to 4 evidence lines of one polarity, each tagged with its source document and date."""
    lines = []
    for e in item.get("evidence", []):
        if e.get("content") and (e.get("supports") is False) == (not supports):
            source = ", ".join(str(e[k]) for k in ("source_doc", "date") if e.get(k) not in (None, "None"))
            lines.append(e["content"] + (f" [{source}]" if source else ""))
    return lines[:4]


def inherit_declines(kept: list[dict], declined: list[dict], codes: dict[str, dict], mondo: Mondo,
                     snomed) -> tuple[list[dict], list[dict], list[dict]]:
    check = _structured(DeclineCheck, GATE_MAX_TOKENS)
    by_mondo = {codes[d["diagnosis_name"]]["mondo"]: d for d in declined if codes[d["diagnosis_name"]]["mondo"]}
    by_sctid = {codes[d["diagnosis_name"]]["sctid"]: d for d in declined if codes[d["diagnosis_name"]]["sctid"]}

    def hits(item: dict) -> list[tuple[int, str, dict]]:
        """Declined ancestors of this item in either ontology, nearest first, one entry per declined name."""
        code, found = codes[item["diagnosis_name"]], {}
        for via, ancestors, table in (
                ("mondo", mondo.ancestors(code["mondo"]) if code["mondo"] else {}, by_mondo),
                ("snomed", snomed.ancestors(code["sctid"]) if code["sctid"] else {}, by_sctid)):
            for anc, hops in ancestors.items():
                if anc in table:
                    name = table[anc]["diagnosis_name"]
                    if name not in found or hops < found[name][0]:
                        found[name] = (hops, via, table[anc])
        return sorted(found.values(), key=lambda h: h[0])

    def judge(item: dict) -> tuple[str | None, list[dict]]:
        """The first declined ancestor (nearest first) whose evidence also rules this item out, if any."""
        decided, log = None, []
        for hops, via, ancestor in hits(item):
            refuting = _evidence_lines(ancestor, supports=False)
            raising = _evidence_lines(ancestor, supports=True)
            prompt = (
                f'For this patient, the diagnosis "{ancestor["diagnosis_name"]}" was ruled out based on:\n- '
                + "\n- ".join(refuting or ["(no refuting evidence recorded)"])
                + ("\n\nThe findings that had raised it:\n- " + "\n- ".join(raising) if raising else "")
                + f'\n\nDoes that ruling-out evidence also remove the basis for suspecting its subtype'
                  f' "{item["diagnosis_name"]}" in this patient? It does when the test examined the lesion that'
                  " raised the suspicion, adequately and representatively, and its conclusion covers the subtype;"
                  " a negative result from another site or a limited sample is local evidence and does not."
                  " Judge each check by its own definition — do not set one to false merely because the disease"
                  " could still arise at a site nobody suspected. Set a check to false when it is not met or the"
                  " evidence does not let you tell.")
            verdict, votes = _vote(lambda: check(prompt), lambda v: "yes" if all(
                (v.targeted_lesion, v.adequate, v.representative, v.scope_covers_subtype)) else "no")
            log.append({"child": item["diagnosis_name"], "declined_ancestor": ancestor["diagnosis_name"],
                        "hops": hops, "via": via, "votes": votes, "verdict": verdict})
            if verdict == "yes":
                decided = ancestor["diagnosis_name"]
                break
        return decided, log

    asked = [item for item in kept if hits(item)]
    print(f"decline: {len(asked)} kept diagnoses have a declined ancestor", flush=True)
    still_kept, inherited, log = [], [], []
    for item, (decided, item_log) in zip(kept, _parallel(judge, kept)):
        log += item_log
        for e in item_log:
            print(f"  decline {e['child']} <- {e['declined_ancestor']} ({e['via']}): {e['verdict']}", flush=True)
        if decided:
            inherited.append({**item, "status": "declined_inherited", "declined_via": decided})
        else:
            still_kept.append(item)
    return still_kept, inherited, log


# ---------------------------------------------------------------------------------------------------
# Stage D — contextual organization of the whole list
# ---------------------------------------------------------------------------------------------------

_ORGANIZE_RULES = """You are a clinician organizing ONE patient's differential diagnosis (DDx) list so a reviewer can
read it faster. Nothing is deleted: you only place some entries under others, and flag entries that are not
diagnoses at all.

Two kinds of placement:
1. same — for THIS patient the two entries express the same diagnostic hypothesis: synonyms, or a generic name
   that, given this patient's evidence, refers to exactly the entity the other entry names (e.g. "Hypersensitivity
   syndrome" in a patient with a culprit drug and eosinophilia = DRESS). The target (representative) is the entry
   the evidence supports most specifically. Similar names are NOT enough: a different cause, agent, site, organism
   or mechanism means different hypotheses — keep them apart.
2. subtype — by strict clinical definition every case of the entry is a case of the target (e.g. pneumococcal
   pneumonia is a subtype of pneumonia). Judge this by definition, not by this patient's evidence.
If one entry is a broader category that contains the other, that is a subtype placement with the BROADER entry as
target — never "same", even when the evidence points to the narrower one.

Clinical rulings to respect:
- Pneumonia and pneumonitis are different: infectious pneumonia is not a subtype of pneumonitis. Pneumonitis is
  within interstitial lung disease (ILD). Organizing pneumonia, AIP, DIP, NSIP and LIP belong to the ILD family, not
  under pneumonia or pneumonitis.
- Immune checkpoint inhibitor pneumonitis is a subtype of pneumonitis and of drug-induced ILD.
- Drug-induced liver injury is a subtype of hepatotoxicity, liver injury and toxic hepatitis; methotrexate-induced
  liver injury is a subtype of methotrexate toxicity.
- Microscopic colitis is not placed under inflammatory bowel disease. IVLBCL is a distinct entity, not under DLBCL.
- When unsure, leave the entry top-level.

Not a diagnosis: only a drug or drug class, a single laboratory or imaging value, an isolated symptom, an exposure,
or a procedure. Clinical syndromes (e.g. rhabdomyolysis, septic shock) ARE diagnoses.

Output: one placement per entry that is NOT top-level, and the non-diagnoses (usually none). Each entry appears at
most once as "entry"; use the entry numbers exactly as listed."""


def _entry_text(number: int, item: dict) -> str:
    proposal, emr = [], []
    for e in item.get("evidence", []):
        if not e.get("content"):
            continue
        sign = "+" if e.get("supports") else "-"
        if e.get("source_doc") not in (None, "None"):
            src = ", ".join(str(e[k]) for k in ("source_doc", "date") if e.get(k) not in (None, "None"))
            emr.append(f"    {sign} {e['content']} [{src}]")
        else:
            proposal.append(f"    {sign} {e['content']}")
    parts = [f"[{number}] {item['diagnosis_name']} (Module 3 status: {item.get('status')})"]
    if proposal:
        parts.append("  Proposal rationale (vignette):\n" + "\n".join(proposal))
    parts.append("  EMR verification:\n" + ("\n".join(emr) if emr else "    (no EMR finding confirmed)"))
    return "\n".join(parts)


def _validated(run: dict, n: int) -> tuple[dict[int, tuple[str, int]], dict[int, str]]:
    """entry -> (relation, target) and entry -> category, dropping out-of-range, self and repeated entries."""
    placements, non_dx = {}, {}
    for p in run["placements"]:
        if 1 <= p["entry"] <= n and 1 <= p["target"] <= n and p["entry"] != p["target"] and p["entry"] not in placements:
            placements[p["entry"]] = (p["relation"], p["target"])
    for x in run["non_diagnoses"]:
        if 1 <= x["entry"] <= n:
            non_dx.setdefault(x["entry"], x["category"])
    return placements, non_dx


def organize(kept: list[dict]) -> dict:
    """Place entries by the whole-list judgement of VOTES independent runs; apply only what all runs agree on."""
    if len(kept) < 2:
        return {"runs": [], "placements": {}, "excluded": {}, "disputed": [], "lineage": set(), "status": "ok"}
    call = _structured(Organized, ORGANIZE_MAX_TOKENS)
    prompt = _ORGANIZE_RULES + "\n\nDDx entries:\n" + "\n".join(_entry_text(i, it) for i, it in enumerate(kept, 1))

    def one(run: int):
        t0 = time.time()
        try:
            out = {**call(prompt).model_dump(), "seconds": round(time.time() - t0)}
            print(f"organize run {run + 1}: {out['seconds']}s, {len(out['placements'])} placements, "
                  f"{len(out['non_diagnoses'])} non-diagnoses", flush=True)
        except Exception as exc:
            out = {"error": str(exc)[:300], "seconds": round(time.time() - t0)}
            print(f"organize run {run + 1}: failed after {out['seconds']}s: {out['error']}", flush=True)
        return out

    print(f"organize: {len(kept)} entries, {VOTES} runs", flush=True)

    runs = _parallel(one, list(range(VOTES)))
    if any("error" in r for r in runs):
        return {"runs": runs, "placements": {}, "excluded": {}, "disputed": [], "lineage": set(), "status": "error"}
    checked = [_validated(r, len(kept)) for r in runs]
    excluded = {e: checked[0][1][e] for e in checked[0][1] if all(e in c[1] for c in checked[1:])}
    placements = {e: v for e, v in checked[0][0].items()
                  if all(c[0].get(e) == v for c in checked[1:]) and e not in excluded and v[1] not in excluded}
    # A cycle (a -> b -> a) has no root to hang from: drop the placement that closes it.
    for entry in list(placements):
        seen, cur = {entry}, placements.get(entry, (None, None))[1]
        while cur in placements:
            if cur in seen:
                placements.pop(entry, None)
                break
            seen.add(cur)
            cur = placements[cur][1]
    # Runs that placed an entry as a subtype under different targets on ONE agreed lineage (e.g. T-cell
    # lymphoma vs peripheral T-cell lymphoma, itself agreed to sit under T-cell lymphoma) agree on the
    # lineage: the entry goes under the most specific of those targets (clinical decision 2026-10-08).
    def chain(x: int) -> list[int]:
        out = []
        while x in placements and x not in out:
            x = placements[x][1]
            out.append(x)
        return out
    lineage = set()
    for entry in sorted({e for c in checked for e in c[0]} - set(placements) - set(excluded)):
        proposals = [c[0].get(entry) for c in checked]
        if None in proposals or any(rel != "subtype" or t in excluded for rel, t in proposals):
            continue
        targets = {t for _, t in proposals}
        deepest = next((t for t in targets if all(o == t or o in chain(t) for o in targets)), None)
        if deepest is not None and entry not in [deepest] + chain(deepest):
            placements[entry] = ("subtype", deepest)
            lineage.add(entry)
    proposed = {e for c in checked for e in c[0]} | {e for c in checked for e in c[1]}
    disputed = sorted(proposed - set(placements) - set(excluded))
    return {"runs": runs, "placements": placements, "excluded": excluded, "disputed": disputed,
            "lineage": lineage, "status": "ok"}


def _reasons(runs: list[dict], entry: int, kind: str = "placements") -> list[str]:
    return [p["reason"] for r in runs for p in r.get(kind, []) if p["entry"] == entry]


# ---------------------------------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------------------------------

def _emr_support(item: dict) -> int:
    # Module 3's "supported" status needs every criterion confirmed, so the count of EMR-traced supporting
    # judgements is the usable signal for a reviewer.
    return sum(1 for e in item.get("evidence", []) if e.get("supports") and e.get("source_doc"))


# Pairs the clinician ruled apart although an ontology links them: never offered as a suggestion.
_RULED_APART = {("pneumonia", "pneumonitis"), ("acute interstitial pneumonitis", "pneumonitis"),
                ("acute interstitial pneumonitis", "pneumonia"), ("organizing pneumonia", "pneumonia"),
                ("organizing pneumonia", "pneumonitis"), ("cryptogenic organizing pneumonia", "pneumonia"),
                ("bronchiolitis obliterans organizing pneumonia", "pneumonia"),
                ("acute fibrinous and organizing pneumonia", "pneumonia"),
                ("intravascular large b-cell lymphoma", "diffuse large b-cell lymphoma"),
                ("microscopic colitis", "inflammatory bowel disease"), ("collagenous colitis", "inflammatory bowel disease"),
                ("lymphocytic colitis", "inflammatory bowel disease"), ("acute disseminated encephalomyelitis", "encephalitis"),
                ("renal osteodystrophy", "secondary hyperparathyroidism"), ("renal osteodystrophy", "rickets"),
                ("acute encephalopathy with biphasic seizures and late reduced diffusion", "epilepsy"),
                ("acute respiratory distress syndrome", "interstitial lung disease")}


def _ruled_apart(child: str, parent: str) -> bool:
    child, parent = child.lower(), parent.lower()
    return (child, parent) in _RULED_APART or (parent == "pneumonitis" and child.endswith("pneumonia"))


def _ontology_tags(roots: list[dict], codes: dict[str, dict], mondo: Mondo, snomed) -> list[dict]:
    """Tag the forest in place. Placed node: "ontology" = {"tag": agrees | same | inverted, "via": [...]} when
    MONDO or SNOMED relates it to its parent (agreement wins over contradiction). Any node:
    "ontology_suggests" = relations an ontology states that the forest leaves out. Returns the tags for the log."""
    anc_cache: dict[tuple[str, str], dict] = {}

    def anc(onto: str, code: str) -> dict:
        if (onto, code) not in anc_cache:
            anc_cache[(onto, code)] = mondo.ancestors(code) if onto == "MONDO" else snomed.ancestors(code)
        return anc_cache[(onto, code)]

    def relate(a: str, b: str):
        """'same' | 'below' (b is an ancestor of a) | 'above', with the ontologies saying so; None if unlinked."""
        found: dict[str, list[str]] = {}
        for onto, key in (("MONDO", "mondo"), ("SNOMED", "sctid")):
            x, y = codes.get(a, {}).get(key), codes.get(b, {}).get(key)
            if x and y:
                rel = "same" if x == y else "below" if y in anc(onto, x) else "above" if x in anc(onto, y) else None
                if rel:
                    found.setdefault(rel, []).append(onto)
        rel = next((r for r in ("same", "below", "above") if r in found), None)
        return (rel, found[rel]) if rel else None

    nodes, above = [], {}

    def walk(level: list[dict], chain: list[dict]):
        for n in level:
            nodes.append(n)
            above[n["diagnosis_name"]] = [c["diagnosis_name"] for c in chain]
            n["ontology"], n["ontology_suggests"] = None, []
            if chain:
                r = relate(n["diagnosis_name"], chain[-1]["diagnosis_name"])
                if r:
                    n["ontology"] = {"tag": {"same": "same", "below": "agrees", "above": "inverted"}[r[0]], "via": r[1]}
            walk(n["children"], chain + [n])
    walk(roots, [])

    log = [{"name": n["diagnosis_name"], "parent": above[n["diagnosis_name"]][-1], **n["ontology"]}
           for n in nodes if n["ontology"]]
    for a in nodes:
        for b in nodes:
            na, nb = a["diagnosis_name"], b["diagnosis_name"]
            if na == nb or nb in above[na] or na in above[nb]:
                continue
            r = relate(na, nb)
            if r and (r[0] == "below" or (r[0] == "same" and na < nb)) and not _ruled_apart(na, nb):
                s = {"target": nb, "relation": "subtype" if r[0] == "below" else "same", "via": r[1]}
                a["ontology_suggests"].append(s)
                log.append({"name": na, "tag": "suggests", **s})
    return log


def _relabel(item: dict, codes: dict[str, dict], mondo: Mondo, snomed) -> dict:
    code = codes.get(item["diagnosis_name"], {"mondo": None, "sctid": None})
    uri, sctid = code["mondo"], code["sctid"]
    return {**item, "mondo_id": uri.split("/")[-1] if uri else None, "mondo_label": mondo.id2label.get(uri or ""),
            "sctid": sctid, "snomed_term": snomed.preferred.get(sctid) if sctid else None}


def retag(run_dir: Path) -> dict:
    """Recompute codes, labels and ontology tags on an existing v3 result — no LLM, placements untouched."""
    from .snomed import load_terms

    mondo, snomed = _load_mondo(), load_terms()
    files = {k: json.loads((run_dir / f"07_{k}.json").read_text(encoding="utf-8"))
             for k in ("grouped_ddx_list", "decline_ddx_list", "excluded_ddx_list", "collapse_log")}

    def flat(level):
        for n in level:
            yield n
            yield from flat(n["children"])
    names = list(dict.fromkeys(n["diagnosis_name"] for n in list(flat(files["grouped_ddx_list"]))
                               + files["decline_ddx_list"] + files["excluded_ddx_list"]))
    codes = assign_codes(names, mondo, snomed)

    def redo(level):
        out = []
        for n in level:
            n = _relabel(n, codes, mondo, snomed)
            uri = codes[n["diagnosis_name"]]["mondo"]
            n["categorical"] = bool(uri) and mondo.descendant_count(uri) > UMBRELLA_MAX_DESCENDANTS
            n["children"] = redo(n["children"])
            out.append(n)
        return out
    roots = redo(files["grouped_ddx_list"])
    log = files["collapse_log"]
    log["codes"] = [{"name": n, "mondo_id": c["mondo"].split("/")[-1] if c["mondo"] else None, "sctid": c["sctid"]}
                    for n, c in codes.items()]
    log["ontology_tags"] = _ontology_tags(roots, codes, mondo, snomed)
    out = {"grouped_ddx_list": roots, "collapse_log": log,
           "decline_ddx_list": [_relabel(d, codes, mondo, snomed) for d in files["decline_ddx_list"]],
           "excluded_ddx_list": [_relabel(x, codes, mondo, snomed) for x in files["excluded_ddx_list"]]}
    for k, data in out.items():
        (run_dir / f"07_{k}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def collapse(run_dir: Path) -> dict:
    from .snomed import load_terms

    kept = json.loads((run_dir / "03_final_ddx_list.json").read_text(encoding="utf-8"))
    declined = json.loads((run_dir / "decline_ddx_list.json").read_text(encoding="utf-8"))
    mondo, snomed = _load_mondo(), load_terms()

    names = list(dict.fromkeys(item["diagnosis_name"] for item in kept + declined))
    codes = assign_codes(names, mondo, snomed)
    kept, inherited, decline_log = inherit_declines(kept, declined, codes, mondo, snomed)
    org = organize(kept)

    review = [{"stage": "decline", "name": e["child"], "detail": f'{e["verdict"]} ({e["declined_ancestor"]})'}
              for e in decline_log if e["verdict"] in ("split", "error")]
    if org["status"] == "error":
        review.append({"stage": "organize", "name": "(whole list)",
                       "detail": "; ".join(r.get("error", "") for r in org["runs"])})
    proposals = {}
    for entry in org["disputed"]:
        proposals[entry] = [next((f'{p["relation"]} -> {kept[p["target"] - 1]["diagnosis_name"]}'
                                  for p in r["placements"] if p["entry"] == entry and 1 <= p["target"] <= len(kept)),
                                 "not a diagnosis" if any(x["entry"] == entry for x in r["non_diagnoses"]) else "top-level")
                            for r in org["runs"]]
        review.append({"stage": "organize", "name": kept[entry - 1]["diagnosis_name"],
                       "detail": " | ".join(proposals[entry])})
    flagged = {r["name"] for r in review}

    def label(item: dict) -> dict:
        return {**_relabel(item, codes, mondo, snomed), "needs_review": item["diagnosis_name"] in flagged}

    nodes = {}
    for i, item in enumerate(kept, 1):
        if i in org["excluded"]:
            continue
        uri = codes[item["diagnosis_name"]]["mondo"]
        relation = org["placements"].get(i, (None, None))[0]
        nodes[i] = {**label(item), "emr_support": _emr_support(item),
                    "categorical": bool(uri) and mondo.descendant_count(uri) > UMBRELLA_MAX_DESCENDANTS,
                    "relation": relation, "placement_reasons": _reasons(org["runs"], i) if relation else [],
                    "children": []}
    roots = []
    for i, node in nodes.items():
        parent = org["placements"].get(i, (None, None))[1]
        (nodes[parent]["children"] if parent in nodes else roots).append(node)
    tags = _ontology_tags(roots, codes, mondo, snomed)

    excluded_out = [{**label(kept[i - 1]), "status": "excluded_non_diagnosis", "exclusion_category": cat,
                     "exclusion_reasons": _reasons(org["runs"], i, "non_diagnoses")} for i, cat in org["excluded"].items()]
    declined_out = [label(d) for d in declined + inherited]
    from .llm import BACKENDS, DEFAULT_BACKEND, QWEN_SERVER
    log = {
        "llm": {"server": QWEN_SERVER, "model": BACKENDS[DEFAULT_BACKEND][1]},
        "codes": [{"name": n, "mondo_id": c["mondo"].split("/")[-1] if c["mondo"] else None, "sctid": c["sctid"]}
                  for n, c in codes.items()],
        "decline": decline_log,
        "organize": {"status": org["status"], "runs": org["runs"],
                     "placements": [{"entry": kept[e - 1]["diagnosis_name"], "relation": rel,
                                     "target": kept[t - 1]["diagnosis_name"], "reasons": _reasons(org["runs"], e),
                                     "rule": "lineage" if e in org["lineage"] else "exact"}
                                    for e, (rel, t) in org["placements"].items()],
                     "excluded": [{"name": kept[e - 1]["diagnosis_name"], "category": c,
                                   "reasons": _reasons(org["runs"], e, "non_diagnoses")} for e, c in org["excluded"].items()],
                     "disputed": [{"name": kept[e - 1]["diagnosis_name"], "proposals": proposals[e]} for e in org["disputed"]]},
        "ontology_tags": tags,
        "review": review,
    }
    for filename, data in [("07_grouped_ddx_list.json", roots), ("07_decline_ddx_list.json", declined_out),
                           ("07_excluded_ddx_list.json", excluded_out), ("07_collapse_log.json", log)]:
        (run_dir / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"groups": roots, "declined": declined_out, "excluded": excluded_out, "log": log}


def _print_tree(nodes: list[dict], depth: int = 0) -> None:
    for node in nodes:
        tag = f" [{node['relation']}]" if node.get("relation") else ""
        flags = (" (categorical)" if node.get("categorical") else "") + (" (REVIEW)" if node.get("needs_review") else "")
        print(f"{'  ' * depth}- {node['diagnosis_name']}{tag}{flags}  emr_support={node['emr_support']}")
        _print_tree(node["children"], depth + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Organize a patient's DDx list after Module 3.")
    parser.add_argument("patient", help="Patient id, e.g. PT01")
    parser.add_argument("--run-dir", type=Path, default=None, help="Explicit Module 3 run directory")
    parser.add_argument("--retag", action="store_true", help="Only recompute codes and ontology tags (no LLM)")
    args = parser.parse_args()

    run_dir = args.run_dir or _latest_run_dir(args.patient)
    if args.retag:
        tags = retag(run_dir)["collapse_log"]["ontology_tags"]
        print(f"Retagged {run_dir.name}: " + ", ".join(f"{t} {sum(x['tag'] == t for x in tags)}"
                                                      for t in ("agrees", "same", "inverted", "suggests")))
        return
    result = collapse(run_dir)
    log = result["log"]
    print(f"Run directory: {run_dir}")
    print(f"Organize: {log['organize']['status']}, " + ", ".join(
        f"run {i + 1} {r.get('seconds')}s" for i, r in enumerate(log["organize"]["runs"])))
    print("Grouped DDx:")
    _print_tree(result["groups"], 1)
    print(f"\nInherited declines: {', '.join(d['diagnosis_name'] for d in result['declined'] if d.get('declined_via')) or 'none'}")
    print(f"Excluded non-diagnoses: {', '.join(e['diagnosis_name'] for e in result['excluded']) or 'none'}")
    for flag in log["review"]:
        print(f"REVIEW [{flag['stage']}] {flag['name']}: {flag['detail']}")


if __name__ == "__main__":
    main()
