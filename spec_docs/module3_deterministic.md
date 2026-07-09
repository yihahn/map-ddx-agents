# Module 3. ReAct Evid. Foraging — Code Design Specification (v2)

```
        ┌───────────────┐   ┌───────────────┐
        │ Module1 결과  │   │ Module2 결과  │
        └───────┬───────┘   └───────┬───────┘
                └─────────┬─────────┘
                          ▼
              ┌─────────────────────┐
              │  merge_prelim_ddx   │
              │ (이름 dedup + 병합) │
              └──────────┬──────────┘
                         ▼
               Send × N (DDx별, 병렬 fan-out)
     ┌───────────┬───────────┬─── ・・・ ───┐
     ▼           ▼           ▼             ▼
┌──────────┐┌──────────┐┌──────────┐  ┌──────────┐
│DDx 1     ││DDx 2     ││DDx 3     │  │DDx N     │
│fetch_    ││fetch_    ││fetch_    │  │fetch_    │
│criteria  ││criteria  ││criteria  │  │criteria  │
│   ↓      ││   ↓      ││   ↓      │  │   ↓      │
│verify_   ││verify_   ││verify_   │  │verify_   │
│with_emr  ││with_emr  ││with_emr  │  │with_emr  │
│  ↓    ↓  ││  ↓    ↓  ││  ↓    ↓  │  │  ↓    ↓  │
│determine ││build_    ││   ...    │  │   ...    │
│_status   ││workup_gap││          │  │          │
└────┬─────┘└────┬─────┘└────┬─────┘  └────┬─────┘
     └───────────┴───────────┴── ・・・ ─────┘
                         ▼
              ┌─────────────────────┐
              │      aggregate      │
              └──────────┬──────────┘
                         ▼
     Refined DDx with Concrete Evid. + Workup Gap List
```

## 프레임워크 선택 근거
- **LangGraph (merge_prelim_ddx → fan-out)**: 두 모듈 결과 병합을 첫 노드로 명시하고, DDx별로 `Send`해 전체 진단을 빠짐없이 검증 — "어떤 진단을 볼지"는 결정론적으로 보장
- **deterministic scraping (fetch_criteria)**: 진단기준 출처를 고정 사이트로 제한 — 출처 신뢰성 문제 원천 해소
- **deepagents (verify_with_emr만)**: "기준 몇 개를 더 봐야 하는지"는 가변 깊이 문제라 self-directed 필요. 단, **최종 status 판정(`determine_status`)과 Gap 기록(`build_workup_gap`)은 deterministic 규칙 함수로 분리** — self-directed 영역을 "EMR 대조 판정"으로 좁혀 종료판단·임계치 리스크를 최소화

## State
```python
from typing import Annotated, Literal, Optional
from pydantic import BaseModel
import operator
from schemas import DDxItem, Evidence, WorkupGap

class Criterion(BaseModel):
    text: str            # 진단기준 항목 원문
    source_doc: str      # "MedlinePlus" | "Merck Manual Professional | StatPearls"

class CriteriaList(BaseModel):
    criteria: list[Criterion]

class CriterionVerification(BaseModel):
    criterion: str
    date: Optional[str] = None
    source_doc: Optional[str] = None
    content: str                                    # EMR에서 찾은 근거 문구
    judgement: Literal["supported", "refuted", "unconfirmed"]

class VerificationResult(BaseModel):
    diagnosis_name: str
    verifications: list[CriterionVerification]

class Module3State(dict):
    module1_ddx: Optional[list[DDxItem]] = None   # Module1 실행 실패/누락 대비
    module2_ddx: Optional[list[DDxItem]] = None   # Module2 실행 실패/누락 대비
    prelim_ddx_list: list[DDxItem]                   # merge_prelim_ddx 출력, module1_ddx/module2_ddx가 None이면 빈 리스트로 취급, 남은 쪽만으로 진행
    refined_ddx: Annotated[list[DDxItem], operator.add] # operator.add reducer라 LangGraph가 내부적으로 빈 리스트로 관리 — 각 branch가 항상 값을 반환하므로 None이 될 일이 없음
    workup_gaps: Annotated[list[WorkupGap], operator.add] # operator.add reducer라 LangGraph가 내부적으로 빈 리스트로 관리 — 각 branch가 항상 값을 반환하므로 None이 될 일이 없음
    final_ddx_list: list[DDxItem]
    final_gap_list: list[WorkupGap] # aggregate()가 직접 만드는 값이라, 코드만 항상 []를 반환하도록 하면 None 걱정 없음

# 주: ddx_item / criteria / verification_result는 Send 분기 내부에서만 도는
#     branch-local 값이며 top-level Module3State에는 없음 (fan-in 시 reducer로만 수렴)
```

## Nodes

### 0. 병합 (신규)
```python
def merge_prelim_ddx(state: Module3State) -> dict:
    """
    - module1_ddx + module2_ddx를 diagnosis_name 완전일치로 deduplicate
      (양쪽 다 BioLORD/MONDO로 정규화된 이름이므로 문자열 매칭으로 충분)
    - 중복 시 evidence 리스트를 합치고, source_detail을 " | "로 결합
      예) "Module1: Endocrinology | Module2: (query...)"
    - 반환: {"prelim_ddx_list": [...]}
    """
```

### 1. Fan-out
```python
def route_to_verification(state: Module3State) -> list[Send]:
    """prelim_ddx_list 각 DDxItem마다 Send("fetch_criteria", {...}) 생성 (병렬 fan-out)"""
```

### 2. 진단기준 추출 (deterministic)
```python
def fetch_criteria(state: dict) -> dict:
    """
    - diagnosis_name으로 MedlinePlus, Merck Manual Professional 고정 사이트만 스크래핑
      (StatPearls는 검색 가능한 엔드포인트 미확보로 보류 — 열린 이슈 참고)
    - 진단기준 항목을 CriteriaList로 구조화 추출 (사이트 URL은 코드가 고정, LLM은 파싱만)
    - 반환: {"ddx_item": DDxItem, "criteria": [...]}
    """
```

### 3. EMR 대조 (self-directed, deepagents)
```python
def query_emr_index(keyword: str) -> list[str]:
    """EMR Index에서 keyword 관련 문서 ID 목록 조회"""

def load_emr_doc(doc_id: str) -> str:
    """특정 EMR 문서 원문 로드"""

def verify_with_emr(state: dict) -> dict:
    """
    - deepagents subagent 생성: tools=[query_emr_index, load_emr_doc], response_format=VerificationResult
    - ReAct 루프: Thought(다음에 확인할 기준/문서) → Action(EMR 조회) → Observation(찾음/못찾음) 반복
    - criteria 각 항목을 supported/refuted/unconfirmed로 판정
    - 애매하거나 근거 부족 시 스스로 추가 EMR 문서 조회 (recursion_limit으로 상한)
    - Reflect: 전 항목 판정 완료 시 종료, 미완료면 반복 지속(상한까지)
    - 이 함수는 status/WorkupGap을 직접 결정하지 않음 — 판정 결과(VerificationResult)만 반환
    - 반환: {"verification_result": VerificationResult}
    """
    # Code 예시 
    ddx_item = state["ddx_item"]
    criteria = state["criteria"]

    verifier = create_deep_agent(
        model=VERIFIER_MODEL,
        tools=[query_emr_index, load_emr_doc],
        system_prompt=f"'{ddx_item.diagnosis_name}'의 아래 진단기준을 EMR과 대조해 판정하라: {criteria}",
        response_format=VerificationResult,
    )
    result = verifier.invoke(
        {"messages": [{"role": "user", "content": "EMR을 조회해 각 기준을 판정하라"}]},
        config={"recursion_limit": 5},   # 이 서브에이전트만의 한도 (외부 그래프의 recursion_limit과 별개)
    )
    return {"verification_result": result["structured_response"]}
```

### 4. 최종 판정 (deterministic, 분리됨)
```python
def determine_status(state: dict) -> dict:
    """
    - verification_result를 다음 규칙으로 status 변환:
      refuted 1개 이상 → "declined"
      전 항목 supported → "supported"
      그 외(unconfirmed 존재, refuted 없음) → "pending"
    - ddx_item.evidence를 supported/refuted 항목의 (date, source_doc, content)로 갱신
    - 반환: {"refined_ddx": [DDxItem]}
    """
```

### 5. Workup Gap 기록 (deterministic, 분리됨)
```python
def build_workup_gap(state: dict) -> dict:
    """
    - verification_result 중 judgement == "unconfirmed"인 항목마다 WorkupGap 생성
      missing_item = criterion, recommended_action = 해당 항목 확인에 필요한 검사/기록 제안
    - 반환: {"workup_gaps": [WorkupGap]}
    """
```

### 6. 취합
```python
def aggregate(state: Module3State) -> dict:
    """
    - refined_ddx, workup_gaps 취합
    - 반환: {"final_ddx_list": [...], "final_gap_list": [...]}
    - dict의 각 key 값의 내용이 없다라도 빈 list 반환해야
    """
```

## 그래프 조립
```python
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

graph = StateGraph(Module3State)
graph.add_node("merge_prelim_ddx", merge_prelim_ddx)
graph.add_node("fetch_criteria", fetch_criteria)
graph.add_node("verify_with_emr", verify_with_emr)
graph.add_node("determine_status", determine_status)
graph.add_node("build_workup_gap", build_workup_gap)
graph.add_node("aggregate", aggregate)

graph.add_edge(START, "merge_prelim_ddx")
graph.add_conditional_edges("merge_prelim_ddx", route_to_verification, ["fetch_criteria"])
graph.add_edge("fetch_criteria", "verify_with_emr")
graph.add_edge("verify_with_emr", "determine_status")
graph.add_edge("verify_with_emr", "build_workup_gap")
graph.add_edge("determine_status", "aggregate")
graph.add_edge("build_workup_gap", "aggregate")
graph.add_edge("aggregate", END)

module3_app = graph.compile()
```

## 출력
`schema_examples.md` 파일의 Module 3 출력예시 참조

## 설계상 열린 이슈
1. StatPearls: 진단명 기반 검색 가능한 엔드포인트 미확보 (스크래핑 vs MCP 여부 포함) — 확보 전까지 2개 사이트만 사용
2. MedlinePlus/Merck 중 우선 Merch 검색 후 없을 경우 MedlinePlus 
3. `determine_status`의 "refuted 1개 이상 → declined" 규칙은 첫 제안안 — MD 자문으로 검증 필요
4. `verify_with_emr`의 `recursion_limit` 구체값 현재 5개로 해도 충분할까? 
5. `merge_prelim_ddx`의 dedup이 문자열 완전일치에 의존 — BioLORD가 threshold 없이 강제매칭하므로, 오매칭된 진단명이 있으면 dedup도 함께 틀어질 수 있음, MONDO를 쓰기 때문에 합칠 때 문제 없을 가능성 높음 

---
*근거: Module 3 개요, merge_prelim_ddx 추가, verify_with_emr 3분할(판정/상태결정/Gap기록), source_detail 병기 방식 반영*
