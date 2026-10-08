import csv
from dataclasses import dataclass
from pathlib import Path

# Input: data_prep/snomed_concepts.csv, snomed_descriptions.csv and snomed_isa.csv (built by
# data_prep/extract_snomed_disorders.py; licensed content, not in git). Output: load_terms(), an in-memory
# exact-term lookup (lower-cased preferred / acceptable / FSN-without-tag term -> concepts carrying it), the
# US English preferred term per concept, and is_a parents with an ancestors() walk. Algorithm: read the three
# tables once; HISTORICAL terms of inactivated concepts are skipped, since collapse attaches a code only on an
# exact, unambiguous match. Collapse uses it to label names, find declined ancestors and tag placements.

_DATA = Path(__file__).resolve().parents[2] / "data_prep"


def _strip_tag(term: str) -> str:
    return term.rsplit(" (", 1)[0] if term.endswith(")") and " (" in term else term


@dataclass
class SnomedTerms:
    term2ids: dict[str, set[str]]    # lower-cased preferred/acceptable/FSN term -> concepts carrying it
    preferred: dict[str, str]
    parents: dict[str, set[str]]

    def ancestors(self, sctid: str) -> dict[str, int]:
        """Ancestor SCTIDs mapped to their minimum is_a hop distance."""
        seen: dict[str, int] = {}
        frontier, depth = {sctid}, 0
        while frontier:
            depth += 1
            frontier = {p for node in frontier for p in self.parents.get(node, ())} - seen.keys()
            for node in frontier:
                seen[node] = depth
        return seen


def load_terms() -> SnomedTerms:
    preferred = {}
    with (_DATA / "snomed_concepts.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            preferred[r["SCTID"]] = r["Preferred_Term"]
    term2ids: dict[str, set[str]] = {}
    with (_DATA / "snomed_descriptions.csv").open(encoding="utf-8") as f:
        for d in csv.DictReader(f):
            if d["Type"] != "HISTORICAL":
                text = _strip_tag(d["Term"]) if d["Type"] == "FSN" else d["Term"]
                term2ids.setdefault(text.lower(), set()).add(d["SCTID"])
    parents: dict[str, set[str]] = {}
    with (_DATA / "snomed_isa.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            parents.setdefault(r["Child_SCTID"], set()).add(r["Parent_SCTID"])
    return SnomedTerms(term2ids, preferred, parents)
