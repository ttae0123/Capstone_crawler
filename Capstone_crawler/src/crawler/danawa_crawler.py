import os
import time
import re
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from datetime import datetime



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



def crawl_danawa(category_name, cate_code, total_pages):
    print(f"\n>>> {category_name} 수집 시작 (목표: {total_pages}페이지)")

    temp_dir = os.path.join(os.path.dirname(__file__), "temp_html", category_name)
    os.makedirs(temp_dir, exist_ok=True)

    with sync_playwright() as p:
        # 동적 로딩을 눈으로 확인하려면 headless=False를 권장합니다.
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page_obj = context.new_page()

        # 최초 진입: URL로 접속
        start_url = f"https://prod.danawa.com/list/?cate={cate_code}"
        page_obj.goto(start_url, wait_until="networkidle")
        time.sleep(2)

        for page in range(1, total_pages + 1):
            try:
                # 1단계: 자바스크립트 함수 movePage 실행 (동적 로딩)
                page_obj.evaluate(f"movePage({page})")

                # 2단계: 페이지 번호가 'now_on'으로 바뀔 때까지 대기 (이미지의 클래스 반영)
                page_obj.wait_for_selector(f"a.num.now_on:has-text('{page}')", timeout=15000)

                # 3단계: 리스트 내용이 로드될 때까지 충분히 대기 및 스크롤
                time.sleep(2)
                for _ in range(5):
                    page_obj.keyboard.press("PageDown")
                    time.sleep(0.4)

                # 파일 저장
                file_path = os.path.join(temp_dir, f"page_{page}.html")
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(page_obj.content())

                print(f"  [{page}/{total_pages}] 저장 완료", end='\r')
                time.sleep(random.uniform(1.5, 3.0))

            except Exception as e:
                print(f"\n  ! {page}페이지 이동/로딩 오류: {e}")
                # 오류 시 재시도 혹은 다음 페이지 진행
                continue

        browser.close()

    # 2단계: 파싱 및 중복 제거
    all_data = []
    for file_name in os.listdir(temp_dir):
        if not file_name.endswith(".html"): continue
        with open(os.path.join(temp_dir, file_name), "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        products = soup.select("li[id^='productItem']:not(.ad_prod_item)")

        for p in products:
            try:
                # p_id 추출 코드 제거함
                name_tag = p.select_one(".prod_name a")
                price_tag = p.select_one(".price_sect strong")
                if not name_tag or not price_tag: continue

                name = name_tag.get_text().strip()
                price_str = re.sub(r'[^0-9]', '', price_tag.get_text())
                if not price_str: continue
                price = int(price_str)

                spec_list = [s.get_text().strip() for s in p.select(".spec_list .view_dic")]

                # item 딕셔너리에서 "id" 키를 제외함
                item = {"name": name, "price": price}
                item.update(extract_refined_spec(spec_list, category_name))
                all_data.append(item)
            except:
                continue

    df = pd.DataFrame(all_data)
    if not df.empty:
        df["created_at"] = datetime.now()
        # 중복 제거 시 'name'을 기준으로 수행 (id가 없으므로 name 기준이 가장 확실함)
        df = df.sort_values(by='price', ascending=True)
        df = df.drop_duplicates(subset=['name'], keep='first')

    base_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data")
    )

    if category_name in ["Mainboard", "Power", "Case"]:
        output_dir = os.path.join(base_dir, "result")
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = base_dir

    os.makedirs(output_dir, exist_ok=True)

    output_path = os.path.join(output_dir, f"data_{category_name}.csv")
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\n★ {category_name} 완료: {len(df)}개 고유 데이터 확보")


if __name__ == "__main__":
    parts_list = [
        {"name": "CPU", "code": "112747", "page": 18},
        {"name": "GPU", "code": "112753", "page": 20},
        {"name": "Mainboard", "code": "112751", "page": 20},
        {"name": "Power", "code": "112777", "page": 20},
        {"name": "RAM", "code": "112752", "page": 20},
        {"name": "SSD", "code": "112760", "page": 20},
        {"name": "Case", "code": "112775", "page": 20}
    ]

    for p in parts_list:
        crawl_danawa(p["name"], p["code"], p["page"])
        # 카테고리 간 충분한 휴식 (차단 방지)
        time.sleep(random.uniform(5.0, 8.0))