import csv
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

# Input: a list of free-text diagnosis names produced by the specialist personas. Output: two
# things — index groups of names that denote the same diagnosis (group_by_similarity, used to build
# the union), and one MondoMatch per name (match_to_mondo, recorded for auditing only). Algorithm:
# BioLORD-2023 encodes every name; grouping walks the names in order and merges each into the
# already-open group whose representative is the *most* similar above SIMILARITY_THRESHOLD (the
# highest-scoring candidate, not the first one found), otherwise opens a new group. MONDO matching
# loads the 32k (id, label) pairs from data_prep/mondo_diseases.csv, caches their L2-normalized
# embeddings under embeddings/ (rebuilt whenever the cached row count stops matching the CSV), and
# takes the argmax of one dot-product — with no threshold, so the recorded scores can drive the
# threshold experiment in the spec's open issue 1.

SIMILARITY_THRESHOLD = 0.85

_MODEL_NAME = "FremyCompany/BioLORD-2023"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MONDO_CSV = _REPO_ROOT / "data_prep" / "mondo_diseases.csv"
_CACHE_PATH = _REPO_ROOT / "embeddings" / "mondo_biolord.npy"

_model: SentenceTransformer | None = None
_registry: tuple[list[str], list[str], np.ndarray] | None = None


class MondoMatch:
    def __init__(self, query: str, mondo_id: str, mondo_name: str, score: float):
        self.query = query
        self.mondo_id = mondo_id
        self.mondo_name = mondo_name
        self.score = score

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "mondo_id": self.mondo_id,
            "mondo_name": self.mondo_name,
            "score": round(self.score, 4),
        }


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _load_mondo() -> tuple[list[str], list[str]]:
    with _MONDO_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r["Mondo_ID"] for r in rows], [r["Disease_Name"] for r in rows]


def _get_registry() -> tuple[list[str], list[str], np.ndarray]:
    """Return (mondo_ids, mondo_names, normalized_vectors), building the embedding cache on first use."""
    global _registry
    if _registry is not None:
        return _registry

    ids, names = _load_mondo()

    vectors = None
    if _CACHE_PATH.exists():
        cached = np.load(_CACHE_PATH)
        if len(cached) == len(names):
            vectors = cached

    if vectors is None:
        vectors = _get_model().encode(
            names, batch_size=256, normalize_embeddings=True, show_progress_bar=True
        )
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        np.save(_CACHE_PATH, vectors)

    _registry = (ids, names, vectors)
    return _registry


def match_to_mondo(queries: list[str]) -> list[MondoMatch]:
    """Match each query name to its nearest MONDO entry by BioLORD cosine similarity (no threshold)."""
    if not queries:
        return []

    ids, names, vectors = _get_registry()
    query_vectors = _get_model().encode(queries, normalize_embeddings=True)
    sims = np.asarray(query_vectors) @ np.asarray(vectors).T

    matches = []
    for query, row in zip(queries, sims):
        best = int(np.argmax(row))
        matches.append(MondoMatch(query, ids[best], names[best], float(row[best])))
    return matches


def group_by_similarity(names: list[str]) -> list[list[int]]:
    """
    Group indices of names that denote the same diagnosis, by BioLORD cosine similarity.

    Same greedy pass as Module 2's dedup, with one difference: a name joins the *best* matching
    open group above SIMILARITY_THRESHOLD rather than the first one found. Each group keeps its
    first member's vector as the representative (no centroid update), so the grouping still
    depends on input order when three or more names are mutually similar.
    """
    if not names:
        return []

    vectors = _get_model().encode(names, normalize_embeddings=True)

    groups: list[list[int]] = []
    representatives: list[np.ndarray] = []
    for index, vector in enumerate(vectors):
        best_group, best_score = None, SIMILARITY_THRESHOLD
        for group_index, representative in enumerate(representatives):
            score = float(np.dot(vector, representative))
            if score > best_score:
                best_group, best_score = group_index, score
        if best_group is None:
            groups.append([index])
            representatives.append(vector)
        else:
            groups[best_group].append(index)

    return groups
