# Interface Schema (Module 1/2/3 출력 예시)
날짜: 2026-07-07

## Module 1 출력 예시
```json
{
  "diagnosis_name": "투석관련 아밀로이드증 (β2-microglobulin amyloidosis)",
  "source_detail": "Module1: Rheumatology, Hematology",  
  "status": "supported",
  "evidence": [
    { "content": "CAPD 12년", "supports": true },
    { "content": "CK 5343↑↑ / Myoglobin 8626↑↑", "supports": true },
  ]
}
```
| 필드 | 설명 |
|---|---|
|`source_detail`|Module1: Persona 전문의 과, 복수일 경우 ", "로 concat|
|`status`|supported, declined, pending, 3가지 중 하나의 값으로|
|`content of evidence`|vignette or problem list에 있는 문구 그대로 인용|


## Module 2 출력 예시
```json
{
  "diagnosis_name": "Hypothyroidism",
  "source_detail": "Moduel2: ((Rhabdomyolysis) AND (Hyponatremia)) AND case reports[Publication Type]",
  "status": "pending",
  "evidence": [
    { "content": "Rhabdomyolysis, hyponatremia", "supports": true }
  ]
}
```
| 필드 | 설명 |
|---|---|
|`source_detail`|Module2: PubMed 검색식, 복수 검색식에서 추출되었을 경우 "," 연결|
|`content of evidence`|PubMed 검색식 구성 소견, 복수 검색식에서 추출되었을 경우 { "content": "검색어1, 검색어2", "supports": ..}로 연결|


## Module 3, Module 1 & 2 병합 결과 (동일진단이 양쪽에서 나온 경우)
```json
{
  "diagnosis_name": "Hypothyroidism",
  "source_detail": "Module1: Endocrinology | Module2: ((Rhabdomyolysis) AND (Hyponatremia)) AND case report",
  "status": "pending",
  "evidence": [
    { "content": "Na 130↓", "supports": true },
    { "content": "Rhabdomyolysis, hyponatremia", "supports": true }
  ]
}
```

## Module 3 출력 스키마

### DDxItem (ReAct 검증 후 상태 갱신)
```json
{
  "diagnosis_name": "T-cell lymphoma-associated HLH",
  "source_detail": "Module1: Hematology",
  "status": "supported",
  "evidence": [
    { "date": "2024-11-08", "source_doc": "BMB(병리)", "content": "Hemophagocytosis ++", "supports": true }
  ]
}
```
| 필드 | 설명 |
|---|---|
|`source_detail`|"ModuleN: 상세" 형식, Module 복수 출처는 " | "로 결합|
|`date of evidence`|`source_doc` (의무기록서식명) 작성일시 (Module 3 단계에서 생성)|
|`source_doc of evidence`|의무기록 서식명 (Module 3 단계에서 생성)|
|`content of evidence`|근거 문구 (Module1: vignette 인용 / Module2: 검색 term / Module3: EMR 원문 인용)|


### WorkupGap
```json
{
  "diagnosis_name": "T-cell lymphoma-associated HLH",
  "missing_item": "IL-6",
  "recommended_action": "IL-6 lab"
}
```
