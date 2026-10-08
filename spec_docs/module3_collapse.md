# Module 3 후처리. Collapse (환자 문맥 기반 DDx 정리) — Code Design Specification (v3)

```
   03_final_ddx_list.json (kept)      decline_ddx_list.json (declined)
                └──────────────┬──────────────┘
                               ▼
                 ┌──────────────────────────┐
                 │ A. 코드 부착 (LLM 없음)   │  MONDO 정식 label, SNOMED 용어와
                 │                          │  글자가 똑같을 때만 — 라벨·계층 후보용
                 └────────────┬─────────────┘
                              ▼
                 ┌──────────────────────────┐
                 │ C. decline 상속          │  MONDO·SNOMED 조상이 declined인 kept 진단
                 │   (4기준, 2표 만장일치)  │  반박 근거가 4기준을 모두 충족할 때만
                 └────────────┬─────────────┘
                              ▼
                 ┌──────────────────────────┐
                 │ D. 문맥 기반 정리        │  환자 목록 전체 + 근거를 한 번에 판정
                 │   (전체 목록 2회, 일치만)│  같은 가설·하위 진단·비진단 — 삭제 없음
                 └────────────┬─────────────┘
                              ▼
                 ┌──────────────────────────┐
                 │ E. 온톨로지 교차 태그    │  코드가 있는 쌍만, MONDO·SNOMED와 대조해
                 │   (LLM 없음, 표시만)     │  일치·동의어·반대·제안 — 배치는 안 바꿈
                 └────────────┬─────────────┘
                              ▼
   07_grouped_ddx_list.json · 07_decline_ddx_list.json · 07_excluded_ddx_list.json · 07_collapse_log.json
```

## 목적과 원칙
- 목적: **이 환자의 감별진단 목록을 사람이 검토하기 쉽게 정리**하는 것. 온톨로지 표준화가 목적이 아니다.
- 핵심 판단은 "이 환자에서 두 진단명이 같은 가설인가 / 한쪽이 다른 쪽의 하위 진단인가" — 환자 근거(Module 1·2 제안 근거, Module 3 EMR 검증)를 보고 판단한다.
- **삭제는 두 경우뿐**: 진단이 아닌 항목(약물·노출 등)을 제외 목록으로, 상위 진단의 반박 근거가 하위 진단까지 배제할 때 기각 목록으로 옮긴다. 나머지는 지우지 않고 묶는다.
- 목록을 바꾸는 LLM 판정은 **두 번의 독립 판정이 똑같을 때만** 반영한다. 다르거나 실패하면 목록을 바꾸지 않고 `needs_review`로 표시한다.
- LLM 시간은 핵심 판단에 쓴다 (v2.1은 환자당 약 160회 호출 중 94%를 용어 코드 매칭·비진단 판정에 썼고, 목록 정리 판정은 6%였다).

## 입력
- `output/<PID>_<ts>/03_final_ddx_list.json` (kept), `decline_ddx_list.json` (declined) — `DDxItem` 목록, 각 항목의 `evidence`에 Module 1·2 제안 근거(`source_doc` 없음)와 Module 3 EMR 판정(`source_doc` 있음)이 함께 있다.
- `data_prep/mondo.json` — label, EXACT 동의어, is_a.
- `data_prep/snomed_concepts.csv`, `snomed_descriptions.csv`, `snomed_isa.csv` — 용어와 is_a (라이선스 콘텐츠, git 미추적). 임베딩은 쓰지 않는다.

## A. 코드 부착 (LLM 없음)
- MONDO: 이름(소문자)이 정식 label과 같을 때만. EXACT 동의어는 쓰지 않는다 — 9명 세트에서 10건을 잘못 붙였다 (Hashimoto encephalopathy → hereditary elliptocytosis: 약어 HE 공유, HIE → perinatal asphyxia, AEP → idiopathic AEP 등).
- SNOMED: 이름(소문자)이 선호 용어·허용 동의어·FSN(태그 제외) 중 하나와 같고 그 용어를 가진 개념이 하나뿐이면 그 개념. HISTORICAL 용어는 쓰지 않는다.
- 쓰임: 출력 라벨(`mondo_id`, `sctid`), C의 후보 쌍(조상 관계), E의 태그, `categorical` 표시. D의 판정에는 영향을 주지 않는다.
- 커버리지(9명, 서로 다른 이름 315개): MONDO label 52%, SNOMED 용어 64%, 합쳐 72% — 나머지 28%는 코드 없이 남는다 (D의 정리는 코드와 무관하게 받는다).
- v2.1의 BioLORD 후보 + LLM 매칭 단계는 없앴다: MONDO 2·3단계는 채택 9건 중 8건이 오답이었고, 코드 매칭이 호출의 80%를 차지했다.

## C. decline 상속
- 대상: kept 진단 중, MONDO 조상(둘 다 MONDO 코드가 있을 때) 또는 SNOMED 조상(둘 다 SNOMED 코드가 있을 때)에 declined 진단이 있는 것. 가까운(hop이 작은) 조상부터 묻고 첫 "예"에서 멈춘다.
- 판정: 상위 진단을 배제한 반박 근거(최대 4개, 출처 문서·날짜 포함)와 상위 진단을 의심하게 한 소견(최대 4개)을 보여주고, 반박 근거가 **네 기준을 모두** 충족하는지 각각 묻는다 (임상 검토 2026-10-06):
  1. `targeted_lesion` — 하위 진단이 의심되는 바로 그 병변을 검사했다 (의심 병변이 림프절인데 골수를 본 것은 아님).
  2. `adequate` — 검체·검사가 하위 진단을 찾아낼 만큼 충분하다 (절제 생검 + 면역표현형 검사 등. 세침흡인 세포검사 단독은 아님).
  3. `representative` — 검체가 의심 병변을 대표한다 (의심 림프절 전체 절제 등).
  4. `scope_covers_subtype` — 결론의 범위가 하위 진단을 포괄한다 ("골수 침범 없음"은 골수 침범만 배제).
  - 표의 key는 네 판정이 모두 true일 때만 "예" — 코드가 계산한다. 하나라도 미충족이거나 판단할 수 없으면 상속하지 않는다.
  - 질문은 "이 근거가 하위 진단을 의심한 근거(그 병변)를 제거하는가"로 묻는다. "국소 증거일 뿐"을 일반 원칙으로만 주면 모델은 네 기준을 모두 충족한 절제 생검도 거부했다 (양성 대조 4쌍 중 0쌍 → 문구 수정 후 4쌍 모두 "예").
- 두 표(같은 질문 2회) 모두 "예" → 기각 목록으로 이동 (`status = "declined_inherited"`). 갈리거나 실패하면 상속하지 않고 `needs_review`.
- 배경: 단일 질문("반드시 배제하는가")은 골수 생검의 "no evidence of lymphoma"(림프절 생검 미시행)로 NHL 하위 림프종 9개 중 8개를 상속 기각했다 — 임상 검토에서 과도한 전파로 판정.

## D. 문맥 기반 정리
- 입력: C 이후 남은 kept 진단 전체에 번호를 붙이고, 각 진단의 Module 3 상태, 제안 근거, EMR 검증을 함께 준다 (환자당 약 5,000~8,000 토큰).
- 출력 (guided JSON): 최상위가 아닌 진단마다 `{reason, entry, relation, target}`, 그리고 진단이 아닌 항목 `{reason, entry, category}`.
  - `relation = "same"`: 이 환자에서 두 진단명이 **같은 진단 가설** (동의어, 또는 근거상 일반명이 다른 항목이 가리키는 특정 질환을 뜻하는 경우 — 예: 원인 약물과 호산구증가가 있는 환자의 "Hypersensitivity syndrome" = DRESS). `target`은 **근거가 가장 구체적으로 뒷받침하는 진단명**(대표).
  - `relation = "subtype"`: 엄격한 임상 정의로 entry의 모든 사례가 target의 사례 — 환자 근거가 아니라 정의로 판단한다.
  - **한쪽이 다른 쪽을 포함하는 넓은 진단이면 "same"이 아니라 "subtype"이고 넓은 쪽이 target이다** — 근거가 좁은 쪽을 가리켜도 마찬가지. 시험에서 "same"으로 합친 쌍(drug-induced ILD = ICI pneumonitis)이 임상 검토에서 상하위 관계로 판정되었고, "same"으로 합치면 넓은 진단이 좁은 진단 아래로 들어가 계층이 뒤집힌다.
  - 비진단: 약물·약물군, 단일 검사·영상 값, 단독 증상, 노출, 시술만. 임상 증후군은 진단이다.
- 규칙(프롬프트, 임상 검토 판정): pneumonia ≠ pneumonitis (감염성 pneumonia는 pneumonitis의 하위가 아님); pneumonitis ⊂ ILD; ICI pneumonitis ⊂ pneumonitis, ⊂ drug-induced ILD; organizing pneumonia·AIP·DIP·NSIP·LIP는 ILD 계열 (pneumonia·pneumonitis 아래 아님); microscopic colitis ⊄ IBD; DILI ⊂ hepatotoxicity·liver injury·toxic hepatitis; MTX-induced liver injury ⊂ MTX toxicity; IVLBCL ⊄ DLBCL; 확신이 없으면 최상위에 둔다. 이 규칙들은 평가 세트의 오판에서 나온 것이라, 같은 세트로 잰 점수는 낙관적이다 — 새 환자로 검증해야 한다.
- 투표: 같은 프롬프트를 2회 독립 호출(호출당 파싱 실패·`length`는 같은 설정으로 최대 3회 시도).
  - 배치 관계 `(entry, relation, target)`가 **두 결과에서 똑같으면** 반영, 한쪽에만 있거나 다르면 `entry`를 최상위에 두고 `needs_review`.
  - 예외 — 같은 계통 (임상 결정 2026-10-08): 두 결과가 모두 `subtype`이고 target이 다르지만, 한 target이 다른 target의 (이미 반영된 배치상) 조상이면 두 결과가 같은 계통에 동의한 것으로 보고 **더 구체적인 target 아래에** 둔다 (로그 `rule: "lineage"`). 예: HSTCL → T-cell lymphoma vs HSTCL → PTCL (PTCL ⊂ T-cell lymphoma 반영됨) → PTCL 아래.
  - 비진단: 두 결과 모두 같은 entry를 비진단으로 표시하면 제외 목록으로, 한쪽만이면 `needs_review`.
  - 한 호출이라도 끝내 실패하면 배치·제외를 하나도 반영하지 않고 모든 항목을 그대로 둔 채 로그에 `error`.
- 검증: 번호가 범위 밖·자기 자신·중복 entry인 관계는 버린다. 제외된 항목을 target으로 하는 관계, 순환을 만드는 관계도 버린다 (entry는 최상위).
- 시험 (PT03, PT06, 2026-10-07): 최상위 31→23, 27→18 (v2.1: 28, 24). 두 결과가 같은 관계 8·9개, 다른 관계 2·2개. 호출당 출력 약 2.5만 토큰, 환자당 15~17분 (v2.1: 70~110분).

## E. 온톨로지 교차 태그 (LLM 없음)
- LLM이 사전 지식으로 정한 배치는 두 실행이 일치해도 **일관되게 틀릴 수 있다** (예: HBV ⊂ hepatitis는 서버·버전을 바꿔도 같은 방향으로 틀림). 코드가 있는 쌍에 한해 온톨로지와 대조해 참고 태그를 붙인다. 태그는 배치·`needs_review`를 바꾸지 않는다.

| 태그 | 조건 |
|---|---|
| `agrees` (온톨로지 일치) | D가 A를 B 아래에 두었고, MONDO 또는 SNOMED에서도 B가 A의 조상 |
| `same` (온톨로지 동의어) | 두 이름의 코드가 같다 |
| `inverted` (온톨로지와 반대) | D가 A를 B 아래에 두었는데, 온톨로지에서는 A가 B의 조상 |
| `suggests` (온톨로지 제안) | D는 따로 두었는데(트리상 조상·자손 아님), 온톨로지에서는 A ⊂ B 또는 같은 코드 |

- 두 온톨로지가 엇갈리면 일치가 반대를 이긴다. 관계가 없으면 태그 없음 (온톨로지가 완전하지 않으므로 "관계 없음"은 표시하지 않는다).
- 임상 검토로 상하위가 아니라고 판정된 쌍(pneumonia ⊄ pneumonitis, IVLBCL ⊄ DLBCL 등, `_RULED_APART`)은 제안하지 않는다.
- `collapse PT05 --retag`: 기존 v3 결과에 LLM 없이 코드·라벨·태그만 다시 계산한다.

## 출력

### `07_grouped_ddx_list.json` — kept 진단의 forest
```python
class GroupNode(DDxItem):
    mondo_id: str | None          # A에서 부착 (글자 일치일 때만)
    mondo_label: str | None
    sctid: str | None
    snomed_term: str | None       # SNOMED US English 선호 용어
    emr_support: int              # supports=True 이고 source_doc 있는 evidence 수
    categorical: bool             # MONDO 하위 질환 > UMBRELLA_MAX_DESCENDANTS (200)
    relation: Literal["same", "subtype"] | None   # 부모와의 관계 (최상위는 None)
    ontology: {"tag": "agrees" | "same" | "inverted", "via": ["MONDO" | "SNOMED"]} | None
    ontology_suggests: list[{"target", "relation", "via"}]
    placement_reasons: list[str]  # D의 두 결과가 이 배치에 단 이유
    needs_review: bool            # 이 진단에 관한 판정이 갈렸거나 실패
    children: list["GroupNode"]
```
- 모든 kept 진단(C에서 상속 기각·D에서 제외된 것 제외)은 정확히 한 번 등장한다.

### `07_decline_ddx_list.json`, `07_excluded_ddx_list.json`
- 원래 declined + C에서 상속된 항목 / D에서 비진단으로 제외된 항목 (`exclusion_category`). 코드 라벨과 `needs_review` 부착.

### `07_collapse_log.json`
```python
{
  "llm":      {"server", "model"},
  "codes":    [{"name", "mondo_id", "sctid"}],
  "decline":  [{"child", "declined_ancestor", "hops", "via", "votes", "verdict"}],   # via: "mondo" | "snomed"
  "organize": {"runs": [{"edges", "non_diagnoses", "seconds", "completion_tokens"} | {"error"}],
               "placements": [{"entry", "relation", "target", "reasons"}],
               "excluded": [{"name", "category", "reasons"}],
               "disputed": [{"name", "proposals"}], "status": "ok" | "error"},
  "ontology_tags": [{"name", "tag", "parent" | "target", "relation"?, "via"}],
  "review":   [{"stage", "name", "detail"}],
}
```

## LLM 설정
- 서버: `QWEN_SERVER` (`llm.py`), Qwen3.8 thinking on, temperature 0.6, top_p 0.95 — 두 판정이 독립 표본이어야 일치가 의미를 가진다.
- structured output: vLLM guided decoding (`response_format: json_schema`)을 plain `chat.completions.create`로 호출.
- `max_tokens`: C 16,384, D 40,960 (시험에서 호출당 약 2.5만 토큰).
- 동시 호출: 서버 한도(`MAX_CONCURRENT_REQUESTS`)까지.

## 평가
- `modules/module3_deterministic/eval/` (git 미추적, 환자 데이터 유래): `gold_hierarchy.json`의 하위유형 쌍(D 평가: 자식이 트리에서 부모 아래에 있으면 "예" — same/subtype 구분 없음), decline 쌍(C), 비진단 후보(D의 제외). 코드는 label 일치라 채점하지 않는다 (`gold_match*.json`은 v2.1 기록).
- 확장분은 Claude 초안(`note`에 draft 표시)이고 애매한 항목만 임상 검토로 확정.
- `eval_collapse.py`가 로그와 트리를 gold와 대조한다.

## 설계상 열린 이슈
1. D는 한 번의 긴 판정이라 실행마다 배치가 달라질 수 있다 — 2회 일치 규칙이 흔들리는 배치를 검토로 돌린다.
2. "같은 가설"의 정답은 환자별이다 — 환자 단위 정답 세트가 필요하다.
3. 코드가 글자 일치로만 붙으므로 표준 코드가 없는 진단명이 남는다 (라벨일 뿐 정리에는 영향 없음).
4. C의 후보는 코드가 있는 진단에만 생긴다 — 코드가 없는 하위 진단은 상속 검토를 받지 않는다 (기각되지 않고 남는 쪽이라 안전).
5. 규칙·예시는 9명 평가 세트의 오판에서 나왔다 — 새 환자로 검증해야 한다.
