# Module 1 (Self-directed, deepagents 완전위임) — Design Spec

## 입력자료
Clinical Vignette or 문제 목록 (Problem list)


## 제공 Tools/Skills
| Tool | 설명 |
|---|---|
| `consult_specialist(department, vignette)` | 지정 과 전문의 persona로 Top 3 DDx+근거 반환 (범용 subagent, 과 이름은 agent가 호출 시 결정) |
| `normalized_diagnosis(name)` | BioLORD 모델로 MONDO 표준질병명 매칭[^1][^2][^3]|
| `save_result(ddx_list)` | Module1 JSON 스키마로 최종 저장 |


## 목표 (`system_prompt`, 그대로 부여)
```
Vignette 또는 Problem list를 보고 감별진단 목록과 각 감별진단별 근거를
입력자료에서 찾아 덧붙여 Module 1의 JSON과 같은 형태로 출력하라.

전문과 소집 시 최소 1개는 희귀/비주류과를 포함하고, 동일 계열 과로 3개 이상 채우지 마라.
각 진단명은 normalize_diagnosis로 표준화한 뒤 저장하라.
```
(몇 개 과를 부를지·몇 번 반복할지는 명시하지 않음 → agent가 `write_todos`로 자율 계획)


## Pseudo Code

```python
from deepagents import create_deep_agent
from pydantic import BaseModel
from schemas import DDxItem, Evidence  # 공통 스키마

SPECIALIST_MODEL = "gemma-4"      # deterministic 버전과 동일 (비교실험 변수 통제)
ORCHESTRATOR_MODEL = "gemma-4"

class Module1Output(BaseModel):
    ddx_list: list[DDxItem]

def consult_specialist(department: str, vignette: str) -> str:
    """지정 과 전문의 persona로 vignette 검토, Top 3 DDx+근거 반환"""
    sub = create_deep_agent(
        model=SPECIALIST_MODEL,
        system_prompt=f"당신은 {department} 전문의입니다. Top 3 감별진단과 근거(원문 인용)를 제시하십시오.",
        response_format=Module1Output,
    )
    return sub.invoke({"messages": [{"role": "user", "content": vignette}]})

def normalize_diagnosis(name: str) -> str:
    """BioLORD 임베딩으로 mondo_diseases_v2.json 중 best-match 표준 질병명 반환 (threshold 없음)"""

def save_result(ddx_list: list[DDxItem]) -> str:
    """final_ddx_list를 Module1 JSON 스키마로 저장하고 저장 경로를 반환"""

agent = create_deep_agent(
    model=ORCHESTRATOR_MODEL,
    tools=[consult_specialist, normalize_diagnosis, save_result],
    system_prompt=GOAL_PROMPT,   # 위 3번 목표문
    response_format=Module1Output,
)

result = agent.invoke({"messages": [{"role": "user", "content": vignette}]})
```


## 출력:schema.md 파일의 Module 1 출력예시 참조 


## 설계상 검증 필요사항
- 소집 과 개수·구성이 실행마다 달라질 수 있음 (fan-out 결정론성 없음)
- 동일 vignette 반복 실행 → 소집 과 variance부터 측정 필요
- "최소 1개 희귀과" 제약이 자유서술 프롬프트뿐이라 실제 준수율 확인 필요 (deterministic은 RecruitedDepartments 구조화출력으로 강제되지만 self-directed는 강제 수단 없음)
- `normalize_diagnosis`를 agent가 매 진단마다 빠짐없이 호출하는지 확인 필요


[^1]: `misc/usage_BioLORD.py` UMLS/SNOMED CT 라이선스 유의 
[^2]: `https://github.com/MAGIC-AI4Med/DeepRare`의 utils.py의 `get_disease_embeddings` 함수 참조 & diagnosis.py의 `get_orphanet_id_from_disease` 함수 line 77 ~ 103 참조
[^3]: `https://mondo.monarchinitiative.org/pages/download`에서 mondo.json download 후 질병명만 추출 `mondo_diseases_v2.json` 
