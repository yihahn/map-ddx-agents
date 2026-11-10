import json
from pathlib import Path

# Input: one pipeline report HTML (reports/report_v7_1031/<PID>_pipeline_report_v7_1031.html) whose
# window.__DATA__ holds the vignette, the EMR documents, Module 3's final and declined DDx with per-criterion
# EMR evidence, criterion categories and criteria-set routes; optionally the collapse forest built from those
# same items. Output: the Narrative input — a dict (saved as JSON) and its prompt text — in which every record
# document, evidence line and candidate carries an id (D#, E#, C#, X#) that the reason pass must cite.
# Algorithm: parse the JSON out of the HTML; fold each collapse group into one candidate (root name first,
# grouped names kept); merge the group's EMR evidence without duplicates and tag each line with its criterion
# category; list unverified criteria of decisive categories by name and nonspecific ones as a count; keep the
# declined list with the evidence that refuted it. The truth box (patient reference standard) never enters the input.

DECISIVE = ("precondition", "necessary", "sufficient", "specific", "exclusion")


def load_report(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    start = text.index("window.__DATA__ = ") + len("window.__DATA__ = ")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def to_ddx_items(items: list[dict]) -> list[dict]:
    """Report DDx entries as schema.DDxItem dicts — the shape collapse() reads from a run directory."""
    out = []
    for it in items:
        evidence = [{"criterion": None, "content": p["content"], "supports": bool(p["supports"]),
                     "date": None, "source_doc": None}
                    for p in it.get("prior_evidence") or [] if p.get("content")]
        evidence += [{"criterion": e.get("criterion"), "content": e["content"], "supports": bool(e["supports"]),
                      "date": e.get("date"), "source_doc": e.get("doc")}
                     for e in it.get("emr_evidence") or [] if e.get("content")]
        out.append({"diagnosis_name": it["name"], "source_detail": it.get("source") or "",
                    "status": it["status"], "evidence": evidence})
    return out


def _flatten(node: dict) -> list[dict]:
    out = [node]
    for child in node.get("children", []):
        out += _flatten(child)
    return out


def _category(item: dict, criterion: str | None) -> str:
    try:
        return item["categories"][item["criteria"].index(criterion)] or "unspecified"
    except (ValueError, IndexError, KeyError):
        return "unspecified"


def _source(source: str) -> str:
    """'Module1: <depts> | Module2: <PubMed query>' with the query text dropped — it carries no clinical content."""
    parts = [p if p.startswith("Module1") else "Module2: PubMed case reports" for p in source.split(" | ")]
    return " | ".join(dict.fromkeys(p for p in parts if p))


def _candidate(names: list[dict], by_name: dict[str, dict], next_id, tag: str) -> dict:
    """One input candidate from the report items of a group. names: [{"name", "relation"}], root first."""
    root = by_name[names[0]["name"]]
    confirmed, refuted, seen = [], [], set()
    for n in names:
        item = by_name[n["name"]]
        for e in item.get("emr_evidence") or []:
            key = (e.get("criterion"), e.get("content"))
            if not e.get("content") or key in seen:
                continue
            seen.add(key)
            line = {"id": next_id(tag), "criterion": e.get("criterion"), "category": _category(item, e.get("criterion")),
                    "content": e["content"], "date": e.get("date"), "doc": e.get("doc"), "name": n["name"]}
            (confirmed if e.get("supports") else refuted).append(line)
    verified = {e.get("criterion") for e in root.get("emr_evidence") or []}
    unverified = [(c, cat or "unspecified") for c, cat in zip(root.get("criteria") or [], root.get("categories") or [])
                  if c not in verified]
    return {
        "name": root["name"], "status": root["status"], "source": _source(root.get("source") or ""),
        "grouped": [{"name": n["name"], "relation": n["relation"], "status": by_name[n["name"]]["status"]} for n in names[1:]],
        "proposed_from": [p["content"] for p in root.get("prior_evidence") or [] if p.get("supports") and p.get("content")][:2],
        "criteria_source": root.get("ref") or None,
        "confirmed": confirmed, "refuted": refuted,
        "unverified_decisive": [{"criterion": c, "category": cat} for c, cat in unverified if cat in DECISIVE],
        "unverified_nonspecific": sum(1 for _, cat in unverified if cat not in DECISIVE),
        "routes": [{"label": r.get("label"), "set": r.get("set"), "met": bool(r.get("met")),
                    "reachable": bool(r.get("reachable")), "share": r.get("share")} for r in root.get("routes") or []],
        "declined_via": root.get("declined_via"),
    }


def build_input(report: dict, groups: list[dict] | None, declined_extra: list[dict] | None = None) -> dict:
    """The Narrative input. groups: collapse roots (07_grouped_ddx_list.json) or None to keep every item apart;
    declined_extra: collapse's declined list (adds the inherited declines)."""
    counters = {"E": 0}

    def next_id(tag: str) -> str:
        counters[tag] = counters.get(tag, 0) + 1
        return f"{tag}{counters[tag]}"

    final, declined = report["final"], list(report["declined"])
    by_name = {it["name"]: it for it in final + declined}
    if groups is None:
        group_names = [[{"name": it["name"], "relation": None}] for it in final]
    else:
        group_names = [[{"name": n["diagnosis_name"], "relation": n.get("relation")} for n in _flatten(root)] for root in groups]
        rank = {it["name"]: i for i, it in enumerate(final)}
        group_names.sort(key=lambda g: rank.get(g[0]["name"], len(rank)))
        for d in declined_extra or []:
            if d.get("declined_via") and d["diagnosis_name"] in by_name:
                by_name[d["diagnosis_name"]] = {**by_name[d["diagnosis_name"]], "declined_via": d["declined_via"]}
                declined.append(by_name[d["diagnosis_name"]])
    candidates = [{"id": f"C{i}", **_candidate(g, by_name, next_id, "E")} for i, g in enumerate(group_names, 1)]
    grouped_away = {g["name"] for c in candidates for g in c["grouped"]}
    declined_in = [{"id": f"X{i}", **_candidate([{"name": d["name"], "relation": None}], by_name, next_id, "E")}
                   for i, d in enumerate(declined, 1) if d["name"] not in grouped_away]
    docs = [{"id": f"D{i}", "date": d.get("date"), "name": d.get("name"), "text": d.get("text", "")}
            for i, d in enumerate(report["emr"], 1)]
    return {"pid": report["pid"], "vignette": report["vignette"], "documents": docs,
            "candidates": candidates, "declined": declined_in}


def _evidence_line(e: dict) -> str:
    where = ", ".join(str(e[k]) for k in ("date", "doc") if e.get(k))
    content = " / ".join(part.strip() for part in e["content"].splitlines() if part.strip())
    return f'  [{e["id"]}] ({e["category"]}) {e["criterion"]} ← "{content}"' + (f" ({where})" if where else "")


def _candidate_text(c: dict) -> str:
    lines = [f'### [{c["id"]}] {c["name"]} — status: {c["status"]}' + (f' — proposed by: {c["source"]}' if c["source"] else "")]
    if c["grouped"]:
        lines.append("Also listed as: " + "; ".join(f'{g["name"]} ({g["relation"]}, {g["status"]})' for g in c["grouped"]))
    if c["declined_via"]:
        lines.append(f'Declined because the broader diagnosis "{c["declined_via"]}" was declined on evidence that covers it.')
    if c["proposed_from"]:
        lines.append("Proposed from: " + " | ".join(c["proposed_from"]))
    if not c["confirmed"] and not c["refuted"] and not c["unverified_decisive"] and not c["unverified_nonspecific"]:
        lines.append("No diagnostic criteria could be retrieved for this name; it was not verified against the record.")
    if c["confirmed"]:
        lines.append("Confirmed in the record:")
        lines += [_evidence_line(e) for e in c["confirmed"]]
    if c["refuted"]:
        lines.append("Refuted in the record:")
        lines += [_evidence_line(e) for e in c["refuted"]]
    if c["unverified_decisive"] or c["unverified_nonspecific"]:
        parts = [f'({u["category"]}) {u["criterion"]}' for u in c["unverified_decisive"]]
        if c["unverified_nonspecific"]:
            parts.append(f'and {c["unverified_nonspecific"]} nonspecific criteria')
        lines.append("Not verified (no matching record entry): " + "; ".join(parts))
    if c["routes"]:
        lines.append("Published criteria sets: " + "; ".join(
            f'{r["label"]} [{r["set"]}] {"MET" if r["met"] else "not met"}, '
            f'{"still reachable" if r["reachable"] else "no longer reachable"}'
            + (f', {r["share"]:.0%} of its items confirmed' if isinstance(r["share"], (int, float)) else "")
            for r in c["routes"]))
    return "\n".join(lines)


def input_text(inp: dict) -> str:
    parts = ["## [V] Clinical vignette", inp["vignette"].strip(), "", "## Medical record documents (date order)"]
    for d in inp["documents"]:
        parts += [f'### [{d["id"]}] {d["date"]} {d["name"]}', d["text"].strip(), ""]
    parts += ["## Candidate diagnoses (verified against the record; names expressing one hypothesis are grouped)"]
    parts += [_candidate_text(c) + "\n" for c in inp["candidates"]]
    parts += ["## Declined candidates (a necessary or precondition criterion was judged refuted)"]
    parts += [_candidate_text(x) + "\n" for x in inp["declined"]] or ["(none)"]
    return "\n".join(parts)


def known_ids(inp: dict) -> set[str]:
    ids = {"V"} | {d["id"] for d in inp["documents"]}
    for c in inp["candidates"] + inp["declined"]:
        ids.add(c["id"])
        ids |= {e["id"] for e in c["confirmed"] + c["refuted"]}
    return ids
