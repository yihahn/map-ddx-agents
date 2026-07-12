import json
import networkx as nx
import matplotlib.pyplot as plt
from collections import deque


def extract_mondo_nhop_relations(json_file_path, target_node_id, max_hops=2, output_json_path=None):
    """
    특정 MONDO ID를 기준으로 N-hop(max_hops) 범위 내의 인접 노드 및 관계를 계층별로 추출합니다.
    """
    # 1. JSON 파일 로드
    with open(json_file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    graph_data = data['graphs'][0]

    # 빠른 인덱싱을 위한 딕셔너리 빌드 [cite: 6, 7]
    node_dict = {}
    actual_target_uri = None

    for node in graph_data.get('nodes', []):
        n_id = node.get('id', '')
        node_dict[n_id] = node
        if target_node_id in n_id or target_node_id.replace(":", "_") in n_id:
            actual_target_uri = n_id

    if not actual_target_uri:
        print(f"Error: '{target_node_id}'를 데이터에서 찾을 수 없습니다.")
        return None

    # 축약 ID 변환용 헬퍼 함수
    def get_short_id(uri):
        return uri.split('/')[-1].replace('_', ':') if '/' in uri else uri

    # 2. 인접 리스트(Adjacency List) 구축 (방향 무시하고 양방향 탐색 가능하도록 그래프화) [cite: 28]
    # 각 노드별로 연결된 에지(상위/하위 관계)를 모아둡니다.
    adj_list = {}
    for edge in graph_data.get('edges', []):
        sub = edge.get('sub')
        obj = edge.get('obj')
        pred = edge.get('pred')

        if sub not in adj_list: adj_list[sub] = []
        if obj not in adj_list: adj_list[obj] = []

        # sub -> obj는 상위 방향(is_a), obj -> sub는 하위 방향
        adj_list[sub].append({"neighbor": obj, "direction": "parent", "predicate": pred})
        adj_list[obj].append({"neighbor": sub, "direction": "child", "predicate": pred})

    # 3. BFS(너비 우선 탐색)를 이용한 N-hop 탐색
    visited = {actual_target_uri}
    queue = deque([(actual_target_uri, 0)]) # (현재 노드 URI, 현재 hop 수)

    # 결과를 담을 구조 (hop 별로 분류)
    hop_results = {str(h): [] for h in range(1, max_hops + 1)}

    while queue:
        current_uri, current_hop = queue.popleft()

        # 지정한 최대 hop에 도달하면 더 이상 이웃을 탐색하지 않음
        if current_hop >= max_hops:
            continue

        # 현재 노드의 이웃(1-hop) 조사
        neighbors = adj_list.get(current_uri, [])
        for n_info in neighbors:
            neighbor_uri = n_info["neighbor"]

            # 이미 방문한 노드는 스킵 (순환 참조 방지 및 최단 거리 보장)
            if neighbor_uri not in visited:
                visited.add(neighbor_uri)
                queue.append((neighbor_uri, current_hop + 1))

                # 결과 데이터 구성
                neighbor_node = node_dict.get(neighbor_uri, {})
                relation_data = {
                    "id": get_short_id(neighbor_uri),
                    "label": neighbor_node.get("lbl", "Unknown"),
                    "direction_from_parent_node": n_info["direction"], # 기점 기준 상위(parent)인지 하위(child)인지
                    "predicate": n_info["predicate"],
                    "from_node_id": get_short_id(current_uri) # 어떤 노드를 거쳐서 연결되었는지 추적
                }

                # 해당 hop 리스트에 추가
                hop_results[str(current_hop + 1)].append(relation_data)

    # 4. 최종 출력 구조화 (기준 노드 메타데이터 포함) [cite: 12]
    target_node_info = node_dict[actual_target_uri]
    synonyms = []
    if "meta" in target_node_info and "synonyms" in target_node_info["meta"]:
        for syn in target_node_info["meta"]["synonyms"]:
            synonyms.append({"pred": syn.get("pred", ""), "value": syn.get("val", "")})

    final_output = {
        "target_id": target_node_id,
        "label": target_node_info.get("lbl", ""),
        "synonyms": synonyms,
        "max_hops_searched": max_hops,
        "hop_ordered_relations": hop_results
    }

    # 파일 저장
    if output_json_path:
        with open(output_json_path, 'w', encoding='utf-8') as out_f:
            json.dump(final_output, out_f, indent=2, ensure_ascii=False)
        print(f"N-hop 추출 완료! 파일 저장됨: {output_json_path}")

    return final_output



def draw_mondo_subgraph(json_file_path, start_node_id, max_hops=2, save_figure=False):

    """
    MONDO start_node_id로부터 max_hops까지의 node와 edge를 그리는 함수
    """

    # 1. JSON 파일 로드
    with open(json_file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # OBOGraph JSON 구조 내부의 graphs 참조 (첫 번째 그래프 선택)
    graph_data = data['graphs'][0]
    
    # 2. NetworkX 전체 그래프 생성
    full_graph = nx.DiGraph()  # 방향성 그래프 객체 생성
    
    # 노드 정보 추가 및 메타데이터(라벨) 매핑용 딕셔너리 구축
    node_labels = {}
    for node in graph_data.get('nodes', []):
        node_id = node.get('id')
        # JSON 내 URI 형태(예: http://.../MONDO_0005002) 또는 축약형 ID 모두 대응 가능하도록 처리
        label = node.get('lbl', node_id)
        
        full_graph.add_node(node_id, label=label)
        node_labels[node_id] = label if label else node_id

    # 에지(관계) 정보 추가
    for edge in graph_data.get('edges', []):
        sub = edge.get('sub')   # 출발 노드 (하위 개념)
        obj = edge.get('obj')   # 도착 노드 (상위 개념)
        pred = edge.get('pred') # 관계 종류 (예: is_a)
        
        # 그래프에 에지와 관계 속성 추가
        full_graph.add_edge(sub, obj, predicate=pred)

    # 3. 입력된 시작 노드의 ID 포맷 확인 및 변환
    # JSON 파일 내에서 ID가 풀 URI(http://purl.obolibrary.org/obo/MONDO_0005002) 형태로 되어 있는지 확인
    actual_start_node = None
    for n in full_graph.nodes():
        if start_node_id in n or start_node_id.replace(":", "_") in n:
            actual_start_node = n
            break
            
    if not actual_start_node or actual_start_node not in full_graph:
        print(f"Error: 시작 노드 '{start_node_id}'를 그래프 데이터 내에서 찾을 수 없습니다.")
        return

    # 4. 방향을 무시하고(주변 상하위 노드 모두 탐색) 3-edge 이내의 이웃 노드 추출
    undirected_graph = full_graph.to_undirected()
    try:
        # 단일 출발점으로부터 각 노드까지의 최단 거리를 계산
        lengths = nx.single_source_shortest_path_length(undirected_graph, actual_start_node, cutoff=max_hops)
        subgraph_nodes = list(lengths.keys())
    except nx.NetworkXNoPath:
        print(f"노드 {start_node_id} 주변에 연결된 다른 노드가 없습니다.")
        return

    # 5. 추출된 노드들로 서브그래프(Subgraph) 구축
    subgraph = full_graph.subgraph(subgraph_nodes)
    
    # 6. 시각화 (Matplotlib)
    plt.figure(figsize=(12, 10))
    
    # 레이아웃 결정 (노드들을 보기 좋게 배치)
    pos = nx.spring_layout(subgraph, k=0.5, seed=42)
    
    # 서브그래프 내의 라벨 매핑 (URI 대신 인간이 읽을 수 있는 질병명 사용)
    sub_labels = {node: node_labels.get(node, node) for node in subgraph.nodes()}
    
    # 시작 노드와 이웃 노드의 색상을 다르게 지정
    node_colors = ['#FF5733' if node == actual_start_node else '#33A2FF' for node in subgraph.nodes()]

    # 노드와 텍스트 그리기
    nx.draw_networkx_nodes(subgraph, pos, node_size=2000, node_color=node_colors, alpha=0.9)
    nx.draw_networkx_labels(subgraph, pos, labels=sub_labels, font_size=9, font_family='sans-serif')
    
    # 에지 그리기
    nx.draw_networkx_edges(subgraph, pos, arrowstyle="->", arrowsize=15, edge_color="gray", width=1.5)
    
    # 에지 종류(예: is_a) 라벨 추가
    edge_labels = {(u, v): d['predicate'] for u, v, d in subgraph.edges(data=True)}
    nx.draw_networkx_edge_labels(subgraph, pos, edge_labels=edge_labels, font_size=8, font_color='red')

    plt.title(f"Mondo Knowledge Subgraph (Up to {max_hops} edges from {start_node_id})", fontsize=14, fontweight='bold')
    plt.axis('off')
    plt.tight_layout()
    # --- 수정된 시각화 출력/저장 제어 부분 ---
    if save_figure:
        # 파일명에서 특수문자(:)를 언더바(_)로 치환하여 안정적인 파일명 생성
        safe_node_id = start_node_id.replace(":", "_")
        output_filename = f"mondo_subgraph_{safe_node_id}_{max_hops}hop.png"
        
        plt.savefig(output_filename, dpi=300, bbox_inches='tight')
        plt.close() # 메모리 해제
        print(f"그래프 이미지가 성공적으로 저장되었습니다: {output_filename}")
    else:
        plt.show()


# --- 실행 부분 ---
# 실제 파일 경로에 맞게 수정하여 사용하세요.
if __name__ == "__main__":

     # 원하는 hop 수(예: 3-hop)를 넣어 
    start_node_id = "MONDO:0005002"
    json_file_path = "mondo.json"
    hops = 1 
    
    result = extract_mondo_nhop_relations(
        json_file_path=json_file_path,
        target_node_id=start_node_id, 
        max_hops=hops, 
        output_json_path=f"mondo_{hops}hop_result.json"
    )

    # draw mondo subgraph from start_node_id 
    draw_mondo_subgraph(
        json_file_path=json_file_path, 
        start_node_id = start_node_id, 
        max_hops=hops, 
        save_figure=True
    )


