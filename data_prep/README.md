1. https://mondo.monarchinitiative.org/pages/download 에서 mondo.json 다운로드
2. `python extract_mondo_id_disease.py` 실행 → `mondo_diseases.csv` 생성
3. SNOMED CT International RF2 release zip (`SnomedCT_InternationalRF2_PRODUCTION_20260901T120000Z.zip`)을 이 디렉토리에 둔다 — 라이선스 콘텐츠이므로 zip과 파생 CSV는 git에 올리지 않는다 (`.gitignore`)
4. `python extract_snomed_disorders.py` 실행 → `snomed_concepts.csv`(disorder/finding 개념), `snomed_descriptions.csv`(US English 용어), `snomed_isa.csv`(inferred is_a), `snomed_inactive_terms.csv`(비활성 disorder/finding 개념의 용어 + 비활성화 사유 + 이력 연결) 생성. SAME AS / REPLACED BY 대상이 하나뿐인 비활성 용어는 `snomed_descriptions.csv`에 `HISTORICAL` 동의어로도 들어간다
