import pandas as pd
import os
import re


# 🔥 개선된 키워드 추출
def extract_model_info(text):
    if pd.isna(text):
        return {"tokens": set(), "numbers": set(), "suffix": set()}

    text = str(text).lower()

    # 노이즈 제거
    noise_words = [
        '삼성전자', '마이크로닉스', '이엠텍', 'asus', 'msi', 'gigabyte', 'asrock', 'zotac',
        'galaxy', '정품', '멀티팩', '대원cts', '제이씨현', '피씨디렉트', '코잇',
        '인텍앤컴퍼니', '박스', '벌크', 'edition', 'colorful'
    ]

    # suffix (중요!)
    suffix_list = ['k', 'kf', 'f', 'x', 'xt', 'ti', 'super']

    # 괄호 제거
    text = re.sub(r'\(.*?\)', ' ', text)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)

    words = text.split()

    tokens = set()
    numbers = set()
    suffix = set()

    for w in words:
        if w in noise_words or len(w) < 2:
            continue

        tokens.add(w)

        # 숫자 추출
        nums = re.findall(r'\d+', w)
        numbers.update(nums)

        # suffix 체크
        for s in suffix_list:
            if w.endswith(s):
                suffix.add(s)

    return {
        "tokens": tokens,
        "numbers": numbers,
        "suffix": suffix
    }


# 🔥 개선된 매칭 함수
def calc_match_score(d, b):
    # 숫자 매칭 (핵심)
    num_inter = d["numbers"] & b["numbers"]

    if not num_inter:
        return -1  # 숫자 하나도 안 맞으면 탈락

    # 토큰 유사도 (Jaccard)
    inter = d["tokens"] & b["tokens"]
    union = d["tokens"] | b["tokens"]

    if not union:
        return -1

    jaccard = len(inter) / len(union)

    # suffix 매칭
    suffix_score = len(d["suffix"] & b["suffix"])

    # 최종 점수 (가중치)
    score = (
        len(num_inter) * 5 +      # 숫자 매우 중요
        jaccard * 3 +            # 전체 유사도
        suffix_score * 2         # suffix 보정
    )

    return score


# 🔥 메인 매칭 함수
def match_data(part_type):
    print(f"\n>>> {part_type} 초정밀 매칭 프로세스 가동...")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
    data_dir = os.path.join(project_root, "data")

    df_danawa = pd.read_csv(os.path.join(data_dir, f"data_{part_type}.csv"))
    df_bench = pd.read_csv(os.path.join(data_dir, f"total_bench_{part_type}.csv"))

    name_col = next((c for c in ['name', 'product_name', 'title', '제품명'] if c in df_danawa.columns), None)

    if name_col is None:
        print("❌ 제품명 컬럼 없음")
        return

    # 벤치 데이터 전처리
    bench_list = []
    for _, row in df_bench.iterrows():
        bench_list.append({
            "score": row["Score"],
            "info": extract_model_info(row["Bench_Name"]),
        })

    results = []

    for _, d_row in df_danawa.iterrows():
        d_info = extract_model_info(d_row[name_col])

        best_score = -1
        best_bench = None

        for b_item in bench_list:
            score = calc_match_score(d_info, b_item["info"])

            # 최소 기준
            if score < 3:
                continue

            if score > best_score:
                best_score = score
                best_bench = b_item["score"]

        d_row["bench_score"] = best_bench
        results.append(d_row)

    df_result = pd.DataFrame(results)

    match_rate = (df_result['bench_score'].notna().sum() / len(df_result)) * 100

    save_path = os.path.join(data_dir, f"integrated_{part_type}.csv")
    df_result.to_csv(save_path, index=False, encoding="utf-8-sig")

    print(f"\n {part_type} 통합 완료: 최종 매칭률 {match_rate:.1f}%")


if __name__ == "__main__":
    for part in ["CPU", "GPU", "SSD", "RAM"]:
        match_data(part)