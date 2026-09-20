import csv
import hashlib
import io
import re
import threading
from pathlib import Path

import numpy as np

from .embed import get_encoder
from .llm import get_llm

# Input: one EMR document's text, and a query built from a diagnostic criterion. Output: the
# document's passages ready to embed (prepare_document), those passages as vectors (embed_chunks),
# and how well a query matches the best of them (score_chunks). Algorithm: prose is packed sentence
# by sentence into passages of at most CHUNK_MAX_CHARS with a one-sentence overlap, a table is first
# rewritten to one clinical statement per row and packed far tighter because each row is its own
# finding, and query and passage are then compared whole in BioLORD's space rather than as keywords
# extracted from each. A record containing Korean is translated to English clinical phrasing before
# it is embedded and the translation cached on disk, because BioLORD is an English encoder and
# scores the Korean original near zero — see the measurement above _HANGUL. Only the embedded text
# is normalized; the verifier is still shown the original, so quotes stay traceable.

# Passage size for prose. Swept against PT09's 85 hand-checked criterion/document pairs: 400 beat
# 300 and 500 (MRR 0.681 / 0.662 / 0.619), which is the 2-3 sentence window this was asked for.
CHUNK_MAX_CHARS = 400
# Below this a trailing fragment is folded back into the previous chunk instead of standing alone.
# A 20-character chunk ("Assessment") embeds to a heading rather than to a finding, and it ranked
# its whole document on the strength of matching any criterion that shared that heading's word.
CHUNK_MIN_CHARS = 80
# A table's rows are independent findings, so its passages are sized in rows rather than in
# sentences: at the prose size one passage held twenty analytes and diluted the one the criterion
# asked about, which cost 15 points of top-1 accuracy (52.9% at 90 chars, 37.6% at 400). Swept over
# 60/90/120/200/400 on the same pairs; 90 is about three lab rows.
TABLE_CHUNK_MAX_CHARS = 90

# BioLORD-2023 is English-only, and the EMR's clinically decisive prose is not: measured across the
# nine patients, 13-52% of .txt characters are Hangul, concentrated exactly where the history lives
# (admission-note HPI 31%, progress-note subjective 19-25%, medication instructions 43-74%).
# Encoding it directly is worse than useless. The same content, against the same criterion:
#
#   EN "…abdominal pain, nausea, vomiting, watery diarrhea…"          0.555
#   KO "복용 후 수 시간 이내에 심한 복통, 구역/구토, 수양성 설사 발생…"    0.026
#   (an unrelated control criterion scores 0.142 against that Korean text — so the correct passage
#    ranks *below* an unrelated one, and PT09's colchicine evidence is that very sentence)
#
# The keyword index this replaced sidestepped Korean by only ever extracting the English terms
# embedded in it. Whole-passage embedding cannot, so the Korean is translated before it is embedded.
_HANGUL = re.compile(r"[가-힣]")
NORMALIZE_PROMPT = (
    "Translate this Korean clinical record into English clinical phrasing, line by line.\n"
    "Keep every number, unit, date, lab name and English term exactly as written. Keep the line "
    "and section structure. Translate only the Korean; leave lines that are already English "
    "untouched. Do not summarize, do not add findings, do not omit any line.\n\n"
)
_CACHE_DIR = Path(__file__).resolve().parents[2] / "embeddings" / "emr_normalized"

_norm_lock = threading.Lock()


def _sentences(text: str) -> list[str]:
    """Split prose into sentence-sized pieces, treating a line break as a boundary.

    These records are line-structured — headers, vitals rows, one finding per line — so a newline
    separates ideas at least as reliably as a period does, and splitting on periods alone glued a
    section heading to the finding beneath it.
    """
    pieces = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.!?。])\s+", line):
            part = part.strip()
            if part:
                pieces.append(part)
    return pieces


def _pack(pieces: list[str], max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Pack pieces into chunks of at most CHUNK_MAX_CHARS, overlapping by one piece.

    The overlap exists because a finding and the value that settles it often sit on either side of
    a boundary ("Lactate 5.8↑↑ (악화)." then "pH 7.24"), and a chunk that splits them matches a
    criterion naming both worse than either half alone.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in pieces:
        if current and size + len(piece) + 1 > max_chars:
            chunks.append(" ".join(current))
            current = current[-1:] if len(current) > 1 else []
            size = sum(len(p) + 1 for p in current)
        current.append(piece)
        size += len(piece) + 1
    if current:
        tail = " ".join(current)
        # A short tail is the end of the previous passage, not a passage of its own.
        if chunks and len(tail) < min(CHUNK_MIN_CHARS, max_chars // 2):
            chunks[-1] = f"{chunks[-1]} {tail}"
        else:
            chunks.append(tail)
    return chunks


_CSV_NOISE = re.compile(r"^\(([▲▼])\)\s*|\[[^\]]*\]|\((?:Qn|Ql)\)")
_CSV_ROW_COLUMNS = (("lab_name", "lab_value", "lab_unit"), ("medication", "instruction"))


def render_table(text: str) -> str:
    """Rewrite a .csv record as one clinical statement per row.

    A lab row embedded as raw CSV is mostly display noise — `"(▲) WBC (Qn)[ChemR-I],Blood",14800,/uL`
    — and a passage of those scores poorly against a criterion written in prose. Measured on PT09's
    hand-checked pairs, ranking raw CSV passages put lab.csv outside the top 3 for 7 of the 8 worst
    criteria, all of them criteria a lab value settles. Rendered as "WBC 14800 /uL" the same row
    reads as the finding it is. This is the same recovery the keyword index made by reading the
    lab_name column instead of running a disease/chemical NER over the table, which typed 7 of 32
    analytes and dropped Platelet, ABG - pO2, Haptoglobin and Reticulocyte count — the very records
    the SOFA and haemolysis criteria ask for.
    """
    lines = []
    for row in csv.DictReader(io.StringIO(text)):
        for columns in _CSV_ROW_COLUMNS:
            if columns[0] not in row:
                continue
            parts = []
            for column in columns:
                value = (row.get(column) or "").split(",")[0] if column.endswith("name") else row.get(column)
                value = re.sub(r"\s+", " ", _CSV_NOISE.sub("", value or "")).strip()
                if value:
                    parts.append(value)
            if parts:
                lines.append(" ".join(parts))
            break
    return "\n".join(lines)


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Split one record's text into the passages it will be embedded and ranked as."""
    return _pack(_sentences(text), max_chars) if text.strip() else []


def prepare_document(text: str, is_table: bool = False) -> tuple[list[str], bool]:
    """Turn one record into (passages to embed, whether it had to be translated).

    Render, then translate, then pack — in that order. A table is rewritten to one statement per row
    while its columns are still intact, so the translator is handed prose lines rather than CSV it
    could reformat into something `csv.DictReader` no longer parses; medication.csv is 43-74% Korean
    and is the record where both steps apply.
    """
    source = (render_table(text) or text) if is_table else text
    embedded, translated = normalize_for_embedding(source)
    return chunk_text(embedded, TABLE_CHUNK_MAX_CHARS if is_table else CHUNK_MAX_CHARS), translated


def has_korean(text: str) -> bool:
    return _HANGUL.search(text) is not None


def normalize_for_embedding(text: str) -> tuple[str, bool]:
    """Return (text to embed, whether it was translated).

    English records are returned untouched. A record containing Hangul is translated once and the
    result cached on disk under embeddings/ (gitignored, like every other derived patient artefact),
    keyed by a hash of the original, so re-runs and parallel branches pay for it once.

    A failed translation returns the original text and says so rather than raising: retrieval on the
    untranslated record is degraded, but losing the record entirely is worse, and the caller records
    which documents fell back so a run cannot quietly report weak coverage as an absent finding.
    """
    if not has_korean(text):
        return text, False

    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    cached = _CACHE_DIR / f"{digest}.txt"
    if cached.exists():
        return cached.read_text(encoding="utf-8"), True

    with _norm_lock:
        if cached.exists():
            return cached.read_text(encoding="utf-8"), True
        try:
            translated = get_llm().invoke(NORMALIZE_PROMPT + text).content.strip()
        except Exception:
            return text, False
        if not translated:
            return text, False
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_text(translated, encoding="utf-8")
        return translated, True


def embed_chunks(chunks: list[str]) -> np.ndarray:
    """Embed a record's passages as rows. Empty input gives an empty (0, dim) array."""
    if not chunks:
        return np.zeros((0, get_encoder().get_sentence_embedding_dimension()), dtype=np.float32)
    return np.asarray(get_encoder().encode(chunks, normalize_embeddings=True))


def embed_query(text: str) -> np.ndarray:
    return np.asarray(get_encoder().encode(text, normalize_embeddings=True))


def score_chunks(query_vector: np.ndarray, chunk_vectors: np.ndarray) -> tuple[float, float, int]:
    """Score a document by its best-matching passage: (best, mean of top 3, index of the best).

    The maximum is the score, not the mean over the document: a criterion is settled by one passage,
    and averaging in the fifteen rows that do not mention it penalizes exactly the long records that
    carry the most. That length bias is what sank the previous whole-document cosine — PT09's
    correct 1,290-character progress note ranked 18th of 18, behind a 56-character lab row. Fixed
    passages remove it, because every candidate is now the same size.

    The top-3 mean breaks ties, and it earns its place: a record that names a criterion's concept in
    three passages is about that criterion, while one that hits the same peak in a single passage
    may have hit it by chance.
    """
    if chunk_vectors.shape[0] == 0:
        return 0.0, 0.0, -1
    similarities = chunk_vectors @ query_vector
    best = int(np.argmax(similarities))
    top = np.sort(similarities)[::-1][:3]
    return float(similarities[best]), float(top.mean()), best
