import html
import json
import re
import threading
import urllib.parse
from pathlib import Path
from typing import Callable

import requests

from . import criteria_extract, criteria_web
from . import ddxcriteria_sources as dx
from .criteria_extract import MAX_TEXT_CHARS, Extraction
from .criteria_literature import MIN_SOURCE_CHARS, _focus, _key

# Input: a diagnosis name. Output: find_criteria(name, trail) -> (extraction, source_doc, detail,
# extractions): the criteria items and rules of the source used (criteria_extract.Extraction), and
# every source read. Algorithm: ddxcriteria_sources.retrieve collects the open-access full texts of
# the papers that state the diagnosis's criteria (PubMed + Europe PMC, rule-scored, no LLM) and says
# whether a web search is still needed and why; the texts are tried by retrieve()'s priority (the
# title: this diagnosis's guideline or criteria), completeness breaking ties — whole, with their
# supplements, cut only past the model's context — each read twice by
# criteria_extract.extract_best, and the first that yields criteria is used, with a newer version of
# one of its sets merged in when it names one and another fetched text states it. The order follows
# retrieve()'s reason: no text or only weak ones → DuckDuckGo first; a better paper left unfetched →
# a web search for that paper by its title first, then DuckDuckGo before the fetched texts when they
# score far below that paper, else the fetched texts, then DuckDuckGo.

_ROOT = Path(__file__).resolve().parents[2] / "embeddings" / "ddxcriteria"
_DOCS_DIR = _ROOT / "docs"      # retrieve()'s output folders, one per diagnosis
_FILE_CACHE = _ROOT / "cache"   # retrieve()'s cross-run file cache
# How many of the unfetched better papers (web_search_hints) are looked for on the web, and how many
# search results per paper are opened. Each is one DuckDuckGo query, which is spaced 20 s apart and
# blocked after about a dozen in a row (criteria_web), so this is kept small.
MAX_HINTS = 2
MAX_HINT_RESULTS = 3
# How far the best fetched paper's priority may fall below the best unfetched one's before DuckDuckGo
# is searched ahead of the fetched texts (2026-10-29: Acute eosinophilic pneumonia's Philit 2002 at 10
# was paywalled, and the fetched texts at 3-5 were an asthma consensus and a military study's index).
WEB_FIRST_GAP = 4
# The text one extraction may take (criteria_extract: the model's context less the prompt and its
# 65,536-token answer room). Only a text longer than this is cut, diagnosis sections first.
MAX_DOC_CHARS = MAX_TEXT_CHARS
_TITLE_WORD = re.compile(r"[a-z0-9]+")
_cache_lock = threading.Lock()


def _retrieve(diagnosis_name: str) -> dict:
    """retrieve()'s result for this diagnosis, from disk when it ran before. PubMed's top results
    differ from call to call, so a rerun reuses the first answer instead of searching again."""
    path = _ROOT / f"retrieve_{_key(diagnosis_name)}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    result = dx.retrieve(diagnosis_name, outdir=str(_DOCS_DIR), cache_dir=str(_FILE_CACHE))
    # Every candidate with its score and the reasons for it, trimmed to what shows why one paper
    # was preferred to another — the record a wrong pick is traced through.
    result["candidates"] = [{f: c.get(f) for f in ("pmid", "title", "year", "priority", "priority_reasons",
                                                   "doc_kind", "excluded", "fetched")}
                            for c in result.get("candidates") or []]
    for doc in result["documents"]:
        doc.pop("_crefs_all", None)
    # A search that still failed after ddxcriteria_sources' retries lost its candidates; caching
    # that result would make every later run reuse the gap (61 of 178 diagnoses on 2026-10-28), so
    # it is used for this run only and the next one searches again.
    if any(q.get("warning", "").startswith("ERROR") for q in result["queries"].values()):
        return result
    with _cache_lock:
        _ROOT.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def _doc_text(doc: dict) -> str:
    """A fetched document's body (its bibliographic header dropped) with its supplements' texts."""
    raw = Path(doc["path"]).read_text(encoding="utf-8", errors="replace")
    body = raw.split("\n---\n", 1)[1] if raw.startswith("---") and "\n---\n" in raw else raw
    parts = [body]
    for sup in doc.get("supplements") or []:
        if sup.get("path") and Path(sup["path"]).exists():
            parts.append(f"# Supplementary material: {sup.get('file', '')}\n"
                         + Path(sup["path"]).read_text(encoding="utf-8", errors="replace"))
    text = "\n\n".join(parts)
    return text if len(text) <= MAX_DOC_CHARS else _focus(text, MAX_DOC_CHARS)


def _doc_label(doc: dict) -> str:
    return f"{doc.get('title', '').rstrip('.')} ({doc.get('journal', '')}, {doc.get('year', '')}; PMID {doc.get('pmid') or doc['key']})"


def _need_kind(result: dict) -> str:
    """Which of retrieve()'s four reasons made it ask for a web search, or "" when it did not."""
    if not result["needs_web_search"]:
        return ""
    if not result["documents"]:
        return "no_text"
    if result["n_weak_documents"] == result["n_documents"]:
        return "weak_only"
    if "미확보" in result["web_search_reason"]:
        return "better_unfetched"
    return "no_core"


def _same_title(a: str, b: str) -> bool:
    """Whether a search result's title names the same paper: most of the paper's title words in it."""
    want = set(_TITLE_WORD.findall(a.lower())) - dx.STOPWORDS
    got = set(_TITLE_WORD.findall(html.unescape(b).lower()))
    return bool(want) and len(want & got) / len(want) >= 0.6


def _hint_text(url: str, title: str) -> tuple[str, str]:
    """(full text or "", how): a page or PDF found for a paper, kept only when it is the paper's
    full text rather than an abstract or a paywall page (ddxcriteria_sources' own length checks)."""
    if url.lower().split("?")[0].endswith(".pdf"):
        raw, err, _ = dx.http_get(url, accept="application/pdf")
        text = dx.pdf_to_text(raw) if raw and raw[:5] == b"%PDF-" else None
        if not text or len(text) < dx.MIN_PDF_CHARS:
            return "", f"PDF not read ({err or 'no text'})"
        return (text if len(text) <= MAX_DOC_CHARS else _focus(text, MAX_DOC_CHARS)), "PDF"
    text, how = criteria_web._page_text(url, title)
    if not text:
        return "", how or "too short"
    if not ("PMC" in how or dx.html_is_fulltext(text)):
        return "", f"{how}, not the full text"
    return text, how


def _find_hinted(result: dict, extract: Callable[[str, str], list], sites: dict) -> tuple[list, str | None, str]:
    """Criteria from a web copy of one of the better papers retrieve() could not fetch."""
    tried = []
    for hint in result["web_search_hints"][:MAX_HINTS]:
        query = f'"{hint["title"].rstrip(".")}"'
        try:
            results = criteria_web._search(query)
        except (RuntimeError, requests.RequestException) as error:
            tried.append(f"{hint['title'][:60]}: search failed ({error})")
            continue
        hits = [h for h in results if _same_title(hint["title"], h["title"])
                and not any(d in h["href"] for d in criteria_web.EXCLUDED_DOMAINS)][:MAX_HINT_RESULTS]
        if not hits:
            tried.append(f"{hint['title'][:60]}: no result with that title")
        for hit in hits:
            text, how = _hint_text(hit["href"], hit["title"])
            site = sites.setdefault(("web copy", hit["href"]), {
                "search": "DuckDuckGo (paper title)", "query": query, "title": html.unescape(hit["title"]),
                "url": hit["href"], "kind": hint.get("doc_kind"), "paper": hint, "attempts": []})
            if not text:
                site["attempts"].append({"pass": "better paper on the web", "outcome": f"not readable: {how}"})
                tried.append(f"{hit['href']} ({how})")
                continue
            domain = urllib.parse.urlparse(hit["href"]).netloc.removeprefix("www.")
            source_doc = f"{hint['title'].rstrip('.')} ({hint.get('journal', '')}, {hint.get('year', '')}; {domain})"
            criteria = extract(source_doc, text)
            site["attempts"].append({"pass": "better paper on the web",
                                     "outcome": "used" if criteria else "read, no criteria extracted",
                                     "source_doc": source_doc})
            if criteria:
                return criteria, source_doc, (f"{hit['href']}, via {how} — web copy of PMID {hint.get('pmid') or '-'} "
                                              f"found for {query}")
            tried.append(f"{hit['href']} (no specific criteria)")
    return [], None, "no web copy of the better papers yielded criteria: " + ("; ".join(tried) or "no hints")


_SET = re.compile(r"^(.*?)[\s,]*((?:19|20)\d{2})\s*$")


def _set_key(label: str) -> tuple[frozenset, int]:
    """A rule's criteria set as (name words, year): "Del Brutto 2017" -> ({del, brutto}, 2017)."""
    m = _SET.match(str(label or "").strip())
    name, year = (m.group(1), int(m.group(2))) if m else (str(label or ""), 0)
    return frozenset(_TITLE_WORD.findall(name.lower())) - {"et", "al", "criteria", "the"}, year


def _same_set(a: frozenset, b: frozenset) -> bool:
    return bool(a and b) and (a <= b or b <= a)


def _set_years(extraction: Extraction) -> list[tuple[frozenset, int]]:
    return [_set_key(r.get("set")) for r in extraction.rules if r.get("set")]


def _newer_wanted(extraction: Extraction) -> list[tuple[frozenset, int, str]]:
    """The newer versions the source mentions of a set it states, newer than the version it states."""
    have = _set_years(extraction)
    wanted = []
    for v in extraction.newer_versions:
        name, _ = _set_key(f"{v.get('set', '')} {v.get('year', '')}")
        year = dx._int(v.get("year"), 0)
        stated = [y for n, y in have if _same_set(n, name)]
        if name and year and stated and year > max(stated):
            wanted.append((name, year, f"{v.get('set')} {year}"))
    return wanted


def _carries(text: str, name: frozenset, year: int) -> bool:
    """Whether a document's text names the set (all its name words) with that year."""
    low = text.lower()
    return str(year) in low and all(w in low for w in name)


def _replace_set(base: Extraction, newer: Extraction, name: frozenset, year: int, prefix: str) -> Extraction:
    """base with one criteria set swapped for its newer version from another source: base's rules of
    that set go, with the items only those rules used; newer's rules of the set (year >= the one
    wanted) and the items they use come in, their ids prefixed so they cannot clash with base's."""
    def uses(rule: dict) -> set[str]:
        ids = {i for g in rule.get("groups") or [] if isinstance(g, dict) for i in g.get("items") or []}
        ids |= set((rule.get("points") or {}).keys()) | set(rule.get("gate") or [])
        # A scored finding's bands and counted lists name their items inside (2026-10-30 format).
        for scored in (rule.get("points") or {}).values():
            if isinstance(scored, dict):
                ids |= {b.get("item") for b in scored.get("bands") or [] if isinstance(b, dict) and b.get("item")}
                ids |= {i for items in (scored.get("count_of") or {}).values() for i in items or []}
        return {str(i) for i in ids}

    def in_set(rule: dict, min_year: int) -> bool:
        n, y = _set_key(rule.get("set"))
        return _same_set(n, name) and y >= min_year

    new_rules = [r for r in newer.rules if in_set(r, year)]
    if not new_rules:
        return base
    kept_rules = [r for r in base.rules if not in_set(r, 0)]
    dropped_ids = set().union(*(uses(r) for r in base.rules if in_set(r, 0))) - set().union(set(), *(uses(r) for r in kept_rules))
    new_ids = set().union(*(uses(r) for r in new_rules))
    rename = {i.id: f"{prefix}{i.id}" for i in newer.items if i.id in new_ids}

    def renamed(rule: dict) -> dict:
        out = dict(rule)
        if out.get("groups"):
            out["groups"] = [{**g, "items": [rename.get(str(i), i) for i in g.get("items") or []]}
                             for g in out["groups"] if isinstance(g, dict)]
        if out.get("points"):
            def scored(v):
                if not isinstance(v, dict):
                    return v
                return {**v,
                        "bands": [{**b, "item": rename.get(str(b.get("item")), b.get("item"))} if isinstance(b, dict) and "item" in b
                                  else b for b in v.get("bands") or []],
                        **({"count_of": {k: [rename.get(str(i), i) for i in items or []]
                                         for k, items in v["count_of"].items()}} if isinstance(v.get("count_of"), dict) else {})}
            out["points"] = {rename.get(str(k), k): scored(v) for k, v in out["points"].items()}
        if out.get("gate"):
            out["gate"] = [rename.get(str(i), i) for i in out["gate"]]
        out["id"] = f"{prefix}{out.get('id', '')}"
        return out

    items = [i for i in base.items if i.id not in dropped_ids] + [
        i._replace(id=rename[i.id]) for i in newer.items if i.id in rename]
    meta = {**base.meta, "merged": base.meta.get("merged", []) + [{
        "set": f"{' '.join(sorted(name))} {year}", "from": newer.meta.get("source_doc"),
        "rules_replaced": len(base.rules) - len(kept_rules), "rules_added": len(new_rules),
        "items_dropped": len(dropped_ids), "items_added": len(rename)}]}
    return Extraction(items, kept_rules + [renamed(r) for r in new_rules], base.newer_versions, meta)


def _find_fetched(result: dict, once: Callable[[str, str], Extraction], sites: dict) -> tuple[Extraction | None, str | None, str]:
    """Criteria from the first fetched document, by priority then completeness, that the extraction finds
    them in — and, only when that document says a set it states has a newer version, that newer
    version taken from the fetched document that carries it, merged in its place (_replace_set)."""
    # Priority first: completeness counts criteria-like tables and numbers whatever condition they
    # belong to (2026-10-29: a juvenile Still's/MAS meeting report at 18 came before the adult-onset
    # Still's guideline at 5), while priority says from the title whether the paper is about this
    # diagnosis's criteria.
    docs = sorted(result["documents"], key=lambda d: (-d.get("priority", 0), -d.get("completeness", 0),
                                                      -dx._int(d.get("year"), 0)))
    texts: dict[str, str] = {}
    tried = []

    def read(doc: dict, pass_label: str) -> tuple[Extraction | None, str]:
        site = sites.setdefault(("ddxcriteria", doc["key"]), {
            "search": "ddxcriteria", "title": doc.get("title", ""), "url": doc.get("url", ""),
            "pmid": doc.get("pmid", ""), "kind": doc.get("doc_kind"), "tier": doc.get("tier"),
            "completeness": doc.get("completeness"), "text_format": doc.get("text_format"),
            "n_suppl": doc.get("n_suppl", 0), "attempts": []})
        if not doc.get("path") or not Path(doc["path"]).exists():
            site["attempts"].append({"pass": pass_label, "outcome": "not readable: file missing"})
            return None, "file missing"
        text = texts.setdefault(doc["key"], _doc_text(doc))
        if len(text) < MIN_SOURCE_CHARS:
            site["attempts"].append({"pass": pass_label, "outcome": "not readable: too short"})
            return None, "too short"
        source_doc = _doc_label(doc)
        extraction = once(source_doc, text)
        extraction.meta["source_doc"] = source_doc
        site["attempts"].append({"pass": pass_label, "outcome": "used" if extraction.items else
                                 f"not about the diagnosis: {extraction.meta['off_topic']}" if "off_topic" in extraction.meta
                                 else "read, no criteria extracted",
                                 "source_doc": source_doc})
        return extraction, source_doc

    for doc in docs:
        base, source_doc = read(doc, "fetched paper")
        if base is None or not base.items:
            tried.append(f"{doc['key']} ({source_doc if base is None else 'not about the diagnosis' if 'off_topic' in base.meta else 'no specific criteria'})")
            continue
        notes = []
        for name, year, label in _newer_wanted(base):
            holders = [d for d in docs if d is not doc and d.get("path") and Path(d["path"]).exists()
                       and _carries(texts.setdefault(d["key"], _doc_text(d)), name, year)]
            # published in or after the newer version's year first, then by completeness
            holders.sort(key=lambda d: (dx._int(d.get("year"), 0) < year, -d.get("completeness", 0)))
            merged = False
            for holder in holders:
                newer, newer_doc = read(holder, f"newer version {label}")
                if newer is not None and newer.items:
                    before = base
                    base = _replace_set(base, newer, name, year, prefix=f"N{len(base.meta.get('merged', [])) + 1}-")
                    if base is not before:
                        source_doc = f"{source_doc} + {label} from {newer_doc}"
                        notes.append(f"{label} merged from {holder['key']}")
                        merged = True
                        break
            if not merged:
                notes.append(f"{label} mentioned, no fetched document states it" if not holders
                             else f"{label} mentioned, not extracted from {', '.join(h['key'] for h in holders)}")
        return base, source_doc, (f"{doc.get('url', '')}, {doc.get('doc_kind')}, {doc.get('tier')}, "
                                  f"priority {doc.get('priority')}, completeness "
                                  f"{doc.get('completeness')}, {len(texts[doc['key']])} chars via {doc.get('source')} — "
                                  f"document {docs.index(doc) + 1} of {len(docs)}; newer versions: {'; '.join(notes) or 'none mentioned'}; "
                                  f"passed over: {'; '.join(tried) or 'none'}")
    return None, None, f"no fetched paper yielded criteria ({len(docs)} fetched): {'; '.join(tried) or 'none'}"


def find_criteria(diagnosis_name: str, trail: dict | None = None) -> tuple[Extraction | None, str | None, str, dict]:
    """(extraction, source_doc, detail, extractions) from the papers ddxcriteria_sources fetches, or
    from the web; extraction is None when no source yields criteria.

    The passes run in an order set by why retrieve() asked for a web search, and stop at the first
    that yields criteria: no text or only weak texts → DuckDuckGo guideline, DuckDuckGo, then the
    fetched texts; a better paper left unfetched (paywalled) → that paper on the web, then the
    fetched texts and DuckDuckGo — DuckDuckGo first when the best fetched paper's priority is
    WEB_FIRST_GAP or more below the best unfetched one's; otherwise the fetched texts, then DuckDuckGo. Every source is read with
    criteria_extract.extract_best (two runs, the finer kept), at most once across the passes;
    extractions holds them all by source. trail, when given, gets every document and page opened to
    read criteria from and what each pass made of it, as criteria_web.find_criteria records them."""
    sites = trail if trail is not None else {}
    done: dict[str, Extraction] = {}

    def once(source_doc: str, text: str) -> Extraction:
        if source_doc not in done:
            done[source_doc] = criteria_extract.extract_best(diagnosis_name, source_doc, text)
        return done[source_doc]

    def items_of(source_doc: str, text: str) -> list:
        return once(source_doc, text).items

    try:
        result = _retrieve(diagnosis_name)
        need = _need_kind(result)
        summary = (f"ddxcriteria {dx.__version__}: {result['status']}, {result['n_documents']} documents, "
                   f"needs_web_search={result['needs_web_search']}"
                   + (f" ({result['web_search_reason']})" if result["needs_web_search"] else ""))
    except Exception as error:  # a failed search leaves the web search, not the run
        result, need, summary = None, "no_text", f"ddxcriteria failed: {error}"

    def found(run) -> Callable[[], tuple[Extraction | None, str | None, str]]:
        """A pass that returns items, turned into one that returns their extraction."""
        def call():
            items, source_doc, detail = run()
            return (done.get(source_doc) if items else None), source_doc, detail
        return call

    fetched = ("fetched paper", lambda: _find_fetched(result, once, sites))
    web = (("DuckDuckGo guideline", found(lambda: criteria_web.find_criteria(
               diagnosis_name, items_of, kinds={"guideline"}, trail=sites, label="DuckDuckGo guideline"))),
           ("DuckDuckGo", found(lambda: criteria_web.find_criteria(
               diagnosis_name, items_of, trail=sites, label="DuckDuckGo"))))
    if result is None or need == "no_text":
        passes = web
    elif need == "weak_only":
        passes = (*web, fetched)
    elif need == "better_unfetched":
        hinted = ("better paper on the web", found(lambda: _find_hinted(result, items_of, sites)))
        gap = (max(h["priority"] for h in result["web_search_hints"])
               - max(d["priority"] for d in result["documents"]))
        passes = (hinted, *web, fetched) if gap >= WEB_FIRST_GAP else (hinted, fetched, *web)
    else:
        passes = (fetched, *web)

    misses = []
    for label, run in passes:
        extraction, source_doc, detail = run()
        if extraction is not None and extraction.items:
            return extraction, source_doc, f"{label}: {detail} | {summary}" + (
                f" | before: {' | '.join(misses)}" if misses else ""), done
        misses.append(f"{label}: {detail}")
    return None, None, f"{summary} | " + " | ".join(misses), done
