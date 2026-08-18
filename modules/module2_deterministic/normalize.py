import numpy as np
from sentence_transformers import SentenceTransformer

from schema import DDxItem

# Input: a list of DDxItem produced across multiple MeSH-term searches (may repeat the same
# diagnosis under different names/wording). Output: a deduplicated list of DDxItem. Algorithm:
# same BioLORD-2023 embedding + cosine-similarity approach as normalization/usage_BioLORD.py —
# encode each diagnosis_name, greedily merge any pair with similarity > 0.85 into the
# first-seen item, concatenating their evidence lists and joining source_detail with " | ".

_SIMILARITY_THRESHOLD = 0.85
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("FremyCompany/BioLORD-2023")
    return _model


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def dedup_ddx_items(items: list[DDxItem]) -> list[DDxItem]:
    if len(items) <= 1:
        return list(items)

    model = _get_model()
    vectors = model.encode([item.diagnosis_name for item in items])

    merged: list[DDxItem] = []
    merged_vectors: list[np.ndarray] = []
    for item, vec in zip(items, vectors):
        match_idx = next(
            (i for i, mv in enumerate(merged_vectors) if _cosine_sim(vec, mv) > _SIMILARITY_THRESHOLD),
            None,
        )
        if match_idx is None:
            merged.append(item)
            merged_vectors.append(vec)
        else:
            target = merged[match_idx]
            target.evidence.extend(item.evidence)
            if item.source_detail not in target.source_detail.split(" | "):
                target.source_detail = f"{target.source_detail} | {item.source_detail}"

    return merged
