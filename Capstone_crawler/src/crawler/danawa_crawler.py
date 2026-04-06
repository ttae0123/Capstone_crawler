import os
import time
import re
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from logic.convert_to_DB import get_db_connection

# [기존 로직 유지] 상세 스펙 추출 함수
def extract_refined_spec(spec_list, category):
    res = {}
    combined_text = " / ".join(spec_list)
    def search(pattern, flags=0): return re.search(pattern, combined_text, flags)
    def findall(pattern, flags=0): return re.findall(pattern, combined_text, flags)
    def normalize_pcie(val): return val.upper().replace(" ", "") if val else None
    def normalize_size(val):
        if not val: return None
        val = val.upper()
        mapping = {"M-ATX": "MATX", "MICRO-ATX": "MATX", "MINI-ITX": "ITX", "E-ATX": "EATX"}
        return mapping.get(val, val.replace("-", ""))
    def pick_largest_size(vals):
        if not vals: return None
        order = {"EATX": 4, "ATX": 3, "MATX": 2, "ITX": 1}
        normalized = [normalize_size(v) for v in vals if normalize_size(v)]
        if not normalized: return None
        return max(normalized, key=lambda x: order.get(x, 0))

    if category == "CPU":
        m = search(r'소켓([a-zA-Z0-9]+)')
        res['socket_type'] = m.group(1).upper() if m else None
        m = findall(r'DDR[0-9]+')
        res['memory_type'] = m[0].upper() if m else None
        res['cooler'] = False if "미포함" in combined_text else True if ("쿨러" in combined_text) else None
    elif category == "GPU":
        m = search(r'정격파워\s*([0-9]+)W')
        res['recommended_power'] = int(m.group(1)) if m else None
        m = search(r'(PCIe[0-9.]+\s*X\s*[0-9]+)', re.I)
        res['pcie_type'] = normalize_pcie(m.group(1)) if m else None
        m = search(r'가로\(길이\)[^0-9]*([\d.]+)\s*mm', re.I)
        res['gpu_length'] = int(m.group(1)) if m else None
    elif category == "Mainboard":
        m = search(r'소켓([a-zA-Z0-9]+)')
        res['socket_type'] = m.group(1).upper() if m else None
        m = findall(r'DDR[0-9]+')
        res['memory_type'] = m[0].upper() if m else None
        m = search(r'PCIe[0-9.]+\s*x\s*[0-9]+', re.I)
        res['pcie_type'] = normalize_pcie(m.group(0)) if m else None
        m = search(r'(ATX|M-ATX|E-ATX|Mini-ITX|Micro-ATX)', re.I)
        res['size'] = normalize_size(m.group(0)) if m else None
        m = search(r'([0-9]+)MHz')
        res['memory_clock'] = int(m.group(1)) if m else None
    elif category == "RAM":
        m = findall(r'DDR[0-9]+')
        res['memory_type'] = m[0].upper() if m else None
        m = search(r'([0-9]+)MHz')
        res['memory_clock'] = int(m.group(1)) if m else None
        m = search(r'램개수:\s*([0-9]+)개')
        res['count'] = int(m.group(1)) if m else 1
    elif category == "Case":
        sizes = findall(r'(ATX|M-ATX|E-ATX|Mini-ITX|Micro-ATX)', re.I)
        res['size'] = pick_largest_size(sizes)
        m = search(r'VGA\s*길이[^0-9]*([\d.]+)\s*mm', re.I)
        res['gpu_length'] = int(m.group(1)) if m else None
        m = search(r'CPU쿨러\s*높이[^0-9]*([\d.]+)\s*mm', re.I)
        res['cooler_length'] = int(m.group(1)) if m else None
    elif category == "Power":
        m = search(r'(ATX|M-ATX|SFX)', re.I)
        res['size'] = normalize_size(m.group(0)) if m else None
        m = search(r'([0-9]+)W')
        res['wattage'] = int(m.group(1)) if m else None
    return res

# [메인 크롤링 함수]
def crawl_danawa(category_name, cate_code, total_pages):
    print(f"\n>>> {category_name} 수집 시작 (1단계: HTML 저장)")

    # 임시 저장 디렉토리 설정
    temp_dir = os.path.join(os.path.dirname(__file__), "temp_html", category_name)
    if not os.path.exists(temp_dir): os.makedirs(temp_dir)

    with sync_playwright() as p:
        # 브라우저 실행
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale="ko-KR"
        )
        page_obj = context.new_page()

        # [1단계] HTML 소스 확보
        for page in range(1, total_pages + 1):
            url = f"https://prod.danawa.com/list/?cate={cate_code}&page={page}&listCategory=all&listMethod=list"
            try:
                # 네트워크 유휴 상태까지 대기 (더 안정적인 로딩)
                page_obj.goto(url, wait_until="networkidle", timeout=60000)
                page_obj.wait_for_selector(".main_prodlist", timeout=30000)

                # [중요] 동적 로딩을 위해 5단계로 나누어 스크롤 수행
                for i in range(1, 6):
                    page_obj.evaluate(f"window.scrollTo(0, (document.body.scrollHeight / 5) * {i})")
                    time.sleep(0.7)

                # HTML 저장
                file_path = os.path.join(temp_dir, f"page_{page}.html")
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(page_obj.content())
                print(f"  [{page}/{total_pages}] HTML 저장 완료")

                # 차단 방지를 위한 휴식
                time.sleep(random.uniform(2.5, 4.0))
            except Exception as e:
                print(f"  ! {page}페이지 저장 실패: {e}")

        browser.close()

    # [2단계] 로컬 HTML 분석
    print(f">>> {category_name} 분석 시작 (2단계: 데이터 추출)")
    all_data = []
    seen_names = set()

    for file_name in os.listdir(temp_dir):
        if not file_name.endswith(".html"): continue

        with open(os.path.join(temp_dir, file_name), "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        # 광고 상품 클래스(.product-pot, .ad_prod_item)를 제외한 상품 리스트만 선택
        products = soup.select("li.prod_item:not(.product-pot):not(.ad_prod_item)")

        for p in products:
            try:
                name_tag = p.select_one(".prod_name a")
                if not name_tag: continue
                raw_name = name_tag.get_text().strip()
                # 괄호 안의 부가 정보 제거 (매칭률 향상을 위함)
                clean_name = re.sub(r'\(.*?\)', '', raw_name).strip()

                # 중복 데이터 체크
                # if not clean_name or clean_name in seen_names: continue
                # seen_names.add(clean_name)

                # 가격 정보 추출
                price_tag = p.select_one(".price_sect strong")
                if not price_tag: continue
                price_val = price_tag.get_text().replace(",", "").strip()
                if not price_val.isdigit(): continue
                price = int(price_val)

                # 상세 스펙 리스트 추출
                spec_list = [s.get_text().strip() for s in p.select(".spec_list .view_dic") if s.get_text().strip()]

                if clean_name and price:
                    item = {"name": clean_name, "price": price}
                    refined_specs = extract_refined_spec(spec_list, category_name)
                    item.update(refined_specs)

                    # RAM 카테고리의 경우 데스크탑용만 필터링
                    if category_name == "RAM" and "데스크탑용" not in " ".join(spec_list):
                        continue

                    all_data.append(item)
            except Exception:
                continue

    # [3단계] 최종 저장 및 DB 연동
    save_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
    if not os.path.exists(save_dir): os.makedirs(save_dir)

    output_file = os.path.join(save_dir, f"data_{category_name}.csv")
    df_result = pd.DataFrame(all_data)

    # 최종 중복 제거 (데이터프레임 레벨)
    # df_result = df_result.drop_duplicates(subset=["name"], keep="first")
    # df_result.to_csv(output_file, index=False, encoding="utf-8-sig")

    print(f"★ {category_name} 수집 완료: 총 {len(df_result)}개 데이터 확보")

    # DB 연동 (지정된 카테고리만)
    # if category_name in ["Mainboard", "Power", "Case"]:
    #     get_db_connection(category_name, df_result)

# 수집 대상 리스트 설정
parts_list = [
    {"name": "CPU", "code": "112747"},
    {"name": "GPU", "code": "112753"},
    {"name": "Mainboard", "code": "112751"},
    {"name": "Power", "code": "112777"},
    {"name": "RAM", "code": "112752"},
    {"name": "SSD", "code": "112760"},
    {"name": "Case", "code": "112775"}
]

if __name__ == "__main__":
    for p in parts_list:
        # 각 부품별로 20페이지 수집 시도
        crawl_danawa(p["name"], p["code"], total_pages=20)
        # 차단 방지를 위해 부품 간 충분한 휴식 시간 부여
        time.sleep(random.uniform(4.0, 7.0))