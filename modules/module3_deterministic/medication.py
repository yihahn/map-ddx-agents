import csv
import re
from functools import lru_cache

from .chunks import normalize_for_embedding, render_table
from .emr import EMR_ROOT

# Input: a patient id whose records live under pending_diag/data/<PID>/<YYYYMMDD>/<file>, plus a
# drug name taken from a diagnosis. Output: every medication that patient was prescribed or given
# (load_medications), and the places a drug is named anywhere in their record (find_exposure_mentions).
# Algorithm: medication.csv and baseline_medication.csv are read verbatim — the strings stay in
# Korean product(ingredient) form because the only thing that compares them to a diagnosis is an
# LLM, and translating first would drop the product name that is sometimes the sole identifier
# ("레날민정" carries no ingredient at all) while adding a step that can silently lose a row.
# find_exposure_mentions exists because the CSVs hold only what the hospital ordered: the substance
# a poisoning is named after is usually not among them. PT09's colchicine — the reference standard's
# answer — appears in no medication.csv and only in a progress note, and PT03's bee venom only in
# the admission note, so a filter that reads the CSVs alone deletes both. The search therefore runs
# over the record text as well, and over the English translation chunks.py already caches, which is
# what lets an English drug name reach a Korean note ("봉침" is cached as "bee venom therapy").

MEDICATION_FILES = ("baseline_medication.csv", "medication.csv")
EXCERPT_CHARS = 300


def load_medications(patient_id: str) -> list[dict]:
    """Every distinct medication this patient has a record of, in the order it first appears.

    Each entry keeps the raw CSV string and says whether it came from the baseline list (what the
    patient was already on) or from an inpatient order, because "was the patient exposed to this
    drug" is a different question from "did we give it to them". Only 4 of the 9 patients have a
    baseline file at all, which is one more reason the CSVs cannot be the only thing consulted.
    """
    patient_dir = EMR_ROOT / patient_id
    entries: dict[str, dict] = {}
    for path in sorted(patient_dir.glob("*/*")):
        if path.name not in MEDICATION_FILES:
            continue
        # utf-8-sig: 11 of these files carry a BOM, which otherwise lands in the first column name.
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                name = (row.get("medication") or "").strip()
                if not name:
                    continue
                entry = entries.setdefault(
                    name,
                    {
                        "medication": name,
                        "source": "baseline" if path.name.startswith("baseline") else "inpatient",
                        "first_date": (row.get("date") or path.parent.name).strip(),
                        "instruction": (row.get("instruction") or "").strip(),
                    },
                )
                if path.name.startswith("baseline"):
                    entry["source"] = "baseline"
    return list(entries.values())


def _pattern(drug: str) -> re.Pattern | None:
    """Match the drug name as a whole word, tolerating any spacing between its parts.

    A trailing suffix is allowed ("colchicine" matches "colchicine-induced") but a leading one is
    not, so "arsenic" does not match inside an unrelated longer token.
    """
    parts = [re.escape(part) for part in drug.lower().split() if part]
    if not parts:
        return None
    return re.compile(r"(?<![0-9a-z])" + r"\s+".join(parts), re.IGNORECASE)


@lru_cache(maxsize=None)
def _searchable(patient_id: str) -> tuple[tuple[str, str, str], ...]:
    """(doc_id, original text, English text) for every record, the third empty when not translated.

    The English text is produced the same way chunks.py produces what it embeds — same source, same
    hash — so this reuses that disk cache rather than paying for the translations again.
    """
    patient_dir = EMR_ROOT / patient_id
    documents = []
    for path in sorted(patient_dir.glob("*/*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            continue
        source = (render_table(text) or text) if path.suffix == ".csv" else text
        english, translated = normalize_for_embedding(source)
        documents.append((str(path.relative_to(patient_dir)), text, english if translated else ""))
    return tuple(documents)


def find_exposure_mentions(patient_id: str, drug: str) -> list[dict]:
    """Every place this patient's record names the drug, as {doc_id, excerpt, from_translation}.

    An empty list is the only thing that lets a diagnosis be filtered out, so this is deliberately
    permissive: any mention counts, including one in a differential the team was still considering.
    A drug named nowhere in the record is a drug the patient has no documented exposure to; a drug
    named in a progress note is one the chart raised, which is not something a filter may discard.
    from_translation marks an excerpt that comes from the English rendering rather than the record
    itself, so a quote read out of this is never mistaken for the original wording.
    """
    pattern = _pattern(drug)
    if pattern is None:
        return []

    mentions = []
    for doc_id, original, english in _searchable(patient_id):
        for text, translated in ((original, False), (english, True)):
            if not text:
                continue
            match = pattern.search(text)
            if not match:
                continue
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            line = text[start : end if end != -1 else len(text)].strip()
            mentions.append(
                {
                    "doc_id": doc_id,
                    "excerpt": line[:EXCERPT_CHARS],
                    "from_translation": translated,
                }
            )
            break
    return mentions
