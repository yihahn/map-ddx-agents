from typing import Literal, Optional
from pydantic import BaseModel


class Evidence(BaseModel):
    criterion: Optional[str] = None    # Module3에서만 채움 (이 근거가 대조한 진단기준)
    content: str                       # 원문 인용문구 또는 검색식 구성 소견
    supports: bool
    date: Optional[str] = None         # Module3에서만 채움 (의무기록 작성일)
    source_doc: Optional[str] = None   # Module3에서만 채움 (의무기록 서식명)


class DDxItem(BaseModel):
    diagnosis_name: str
    source_detail: str                 # Module1: 전문과 | Module2: PubMed 검색식, 복수 출처 병합 시 " | "로 결합 예), "Module1: Rheumatology, Hematology | Module2: (query...)"
    status: Literal["supported", "declined", "pending"] = "pending"
    evidence: list[Evidence]


class WorkupGap(BaseModel):
    diagnosis_name: str
    criterion: Optional[str] = None    # 이 gap을 만든, 확인되지 않은 진단기준
    missing_item: str
    recommended_action: str
