import os
import time
import re
import pandas as pd
from playwright.sync_api import sync_playwright
from logic.convert_to_DB import get_db_connection

# [로직 변경 없음] 기존 정규표현식 추출 함수
def extract_refined_spec(spec_list, category):
    res = {}
    combined_text = " / ".join(spec_list)

    def search(pattern, flags=0):
        return re.search(pattern, combined_text, flags)

    def findall(pattern, flags=0):
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
        if not normalized: return None
        return max(normalized, key=lambda x: order.get(x, 0))

    # 카테고리별 분기 로직 (기존과 동일)
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

# [변경 포인트] Playwright 기반 크롤링 함수
def crawl_danawa(category_name, cate_code, total_pages):
    print(f"\n>>> {category_name} 정밀 수집 시작 (목표: 1~{total_pages}페이지)")

    with sync_playwright() as p:
        # 브라우저 실행 (Headless 모드)
        browser = p.chromium.launch(headless=True)
        # 실제 사용자처럼 보이게 하기 위해 User-Agent 설정
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page_obj = context.new_page()

        all_data = []
        seen_names = set() # 중복 체크용

        try:
            for page in range(1, total_pages + 1):
                url = f"https://prod.danawa.com/list/?cate={cate_code}&page={page}"
                page_obj.goto(url)
                print(f"  [{page}/{total_pages}] 페이지 접속 중...")

                # 메인 리스트 로딩 대기
                page_obj.wait_for_selector(".main_prodlist", timeout=15000)

                # 동적 로딩을 위한 스크롤 다운
                page_obj.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)

                # 상품 리스트 추출
                products = page_obj.query_selector_all("li.prod_item")

                for p in products:
                    p_class = p.get_attribute("class") or ""
                    if "product-pot" in p_class or "ad_prod_item" in p_class:
                        continue

                    try:
                        # 제품명 추출 및 괄호 제거
                        name_el = p.query_selector(".prod_name a")
                        if not name_el: continue
                        name = name_el.inner_text().strip()
                        name = re.sub(r'\(.*?\)', '', name).strip()

                        # 가격 추출
                        price_el = p.query_selector(".price_sect strong")
                        if not price_el: continue
                        price_str = price_el.inner_text().replace(",", "").strip()
                        if not price_str.isdigit(): continue
                        price = int(price_str)

                        # 스펙 추출
                        spec_elements = p.query_selector_all(".spec_list .view_dic")
                        spec_list = [s.inner_text().strip() for s in spec_elements if s.inner_text().strip()]

                        if name and price:
                            # 1차 중복 제거
                            if name in seen_names:
                                continue
                            seen_names.add(name)

                            item = {"name": name, "price": price}
                            refined_specs = extract_refined_spec(spec_list, category_name)
                            item.update(refined_specs)

                            # RAM 예외 처리
                            if category_name == "RAM" and "데스크탑용" not in " ".join(spec_list):
                                continue

                            all_data.append(item)
                    except Exception:
                        continue

                time.sleep(1)

            # 저장 경로 설정
            save_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
            if not os.path.exists(save_dir): os.makedirs(save_dir)

            output_file = os.path.join(save_dir, f"data_{category_name}.csv")
            df_result = pd.DataFrame(all_data)

            # 2차 중복 제거 및 저장
            df_result = df_result.drop_duplicates(subset=["name"], keep="first")
            df_result.to_csv(output_file, index=False, encoding="utf-8-sig")

            print(f"{category_name} 수집 완료: 총 {len(df_result)}개 데이터 저장됨")

            # DB 연동
            if category_name in ["Mainboard", "Power", "Case"]:
                get_db_connection(category_name, df_result)

        except Exception as e:
            print(f"! {category_name} 수집 중 에러 발생: {e}")
        finally:
            browser.close()

# 실행 대상 리스트
parts_list = [
    {"name": "CPU", "code": "112747"},
    {"name": "GPU", "code": "112753"},
    {"name": "Mainboard", "code": "112751"},
    {"name": "Power", "code": "112777"},
    {"name": "RAM", "code": "112752"},
    {"name": "SSD", "code": "112760"},
    {"name": "Case", "code": "112775"}
]

