# Module 1. Vignette 기반 Prelim. DDx 생성 — Code Design Specification


                         ┌──────────────────────┐
                         │ Vignette | 문제 목록 │
                         └──────────┬───────────┘
                                    ▼
                      ┌───────────────────────────┐
                      │   recruit_specialists     │
                      │   (전문과 5개 결정)       │
                      └──────────┬────────────────┘
                                 ▼
                        Send × 5 (병렬 fan-out)
           ┌───────────┬───────────┬───────────┬───────────┐
           ▼           ▼           ▼           ▼           ▼
     ┌──────────┐┌──────────┐┌──────────┐┌──────────┐┌──────────┐
     │Dept.1    ││Dept.2    ││Dept.3    ││Dept.4    ││Dept.5    │
     │Persona   ││Persona   ││Persona   ││Persona   ││Persona   │
     │→ Top 3   ││→ Top 3   ││→ Top 3   ││→ Top 3   ││→ Top 3   │
     │  DDx     ││  DDx     ││  DDx     ││  DDx     ││  DDx     │
     └────┬─────┘└────┬─────┘└────┬─────┘└────┬─────┘└────┬─────┘
          └───────────┴───────────┴───────────┴───────────┘
                                 ▼
                      ┌───────────────────────────┐
                      │     aggregate_union       │
                      │ (정규화 + 합집합 + 저장)  │
                      └──────────┬────────────────┘
                                 ▼
                      Top-N Prelim. DDx with Evid.
                       (module1_prelim_ddx.json)


## 프레임워크 선택 근거
- **LangGraph**: "5개 과 fan-out → 병렬 실행 → 병합" 흐름은 실행 경로가 정해진 결정론적 파이프라인. `Send` API로 recruit 단계에서 정해진 N개 과에 **빠짐없이** 분기시킴 (LLM이 task를 몇 번 호출할지 스스로 판단하게 두면 일부 과 호출 누락 위험 → Send가 이를 구조적으로 방지).

## 아키텍처
```
START → recruit_specialists → (Send × 5, 병렬) → specialist_ddx → aggregate_union → END
```

## State 정의
```python
from typing import Annotated
from pydantic import BaseModel
import operator
from schema import DDxItem, Evidence  # 공통 스키마
from langgraph.types import Send

class RecruitedDepartments(BaseModel):
    departments: list[str]      # 정확히 5개

class SpecialistTop3(BaseModel):
    ddx_list: list[DDxItem]     # 정확히 3개

class Module1State(dict):
    vignette: str
    departments: list[str]
    specialist_outputs: Annotated[list[DDxItem], operator.add]  # 병렬 결과 자동 병합
    final_ddx_list: list[DDxItem]
    output_path: str
```


## Nodes
```python

SPECIALIST_MODEL = "gemma-4"      # deterministic 버전과 동일 (비교실험 변수 통제)
ORCHESTRATOR_MODEL = "gemma-4"

def recruit_specialists(state: Module1State) -> dict:
    """
    - Vignette 검토 후 소집할 전문과 5개 결정 (구조화출력: RecruitedDepartments)
    - 제약: 최소 1개는 희귀/비주류과, 동일 계열 3개 이상 금지
    - 반환: {"departments": [...]}
    """

def route_to_specialists(state: Module1State) -> list[Send]:
    """departments 각 항목마다 Send("specialist_ddx", {...}) 생성 (병렬 fan-out)"""

def specialist_ddx(state: dict) -> dict:
    """
    - 근거는 Vignette or "문제 목록" 문구 인용, source_module/source_detail은 코드가 사후 기입
    - 반환: {"specialist_outputs": [DDxItem, ...]}
    """

def aggregate_union(state: Module1State) -> dict:
    """
    - 진단명 표준화 (diagnosis normalization)은 BioLORD BERT 모델 이용
    - 동일 진단 복수 과 지목 시 evidence 병합, source_detail을 "Hematology, Rheumatology, ..." 형태로 결합
    - JSON 저장 후 반환: {"final_ddx_list": [...], "output_path": str}
    """
```
- `aggregate_union` 함수에서 진단명 표준화는 BioLORD 모델로 이용[^1][^2].
- `specialist_ddx`에서 추출된 질병명을 BioLORD 모델이 비교할 대상은 일단 mondo diseases[^3], 최초 시도시 embedding 후 file로 저장 후 재사용시 loading 할 것


## 그래프 조립
```python
from langgraph.graph import StateGraph, START, END

graph = StateGraph(Module1State)
graph.add_node("recruit_specialists", recruit_specialists)
graph.add_node("specialist_ddx", specialist_ddx)
graph.add_node("aggregate_union", aggregate_union)

graph.add_edge(START, "recruit_specialists")
graph.add_conditional_edges("recruit_specialists", route_to_specialists, ["specialist_ddx"])
graph.add_edge("specialist_ddx", "aggregate_union")
graph.add_edge("aggregate_union", END)

module1_app = graph.compile()
```

## 출력: schema.md 파일의 Module 1 출력예시 참조 


## 설계상 열린 이슈 (논의 필요)
1. BioLORD 질병명 json에서 matching시 문턱값이 없으면 부정확한 질병명 매칭도 인정없이 넘어간다는 의미, 문턱값이 얼마 이상이면 부정확한 매칭이 x% 미만으로 떨어지는 실험을 할 수 있을까? 
2. System prompt의 전문의 Persona만으로 vignette을 보고 차별화된 감별진단 목록을 생성하는지 확인 필요

---
*근거: Module 1 개요 v2(MD 자문 반영), 대화 중 확정된 recruit→persona→union 알고리즘*
[^1]: `../misc/usage_BioLORD.py` UMLS/SNOMED CT 라이선스 유의 
[^2]: `https://github.com/MAGIC-AI4Med/DeepRare`의 utils.py의 `get_disease_embeddings` 함수 참조 & diagnosis.py의 `get_orphanet_id_from_disease` 함수 line 77 ~ 103 참조
[^3]: `https://mondo.monarchinitiative.org/pages/download`에서 mondo.json download 후 질병명만 추출 `mondo_diseases_v2.json` 
