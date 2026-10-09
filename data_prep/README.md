1. https://mondo.monarchinitiative.org/pages/download 에서 mondo.json 다운로드
2. `python extract_mondo_id_disease.py` 실행 → `mondo_diseases.csv` 생성

#### StatPearls (Module 3 진단기준 fallback)
3. NLM LitArch FTP에서 StatPearls 아카이브 다운로드 (약 2GB) — Bookshelf가 자동 수집에 허용하는 유일한 경로
   `curl -L -o statpearls/statpearls_NBK430685.tar.gz https://ftp.ncbi.nlm.nih.gov/pub/litarch/3d/12/statpearls_NBK430685.tar.gz`
4. `python build_statpearls_index.py` 실행 → `statpearls/statpearls.sqlite` 생성 (약 30초)
