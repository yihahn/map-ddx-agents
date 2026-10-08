import csv
import io
import sys
import zipfile

# Input: the SNOMED CT International RF2 release zip in this directory (read in place, never unpacked).
# Output: snomed_concepts.csv (SCTID, FSN, preferred term, semantic tag, text definition),
# snomed_descriptions.csv (every US-English accepted term per concept, preferred/acceptable) and
# snomed_isa.csv (child -> parent is_a edges), and snomed_inactive_terms.csv (terms of inactive
# disorder/finding concepts with inactivation reason and historical association). Algorithm: keep active
# concepts whose FSN semantic tag is "disorder" or "finding"; keep active English descriptions that are
# active members of the US English language refset; keep active inferred is_a (116680003) edges between
# kept concepts. An inactive concept whose SAME AS / REPLACED BY association points to exactly one kept
# concept lends its terms to that target as HISTORICAL synonyms (Acceptability = FROM_<inactive SCTID>,
# skipped when the target already carries the same term); every other inactive term is only listed,
# so a DDx name that hits it can be flagged for review instead of silently matched or deleted.

csv.field_size_limit(sys.maxsize)

ZIP_FILE = "SnomedCT_InternationalRF2_PRODUCTION_20260901T120000Z.zip"
RELEASE = "SnomedCT_InternationalRF2_PRODUCTION_20260901T120000Z/Snapshot"
DATE = "20260901"
KEEP_TAGS = {"disorder", "finding"}

FSN = "900000000000003001"
SYNONYM = "900000000000013009"
US_ENGLISH_REFSET = "900000000000509007"
PREFERRED = "900000000000548007"
CONCEPT_INACTIVATION_REFSET = "900000000000489007"
SAME_AS = "900000000000527005"
REPLACED_BY = "900000000000526001"
IS_A = "116680003"
INFERRED = "900000000000011006"


def rows(zf: zipfile.ZipFile, path: str):
    with zf.open(f"{RELEASE}/{path}") as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE)
        yield from reader


print("SNOMED CT 추출을 시작합니다...")
with zipfile.ZipFile(ZIP_FILE) as zf:
    concept_active = {r["id"]: r["active"] == "1"
                      for r in rows(zf, f"Terminology/sct2_Concept_Snapshot_INT_{DATE}.txt")}
    active = {cid for cid, is_active in concept_active.items() if is_active}
    print(f"활성 개념: {len(active)}")

    # US English acceptability per description id
    acceptability = {
        r["referencedComponentId"]: "PREFERRED" if r["acceptabilityId"] == PREFERRED else "ACCEPTABLE"
        for r in rows(zf, f"Refset/Language/der2_cRefset_LanguageSnapshot-en_INT_{DATE}.txt")
        if r["active"] == "1" and r["refsetId"] == US_ENGLISH_REFSET
    }

    # fsn_any covers every concept (inactive ones and the metadata concepts naming reasons/associations);
    # inactive_terms keeps the synonyms of inactive concepts, whose language-refset membership may lapse.
    fsn, fsn_any, descriptions, inactive_terms = {}, {}, [], []
    for r in rows(zf, f"Terminology/sct2_Description_Snapshot-en_INT_{DATE}.txt"):
        if r["active"] != "1" or r["languageCode"] != "en" or r["typeId"] not in (FSN, SYNONYM):
            continue
        cid = r["conceptId"]
        if r["typeId"] == FSN:
            fsn_any[cid] = r["term"]
        if cid not in active:
            if r["typeId"] == SYNONYM:
                inactive_terms.append((cid, r["term"]))
            continue
        if r["id"] not in acceptability:
            continue
        kind = "FSN" if r["typeId"] == FSN else "SYNONYM"
        if kind == "FSN":
            fsn[cid] = r["term"]
        descriptions.append((cid, r["term"], kind, acceptability[r["id"]]))

    def tag(term: str) -> str:
        return term.rsplit("(", 1)[-1].rstrip(")") if term.endswith(")") and "(" in term else ""

    kept = {cid for cid, term in fsn.items() if tag(term) in KEEP_TAGS}
    print(f"disorder/finding 개념: {len(kept)}")

    preferred = {cid: term for cid, term, kind, acc in descriptions
                 if cid in kept and kind == "SYNONYM" and acc == "PREFERRED"}
    definitions = {r["conceptId"]: r["term"]
                   for r in rows(zf, f"Terminology/sct2_TextDefinition_Snapshot-en_INT_{DATE}.txt")
                   if r["active"] == "1" and r["conceptId"] in kept}

    reason = {r["referencedComponentId"]: r["valueId"]
              for r in rows(zf, f"Refset/Content/der2_cRefset_AttributeValueSnapshot_INT_{DATE}.txt")
              if r["active"] == "1" and r["refsetId"] == CONCEPT_INACTIVATION_REFSET}
    associations: dict[str, list[tuple[str, str]]] = {}
    for r in rows(zf, f"Refset/Content/der2_cRefset_AssociationSnapshot_INT_{DATE}.txt"):
        if r["active"] == "1":
            associations.setdefault(r["referencedComponentId"], []).append((r["refsetId"], r["targetComponentId"]))

    isa = sorted({(r["sourceId"], r["destinationId"])
                  for r in rows(zf, f"Terminology/sct2_Relationship_Snapshot_INT_{DATE}.txt")
                  if r["active"] == "1" and r["typeId"] == IS_A and r["characteristicTypeId"] == INFERRED
                  and r["sourceId"] in kept and r["destinationId"] in kept})

def strip_tag(term: str) -> str:
    return term.rsplit(" (", 1)[0] if tag(term) else term


existing = {(d[0], d[1].lower()) for d in descriptions}
inactive_rows, historical = [], []
for cid, term in sorted(set(inactive_terms), key=lambda x: (int(x[0]), x[1])):
    if tag(fsn_any.get(cid, "")) not in KEEP_TAGS:
        continue
    links = associations.get(cid, [])
    sure = {target for refset, target in links if refset in (SAME_AS, REPLACED_BY)}
    used = len(sure) == 1 and next(iter(sure)) in kept
    if used and (next(iter(sure)), term.lower()) not in existing:
        existing.add((next(iter(sure)), term.lower()))
        historical.append((next(iter(sure)), term, "HISTORICAL", "FROM_" + cid))
    inactive_rows.append([term, cid, fsn_any[cid], strip_tag(fsn_any.get(reason.get(cid, ""), "")),
                          "|".join(sorted({strip_tag(fsn_any.get(refset, refset)) for refset, _ in links})),
                          "|".join(sorted({target for _, target in links})), "Y" if used else "N"])
descriptions += historical

with open("snomed_inactive_terms.csv", "w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["Term", "Inactive_SCTID", "Inactive_FSN", "Reason", "Association", "Target_SCTIDs",
                     "Used_As_Synonym"])
    writer.writerows(inactive_rows)

with open("snomed_concepts.csv", "w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["SCTID", "FSN", "Preferred_Term", "Semantic_Tag", "Definition"])
    for cid in sorted(kept, key=int):
        writer.writerow([cid, fsn[cid], preferred.get(cid, ""), tag(fsn[cid]), definitions.get(cid, "")])

with open("snomed_descriptions.csv", "w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["SCTID", "Term", "Type", "Acceptability"])
    writer.writerows(d for d in descriptions if d[0] in kept)

with open("snomed_isa.csv", "w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["Child_SCTID", "Parent_SCTID"])
    writer.writerows(isa)

print(f"추출 완료! 개념 {len(kept)}개 → snomed_concepts.csv, "
      f"용어 {sum(1 for d in descriptions if d[0] in kept)}개(HISTORICAL {len(historical)}개 포함) → snomed_descriptions.csv, "
      f"is_a {len(isa)}개 → snomed_isa.csv, 비활성 용어 {len(inactive_rows)}개 → snomed_inactive_terms.csv")
