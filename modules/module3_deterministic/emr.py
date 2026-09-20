import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

from .chunks import embed_chunks, embed_query, prepare_document, score_chunks

# Input: a patient id whose records live under pending_diag/data/<PID>/<YYYYMMDD>/<file>, plus a
# query built from one diagnostic criterion. Output: that patient's documents ranked by how closely
# their closest passage matches that criterion (rank_documents), one document's full text
# (load_document), and the document a quote can be traced to (attribute_doc). Algorithm: each record
# is split once per patient into 300-400 character passages and embedded with BioLORD (see
# chunks.py), and a criterion is embedded whole and compared against every passage, the best one
# scoring its document. Comparing passage against passage replaces extracting medical keywords from
# both sides and matching them term by term: that query was too sparse to rank with — measured over
# PT09's 321 criteria, the median criterion yielded 2 keywords and 32% yielded exactly one, which
# saturated 54.5% of them at coverage 1.0 and left 68.2% tied at the top, so the document actually
# read was chosen by a tiebreaker. Fixed-size passages are also what makes whole-text embedding
# usable at all here; the earlier whole-*document* cosine ranked PT09's correct 1,290-character note
# 18th of 18 behind a 56-character lab row, which is a length artefact and not a similarity one.
# Every document is ranked and none is dropped for scoring low: the score orders the list, it does
# not decide membership, because the verification loop reads the top documents anyway and a
# criterion that reaches no document is stamped unconfirmed without a model ever seeing it. That
# loop in graph.py walks this ranking itself, so there is no search tool, no lookup budget, and no
# agent deciding when to look.

EMR_ROOT = Path(__file__).resolve().parents[2] / "pending_diag" / "data"
TOP_K = 5
MIN_MATCH_CHARS = 25
# How much of the best-matching passage is kept in the retrieval log. Enough to see what the score
# was made of; the passage is not evidence and is never quoted, so the whole text is not needed.
LOG_EXCERPT_CHARS = 200

_index_lock = threading.Lock()


@lru_cache(maxsize=None)
def _chunk_index(patient_id: str) -> tuple[tuple[str, str, tuple[str, ...], np.ndarray], ...]:
    """Build the (doc_id, original text, passages, passage vectors) index for one patient, once.

    Held under a lock as well as an lru_cache. The cache alone was enough when building the index
    only ran a local NER, but a record containing Korean is now translated by an LLM call before it
    is embedded, and Send fans out one branch per diagnosis as threads in one process — 48 branches
    racing an empty cache would each pay for the whole patient's translations.

    The passages are what gets embedded; the original text is what gets returned to the verifier, so
    a translation can never reach a quote.
    """
    with _index_lock:
        patient_dir = EMR_ROOT / patient_id
        documents = []
        for path in sorted(patient_dir.glob("*/*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                continue
            doc_id = str(path.relative_to(patient_dir))
            chunks, _ = prepare_document(text, is_table=path.suffix == ".csv")
            documents.append((doc_id, text, tuple(chunks), embed_chunks(chunks)))
        return tuple(documents)


def rank_documents(
    patient_id: str, query: str, top_n: int = TOP_K
) -> list[tuple[str, float, list[tuple[str, float]]]]:
    """Rank this patient's documents by their closest passage to the query, best first.

    Returns (doc_id, score, [(passage excerpt, score), ...]) with the excerpts that produced the
    score, so a reader can see what a ranking was made of the way the keyword pairs used to show it.
    The best passage decides the order and the mean of the document's top three breaks ties.

    Every document is returned; none is dropped for scoring low. Dropping the ones that scored under
    a floor was tried under the previous ranking and reverted: it left 4 of PT09's 37 criteria with
    no documents at all, and a criterion with no documents never enters the verification loop — it
    is stamped unconfirmed by `settled.get`'s default, with no model call and no record consulted,
    which is indistinguishable in the output from a criterion the record genuinely could not settle.
    """
    if not query.strip():
        return []
    query_vector = embed_query(query)

    ranked = []
    for doc_id, _, chunks, vectors in _chunk_index(patient_id):
        best, top_mean, index = score_chunks(query_vector, vectors)
        excerpt = chunks[index][:LOG_EXCERPT_CHARS] if index >= 0 else ""
        ranked.append((doc_id, best, [(excerpt, round(best, 3))], top_mean))
    ranked.sort(key=lambda row: (row[1], row[3]), reverse=True)
    return [(doc_id, round(score, 3), matches) for doc_id, score, matches, _ in ranked[:top_n]]


def load_document(patient_id: str, doc_id: str) -> str:
    """Read one of this patient's records in full. An empty string means it could not be read.

    doc_id always comes from rank_documents, but the path is still confined to the patient's own
    directory: a doc_id is a relative path, and nothing outside that tree may be opened.
    """
    path = (EMR_ROOT / patient_id / doc_id).resolve()
    root = (EMR_ROOT / patient_id).resolve()
    if not path.is_file() or root not in path.parents:
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def attribute_doc(candidates: list[tuple[str, str]], quoted_text: str) -> str | None:
    """Resolve a quoted passage to one of the documents it was supposed to come from.

    The verifier never fills in the source itself (measured on PT09: date, source_doc and an explicit
    doc_id field were all filled 0 times out of 21), so the source is recovered here. The caller
    passes the document the judgement was actually made against, which makes this a check as much as
    a lookup: a quote that does not appear in the record the model was shown is a quote the model
    made up, and it is dropped rather than credited.
    """
    return _best_overlap(candidates, quoted_text)


def _best_overlap(candidates: list[tuple[str, str]], quoted_text: str) -> str | None:
    """Pick the candidate document whose text the quote accounts for most of.

    Each candidate is scored by how many characters the quote and the document text genuinely
    share, and the best match wins — taking the first document that merely overlapped attributed a
    medication row to a lab file, because a short unrelated row can sit inside a long quote.
    Returns None when nothing overlaps by at least MIN_MATCH_CHARS, so a quote with no real source
    is left unattributed rather than guessed.
    """
    needle = " ".join(quoted_text.split())
    if len(needle) < MIN_MATCH_CHARS:
        return None

    best_doc, best_score = None, 0
    for doc_id, text in candidates:
        haystack = " ".join(text.split())
        if haystack and haystack in needle:
            score = len(haystack)
        elif needle in haystack:
            score = len(needle)
        else:
            head = needle[:MIN_MATCH_CHARS]
            score = MIN_MATCH_CHARS if head in haystack else 0
        if score > best_score:
            best_doc, best_score = doc_id, score

    return best_doc if best_score >= MIN_MATCH_CHARS else None
