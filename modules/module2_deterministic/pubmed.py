import os
import threading
import time

import requests

# Input: a PubMed search-term query string (and, for esummary, a list of PMIDs). Output: a
# result count, a list of PMIDs, or a list of {pmid, title, year, journal} dicts (no abstract).
# Algorithm: thin wrappers around NCBI E-utilities (esearch for count/PMIDs, esummary for
# title metadata), rate-limited to NCBI's published per-second caps (10/s with NCBI_API_KEY,
# 3/s without). LangGraph fans this module out across parallel threads (one per MeSH term), so
# the limiter is a single lock-protected "earliest next call time" shared by all threads rather
# than a per-call sleep, which would let concurrent threads collectively blow past the cap.

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_API_KEY = os.environ.get("NCBI_API_KEY")
_MIN_INTERVAL = 0.11 if _API_KEY else 0.34

_rate_lock = threading.Lock()
_next_call_at = 0.0


def _throttle() -> None:
    global _next_call_at
    with _rate_lock:
        now = time.monotonic()
        wait = _next_call_at - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        _next_call_at = now + _MIN_INTERVAL


def _params(**extra) -> dict:
    params = {"db": "pubmed", "retmode": "json", **extra}
    if _API_KEY:
        params["api_key"] = _API_KEY
    return params


def _get(endpoint: str, **params) -> dict:
    for attempt in range(4):
        _throttle()
        resp = requests.get(f"{_EUTILS_BASE}/{endpoint}", params=_params(**params), timeout=30)
        if resp.status_code == 429 and attempt < 3:
            time.sleep(2**attempt)
            continue
        resp.raise_for_status()
        return resp.json()


def esearch_count(query: str) -> int:
    data = _get("esearch.fcgi", term=query, retmax=0)
    return int(data["esearchresult"]["count"])


def esearch_pmids(query: str, retmax: int = 100, sort: str = "relevance") -> list[str]:
    data = _get("esearch.fcgi", term=query, retmax=retmax, sort=sort)
    return data["esearchresult"]["idlist"]


def esummary(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    data = _get("esummary.fcgi", id=",".join(pmids))
    result = data["result"]
    records = []
    for pmid in result.get("uids", []):
        item = result[pmid]
        records.append({
            "pmid": pmid,
            "title": item.get("title", ""),
            "year": (item.get("pubdate") or "").split(" ")[0],
            "journal": item.get("fulljournalname", ""),
        })
    return records
