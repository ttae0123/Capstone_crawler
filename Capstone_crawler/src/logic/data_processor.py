import pandas as pd
import os
import re


def extract_brand_family(text):
    text = str(text).lower()

    families = set()

    mapping = {
        "intel": ["intel", "인텔"],
        "amd": ["amd", "라이젠", "애슬론", "스레드리퍼"],
        "xeon": ["xeon", "제온"],
        "ryzen": ["ryzen", "라이젠"],
        "core": ["core", "코어"],
        "pentium": ["pentium", "펜티엄"],
        "celeron": ["celeron", "셀러론"],
        "opteron": ["opteron"],
        "athlon": ["athlon", "애슬론"],
    }

    for key, words in mapping.items():
        if any(w in text for w in words):
            families.add(key)

    return families


def extract_model_info(text):
    if pd.isna(text):
        return {
            "tokens": set(),
            "numbers": set(),
            "suffix": set(),
            "families": set()
        }

    original_text = str(text).lower()
    text = original_text

    noise_words = [
        '삼성전자', '마이크로닉스', '이엠텍', 'asus', 'msi', 'gigabyte', 'asrock', 'zotac',
        'galaxy', '정품', '멀티팩', '대원cts', '제이씨현', '피씨디렉트', '코잇',
        '인텍앤컴퍼니', '박스', '벌크', 'edition', 'colorful', '그래니트', '라파엘', '세잔',
        '애로우레이크', '릿지', '랩터레이크', '리프레시', '하이퍼프로져', '대원씨티에스', '트라이프로져4',
        '게이밍', '벤투스', '뱅가드'
    ]

    # 긴 접미사를 먼저 검사해야 함
    # 예: 14900KF가 f로 먼저 잡히는 문제 방지
    suffix_list = ['super', 'kf', 'xt', 'ti', 'k', 'f', 'x']

    # 브랜드/제품군은 한글이 필요하므로 original_text에서 먼저 추출
    families = extract_brand_family(original_text)

    # 모델명 토큰 비교용 텍스트는 기존처럼 영문/숫자 중심으로 정제
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

        nums = re.findall(r'\d+', w)
        numbers.update(nums)

        for s in suffix_list:
            if w.endswith(s):
                suffix.add(s)
                break

    return {
        "tokens": tokens,
        "numbers": numbers,
        "suffix": suffix,
        "families": families
    }


def calc_match_score(d, b):
    num_inter = d["numbers"] & b["numbers"]

    if not num_inter:
        return -1

    # 브랜드/제품군이 둘 다 존재하는데 겹치는 것이 없으면 오매칭으로 판단
    # 예: Intel Xeon 6330H ↔ AMD FX-6330 방지
    if d["families"] and b["families"]:
        if d["families"].isdisjoint(b["families"]):
            return -1

    inter = d["tokens"] & b["tokens"]
    union = d["tokens"] | b["tokens"]

    if not union:
        return -1

    jaccard = len(inter) / len(union)

    suffix_score = len(d["suffix"] & b["suffix"])

    score = (
        len(num_inter) * 5 +
        jaccard * 3 +
        suffix_score * 2
    )

    return score


def match_data(part_type):
    print(f"\n>>> {part_type} 매칭 프로세스 가동...")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
    data_dir = os.path.join(project_root, "data")

    df_danawa = pd.read_csv(os.path.join(data_dir, f"data_{part_type}.csv"))
    df_bench = pd.read_csv(os.path.join(data_dir, f"total_bench_{part_type}.csv"))

    name_col = next(
        (c for c in ['name', 'product_name', 'title', '제품명'] if c in df_danawa.columns),
        None
    )

    if name_col is None:
        print("제품명 컬럼 없음")
        return

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

            if score < 3:
                continue

            if score > best_score:
                best_score = score
                best_bench = b_item["score"]

        d_row["bench_score"] = best_bench
        results.append(d_row)

    df_result = pd.DataFrame(results)

    match_rate = (df_result['bench_score'].notna().sum() / len(df_result)) * 100

    output_dir = os.path.join(data_dir, "result")
    os.makedirs(output_dir, exist_ok=True)

    save_path = os.path.join(output_dir, f"integrated_{part_type}.csv")
    df_result.to_csv(save_path, index=False, encoding="utf-8-sig")

    print(f"\n {part_type} 통합 완료: 최종 매칭률 {match_rate:.1f}%")


if __name__ == "__main__":
    for part in ["CPU", "GPU", "SSD", "RAM"]:
        match_data(part)