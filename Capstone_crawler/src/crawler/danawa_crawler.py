import os
import time
import re
import random
from pathlib import Path
from urllib.parse import (
    urlsplit, urlunsplit, parse_qsl, parse_qs, urlencode,
    unquote_plus, urljoin,
)
import json
import traceback

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from datetime import datetime

CRAWLER_VERSION = "V21-product-identity"

# =========================================================
# 기본 설정
# =========================================================
MIN_PRICE_BY_CATEGORY = {
    "CPU": 30000,
    "GPU": 50000,
    "Mainboard": 40000,
    "RAM": 10000,
    "SSD": 10000,
    "Power": 20000,
    "Case": 20000,
    "Cooler": 10000,
}

# 자동 견적에 사용하기 위한 현실적인 소비자용 가격 상한.
# 비정상 가격/서버·산업용 초고가 제품이 추천 후보로 들어가는 것을 방지한다.
MAX_PRICE_BY_CATEGORY = {
    "CPU": 3_000_000,
    "GPU": 6_000_000,
    "Mainboard": 2_000_000,
    "RAM": 5_000_000,
    "SSD": 3_000_000,
    "Power": 1_500_000,
    "Case": 1_500_000,
    "Cooler": 500_000,
}

# 제품명으로 잘못 선택되는 카드혜택/쇼핑몰/리뷰 문자열 공통 패턴.
BENEFIT_NAME_PATTERNS = (
    r"^\s*[\"']?\d{1,3}(?:,\d{3})+\s*원\s*\[",
    r"\[(?:SSG\.COM|신세계몰|롯데ON|하이마트|옥션|G마켓|오늘의집|11번가|쿠팡)\]",
    r"(?:BC|KB국민|삼성|롯데|현대|신한|NH|우리|하나)카드",
    r"무이자\s*최대",
    r"^\s*\d(?:\.\d)?\s*리뷰수\b",
    r"\b리뷰수\s*\(",
)

VALID_RECOMMEND_CPU_MAINBOARD_SOCKETS = {
    "1151V2",
    "1200",
    "1700",
    "1851",
    "AMD4",
    "AMD5",
}

VALID_RECOMMEND_COOLER_SOCKETS = {
    "115X",
    "1200",
    "1700",
    "1851",
    "AMD4",
    "AMD5",
}

ENABLE_STRICT_CASE_COMPATIBILITY_FILTER = True
CASE_REQUIRED_COMPATIBILITY_FIELDS = ("size", "gpu_length", "cooler_length")

LEGACY_PRODUCT_SELECTOR = "li[id^='productItem']:not(.ad_prod_item)"
MODERN_PRODUCT_SELECTOR = ".dnw-product-list-item"
PRODUCT_SELECTOR = f"{LEGACY_PRODUCT_SELECTOR}, {MODERN_PRODUCT_SELECTOR}"

# SSD 추천용 허용 범위.
# 자동 견적에서는 256GB 미만 SSD를 크롤링 결과에서 제외하고,
# 지나치게 큰 기업용/특수 제품도 제외한다.
SSD_MIN_CAPACITY_GB = 256
SSD_MAX_CAPACITY_GB = 8192

# 흔히 판매되는 SSD 용량. 정확히 일치하지 않아도 허용하되,
# 여러 후보가 잡혔을 때 저장장치 본 용량을 고르는 보조 기준으로 사용한다.
COMMON_SSD_CAPACITIES_GB = {
    256, 480, 500, 512,
    960, 1000, 1024, 1920, 2000, 2048,
    3840, 4000, 4096, 7680, 8000, 8192,
}


# =========================================================
# 공통 유틸
# =========================================================
def normalize_space(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def classify_price(category_name, price):
    if price is None:
        return "missing"

    minimum = MIN_PRICE_BY_CATEGORY.get(category_name)
    maximum = MAX_PRICE_BY_CATEGORY.get(category_name)

    if minimum is not None and price < minimum:
        return "below_min"
    if maximum is not None and price > maximum:
        return "above_max"
    return None


def is_noise_by_price(category_name, price):
    return classify_price(category_name, price) is not None


def is_benefit_or_meta_name(name):
    name = normalize_space(name)
    if not name:
        return True

    for pattern in BENEFIT_NAME_PATTERNS:
        if re.search(pattern, name, re.I):
            return True

    if re.fullmatch(r"\d(?:\.\d)?\s*리뷰수.*", name, re.I):
        return True
    if re.match(r"^\d{1,3}(?:,\d{3})+원\b", name):
        return True
    return False


def classify_invalid_product_name(category_name, name):
    """자동 견적 DB에 넣지 않을 상품명을 카테고리별로 분류한다."""
    name = normalize_space(name)
    if not name:
        return "invalid"

    upper = name.upper()
    compact = upper.replace(" ", "")

    if is_benefit_or_meta_name(name):
        return "benefit"

    # 중고는 모든 자동 견적 카테고리에서 제외.
    if "중고" in name or re.search(r"\bUSED\b", upper):
        return "used"

    if category_name == "Mainboard":
        # 메인보드 + GPU/CPU 등의 묶음 판매는 단일 부품 가격이 아니므로 제외.
        package_tokens = ["패키지", "번들", "BUNDLE"]
        gpu_tokens = ["RTX ", "RTX", "GTX ", "GTX", "RADEON", "지포스", "그래픽카드"]
        if any(k in upper for k in package_tokens) and any(k in upper for k in gpu_tokens):
            return "bundle"

    elif category_name == "RAM":
        # 데스크톱 일반 소비자용 RAM만 유지.
        if re.search(r"DDR[123]", upper):
            return "old_generation"
        if not re.search(r"DDR[45]", upper):
            return "unsupported_memory"

        # 노트북/서버 메모리는 제외한다.
        # 단, 일반 DDR5에도 존재하는 온다이 ECC(On-die ECC)는 허용한다.
        notebook_tokens = [
            "노트북", "SO-DIMM", "SODIMM", "SO DIMM",
        ]
        if any(token in upper for token in notebook_tokens):
            return "server_or_notebook"

        server_tokens = [
            "REGISTERED", "REG ", "REG.", "RDIMM", "LRDIMM",
            "서버용", "SERVER", "WORKSTATION MEMORY",
        ]
        if any(token in upper for token in server_tokens):
            return "server_or_notebook"

        ecc_check = re.sub(r"ON[\s\-]?DIE\s*ECC", "", upper, flags=re.I)
        ecc_check = re.sub(r"온다이\s*ECC", "", ecc_check, flags=re.I)
        if re.search(r"\bECC\b", ecc_check, re.I):
            return "server_or_notebook"

    elif category_name == "SSD":
        # 일반 데스크톱 자동 견적에서는 서버/엔터프라이즈 인터페이스를 제외.
        enterprise_tokens = [
            "U.2", "U.3", "SAS", "E1.S", "E1.L", "E3.S", "E3.L",
            "ENTERPRISE", "엔터프라이즈", "데이터센터", "DATACENTER",
        ]
        if any(token in upper for token in enterprise_tokens):
            return "enterprise"

    return None


def normalize_cpu_mainboard_socket(value):
    if not value:
        return None

    value = str(value).upper().replace(" ", "").replace("-", "").replace("_", "")

    if value in {"AM4", "AMD4"}:
        return "AMD4"
    if value in {"AM5", "AMD5"}:
        return "AMD5"

    value = value.replace("LGA", "")
    return value


def is_valid_recommend_socket(category_name, socket_type):
    if not socket_type:
        return False
    if category_name in {"CPU", "Mainboard"}:
        return socket_type in VALID_RECOMMEND_CPU_MAINBOARD_SOCKETS
    return True


def normalize_cooler_socket(value):
    if not value:
        return None

    value = str(value).upper().replace(" ", "").replace("-", "").replace("_", "")
    value = value.replace("LGA", "")

    if value in {"AM4", "AMD4"}:
        return "AMD4"
    if value in {"AM5", "AMD5"}:
        return "AMD5"

    if value in {"115X", "1150", "1151", "1151V2", "1155", "1156"}:
        return "115X"

    if value in {"1200", "1700", "1851"}:
        return value

    return None


def extract_cooler_socket_list(text):
    if not text:
        return None

    text = str(text).upper()

    pattern = r"""
        LGA1151V2|LGA115X|LGA1150|LGA1151|LGA1155|LGA1156|
        LGA1200|LGA1700|LGA1851|
        1151V2|115X|1150|1151|1155|1156|1200|1700|1851|
        AM4|AM5|AMD4|AMD5
    """

    found = re.findall(pattern, text, re.I | re.VERBOSE)
    normalized = []

    for item in found:
        value = normalize_cooler_socket(item)
        if value and value in VALID_RECOMMEND_COOLER_SOCKETS and value not in normalized:
            normalized.append(value)

    return ",".join(normalized) if normalized else None


def classify_invalid_cooler(name, full_text=None):
    """자동 견적에서 제외할 쿨러를 분류한다."""
    if not name:
        return "invalid"

    source = normalize_space(f"{name or ''} / {full_text or ''}")
    upper = source.upper()
    compact = upper.replace(" ", "")

    accessory_keywords = [
        "SCREW", "나사", "볼트", "브라켓", "가이드", "KIT", "킷",
        "클립", "마운트", "마운팅", "리텐션", "서멀패드", "써멀패드",
        "방열패드", "쿨러가이드", "쿨러브라켓",
    ]
    if any(k.upper().replace(" ", "") in compact for k in accessory_keywords):
        return "accessory"

    # 일반 데스크톱 자동 견적에서 서버/랙마운트/산업용 저상형 쿨러는 제외한다.
    server_keywords = [
        "서버용", "서버 쿨러", "SERVER COOLER", "SERVER CPU COOLER",
        "RACKMOUNT", "RACK MOUNT", "랙마운트", "산업용", "INDUSTRIAL",
    ]
    if any(keyword in upper for keyword in server_keywords):
        return "server"

    # 1U/1.5U/2U 등의 랙 높이 표기가 있으면 서버용 쿨러로 본다.
    if re.search(r"(?<![A-Z0-9])(?:1(?:\.5)?|2|3|4|5|6)U(?![A-Z0-9])", upper):
        return "server"

    return None


def is_invalid_cooler_name(name, full_text=None):
    return classify_invalid_cooler(name, full_text) is not None


def extract_pcie_type(text):
    if not text:
        return None

    compact = str(text).upper().replace(" ", "").replace("-", "").replace("_", "")
    compact = compact.replace("PCIEXPRESS", "PCIE")
    m = re.search(r"PCIE[0-9.]+X[0-9]+", compact, re.I)
    return m.group(0) if m else None


def extract_recommended_power(text):
    if not text:
        return None

    text = normalize_space(text).upper().replace(",", "")
    compact = text.replace(" ", "")

    patterns = [
        r"정격\s*파워[^0-9]{0,30}([0-9]{3,4})\s*W",
        r"권장\s*파워[^0-9]{0,30}([0-9]{3,4})\s*W",
        r"추천\s*파워[^0-9]{0,30}([0-9]{3,4})\s*W",
        r"권장\s*전원[^0-9]{0,30}([0-9]{3,4})\s*W",
        r"추천\s*전원[^0-9]{0,30}([0-9]{3,4})\s*W",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return int(m.group(1))

    # 다나와 GPU 목록의 "550W 이상" 형태
    gpu_context = any(
        token in compact
        for token in ["RTX", "GTX", "RADEON", "RX", "PCIE", "GDDR", "그래픽카드"]
    )

    if gpu_context:
        m = re.search(r"([0-9]{3,4})W이상", compact, re.I)
        if m:
            return int(m.group(1))

    return None


# =========================================================
# 메모리 클럭 / Power 규격
# =========================================================
def extract_memory_clock(name=None, spec_text=None, category=None):
    """
    RAM은 상품명의 DDR4-3200 / DDR5-6000 표기를 최우선으로 사용한다.
    스펙의 100~200MHz base clock을 메모리 속도로 오인하지 않도록
    소비자 DDR4/DDR5 범위(1600~10000MHz)만 허용한다.
    """
    name = normalize_space(name)
    spec_text = normalize_space(spec_text)

    if name:
        m = re.search(r"DDR[45]\s*[- ]\s*(\d{4,5})", name, re.I)
        if m:
            value = int(m.group(1))
            if 1600 <= value <= 10000:
                return value

    candidates = []
    for source in [spec_text, name]:
        if not source:
            continue
        for raw in re.findall(r"(?<!\d)(\d{4,5})\s*MHz", source, re.I):
            value = int(raw)
            if 1600 <= value <= 10000:
                candidates.append(value)

    if not candidates:
        return None

    # 메인보드는 여러 지원 클럭이 나열되는 경우가 많으므로 최대 지원값을 저장.
    # RAM은 첫 번째 유효 스펙 값이 실제 동작 클럭일 가능성이 높지만,
    # 상품명 값이 이미 최우선이므로 fallback에서는 최대값을 사용해도 안전하다.
    return max(candidates)


def extract_power_size(name=None, spec_text=None):
    """파워서플라이 폼팩터를 메인보드 M-ATX 문자열과 혼동하지 않고 추출."""
    source = normalize_space(f"{name or ''} / {spec_text or ''}").upper()

    # 구체적인 규격을 먼저 탐색한다.
    patterns = [
        (r"\bSFX[- ]?L\b", "SFXL"),
        (r"\bSFX\b", "SFX"),
        (r"\bTFX\b", "TFX"),
        (r"\bFLEX[- ]?ATX\b", "FLEXATX"),
        (r"\bATX\b", "ATX"),
    ]

    for pattern, normalized in patterns:
        if re.search(pattern, source, re.I):
            return normalized
    return None

# =========================================================
# RAM / SSD 용량
# =========================================================
def capacity_to_gb(value, unit):
    try:
        value = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None

    unit = str(unit).upper().strip()

    if unit in {"TB", "T"}:
        value *= 1000
    elif unit in {"GB", "G"}:
        pass
    else:
        return None

    if value <= 0:
        return None

    return int(round(value))


def normalize_capacity_text(text):
    if not text:
        return ""

    text = str(text).upper()
    replacements = {
        "㎇": "GB",
        "㎔": "TB",
        "기가바이트": "GB",
        "테라바이트": "TB",
        "기가": "GB",
        "테라": "TB",
        "×": "X",
        "*": "X",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return normalize_space(text)



def extract_ram_module_count(text):
    """
    RAM 상품의 모듈 개수(램개수)를 추출한다.

    우선순위:
      1) 다나와 스펙의 "램개수: 2개"
      2) 16GBx2 / 16Gx2 / 32GB(16Gx2)
      3) 2x16GB

    반환:
      1, 2, 4 ... 형태의 모듈 개수
      추출 실패 시 None
    """
    if not text:
        return None

    text = normalize_capacity_text(text)

    m = re.search(
        r"램\s*개수\s*[:：]?\s*(\d+)\s*개",
        text,
        re.I,
    )
    if m:
        count = int(m.group(1))
        if 1 <= count <= 16:
            return count

    matches = re.findall(
        r"\d+(?:\.\d+)?\s*(?:TB|GB|T|G)\s*X\s*(\d+)",
        text,
        re.I,
    )
    for raw in matches:
        count = int(raw)
        if 1 <= count <= 16:
            return count

    matches = re.findall(
        r"(?<!\d)(\d+)\s*X\s*\d+(?:\.\d+)?\s*(?:TB|GB|T|G)",
        text,
        re.I,
    )
    for raw in matches:
        count = int(raw)
        if 1 <= count <= 16:
            return count

    return None


def extract_ram_capacity_candidates(text):
    """
    RAM 문자열에서 실제 총 용량 후보를 GB 단위로 추출한다.

    예:
      64GB(32Gx2)   -> 64
      64GB(32GBx2)  -> 64
      32GBx2        -> 64
      2x32GB        -> 64
      96GB(48Gx2)   -> 96

    패키지 표기 안쪽의 32GB/48GB를 별도 총용량으로 다시 세지 않도록
    먼저 패키지 표현을 추출하고 원문에서 제거한 뒤 단일 용량을 찾는다.
    """
    if not text:
        return []

    work = normalize_capacity_text(text)
    candidates = []

    # 64GB(32Gx2), 64GB(32GBx2)
    pattern_total_pack = re.compile(
        r"(?<![A-Z0-9])"
        r"(\d+(?:\.\d+)?)\s*(GB|TB)"
        r"\s*\(\s*"
        r"(\d+(?:\.\d+)?)\s*(GB|TB|G|T)?"
        r"\s*X\s*(\d+)\s*\)",
        re.I,
    )

    def replace_total_pack(m):
        total = capacity_to_gb(m.group(1), m.group(2))
        if total is not None:
            candidates.append(int(total))
        return " "

    work = pattern_total_pack.sub(replace_total_pack, work)

    # 32GB x 2 / 48G x 2
    pattern_value_x_count = re.compile(
        r"(\d+(?:\.\d+)?)\s*(TB|GB|T|G)\s*X\s*(\d+)",
        re.I,
    )

    def replace_value_x_count(m):
        value = capacity_to_gb(m.group(1), m.group(2))
        count = int(m.group(3))
        if value is not None and 1 <= count <= 16:
            candidates.append(int(value * count))
        return " "

    work = pattern_value_x_count.sub(replace_value_x_count, work)

    # 2 x 32GB
    pattern_count_x_value = re.compile(
        r"(\d+)\s*X\s*(\d+(?:\.\d+)?)\s*(TB|GB|T|G)",
        re.I,
    )

    def replace_count_x_value(m):
        count = int(m.group(1))
        value = capacity_to_gb(m.group(2), m.group(3))
        if value is not None and 1 <= count <= 16:
            candidates.append(int(value * count))
        return " "

    work = pattern_count_x_value.sub(replace_count_x_value, work)

    # 남은 단일 32GB / 64GB / 96GB
    for m in re.finditer(
        r"(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*(TB|GB)(?![A-Z0-9])",
        work,
        re.I,
    ):
        value = capacity_to_gb(m.group(1), m.group(2))
        if value is not None:
            candidates.append(int(value))

    candidates = [v for v in candidates if 1 <= v <= 4096]
    return list(dict.fromkeys(candidates))


def extract_ram_capacity(text):
    """
    단일 용량이 명확할 때만 반환한다.

    기존 max(candidates)는 여러 용량 옵션이 한 상품 카드에 같이 있을 때
    가장 큰 용량과 가장 싼 가격이 잘못 결합되는 원인이므로 사용하지 않는다.
    """
    candidates = sorted(set(extract_ram_capacity_candidates(text)))
    return candidates[0] if len(candidates) == 1 else None


def is_ram_unit_price_text(text):
    """26,568원/1GB 같은 RAM GB당 단가 문자열인지 확인한다."""
    if not text:
        return False

    compact = normalize_space(text).upper().replace(" ", "")
    return bool(re.search(r"원/(?:1)?GB|1GB당", compact, re.I))


def extract_ram_capacity_label(text):
    """
    RAM 가격 옵션 한 행에서 총 용량과 표시용 라벨을 추출한다.

    예:
      64GB(32Gx2) -> (64, "64GB(32Gx2)")
      48GB(24Gx2) -> (48, "48GB(24Gx2)")
      96GB(48Gx2) -> (96, "96GB(48Gx2)")
    """
    if not text:
        return None, None

    text = normalize_capacity_text(text)

    # 64GB(32Gx2), 64GB(32GBx2)
    m = re.search(
        r"(?<![A-Z0-9])"
        r"(\d+(?:\.\d+)?)\s*(GB|TB)"
        r"\s*\(\s*"
        r"(\d+(?:\.\d+)?)\s*(GB|TB|G|T)?"
        r"\s*X\s*(\d+)\s*\)",
        text,
        re.I,
    )
    if m:
        total = capacity_to_gb(m.group(1), m.group(2))
        if total is not None:
            each_unit = (m.group(4) or "G").upper()
            label = (
                f"{m.group(1)}{m.group(2).upper()}"
                f"({m.group(3)}{each_unit}x{m.group(5)})"
            )
            return int(total), label

    # 32GB x 2
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(GB|TB|G|T)\s*X\s*(\d+)",
        text,
        re.I,
    )
    if m:
        each = capacity_to_gb(m.group(1), m.group(2))
        count = int(m.group(3))
        if each is not None and 1 <= count <= 16:
            total = int(each * count)
            label = f"{total}GB({m.group(1)}{m.group(2).upper()}x{count})"
            return total, label

    # 2 x 32GB
    m = re.search(
        r"(\d+)\s*X\s*(\d+(?:\.\d+)?)\s*(GB|TB|G|T)",
        text,
        re.I,
    )
    if m:
        count = int(m.group(1))
        each = capacity_to_gb(m.group(2), m.group(3))
        if each is not None and 1 <= count <= 16:
            total = int(each * count)
            label = f"{total}GB({m.group(2)}{m.group(3).upper()}x{count})"
            return total, label

    # 단일 32GB / 64GB / 96GB
    m = re.search(
        r"(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*(GB|TB)(?![A-Z0-9])",
        text,
        re.I,
    )
    if m:
        capacity = capacity_to_gb(m.group(1), m.group(2))
        if capacity is not None:
            return int(capacity), f"{m.group(1)}{m.group(2).upper()}"

    return None, None


def extract_ram_total_price_from_option_row(option_row):
    """
    RAM 용량 옵션 한 행에서 실제 총가격만 추출한다.

    예:
      96GB(48Gx2) / 26,568원/1GB / 2,550,510원
      -> 2,550,510

    /1GB 단가는 가격 후보에서 제외한다.
    """
    minimum = MIN_PRICE_BY_CATEGORY["RAM"]
    maximum = MAX_PRICE_BY_CATEGORY["RAM"]

    # 1순위: strong에 표시되는 실제 총가격
    strong_prices = []
    for tag in option_row.select("strong"):
        value = _parse_price(tag.get_text(" ", strip=True))
        if value is None:
            continue

        context = normalize_space(
            tag.parent.get_text(" ", strip=True)
            if tag.parent
            else tag.get_text(" ", strip=True)
        )
        if is_ram_unit_price_text(context):
            continue

        if minimum <= value <= maximum:
            strong_prices.append(value)

    if strong_prices:
        return max(strong_prices)

    # 2순위: aria-label
    aria_prices = []
    for tag in option_row.select("[aria-label]"):
        aria = normalize_space(tag.get("aria-label", ""))
        if not aria or is_ram_unit_price_text(aria):
            continue

        for raw in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{4,})\s*원", aria):
            value = _parse_price(raw)
            if value is not None and minimum <= value <= maximum:
                aria_prices.append(value)

    if aria_prices:
        return max(aria_prices)

    # 3순위: 행 전체 텍스트에서 /1GB 단가를 지운 뒤 총가격 검색
    row_text = normalize_space(option_row.get_text(" ", strip=True))
    cleaned = re.sub(
        r"\d{1,3}(?:,\d{3})+\s*원\s*/\s*(?:1)?\s*GB",
        " ",
        row_text,
        flags=re.I,
    )
    cleaned = re.sub(
        r"\d{4,}\s*원\s*/\s*(?:1)?\s*GB",
        " ",
        cleaned,
        flags=re.I,
    )

    candidates = []
    for raw in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{4,})\s*원", cleaned):
        value = _parse_price(raw)
        if value is not None and minimum <= value <= maximum:
            candidates.append(value)

    return max(candidates) if candidates else None


def extract_ram_capacity_price_options(product):
    """
    RAM 상품 카드에서 용량과 총가격을 반드시 같은 옵션 행 기준으로 묶는다.

    예:
      64GB(32Gx2) -> 1,819,980원
      48GB(24Gx2) -> 1,246,960원
      32GB(16Gx2) ->   849,890원
    """
    if product is None:
        return []

    selectors = [
        # legacy 다나와
        ".prod_pricelist > ul > li",
        ".prod_pricelist li",
        ".prod_pricelist_item",

        # modern 다나와
        "[data-testid='ProductListPriceCompare'] > ul > li",
        "[data-testid='ProductListPriceCompare'] ul > li",
        "[data-testid='ProductListPriceCompare'] li",
        ".dnw-product-price ul > li",
        ".dnw-product-price li",

        # DOM 변경 대비 fallback
        "[class*='pricelist'] li",
        "[class*='price-list'] li",
        "[class*='price_compare'] li",
    ]

    option_rows = []
    seen = set()

    for selector in selectors:
        for row in product.select(selector):
            # 같은 BeautifulSoup Tag가 여러 selector에 잡히는 것을 방지
            row_key = id(row)
            if row_key in seen:
                continue
            seen.add(row_key)
            option_rows.append(row)

    variants = []

    for option_row in option_rows:
        row_text = normalize_space(option_row.get_text(" ", strip=True))
        capacity, label = extract_ram_capacity_label(row_text)
        module_count = extract_ram_module_count(row_text)

        if capacity is None or not (2 <= capacity <= 4096):
            continue

        price = extract_ram_total_price_from_option_row(option_row)
        if price is None or is_noise_by_price("RAM", price):
            continue

        variants.append({
            "capacity": int(capacity),
            "module_count": int(module_count) if module_count is not None else None,
            "label": label or f"{capacity}GB",
            "price": int(price),
        })

    # 같은 총용량이라도 32GBx1 / 16GBx2처럼 모듈 구성이 다르면
    # 서로 다른 상품 구성이므로 별도 variant로 유지한다.
    # 동일한 (총용량, 모듈개수)만 중복일 때 최저 총가격을 유지한다.
    best_by_variant = {}
    for variant in variants:
        key = (
            variant["capacity"],
            variant.get("module_count"),
        )
        current = best_by_variant.get(key)
        if current is None or variant["price"] < current["price"]:
            best_by_variant[key] = variant

    return sorted(
        best_by_variant.values(),
        key=lambda item: (
            item["capacity"],
            item.get("module_count") or 0,
        ),
        reverse=True,
    )


def normalize_ram_variant_base_name(name):
    """옵션별 행을 만들기 전에 상품명 끝의 기존 용량 표기를 제거한다."""
    name = normalize_space(name)
    if not name:
        return name

    # ... (64GB)
    name = re.sub(
        r"\s*\(\s*\d+(?:\.\d+)?\s*(?:GB|TB)\s*\)\s*$",
        "",
        name,
        flags=re.I,
    )

    # ... 64GB(32GBx2)
    name = re.sub(
        r"\s+\d+(?:\.\d+)?\s*(?:GB|TB)\s*\([^)]*[xX][^)]*\)\s*$",
        "",
        name,
        flags=re.I,
    )

    # ... 64GB
    name = re.sub(
        r"\s+\d+(?:\.\d+)?\s*(?:GB|TB)\s*$",
        "",
        name,
        flags=re.I,
    )

    return normalize_space(name)


def extract_ram_single_total_price(product):
    """
    다중 옵션이 없는 RAM에서 실제 총가격을 추출한다.
    /1GB 단가는 제외하고 strong의 정상 가격을 우선한다.
    """
    if product is None:
        return None

    minimum = MIN_PRICE_BY_CATEGORY["RAM"]
    maximum = MAX_PRICE_BY_CATEGORY["RAM"]
    candidates = []

    for tag in product.select(
        ".price_sect strong, "
        ".prod_pricelist strong, "
        "[class*='price'] strong, "
        "strong"
    ):
        value = _parse_price(tag.get_text(" ", strip=True))
        if value is None:
            continue

        context = normalize_space(
            tag.parent.get_text(" ", strip=True)
            if tag.parent
            else tag.get_text(" ", strip=True)
        )
        if is_ram_unit_price_text(context):
            continue

        if minimum <= value <= maximum:
            candidates.append(value)

    return max(candidates) if candidates else None


def extract_ram_base_specs(name, spec_list):
    """RAM의 용량을 제외한 DDR 타입과 메모리 클럭을 추출한다."""
    combined_text = " / ".join(spec_list or [])

    name_memory = re.search(r"DDR[45]", name or "", re.I)
    if name_memory:
        memory_type = name_memory.group(0).upper()
    else:
        m = re.search(r"DDR[45]", combined_text, re.I)
        memory_type = m.group(0).upper() if m else None

    memory_clock = extract_memory_clock(
        name=name,
        spec_text=combined_text,
        category="RAM",
    )

    module_count = extract_ram_module_count(
        f"{name or ''} / {combined_text}"
    )

    if memory_type not in {"DDR4", "DDR5"} or memory_clock is None:
        return None

    return {
        "memory_type": memory_type,
        "memory_clock": int(memory_clock),
        "module_count": int(module_count) if module_count is not None else None,
    }


def build_ram_rows_from_product(
    product, base_name, spec_list, full_text,
    product_code=None, product_url=None,
):
    """
    RAM 상품 카드 하나를 CSV/DB용 1개 이상의 행으로 확장한다.

    1) 용량별 가격 옵션이 있으면 각 옵션을 별도 행으로 생성한다.
    2) 옵션 추출이 안 됐을 때 카드에 용량이 여러 개 보이면 저장하지 않는다.
       이 경우 임의의 최대 용량 + 최소 가격 조합을 만들지 않는다.
    3) 단일 용량 제품만 안전하게 fallback 처리한다.
    """
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    base_specs = extract_ram_base_specs(base_name, spec_list)
    if base_specs is None:
        return [], "spec-missing"

    # -----------------------------------------------------
    # 1) 같은 옵션 행의 capacity-price 쌍을 최우선 사용
    # -----------------------------------------------------
    options = extract_ram_capacity_price_options(product)
    if options:
        rows = []
        variant_base_name = normalize_ram_variant_base_name(base_name)
        if not variant_base_name:
            variant_base_name = normalize_space(base_name)

        for option in options:
            capacity = option.get("capacity")
            price = option.get("price")
            label = option.get("label") or f"{capacity}GB"

            module_count = option.get("module_count")
            if module_count is None:
                module_count = base_specs.get("module_count")

            if capacity is None or price is None or module_count is None:
                continue
            if is_noise_by_price("RAM", price):
                continue

            rows.append({
                "name": f"{variant_base_name} ({label})",
                "price": int(price),
                "memory_type": base_specs["memory_type"],
                "memory_clock": int(base_specs["memory_clock"]),
                "capacity": int(capacity),
                "module_count": int(module_count),
                "product_code": product_code,
                "product_url": product_url,
                "created_at": created_at,
            })

        if rows:
            return rows, "multi-option"

    # -----------------------------------------------------
    # 2) 옵션 파싱 실패 시 단일 용량인지 확인
    # -----------------------------------------------------
    card_capacities = sorted(set(extract_ram_capacity_candidates(full_text)))
    name_capacities = sorted(set(extract_ram_capacity_candidates(base_name)))

    capacity = name_capacities[0] if len(name_capacities) == 1 else None

    if capacity is None:
        # spec_list에 full_text가 fallback으로 들어갈 수 있으므로
        # full_text와 완전히 같은 항목은 제외하고 스펙 용량을 검사한다.
        normalized_full = normalize_space(full_text)
        clean_specs = [
            normalize_space(item)
            for item in (spec_list or [])
            if normalize_space(item)
            and normalize_space(item) != normalized_full
        ]
        spec_text = " / ".join(clean_specs)
        spec_capacities = sorted(set(extract_ram_capacity_candidates(spec_text)))
        if len(spec_capacities) == 1:
            capacity = spec_capacities[0]

    # 카드에는 여러 옵션이 있는데 특정 단일 용량을 확정하지 못했다면 제거
    if len(card_capacities) > 1 and capacity is None:
        return [], "ambiguous-capacity"

    if capacity is None:
        if len(card_capacities) == 1:
            capacity = card_capacities[0]
        else:
            return [], "capacity-missing"

    # -----------------------------------------------------
    # 3) 단일 RAM의 실제 총가격 추출
    # -----------------------------------------------------
    price = extract_ram_single_total_price(product)
    if price is None:
        return [], "price-missing"
    if is_noise_by_price("RAM", price):
        return [], "invalid-price"

    module_count = base_specs.get("module_count")
    if module_count is None:
        return [], "module-count-missing"

    variant_name = normalize_space(base_name)
    if not extract_ram_capacity_candidates(variant_name):
        variant_name = f"{variant_name} ({capacity}GB)"

    return [{
        "name": variant_name,
        "price": int(price),
        "memory_type": base_specs["memory_type"],
        "memory_clock": int(base_specs["memory_clock"]),
        "capacity": int(capacity),
        "module_count": int(module_count),
        "product_code": product_code,
        "product_url": product_url,
        "created_at": created_at,
    }], "single"

def _ssd_capacity_candidates(text):
    """문자열 하나에서 SSD 용량 후보를 모두 GB 단위로 반환."""
    text = normalize_capacity_text(text)
    candidates = []

    for m in re.finditer(
        r"(?<![A-Z0-9])(\d+(?:\.\d+)?)\s*(TB|GB)(?![A-Z0-9])",
        text,
        re.I,
    ):
        value = capacity_to_gb(m.group(1), m.group(2))
        if value is not None and SSD_MIN_CAPACITY_GB <= value <= SSD_MAX_CAPACITY_GB:
            candidates.append(value)

    return candidates


def _pick_ssd_capacity(candidates):
    if not candidates:
        return None

    # 중복 제거
    candidates = list(dict.fromkeys(candidates))

    # 가장 흔한 실제 SSD 용량을 우선한다.
    common = [v for v in candidates if v in COMMON_SSD_CAPACITIES_GB]
    if common:
        return max(common)

    # DRAM 캐시(예: 1GB, 2GB)와 256GB 미만 저용량 SSD는 최소 용량 필터로 제거됨.
    # 남은 후보 중 가장 큰 값을 본 저장 용량으로 사용한다.
    return max(candidates)


def extract_ssd_capacity(name=None, spec_text=None, full_text=None):
    """
    SSD 용량 추출 우선순위

    1. 상품명: 가장 신뢰도가 높음
       - 삼성전자 990 PRO M.2 NVMe (2TB) -> 2000
       - WD BLACK SN850X 1TB -> 1000

    2. 스펙의 명시적 '용량' 필드

    3. 스펙 전체 / 카드 전체 텍스트 fallback

    기존 문제:
    legacy SSD에서 .spec_list가 축약되거나 용량 링크가 누락되면
    대부분 capacity=None으로 떨어졌음.
    """
    # -----------------------------------------------------
    # 1) 상품명 우선
    # -----------------------------------------------------
    name_candidates = _ssd_capacity_candidates(name)
    capacity = _pick_ssd_capacity(name_candidates)
    if capacity is not None:
        return capacity

    # -----------------------------------------------------
    # 2) 명시적 용량 필드
    # -----------------------------------------------------
    for text in [spec_text, full_text]:
        if not text:
            continue

        normalized = normalize_capacity_text(text)
        label_patterns = [
            r"SSD\s*용량\s*[:：/]?\s*(\d+(?:\.\d+)?)\s*(TB|GB)",
            r"저장\s*용량\s*[:：/]?\s*(\d+(?:\.\d+)?)\s*(TB|GB)",
            r"저장용량\s*[:：/]?\s*(\d+(?:\.\d+)?)\s*(TB|GB)",
            r"용량\s*[:：/]?\s*(\d+(?:\.\d+)?)\s*(TB|GB)",
        ]

        for pattern in label_patterns:
            m = re.search(pattern, normalized, re.I)
            if not m:
                continue

            value = capacity_to_gb(m.group(1), m.group(2))
            if value is not None and SSD_MIN_CAPACITY_GB <= value <= SSD_MAX_CAPACITY_GB:
                return value

    # -----------------------------------------------------
    # 3) 전체 문자열 fallback
    # -----------------------------------------------------
    all_candidates = []
    all_candidates.extend(_ssd_capacity_candidates(spec_text))
    all_candidates.extend(_ssd_capacity_candidates(full_text))
    return _pick_ssd_capacity(all_candidates)


def _format_ssd_capacity_label(capacity_gb):
    """GB 정수 용량을 상품명 표시용 문자열로 변환한다."""
    if capacity_gb is None:
        return None

    if capacity_gb % 1000 == 0:
        return f"{capacity_gb // 1000}TB"

    # 1.92TB 같은 제조사 표기는 원문을 우선 사용하지만,
    # 원문 라벨을 얻지 못한 경우에는 GB로 표시한다.
    return f"{capacity_gb}GB"


def _extract_price_candidates_from_text(text, minimum_price=None):
    """문자열에서 원화 가격 후보를 정수로 반환한다."""
    if not text:
        return []

    minimum_price = minimum_price or MIN_PRICE_BY_CATEGORY["SSD"]
    result = []

    for raw in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{4,})\s*원", str(text)):
        value = _parse_price(raw)
        if value is not None and value >= minimum_price:
            result.append(value)

    return result


def extract_ssd_capacity_price_options(product):
    """
    modern 다나와 SSD 카드의 "용량별 가격비교" 영역에서
    용량과 가격을 같은 <li> 행 기준으로 묶어서 추출한다.

    실제 DOM 예:
        <div data-testid="ProductListPriceCompare">
            <ul>
                <li>
                    <span>4TB</span>
                    <a aria-label="4TB 891,000원">...</a>
                </li>
                <li>
                    <span>2TB</span>
                    <a aria-label="2TB 467,800원">...</a>
                </li>
                ...
            </ul>
        </div>

    중요:
    - 카드 전체 텍스트에서 "가장 큰 용량 + 가장 싼 가격"을 조합하지 않는다.
    - 반드시 같은 옵션 행 안에서 capacity와 price를 함께 추출한다.
    - aria-label을 최우선으로 사용하고, 없을 때만 anchor 텍스트를 fallback한다.
    """
    if product is None:
        return []

    capacity_pattern = re.compile(r"^(\d+(?:\.\d+)?)\s*(TB|GB)$", re.I)
    min_price = MIN_PRICE_BY_CATEGORY["SSD"]

    price_compare = product.select_one(
        "[data-testid='ProductListPriceCompare'], "
        ".dnw-product-price"
    )
    if price_compare is None:
        return []

    variants = []

    # 실제 옵션은 ul > li 한 줄에 "용량 / 가격 / 몰수"가 함께 존재한다.
    option_rows = price_compare.select("ul > li")
    if not option_rows:
        option_rows = price_compare.select("li")

    for option_row in option_rows:
        capacity = None
        label = None

        # -------------------------------------------------
        # 1) 이 행의 용량 라벨 추출
        # -------------------------------------------------
        for tag in option_row.find_all(["span", "div"], recursive=True):
            token = normalize_space(tag.get_text(" ", strip=True))
            match = capacity_pattern.fullmatch(token)
            if not match:
                continue

            value = capacity_to_gb(match.group(1), match.group(2))
            if value is None:
                continue
            if not (SSD_MIN_CAPACITY_GB <= value <= SSD_MAX_CAPACITY_GB):
                continue

            capacity = int(value)
            label = f"{match.group(1)}{match.group(2).upper()}"
            break

        # 용량 라벨이 별도 span에 없는 변형 DOM은 aria-label에서 보완.
        if capacity is None:
            for anchor in option_row.select("a[aria-label]"):
                aria = normalize_space(anchor.get("aria-label", ""))
                match = re.search(
                    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(TB|GB)(?![\w/])",
                    aria,
                    re.I,
                )
                if not match:
                    continue

                value = capacity_to_gb(match.group(1), match.group(2))
                if value is None:
                    continue
                if not (SSD_MIN_CAPACITY_GB <= value <= SSD_MAX_CAPACITY_GB):
                    continue

                capacity = int(value)
                label = f"{match.group(1)}{match.group(2).upper()}"
                break

        if capacity is None:
            continue

        # -------------------------------------------------
        # 2) 같은 행 안의 가격 추출
        # -------------------------------------------------
        price = None

        # aria-label="4TB 891,000원" 구조가 현재 modern DOM에서 가장 정확하다.
        for anchor in option_row.select("a[aria-label]"):
            aria = normalize_space(anchor.get("aria-label", ""))

            # 다른 용량의 링크가 섞이는 것을 막기 위해
            # 현재 option label이 aria-label에 있을 때 우선 사용한다.
            if label and label.upper() not in aria.upper().replace(" ", ""):
                # aria-label에 공백이 낄 수 있어 정규식으로 한 번 더 확인
                cap_match = re.search(
                    r"(\d+(?:\.\d+)?)\s*(TB|GB)",
                    aria,
                    re.I,
                )
                if cap_match:
                    aria_capacity = capacity_to_gb(cap_match.group(1), cap_match.group(2))
                    if aria_capacity != capacity:
                        continue

            prices = _extract_price_candidates_from_text(aria, min_price)
            if prices:
                price = min(prices)
                break

        # aria-label이 없는 경우 anchor 내부의 "891,000 원" 텍스트 사용.
        if price is None:
            for anchor in option_row.select("a"):
                anchor_text = normalize_space(anchor.get_text(" ", strip=True))
                prices = _extract_price_candidates_from_text(anchor_text, min_price)
                if prices:
                    price = min(prices)
                    break

        # 마지막 fallback: 행 전체 텍스트.
        # 단, "223원/1GB"는 10,000원 미만이므로 자동 제외된다.
        if price is None:
            row_text = normalize_space(option_row.get_text(" ", strip=True))
            prices = _extract_price_candidates_from_text(row_text, min_price)
            if prices:
                price = min(prices)

        if price is None:
            continue

        variants.append(
            {
                "capacity": int(capacity),
                "label": label or _format_ssd_capacity_label(capacity),
                "price": int(price),
            }
        )

    # 같은 용량이 카드 안에서 중복 노출될 경우 용량별 최저가만 유지.
    best_by_capacity = {}
    for variant in variants:
        current = best_by_capacity.get(variant["capacity"])
        if current is None or variant["price"] < current["price"]:
            best_by_capacity[variant["capacity"]] = variant

    return sorted(
        best_by_capacity.values(),
        key=lambda item: item["capacity"],
        reverse=True,
    )

def _normalize_ssd_variant_base_name(name):
    """
    다중 용량 옵션을 별도 행으로 만들 때 대표 상품명에 붙은 용량 표기를 제거한다.

    예:
      삼성전자 990 PRO M.2 NVMe (4TB) -> 삼성전자 990 PRO M.2 NVMe
      WD BLACK SN850X 4TB              -> WD BLACK SN850X

    상품명 중간의 숫자는 모델명일 수 있으므로 맨 끝의 GB/TB 표기만 제거한다.
    """
    name = normalize_space(name)
    if not name:
        return name

    # 끝의 괄호형 용량: "... (4TB)"
    name = re.sub(
        r"\s*\(\s*\d+(?:\.\d+)?\s*(?:TB|GB)\s*\)\s*$",
        "",
        name,
        flags=re.I,
    )

    # 끝의 일반 용량: "... 4TB"
    name = re.sub(
        r"\s+\d+(?:\.\d+)?\s*(?:TB|GB)\s*$",
        "",
        name,
        flags=re.I,
    )

    return normalize_space(name)


def build_ssd_rows_from_product(
    product, base_name, base_price, spec_list, full_text,
    product_code=None, product_url=None,
):
    """
    SSD 상품 카드 하나를 DB/CSV용 1개 이상의 행으로 확장한다.

    V20.1 우선순위

    1) 용량별 가격 옵션이 있으면 각 옵션을 별도 행으로 먼저 생성
       예: 4TB / 2TB / 1TB / 500GB -> 4행

       중요:
       modern canonical 상품명이 대표 용량을 포함하더라도
       상품명 용량을 먼저 보고 단일 제품으로 확정하지 않는다.

    2) 용량별 가격 옵션이 없을 때만 상품명에 포함된 용량을 단일 제품으로 사용
       예: 키오시아 ... (1TB)

    3) 둘 다 없으면 스펙/카드 전체 텍스트에서 용량 1개를 fallback 추출
    """
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # -----------------------------------------------------
    # 1) 다중 용량 옵션을 최우선으로 각각 별도 상품으로 확장
    # -----------------------------------------------------
    options = extract_ssd_capacity_price_options(product)
    if options:
        rows = []
        variant_base_name = _normalize_ssd_variant_base_name(base_name)
        if not variant_base_name:
            variant_base_name = normalize_space(base_name)

        for option in options:
            option_price = option.get("price")
            option_capacity = option.get("capacity")

            if option_price is None or option_capacity is None:
                continue

            # 최소/최대 가격 범위를 모두 적용한다.
            if is_noise_by_price("SSD", option_price):
                continue

            label = option.get("label") or _format_ssd_capacity_label(option_capacity)
            variant_name = f"{variant_base_name} ({label})"

            rows.append(
                {
                    "name": variant_name,
                    "price": int(option_price),
                    "capacity": int(option_capacity),
                    "product_code": product_code,
                    "product_url": product_url,
                    "created_at": created_at,
                }
            )

        if rows:
            return rows, "multi-option"

    # -----------------------------------------------------
    # 2) 다중 옵션이 없을 때만 상품명 용량을 단일 상품으로 사용
    # -----------------------------------------------------
    name_capacity = _pick_ssd_capacity(_ssd_capacity_candidates(base_name))
    if name_capacity is not None:
        if base_price is None or is_noise_by_price("SSD", base_price):
            return [], "single-invalid-price"

        return [
            {
                "name": base_name,
                "price": int(base_price),
                "capacity": int(name_capacity),
                "product_code": product_code,
                "product_url": product_url,
                "created_at": created_at,
            }
        ], "single-name"

    # -----------------------------------------------------
    # 3) 마지막 fallback: 스펙/카드 전체에서 저장용량 1개 추출
    # -----------------------------------------------------
    combined_text = " / ".join(spec_list or [])
    capacity = extract_ssd_capacity(
        name=base_name,
        spec_text=combined_text,
        full_text=full_text,
    )

    if capacity is None:
        return [], "capacity-missing"

    if base_price is None or is_noise_by_price("SSD", base_price):
        return [], "fallback-invalid-price"

    return [
        {
            "name": f"{base_name} ({_format_ssd_capacity_label(capacity)})",
            "price": int(base_price),
            "capacity": int(capacity),
            "product_code": product_code,
            "product_url": product_url,
            "created_at": created_at,
        }
    ], "fallback"


# =========================================================
# Case 전용 추출
# =========================================================
CASE_SIZE_ORDER = {"EATX": 4, "ATX": 3, "MATX": 2, "ITX": 1}


def normalize_case_size_token(value):
    if not value:
        return None

    value = str(value).upper().replace(" ", "")
    mapping = {
        "E-ATX": "EATX",
        "EATX": "EATX",
        "ATX": "ATX",
        "M-ATX": "MATX",
        "MATX": "MATX",
        "MICRO-ATX": "MATX",
        "MINI-ITX": "ITX",
        "M-ITX": "ITX",
        "ITX": "ITX",
    }
    return mapping.get(value)


def extract_case_size(text):
    if not text:
        return None

    text = normalize_space(text)

    section = re.search(
        r"지원보드규격\s*(?:[:：]|/)?\s*(.+?)(?=VGA\s*길이|GPU\s*길이|그래픽카드|CPU\s*쿨러|\[패널\]|\[크기\]|\[호환성\]|$)",
        text,
        re.I,
    )

    source = section.group(1) if section else text
    tokens = re.findall(r"E-ATX|EATX|MICRO-ATX|M-ATX|MATX|MINI-ITX|M-ITX|ITX|ATX", source, re.I)
    normalized = [normalize_case_size_token(x) for x in tokens]
    normalized = [x for x in normalized if x]

    return max(normalized, key=lambda x: CASE_SIZE_ORDER[x]) if normalized else None


def extract_case_gpu_length(text):
    if not text:
        return None

    patterns = [
        r"VGA\s*길이[^0-9]{0,20}([0-9]{2,3}(?:\.\d+)?)\s*~\s*([0-9]{2,3}(?:\.\d+)?)\s*mm",
        r"VGA\s*길이[^0-9]{0,20}([0-9]{2,3}(?:\.\d+)?)\s*mm",
        r"GPU\s*길이[^0-9]{0,20}([0-9]{2,3}(?:\.\d+)?)\s*mm",
        r"그래픽카드[^0-9]{0,30}([0-9]{2,3}(?:\.\d+)?)\s*mm",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            values = [float(v) for v in m.groups() if v is not None]
            if values:
                return int(max(values))

    return None


def extract_case_cooler_length(text):
    if not text:
        return None

    patterns = [
        r"CPU\s*쿨러\s*높이[^0-9]{0,20}([0-9]{2,3}(?:\.\d+)?)\s*mm",
        r"CPU쿨러\s*높이[^0-9]{0,20}([0-9]{2,3}(?:\.\d+)?)\s*mm",
        r"쿨러\s*높이[^0-9]{0,20}([0-9]{2,3}(?:\.\d+)?)\s*mm",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            value = int(float(m.group(1)))
            if 80 <= value <= 250:
                return value

    # 다나와의 158m 같은 단위 오타 제한 보정
    m = re.search(r"CPU\s*쿨러\s*높이[^0-9]{0,20}([0-9]{3})\s*m(?!m)", text, re.I)
    if m:
        value = int(m.group(1))
        if 80 <= value <= 250:
            return value

    return None


def classify_invalid_case(name, full_text=None):
    """
    자동 견적에서 제외할 케이스/케이스 부속품을 분류한다.

    중요:
    - 케이스 액세서리 판정은 상품명만 검사한다.
      정상 케이스의 스펙에 "Vertical GPU", "Riser Cable", "Screen" 같은
      지원 기능 문구가 포함되어 있어도 본체 케이스를 오탐 제거하지 않는다.
    - 랙마운트/서버/산업용 판정은 상품명 + 전체 스펙을 검사한다.
      상품명에 랙마운트라는 단어가 없어도 스펙의 4U/6U 등을 잡기 위함이다.
    """
    if not name:
        return "invalid"

    # -----------------------------------------------------
    # 1) 케이스가 아니라 부속품 자체인 상품은 상품명 기준으로만 제거
    # -----------------------------------------------------
    name_upper = normalize_space(name).upper()

    accessory_name_keywords = [
        "라이저 케이블",
        "RISER CABLE",
        "GPU BRACKET",
        "브라켓 KIT",
        "VERTICAL GPU",
        "멀티 브라켓",
        "DISPLAY UPGRADE",
        "채굴",
        "MINING",
    ]

    if any(keyword.upper() in name_upper for keyword in accessory_name_keywords):
        return "accessory"

    # -----------------------------------------------------
    # 2) 랙마운트/서버/산업용 섀시는 상품명 + 전체 스펙 기준으로 제거
    # -----------------------------------------------------
    source = normalize_space(f"{name or ''} / {full_text or ''}")
    upper = source.upper()

    rack_keywords = [
        "랙마운트",
        "RACKMOUNT",
        "RACK MOUNT",
        "SERVER CHASSIS",
        "서버용 케이스",
        "서버 케이스",
        "산업용 케이스",
    ]

    if any(keyword.upper() in upper for keyword in rack_keywords):
        return "rackmount"

    # 1U~8U는 일반적으로 랙마운트 섀시 높이 표기다.
    if re.search(
        r"(?<![A-Z0-9])(?:1|2|3|4|5|6|7|8)U(?![A-Z0-9])",
        upper,
    ):
        return "rackmount"

    # IPC가 독립 토큰으로 표시된 산업용 PC 섀시도 자동 견적에서 제외한다.
    if re.search(r"(?<![A-Z0-9])IPC(?![A-Z0-9])", upper):
        return "rackmount"

    return None


def is_invalid_case_name(name, full_text=None):
    return classify_invalid_case(name, full_text) is not None


# =========================================================
# 상품 스펙 파싱
# =========================================================
def extract_spec_list_from_product(product):
    """legacy 상품 카드 스펙 추출."""
    spec_list = []

    for selector in [
        ".spec_list .view_dic",
        ".spec_list a",
        ".spec_list span",
        ".spec_list",
        ".prod_spec_set",
    ]:
        for tag in product.select(selector):
            text = normalize_space(tag.get_text(" ", strip=True))
            if text and text not in spec_list:
                spec_list.append(text)

    # SSD에서 spec_list가 축약되는 경우를 위한 마지막 fallback.
    # 다른 카테고리에도 안전하게 전체 상품 카드 텍스트를 추가한다.
    full_text = normalize_space(product.get_text(" ", strip=True))
    if full_text and full_text not in spec_list:
        spec_list.append(full_text)

    return spec_list


def extract_refined_spec(spec_list, category, name=None, full_text=None):
    res = {}
    combined_text = " / ".join(spec_list)

    def search(pattern, flags=0):
        return re.search(pattern, combined_text, flags)

    def findall(pattern, flags=0):
        return re.findall(pattern, combined_text, flags)

    def normalize_size(value):
        if not value:
            return None
        value = value.upper()
        mapping = {
            "M-ATX": "MATX",
            "MICRO-ATX": "MATX",
            "MINI-ITX": "ITX",
            "E-ATX": "EATX",
        }
        return mapping.get(value, value.replace("-", ""))

    if category == "CPU":
        m = search(r"소켓\s*[:\-]?\s*([a-zA-Z0-9\-]+)", re.I)
        res["socket_type"] = normalize_cpu_mainboard_socket(m.group(1)) if m else None
        if not is_valid_recommend_socket("CPU", res["socket_type"]):
            return None

        memory = findall(r"DDR[0-9]+", re.I)
        res["memory_type"] = memory[0].upper() if memory else None

    elif category == "GPU":
        gpu_text = f"{name or ''} / {combined_text}"
        res["recommended_power"] = extract_recommended_power(gpu_text)
        if res["recommended_power"] is None:
            return None

        res["pcie_type"] = extract_pcie_type(gpu_text)
        m = search(r"가로\s*\(?길이\)?[^0-9]*([\d.]+)\s*mm", re.I)
        if not m:
            m = search(r"길이[^0-9]*([\d.]+)\s*mm", re.I)
        res["gpu_length"] = int(float(m.group(1))) if m else None

    elif category == "Mainboard":
        m = search(r"소켓\s*[:\-]?\s*([a-zA-Z0-9\-]+)", re.I)
        res["socket_type"] = normalize_cpu_mainboard_socket(m.group(1)) if m else None
        if not is_valid_recommend_socket("Mainboard", res["socket_type"]):
            return None

        memory = findall(r"DDR[0-9]+", re.I)
        res["memory_type"] = memory[0].upper() if memory else None
        if res["memory_type"] not in {"DDR4", "DDR5"}:
            return None

        res["pcie_type"] = extract_pcie_type(combined_text)

        m = search(r"(E-ATX|ATX|M-ATX|Micro-ATX|Mini-ITX)", re.I)
        res["size"] = normalize_size(m.group(0)) if m else None

        res["memory_clock"] = extract_memory_clock(
            name=name,
            spec_text=combined_text,
            category="Mainboard",
        )

    elif category == "RAM":
        ram_text = f"{name or ''} / {combined_text}"

        # 상품명 DDR4/DDR5를 우선 사용해 주변의 다른 DDR 문자열 오염을 방지한다.
        name_memory = re.search(r"DDR[45]", name or "", re.I)
        if name_memory:
            res["memory_type"] = name_memory.group(0).upper()
        else:
            memory = findall(r"DDR[45]", re.I)
            res["memory_type"] = memory[0].upper() if memory else None

        res["memory_clock"] = extract_memory_clock(
            name=name,
            spec_text=combined_text,
            category="RAM",
        )
        res["capacity"] = extract_ram_capacity(ram_text)
        res["module_count"] = extract_ram_module_count(ram_text)

        if res["memory_type"] not in {"DDR4", "DDR5"}:
            return None
        if any(
            res.get(k) is None
            for k in ["memory_type", "memory_clock", "capacity", "module_count"]
        ):
            return None

    elif category == "SSD":
        # 핵심 수정:
        # combined_text 하나만 보지 않고 상품명 -> 스펙 -> 카드 전체 순서로 추출.
        res["capacity"] = extract_ssd_capacity(
            name=name,
            spec_text=combined_text,
            full_text=full_text,
        )

        if res["capacity"] is None:
            return None

    elif category == "Power":
        res["size"] = extract_power_size(
            name=name,
            spec_text=combined_text,
        )

        m = search(r"([0-9]{3,4})\s*W", re.I)
        res["wattage"] = int(m.group(1)) if m else None

    elif category == "Case":
        source = full_text or combined_text
        res["size"] = extract_case_size(source)
        res["gpu_length"] = extract_case_gpu_length(source)
        res["cooler_length"] = extract_case_cooler_length(source)

        if ENABLE_STRICT_CASE_COMPATIBILITY_FILTER:
            if any(res.get(k) is None for k in CASE_REQUIRED_COMPATIBILITY_FIELDS):
                return None

    elif category == "Cooler":
        cooler_text = f"{name or ''} / {combined_text}"
        if "공랭" not in cooler_text:
            return None

        res["socket_type"] = extract_cooler_socket_list(cooler_text)
        if not res["socket_type"]:
            return None

        m = re.search(r"높이[^0-9]*([\d.]+)\s*mm", combined_text, re.I)
        res["cooler_length"] = int(float(m.group(1))) if m else None
        if res["cooler_length"] is None:
            return None

    return res


# =========================================================
# SSD 상품명 / 노이즈 필터
# =========================================================
SSD_BENEFIT_NAME_PATTERNS = (
    r"^\s*[\"']?\d{1,3}(?:,\d{3})+\s*원\s*\[",
    r"\[(?:SSG\.COM|신세계몰|롯데ON|하이마트|옥션|G마켓|오늘의집|11번가|쿠팡)\]",
    r"(?:BC|KB국민|삼성|롯데|현대|신한|NH|우리|하나)카드",
    r"무이자\s*최대",
    r"^\s*\d(?:\.\d)?\s*리뷰수\b",
    r"\b리뷰수\s*\(",
)

SSD_ACCESSORY_KEYWORDS = (
    "케이블",
    "컨버터",
    "젠더",
    "변환기",
    "변환 어댑터",
    "변환어댑터",
    "외장케이스",
    "외장 케이스",
    "하드케이스",
    "하드 케이스",
    "도킹스테이션",
    "도킹 스테이션",
    "클로너",
)


def is_unsupported_ssd_interface(text):
    """일반 데스크톱 자동 견적에서 제외할 SSD 인터페이스인지 확인한다."""
    if not text:
        return False

    upper = normalize_space(text).upper()

    # mSATA / m-SATA / m.SATA만 차단한다.
    # M.2 SATA는 이 패턴에 걸리지 않는다.
    return bool(
        re.search(
            r"(?<![A-Z0-9])M[\s.\-]?SATA(?![A-Z0-9])",
            upper,
            re.I,
        )
    )


def extract_modern_ssd_name(product):
    """SSD도 공통 canonical 이미지 상품명 추출기를 사용한다."""
    return extract_modern_canonical_name(product)


def classify_invalid_ssd_name(name):
    """
    추천 데이터에서 제외할 SSD 이름을 분류한다.

    반환값:
      None        -> 정상
      benefit     -> 카드혜택/쇼핑몰/리뷰 문구
      used        -> 중고 제품
      accessory   -> 케이블/컨버터/젠더 등 SSD가 아닌 액세서리
      invalid     -> 기타 명백한 비상품명

    병행수입/해외구매/벌크는 정상 제품으로 유지한다.
    """
    name = normalize_space(name)
    if not name:
        return "invalid"

    if "중고" in name:
        return "used"

    general_reason = classify_invalid_product_name("SSD", name)
    if general_reason in {"enterprise", "benefit", "used"}:
        return general_reason

    for pattern in SSD_BENEFIT_NAME_PATTERNS:
        if re.search(pattern, name, re.I):
            return "benefit"

    lowered = name.lower()
    for keyword in SSD_ACCESSORY_KEYWORDS:
        if keyword.lower() in lowered:
            return "accessory"

    # "4.7 리뷰수 (999+)" 같은 메타 문자열 방어.
    if re.fullmatch(r"\d(?:\.\d)?\s*리뷰수.*", name, re.I):
        return "benefit"

    # 실제 제품명 없이 가격/혜택만 있는 문자열 방어.
    if re.match(r"^\d{1,3}(?:,\d{3})+원\b", name):
        return "benefit"

    return None



# =========================================================
# 다나와 상품 고유 식별자 / 실제 상품 URL
# =========================================================
DANAWA_BASE_URL = "https://prod.danawa.com"


def normalize_product_url(url):
    """상대경로/프로토콜 생략 링크를 다나와 절대 URL로 정규화한다."""
    url = normalize_space(url)
    if not url or url.lower().startswith("javascript:"):
        return None

    if url.startswith("//"):
        return "https:" + url

    return urljoin(DANAWA_BASE_URL, url)


def extract_product_code_from_url(url):
    """다나와 상품 URL의 pcode를 추출한다."""
    if not url:
        return None

    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        for key in ("pcode", "PCode", "productCode", "productcode"):
            values = query.get(key)
            if values and values[0]:
                value = re.sub(r"[^0-9A-Za-z_-]", "", str(values[0]))
                return value or None
    except Exception:
        pass

    m = re.search(r"(?:[?&]|\\b)pcode=([0-9A-Za-z_-]+)", str(url), re.I)
    return m.group(1) if m else None


def extract_product_identity(product, mode=None):
    """
    상품 카드에서 다나와 고유 상품번호(product_code)와 실제 상품 URL을 추출한다.

    우선순위:
      1) 상품명/대표 이미지 영역의 pcode 링크
      2) 카드 내부의 모든 pcode 링크
      3) data-* 속성의 상품 코드

    RAM/SSD의 용량별 variant는 같은 원본 상품 카드에서 파생되므로
    동일 product_code / product_url을 공유한다.
    """
    if product is None:
        return None, None

    selectors = [
        ".prod_name a[href]",
        "[data-testid='ProductListBaseProductImage'] a[href]",
        ".dnw-product-image a[href]",
        ".dnw-product-info a[href*='pcode=']",
        "[class*='product-info'] a[href*='pcode=']",
        "a[href*='pcode=']",
    ]

    candidates = []
    seen = set()

    for selector in selectors:
        for anchor in product.select(selector):
            href = normalize_product_url(anchor.get("href"))
            if not href or href in seen:
                continue
            seen.add(href)

            code = extract_product_code_from_url(href)
            score = 0
            if code:
                score += 200
            if "prod.danawa.com/info" in href.lower():
                score += 80
            if "pcode=" in href.lower():
                score += 50
            if selector.startswith(".prod_name"):
                score += 60
            if "ProductListBaseProductImage" in selector or "dnw-product-image" in selector:
                score += 40

            candidates.append((score, code, href))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, code, url = candidates[0]
        if code:
            return str(code), url

    # DOM이 바뀌어 href에서 못 찾는 경우 data-* 속성 fallback
    for tag in [product] + list(product.find_all(True)):
        attrs = getattr(tag, "attrs", {}) or {}
        for key, value in attrs.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key not in {
                "pcode", "productcode", "productid", "productno", "prodcode"
            }:
                continue

            if isinstance(value, (list, tuple)):
                value = value[0] if value else None
            value = normalize_space(value)
            if not value:
                continue

            code_match = re.search(r"[0-9A-Za-z_-]+", value)
            if code_match:
                return code_match.group(0), None

    return None, None

# =========================================================
# 상품명 / 가격
# =========================================================
def is_legacy_product(product):
    classes = set(product.get("class") or [])
    return product.name == "li" and (
        str(product.get("id", "")).startswith("productItem")
        or "prod_item" in classes
    )


def extract_modern_canonical_name(product):
    """
    모든 modern 카테고리에서 이미지 영역의 canonical 상품명을 최우선 사용한다.
    V19의 Mainboard 혜택 문구 오인 문제를 SSD에서 검증된 방식으로 일반화한다.
    """
    if product is None:
        return None

    for anchor in product.select(
        "[data-testid='ProductListBaseProductImage'] a[aria-label], "
        ".dnw-product-image a[aria-label]"
    ):
        aria = normalize_space(anchor.get("aria-label", ""))
        name = re.sub(r"\s*상세보기\s*$", "", aria).strip()
        if name and len(name) >= 3 and not is_benefit_or_meta_name(name):
            return name

    for img in product.select(
        "[data-testid='ProductListBaseProductImage'] img[alt], "
        ".dnw-product-image img[alt]"
    ):
        name = normalize_space(img.get("alt", ""))
        if name and len(name) >= 3 and not is_benefit_or_meta_name(name):
            return name

    return None


def extract_modern_name(product):
    canonical = extract_modern_canonical_name(product)
    if canonical:
        return canonical

    candidates = []
    selectors = [
        ".dnw-product-info a[href*='pcode=']",
        "[class*='product-info'] a[href*='pcode=']",
        "h3 a[href*='pcode=']",
        "h4 a[href*='pcode=']",
        "a[href*='pcode=']",
    ]

    for index, selector in enumerate(selectors):
        for tag in product.select(selector):
            name = normalize_space(tag.get_text(" ", strip=True))
            if not name or len(name) < 3:
                continue
            if is_benefit_or_meta_name(name):
                continue
            if re.fullmatch(r"\d+(?:\.\d+)?\s*(GB|TB|W|MM)", name, re.I):
                continue

            score = 100 - index * 10 + min(len(name), 80)
            if re.search(r"[가-힣A-Za-z]", name):
                score += 20
            candidates.append((score, name))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


def _parse_price(text):
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else None


def extract_modern_price(product, category_name):
    minimum = MIN_PRICE_BY_CATEGORY.get(category_name, 0)
    values = []

    for selector in [
        "[class*='price'] strong",
        "[class*='price'] a",
        "[class*='price'] span",
    ]:
        for tag in product.select(selector):
            text = tag.get_text(" ", strip=True)
            for raw in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{4,})\s*원", text):
                value = _parse_price(raw)
                if value is not None and value >= max(5000, minimum // 2):
                    values.append(value)

    if not values:
        text = product.get_text(" ", strip=True)
        for raw in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{4,})\s*원", text):
            value = _parse_price(raw)
            if value is not None and value >= max(5000, minimum // 2):
                values.append(value)

    return min(values) if values else None


def extract_product_name_price(product, category_name, mode):
    if mode == "legacy" or is_legacy_product(product):
        name_tag = product.select_one(".prod_name a")
        price_tag = product.select_one(".price_sect strong")

        if not name_tag or not price_tag:
            return None, None

        name = normalize_space(name_tag.get_text(" ", strip=True))
        price = _parse_price(price_tag.get_text(" ", strip=True))
        return name, price

    if category_name == "SSD":
        return extract_modern_ssd_name(product), extract_modern_price(product, category_name)

    return extract_modern_name(product), extract_modern_price(product, category_name)


def extract_product_spec_list(product, mode):
    if mode == "legacy" or is_legacy_product(product):
        return extract_spec_list_from_product(product)

    spec_list = []
    for selector in [".dnw-product-info", "[class*='product-info']"]:
        tag = product.select_one(selector)
        if tag:
            text = normalize_space(tag.get_text(" ", strip=True))
            if text:
                spec_list.append(text)
                break

    full_text = normalize_space(product.get_text(" ", strip=True))
    if full_text and full_text not in spec_list:
        spec_list.append(full_text)

    return spec_list



# =========================================================
# AJAX 페이지네이션 fallback
# - 현재 잘 동작하던 Danawa legacy/modern AJAX를 재사용
# - UI 페이지 번호가 안 보이거나 10 -> 11 그룹 이동이 실패해도 사용
# =========================================================
PAGE_PARAM_KEYS = {
    "page", "pageno", "pagenum", "pageindex", "currentpage", "curpage",
    "current_page", "page_no", "pagenumber", "page_number", "pageidx",
    "page_idx", "nowpage", "now_page",
}


def _normalized_key(value):
    return re.sub(r"[^a-z0-9_]", "", str(value).lower())


def _contains_page_key(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _normalized_key(key) in PAGE_PARAM_KEYS:
                return True
            if _contains_page_key(value):
                return True
    elif isinstance(obj, list):
        return any(_contains_page_key(x) for x in obj)
    return False


def _has_page_parameter(text):
    if not text:
        return False

    raw = str(text).strip()
    if not raw:
        return False

    if re.search(
        r"(^|[?&])(?:page|pageno|pagenum|pageindex|currentpage|curpage|current_page|page_no)=",
        raw,
        re.I,
    ):
        return True

    if re.search(
        r'"(?:page|pageNo|pageNum|pageIndex|currentPage|curPage|current_page|page_no)"\s*:\s*\d+',
        raw,
        re.I,
    ):
        return True

    try:
        if _contains_page_key(json.loads(raw)):
            return True
    except Exception:
        pass

    try:
        for key, value in parse_qsl(raw, keep_blank_values=True):
            if _normalized_key(key) in PAGE_PARAM_KEYS:
                return True
            decoded = unquote_plus(str(value))
            try:
                if _contains_page_key(json.loads(decoded)):
                    return True
            except Exception:
                pass
    except Exception:
        pass

    return False


def _request_capture_score(request):
    try:
        url = request.url or ""
        method = (request.method or "").upper()
        resource_type = (request.resource_type or "").lower()
        body = request.post_data or ""
    except Exception:
        return -1

    low_url = url.lower()
    low_body = body.lower()
    score = 0

    if resource_type in {"xhr", "fetch"}:
        score += 30
    if "getproductlist" in low_url:
        score += 200
    if "/list/ajax/" in low_url:
        score += 120
    if "productlist" in low_url:
        score += 90
    if "product" in low_url and "list" in low_url:
        score += 45
    if _has_page_parameter(url):
        score += 55
    if _has_page_parameter(body):
        score += 70
    if any(k in low_body for k in ["cate=", "categorycode", "listcategorycode", "physicscate"]):
        score += 40
    if "cate=" in low_url or "category" in low_url:
        score += 20
    if method == "POST":
        score += 10

    return score


def capture_ajax_request(request, holder):
    try:
        score = _request_capture_score(request)
        if score < 100:
            return

        url = request.url or ""
        body = request.post_data or ""
        if not (_has_page_parameter(url) or _has_page_parameter(body)):
            return

        if score < holder.get("score", -1):
            return

        try:
            headers = dict(request.headers)
        except Exception:
            headers = {}

        old_url = holder.get("url")
        holder.clear()
        holder.update({
            "url": url,
            "method": (request.method or "GET").upper(),
            "post_data": body,
            "headers": headers,
            "score": score,
        })

        if old_url != url:
            print(f"\n  - 상품목록 AJAX 감지: {holder['method']} {url[:180]}")
    except Exception:
        pass


def _replace_page_in_json_value(value, target_page):
    changed = False

    def walk(obj):
        nonlocal changed
        if isinstance(obj, dict):
            out = {}
            for key, val in obj.items():
                if _normalized_key(key) in PAGE_PARAM_KEYS:
                    out[key] = target_page
                    changed = True
                else:
                    out[key] = walk(val)
            return out
        if isinstance(obj, list):
            return [walk(x) for x in obj]
        return obj

    try:
        parsed = json.loads(value)
    except Exception:
        return value, False

    updated = walk(parsed)
    if not changed:
        return value, False
    return json.dumps(updated, ensure_ascii=False, separators=(",", ":")), True


def _replace_page_in_query_or_form(raw_text, target_page):
    if raw_text is None:
        return raw_text, False

    raw_text = str(raw_text)

    try:
        pairs = parse_qsl(raw_text, keep_blank_values=True, strict_parsing=False)
    except Exception:
        pairs = []

    if pairs:
        changed = False
        new_pairs = []
        for key, value in pairs:
            if _normalized_key(key) in PAGE_PARAM_KEYS:
                new_pairs.append((key, str(target_page)))
                changed = True
                continue

            new_value, nested_changed = _replace_page_in_json_value(value, target_page)
            new_pairs.append((key, new_value if nested_changed else value))
            changed = changed or nested_changed

        if changed:
            return urlencode(new_pairs, doseq=True), True

    updated, count = re.subn(
        r"(?i)(^|&)(page|pageno|pagenum|pageindex|currentpage|curpage|current_page|page_no)=\d+",
        lambda m: f"{m.group(1)}{m.group(2)}={target_page}",
        raw_text,
    )
    if count:
        return updated, True

    updated, count = re.subn(
        r'(?i)("(?:page|pageNo|pageNum|pageIndex|currentPage|curPage|current_page|page_no)"\s*:\s*)\d+',
        lambda m: f"{m.group(1)}{target_page}",
        raw_text,
    )
    if count:
        return updated, True

    return _replace_page_in_json_value(raw_text, target_page)


def build_ajax_request_for_page(template, target_page):
    if not template or not template.get("url"):
        return None

    parts = urlsplit(template["url"])
    new_query, query_changed = _replace_page_in_query_or_form(parts.query, target_page)
    new_url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    body = template.get("post_data") or ""
    new_body, body_changed = _replace_page_in_query_or_form(body, target_page)

    if not query_changed and not body_changed:
        return None

    return {
        "url": new_url,
        "method": template.get("method", "GET").upper(),
        "body": new_body if template.get("method", "GET").upper() != "GET" else None,
        "headers": template.get("headers") or {},
    }


def _find_product_html_in_json(value):
    candidates = []

    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, str):
            if "dnw-product-list-item" in obj or "productItem" in obj or "prod_item" in obj:
                candidates.append(obj)

    walk(value)
    return max(candidates, key=len) if candidates else None


def normalize_ajax_response_html(text_value):
    if not text_value:
        return None

    text_value = str(text_value).strip()
    if not text_value:
        return None

    if text_value.startswith("{") or text_value.startswith("["):
        try:
            html = _find_product_html_in_json(json.loads(text_value))
            if html:
                return html
        except Exception:
            pass

    if "dnw-product-list-item" in text_value or "productItem" in text_value or "prod_item" in text_value:
        return text_value

    return None


def count_products_in_html(html_text):
    if not html_text:
        return 0, "none"

    soup = BeautifulSoup(html_text, "html.parser")
    legacy = soup.select(LEGACY_PRODUCT_SELECTOR)
    if legacy:
        return len(legacy), "legacy"
    modern = soup.select(MODERN_PRODUCT_SELECTOR)
    if modern:
        return len(modern), "modern"
    return 0, "none"


def html_signature(html_text):
    soup = BeautifulSoup(html_text or "", "html.parser")
    products = soup.select(LEGACY_PRODUCT_SELECTOR) or soup.select(MODERN_PRODUCT_SELECTOR)
    values = []
    for product in products[:8]:
        values.append(normalize_space(product.get_text(" ", strip=True))[:120])
    return "||".join(values)


def fetch_page_via_captured_ajax(page_obj, template, target_page):
    request_info = build_ajax_request_for_page(template, target_page)
    if request_info is None:
        return None

    safe_headers = {}
    for key, value in (request_info.get("headers") or {}).items():
        low = str(key).lower()
        if low in {"accept", "content-type", "x-requested-with"} or low.startswith("x-"):
            safe_headers[key] = value

    payload = {
        "url": request_info["url"],
        "method": request_info["method"],
        "body": request_info.get("body"),
        "headers": safe_headers,
    }

    try:
        result = page_obj.evaluate(
            r"""
            async (payload) => {
                const options = {
                    method: payload.method || 'GET',
                    credentials: 'include',
                    headers: payload.headers || {},
                    cache: 'no-store',
                    referrer: location.href
                };
                if (options.method !== 'GET' && options.method !== 'HEAD' && payload.body != null) {
                    options.body = payload.body;
                }
                const response = await fetch(payload.url, options);
                return { status: response.status, text: await response.text() };
            }
            """,
            payload,
        )
    except Exception:
        return None

    if not result or int(result.get("status", 0)) < 200 or int(result.get("status", 0)) >= 300:
        return None

    html = normalize_ajax_response_html(result.get("text"))
    if not html:
        return None

    count, mode = count_products_in_html(html)
    if count <= 0:
        return None

    return {"html": html, "count": count, "mode": mode, "signature": html_signature(html)}


def save_html_text(temp_dir, page_no, html_text):
    path = os.path.join(temp_dir, f"page_{page_no:02d}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return path

# =========================================================
# 페이지 처리
# =========================================================
def clear_temp_html_files(temp_dir):
    os.makedirs(temp_dir, exist_ok=True)
    count = 0
    for name in os.listdir(temp_dir):
        if name.lower().endswith(".html"):
            try:
                os.remove(os.path.join(temp_dir, name))
                count += 1
            except OSError:
                pass
    print(f"  - 이전 temp HTML {count}개 삭제")


def get_page_product_counts(page_obj):
    try:
        legacy = page_obj.locator(LEGACY_PRODUCT_SELECTOR).count()
    except Exception:
        legacy = 0

    try:
        modern = page_obj.locator(MODERN_PRODUCT_SELECTOR).count()
    except Exception:
        modern = 0

    return legacy, modern


def current_page_signature(page_obj):
    try:
        html = page_obj.content()
    except Exception:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    products = soup.select(LEGACY_PRODUCT_SELECTOR)
    if not products:
        products = soup.select(MODERN_PRODUCT_SELECTOR)

    names = []
    for product in products[:8]:
        text = normalize_space(product.get_text(" ", strip=True))
        names.append(text[:120])

    return "||".join(names)


def wait_product_change(page_obj, before_signature, timeout_ms=12000):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        page_obj.wait_for_timeout(300)
        after = current_page_signature(page_obj)
        if after and after != before_signature:
            return True
    return False


def _visible_page_numbers(page_obj):
    """페이지네이션 컨테이너 안의 실제 페이지 번호만 반환한다."""
    try:
        return page_obj.evaluate(
            r"""
            () => {
                const clean = (v) => (v || "").replace(/\s+/g, " ").trim();
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.visibility !== "hidden" && s.display !== "none";
                };

                const containers = Array.from(document.querySelectorAll(
                    "[class*='pagination'], [class*='paging'], " +
                    "[id*='pagination'], [id*='paging'], " +
                    "[data-testid*='pagination'], [data-testid*='paging']"
                )).filter(visible);

                const collect = (root) => Array.from(
                    root.querySelectorAll("a, button, [role='button']")
                )
                .filter(visible)
                .map((el) => clean(el.textContent))
                .filter((x) => /^\d+$/.test(x))
                .map(Number)
                .filter((n) => n >= 1 && n <= 100);

                let groups = containers
                    .map((root) => collect(root))
                    .filter((nums) => nums.length >= 2);

                if (!groups.length) {
                    // class명이 바뀐 경우를 위한 보수적 fallback.
                    // 1~100 사이의 연속적인 작은 숫자 그룹만 페이지네이션으로 인정한다.
                    const controls = Array.from(
                        document.querySelectorAll("a, button, [role='button']")
                    ).filter(visible);

                    for (const el of controls) {
                        let p = el.parentElement;
                        for (let depth = 0; depth < 6 && p; depth++, p = p.parentElement) {
                            const nums = collect(p);
                            const small = nums.filter((n) => n >= 1 && n <= 100);
                            if (small.length >= 5 && Math.max(...small) - Math.min(...small) <= 30) {
                                groups.push(small);
                                break;
                            }
                        }
                    }
                }

                if (!groups.length) return [];
                groups.sort((a, b) => b.length - a.length);
                return Array.from(new Set(groups[0])).sort((a, b) => a - b);
            }
            """
        ) or []
    except Exception:
        return []

def prepare_pagination_area(page_obj):
    """
    modern 목록은 하단으로 내려가기 전 페이지네이션이 늦게 렌더링되는 경우가 있어
    상품 목록 하단까지 한 번 스크롤하고 페이지 번호가 나타날 시간을 준다.
    """
    try:
        page_obj.evaluate(
            """
            () => {
                const selectors = [
                    "[class*='pagination']",
                    "[class*='paging']",
                    "[data-testid*='pagination']",
                    "[data-testid*='paging']"
                ];
                for (const selector of selectors) {
                    const el = document.querySelector(selector);
                    if (el) {
                        el.scrollIntoView({block: "center"});
                        return;
                    }
                }
                window.scrollTo(0, document.body.scrollHeight);
            }
            """
        )
        page_obj.wait_for_timeout(900)
    except Exception:
        pass


def click_visible_page_number(page_obj, target_page):
    """
    정확한 페이지 번호 후보를 찾아 클릭한다.

    V19:
    - 하단 페이지네이션 렌더링을 먼저 유도
    - 텍스트가 숫자인 버튼뿐 아니라 data-page / data-pageno / aria-label /
      href / onclick 안에 target page가 들어있는 요소도 후보로 사용
    - 숫자 그룹이 3개 이상이어야 한다는 기존 강제 조건 제거
    """
    prepare_pagination_area(page_obj)

    try:
        result = page_obj.evaluate(
            r"""
            (target) => {
                const targetText = String(target);
                const clean = (v) => (v || "").replace(/\s+/g, " ").trim();

                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.visibility !== "hidden" &&
                           s.display !== "none";
                };

                const controls = Array.from(
                    document.querySelectorAll(
                        "a, button, [role='button'], [data-page], [data-pageno], " +
                        "[data-page-no], [data-page-number]"
                    )
                ).filter(visible);

                const pageLike = (el) => {
                    const text = clean(el.textContent);
                    const aria = clean(el.getAttribute("aria-label"));
                    const title = clean(el.getAttribute("title"));
                    const href = clean(el.getAttribute("href"));
                    const onclick = clean(el.getAttribute("onclick"));

                    const attrs = [
                        el.getAttribute("data-page"),
                        el.getAttribute("data-pageno"),
                        el.getAttribute("data-page-no"),
                        el.getAttribute("data-page-number"),
                        el.getAttribute("data-index")
                    ].map(clean);

                    if (text === targetText) return true;
                    if (attrs.includes(targetText)) return true;

                    const meta = `${aria} ${title}`.toLowerCase();
                    const explicitPage = new RegExp(
                        `(?:page|페이지)\\s*${targetText}(?:\\D|$)`, "i"
                    );
                    if (explicitPage.test(meta)) return true;

                    const jsPatterns = [
                        new RegExp(`(?:movePage|getPage|goPage|setPage|changePage)\\s*\\(\\s*${targetText}\\s*\\)`, "i"),
                        new RegExp(`(?:page|pageNo|pageNum|currentPage)\\s*[=:]\\s*['"]?${targetText}(?:['"&;\\s]|$)`, "i")
                    ];

                    return jsPatterns.some((r) => r.test(href) || r.test(onclick));
                };

                const candidates = controls.filter(pageLike);

                const scored = candidates.map((el) => {
                    let score = 0;
                    let p = el;

                    const text = clean(el.textContent);
                    if (text === targetText) score += 180;

                    const attrs = [
                        el.getAttribute("data-page"),
                        el.getAttribute("data-pageno"),
                        el.getAttribute("data-page-no"),
                        el.getAttribute("data-page-number")
                    ].map(clean);

                    if (attrs.includes(targetText)) score += 220;

                    for (let depth = 0; depth < 10 && p; depth++, p = p.parentElement) {
                        const meta = [
                            p.className || "",
                            p.id || "",
                            p.getAttribute?.("data-testid") || "",
                            p.getAttribute?.("aria-label") || ""
                        ].join(" ").toLowerCase();

                        if (/pagination|paging|page-nav|pagenation|paginate/.test(meta)) {
                            score += 400 - depth * 15;
                        }

                        const nums = Array.from(
                            p.querySelectorAll?.("a, button, [role='button']") || []
                        )
                        .filter(visible)
                        .map((x) => clean(x.textContent))
                        .filter((x) => /^\d+$/.test(x));

                        if (nums.length >= 2) {
                            score += nums.length * 20 - depth * 2;
                        }
                    }

                    score += Math.max(0, el.getBoundingClientRect().top / 1500);
                    return {el, score};
                }).sort((a, b) => b.score - a.score);

                if (!scored.length) {
                    return {clicked: false, candidates: 0};
                }

                const chosen = scored[0].el;
                chosen.scrollIntoView({block: "center", inline: "center"});

                try {
                    chosen.dispatchEvent(
                        new MouseEvent("click", {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        })
                    );
                } catch (_) {
                    chosen.click();
                }

                return {
                    clicked: true,
                    candidates: scored.length,
                    score: scored[0].score,
                    text: clean(chosen.textContent)
                };
            }
            """,
            target_page,
        )
        return bool(isinstance(result, dict) and result.get("clicked"))
    except Exception:
        return False


def click_page_by_metadata(page_obj, target_page):
    """
    텍스트 페이지 번호가 잡히지 않을 때 href/onclick/data-*에
    페이지 번호가 들어간 요소를 마지막으로 직접 클릭한다.
    """
    prepare_pagination_area(page_obj)

    try:
        return bool(
            page_obj.evaluate(
                r"""
                (target) => {
                    const t = String(target);
                    const nodes = Array.from(document.querySelectorAll(
                        "[data-page], [data-pageno], [data-page-no], [data-page-number], " +
                        "a[href], a[onclick], button[onclick]"
                    ));

                    const clean = (v) => (v || "").replace(/\s+/g, " ").trim();

                    const matched = nodes.filter((el) => {
                        const values = [
                            el.getAttribute("data-page"),
                            el.getAttribute("data-pageno"),
                            el.getAttribute("data-page-no"),
                            el.getAttribute("data-page-number")
                        ].map(clean);

                        if (values.includes(t)) return true;

                        const href = clean(el.getAttribute("href"));
                        const onclick = clean(el.getAttribute("onclick"));

                        const re1 = new RegExp(
                            `(?:movePage|getPage|goPage|setPage|changePage)\\s*\\(\\s*${t}\\s*\\)`,
                            "i"
                        );
                        const re2 = new RegExp(
                            `(?:page|pageNo|pageNum|currentPage)\\s*[=:]\\s*['"]?${t}(?:['"&;\\s]|$)`,
                            "i"
                        );

                        return re1.test(href) || re1.test(onclick) ||
                               re2.test(href) || re2.test(onclick);
                    });

                    if (!matched.length) return false;

                    const el = matched[0];
                    el.scrollIntoView({block: "center"});
                    el.click();
                    return true;
                }
                """,
                target_page,
            )
        )
    except Exception:
        return False

def click_next_page_group(page_obj, current_page=None):
    """
    1~10 -> 11~20 같은 다음 페이지 그룹 버튼 클릭.
    숨겨진 span, aria-label, class 기반 버튼까지 모두 탐색한다.
    """
    try:
        result = page_obj.evaluate(
            r"""
            (currentPage) => {
                const clean = (v) => (v || "").replace(/\s+/g, " ").trim();
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.visibility !== "hidden" && s.display !== "none";
                };

                const controls = Array.from(
                    document.querySelectorAll("a, button, [role='button']")
                ).filter(visible);

                const isNext = (el) => {
                    const text = clean(el.textContent);
                    const aria = clean(el.getAttribute("aria-label"));
                    const title = clean(el.getAttribute("title"));
                    const cls = String(el.className || "");
                    const testid = clean(el.getAttribute("data-testid"));
                    const combined =
                        `${text} ${aria} ${title} ${cls} ${testid}`.toLowerCase();

                    return (
                        /다음\s*페이지/.test(combined) ||
                        /(^|\s)다음($|\s)/.test(combined) ||
                        /\bnext\b/.test(combined) ||
                        /nav[_-]?next/.test(combined) ||
                        /page[_-]?next/.test(combined) ||
                        /pagination.*next/.test(combined)
                    );
                };

                const candidates = [];

                for (const el of controls.filter(isNext)) {
                    let score = 0;
                    let bestNumericCount = 0;
                    let p = el;

                    for (let depth = 0; depth < 9 && p; depth++, p = p.parentElement) {
                        const meta = [
                            p.className || "",
                            p.id || "",
                            p.getAttribute?.("data-testid") || "",
                            p.getAttribute?.("aria-label") || ""
                        ].join(" ").toLowerCase();

                        if (/pagination|paging|page-nav|pagenation/.test(meta)) {
                            score += 300 - depth * 10;
                        }

                        const nums = Array.from(
                            p.querySelectorAll?.("a, button, [role='button']") || []
                        )
                        .filter(visible)
                        .map((x) => clean(x.textContent))
                        .filter((x) => /^\d+$/.test(x));

                        bestNumericCount = Math.max(bestNumericCount, nums.length);
                        if (nums.length >= 3) {
                            score += nums.length * 30 - depth * 3;
                        }
                    }

                    if (bestNumericCount >= 3) {
                        score += Math.max(0, el.getBoundingClientRect().top / 1000);
                        candidates.push({ el, score, bestNumericCount });
                    }
                }

                candidates.sort((a, b) => b.score - a.score);

                if (!candidates.length) {
                    return { clicked: false };
                }

                const chosen = candidates[0].el;
                chosen.scrollIntoView({ block: "center", inline: "center" });

                const href = chosen.getAttribute("href") || "";
                if (href && href !== "#" && !/^javascript:\s*void/i.test(href)) {
                    try {
                        if (/^javascript:/i.test(href)) {
                            window.eval(href.replace(/^javascript:\s*/i, ""));
                        } else {
                            chosen.click();
                        }
                    } catch (_) {
                        chosen.click();
                    }
                } else {
                    chosen.click();
                }

                return {
                    clicked: true,
                    score: candidates[0].score,
                    groupSize: candidates[0].bestNumericCount
                };
            }
            """,
            current_page,
        )

        return bool(isinstance(result, dict) and result.get("clicked"))
    except Exception:
        return False


def try_javascript_page_move(page_obj, target_page):
    scripts = [
        f"typeof movePage === 'function' ? (movePage({target_page}), true) : false",
        f"typeof getPage === 'function' ? (getPage({target_page}), true) : false",
        f"typeof goPage === 'function' ? (goPage({target_page}), true) : false",
    ]

    for script in scripts:
        try:
            if page_obj.evaluate(script):
                return True
        except Exception:
            continue

    return False


def _wait_stable_page_change(page_obj, before_signature, timeout_ms=15000):
    """
    상품 목록이 실제로 바뀌고 잠깐의 skeleton/중간 렌더가 끝난 뒤
    같은 시그니처가 연속으로 확인될 때 성공 처리한다.
    """
    deadline = time.time() + timeout_ms / 1000
    last = None
    stable_hits = 0

    while time.time() < deadline:
        page_obj.wait_for_timeout(350)
        current = current_page_signature(page_obj)

        if not current or current == before_signature:
            last = current
            stable_hits = 0
            continue

        if current == last:
            stable_hits += 1
        else:
            last = current
            stable_hits = 1

        if stable_hits >= 2:
            return True

    return False


def navigate_to_page(page_obj, target_page):
    before = current_page_signature(page_obj)
    prepare_pagination_area(page_obj)

    if click_visible_page_number(page_obj, target_page):
        if _wait_stable_page_change(page_obj, before):
            return True

    if click_page_by_metadata(page_obj, target_page):
        if _wait_stable_page_change(page_obj, before):
            return True

    visible_numbers = _visible_page_numbers(page_obj)

    if target_page > 10 and target_page not in visible_numbers:
        before_numbers = visible_numbers

        if click_next_page_group(page_obj, current_page=target_page - 1):
            deadline = time.time() + 8
            while time.time() < deadline:
                page_obj.wait_for_timeout(350)
                new_numbers = _visible_page_numbers(page_obj)
                if target_page in new_numbers or new_numbers != before_numbers:
                    break

            if click_visible_page_number(page_obj, target_page):
                if _wait_stable_page_change(page_obj, before):
                    return True

            if click_page_by_metadata(page_obj, target_page):
                if _wait_stable_page_change(page_obj, before):
                    return True

    if try_javascript_page_move(page_obj, target_page):
        if _wait_stable_page_change(page_obj, before):
            return True

    return False

def save_current_page(page_obj, temp_dir, page_no):
    path = os.path.join(temp_dir, f"page_{page_no:02d}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page_obj.content())
    return path


# =========================================================
# 저장 HTML 파싱
# =========================================================
def parse_saved_pages(category_name, temp_dir):
    rows = []

    stats = {
        "price_before": 0,
        "price_after": 0,
        "price_removed": 0,
        "price_too_high_removed": 0,
        "socket_removed": 0,
        "consumer_name_removed": 0,
        "ram_consumer_removed": 0,
        "ram_multi_option_products": 0,
        "ram_multi_option_rows": 0,
        "ram_single_products": 0,
        "ram_ambiguous_removed": 0,
        "ram_capacity_missing": 0,
        "ram_price_missing": 0,
        "ram_spec_missing": 0,
        "ram_module_count_missing": 0,
        "mainboard_bundle_removed": 0,
        "ssd_enterprise_removed": 0,
        "ssd_unsupported_interface_removed": 0,
        "cooler_accessory_removed": 0,
        "cooler_server_removed": 0,
        "case_rackmount_removed": 0,
        "gpu_power_removed": 0,
        "spec_removed": 0,
        "invalid_name_removed": 0,
        "parse_error": 0,
        "ssd_single_name_products": 0,
        "ssd_multi_option_products": 0,
        "ssd_multi_option_rows": 0,
        "ssd_fallback_products": 0,
        "ssd_capacity_missing": 0,
        "ssd_name_missing": 0,
        "ssd_benefit_removed": 0,
        "ssd_used_removed": 0,
        "ssd_accessory_removed": 0,
        "ssd_base_valid_products": 0,
        "product_identity_missing": 0,
    }

    html_files = sorted(Path(temp_dir).glob("*.html"))

    for html_path in html_files:
        try:
            html = html_path.read_text(encoding="utf-8")
            soup = BeautifulSoup(html, "html.parser")

            legacy_products = soup.select(LEGACY_PRODUCT_SELECTOR)
            modern_products = soup.select(MODERN_PRODUCT_SELECTOR)

            if legacy_products:
                products = legacy_products
                mode = "legacy"
            else:
                products = modern_products
                mode = "modern"

            for product in products:
                try:
                    name, price = extract_product_name_price(product, category_name, mode)
                    product_code, product_url = extract_product_identity(product, mode)

                    if product_code is None:
                        stats["product_identity_missing"] += 1

                    if not name:
                        if category_name == "SSD":
                            stats["ssd_name_missing"] += 1
                        continue

                    if price is None:
                        continue

                    stats["price_before"] += 1

                    # 모든 카테고리 공통 상품명/소비자용 제품 필터.
                    invalid_product_reason = classify_invalid_product_name(
                        category_name,
                        name,
                    )
                    if invalid_product_reason is not None:
                        stats["invalid_name_removed"] += 1
                        stats["consumer_name_removed"] += 1
                        if category_name == "RAM" and invalid_product_reason in {
                            "old_generation", "unsupported_memory", "server_or_notebook", "used"
                        }:
                            stats["ram_consumer_removed"] += 1
                        elif category_name == "Mainboard" and invalid_product_reason == "bundle":
                            stats["mainboard_bundle_removed"] += 1
                        elif category_name == "SSD" and invalid_product_reason == "enterprise":
                            stats["ssd_enterprise_removed"] += 1
                        continue

                    # SSD는 canonical 상품명을 확보한 뒤 노이즈를 먼저 제거한다.
                    # 중고는 자동견적 추천 대상에서 제외하고, 병행수입/해외구매/벌크는 유지한다.
                    if category_name == "SSD":
                        invalid_reason = classify_invalid_ssd_name(name)
                        if invalid_reason is not None:
                            stats["invalid_name_removed"] += 1
                            if invalid_reason == "benefit":
                                stats["ssd_benefit_removed"] += 1
                            elif invalid_reason == "used":
                                stats["ssd_used_removed"] += 1
                            elif invalid_reason == "accessory":
                                stats["ssd_accessory_removed"] += 1
                            elif invalid_reason == "enterprise":
                                stats["ssd_enterprise_removed"] += 1
                            continue
                        stats["ssd_base_valid_products"] += 1

                    spec_list = extract_product_spec_list(product, mode)
                    full_text = normalize_space(product.get_text(" ", strip=True))

                    # SSD: mSATA 계열은 메인보드 인터페이스 호환성을 보장할 수 없으므로 제외한다.
                    if category_name == "SSD" and is_unsupported_ssd_interface(
                        f"{name} / {full_text}"
                    ):
                        stats["ssd_unsupported_interface_removed"] += 1
                        continue

                    # =================================================
                    # RAM은 용량 옵션별 총가격이 별도로 존재하므로
                    # 같은 옵션 행의 capacity-price 쌍을 기준으로 확장한다.
                    # =================================================
                    if category_name == "RAM":
                        ram_rows, ram_mode = build_ram_rows_from_product(
                            product=product,
                            base_name=name,
                            spec_list=spec_list,
                            full_text=full_text,
                            product_code=product_code,
                            product_url=product_url,
                        )

                        if ram_mode == "multi-option":
                            stats["ram_multi_option_products"] += 1
                            stats["ram_multi_option_rows"] += len(ram_rows)
                        elif ram_mode == "single":
                            stats["ram_single_products"] += 1
                        elif ram_mode == "ambiguous-capacity":
                            stats["ram_ambiguous_removed"] += 1
                            stats["spec_removed"] += 1
                        elif ram_mode == "capacity-missing":
                            stats["ram_capacity_missing"] += 1
                            stats["spec_removed"] += 1
                        elif ram_mode == "price-missing":
                            stats["ram_price_missing"] += 1
                            stats["price_removed"] += 1
                        elif ram_mode == "invalid-price":
                            stats["price_removed"] += 1
                        elif ram_mode == "spec-missing":
                            stats["ram_spec_missing"] += 1
                            stats["spec_removed"] += 1
                        elif ram_mode == "module-count-missing":
                            stats["ram_module_count_missing"] += 1
                            stats["spec_removed"] += 1

                        if not ram_rows:
                            continue

                        stats["price_after"] += len(ram_rows)
                        rows.extend(ram_rows)
                        continue

                    # =================================================
                    # SSD는 용량별 가격 옵션이 존재할 수 있으므로
                    # 일반 카테고리보다 먼저 전용 확장 로직을 적용한다.
                    # =================================================
                    if category_name == "SSD":
                        ssd_rows, ssd_mode = build_ssd_rows_from_product(
                            product=product,
                            base_name=name,
                            base_price=price,
                            spec_list=spec_list,
                            full_text=full_text,
                            product_code=product_code,
                            product_url=product_url,
                        )

                        if ssd_mode == "single-name":
                            stats["ssd_single_name_products"] += 1
                        elif ssd_mode == "multi-option":
                            stats["ssd_multi_option_products"] += 1
                            stats["ssd_multi_option_rows"] += len(ssd_rows)
                        elif ssd_mode == "fallback":
                            stats["ssd_fallback_products"] += 1
                        elif ssd_mode == "capacity-missing":
                            stats["ssd_capacity_missing"] += 1
                            stats["spec_removed"] += 1

                        if not ssd_rows:
                            if ssd_mode in {"single-invalid-price", "fallback-invalid-price"}:
                                stats["price_removed"] += 1
                            continue

                        # SSD는 실제 생성된 variant 중 가격 기준을 통과한 행만 들어온다.
                        stats["price_after"] += len(ssd_rows)
                        rows.extend(ssd_rows)
                        continue

                    price_reason = classify_price(category_name, price)
                    if price_reason is not None:
                        stats["price_removed"] += 1
                        if price_reason == "above_max":
                            stats["price_too_high_removed"] += 1
                        continue

                    stats["price_after"] += 1

                    if category_name == "Cooler":
                        cooler_invalid_reason = classify_invalid_cooler(
                            name,
                            full_text,
                        )
                        if cooler_invalid_reason is not None:
                            if cooler_invalid_reason == "server":
                                stats["cooler_server_removed"] += 1
                            else:
                                stats["cooler_accessory_removed"] += 1
                            continue

                    if category_name == "Case":
                        case_invalid_reason = classify_invalid_case(
                            name,
                            full_text,
                        )
                        if case_invalid_reason is not None:
                            if case_invalid_reason == "rackmount":
                                stats["case_rackmount_removed"] += 1
                            else:
                                stats["invalid_name_removed"] += 1
                            continue

                    refined = extract_refined_spec(
                        spec_list,
                        category_name,
                        name=name,
                        full_text=full_text,
                    )

                    if refined is None:
                        stats["spec_removed"] += 1
                        continue

                    row = {
                        "name": name,
                        "price": int(price),
                        **refined,
                        "product_code": product_code,
                        "product_url": product_url,
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    rows.append(row)

                except Exception:
                    stats["parse_error"] += 1

        except Exception:
            stats["parse_error"] += 1

    df = pd.DataFrame(rows)

    before_dedup = len(df)
    if not df.empty:
        df = df.drop_duplicates(subset=["name", "price"], keep="first").reset_index(drop=True)
    after_dedup = len(df)

    print("\n[가격 노이즈 필터]")
    print(f"카테고리: {category_name}")
    print(f"최소 허용 가격: {MIN_PRICE_BY_CATEGORY.get(category_name, 0):,}원")
    if category_name == "RAM":
        print(f"기본 상품 카드 수: {stats['price_before']}")
        print(f"용량 옵션 확장 후 유효 행: {stats['price_after']}")
        print(f"가격/옵션 기준 제거: {stats['price_removed']}")
    elif category_name == "SSD":
        print(f"기본 상품 파싱 개수: {stats['price_before']}")
        print(f"상품명 필터 통과 기본 상품: {stats['ssd_base_valid_products']}")
        print(f"용량 옵션 확장 후 유효 가격 행: {stats['price_after']}")
        print(f"가격 기준 제거: {stats['price_removed']}")
    else:
        print(f"필터 전 개수: {stats['price_before']}")
        print(f"필터 후 개수: {stats['price_after']}")
        print(f"제거된 개수: {stats['price_removed']}")

    print("\n[추가 제거 로그]")
    print(f"Cooler 부속품 이름 필터 제거: {stats['cooler_accessory_removed']}")
    print(f"Cooler 서버/랙마운트/산업용 제거: {stats['cooler_server_removed']}")
    print(f"Case 랙마운트/산업용 제거: {stats['case_rackmount_removed']}")
    print(f"SSD mSATA 인터페이스 제거: {stats['ssd_unsupported_interface_removed']}")
    print(f"GPU 권장 파워 추출 실패 제거: {stats['gpu_power_removed']}")
    print(f"스펙 추출 실패 제거: {stats['spec_removed']}")
    print(f"잘못된 상품명/비추천 제품 제거: {stats['invalid_name_removed']}")
    print(f"가격 상한 초과 제거: {stats['price_too_high_removed']}")
    print(f"RAM 구형/노트북/서버용 제거: {stats['ram_consumer_removed']}")
    print(f"Mainboard 묶음상품 제거: {stats['mainboard_bundle_removed']}")
    print(f"SSD 서버/엔터프라이즈 제거: {stats['ssd_enterprise_removed']}")
    print(f"상품 식별자 추출 실패: {stats['product_identity_missing']}")
    print(f"파싱 예외 발생: {stats['parse_error']}")
    print(f"중복 제거 전 개수: {before_dedup}")
    print(f"중복 제거 후 개수: {after_dedup}")

    if category_name == "SSD":
        print("\n[SSD 단일/다중 용량 추출 진단]")
        print(f"상품명 용량 단일 제품: {stats['ssd_single_name_products']}")
        print(f"다중 용량 옵션 제품: {stats['ssd_multi_option_products']}")
        print(f"다중 옵션에서 생성된 행: {stats['ssd_multi_option_rows']}")
        print(f"fallback 단일 제품: {stats['ssd_fallback_products']}")
        print(f"용량 추출 실패 제품: {stats['ssd_capacity_missing']}")

        print("\n[SSD 상품명/추천 제외 진단]")
        print(f"canonical 상품명 추출 실패: {stats['ssd_name_missing']}")
        print(f"카드혜택/리뷰 문구 제거: {stats['ssd_benefit_removed']}")
        print(f"중고 제품 제거: {stats['ssd_used_removed']}")
        print(f"케이블/컨버터 등 액세서리 제거: {stats['ssd_accessory_removed']}")
        print(f"mSATA 인터페이스 제거: {stats['ssd_unsupported_interface_removed']}")
        print(f"SSD 상품명 기준 총 제거: {stats['invalid_name_removed']}")

    if category_name == "RAM":
        print("\n[RAM 용량/가격 옵션 진단]")
        print(f"다중 용량 상품 카드: {stats['ram_multi_option_products']}")
        print(f"다중 옵션에서 생성된 행: {stats['ram_multi_option_rows']}")
        print(f"단일 용량 상품: {stats['ram_single_products']}")
        print(f"여러 용량 존재 + 옵션 매칭 실패 제거: {stats['ram_ambiguous_removed']}")
        print(f"용량 추출 실패: {stats['ram_capacity_missing']}")
        print(f"총가격 추출 실패: {stats['ram_price_missing']}")
        print(f"DDR/클럭 추출 실패: {stats['ram_spec_missing']}")
        print(f"램개수 추출 실패: {stats['ram_module_count_missing']}")

    if category_name == "RAM" and not df.empty:
        print("\n[RAM 소비자용 데이터 검증]")
        print(f"memory_type 분포: {df['memory_type'].value_counts().to_dict()}")
        print(
            f"클럭 범위: {int(df['memory_clock'].min())} ~ "
            f"{int(df['memory_clock'].max())} MHz"
        )
        print(
            f"용량 범위: {int(df['capacity'].min())} ~ "
            f"{int(df['capacity'].max())} GB"
        )
        print(
            f"가격 범위: {int(df['price'].min()):,} ~ "
            f"{int(df['price'].max()):,}원"
        )
        print("용량 분포:")
        print(df["capacity"].value_counts().sort_index())
        print("램개수 분포:")
        print(df["module_count"].value_counts().sort_index())

    if category_name == "SSD" and not df.empty:
        print("\n[SSD 용량 검증]")
        print(f"최소 허용 용량: {SSD_MIN_CAPACITY_GB} GB")
        print(f"capacity NULL 개수: {int(df['capacity'].isna().sum())}")
        print(f"용량 범위: {int(df['capacity'].min())} ~ {int(df['capacity'].max())} GB")
        print("용량 분포 상위 20개:")
        print(df["capacity"].value_counts().head(20).sort_index())

    return df


# =========================================================
# 출력 컬럼 / 저장 위치
# =========================================================
def get_expected_columns(category_name):
    base = ["name", "price"]
    category_columns = {
        "CPU": ["socket_type", "memory_type"],
        "GPU": ["recommended_power", "pcie_type", "gpu_length"],
        "Mainboard": ["socket_type", "memory_type", "pcie_type", "size", "memory_clock"],
        "RAM": ["memory_type", "memory_clock", "capacity", "module_count"],
        "SSD": ["capacity"],
        "Power": ["size", "wattage"],
        "Case": ["size", "gpu_length", "cooler_length"],
        "Cooler": ["socket_type", "cooler_length"],
    }
    return (
        base
        + category_columns.get(category_name, [])
        + ["product_code", "product_url", "created_at"]
    )


def get_output_path(category_name):
    crawler_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(crawler_dir))
    data_dir = os.path.join(project_root, "data")
    result_dir = os.path.join(data_dir, "result")

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(result_dir, exist_ok=True)

    if category_name in {"CPU", "GPU", "RAM", "SSD"}:
        return os.path.join(data_dir, f"data_{category_name}.csv")

    return os.path.join(result_dir, f"data_{category_name}.csv")


# =========================================================
# 메인 크롤러
# =========================================================
def crawl_danawa(category_name, cate_code, total_pages):
    print(f"\n>>> {category_name} 수집 시작 (목표: {total_pages}페이지)")
    print(f"  - crawler version: {CRAWLER_VERSION}")

    temp_dir = os.path.join(os.path.dirname(__file__), "temp_html", category_name)
    os.makedirs(temp_dir, exist_ok=True)
    clear_temp_html_files(temp_dir)

    saved_count = 0
    saved_signatures = set()
    crawl_error = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
            viewport={"width": 1440, "height": 1000},
        )

        page_obj = context.new_page()
        ajax_template = {}
        page_obj.on(
            "request",
            lambda request: capture_ajax_request(request, ajax_template)
        )

        try:
            start_url = f"https://prod.danawa.com/list/?cate={cate_code}"
            page_obj.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            try:
                page_obj.wait_for_selector(PRODUCT_SELECTOR, timeout=20000)
            except Exception:
                page_obj.wait_for_timeout(3000)

            legacy_count, modern_count = get_page_product_counts(page_obj)

            if legacy_count > 0:
                print(f"  - DOM 구조: legacy productItem ({legacy_count}개 감지)")
            elif modern_count > 0:
                print(
                    f"  - DOM 구조: modern dnw-product-list-item "
                    f"({modern_count}개 감지)"
                )
            else:
                print("  ! 최초 상품 DOM 감지 실패")
                crawl_error = "최초 상품 DOM 감지 실패"

            if not crawl_error:
                first_signature = current_page_signature(page_obj)

                if first_signature:
                    save_current_page(page_obj, temp_dir, 1)
                    saved_signatures.add(first_signature)
                    saved_count = 1
                    print("  - 1페이지 저장 성공")
                else:
                    crawl_error = "1페이지 상품 시그니처 생성 실패"

            if not crawl_error:
                prepare_pagination_area(page_obj)
                visible_numbers = _visible_page_numbers(page_obj)
                if visible_numbers:
                    print(
                        "  - 페이지네이션 감지: "
                        + ", ".join(map(str, visible_numbers[:20]))
                    )
                else:
                    print(
                        "  ! 숫자 페이지네이션을 즉시 감지하지 못했습니다. "
                        "메타데이터/AJAX fallback을 사용합니다."
                    )

            for target_page in range(2, total_pages + 1):
                if crawl_error:
                    break

                ajax_result = None

                if target_page >= 3 and ajax_template:
                    ajax_result = fetch_page_via_captured_ajax(
                        page_obj,
                        ajax_template,
                        target_page,
                    )

                    if (
                        ajax_result
                        and ajax_result["signature"]
                        and ajax_result["signature"] not in saved_signatures
                    ):
                        save_html_text(
                            temp_dir,
                            target_page,
                            ajax_result["html"],
                        )
                        saved_signatures.add(ajax_result["signature"])
                        saved_count += 1
                        print(
                            f"  - {target_page}페이지 AJAX 저장 성공 "
                            f"({ajax_result['count']}개)"
                        )
                        time.sleep(random.uniform(0.10, 0.30))
                        continue

                ok = navigate_to_page(page_obj, target_page)

                if ok:
                    page_obj.wait_for_timeout(500)
                    signature = current_page_signature(page_obj)

                    if not signature:
                        ok = False
                    elif signature in saved_signatures:
                        print(
                            f"  ! {target_page}페이지 UI 이동 후 "
                            f"이전 페이지와 동일한 상품 목록 감지"
                        )
                        ok = False
                    else:
                        save_current_page(
                            page_obj,
                            temp_dir,
                            target_page,
                        )
                        saved_signatures.add(signature)
                        saved_count += 1
                        print(
                            f"  - {target_page}페이지 이동 성공 "
                            f"({saved_count}/{total_pages})"
                        )

                        if target_page == 2:
                            page_obj.wait_for_timeout(500)

                        time.sleep(random.uniform(0.15, 0.45))
                        continue

                if ajax_template:
                    ajax_result = fetch_page_via_captured_ajax(
                        page_obj,
                        ajax_template,
                        target_page,
                    )

                    if (
                        ajax_result
                        and ajax_result["signature"]
                        and ajax_result["signature"] not in saved_signatures
                    ):
                        save_html_text(
                            temp_dir,
                            target_page,
                            ajax_result["html"],
                        )
                        saved_signatures.add(ajax_result["signature"])
                        saved_count += 1
                        print(
                            f"  - {target_page}페이지 AJAX fallback 성공 "
                            f"({ajax_result['count']}개)"
                        )
                        time.sleep(random.uniform(0.10, 0.30))
                        continue

                try:
                    direct_url = (
                        f"https://prod.danawa.com/list/"
                        f"?cate={cate_code}&page={target_page}"
                    )

                    page_obj.goto(
                        direct_url,
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )

                    try:
                        page_obj.wait_for_selector(
                            PRODUCT_SELECTOR,
                            timeout=10000,
                        )
                    except Exception:
                        page_obj.wait_for_timeout(1500)

                    signature = current_page_signature(page_obj)
                    legacy_count, modern_count = get_page_product_counts(page_obj)

                    direct_ok = bool(
                        (legacy_count > 0 or modern_count > 0)
                        and signature
                        and signature not in saved_signatures
                    )

                    if direct_ok:
                        save_current_page(
                            page_obj,
                            temp_dir,
                            target_page,
                        )
                        saved_signatures.add(signature)
                        saved_count += 1
                        print(
                            f"  - {target_page}페이지 direct URL fallback 성공"
                        )
                        time.sleep(random.uniform(0.15, 0.45))
                        continue
                except Exception:
                    pass

                crawl_error = f"{target_page}페이지 확보 실패"
                print(f"  ! {crawl_error}")
                break

        except Exception as e:
            crawl_error = f"브라우저 크롤링 예외: {e}"
            print(f"  ! {crawl_error}")
            traceback.print_exc()
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    print(f"\n  - 현재 실행에서 저장된 HTML: {saved_count}개")

    df = parse_saved_pages(category_name, temp_dir)

    if df.empty:
        print("최종 데이터가 없습니다.")
        return False

    expected_columns = get_expected_columns(category_name)
    for column in expected_columns:
        if column not in df.columns:
            df[column] = None

    df = df[expected_columns]

    print("\n[상품명 파싱 검증]")
    print(f"중복 제거 후 행 수: {len(df)}")
    print(f"서로 다른 상품명 수: {df['name'].nunique()}")

    completed = (
        crawl_error is None
        and saved_count == total_pages
    )

    output_path = get_output_path(category_name)

    if not completed:
        root, ext = os.path.splitext(output_path)
        partial_path = f"{root}.partial{ext or '.csv'}"
        df.to_csv(
            partial_path,
            index=False,
            encoding="utf-8-sig",
        )

        print(
            f"\n[실패] {category_name}: "
            f"{saved_count}/{total_pages}페이지만 확보"
        )
        if crawl_error:
            print(f"  원인: {crawl_error}")
        print(
            "  기존 정상 CSV는 덮어쓰지 않았습니다.\n"
            f"  부분 결과: {partial_path}"
        )
        return False

    df.to_csv(
        output_path,
        index=False,
        encoding="utf-8-sig",
    )

    print(
        f"\n★ {category_name} 완료: "
        f"{len(df)}개 고유 데이터 확보 "
        f"({saved_count}/{total_pages}페이지)"
    )
    print(f"  저장 위치: {output_path}")

    return True

# =========================================================
# 전체 카테고리 실행
# =========================================================
if __name__ == "__main__":
    parts_list = [
        {"name": "CPU", "code": "112747", "page": 12},
        {"name": "GPU", "code": "112753", "page": 12},
        {"name": "Mainboard", "code": "112751", "page": 12},
        {"name": "Power", "code": "112777", "page": 12},
        {"name": "RAM", "code": "112752", "page": 15},
        {"name": "SSD", "code": "112760", "page": 15},
        {"name": "Case", "code": "112775", "page": 12},
        {"name": "Cooler", "code": "11336857", "page": 12},
    ]

    success_count = 0
    fail_count = 0

    print("=" * 70)
    print("다나와 전체 부품 크롤링 시작")
    print(f"crawler version: {CRAWLER_VERSION}")
    print(f"대상 카테고리: {len(parts_list)}개")
    print("=" * 70)

    for index, part in enumerate(parts_list, start=1):
        print("\n" + "=" * 70)
        print(
            f"[{index}/{len(parts_list)}] "
            f"{part['name']} 크롤링 시작 "
            f"(cate={part['code']}, pages={part['page']})"
        )
        print("=" * 70)

        try:
            ok = crawl_danawa(
                part["name"],
                part["code"],
                part["page"],
            )

            if ok:
                success_count += 1
            else:
                fail_count += 1
                print(
                    f"\n! {part['name']} 크롤링 미완료: "
                    f"목표 페이지를 모두 확보하지 못했습니다."
                )

        except KeyboardInterrupt:
            print("\n사용자 중단 요청으로 전체 크롤링을 종료합니다.")
            raise
        except Exception as e:
            fail_count += 1
            print(f"\n! {part['name']} 크롤링 중 예외 발생: {e}")
            traceback.print_exc()

        if index < len(parts_list):
            wait_seconds = random.uniform(5.0, 8.0)
            print(f"\n다음 카테고리까지 {wait_seconds:.1f}초 대기...")
            time.sleep(wait_seconds)

    print("\n" + "=" * 70)
    print("다나와 전체 부품 크롤링 종료")
    print(f"완료 처리: {success_count}개")
    print(f"예외 발생: {fail_count}개")
    print("=" * 70)
