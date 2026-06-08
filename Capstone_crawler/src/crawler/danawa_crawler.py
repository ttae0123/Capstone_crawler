import os
import time
import re
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from datetime import datetime


# =========================================================
# 부품별 최소 가격 필터
# - 특정 금액 이하 제품은 노이즈로 판단하고 제거
# - 너무 낮게 잡으면 노이즈가 남고, 너무 높게 잡으면 저가 정상 제품도 사라질 수 있음
# - 필요하면 실제 CSV 확인 후 금액만 조정하면 됨
# =========================================================
MIN_PRICE_BY_CATEGORY = {
    "CPU": 30000,
    "GPU": 50000,
    "Mainboard": 40000,
    "RAM": 10000,
    "SSD": 10000,
    "Power": 20000,
    "Case": 20000,
    "Cooler": 10000
}


def is_noise_by_price(category_name, price):
    min_price = MIN_PRICE_BY_CATEGORY.get(category_name)

    if min_price is None:
        return False

    return price < min_price


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

    if socket in ["AM5", "AMD5"]:
        return "AMD5"

    if socket in ["AM4", "AMD4"]:
        return "AMD4"

    socket = socket.replace("LGA", "")

    if socket == "1151V2":
        return "1151V2"

    if re.fullmatch(r"\d+", socket):
        return socket

    return socket


# =========================================================
# Cooler 소켓 정규화
# - 쿨러는 브라켓 호환 때문에 LGA115x 계열을 묶어서 지원하는 경우가 많음
# - 예: 1150, 1151, 1151V2, 1155, 1156 -> 115X
# - AM4 / AMD4는 최종적으로 AMD4로 통일
# - AM5 / AMD5는 최종적으로 AMD5로 통일
# =========================================================
def normalize_cooler_socket(socket):
    if not socket:
        return None

    socket = socket.upper()
    socket = socket.replace(" ", "")
    socket = socket.replace("-", "")
    socket = socket.replace("_", "")

    socket = socket.replace("LGA", "")

    if socket in ["AM5", "AMD5"]:
        return "AMD5"

    if socket in ["AM4", "AMD4"]:
        return "AMD4"

    if socket in ["AM3", "AM3+", "AM2", "AM2+"]:
        return socket

    if socket in ["FM1", "FM2", "FM2+"]:
        return socket

    if socket in ["TR4", "STRX4", "SWRX8"]:
        return socket

    if socket in ["115X", "1150", "1151", "1151V2", "1155", "1156"]:
        return "115X"

    valid_intel_sockets = {
        "1200",
        "1700",
        "1851",
        "2011",
        "2011V3",
        "2066"
    }

    if socket in valid_intel_sockets:
        return socket

    return None


# =========================================================
# Cooler 소켓 리스트 추출
# - 쿨러는 여러 소켓을 동시에 지원하므로 콤마 문자열로 저장
# - 예: "115X,1200,1700,1851,AMD4,AMD5"
# - \d{4} 같은 범용 숫자 추출 금지
# =========================================================
def extract_cooler_socket_list(text):
    if not text:
        return None

    text = text.upper()

    text = text.replace(" ", "")
    text = text.replace("-", "")
    text = text.replace("_", "")

    text = text.replace("소켓", "/")
    text = text.replace("인텔", "/")
    text = text.replace("라이젠", "/")
    text = text.replace("AMD", "AMD")

    pattern = r"""
        LGA1151V2|
        LGA2011V3|
        LGA115X|
        LGA1150|LGA1151|LGA1155|LGA1156|
        LGA1200|LGA1700|LGA1851|
        LGA2011|LGA2066|
        1151V2|
        2011V3|
        115X|1150|1151|1155|1156|
        1200|1700|1851|
        2011|2066|
        AM2\+|AM3\+|AM2|AM3|AM4|AM5|
        AMD4|AMD5|
        FM2\+|FM1|FM2|
        STRX4|SWRX8|TR4
    """

    found = re.findall(pattern, text, re.I | re.VERBOSE)

    normalized = []

    for socket in found:
        value = normalize_cooler_socket(socket)

        if value and value not in normalized:
            normalized.append(value)

    if not normalized:
        return None

    return ",".join(normalized)


# =========================================================
# Cooler 부속품/브라켓/나사류 제거
# - CPU 쿨러 본체가 아닌 부속품이 추천 결과에 들어가는 것을 방지
# =========================================================
def is_invalid_cooler_name(name):
    if not name:
        return True

    upper_name = name.upper().replace(" ", "")

    exclude_keywords = [
        "SCREW",
        "나사",
        "볼트",
        "브라켓",
        "가이드",
        "KIT",
        "킷",
        "클립",
        "마운트",
        "마운팅",
        "리텐션",
        "서멀패드",
        "써멀패드",
        "방열패드",
        "패드",
        "쿨러가이드",
        "쿨러브라켓"
    ]

    for keyword in exclude_keywords:
        if keyword.upper().replace(" ", "") in upper_name:
            return True

    return False


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


def extract_refined_spec(spec_list, category, name=None):
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
        if "공랭" not in combined_text:
            return None

        cooler_text = combined_text

        if name:
            cooler_text = name + " / " + combined_text

        res["socket_type"] = extract_cooler_socket_list(cooler_text)

        if not res["socket_type"]:
            return None

        m = search(r"높이[^0-9]*([\d.]+)\s*mm", re.I)
        res["cooler_length"] = int(float(m.group(1))) if m else None

        if res["cooler_length"] is None:
            return None

    return res


def apply_cooler_air_filter(page_obj, category_name):
    if category_name != "Cooler":
        return

    try:
        print("  - Cooler 공랭 필터 적용 시도")

        time.sleep(2)

        page_obj.get_by_text("공랭", exact=True).click(timeout=10000)

        page_obj.wait_for_load_state("networkidle")
        time.sleep(2)

        print("  - Cooler 공랭 필터 적용 완료")

    except Exception as e:
        print(f"  ! Cooler 공랭 필터 적용 실패: {e}")
        print("  ! 필터 적용 실패 시 전체 Cooler 페이지를 수집하고 파싱 단계에서 공랭만 거릅니다.")


def print_cooler_socket_validation(df):
    if df.empty or "socket_type" not in df.columns:
        print("\n[Cooler 소켓 타입 검증]")
        print("검증할 데이터가 없습니다.")
        return

    print("\n[Cooler 소켓 타입 검증]")

    all_sockets = set()

    for socket_text in df["socket_type"].dropna():
        for socket in str(socket_text).split(","):
            socket = socket.strip()
            if socket:
                all_sockets.add(socket)

    sorted_sockets = sorted(all_sockets)

    print("추출된 소켓 목록:")
    print(sorted_sockets)

    valid_sockets = {
        "115X",
        "1200",
        "1700",
        "1851",
        "2011",
        "2011V3",
        "2066",
        "AM2",
        "AM2+",
        "AM3",
        "AM3+",
        "AMD4",
        "AMD5",
        "FM1",
        "FM2",
        "FM2+",
        "TR4",
        "STRX4",
        "SWRX8"
    }

    suspicious = []

    for socket in sorted_sockets:
        if socket not in valid_sockets:
            suspicious.append(socket)

    for socket in sorted_sockets:
        if re.fullmatch(r"\d{4}", socket):
            if socket not in ["1200", "1700", "1851", "2011", "2066"]:
                suspicious.append(socket)

    suspicious = sorted(set(suspicious))

    if suspicious:
        print("[경고] 의심 소켓 발견:")
        print(suspicious)
    else:
        print("[정상] 의심 소켓 없음")

    if "1700" not in all_sockets:
        print("[주의] LGA1700 소켓이 하나도 추출되지 않았습니다. 쿨러 소켓 파싱을 다시 확인하세요.")

    if "AMD5" not in all_sockets:
        print("[주의] AMD5 소켓이 하나도 추출되지 않았습니다. 최신 AM5 쿨러 데이터가 부족할 수 있습니다.")


def print_price_filter_summary(category_name, before_count, after_count):
    removed_count = before_count - after_count
    min_price = MIN_PRICE_BY_CATEGORY.get(category_name)

    print("\n[가격 노이즈 필터]")
    print(f"카테고리: {category_name}")
    print(f"최소 허용 가격: {min_price:,}원")
    print(f"필터 전 개수: {before_count}")
    print(f"필터 후 개수: {after_count}")
    print(f"제거된 개수: {removed_count}")


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

    total_parsed_count = 0
    price_noise_count = 0
    invalid_cooler_name_count = 0
    spec_fail_count = 0

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

                if category_name == "Cooler" and is_invalid_cooler_name(name):
                    invalid_cooler_name_count += 1
                    continue

                price_str = re.sub(r"[^0-9]", "", price_tag.get_text())

                if not price_str:
                    continue

                price = int(price_str)

                if price <= 0:
                    continue

                total_parsed_count += 1

                # =================================================
                # 모든 부품 공통 가격 노이즈 필터
                # =================================================
                if is_noise_by_price(category_name, price):
                    price_noise_count += 1
                    continue

                spec_list = [
                    s.get_text().strip()
                    for s in p.select(".spec_list .view_dic")
                ]

                spec_data = extract_refined_spec(spec_list, category_name, name)

                if spec_data is None:
                    spec_fail_count += 1
                    continue

                item = {
                    "name": name,
                    "price": price
                }

                item.update(spec_data)

                all_data.append(item)

            except Exception:
                continue

    df = pd.DataFrame(all_data)

    before_dedup_count = len(df)

    if not df.empty:
        df["created_at"] = datetime.now()

        df = df.sort_values(by="price", ascending=True)
        df = df.drop_duplicates(subset=["name"], keep="first")

        after_dedup_count = len(df)

        print_price_filter_summary(
            category_name=category_name,
            before_count=total_parsed_count,
            after_count=total_parsed_count - price_noise_count
        )

        print("\n[추가 제거 로그]")
        print(f"Cooler 부속품 이름 필터 제거: {invalid_cooler_name_count}")
        print(f"스펙 추출 실패 제거: {spec_fail_count}")
        print(f"중복 제거 전 개수: {before_dedup_count}")
        print(f"중복 제거 후 개수: {after_dedup_count}")

        if category_name == "Cooler":
            print_cooler_socket_validation(df)
    else:
        print_price_filter_summary(
            category_name=category_name,
            before_count=total_parsed_count,
            after_count=total_parsed_count - price_noise_count
        )

        print("\n[추가 제거 로그]")
        print(f"Cooler 부속품 이름 필터 제거: {invalid_cooler_name_count}")
        print(f"스펙 추출 실패 제거: {spec_fail_count}")
        print("최종 데이터가 없습니다.")

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

        time.sleep(random.uniform(5.0, 8.0))