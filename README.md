# map-ddx-agents

MAP(Multidisciplinary Assessment & Planning) 진단 보조 AI Agent 파이프라인.
Module 1~3을 LangGraph+deepagents 기반 deterministic/self-directed 두 방식으로 각각 구현 예정.

현재 설계 단계 (`spec_docs` 참고) — 코드 구현 진행 중

#### How to run module 1 workflow
`uv run python -m modules.module1_deterministic.run PT09` or `./run_module1.sh PT09`

최초 실행 시 `data_prep/mondo_diseases.csv`(32,095건)를 BioLORD로 임베딩해
`embeddings/mondo_biolord.npy`에 캐시하므로 수 분이 걸리며, 이후 실행은 캐시를 로딩한다.

#### How to run module 2 workflow 
`uv run python -m modules.module2_deterministic.run --patient PT09` or `./run_module2.sh PT09`
