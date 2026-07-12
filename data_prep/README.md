1. https://mondo.monarchinitiative.org/pages/download 에서 mondo.json 다운로드
2. `python extract_mondo_id_disease.py` 실행 → `mondo_diseases.csv` 생성
3. `mondo_graph.py`는 `extract_mondo_nhop_relations`와 `draw_mondo_subgraph`로 구성
    - `extract_mondo_nhop_relations`:
        - disease ID를 기준으로 지정한 N-hop 범위 내의 인접 질병 관계와 동의어 목록을 탐색하여 정형화된 딕셔너리 구조로 반환하고 JSON 파일로 저장
        - 주요 기능: 너비 우선 탐색(BFS)을 통해 각 단계(Hop)별 상하위 관계(`is_a`) 및 경로를 추적하여 구조화
    - `draw_mondo_subgraph`: disease ID로부터 네트워크 에지(Edge)를 추적하여 시각적인 하위 그래프(Subgraph)를 생성
