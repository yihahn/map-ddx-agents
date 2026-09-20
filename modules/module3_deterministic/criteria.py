import html
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import requests
from bs4 import BeautifulSoup

from .embed import get_encoder

# Input: a diagnosis name. Output: (raw_text, source_doc, detail) — reference text its diagnostic
# criteria can be parsed out of, or (None, None, detail) when no source covers it. Algorithm: Merck
# Manual Professional first, resolved against its own sitemaps rather than a web search — the topic
# and table sitemaps (2,834 + 1,000 URLs) are downloaded once, their slugs embedded with BioLORD and
# cached under embeddings/, and a diagnosis matches the closest slug above MERCK_SIMILARITY_FLOOR.
# This replaces a DuckDuckGo site: query that returned different results run to run, saw only its
# top 8 hits, and raised rate-limit errors, none of which a fixed local index does. The matched
# article is then scraped (article pages are server-rendered, unlike Merck's search), spaced to the
# Crawl-delay: 5 its robots.txt asks for and cached per diagnosis. If Merck has no close article,
# fall back to the StatPearls chapter NCBI Bookshelf holds for the diagnosis, keeping only the
# sections that decide a diagnosis. MedlinePlus was the fallback before and could not serve this
# module: it is NLM's consumer resource, so its sepsis topic describes the workup ("will likely
# order lab tests") without a single threshold to check an EMR against, and it has no colchicine
# poisoning topic at all. StatPearls is written for clinicians and carries the thresholds.

MERCK_SIMILARITY_FLOOR = 0.85
MERCK_CRAWL_DELAY = 5.0
TABLE_PATH = "/multimedia/table/"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BOOKSHELF_URL = "https://www.ncbi.nlm.nih.gov/books/{accession}/"
NCBI_DELAY = 0.4  # eutils allows 3 requests/second without an API key
NCBI_ATTEMPTS = 3
NCBI_RETRY_BACKOFF = 1.0
# Chapter titles are real disease names rather than slugs, so they separate far more cleanly than
# Merck's URLs: on PT09 the right chapter scored 0.985-1.000 and every wrong one 0.054-0.203. The
# floor sits below that gap so a poisoning diagnosis can still reach the drug's own chapter, where
# the toxicity section lives ("Colchicine poisoning" vs the "Colchicine" chapter scores 0.558).
STATPEARLS_SIMILARITY_FLOOR = 0.50
STATPEARLS_SECTIONS = ("Evaluation", "History and Physical", "Differential Diagnosis", "Toxicity")
_TOXIC_SUFFIXES = ("poisoning", "toxicity", "intoxication", "overdose")
USER_AGENT = "map-ddx-agents/0.1"
REQUEST_TIMEOUT = 40

SITEMAPS = (
    "https://www.merckmanuals.com/sitemaps/professional-topic.xml.gz",
    "https://www.merckmanuals.com/sitemaps/professional-table.xml",
)
_CACHE_DIR = Path(__file__).resolve().parents[2] / "embeddings"
_URLS_CACHE = _CACHE_DIR / "merck_urls.txt"
_VECTORS_CACHE = _CACHE_DIR / "merck_slugs.npy"

_index: tuple[list[str], np.ndarray] | None = None
_index_lock = threading.Lock()
_crawl_lock = threading.Lock()
_next_crawl_at = 0.0
_ncbi_lock = threading.Lock()
_next_ncbi_at = 0.0
_page_cache: dict[str, tuple[str | None, str | None, str]] = {}
_page_cache_lock = threading.Lock()


def _throttle() -> None:
    """Space Merck requests by the Crawl-delay its robots.txt asks for, across all branches."""
    global _next_crawl_at
    with _crawl_lock:
        now = time.monotonic()
        wait = _next_crawl_at - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        _next_crawl_at = now + MERCK_CRAWL_DELAY


def slug_to_title(url: str) -> str:
    return urllib.parse.unquote(url.rstrip("/").split("/")[-1].split("?")[0]).replace("-", " ")


def _download_sitemap_urls() -> list[str]:
    urls: list[str] = []
    for sitemap in SITEMAPS:
        _throttle()
        response = requests.get(sitemap, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        urls.extend(re.findall(r"<loc>([^<]+)</loc>", response.text))
    return urls


def _get_index() -> tuple[list[str], np.ndarray]:
    """Return (urls, slug_vectors), building and caching them on first use."""
    global _index
    with _index_lock:
        if _index is not None:
            return _index

        if _URLS_CACHE.exists() and _VECTORS_CACHE.exists():
            urls = _URLS_CACHE.read_text(encoding="utf-8").split()
            vectors = np.load(_VECTORS_CACHE)
            if len(urls) == len(vectors):
                _index = (urls, vectors)
                return _index

        urls = _download_sitemap_urls()
        vectors = get_encoder().encode(
            [slug_to_title(u) for u in urls], batch_size=256, normalize_embeddings=True
        )
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _URLS_CACHE.write_text("\n".join(urls), encoding="utf-8")
        np.save(_VECTORS_CACHE, vectors)
        _index = (urls, vectors)
        return _index


def _match_merck_url(diagnosis_name: str) -> tuple[str | None, float]:
    """Return the Merck article whose slug is closest to the diagnosis name, topic pages first.

    A quarter of the index is table pages, and their titles can read closer to a diagnosis name
    than the topic article does — "Syndromes Caused by Enteroviruses" beat "Overview of Enterovirus
    Infections" by 0.002. Tables are worth indexing, because Merck files whole subjects only there
    (specific poisons live in one table and nowhere else), but a table lists associations rather
    than diagnostic criteria, so that win produced an article with nothing to check an EMR against.
    A topic page above the floor therefore wins outright, and a table is used only when no topic
    page clears it.
    """
    urls, vectors = _get_index()
    query = get_encoder().encode(diagnosis_name, normalize_embeddings=True)
    scores = np.asarray(vectors) @ np.asarray(query)

    topics = [i for i, url in enumerate(urls) if TABLE_PATH not in url]
    tables = [i for i, url in enumerate(urls) if TABLE_PATH in url]
    best_topic = max(topics, key=lambda i: scores[i]) if topics else None
    best_table = max(tables, key=lambda i: scores[i]) if tables else None

    for candidate in (best_topic, best_table):
        if candidate is not None and float(scores[candidate]) >= MERCK_SIMILARITY_FLOOR:
            return urls[candidate], float(scores[candidate])

    fallback = max((i for i in (best_topic, best_table) if i is not None),
                   key=lambda i: scores[i], default=None)
    return None, float(scores[fallback]) if fallback is not None else 0.0


def _scrape(url: str) -> str | None:
    """Fetch one Merck article, retried on transient failures. None means it could not be read."""
    for attempt in range(NCBI_ATTEMPTS):
        _throttle()
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt + 1 == NCBI_ATTEMPTS:
                return None
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _strip_markup(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", "", html.unescape(text or ""))).strip()


def _ncbi_get(path: str, params: dict) -> requests.Response | None:
    """One eutils/Bookshelf request, spaced across branches and retried on transient failures.

    Returning None means the lookup could not be made — a refused or timed-out request, not an
    answer. Callers have to keep the two apart: a dropped request once reported "StatPearls no
    chapter" for arsenic poisoning, whose chapter resolves at 0.985 on the very next try, so a
    network blip read as permanent absence and silently cost a diagnosis its criteria.
    """
    global _next_ncbi_at
    for attempt in range(NCBI_ATTEMPTS):
        with _ncbi_lock:
            wait = _next_ncbi_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _next_ncbi_at = time.monotonic() + NCBI_DELAY
        try:
            response = requests.get(
                path, params=params, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt + 1 < NCBI_ATTEMPTS:
                time.sleep(NCBI_RETRY_BACKOFF * (attempt + 1))
    return None


def _title_queries(diagnosis_name: str) -> list[str]:
    """Title variants to search Bookshelf with, so a poisoning reaches the chapter that covers it.

    [title] demands every word appear in the title, and StatPearls names its toxicology chapters
    after the agent rather than after "poisoning" — "Arsenic poisoning" finds nothing while the
    "Arsenic Toxicity" chapter sits right there, and colchicine's toxicity lives in the drug's own
    chapter. Widening the search is safe because the similarity check still decides what is used.
    """
    variants = [diagnosis_name]
    words = diagnosis_name.split()
    if len(words) > 1 and words[-1].lower() in _TOXIC_SUFFIXES:
        stem = " ".join(words[:-1])
        variants.append(stem)
        variants.extend(f"{stem} {suffix}" for suffix in _TOXIC_SUFFIXES)
    return list(dict.fromkeys(variants))


def _statpearls_chapters(diagnosis_name: str) -> list[tuple[str, str]] | None:
    """Return (accession, chapter_title) for the StatPearls chapters a diagnosis name turns up.

    Bookshelf indexes chapters and their sections side by side, and a plain keyword search returns
    sections of unrelated chapters (searching colchicine toxicity returns the Pancytopenia chapter's
    Etiology section). Restricting to [title] and keeping only records whose RID carries no section
    suffix leaves chapters alone, which is what the similarity check below needs to be meaningful.
    """
    titles = " OR ".join(f"{variant}[title]" for variant in _title_queries(diagnosis_name))
    query = f"({titles}) AND statpearls[book]"
    found = _ncbi_get(f"{EUTILS}/esearch.fcgi", {"db": "books", "term": query, "retmax": "20"})
    if found is None:
        return None
    try:
        ids = [node.text for node in ET.fromstring(found.text).iter("Id") if node.text]
    except ET.ParseError:
        return None
    if not ids:
        return []

    summaries = _ncbi_get(f"{EUTILS}/esummary.fcgi", {"db": "books", "id": ",".join(ids)})
    if summaries is None:
        return None
    try:
        root = ET.fromstring(summaries.text)
    except ET.ParseError:
        return None

    chapters = []
    for doc in root.iter("DocSum"):
        fields = {item.get("Name"): (item.text or "") for item in doc.iter("Item")}
        rid, title = fields.get("RID", ""), _strip_markup(fields.get("Title", ""))
        if rid and "/" not in rid and title:
            chapters.append((rid, title))
    return chapters


def _fetch_statpearls(diagnosis_name: str) -> tuple[str | None, str]:
    """Return the decision-making sections of the closest StatPearls chapter, and what matched.

    Only STATPEARLS_SECTIONS are kept. A chapter runs to ~90KB of epidemiology, treatment and
    review questions, and handing all of that to the criteria extractor buries the few paragraphs
    that actually say how the diagnosis is established.
    """
    chapters = _statpearls_chapters(diagnosis_name)
    if chapters is None:
        return None, f"lookup failed after {NCBI_ATTEMPTS} attempts"
    if not chapters:
        return None, "no chapter"

    encoder = get_encoder()
    query_vector = encoder.encode(diagnosis_name, normalize_embeddings=True)
    titles = encoder.encode([title for _, title in chapters], normalize_embeddings=True)
    scores = titles @ query_vector
    best = int(np.argmax(scores))
    accession, title = chapters[best]
    score = float(scores[best])
    if score < STATPEARLS_SIMILARITY_FLOOR:
        return None, f"closest chapter '{title}' only {score:.3f}"

    page = _ncbi_get(BOOKSHELF_URL.format(accession=accession), {})
    if page is None:
        return None, f"matched '{title}' but the chapter would not load after {NCBI_ATTEMPTS} tries"

    soup = BeautifulSoup(page.text, "html.parser")
    kept = []
    for heading in soup.find_all(["h2", "h3"]):
        if heading.get_text(strip=True) not in STATPEARLS_SECTIONS:
            continue
        body = []
        for element in heading.find_next_siblings():
            if element.name in ("h2", "h3"):
                break
            body.append(element.get_text(separator="\n", strip=True))
        text = "\n".join(part for part in body if part)
        if text:
            kept.append(f"{heading.get_text(strip=True)}\n{text}")
    if not kept:
        return None, f"matched '{title}' but it has none of {', '.join(STATPEARLS_SECTIONS)}"
    return "\n\n".join(kept), f"{title} ({accession}, title similarity {score:.3f})"


def fetch_reference_text(diagnosis_name: str) -> tuple[str | None, str | None, str]:
    """Return (raw_text, source_doc, detail) for a diagnosis, Merck first then StatPearls."""
    with _page_cache_lock:
        if diagnosis_name in _page_cache:
            return _page_cache[diagnosis_name]

    url, score = _match_merck_url(diagnosis_name)
    result: tuple[str | None, str | None, str]
    text = _scrape(url) if url else None
    if text:
        result = (text, "Merck Manual Professional", f"{url} (slug similarity {score:.3f})")
    else:
        merck = (
            f"no article (best slug {score:.3f})"
            if url is None
            else f"{url} matched at {score:.3f} but would not load"
        )
        sections, detail = _fetch_statpearls(diagnosis_name)
        if sections:
            result = (sections, "StatPearls (NCBI Bookshelf)", detail)
        else:
            result = (None, None, f"Merck {merck}; StatPearls {detail}")

    with _page_cache_lock:
        _page_cache[diagnosis_name] = result
    return result
