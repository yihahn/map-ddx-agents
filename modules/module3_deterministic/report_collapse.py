import argparse
import html
import json
from pathlib import Path

# Input: for each patient id, the newest Module 3 run directory that has collapse output (03_final_ddx_list,
# decline_ddx_list, 07_grouped/decline/excluded lists and 07_collapse_log.json). Output: one self-contained
# HTML page (default reports/collapse_report.html, local only — it carries EMR evidence) with a before/after
# count table and, per patient, the collapsed forest as nested click-to-open sections. Algorithm: walk each
# patient's grouped forest (collapse v3) and attach to every node the reasons the two whole-list runs gave for
# its placement, the proposals when they disagreed, its decline-inheritance checks and its Module 1/2 and EMR evidence.

MODULE3_OUTPUT = Path(__file__).resolve().parent / "output"
REPORT = Path(__file__).resolve().parents[2] / "reports" / "collapse_report.html"
PATIENTS = ["PT01", "PT03", "PT04", "PT05", "PT06", "PT07", "PT09", "PT10", "PT11"]
CHECKS = {"targeted_lesion": "병변", "adequate": "적절", "representative": "대표", "scope_covers_subtype": "범위"}
e = html.escape


def _latest_collapsed(patient_id: str) -> Path:
    runs = [d for d in sorted(MODULE3_OUTPUT.glob(f"{patient_id}_*")) if (d / "07_collapse_log.json").exists()]
    if not runs:
        raise SystemExit(f"No collapsed run for {patient_id}")
    return runs[-1]


def _load(run: Path, name: str):
    return json.loads((run / name).read_text(encoding="utf-8"))


def _walk(nodes):
    for n in nodes:
        yield n
        yield from _walk(n["children"])


def _votes(votes: list[dict], extra=lambda v: "") -> str:
    rows = []
    for v in votes:
        if "error" in v:
            rows.append(f"<li class='err'>오류: {e(v['error'])}</li>")
        else:
            rows.append(f"<li><b>{e(str(v.get('key')))}</b>{extra(v)} — {e(v.get('reason', ''))}</li>")
    return "<ul class='votes'>" + "".join(rows) + "</ul>"


def _evidence(item: dict, emr: bool) -> str:
    """emr=False: Module 1/2 proposal rationale (vignette quotes; supports set by the proposing LLM).
    emr=True: Module 3 judgements against an EMR document (they carry source_doc)."""
    rows = []
    for ev in item.get("evidence", []):
        if not ev.get("content") or bool(ev.get("source_doc") not in (None, "None")) != emr:
            continue
        mark = {True: "✓ 지지", False: "✗ 반박"}.get(ev.get("supports"), "· 미확정")
        src = " · ".join(str(ev[k]) for k in ("source_doc", "date") if ev.get(k) not in (None, "None"))
        crit = f"<div class='crit'>기준: {e(ev['criterion'])}</div>" if ev.get("criterion") not in (None, "None") else ""
        rows.append(f"<li><span class='sup'>{mark}</span> {e(ev['content'])}"
                    + (f" <span class='src'>[{e(src)}]</span>" if src else "") + crit + "</li>")
    return "<ul class='ev'>" + "".join(rows) + "</ul>" if rows else "<p class='muted'>없음</p>"


def _evidence_blocks(item: dict, status_line: str) -> str:
    return (f"<details class='inner'><summary>제안 근거 (Module 1·2, vignette 기반 — 지지/반박은 제안한 LLM의 표시)</summary>"
            f"{_evidence(item, emr=False)}</details>"
            f"<details class='inner'><summary>Module 3 EMR 검증 ({status_line})</summary>{_evidence(item, emr=True)}</details>")


def _code_line(n: dict) -> str:
    parts = [f"MONDO {n['mondo_id']} · {n['mondo_label']}" if n.get("mondo_id") else "",
             f"SNOMED {n['sctid']} · {n['snomed_term']}" if n.get("sctid") else ""]
    return " / ".join(x for x in parts if x) or "코드 없음 (글자가 정확히 일치하는 용어 없음)"


def _decline_checks(entries: list[dict]) -> str:
    out = []
    for d in entries:
        out.append(f"<p>상위 진단 <b>{e(d['declined_ancestor'])}</b>의 기각 상속 검토 ({e(d.get('via', 'mondo')).upper()} {d['hops']}단계 위): <b>{e(d['verdict'])}</b></p>")
        out.append(_votes(d["votes"], lambda v: " <span class='chk'>" + " ".join(
            ("●" if v.get(c) else "○") + label for c, label in CHECKS.items()) + "</span>" if "adequate" in v else ""))
    return "".join(out)


def _node(n: dict, idx: dict) -> str:
    name = n["diagnosis_name"]
    badges = []
    if n.get("relation") == "subtype":
        badges.append("<span class='b sub'>하위 진단</span>")
    elif n.get("relation") == "same":
        badges.append("<span class='b same'>같은 가설</span>")
    if n.get("needs_review"):
        badges.append("<span class='b rev'>검토 필요</span>")
    if n.get("categorical"):
        badges.append("<span class='b cat'>넓은 범주</span>")
    onto = n.get("ontology") or {}
    if onto:
        badges.append({"agrees": "<span class='b ok'>온톨로지 일치</span>", "same": "<span class='b ok'>온톨로지 동의어</span>",
                       "inverted": "<span class='b rev'>온톨로지와 반대</span>"}[onto["tag"]])
    if n.get("ontology_suggests"):
        badges.append("<span class='b cat'>온톨로지 제안</span>")
    body = []
    if n.get("relation"):
        kind = "같은 가설 (대표 아래)" if n["relation"] == "same" else "하위 진단"
        body.append(f"<p>배치: <b>{kind}</b> — 두 실행이 같은 배치를 냄. 각 실행의 이유:</p>"
                    + "<ul class='votes'>" + "".join(f"<li>{e(r)}</li>" for r in n.get("placement_reasons", [])) + "</ul>")
    if name in idx["disputed"]:
        body.append("<p>배치가 두 실행에서 달라 원래 자리에 둠 (검토 필요):</p><ul class='votes'>"
                    + "".join(f"<li>실행 {i}: {e(p)}</li>" for i, p in enumerate(idx["disputed"][name], 1)) + "</ul>")
    if onto:
        what = {"agrees": "온톨로지에서도 상위 항목이 이 진단의 상위 개념", "same": "온톨로지에서 상위 항목과 같은 코드(동의어)",
                "inverted": "온톨로지에서는 오히려 이 진단이 상위 항목의 상위 개념 — 확인 필요"}[onto["tag"]]
        body.append(f"<p>온톨로지 대조: {what} ({e(', '.join(onto['via']))})</p>")
    for sg in n.get("ontology_suggests", []):
        rel = "하위 개념" if sg["relation"] == "subtype" else "같은 코드(동의어)"
        body.append(f"<p>온톨로지 제안: {e(sg['via'][0] if len(sg['via']) == 1 else ' · '.join(sg['via']))}에서는 "
                    f"<b>{e(sg['target'])}</b>의 {rel}인데 따로 놓였음 — 확인 필요</p>")
    body.append(f"<p class='muted'>코드: {e(_code_line(n))}</p>")
    if idx["decline"].get(name):
        body.append(_decline_checks(idx["decline"][name]))
    body.append(_evidence_blocks(n, f"상태: {e(str(n.get('status')))}, EMR에서 확인된 지지 {n.get('emr_support', 0)}건"))
    children = "".join(_node(c, idx) for c in n["children"])
    kids = f" <span class='muted'>· 하위 {len(list(_walk(n['children'])))}개</span>" if n["children"] else ""
    return (f"<details class='node'><summary><span class='nm'>{e(name)}</span> {''.join(badges)}{kids}</summary>"
            f"<div class='detail'>{''.join(body)}</div>"
            f"{('<div class=kids>' + children + '</div>') if children else ''}</details>")


def _patient(pid: str, run: Path) -> tuple[dict, str]:
    kept, declined = _load(run, "03_final_ddx_list.json"), _load(run, "decline_ddx_list.json")
    roots, decl_out = _load(run, "07_grouped_ddx_list.json"), _load(run, "07_decline_ddx_list.json")
    excluded, log = _load(run, "07_excluded_ddx_list.json"), _load(run, "07_collapse_log.json")
    org = log["organize"]
    idx = {"decline": {}, "disputed": {d["name"]: d["proposals"] for d in org["disputed"]}}
    for d in log["decline"]:
        idx["decline"].setdefault(d["child"], []).append(d)
    nodes = list(_walk(roots))
    inherited = [d for d in decl_out if d.get("declined_via")]
    counts = {"pid": pid, "run": run.name, "m3_kept": len(kept), "m3_declined": len(declined), "roots": len(roots),
              "subtype": sum(n.get("relation") == "subtype" for n in nodes),
              "same": sum(n.get("relation") == "same" for n in nodes),
              "inherited": len(inherited), "excluded": len(excluded),
              "review": sum(bool(n.get("needs_review")) for n in nodes + decl_out + excluded),
              "onto_ok": sum(bool(n.get("ontology")) and n["ontology"]["tag"] != "inverted" for n in nodes),
              "onto_inv": sum(bool(n.get("ontology")) and n["ontology"]["tag"] == "inverted" for n in nodes),
              "onto_sug": sum(len(n.get("ontology_suggests", [])) for n in nodes)}
    timing = ", ".join(f"실행 {i} {r.get('seconds')}초" + (f" (오류: {e(r['error'])})" if "error" in r else "")
                       for i, r in enumerate(org["runs"], 1))
    parts = [f"<p class='muted'>실행 폴더: {e(run.name)} · 문맥 정리 {e(org['status'])}: {timing}</p>",
             f"<h3>남은 진단 — 최상위 {len(roots)}개 (전체 {len(nodes)}개)</h3>",
             "".join(_node(n, idx) for n in roots)]
    parts.append(f"<h3>기각 — {len(decl_out)}개 (Module 3 기각 {len(declined)} + 상속 {len(inherited)})</h3>")
    for d in decl_out:
        why = (f"<p>상위 진단 <b>{e(d['declined_via'])}</b>에서 상속 기각</p>" + _decline_checks(idx["decline"].get(d["diagnosis_name"], []))
               if d.get("declined_via") else "<p>Module 3에서 EMR 근거로 기각</p>")
        tag = " <span class='b inh'>상속 기각</span>" if d.get("declined_via") else ""
        parts.append(f"<details class='node dec'><summary><span class='nm'>{e(d['diagnosis_name'])}</span>{tag}</summary>"
                     f"<div class='detail'>{why}<p class='muted'>코드: {e(_code_line(d))}</p>"
                     f"{_evidence_blocks(d, '상태: ' + e(str(d.get('status'))))}</div></details>")
    if excluded:
        parts.append(f"<h3>제외 (진단 아님) — {len(excluded)}개</h3>")
        for x in excluded:
            reasons = "".join(f"<li>{e(r)}</li>" for r in x.get("exclusion_reasons", []))
            parts.append(f"<details class='node exc'><summary><span class='nm'>{e(x['diagnosis_name'])}</span>"
                         f" <span class='muted'>{e(x.get('exclusion_category', ''))}</span></summary>"
                         f"<div class='detail'><ul class='votes'>{reasons}</ul></div></details>")
    return counts, "".join(parts)


CSS = """
:root{--bg:#fbfbf9;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;--sub:#2f6f4f;--same:#4b5fa8;--rev:#b5541b;--cat:#7a6a2a;--inh:#8a3b3b}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#9c9b94;--line:#33332f;--card:#1f1f1d;--sub:#7fc4a0;--same:#9fb0f0;--rev:#f0a070;--cat:#d8c47a;--inh:#e39a9a}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:0}h3{font-size:15px;margin:18px 0 8px;color:var(--muted)}
.muted{color:var(--muted)}table{border-collapse:collapse;width:100%;margin:12px 0 20px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right}th:first-child,td:first-child{text-align:left}
details{border:1px solid var(--line);border-radius:8px;background:var(--card);margin:6px 0}
summary{cursor:pointer;padding:8px 10px;list-style:none}summary::-webkit-details-marker{display:none}
summary:before{content:"▸";display:inline-block;width:14px;color:var(--muted)}details[open]>summary:before{content:"▾"}
.patient>summary{font-weight:600;font-size:16px}.patient>.body{padding:0 12px 12px}
.kids{margin:0 8px 8px 22px}.detail{padding:0 12px 6px 26px;font-size:14px}.detail p{margin:6px 0}
.code{float:right;color:var(--muted);font-size:12.5px;max-width:45%;text-align:right}.nm{font-weight:600}
.b{font-size:11.5px;border:1px solid;border-radius:10px;padding:0 7px;margin-left:6px}
.sub{color:var(--sub)}.ok{color:var(--sub)}.same{color:var(--same)}.rev{color:var(--rev)}.cat{color:var(--cat)}.inh{color:var(--inh)}
.votes,.ev{margin:4px 0 8px;padding-left:20px}.votes li,.ev li{margin:3px 0}.err{color:var(--rev)}
.chk{font-family:ui-monospace,monospace;font-size:12px;color:var(--muted)}.src{color:var(--muted);font-size:12.5px}
.crit{color:var(--muted);font-size:12.5px}.inner{margin:6px 0 4px}.inner>summary{font-size:13.5px;color:var(--muted)}
.sup{font-size:12.5px}.toolbar button{font:inherit;padding:4px 10px;margin-right:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;cursor:pointer}
@media (max-width:640px){.code{float:none;display:block;max-width:100%;text-align:left;margin-left:14px}}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a click-to-open HTML report of collapse results.")
    parser.add_argument("patients", nargs="*", default=PATIENTS)
    parser.add_argument("--out", type=Path, default=REPORT)
    args = parser.parse_args()

    rows, sections = [], []
    for pid in args.patients:
        c, body = _patient(pid, _latest_collapsed(pid))
        rows.append(c)
        sections.append(f"<details class='patient'><summary>{pid} — Module 3 남은 진단 {c['m3_kept']}개 → 접은 뒤 최상위 {c['roots']}개"
                        f" <span class='muted'>(하위 진단 {c['subtype']}, 같은 가설 {c['same']}, 상속 기각 {c['inherited']}, 제외 {c['excluded']})</span></summary>"
                        f"<div class='body'>{body}</div></details>")
    keys = ("m3_kept", "m3_declined", "roots", "subtype", "same", "inherited", "excluded", "review", "onto_ok", "onto_inv", "onto_sug")
    total = {k: sum(r[k] for r in rows) for k in keys}
    head = "".join("<tr><td>" + r["pid"] + "</td>" + "".join(f"<td>{'<b>' if k == 'roots' else ''}{r[k]}</td>" for k in keys) + "</tr>"
                   for r in rows)
    head += "<tr><th>합계</th>" + "".join(f"<th>{total[k]}</th>" for k in keys) + "</tr>"
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Collapse 결과 보기</title><style>{CSS}</style></head><body><main>
<h1>진단명 접기(collapse) 결과 — 환자 {len(rows)}명</h1>
<p class="muted">로컬 전용: 실제 EMR 근거가 들어 있어 외부로 공유하지 않습니다. 항목을 클릭하면 배치 이유(두 번의 독립 판정)와 Module 1·2 제안 근거, Module 3 EMR 검증이 펼쳐집니다.</p>
<table><thead><tr><th>환자</th><th>Module 3 남은 진단</th><th>Module 3 기각</th><th>접은 뒤 최상위</th><th>하위 진단</th><th>같은 가설</th><th>상속 기각</th><th>제외</th><th>검토 필요</th><th>온톨로지 일치·동의어</th><th>온톨로지와 반대</th><th>온톨로지 제안</th></tr></thead><tbody>{head}</tbody></table>
<p class="muted">"접은 뒤 최상위" = 묶은 트리의 맨 위 항목 수. 하위 진단과 같은 가설로 묶인 진단은 지워지지 않고 상위 항목 아래에 펼쳐 볼 수 있습니다. 상속 기각과 제외 항목은 기각·제외 목록으로 옮겨집니다.
온톨로지 태그는 MONDO·SNOMED 코드가 있는 쌍에만 붙는 참고 표시로, 배치를 바꾸지 않습니다 (코드가 없는 진단명은 확인 불가).
기각 상속 검토의 ●/○ = 4기준 충족/미충족 (병변: 의심 병변 직접 검사, 적절: 적절성, 대표: 대표성, 범위: 결론 범위) — 넷 다 ●일 때만 상속합니다.</p>
<div class="toolbar"><button onclick="document.querySelectorAll('details').forEach(d=>d.open=true)">모두 펼치기</button><button onclick="document.querySelectorAll('details').forEach(d=>d.open=false)">모두 접기</button></div>
{''.join(sections)}
</main></body></html>"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"Wrote {args.out} ({len(page) // 1024} KB)")
    for r in rows:
        print(f"{r['pid']}: {r['m3_kept']} kept / {r['m3_declined']} declined -> {r['roots']} top-level "
              f"(subtype {r['subtype']}, same {r['same']}, inherited {r['inherited']}, excluded {r['excluded']}, review {r['review']})")


if __name__ == "__main__":
    main()
