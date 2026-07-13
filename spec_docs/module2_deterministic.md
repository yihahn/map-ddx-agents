# Module 2. Case Report 기반 신규 DDx 발굴 — Code Design Specification

```
                         ┌──────────────────────┐
                         │ Vignette + 문제 목록 │
                         └──────────┬───────────┘
                                    ▼
                      ┌───────────────────────────┐
                      │   extract_mesh_terms      │
                      │  (단일 MeSH Term 최대 10) │
                      └──────────┬────────────────┘
                                 ▼
                        Send × N (≤10, 병렬 fan-out)
           ┌───────────┬───────────┬─── ・・・ ───┐
           ▼           ▼           ▼             ▼
     ┌──────────┐┌──────────┐┌──────────┐  ┌──────────┐
     │Term 1    ││Term 2    ││Term 3    │  │Term N    │
     │검색+추출 ││검색+추출 ││검색+추출 │  │검색+추출 │
     └────┬─────┘└────┬─────┘└────┬─────┘  └────┬─────┘
          └───────────┴───────────┴── ・・・ ─────┘
                                 ▼
                      ┌───────────────────────────┐
                      │        aggregate          │
                      │   (정규화 + 중복제거)     │
                      └──────────┬────────────────┘
                                 ▼
                        Candidate DDx (신규 후보)
```

## 프레임워크 선택 근거
- **LangGraph**: term별 검색을 `Send`로 fan-out해 최대 10개 term 모두 빠짐없이 검색 (agent 자율판단 시 일부 term 누락 위험 방지)


## 아키텍처
```
START → extract_mesh_terms → (Send × 10, 병렬) → search_case_reports → aggregate_union → END
```


## State
```python
from typing import Annotated
from pydantic import BaseModel
import operator
from schema import DDxItem, Evidence
from langgraph.types import Send

class MeshTermList(BaseModel):
    terms: list[str]              # 우선순위순, 최대 10개

class CandidateDiagnoses(BaseModel):
    diagnosis_names: list[str]

class Module2State(dict):
    vignette: str
    mesh_terms: list[str]
    new_ddx: Annotated[list[DDxItem], operator.add]
    final_ddx_list: list[DDxItem]
```


## Nodes
```python
def extract_mesh_terms(state: Module2State) -> dict:
    """
    - 증상·질병명 위주 우선순위로 단일 MeSH Term 최대 10개 추출 (구조화출력: MeshTermList)
    - 반환: {"mesh_terms": [...]}
    """

def route_to_search(state: Module2State) -> list[Send]:
    """mesh_terms 각 항목마다 Send("search_case_reports", {...}) 생성 (병렬 fan-out, ≤10)"""

def search_case_reports(state: dict) -> dict:
    """
    - query = f'({term}) AND "case reports"[Publication Type]'
    - PubMed esearch(retmax=0)로 건수 조회, 500건 초과 시 LLM이 추출해 놓은 MeSH term 중 연관성이 높은 1개 추가(최대 2 term)
    - top100 pmid/title/year/journal 조회 (abstract 제외)
    - title 목록에서 신규 진단명만 LLM이 판단해 추출 (구조화출력: CandidateDiagnoses)
    - source_detail=최종 검색식, evidence.content=사용된 term(들)
    - 반환: {"new_ddx": [DDxItem, ...]}
    """

def aggregate(state: Module2State) -> dict:
    """
    - 진단명 표준화 (diagnosis normalization)은 BioLORD BERT 모델 이용
    - 동일 진단 지목 시 evidence "|"병합, source_detail을 검색식 "A|B" 형태로 결합
    - JSON 저장 후 반환: {"final_ddx_list": [...], "output_path": str}
    """
```

## 그래프 조립
```python
from langgraph.graph import StateGraph, START, END

graph = StateGraph(Module2State)
graph.add_node("extract_mesh_terms", extract_mesh_terms)
graph.add_node("search_case_reports", search_case_reports)
graph.add_node("aggregate", aggregate)

graph.add_edge(START, "extract_mesh_terms")
graph.add_conditional_edges("extract_mesh_terms", route_to_search, ["search_case_reports"])
graph.add_edge("search_case_reports", "aggregate")
graph.add_edge("aggregate", END)

module2_app = graph.compile()
```

## 출력: schema.md 파일의 Module 2 출력예시 참조

## 설계상 열린 이슈
1. count=0(검색결과 없음) 처리 방침 미정 — 스킵 vs 검색식 문법 오류 검토 vs MeSH Term 변경 (어떻게?)
2. `MeshTermList`를 어떻게 관리할지, MeSH Term의 임상중요/적절도의 rank를 어떻게 하는 게 좋을 지...
3. top100 정렬기준은 PubMed의 Best Match

---
*근거: Module 2 개요, 대화 중 확정된 term추출→검색→추출→합집합 알고리즘*
