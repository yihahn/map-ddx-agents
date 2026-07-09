# Module 2 (Self-directed, deepagents 완전위임) — Design Spec

## 입력자료
Clinical Vignette or 문제 목록 (Problem list)

## 제공 Tools/Skills
| Tool | 설명 |
|---|---|
| `pubmed_count(query)` | PubMed 검색 건수 조회 |
| `pubmed_top100(query)` | top100 title/year/journal 조회 (abstract 제외) |
| `normalized_diagnosis(name)` | BioLORD 모델로 MONDO 표준질병명 매칭|
| `save_result(ddx_list)` | Module2 JSON 스키마로 최종 저장 |

## 목표 (`system_prompt`, 그대로 부여)
```
Vignette 또는 Problem list를 보고 Mesh Term을 추출하여 
PubMed Case Report 검색으로 top100 title에서 진단명을 판단해 감별진단을 찾아
Module 2 JSON과 같은 형태로 출력하라.

검색 규칙:
- 검색식은 최대 2개 MeSH Term만 사용하고 항상 'AND "case reports"[Publication Type]'를 포함하라.
- 초기 검색은 단일 term으로 하되, 결과가 500건 초과하면 추출해 놓은 MeSH Term 중 연관 term 1개를 추가하라.
- 검색은 최대 10회를 넘지 마라.

진단명 작성 규칙:
- 검색된 top100 title을 보고 진단명을 판단해 추출하라.
- 진단명은 normalize_diagnosis로 표준화한 뒤 저장하라.
- source_detail은 최종 검색식, evidence.content는 검색에 사용한 term(들)로 채워라.

종료 조건:
- 계획한 검색을 모두 마쳤으면 결과를 저장하고 종료하라.
- 10회에 도달했는데도 끝내지 못했다면 "목표 달성 실패: 신규 진단 미발견"이라는
  메시지와 함께 그때까지의 결과(빈 목록일 수 있음)를 저장하고 종료하라.
```

## Pseudo Code
```python
from deepagents import create_deep_agent
from pydantic import BaseModel
from schemas import DDxItem, Evidence  # 공통 스키마

ORCHESTRATOR_MODEL = "gemma-4"   # deterministic 버전과 동일 (비교실험 변수 통제)

class Module2Output(BaseModel):
    ddx_list: list[DDxItem]
    status: str   # "success" | "실패: 신규 진단 미발견"

def pubmed_count(query: str) -> int:
    """PubMed 검색 건수 조회"""

def pubmed_top100(query: str) -> list[dict]:
    """top100 pmid/title/year/journal 조회 (abstract 제외)"""

def normalize_diagnosis(name: str) -> str:
    """BioLORD 임베딩으로 mondo_diseases_v2.json 중 best-match 표준 질병명 반환 (threshold 없음)"""

def save_result(ddx_list: list[DDxItem]) -> str:
    """ddx_list를 Module2 JSON 스키마로 저장하고 저장 경로를 반환"""


agent = create_deep_agent(
    model=ORCHESTRATOR_MODEL,
    tools=[pubmed_count, pubmed_top100, normalize_diagnosis, save_result],
    system_prompt=GOAL_PROMPT,   # 위 3번 목표문
    response_format=Module2Output,
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": f"{vignette}}]},
    config={"recursion_limit": 30},  # 프롬프트상 10회 검색 지시 + 프레임워크 레벨 안전장치 이중화
)
```

## 출력:schema.md 파일의 Module 2 출력예시 참조 


## 설계상 검증 필요사항 
- 검색 term 개수·순서가 실행마다 달라질 수 있음 (fan-out 결정론성 없음)
- "10회 도달 시 실패 메시지" 지시를 agent가 실제로 지키는지 반복 실행으로 확인 필요
- top100 title에서 진단명 추출을 agent 자체 reasoning으로 하는데, deterministic의 구조화출력(NewDiagnoses)만큼 일관적인지 확인 필요
- `normalize_diagnosis`를 매 진단마다 빠짐없이 호출하는지 확인 필요
