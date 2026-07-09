import json
import csv

# 1. 파일 경로 설정
input_file = "mondo.json"
output_file = "mondo_diseases.csv"

print("데이터 추출을 시작합니다...")

# 2. JSON 파일 읽기 및 처리
with open(input_file, 'r', encoding='utf-8') as f:
    data = json.load(f)
    
    # OBO Graphs 구조에 따라 첫 번째 그래프의 nodes 접근
    nodes = data.get("graphs", [{}])[0].get("nodes", [])
    
    obsolete_count = sum(1 for n in nodes if n.get("meta", {}).get("deprecated") is True)
    print(f"obsolete_count = {obsolete_count}")

    extracted_data = []
    
    for node in nodes:
        # 노드 타입이 CLASS(질병 클래스)인 경우만 추출
#        if node.get("type") == "CLASS":
        if node.get("type") == "CLASS" and not node.get("meta", {}).get("deprecated", False):
            full_id = node.get("id", "")
            disease_name = node.get("lbl", "")
            
            # ID가 Mondo 공식 URI 포맷인지 확인 후 ID만 추출
            # 예: http://obolibrary.org -> MONDO_0004975
            if "MONDO_" in full_id:
                mondo_id = "MONDO_" + full_id.split("MONDO_")[-1]
                
                # 질병명이 존재하는 경우만 리스트에 추가
                if disease_name:
                    extracted_data.append([mondo_id, disease_name])

# 3. CSV 파일로 결과 저장
with open(output_file, 'w', encoding='utf-8', newline='') as f:
    writer = csv.writer(f)
    # 헤더 작성
    writer.writerow(["Mondo_ID", "Disease_Name"])
    # 데이터 작성
    writer.writerows(extracted_data)

print(f"추출 완료! 총 {len(extracted_data)}개의 질병 데이터가 '{output_file}'에 저장되었습니다.")
