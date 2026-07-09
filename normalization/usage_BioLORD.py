from sentence_transformers import SentenceTransformer
import numpy as np

def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


model = SentenceTransformer("FremyCompany/BioLORD-2023")

registry = {
    "Hemophagocytic Lymphohistiocytosis": model.encode("Hemophagocytic Lymphohistiocytosis")
}

new_name = "HLH"
new_vec = model.encode(new_name)

# 코사인 유사도 계산
best_match, score = max(
    ((k, cosine_sim(new_vec, v)) for k, v in registry.items()),
    key=lambda x: x[1]
)
print(f"best_match = {best_match}")
print(f"score = {score}")
# score > 0.85 → 자동 매칭, 0.6~0.85 → 사람 검토, 그 이하 → 신규 등록
