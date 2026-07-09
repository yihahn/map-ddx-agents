# Module 3 (Self-directed, deepagents 완전위임) — Design Spec (v2)

## 입력자료
Module1 결과 + Module2 결과 (각각 별도 리스트, 병합은 agent가 `merge_prelim_ddx` 도구로 직접 수행)


## 목표 (Goal)
```
Module1 결과와 Module2 결과를 merge_prelim_ddx로 병합해 Top-N Prelim. DDx 목록을 만들어라.
그 목록의 각 진단마다:
1. fetch_criteria로 진단기준을 가져오라 (사이트는 고정되어 있으니 임의로 다른 곳을 찾지 마라)
2. 기준 항목마다 query_emr_index / load_emr_doc으로 EMR을 조회해 지지/반박/미확인을 판정하라
3. 애매하거나 근거가 부족하면 추가 EMR 문서를 스스로 조회하라
4. 판정이 끝나면 determine_status를 호출해 최종 status를 결정하라 (직접 판단해 채우지 말고 반드시 이 도구를 써라)
5. build_workup_gap을 호출해 미확인 항목을 Workup Gap으로 기록하라

종료 조건:
- 목록의 모든 진단을 처리했으면 save_result로 저장 후 종료하라.
- 한 진단에서 EMR 조회가 5회를 초과하면 그 시점까지의 판정으로 determine_status를 호출한 뒤 다음 진단으로 넘어가라.
- 전체 목록을 다 처리하지 못했다면 "목표 달성 실패: N개 진단 미완료" 메시지와 함께
  완료된 부분까지 저장하고 종료하라.
```


## 제공 Tools/Skills

| 함수명(입력인자) | 알고리즘 개요 | 반환값 |
|---|---|---|
| `merge_prelim_ddx(module1_ddx, module2_ddx)` | diagnosis_name 완전일치로 dedup, evidence 병합, source_detail " \| " 결합. 둘 중 하나가 None이면 빈 리스트로 취급 | `list[DDxItem]` |
| `fetch_criteria(diagnosis_name)` | MedlinePlus, Merck Manual Professional 고정 사이트 스크래핑 (StatPearls는 검색 가능한 엔드포인트 미확보로 제외) | `list[Criterion]` |
| `query_emr_index(keyword)` | EMR Index에서 keyword 관련 문서 ID 검색 | `list[str]` (문서 ID 목록) |
| `load_emr_doc(doc_id)` | 지정 문서 원문 로드 | `str` |
| `determine_status(verification_result)` | 결정론적 규칙: refuted 1개 이상→"declined" / 전부 supported→"supported" / 그 외→"pending" | `str` (status) |
| `build_workup_gap(diagnosis_name, verification_result)` | judgement=="unconfirmed" 항목마다 Gap 생성 | `list[WorkupGap]` |
| `save_result(ddx_list, gap_list)` | Module3 JSON 스키마로 저장 | `str` (저장 경로) |


## Pseudo Code (agent 조립)
```python
from deepagents import create_deep_agent
from pydantic import BaseModel
from schemas import DDxItem, Evidence, WorkupGap

class Module3Output(BaseModel):
    ddx_list: list[DDxItem]
    gap_list: list[WorkupGap]
    status: str   # "success" | "실패: N개 진단 미완료"

agent = create_deep_agent(
    model=ORCHESTRATOR_MODEL,
    tools=[merge_prelim_ddx, fetch_criteria, query_emr_index, load_emr_doc,
           determine_status, build_workup_gap, save_result],
    system_prompt=GOAL_PROMPT,   # 위 2번 목표문
    response_format=Module3Output,
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": f"Module1: {module1_ddx}\nModule2: {module2_ddx}"}]},
    config={"recursion_limit": len(prelim_ddx_list) *10 + 10}, # 진단당 ~ 10 step + merge 등 초기 오버헤드 10
)
```


## 출력: `schema_examples.md` 파일의 Module 3 출력 예시 참조 


## Deterministic(hybrid) 버전 대비 검증 필요사항
- fan-out 없어 진단 처리 순서·깊이가 실행마다 달라짐 — 뒷 순번 진단이 아예 처리 안 될 위험
- **`determine_status`/`build_workup_gap`을 tool로 제공해도 agent가 실제로 호출하는지는 강제되지 않음** — hybrid 버전은 그래프 edge로 강제되지만, 여기선 agent가 도구 호출을 건너뛰고 자기 판단으로 status를 채워도 막을 방법이 없음 (가장 중요한 검증 포인트)
- "진단당 EMR 조회 5회 초과 시 pending" 규칙 준수 여부 반복 실행으로 확인 필요
- 전체 `recursion_limit` 도달 시 앞쪽 진단에 자원이 몰려 뒤쪽이 누락될 위험
- Tools / Skills은 hybrid version을 참조하여 작성하면 될 듯
