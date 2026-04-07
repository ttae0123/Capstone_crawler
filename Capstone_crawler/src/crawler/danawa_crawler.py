import os
import time
import re
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup


def extract_refined_spec(spec_list, category):
    res = {}
    combined_text = " / ".join(spec_list)

    def search(pattern, flags=re.I):
        return re.search(pattern, combined_text, flags)

    def findall(pattern, flags=re.I):
        return re.findall(pattern, combined_text, flags)

    def normalize_pcie(val):
        return val.upper().replace(" ", "") if val else None

    def normalize_size(val):
        if not val: return None
        val = val.upper()
        mapping = {"M-ATX": "MATX", "MICRO-ATX": "MATX", "MINI-ITX": "ITX", "E-ATX": "EATX"}
        return mapping.get(val, val.replace("-", ""))

    def pick_largest_size(vals):
        if not vals: return None
        order = {"EATX": 4, "ATX": 3, "MATX": 2, "ITX": 1}
        normalized = [normalize_size(v) for v in vals if normalize_size(v)]
        return max(normalized, key=lambda x: order.get(x, 0)) if normalized else None

    # 카테고리별 상세 로직
    if category == "CPU":
        m = search(r'소켓([a-zA-Z0-9]+)')
        res['socket_type'] = m.group(1).upper() if m else None
        m = findall(r'DDR[0-9]+')
        res['memory_type'] = m[0].upper() if m else None

    elif category == "GPU":
        m = search(r'정격파워\s*([0-9]+)W')
        res['recommended_power'] = int(m.group(1)) if m else None
        m = search(r'(PCIe[0-9.]+\s*X\s*[0-9]+)')
        res['pcie_type'] = normalize_pcie(m.group(1)) if m else None
        m = search(r'가로\(길이\)[^0-9]*([\d.]+)\s*mm')
        res['gpu_length'] = int(m.group(1)) if m else None

    elif category == "Mainboard":
        m = search(r'소켓([a-zA-Z0-9]+)')
        res['socket_type'] = m.group(1).upper() if m else None
        m = findall(r'DDR[0-9]+')
        res['memory_type'] = m[0].upper() if m else None
        m = search(r'(ATX|M-ATX|E-ATX|Mini-ITX|Micro-ATX)')
        res['size'] = normalize_size(m.group(0)) if m else None

    elif category == "RAM":
        m = findall(r'DDR[0-9]+')
        res['memory_type'] = m[0].upper() if m else None
        m = search(r'([0-9]+)MHz')
        res['memory_clock'] = int(m.group(1)) if m else None

    elif category == "SSD":
        m = search(r'(M\.2 \(2280\)|SATA3|PCIe[0-9.]+\s*x\s*[0-9]+)')
        res['interface'] = m.group(1).upper() if m else None
        m = search(r'([0-9]+(GB|TB))')
        res['capacity'] = m.group(1).upper() if m else None

    elif category == "Case":
        sizes = findall(r'(ATX|M-ATX|E-ATX|Mini-ITX|Micro-ATX)')
        res['size'] = pick_largest_size(sizes)
        m = search(r'VGA\s*길이[^0-9]*([\d.]+)\s*mm')
        res['gpu_length'] = int(m.group(1)) if m else None

    elif category == "Power":
        m = search(r'([0-9]+)W')
        res['wattage'] = int(m.group(1)) if m else None

    return res


def crawl_danawa(category_name, cate_code, total_pages=20):
    print(f"\n>>> {category_name} 수집 시작 (목표: {total_pages}페이지)")

    temp_dir = os.path.join(os.path.dirname(__file__), "temp_html", category_name)
    os.makedirs(temp_dir, exist_ok=True)

    with sync_playwright() as p:
        # headless=False로 설정하면 차단 확률이 낮아지지만 속도를 위해 True 유지 시 스텔스 설정 권장
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page_obj = context.new_page()

        for page in range(1, total_pages + 1):
            # 정렬 방식을 신상품순(newest) 혹은 판매순 등으로 섞으면 중복을 피하기 좋음
            url = f"https://prod.danawa.com/list/?cate={cate_code}&page={page}&limit=30"

            try:
                page_obj.goto(url, wait_until="domcontentloaded", timeout=60000)
                page_obj.wait_for_selector(".main_prodlist", timeout=30000)

                # 페이지 끝까지 스크롤 (다나와는 스크롤해야 상품 정보가 완전히 로드됨)
                for _ in range(5):
                    page_obj.keyboard.press("PageDown")
                    time.sleep(0.5)

                file_path = os.path.join(temp_dir, f"page_{page}.html")
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(page_obj.content())

                print(f"  [{page}/{total_pages}] 저장 완료", end='\r')
                time.sleep(random.uniform(1.5, 3.0))
            except Exception as e:
                print(f"\n  ! {page}페이지 오류: {e}")

        browser.close()

    # 2단계: 파싱 및 중복 제거
    all_data = []
    for file_name in os.listdir(temp_dir):
        if not file_name.endswith(".html"): continue
        with open(os.path.join(temp_dir, file_name), "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        # 광고 상품(.ad_prod_item)을 제외한 실제 상품 리스트만 선택
        products = soup.select("#productListArea .prod_item:not(.ad_prod_item)")

        for p in products:
            try:
                name_tag = p.select_one(".prod_name a")
                price_tag = p.select_one(".price_sect strong")
                if not name_tag or not price_tag: continue

                name = name_tag.get_text().strip()
                price = int(price_tag.get_text().replace(",", "").strip())

                spec_list = [s.get_text().strip() for s in p.select(".spec_list .view_dic")]

                item = {"name": name, "price": price}
                item.update(extract_refined_spec(spec_list, category_name))
                all_data.append(item)
            except:
                continue

    # 3단계: 데이터프레임 생성 및 사후 중복 제거
    df = pd.DataFrame(all_data)
    if not df.empty:
        # 이름이 완전히 같거나, 핵심 스펙이 겹치는 경우 제거 (최저가 우선)
        df = df.sort_values(by='price', ascending=True)
        df = df.drop_duplicates(subset=['name'], keep='first')

    save_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data")
    )
    os.makedirs(save_dir, exist_ok=True)

    output_path = os.path.join(save_dir, f"data_{category_name}.csv")
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\n★ {category_name} 완료: {len(df)}개 고유 데이터 확보")


if __name__ == "__main__":
    parts_list = [
        {"name": "CPU", "code": "112747"},
        {"name": "GPU", "code": "112753"},
        {"name": "Mainboard", "code": "112751"},
        {"name": "Power", "code": "112777"},
        {"name": "RAM", "code": "112752"},
        {"name": "SSD", "code": "112760"},
        {"name": "Case", "code": "112775"}
    ]

    for p in parts_list:
        crawl_danawa(p["name"], p["code"], total_pages=20)  # 페이지 수를 20으로 상향
        time.sleep(5)