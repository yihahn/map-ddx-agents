import html
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Literal

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel

from .criteria import _archived_sections, _scrape
from . import criteria_literature
from .criteria_literature import MIN_SOURCE_CHARS, _focus, _key, _pmc_text
from .llm import get_llm

# Input: a diagnosis name, plus a function that extracts criteria from a source text. Output:
# find_criteria(name, extract) -> (criteria, source_doc, detail), the same contract as
# criteria_literature.find_criteria, from the best guideline or paper a DuckDuckGo search for
# "diagnosis for <name>" turns up. Algorithm: search DuckDuckGo's HTML endpoint, let the LLM rank
# the results that are guidelines, scholarly literature or professional clinical references
# (Merck Manual Professional, StatPearls) and mark each as about the
# condition as a whole or one subtype (code orders whole first), then fetch them in that order —
# PubMed Central articles through E-utilities, StatPearls from the LitArch archive, Merck through
# criteria._scrape, other pages as HTML — and use the first one the
# extraction finds criteria in. Search results, rankings and page texts are cached on disk, so a
# rerun asks DuckDuckGo nothing it asked before. DuckDuckGo has no search API and answered this
# host with its bot-check page (HTTP 202) after about a dozen queries on 2026-10-07, lifting about
# 40 minutes later, so queries are spaced well apart and a block is waited out rather than retried.

QUERY_TEMPLATE = "diagnosis for {}"
SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_INTERVAL = 20.0
# Waits after a bot-check answer before asking again; the observed block lasted about 40 minutes.
BLOCK_WAITS = (900.0, 1800.0, 2700.0)
MAX_CANDIDATES = 8
MAX_EXTRACTIONS = 3
REQUEST_TIMEOUT = 40
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Not a source of diagnostic criteria, and frequent in results: struck by code so the ranking
# cannot pick them even when nothing better is listed. Professional references — Merck Manual
# Professional and StatPearls — are allowed; the Merck/MSD consumer editions are not.
EXCLUDED_DOMAINS = (
    "wikipedia.org", "medscape.com", "mayoclinic.org", "clevelandclinic.org", "webmd.com",
    "healthline.com", "droracle.ai", "youtube.com", "reddit.com", "quizlet.com", "verywellhealth.com",
    "fpnotebook.com",
)
# Sources that never give this host a readable page, struck the same way so they do not take a
# candidate slot: BMJ Best Practice serves non-subscribers only a summary and a list of test names
# (it yielded a fever threshold the page does not state), UpToDate answers with a bot check, and
# the rest answered HTTP 403 every time they came up in the 2026-10-07/08 runs. PDFs are struck
# too, since nothing here reads them.
UNREADABLE_DOMAINS = (
    "bestpractice.bmj.com", "uptodate.com", "sciencedirect.com", "academic.oup.com",
    "journal.chestnet.org", "pubs.rsna.org", "ahajournals.org", "jacc.org", "litfl.com", "cdc.gov",
)
_CONSUMER_EDITION = re.compile(r"(merckmanuals|msdmanuals)\.com/(?:[a-z-]+/)?home/", re.I)
_STATPEARLS_SUFFIX = re.compile(r"\s*-\s*StatPearls.*$", re.I)
_BLOCKED_PAGE = re.compile(
    r"just a moment|cf-chl|captcha|access denied|are you a robot|enable javascript and cookies", re.I
)
_CACHE_DIR = Path(__file__).resolve().parents[2] / "embeddings" / "web_criteria"

_search_lock = threading.Lock()
_next_search_at = 0.0
_cache_lock = threading.Lock()


class RankedResult(BaseModel):
    number: int
    scope: Literal["whole", "subtype"]
    kind: Literal["guideline", "criteria_paper", "review", "reference"]


class ResultRanking(BaseModel):
    ranked: list[RankedResult]  # best first; only scholarly literature or guidelines


class SearchBlocked(RuntimeError):
    pass


def _ddg(query: str) -> list[dict]:
    """[{title, href, body}] from DuckDuckGo's HTML endpoint; SearchBlocked on its bot check."""
    resp = requests.get(SEARCH_URL, params={"q": query}, timeout=REQUEST_TIMEOUT,
                        headers={"User-Agent": BROWSER_UA})
    if resp.status_code == 202 or "anomaly" in resp.text[:20000].lower():
        raise SearchBlocked(f"DuckDuckGo bot check (HTTP {resp.status_code})")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for block in soup.select(".result"):
        link = block.select_one("a.result__a")
        if link is None or not link.get("href"):
            continue
        href = link["href"]
        target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        url = target[0] if target else href
        if "duckduckgo.com/y.js" in url:  # an advert
            continue
        snippet = block.select_one(".result__snippet")
        results.append({"title": link.get_text(" ", strip=True), "href": url,
                        "body": snippet.get_text(" ", strip=True) if snippet else ""})
    return results


def _search(query: str) -> list[dict]:
    """DuckDuckGo results for one query, cached on disk, spaced and blocks waited out."""
    global _next_search_at
    path = _CACHE_DIR / f"search_{_key(query)}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["results"]
    for wait in (0.0, *BLOCK_WAITS):
        time.sleep(wait)
        with _search_lock:
            time.sleep(max(0.0, _next_search_at - time.monotonic()))
            try:
                results = _ddg(query)
            except SearchBlocked as error:
                blocked = error
                continue
            finally:
                _next_search_at = time.monotonic() + SEARCH_INTERVAL
        with _cache_lock:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"query": query, "results": results}, ensure_ascii=False,
                                       indent=1), encoding="utf-8")
        return results
    raise RuntimeError(f"{blocked}; still blocked after waiting {sum(BLOCK_WAITS) / 60:.0f} min")


def _excluded(url: str) -> bool:
    return (
        any(domain in url for domain in EXCLUDED_DOMAINS + UNREADABLE_DOMAINS)
        or bool(_CONSUMER_EDITION.search(url))
        or url.lower().split("?")[0].endswith(".pdf")
    )


def _rank(diagnosis_name: str, results: list[dict]) -> list[tuple[int, str]]:
    listed = "\n".join(
        f"{i}. {r['title']}\n   {r['href']}\n   {r['body'][:240]}" for i, r in enumerate(results, 1)
    )
    reply: ResultRanking = get_llm().with_structured_output(ResultRanking).invoke(
        f"These are web search results for 'diagnosis for {diagnosis_name}'.\n\n{listed}\n\n"
        "Rank the results that would be the best source of diagnostic criteria for this condition, "
        "best first. These qualify: a society or government clinical practice guideline, a "
        "consensus statement, a paper that proposes or validates diagnostic criteria, a review "
        "article in a medical journal, and a professional clinical reference written for "
        "clinicians (Merck Manual Professional, StatPearls). Rank a guideline or consensus "
        "statement from a professional society or a government body first, the most recent first, "
        "then papers that propose or validate diagnostic criteria, then reviews, then professional "
        "references. Within each of these, put the more recent ahead of the older, and one whose "
        "title or snippet shows it states the diagnostic criteria or case definition ahead of one "
        "that only discusses diagnosis or management. "
        "Leave out general encyclopaedias, consumer or patient editions, Q&A and AI-answer sites, "
        "personal or point-of-care note sites, blogs, news, patient information pages, "
        "laboratory-methods papers, and results about a different condition. Return an empty "
        "list if none qualify.\n"
        "For each result give its number, its kind ('guideline' for a society or government "
        "guideline or consensus statement, 'criteria_paper', 'review', or 'reference' for a "
        "professional clinical reference or any other qualifying page) and its scope: "
        f"'whole' if it is about {diagnosis_name} "
        "as named, 'subtype' if it is about one subtype, organ manifestation or patient group of "
        "it — for 'Toxoplasmosis', a page on ocular toxoplasmosis is 'subtype'."
    )
    seen, ranked = set(), []
    for item in reply.ranked:
        index = item.number - 1
        if 0 <= index < len(results) and index not in seen and not _excluded(results[index]["href"]):
            seen.add(index)
            ranked.append((index, item.scope, item.kind))
    return [r for r in ranked if r[1] == "whole"] + [r for r in ranked if r[1] != "whole"]


def _ranking(diagnosis_name: str) -> dict:
    path = _CACHE_DIR / f"rank_{_key(diagnosis_name)}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    query = QUERY_TEMPLATE.format(diagnosis_name)
    results = _search(query)  # raises when DuckDuckGo stays blocked; nothing is cached then
    record = {"diagnosis_name": diagnosis_name, "query": query, "results": results,
              "ranked": [list(r) for r in _rank(diagnosis_name, results)] if results else []}
    with _cache_lock:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


def _html_text(page: str) -> str:
    """Readable text of a page: navigation and chrome dropped, headings marked with ##."""
    soup = BeautifulSoup(page, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"]):
        tag.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup
    lines = []
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"]):
        if el.name == "tr":
            text = " | ".join(" ".join(c.get_text(" ").split()) for c in el.find_all(["td", "th"]))
        elif el.find_parent(["li", "td"]) is not None and el.name in ("p", "li"):
            continue
        else:
            text = " ".join(el.get_text(" ").split())
        if text.strip(" |"):
            lines.append(("## " + text) if el.name.startswith("h") else text)
    return "\n".join(dict.fromkeys(lines))


def _page_text(url: str, title: str = "") -> tuple[str, str]:
    """(focused text or "", how it was read or why not), cached on disk per URL."""
    path = _CACHE_DIR / f"page_{_key(url)}.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        return cached["text"], cached["how"]
    text, how = "", ""
    pmc = re.search(r"PMC\d+", url)
    if "ncbi.nlm.nih.gov/books/" in url:
        # StatPearls on NCBI Bookshelf: Bookshelf forbids automated retrieval and answers scripts
        # with a reCAPTCHA, so the chapter is read from the LitArch archive by its title instead.
        chapter = _STATPEARLS_SUFFIX.sub("", html.unescape(title)).strip(" .")
        sections = _archived_sections(chapter) if chapter else None
        text = "\n".join(f"## {name}\n{body}" for name, body in sections or [])
        how = f"StatPearls LitArch archive '{chapter}'" if sections else f"not in StatPearls archive: '{chapter}'"
    elif re.search(r"(merckmanuals|msdmanuals)\.com", url):
        # Merck's own crawler etiquette (robots.txt Crawl-delay) lives in criteria._scrape.
        raw = _scrape(url)
        if raw is None:
            return "", "Merck request failed"  # transient: not cached
        text, how = raw, "Merck Manual"
    elif pmc and "ncbi.nlm.nih.gov" in url:
        raw = _pmc_text(pmc.group(0))
        text, how = (raw or ""), f"PMC E-utilities {pmc.group(0)}"
    elif url.lower().split("?")[0].endswith(".pdf"):
        how = "PDF (not read)"
    else:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": BROWSER_UA})
        except requests.RequestException as error:
            return "", f"request failed: {type(error).__name__}"  # transient: not cached
        if resp.status_code != 200:
            how = f"HTTP {resp.status_code}"
        elif "pdf" in resp.headers.get("content-type", "").lower():
            how = "PDF (not read)"
        elif _BLOCKED_PAGE.search(resp.text[:20000]) and len(resp.text) < 60000:
            how = "bot check page"
        else:
            text, how = _html_text(resp.text), "HTML"
    text = _focus(text) if len(text) >= MIN_SOURCE_CHARS else ""
    with _cache_lock:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "how": how, "text": text}, ensure_ascii=False),
                        encoding="utf-8")
    return text, how


def find_criteria(
    diagnosis_name: str, extract: Callable[[str, str], list], kinds: set[str] | None = None
) -> tuple[list, str | None, str]:
    """(criteria, source_doc, detail) from the first ranked result extract() finds criteria in.

    kinds limits the search to results the ranking gave one of those kinds ("guideline", ...)."""
    try:
        record = _ranking(diagnosis_name)
    except (RuntimeError, requests.RequestException) as error:
        return [], None, f"DuckDuckGo search failed: {error}"
    results, tried, extractions = record["results"], [], 0
    if not record["ranked"]:
        return [], None, f"no guideline or paper among {len(results)} results for '{record['query']}'"
    for index, scope, *kind in record["ranked"][:MAX_CANDIDATES]:
        hit = results[index]
        if _excluded(hit["href"]):  # a ranking cached before the domain was struck
            continue
        if kinds is not None and (kind[0] if kind else None) not in kinds:
            continue
        text, how = _page_text(hit["href"], hit["title"])
        if not text:
            tried.append(f"{hit['href']} ({how or 'too short'})")
            continue
        domain = urllib.parse.urlparse(hit["href"]).netloc.removeprefix("www.")
        source_doc = f"{html.unescape(hit['title'])} ({domain})"
        criteria = extract(source_doc, text)
        extractions += 1
        if criteria:
            detail = (f"{hit['href']}, {scope}, {kind[0] if kind else 'unranked kind'}, via {how} — result {index + 1} of {len(results)} "
                      f"for '{record['query']}'; passed over: {'; '.join(tried) or 'none'}")
            return criteria, source_doc, detail
        tried.append(f"{hit['href']} (no specific criteria)")
        if extractions >= MAX_EXTRACTIONS:
            break
    return [], None, f"no result yielded criteria for '{record['query']}': {'; '.join(tried)}"


def find_criteria_with_fallback(
    diagnosis_name: str, extract: Callable[[str, str], list]
) -> tuple[list, str | None, str]:
    """(criteria, source_doc, detail) from a society guideline when either search has one, else
    from DuckDuckGo's best result, else from PubMed.

    A guideline or consensus statement is tried first in both searches, because a web search for a
    rare condition often lists only reference pages and old reviews — for adult-onset Still's
    disease DuckDuckGo's best was a dermatology reference page with the 1992 Yamaguchi criteria,
    while PubMed had the 2024 EULAR/PReS recommendations. Past that, the two complement each
    other: the web search reaches clinical references (Merck, imaging criteria) that PubMed's
    filter misses, and PubMed reaches full-text criteria papers when every web result is a paywall,
    a PDF or a page without criteria. A source is extracted at most once across the passes."""
    done: dict[str, list] = {}

    def once(source_doc: str, text: str) -> list:
        if source_doc not in done:
            done[source_doc] = extract(source_doc, text)
        return done[source_doc]

    passes = (
        ("DuckDuckGo guideline", lambda: find_criteria(diagnosis_name, once, kinds={"guideline"})),
        ("PubMed guideline",
         lambda: criteria_literature.find_criteria(diagnosis_name, once, guidelines_only=True)),
        ("DuckDuckGo", lambda: find_criteria(diagnosis_name, once)),
        ("PubMed", lambda: criteria_literature.find_criteria(diagnosis_name, once)),
    )
    misses = []
    for label, run in passes:
        criteria, source_doc, detail = run()
        if criteria:
            return criteria, source_doc, f"{label}: {detail}" + (
                f" | before: {' | '.join(misses)}" if misses else "")
        misses.append(f"{label}: {detail}")
    return [], None, " | ".join(misses)
