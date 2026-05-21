import os
import time
import re
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from datetime import datetime


# =========================================================
# CPU / Mainboard 소켓 정규화
# - CPU와 메인보드는 실제 장착 호환성이 중요하므로 115X로 통합하지 않음
# - 예: LGA1151 -> 1151
# - 예: LGA1151V2 -> 1151V2
# - 예: LGA1700 -> 1700
# - 예: AM5 / AMD5 -> AMD5
# =========================================================
def normalize_cpu_mainboard_socket(socket):
    if not socket:
        return None

    socket = socket.upper()
    socket = socket.replace(" ", "")
    socket = socket.replace("-", "")

    # AMD 소켓 정규화
    if socket in ["AM5", "AMD5"]:
        return "AMD5"
    if socket in ["AM4", "AMD4"]:
        return "AMD4"

    # 인텔 LGA 제거
    socket = socket.replace("LGA", "")

    # CPU / Mainboard에서는 115X 통합 금지
    if socket == "1151V2":
        return "1151V2"

    # 1150, 1151, 1155, 1156, 1200, 1700, 1851 등 숫자 소켓 유지
    if re.fullmatch(r"\d+", socket):
        return socket

    return socket


# =========================================================
# Cooler 소켓 정규화
# - 쿨러는 브라켓 호환 때문에 LGA115x 계열을 묶어서 지원하는 경우가 많음
# - 예: 1150, 1151, 1151V2, 1155, 1156 -> 115X
# =========================================================
def normalize_cooler_socket(socket):
    if not socket:
        return None

    socket = socket.upper()
    socket = socket.replace(" ", "")
    socket = socket.replace("-", "")

    # AMD 소켓 정규화
    if socket in ["AM5", "AMD5"]:
        return "AMD5"
    if socket in ["AM4", "AMD4"]:
        return "AMD4"

    # 인텔 LGA 제거
    socket = socket.replace("LGA", "")

    # 쿨러는 115x 계열 통합
    if socket in ["115X", "1150", "1151", "1151V2", "1155", "1156"]:
        return "115X"

    # 1200, 1700, 1851 등은 숫자 그대로 유지
    if re.fullmatch(r"\d+", socket):
        return socket

    return socket


# =========================================================
# 쿨러용 소켓 리스트 추출
# - 쿨러는 여러 소켓을 동시에 지원하므로 콤마 문자열로 저장
# - 예: "115X,1200,1700,AMD4,AMD5"
# =========================================================
def extract_cooler_socket_list(text):
    if not text:
        return None

    text = text.upper()
    text = text.replace(" ", "")
    text = text.replace("-", "")

    sockets = re.findall(
        r"LGA1151V2|LGA\d{4}|LGA115X|AM\d|AMD\d|1151V2|115X|\d{4}",
        text,
        re.I
    )

    normalized = []

    for socket in sockets:
        value = normalize_cooler_socket(socket)

        if value and value not in normalized:
            normalized.append(value)

    if not normalized:
        return None

    return ",".join(normalized)


# =========================================================
# PCIe 타입 추출 공통 함수
# - GPU / Mainboard에서 동일하게 사용
# - 예: PCIe 4.0 x16 -> PCIE4.0X16
# - 예: PCIe5.0 X 16 -> PCIE5.0X16
# =========================================================
def extract_pcie_type(text):
    if not text:
        return None

    text = text.upper()
    text = text.replace(" ", "")
    text = text.replace("-", "")

    m = re.search(r"PCIE[0-9.]+X[0-9]+", text, re.I)

    return m.group(0) if m else None


def extract_refined_spec(spec_list, category):
    res = {}
    combined_text = " / ".join(spec_list)

    def search(pattern, flags=0):
        return re.search(pattern, combined_text, flags)

    def findall(pattern, flags=0):
        return re.findall(pattern, combined_text, flags)

    def normalize_size(val):
        if not val:
            return None

        val = val.upper()

        mapping = {
            "M-ATX": "MATX",
            "MICRO-ATX": "MATX",
            "MINI-ITX": "ITX",
            "E-ATX": "EATX"
        }

        return mapping.get(val, val.replace("-", ""))

    def pick_largest_size(vals):
        if not vals:
            return None

        order = {
            "EATX": 4,
            "ATX": 3,
            "MATX": 2,
            "ITX": 1
        }

        normalized = [normalize_size(v) for v in vals if normalize_size(v)]

        if not normalized:
            return None

        return max(normalized, key=lambda x: order.get(x, 0))

    # =====================================================
    # CPU
    # =====================================================
    if category == "CPU":
        m = search(r"소켓\s*([a-zA-Z0-9]+)", re.I)
        res["socket_type"] = normalize_cpu_mainboard_socket(m.group(1)) if m else None

        m = findall(r"DDR[0-9]+", re.I)
        res["memory_type"] = m[0].upper() if m else None

    # =====================================================
    # GPU
    # =====================================================
    elif category == "GPU":
        m = search(r"정격파워\s*([0-9]+)W", re.I)
        res["recommended_power"] = int(m.group(1)) if m else None

        # PCIe 추출 공통 함수 사용
        res["pcie_type"] = extract_pcie_type(combined_text)

        m = search(r"가로\(길이\)[^0-9]*([\d.]+)\s*mm", re.I)
        res["gpu_length"] = int(float(m.group(1))) if m else None

    # =====================================================
    # Mainboard
    # =====================================================
    elif category == "Mainboard":
        m = search(r"소켓\s*([a-zA-Z0-9]+)", re.I)
        res["socket_type"] = normalize_cpu_mainboard_socket(m.group(1)) if m else None

        m = findall(r"DDR[0-9]+", re.I)
        res["memory_type"] = m[0].upper() if m else None

        # PCIe 추출 공통 함수 사용
        res["pcie_type"] = extract_pcie_type(combined_text)

        m = search(r"(ATX|M-ATX|E-ATX|Mini-ITX|Micro-ATX)", re.I)
        res["size"] = normalize_size(m.group(0)) if m else None

        m = search(r"([0-9]+)MHz", re.I)
        res["memory_clock"] = int(m.group(1)) if m else None

    # =====================================================
    # RAM
    # =====================================================
    elif category == "RAM":
        m = findall(r"DDR[0-9]+", re.I)
        res["memory_type"] = m[0].upper() if m else None

        m = search(r"([0-9]+)MHz", re.I)
        res["memory_clock"] = int(m.group(1)) if m else None

    # =====================================================
    # Case
    # =====================================================
    elif category == "Case":
        sizes = findall(r"(ATX|M-ATX|E-ATX|Mini-ITX|Micro-ATX)", re.I)
        res["size"] = pick_largest_size(sizes)

        m = search(r"VGA\s*길이[^0-9]*([\d.]+)\s*mm", re.I)
        res["gpu_length"] = int(float(m.group(1))) if m else None

        m = search(r"CPU쿨러\s*높이[^0-9]*([\d.]+)\s*mm", re.I)
        res["cooler_length"] = int(float(m.group(1))) if m else None

    # =====================================================
    # Power
    # =====================================================
    elif category == "Power":
        m = search(r"(ATX|M-ATX|SFX)", re.I)
        res["size"] = normalize_size(m.group(0)) if m else None

        m = search(r"([0-9]+)W", re.I)
        res["wattage"] = int(m.group(1)) if m else None

    # =====================================================
    # Cooler
    # =====================================================
    elif category == "Cooler":
        # 2순위 방식으로 공랭 필터를 클릭하더라도,
        # 혹시 다른 제품이 섞일 수 있으니 마지막 방어용으로 한 번 더 체크
        if "공랭" not in combined_text:
            return None

        # 쿨러만 115X 통합 허용
        res["socket_type"] = extract_cooler_socket_list(combined_text)

        # 다나와 쿨러 스펙 예:
        # 가로: 120mm / 세로: 75mm / 높이: 155mm
        m = search(r"높이[^0-9]*([\d.]+)\s*mm", re.I)
        res["cooler_length"] = int(float(m.group(1))) if m else None

    return res


def apply_cooler_air_filter(page_obj, category_name):
    if category_name != "Cooler":
        return

    try:
        print("  - Cooler 공랭 필터 적용 시도")

        time.sleep(2)

        # 냉각 방식 행의 '공랭' 체크박스 클릭
        page_obj.get_by_text("공랭", exact=True).click(timeout=10000)

        page_obj.wait_for_load_state("networkidle")
        time.sleep(2)

        print("  - Cooler 공랭 필터 적용 완료")

    except Exception as e:
        print(f"  ! Cooler 공랭 필터 적용 실패: {e}")
        print("  ! 필터 적용 실패 시 전체 Cooler 페이지를 수집하고 파싱 단계에서 공랭만 거릅니다.")


def crawl_danawa(category_name, cate_code, total_pages):
    print(f"\n>>> {category_name} 수집 시작 (목표: {total_pages}페이지)")

    temp_dir = os.path.join(os.path.dirname(__file__), "temp_html", category_name)
    os.makedirs(temp_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )

        page_obj = context.new_page()

        start_url = f"https://prod.danawa.com/list/?cate={cate_code}"
        page_obj.goto(start_url, wait_until="networkidle")
        time.sleep(2)

        # Cooler일 때만 '공랭' 필터 클릭
        apply_cooler_air_filter(page_obj, category_name)

        for page in range(1, total_pages + 1):
            try:
                page_obj.evaluate(f"movePage({page})")

                page_obj.wait_for_selector(
                    f"a.num.now_on:has-text('{page}')",
                    timeout=15000
                )

                time.sleep(2)

                for _ in range(5):
                    page_obj.keyboard.press("PageDown")
                    time.sleep(0.4)

                file_path = os.path.join(temp_dir, f"page_{page}.html")

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(page_obj.content())

                print(f"  [{page}/{total_pages}] 저장 완료", end="\r")

                time.sleep(random.uniform(1.5, 3.0))

            except Exception as e:
                print(f"\n  ! {page}페이지 이동/로딩 오류: {e}")
                continue

        browser.close()

    all_data = []

    for file_name in os.listdir(temp_dir):
        if not file_name.endswith(".html"):
            continue

        with open(os.path.join(temp_dir, file_name), "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        products = soup.select("li[id^='productItem']:not(.ad_prod_item)")

        for p in products:
            try:
                name_tag = p.select_one(".prod_name a")
                price_tag = p.select_one(".price_sect strong")

                if not name_tag or not price_tag:
                    continue

                name = name_tag.get_text().strip()

                price_str = re.sub(r"[^0-9]", "", price_tag.get_text())

                if not price_str:
                    continue

                price = int(price_str)

                if price <= 0:
                    continue

                spec_list = [
                    s.get_text().strip()
                    for s in p.select(".spec_list .view_dic")
                ]

                spec_data = extract_refined_spec(spec_list, category_name)

                # Cooler에서 공랭이 아니거나 스펙 추출 실패 시 제외
                if spec_data is None:
                    continue

                item = {
                    "name": name,
                    "price": price
                }

                item.update(spec_data)

                all_data.append(item)

            except Exception as e:
                continue

    df = pd.DataFrame(all_data)

    if not df.empty:
        df["created_at"] = datetime.now()

        df = df.sort_values(by="price", ascending=True)
        df = df.drop_duplicates(subset=["name"], keep="first")

    base_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data")
    )

    if category_name in ["Mainboard", "Power", "Case", "Cooler"]:
        output_dir = os.path.join(base_dir, "result")
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = base_dir

    os.makedirs(output_dir, exist_ok=True)

    output_path = os.path.join(output_dir, f"data_{category_name}.csv")

    df.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"\n★ {category_name} 완료: {len(df)}개 고유 데이터 확보")
    print(f"  저장 위치: {output_path}")


if __name__ == "__main__":
    parts_list = [
        {"name": "CPU", "code": "112747", "page": 18},
        {"name": "GPU", "code": "112753", "page": 20},
        {"name": "Mainboard", "code": "112751", "page": 20},
        {"name": "Power", "code": "112777", "page": 20},
        {"name": "RAM", "code": "112752", "page": 20},
        {"name": "SSD", "code": "112760", "page": 20},
        {"name": "Case", "code": "112775", "page": 20},
        {"name": "Cooler", "code": "11336857", "page": 20}
    ]

    for p in parts_list:
        crawl_danawa(p["name"], p["code"], p["page"])

        # 카테고리 간 충분한 휴식
        time.sleep(random.uniform(5.0, 8.0))