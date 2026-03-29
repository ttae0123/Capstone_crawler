import pandas as pd
import os
import re

def extract_model_keywords(text):
    if pd.isna(text): return set()
    text = str(text).lower()

    # 1. 제거 단어 (유통사, 패키지 형태 등)
    noise_words = [
        '삼성전자', '마이크로닉스', '이엠텍', 'asus', 'msi', 'gigabyte', 'asrock', 'zotac', 'galaxy',
        '정품', '멀티팩', '대원cts', '제이씨현', '피씨디렉트', '코잇', '인텍앤컴퍼니', '박스', '벌크',
        'lhr', 'v2', 'v3', 'oc', '에디션', 'edition', 'pny', 'palit', 'inno3d', '컬러풀', 'colorful'
    ]

    # 2. 괄호 내용 제거
    text = re.sub(r'\(.*?\)', ' ', text)
    # 3. 특수문자 제거 (단, 모델명에 쓰이는 '-'나 '.'은 공백으로 변경)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)

    words = text.split()
    # 4. 단어 제거 및 핵심 키워드(숫자 포함된 단어 등) 추출
    core_keywords = set()
    for w in words:
        if w not in noise_words and len(w) >= 2:
            core_keywords.add(w)

    return core_keywords

#데이터 연결
def match_data(part_type):
    print(f"\n>>> {part_type} 초정밀 매칭 프로세스 가동...")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
    data_dir = os.path.join(project_root, "data")

    df_danawa = pd.read_csv(os.path.join(data_dir, f"data_{part_type}.csv"))
    df_bench = pd.read_csv(os.path.join(data_dir, f"total_bench_{part_type}.csv"))

    name_col = next((c for c in ['name', 'product_name', 'title', '제품명'] if c in df_danawa.columns), None)

    # 벤치마크 데이터 키워드 최적화
    bench_list = []
    for _, row in df_bench.iterrows():
        bench_list.append({
            'score': row['Score'],
            'keys': extract_model_keywords(row['Bench_Name']),
            'orig_name': row['Bench_Name']
        })

    results = []
    for _, d_row in df_danawa.iterrows():
        d_keys = extract_model_keywords(d_row[name_col])
        best_match_score = None
        max_score = -1

        for b_item in bench_list:
            if not b_item['keys'] or not d_keys: continue

            # 교집합 계산
            intersection = d_keys.intersection(b_item['keys'])
            match_score = len(intersection)

            # [핵심 로직] 교집합이 클수록, 그리고 벤치마크 키워드를 더 많이 포함할수록 가점
            if match_score > 0:
                # 벤치마크 핵심 키워드가 다나와에 모두 포함되어 있는지 확인
                if b_item['keys'].issubset(d_keys):
                    match_score += 10 # 완전 포함 시 강력 가점

                if match_score > max_score:
                    max_score = match_score
                    best_match_score = b_item['score']

        d_row['bench_score'] = best_match_score
        results.append(d_row)

    df_result = pd.DataFrame(results)
    match_rate = (df_result['bench_score'].notna().sum() / len(df_result)) * 100
    df_result.to_csv(os.path.join(data_dir, f"integrated_old_{part_type}.csv"), index=False, encoding="utf-8-sig")

    print(f"\n {part_type} 통합 완료: 최종 매칭률 {match_rate:.1f}%")

if __name__ == "__main__":
    for part in ["CPU", "GPU", "SSD", "RAM"]:
        match_data(part)