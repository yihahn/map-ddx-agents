from functools import lru_cache

import numpy as np
import spacy
from spacy.language import Language

from .embed import get_encoder

# Input: free text — an EMR document, or a diagnosis name joined to one diagnostic criterion.
# Output: the medical keywords it contains (extract_keywords), and a coverage score saying how much
# of one text's keywords another text accounts for (score_coverage). Algorithm: scispaCy's
# en_ner_bc5cdr_md tags diseases and chemicals/drugs without needing a vocabulary, then BioLORD
# embeds each keyword so two surface forms of one concept still match — "colchicine" in a progress
# note against "colchicine poisoning" in a criterion scores 0.558 while "colchicine" against
# "arsenic" scores 0.212, so a threshold separates them. Matching term-to-term rather than
# query-to-document is the point: both sides are short, so a 1,290-character note is no longer
# diluted below a 56-character lab row, which is what made the correct diagnosis's own evidence
# rank last out of 18 documents under whole-chunk cosine.

_NER_MODEL = "en_ner_bc5cdr_md"
# Measured on BioLORD: the same drug under two surface forms scores 0.526-0.558 ("colchicine" vs
# "colchicine overdose"/"colchicine poisoning") and clinically adjacent terms 0.485-0.533
# ("hypotension"/"shock", "lactate"/"lactic acidosis"), while unrelated pairs sit at 0.13-0.30
# ("colchicine"/"toxicity", "colchicine"/"arsenic"). The floor sits under the first band and over
# the last. It is not tight: "arsenic poisoning" reaches "gi toxicity" at 0.463, so a match near the
# floor is weak evidence and is reported with its score rather than silently treated as a hit.
MATCH_THRESHOLD = 0.45
# Two characters, not three: checkable_by names records by their ward abbreviations, and "UA" is
# the one that a three-character floor silently dropped — 19 of PT09's criteria named it, so the
# urine axis fell out of those queries entirely.
MIN_KEYWORD_CHARS = 2
MAX_NER_CHARS = 100_000  # one document; the largest PT09 record is under 2KB

_nlp: Language | None = None


def _get_nlp() -> Language:
    global _nlp
    if _nlp is None:
        _nlp = spacy.load(_NER_MODEL)
    return _nlp


def extract_keywords(text: str) -> list[str]:
    """Return the distinct medical keywords in a text, lowercased, in a stable order."""
    entities = _get_nlp()(text[:MAX_NER_CHARS]).ents
    seen = {}
    for entity in entities:
        term = entity.text.lower().strip()
        if len(term) >= MIN_KEYWORD_CHARS:
            seen[term] = None
    return list(seen)


@lru_cache(maxsize=8192)
def _vector(term: str) -> np.ndarray:
    """Embed one keyword. Cached because the same terms recur across criteria and documents."""
    return np.asarray(get_encoder().encode(term, normalize_embeddings=True))


def embed_keywords(terms: list[str]) -> np.ndarray:
    """Stack the vectors for a keyword list, as rows. Empty input gives an empty (0, dim) array."""
    if not terms:
        return np.zeros((0, _vector("x").shape[0]), dtype=np.float32)
    return np.stack([_vector(term) for term in terms])


def score_coverage(
    query_terms: list[str], doc_terms: list[str]
) -> tuple[float, list[tuple[str, str, float]], float]:
    """How much of query_terms this document accounts for, the pairs that account for it, and the
    same measure taken without the threshold.

    Each query term is matched to its closest document term; the match counts when it clears
    MATCH_THRESHOLD. The score is the fraction of query terms matched, deliberately normalized by
    the *query* rather than by either set's size — a document's own keyword count then has no effect
    on its score, which is what keeps a long note from being punished and a one-row lab file from
    being rewarded. The pairs are returned so a reader can see what a score was made of: a match at
    0.46 and one at 0.95 count the same here and should not read the same downstream.

    The third value, affinity, is the mean of those closest-term similarities with no threshold
    applied. Coverage cannot order documents that clear the threshold nowhere — they all score 0.0
    — and rank_documents no longer drops them, so affinity is what puts them in a sensible order
    instead of leaving them tied at zero and sorted by filename.
    """
    if not query_terms or not doc_terms:
        return 0.0, [], 0.0

    doc_vectors = embed_keywords(doc_terms)
    matches = []
    best_scores = []
    for term in query_terms:
        similarities = doc_vectors @ _vector(term)
        best = int(np.argmax(similarities))
        score = float(similarities[best])
        best_scores.append(score)
        if score >= MATCH_THRESHOLD:
            matches.append((term, doc_terms[best], round(score, 3)))
    return len(matches) / len(query_terms), matches, sum(best_scores) / len(best_scores)
