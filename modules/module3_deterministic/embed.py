import numpy as np
from sentence_transformers import SentenceTransformer

# Input: none. Output: the one BioLORD-2023 encoder this module shares. Algorithm: lazily construct
# the model on first use and hand back the same instance thereafter. Both the Merck slug matcher and
# the EMR index embed with it, and Send fans branches out as threads in one process, so without a
# single shared instance the model is loaded several times over — measured at 11.7 GB resident.
# group_by_similarity lives here too, since merging Module 1 and 2's lists needs the same encoder.

_MODEL_NAME = "FremyCompany/BioLORD-2023"
SIMILARITY_THRESHOLD = 0.85
_model: SentenceTransformer | None = None


def get_encoder() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def group_by_similarity(names: list[str]) -> list[list[int]]:
    """Group indices of names that denote the same diagnosis, by BioLORD cosine similarity.

    The same greedy best-match pass Module 1 uses to union its specialists: a name joins the
    highest-scoring open group above SIMILARITY_THRESHOLD rather than the first one that clears it.
    Each group keeps its first member's vector as the representative, so grouping still depends on
    input order when three or more names are mutually similar.
    """
    if not names:
        return []

    vectors = get_encoder().encode(names, normalize_embeddings=True)

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
