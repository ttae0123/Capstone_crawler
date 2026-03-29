import os
import time
import re
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from convert_to_DB import get_db_connection

def extract_refined_spec(spec_list, category):

    res = {}
    combined_text = " / ".join(spec_list)

    if category == "CPU":
        res['칩셋 종류'] = re.search(r'소켓([a-zA-Z0-9]+)', combined_text).group(1) if re.search(r'소켓([a-zA-Z0-9]+)', combined_text) else "N/A"
        res['메모리규격'] = re.findall(r'DDR[45]', combined_text)[0] if re.findall(r'DDR[45]', combined_text) else "N/A"
        res['쿨러'] = "미포함" if "미포함" in combined_text else ("포함" if "쿨러" in combined_text else "N/A")

    elif category == "GPU":
        res['정격파워'] = re.search(r'정격파워\s*([0-9]+W)', combined_text).group(1) if re.search(r'정격파워\s*([0-9]+W)', combined_text) else "N/A"
        res['메인보드 연결'] = re.search(r'(PCIe[0-9.]+\s*X\s*[0-9]+)', combined_text, re.I).group(1) if re.search(r'(PCIe[0-9.]+\s*X\s*[0-9]+)', combined_text, re.I) else "N/A"
        gpu_len = re.search(r'가로\(길이\)[^0-9]*([\d.]+)\s*mm', combined_text, re.I)
        res['가로(길이)'] = f"{gpu_len.group(1)}mm" if gpu_len else "N/A"

    elif category == "Mainboard":
        res['칩셋 종류'] = re.search(r'소켓([a-zA-Z0-9]+)', combined_text).group(1) if re.search(r'소켓([a-zA-Z0-9]+)', combined_text) else "N/A"
        res['램슬롯'] = re.findall(r'DDR[45]', combined_text)[0] if re.findall(r'DDR[45]', combined_text) else "N/A"
        res['그래픽카드 연결'] = re.search(r'PCIe[0-9.]+\s*x[0-9]+', combined_text, re.I).group(0) if re.search(r'PCIe[0-9.]+\s*x[0-9]+', combined_text, re.I) else "N/A"
        res['보드 사이즈'] = re.search(r'(ATX|M-ATX|E-ATX|Mini-ITX)', combined_text, re.I).group(0) if re.search(r'(ATX|M-ATX|E-ATX|Mini-ITX)', combined_text, re.I) else "N/A"
        res['램클럭'] = re.search(r'([0-9]+MHz)', combined_text).group(1) if re.search(r'([0-9]+MHz)', combined_text) else "N/A"

    elif category == "RAM":
        res['램슬롯'] = re.findall(r'DDR[45]', combined_text)[0] if re.findall(r'DDR[45]', combined_text) else "N/A"
        res['램클럭'] = re.search(r'([0-9]+MHz)', combined_text).group(1) if re.search(r'([0-9]+MHz)', combined_text) else "N/A"
        res['램개수'] = re.search(r'램개수:\s*([0-9]+개)', combined_text).group(1) if re.search(r'램개수:\s*([0-9]+개)', combined_text) else "1개"

    elif category == "Case":
        res['지원보드규격'] = ", ".join(re.findall(r'(ATX|M-ATX|E-ATX|Mini-ITX)', combined_text, re.I))
        vga_len = re.search(r'VGA\s*길이[^0-9]*([\d.]+)\s*mm', combined_text, re.I)
        res['VGA(GPU)길이'] = f"{vga_len.group(1)}mm" if vga_len else "N/A"
        cpu_h = re.search(r'CPU쿨러\s*높이[^0-9]*([\d.]+)\s*mm', combined_text, re.I)
        res['CPU쿨러 높이'] = f"{cpu_h.group(1)}mm" if cpu_h else "N/A"

    elif category == "Power":
        res['파워사이즈'] = re.search(r'(ATX|M-ATX|SFX)', combined_text, re.I).group(0) if re.search(r'(ATX|M-ATX|SFX)', combined_text, re.I) else "N/A"
        res['정격출력'] = re.search(r'([0-9]+W)', combined_text).group(1) if re.search(r'([0-9]+W)', combined_text) else "N/A"

    return res

def crawl_danawa(category_name, cate_code, total_pages):
    print(f"\n>>> {category_name} 정밀 수집 시작 (목표: 1~{total_pages}페이지)")

    options = Options()
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    options.add_argument("--headless")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    all_data = []

    try:
        for page in range(1, total_pages + 1):
            url = f"https://prod.danawa.com/list/?cate={cate_code}&page={page}"
            driver.get(url)
            print(f"  [{page}/{total_pages}] 페이지 접속 중...")

            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".main_prodlist")))

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

            products = driver.find_elements(By.CSS_SELECTOR, "li.prod_item")

            for p in products:

                p_class = p.get_attribute("class")
                if "product-pot" in p_class or "ad_prod_item" in p_class:
                    continue

                try:

                    name = p.find_element(By.CSS_SELECTOR, ".prod_name a").text.strip()


                    price_str = p.find_element(By.CSS_SELECTOR, ".price_sect strong").text.replace(",", "").strip()
                    if not price_str.isdigit(): continue
                    price = int(price_str)


                    spec_elements = p.find_elements(By.CSS_SELECTOR, ".spec_list .view_dic")
                    spec_list = [s.text.strip() for s in spec_elements if s.text.strip()]

                    if name and price:
                        item = {"제품명": name, "가격": price}

                        refined_specs = extract_refined_spec(spec_list, category_name)
                        item.update(refined_specs)


                        if category_name == "RAM" and "데스크탑용" not in " ".join(spec_list):
                            continue

                        all_data.append(item)
                except Exception:
                    continue


            time.sleep(1.5)


        save_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
        if not os.path.exists(save_dir): os.makedirs(save_dir)

        output_file = os.path.join(save_dir, f"data_{category_name}.csv")
        df_result = pd.DataFrame(all_data)
        df_result.to_csv(output_file, index=False, encoding="utf-8-sig")
        print(f"★ {category_name} 수집 완료: 총 {len(all_data)}개 데이터 저장됨")
        if category_name in ["Mainboard", "Power", "CASE"]:
            get_db_connection(category_name, df_result)

    except Exception as e:
        print(f"! {category_name} 수집 중 에러 발생: {e}")
    finally:
        driver.quit()

# 수집 대상 리스트
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

        crawl_danawa(p["name"], p["code"], total_pages=5)

        time.sleep(3)