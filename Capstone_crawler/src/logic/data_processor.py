import os
import re
import math
import traceback
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import numpy as np
import pandas as pd


SAVE_MATCH_DEBUG = True

# RAM 벤치 데이터는 현재 DDR4 위주이므로, DDR5 등 직접 대응 데이터가 없을 때
# benchmark 데이터로 회귀식을 동적으로 보정하여 스펙 기반 proxy score를 생성한다.
# False로 두면 실제/스펙 proxy로 찾지 못한 RAM은 bench_score=None으로 남긴다.
RAM_ALLOW_ESTIMATED_FALLBACK = True

# SSD 동일 모델의 다른 용량 점수 fallback 허용 범위.
# 4.0이면 1TB -> 4TB, 4TB -> 1TB까지 허용한다.
SSD_MAX_CAPACITY_RATIO = 4.0

# SSD 모델명이 매우 비슷하고 제조사/시리즈가 일치할 때 family fallback 허용.
SSD_ALLOW_MODEL_FAMILY_FALLBACK = True

# SSD 3차: 매칭률 우선 모드
SSD_AGGRESSIVE_MATCHING = True
SSD_FAMILY_MIN_SIMILARITY = 0.80
SSD_FAMILY_MAX_CAPACITY_RATIO = 2.0
SSD_SERIES_MAX_CAPACITY_RATIO = 2.0

# SSD 4차: 모델 단위 매칭이 모두 실패하면 동일 제조사의 비슷한 스펙군 중앙값을 사용한다.
# 실제 동일 제품 벤치가 아니므로 match_type=brand_spec_proxy 로 별도 표시한다.
SSD_ALLOW_BRAND_SPEC_PROXY = True
SSD_BRAND_SPEC_MAX_CAPACITY_RATIO = 4.0
SSD_BRAND_SPEC_POOL_SCORE_WINDOW = 15


# =========================================================
# 공통 정규화
# =========================================================

KOREAN_NORMALIZE_REPLACEMENTS = {
    "인텔": " intel ",
    "라이젠": " ryzen ",
    "라데온": " radeon ",
    "지포스": " geforce ",
    "제온": " xeon ",
    "코어": " core ",
    "울트라": " ultra ",
    "프로세서": " processor ",
    "스레드리퍼": " threadripper ",
    "애슬론": " athlon ",
    "펜티엄": " pentium ",
    "셀러론": " celeron ",
    "시리즈": " series ",
    "세대": " generation ",

    # 저장장치 / 메모리 제조사 및 제품군
    "삼성전자": " samsung ",
    "키오시아": " kioxia ",
    "에스케이하이닉스": " sk hynix ",
    "SK하이닉스": " sk hynix ",
    "하이닉스": " hynix ",
    "마이크론": " crucial ",
    "트랜센드": " transcend ",
    "킹스톤": " kingston ",
    "씨게이트": " seagate ",
    "파이어쿠다": " firecuda ",
    "바라쿠다": " barracuda ",
    "솔리다임": " solidigm ",
    "에이서": " acer ",
    "프레데터": " predator ",
    "파이슨": " phison ",
    "타무즈": " tamuz ",
    "화웨이": " huawei ",
    "이메이션": " imation ",
    "샌디스크": " sandisk ",
    "클레브": " klevv ",
    "써멀테이크": " thermaltake ",
    "×": " x ",
}


def normalize_text(text):
    if pd.isna(text):
        return ""

    text = str(text)

    # 긴 문자열부터 치환해야 SK하이닉스 -> hynix 등이 안정적이다.
    for old, new in sorted(
        KOREAN_NORMALIZE_REPLACEMENTS.items(),
        key=lambda item: -len(item[0]),
    ):
        text = text.replace(old, new)

    text = text.lower()

    text = re.sub(r"[\(\)\[\]\{\},/+]", " ", text)
    text = re.sub(r"[^a-z0-9가-힣\s\-@.]", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def compact_text(text):
    return re.sub(r"[^a-z0-9]", "", normalize_text(text))


# =========================================================
# 브랜드
# =========================================================

SSD_RAM_BRAND_ALIASES = {
    "samsung": ["samsung"],
    "crucial": ["crucial", "micron"],
    "wd": ["western digital", "wdc", "wd"],
    "sandisk": ["sandisk"],
    "hynix": ["sk hynix", "hynix"],
    "klevv": ["klevv", "essencore"],
    "kingston": ["kingston"],
    "corsair": ["corsair"],
    "kioxia": ["kioxia"],
    "seagate": ["seagate", "firecuda", "barracuda"],
    "adata": ["adata", "xpg"],
    "lexar": ["lexar"],
    "transcend": ["transcend"],
    "teamgroup": ["teamgroup", "team group", "team"],
    "solidigm": ["solidigm"],
    "gigabyte": ["gigabyte", "aorus"],
    "fanxiang": ["fanxiang"],
    "oloy": ["oloy"],
    "pny": ["pny"],
    "patriot": ["patriot"],
    "hiksemi": ["hiksemi"],
    "phison": ["phison"],
    "tamuz": ["tamuz"],
    "huawei": ["huawei", "ekitstor"],
    "siliconpower": ["silicon power"],
    "addlink": ["addlink"],
    "sabrent": ["sabrent"],
    "biwin": ["biwin"],
    "msi": ["msi", "spatium"],
    "longsys": ["longsys", "foresee"],
    "apacer": ["apacer"],
    "imation": ["imation"],
    "agi": ["agi"],
    "colorful": ["colorful"],
    "acer": ["acer", "predator"],
    "fastro": ["fastro"],
    "cusu": ["cusu", "twsc"],
    "biostar": ["biostar"],
    "ymtc": ["ymtc", "zhitai"],
    "afox": ["afox"],
    "gskill": ["g.skill", "g skill", "gskill"],
    "geil": ["geil"],
    "thermaltake": ["thermaltake"],
    "vcolor": ["v-color", "v color"],
    "outton": ["outton"],
    "gudga": ["gudga"],
}


def _contains_alias(normalized, alias):
    alias = normalize_text(alias)
    if not alias:
        return False

    pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
    return bool(re.search(pattern, normalized, re.I))


def extract_explicit_ssd_ram_brands(text):
    """
    제품명에 제조사/브랜드가 실제 문자열로 명시된 경우만 반환한다.

    extract_brand()의 SKU 추론(예: TSxxx -> Transcend)은 정확 매칭에는 도움이 되지만,
    brand_spec_proxy에서 사용하면 APPLE SSD TS128 같은 제품을 Transcend로 오인할 수 있다.
    따라서 제조사 단위 proxy에서는 이 함수를 사용한다.
    """
    normalized = normalize_text(text)
    brands = set()

    for brand, aliases in SSD_RAM_BRAND_ALIASES.items():
        for alias in aliases:
            if _contains_alias(normalized, alias):
                brands.add(brand)
                break

    if "apacer" in brands:
        brands.discard("acer")

    return brands


def extract_brand(text, part_type):
    text = normalize_text(text)
    brands = set()

    if part_type == "CPU":
        if re.search(r"\bintel\b", text):
            brands.add("intel")

        if (
            re.search(r"\bamd\b", text)
            or re.search(r"\bryzen\b", text)
            or re.search(r"\bthreadripper\b", text)
            or re.search(r"\bepyc\b", text)
            or re.search(r"\bathlon\b", text)
        ):
            brands.add("amd")

        return brands

    if part_type == "GPU":
        if (
            re.search(r"\bnvidia\b", text)
            or re.search(r"\bgeforce\b", text)
            or re.search(r"\brtx\b", text)
            or re.search(r"\bgtx\b", text)
            or re.search(r"\bgt\s*\d", text)
        ):
            brands.add("nvidia")

        if (
            re.search(r"\bamd\b", text)
            or re.search(r"\bradeon\b", text)
            or re.search(r"\brx\s*\d", text)
            or re.search(r"\bai\s*pro\s*r\d", text)
        ):
            brands.add("amd")

        if re.search(r"\bintel\b", text) or re.search(r"\barc\b", text):
            brands.add("intel")

        return brands

    if part_type in {"SSD", "RAM"}:
        for brand, aliases in SSD_RAM_BRAND_ALIASES.items():
            for alias in aliases:
                if _contains_alias(text, alias):
                    brands.add(brand)
                    break

        compact = compact_text(text)

        # SKU 기반 추론
        if re.search(r"ct\d{3,5}[a-z0-9]+ssd\d*", compact, re.I):
            brands.add("crucial")

        if (
            re.search(r"sh(?:g|p)?p\d{2,3}", compact, re.I)
            or re.search(r"hfs\d", compact, re.I)
        ):
            brands.add("hynix")

        if re.search(r"kbg\d", compact, re.I):
            brands.add("kioxia")

        if re.search(r"mz[a-z0-9]+", compact, re.I):
            brands.add("samsung")

        if re.search(r"wds?\d", compact, re.I) or re.search(r"\bwdc\b", text):
            brands.add("wd")

        if re.search(r"\bts\d", text, re.I):
            brands.add("transcend")

        # HIKSEMI 벤치에 HS-SSD-* 형태가 자주 존재
        if re.search(r"\bhs[- ]?ssd\b", text, re.I):
            brands.add("hiksemi")

        # Viper VPxxxx는 Patriot의 SSD 제품군이다.
        if re.search(r"\bviper\b", text, re.I) and re.search(r"\bvp\d", text, re.I):
            brands.add("patriot")

        # GXF-R-* benchmark는 GUDGA GXF 판매명과 대응되는 패턴으로 취급한다.
        if re.search(r"\bgxf[- ]r\b", text, re.I):
            brands.add("gudga")

        # Apacer 안의 acer 문자열 오검출 방지
        if "apacer" in brands:
            brands.discard("acer")

    return brands



# =========================================================
# 최종 CSV/DB 저장용 브랜드 + GPU 칩셋 제조사
# =========================================================
# 최종 brand 컬럼은 CPU / GPU / Mainboard에만 저장한다.
# 주의: 위쪽의 extract_brand() / SSD_RAM_BRAND_ALIASES는
# SSD/RAM 벤치마크 매칭 정확도를 위해 내부적으로 계속 사용한다.
# 즉 SSD/RAM에는 최종 brand 컬럼을 만들지 않지만, 매칭 내부 판별은 유지한다.

FINAL_BRAND_PARTS = {"CPU", "GPU", "Mainboard"}

PRODUCT_BRAND_ALIASES = {
    # CPU
    "INTEL": ["intel", "인텔"],
    "AMD": ["amd"],

    # GPU / Mainboard 제조사
    "ASUS": ["asus"],
    "MSI": ["msi"],
    "GIGABYTE": ["gigabyte", "기가바이트", "aorus"],
    "ASROCK": ["asrock", "애즈락"],
    "BIOSTAR": ["biostar", "바이오스타"],
    "COLORFUL": ["colorful", "컬러풀"],
    "MAXSUN": ["maxsun"],
    "AXLE": ["axle", "액슬"],
    "AFOX": ["afox"],

    # GPU 전용 제조사
    "ZOTAC": ["zotac", "조텍"],
    "GALAX": ["galax", "갤럭시"],
    "PALIT": ["palit"],
    "GAINWARD": ["gainward"],
    "SAPPHIRE": ["sapphire", "사파이어"],
    "POWERCOLOR": ["powercolor", "파워컬러"],
    "XFX": ["xfx"],
    "EMTEK": ["emtek", "이엠텍"],
    "MANLI": ["manli"],
    "PNY": ["pny"],
    "SPARKLE": ["sparkle"],
    "FORSA": ["forsa"],
    "STCOM": ["stcom"],
}

CATEGORY_BRAND_PRIORITY = {
    "CPU": ["INTEL", "AMD"],
    "GPU": [
        "ASUS", "MSI", "GIGABYTE", "ASROCK", "ZOTAC", "GALAX",
        "COLORFUL", "PALIT", "GAINWARD", "SAPPHIRE", "POWERCOLOR",
        "XFX", "EMTEK", "MANLI", "PNY", "SPARKLE", "AXLE",
        "AFOX", "FORSA", "STCOM", "MAXSUN",
    ],
    "Mainboard": [
        "ASUS", "MSI", "GIGABYTE", "ASROCK", "BIOSTAR", "COLORFUL",
        "MAXSUN", "AXLE", "AFOX",
    ],
}


def _brand_alias_match(text, alias):
    normalized = normalize_text(text)
    alias_normalized = normalize_text(alias)
    if not alias_normalized:
        return False

    pattern = rf"(?<![a-z0-9가-힣]){re.escape(alias_normalized)}(?![a-z0-9가-힣])"
    if re.search(pattern, normalized, re.I):
        return True

    alias_compact = compact_text(alias_normalized)
    normalized_compact = compact_text(normalized)
    if len(alias_compact) >= 2:
        return alias_compact in normalized_compact

    return False


def extract_product_brand(text, part_type):
    """최종 CSV/DB용 브랜드. CPU/GPU/Mainboard 외에는 생성하지 않는다."""
    if part_type not in FINAL_BRAND_PARTS or pd.isna(text):
        return None

    priorities = CATEGORY_BRAND_PRIORITY.get(part_type, [])
    for brand in priorities:
        aliases = PRODUCT_BRAND_ALIASES.get(brand, [])
        if any(_brand_alias_match(text, alias) for alias in aliases):
            return brand

    return None


def extract_gpu_chipset_brand(text):
    """GPU 칩셋 제조사를 NVIDIA / AMD / INTEL로 정규화한다."""
    normalized = normalize_text(text)

    if (
        re.search(r"\bnvidia\b", normalized, re.I)
        or re.search(r"\bgeforce\b", normalized, re.I)
        or re.search(r"\brtx\s*\d", normalized, re.I)
        or re.search(r"\bgtx\s*\d", normalized, re.I)
        or re.search(r"\bgt\s*\d", normalized, re.I)
    ):
        return "NVIDIA"

    if (
        re.search(r"\bradeon\b", normalized, re.I)
        or re.search(r"\brx\s*\d{3,4}", normalized, re.I)
        or re.search(r"\bai\s*pro\s*r\d", normalized, re.I)
    ):
        return "AMD"

    if (
        re.search(r"\bintel\b", normalized, re.I)
        or re.search(r"\barc\s*[ab]\d", normalized, re.I)
    ):
        return "INTEL"

    return None

# =========================================================
# 공통 모바일 판별
# =========================================================


def is_mobile_product(text):
    text = normalize_text(text)
    keywords = [
        "laptop",
        "notebook",
        "mobile",
        "max q",
        "max-q",
        "노트북",
    ]
    return any(keyword in text for keyword in keywords)


# =========================================================
# CPU
# =========================================================


def extract_cpu_model_key(text):
    text = normalize_text(text)

    # Intel Core Ultra 7 270K / 270K Plus
    m = re.search(
        r"\bultra\s*([3579]).{0,35}?\b(\d{3})(ks|kf|k|f|t|h|hx|u)?\b",
        text,
        re.I,
    )
    if m:
        tier = m.group(1)
        number = m.group(2)
        suffix = m.group(3) or ""
        after_model = text[m.end():m.end() + 25]
        plus = "-plus" if re.search(r"\bplus\b", after_model, re.I) else ""
        return f"intel-ultra-{tier}-{number}{suffix}{plus}"

    # Intel Core i
    m = re.search(
        r"\bi([3579]).{0,35}?\b(\d{4,5})(ks|kf|te|k|f|t|e)?\b",
        text,
        re.I,
    )
    if m:
        return f"intel-core-i{m.group(1)}-{m.group(2)}{m.group(3) or ''}"

    # Intel Processor 300 / Intel 300
    m = re.search(
        r"\bintel\s+(?:processor\s+)?(\d{3,4})([a-z]{0,2})?\b",
        text,
        re.I,
    )
    if m and not re.search(r"\bcore\b|\bxeon\b|\bceleron\b|\bpentium\b", text):
        return f"intel-processor-{m.group(1)}{m.group(2) or ''}"

    m = re.search(
        r"\b(?:intel\s+)?processor\s+(\d{3,4})([a-z]{0,2})?\b",
        text,
        re.I,
    )
    if m:
        return f"intel-processor-{m.group(1)}{m.group(2) or ''}"

    # Xeon
    if re.search(r"\bxeon\b", text):
        m = re.search(
            r"\b(?:xeon\s*)?([ew])[\s\-]*(\d{4,5})([a-z]{0,3})\b",
            text,
            re.I,
        )
        if m:
            return f"intel-xeon-{m.group(1)}{m.group(2)}{m.group(3) or ''}"

    # Pentium / Celeron
    m = re.search(
        r"\b(pentium|celeron).{0,20}?\b([gjn]\d{3,4}[a-z]?)\b",
        text,
        re.I,
    )
    if m:
        return f"intel-{m.group(1)}-{m.group(2)}"

    # AMD Ryzen
    m = re.search(
        r"\bryzen\s*([3579]).{0,35}?\b(\d{4})(x3d2|x3d|gt|xt|ge|hs|hx|x|g|f|t|h|u)?\b",
        text,
        re.I,
    )
    if m:
        tier = m.group(1)
        number = m.group(2)
        suffix = m.group(3) or ""
        prefix_region = text[m.start():m.start(2)]
        if re.search(r"\bpro\b", prefix_region, re.I):
            return f"amd-ryzen-{tier}-pro-{number}{suffix}"
        return f"amd-ryzen-{tier}-{number}{suffix}"

    # Threadripper
    m = re.search(
        r"\bthreadripper(?:\s+pro)?.{0,20}?\b(\d{4})(wx|x)?\b",
        text,
        re.I,
    )
    if m:
        suffix = m.group(2) or ""
        if re.search(r"\bthreadripper\s+pro\b", text, re.I):
            return f"amd-threadripper-pro-{m.group(1)}{suffix}"
        return f"amd-threadripper-{m.group(1)}{suffix}"

    # EPYC
    m = re.search(r"\bepyc.{0,20}?\b(\d{4})([a-z]{0,3})\b", text, re.I)
    if m:
        return f"amd-epyc-{m.group(1)}{m.group(2) or ''}"

    # Athlon
    m = re.search(r"\bathlon.{0,20}?\b(\d{3,4})([a-z]{0,3})?\b", text, re.I)
    if m:
        return f"amd-athlon-{m.group(1)}{m.group(2) or ''}"

    return None


# =========================================================
# GPU
# =========================================================


def extract_gpu_model_key(text):
    text = normalize_text(text)

    # NVIDIA
    m = re.search(
        r"\b(rtx|gtx|gt)\s*(\d{3,4})(?:\s*(ti))?(?:\s*(super))?(?:\s*(d))?\b",
        text,
        re.I,
    )
    if m:
        modifiers = []
        if m.group(3):
            modifiers.append("ti")
        if m.group(4):
            modifiers.append("super")
        if m.group(5):
            modifiers.append("d")

        key = f"nvidia-{m.group(1)}-{m.group(2)}"
        if modifiers:
            key += "-" + "-".join(modifiers)
        return key

    # AMD Radeon RX
    m = re.search(r"\brx\s*(\d{3,4})(?:\s*(xtx|xt|gre))?\b", text, re.I)
    if m:
        key = f"amd-rx-{m.group(1)}"
        if m.group(2):
            key += f"-{m.group(2)}"
        return key

    # Radeon AI PRO R9700 / R9700S
    m = re.search(r"\b(?:radeon\s+)?ai\s*pro\s*r(\d{4})(s)?\b", text, re.I)
    if m:
        suffix = "s" if m.group(2) else ""
        return f"amd-ai-pro-r{m.group(1)}{suffix}"

    # Intel Arc
    m = re.search(r"\barc\s*([ab]\d{3})\b", text, re.I)
    if m:
        return f"intel-arc-{m.group(1)}"

    return None


def extract_gpu_vram_gb(text):
    text = normalize_text(text)
    values = []
    for m in re.finditer(r"(?<!\d)(\d{1,3})\s*gb\b", text, re.I):
        value = int(m.group(1))
        if 1 <= value <= 128:
            values.append(value)
    return values[-1] if values else None


# =========================================================
# 공통 토큰
# =========================================================


def extract_tokens(text, part_type):
    text = normalize_text(text)

    common_noise = {
        "정품", "멀티팩", "벌크", "박스", "box", "edition", "에디션",
        "대원", "대원cts", "대원씨티에스", "피씨디렉트", "제이씨현",
        "코잇", "인텍앤컴퍼니", "gaming", "게이밍", "oc",
    }

    gpu_noise = {
        "asus", "msi", "gigabyte", "zotac", "galaxy", "colorful",
        "palit", "gainward", "sapphire", "powercolor", "xfx", "emtek",
        "이엠텍", "d6", "d6x", "d7", "gddr6", "gddr6x", "gddr7",
    }

    cpu_noise = {
        "intel", "amd", "processor", "cpu", "generation", "series",
        "그래니트", "릿지", "라파엘", "세잔", "애로우레이크", "랩터레이크", "리프레시",
    }

    ram_noise = {"ram", "memory", "메모리", "package", "패키지"}

    noise = set(common_noise)
    if part_type == "GPU":
        noise |= gpu_noise
    elif part_type == "CPU":
        noise |= cpu_noise
    elif part_type == "RAM":
        noise |= ram_noise

    words = re.findall(
        r"[a-z]+[a-z0-9\-]*|\d+[a-z][a-z0-9\-]*|\d+",
        text,
        re.I,
    )

    result = set()
    for word in words:
        word = word.strip("-").lower()
        if not word or word in noise or len(word) < 2:
            continue
        result.add(word)

    return result


# =========================================================
# SSD
# =========================================================

SSD_VARIANT_TOKENS = {
    "pro", "evo", "plus", "lite", "qvo", "basic", "ultra", "max",
}

SSD_NOISE_TOKENS = {
    "ssd", "nvme", "pcie", "express", "m2", "m", "sata",
    "gen3", "gen4", "gen5", "gen6", "tlc", "qlc", "mlc", "slc",
    "dram", "nand", "internal", "solid", "state", "drive",
    "정품", "벌크", "해외구매", "병행수입", "내장형",
    "heatsink", "히트싱크", "제이씨현", "피씨디렉트", "파인인포", "서린",
}

SSD_DIMENSION_TOKENS = {"2230", "2242", "2260", "2280", "22110"}

SSD_LOW_VALUE_SERIES = {
    "black", "blue", "green", "gold", "platinum", "red",
}

# 이 토큰들은 같은 브랜드만으로는 부족하고 제품군(series)까지 같아야 한다.
SSD_SERIES_REQUIRED_MODELS = {
    "g2", "g3", "g4", "g5", "3d", "v2", "b2", "201",
}

# 숫자형 모델은 다른 브랜드 간 충돌이 많으므로 같은 브랜드를 필수로 한다.
SSD_BRAND_REQUIRED_MODELS = {
    "m100", "980", "970", "520", "500", "700", "800", "900",
}

SSD_MODEL_ALIASES = {
    "p400l": "p400",
    "sn5100s": "sn5100",
    "g4000e": "4000e",
    "q971b": "q971",
    "p41pl": "p41",
}


def canonicalize_ssd_capacity_gb(value):
    if value is None or pd.isna(value):
        return None

    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return None

    capacity_map = {
        1024: 1000,
        2048: 2000,
        3072: 3000,
        4096: 4000,
        6144: 6000,
        8192: 8000,
    }
    return capacity_map.get(value, value)


def extract_ssd_capacity_candidates(text):
    if pd.isna(text):
        return set()

    raw = str(text).lower()
    capacities = set()

    for m in re.finditer(
        r"(?<![a-z0-9])(\d+(?:\.\d+)?)\s*(tb|gb)(?![a-z0-9])",
        raw,
        re.I,
    ):
        value = float(m.group(1))
        if m.group(2).lower() == "tb":
            value *= 1000
        value = canonicalize_ssd_capacity_gb(value)
        if value is not None and 120 <= value <= 16384:
            capacities.add(value)

    for m in re.finditer(
        r"(?<![a-z0-9])(\d{3,5})g(?![a-z0-9])",
        raw,
        re.I,
    ):
        value = canonicalize_ssd_capacity_gb(int(m.group(1)))
        if value is not None and 120 <= value <= 16384:
            capacities.add(value)

    compact = re.sub(r"[^a-z0-9]", "", raw)

    # Crucial CT4000P310SSD8
    m = re.search(r"ct(\d{3,5})[a-z]", compact, re.I)
    if m:
        value = canonicalize_ssd_capacity_gb(int(m.group(1)))
        if value is not None and 120 <= value <= 16384:
            capacities.add(value)

    # SK hynix SHPP41-1000GM
    for m in re.finditer(
        r"sh(?:g|p)?p\d{2,3}[a-z]?[-\s]*(\d{3,5})gm",
        raw,
        re.I,
    ):
        value = canonicalize_ssd_capacity_gb(int(m.group(1)))
        if value is not None and 120 <= value <= 16384:
            capacities.add(value)

    # KIOXIA KBG40ZNS256G
    m = re.search(r"kbg\d+[a-z]*?(\d{3,5})g", compact, re.I)
    if m:
        value = canonicalize_ssd_capacity_gb(int(m.group(1)))
        if value is not None and 120 <= value <= 16384:
            capacities.add(value)

    # CL4-8D512 / CL4-3D512
    for m in re.finditer(r"cl4[-\s]?(?:8d|3d)(\d{3,5})", raw, re.I):
        value = canonicalize_ssd_capacity_gb(int(m.group(1)))
        if value is not None and 120 <= value <= 16384:
            capacities.add(value)

    # WD OEM 1T0 / 2T00
    for m in re.finditer(r"(?<!\d)([1248])t0{1,2}(?!\d)", raw, re.I):
        capacities.add(int(m.group(1)) * 1000)

    # XP2300F256G 같은 모델+용량 SKU
    for token in re.findall(r"[a-z0-9]+", raw, re.I):
        m = re.fullmatch(r"(.+?[a-z])(\d{3,5})g", token, re.I)
        if not m:
            continue
        prefix = m.group(1).lower()
        if not re.search(r"\d", prefix):
            continue
        value = canonicalize_ssd_capacity_gb(int(m.group(2)))
        if value is not None and 120 <= value <= 16384:
            capacities.add(value)

    return capacities


def canonicalize_ssd_model_token(token, full_text=""):
    if token is None:
        return None

    token = str(token).lower().strip()
    full_text = normalize_text(full_text)

    if token in SSD_MODEL_ALIASES:
        token = SSD_MODEL_ALIASES[token]

    # ADATA SX8200NP / SX8200PNP는 SX8200 Pro 계열 표기
    if re.fullmatch(r"sx8200(?:n?p|pnp)", token):
        return "sx8200"

    # SX6000LNP 등 Lite 표기
    if re.fullmatch(r"sx6000l(?:np)?", token):
        return "sx6000"

    return token


def clean_ssd_model_text(text):
    text = normalize_text(text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:tb|gb)\b", " ", text, flags=re.I)
    text = re.sub(r"\bm\s*\.\s*2\b|\bm\s*2\b", " ", text, flags=re.I)
    text = re.sub(r"\bpcie\s*[3456](?:\.\d+)?\b", " ", text, flags=re.I)
    text = re.sub(r"\bgen\s*[3456]\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_ssd_model_tokens(text):
    cleaned = clean_ssd_model_text(text)
    tokens = re.findall(r"[a-z0-9]+", cleaned, re.I)
    model_tokens = set()

    normalized = normalize_text(text)
    compact = compact_text(text)

    # Lexar PLAY X처럼 문자 단독 토큰 2개가 합쳐져 하나의 모델명을 이루는 경우.
    # play 하나만으로 series 매칭하면 충돌 범위가 너무 넓으므로 강한 composite model token으로 만든다.
    if re.search(r"\bplay\s*x\b", normalized, re.I):
        model_tokens.add("playx")

    # 마케팅명/벤치명 표기가 달라도 동일 제품군으로 볼 수 있는 강한 composite token.
    if re.search(r"\bsandisk\b.*\bssd\s+plus\b", normalized, re.I):
        model_tokens.add("ssdplus")

    if re.search(r"\bfury\s+renegade(?:\s+g5)?\b", normalized, re.I):
        model_tokens.add("furyrenegade")

    if re.search(r"\bgxf(?:[-\s]r)?\b", normalized, re.I):
        model_tokens.add("gxf")

    # Crucial SKU -> P310 / P510 / BX500 / E100 / T500
    m = re.search(
        r"ct\d{3,5}([a-z]{1,3}\d{2,4}[a-z]?)ssd\d*",
        compact,
        re.I,
    )
    if m:
        model_tokens.add(canonicalize_ssd_model_token(m.group(1), text))

    # Hynix SHGP31 / SHPP41 -> P31 / P41
    m = re.search(
        r"sh(?:g|p)?(p\d{2,3}[a-z]?)[-\s]*\d{3,5}gm",
        normalize_text(text),
        re.I,
    )
    if m:
        model_tokens.add(canonicalize_ssd_model_token(m.group(1), text))

    # KIOXIA KBG40 -> BG4
    m = re.search(r"k(bg\d)0", compact, re.I)
    if m:
        model_tokens.add(canonicalize_ssd_model_token(m.group(1), text))

    # CL4
    if re.search(r"\bcl4(?:[-\s]?(?:8d|3d)\d*)?\b", normalize_text(text), re.I):
        model_tokens.add("cl4")

    for token in tokens:
        token = token.lower()

        if token in SSD_NOISE_TOKENS or token in SSD_DIMENSION_TOKENS:
            continue

        if token in SSD_VARIANT_TOKENS:
            continue

        # 브랜드 단어 제거
        if any(token == normalize_text(alias) for aliases in SSD_RAM_BRAND_ALIASES.values() for alias in aliases):
            continue

        if re.fullmatch(r"\d+(?:tb|gb)", token, re.I):
            continue
        if re.fullmatch(r"\d{3,5}g", token, re.I):
            continue
        if re.fullmatch(r"[1248]t0{1,2}", token, re.I):
            continue

        # 전체 SKU는 embedded 모델 추출 후 제외
        if re.fullmatch(r"ct\d+[a-z0-9]+ssd\d*", token, re.I):
            continue
        if re.fullmatch(r"sh(?:g|p)?p\d+[a-z0-9]*", token, re.I):
            continue
        if re.fullmatch(r"kbg\d+[a-z0-9]*", token, re.I):
            continue

        # XP2300F256G -> XP2300F
        m = re.fullmatch(r"(.+?[a-z])(\d{3,5})g", token, re.I)
        if m and re.search(r"\d", m.group(1)):
            model_tokens.add(canonicalize_ssd_model_token(m.group(1), text))
            continue

        # C910G 같은 모델은 반드시 살린다.
        if re.search(r"[a-z]", token) and re.search(r"\d", token):
            if re.fullmatch(r"(?:gen|pcie|ddr|d)\d+", token, re.I):
                continue
            model_tokens.add(canonicalize_ssd_model_token(token, text))
            continue

        # 숫자형 모델.
        # 용량 표기는 clean_ssd_model_text()에서 이미 "960GB", "2TB"처럼 제거된다.
        # 따라서 남아 있는 960 / 970 / 980 / 990 / 201 등은 모델명으로 본다.
        if re.fullmatch(r"\d{3,4}", token):
            if token not in SSD_DIMENSION_TOKENS:
                model_tokens.add(canonicalize_ssd_model_token(token, text))

    return {token for token in model_tokens if token}


def extract_ssd_series_tokens(text):
    cleaned = clean_ssd_model_text(text)
    words = re.findall(r"[a-z]+", cleaned, re.I)

    brand_words = set()
    for aliases in SSD_RAM_BRAND_ALIASES.values():
        for alias in aliases:
            brand_words |= set(normalize_text(alias).split())

    excluded = SSD_NOISE_TOKENS | SSD_VARIANT_TOKENS | brand_words
    result = set()

    for word in words:
        word = word.lower()
        if word in excluded:
            continue
        if len(word) >= 4 or word in SSD_LOW_VALUE_SERIES:
            result.add(word)

    return result


def extract_ssd_variant_tokens(text):
    normalized = normalize_text(text)
    result = set()

    for token in SSD_VARIANT_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", normalized, re.I):
            result.add(token)

    # 약칭
    if re.search(r"\bp400l\b", normalized, re.I):
        result.add("lite")
    if re.search(r"\bsx8200(?:np|pnp)\b", normalized, re.I):
        result.add("pro")
    if re.search(r"\bsx6000l(?:np)?\b", normalized, re.I):
        result.add("lite")

    # Solidigm benchmark의 P41PL은 P41 PLUS 축약형으로 취급한다.
    if re.search(r"\bp41pl\b", normalized, re.I):
        result.add("plus")

    return result


def extract_ssd_revision_tokens(text):
    normalized = normalize_text(text)
    result = set()

    for m in re.finditer(r"\bv(\d+)\b", normalized, re.I):
        result.add(f"v{m.group(1)}")

    m = re.search(r"\bcl4[-\s]?(8d|3d)(?:\d+)?\b", normalized, re.I)
    if m:
        result.add(m.group(1).lower())

    return result


def get_ssd_brand_relation(d_brands, b_brands):
    if d_brands and b_brands:
        if not d_brands.isdisjoint(b_brands):
            return "same"

        related_pairs = {
            frozenset(("wd", "sandisk")),
            frozenset(("solidigm", "intel")),
        }
        for d_brand in d_brands:
            for b_brand in b_brands:
                if frozenset((d_brand, b_brand)) in related_pairs:
                    return "related"

        return "conflict"

    return "unknown"


def get_numeric_backbone(token):
    return "".join(re.findall(r"\d+", token or ""))


def get_ssd_model_similarity(d_models, b_models):
    best_similarity = 0.0
    best_pair = None

    for d_model in d_models:
        for b_model in b_models:
            if d_model == b_model:
                similarity = 1.0
            else:
                similarity = SequenceMatcher(None, d_model, b_model).ratio()

                d_num = get_numeric_backbone(d_model)
                b_num = get_numeric_backbone(b_model)

                # 숫자 핵심이 다르면 비슷한 철자만으로 family 처리하지 않는다.
                if d_num and b_num and d_num != b_num:
                    similarity = min(similarity, 0.60)

            if similarity > best_similarity:
                best_similarity = similarity
                best_pair = (d_model, b_model)

    return best_similarity, best_pair


# 검증된 family alias만 허용한다.
# generic fuzzy를 열어두면 VP4300 -> VP4300L, PM9C1 -> PM9C1b 같은
# 서로 다른 제품/리비전이 붙을 수 있으므로 whitelist 방식으로 제한한다.
SSD_SAFE_FAMILY_MODEL_PAIRS = {
    frozenset(("as2280p4x", "as2280p4")),
    frozenset(("as350x", "as350")),
}


def is_safe_ssd_family_pair(pair, d, b):
    if not pair:
        return False

    left, right = pair
    pair_key = frozenset((left, right))
    relation = get_ssd_brand_relation(d["brands"], b["brands"])

    if pair_key in SSD_SAFE_FAMILY_MODEL_PAIRS:
        return relation == "same"

    if not SSD_AGGRESSIVE_MATCHING:
        return False

    # 공격 모드에서도 서로 다른 제조사의 유사 모델은 차단한다.
    if relation not in {"same", "related"}:
        return False

    similarity = SequenceMatcher(None, left, right).ratio()
    left_num = get_numeric_backbone(left)
    right_num = get_numeric_backbone(right)

    # 숫자 핵심이 같고 suffix/revision만 다른 경우 동일 계열로 허용.
    # PM9A1a -> PM9A1, PM9C1 -> PM9C1a/b, VP4300 -> VP4300L,
    # AST280X -> AST280, AS2280Q4X -> AS2280Q4 같은 케이스를 살린다.
    if left_num and right_num and left_num == right_num:
        return similarity >= SSD_FAMILY_MIN_SIMILARITY

    # 단순 확장형도 같은 제조사에서는 허용한다.
    if relation == "same" and (left.startswith(right) or right.startswith(left)):
        return similarity >= SSD_FAMILY_MIN_SIMILARITY

    # WD/SanDisk 같은 related 브랜드는 더 엄격하게 제한.
    if relation == "related":
        return similarity >= 0.92

    return similarity >= 0.90

def has_ssd_variant_conflict(d_variants, b_variants, common_models=None):
    common_models = common_models or set()

    if d_variants and b_variants and d_variants != b_variants:
        return True

    # 같은 base 모델인데 PRO/EVO/Lite/QVO/Plus 등이 한쪽에만 있으면 다른 제품일 수 있다.
    critical = {"pro", "evo", "plus", "lite", "qvo", "basic", "max"}
    if common_models and d_variants != b_variants and (d_variants | b_variants) & critical:
        return True

    return False


def get_ssd_capacity_relation(d_caps, b_caps):
    if not d_caps:
        return "unknown", None, None, None

    if not b_caps:
        return "benchmark_capacity_unknown", None, None, 0.0

    common = d_caps & b_caps
    if common:
        cap = min(common)
        return "exact", cap, cap, 0.0

    best = None
    for d_cap in d_caps:
        for b_cap in b_caps:
            if d_cap <= 0 or b_cap <= 0:
                continue
            ratio = max(d_cap, b_cap) / min(d_cap, b_cap)
            distance = abs(math.log(b_cap / d_cap))
            item = (distance, ratio, d_cap, b_cap)
            if best is None or item < best:
                best = item

    if best is None:
        return "unknown", None, None, None

    distance, ratio, d_cap, b_cap = best
    if ratio <= SSD_MAX_CAPACITY_RATIO:
        return "capacity_fallback", d_cap, b_cap, distance

    return "capacity_too_far", d_cap, b_cap, distance


def calc_ssd_identity_score(d, b, model_freq, series_freq):
    relation = get_ssd_brand_relation(d["brands"], b["brands"])
    if relation == "conflict":
        return None

    # M.2/NVMe/SATA 내부 SSD를 Portable/USB 외장 SSD 벤치에 붙이지 않는다.
    if d.get("ssd_is_internal") and b.get("ssd_is_external"):
        return None
    if d.get("ssd_is_external") and b.get("ssd_is_internal"):
        return None

    d_models = d["ssd_model_tokens"]
    b_models = b["ssd_model_tokens"]
    common_models = d_models & b_models
    common_series = d["ssd_series_tokens"] & b["ssd_series_tokens"]

    # -----------------------------------------------------
    # 1) exact model token
    # -----------------------------------------------------
    if common_models:
        if has_ssd_variant_conflict(
            d["ssd_variant_tokens"],
            b["ssd_variant_tokens"],
            common_models=common_models,
        ):
            return None

        # G3/G4/3D처럼 짧은 계열 토큰은 같은 브랜드 + 같은 시리즈가 모두 필요하다.
        if all(token in SSD_SERIES_REQUIRED_MODELS for token in common_models):
            if relation != "same" or not common_series:
                return None

        # 아주 짧은 코드인데 제조사조차 식별되지 않으면 우연한 충돌 가능성이 높다.
        if all(len(token) <= 3 for token in common_models) and relation == "unknown":
            return None

        # 970/980/M100 같은 숫자형 모델은 최소한 같은 브랜드여야 한다.
        if all(token in SSD_BRAND_REQUIRED_MODELS for token in common_models):
            if relation != "same":
                return None

        score = 1000
        score += 80 * len(common_models)

        if relation == "same":
            score += 70
        elif relation == "related":
            score += 30
        elif relation == "unknown":
            score += 5

        for token in common_models:
            frequency = model_freq.get(token, 999999)
            if frequency <= 2:
                score += 60
            elif frequency <= 10:
                score += 35
            elif frequency <= 50:
                score += 15

        for token in common_series:
            if token not in SSD_LOW_VALUE_SERIES:
                score += 15

        if d.get("ssd_has_heatsink") == b.get("ssd_has_heatsink"):
            score += 20
        elif d.get("ssd_has_heatsink") or b.get("ssd_has_heatsink"):
            score -= 25

        return {
            "identity_score": score,
            "identity_type": "model_exact",
            "model_similarity": 1.0,
            "model_pair": None,
        }

    # -----------------------------------------------------
    # 2) model family fuzzy
    # AS2280P4X -> AS2280P4 같은 경우
    # -----------------------------------------------------
    if SSD_ALLOW_MODEL_FAMILY_FALLBACK and d_models and b_models:
        similarity, pair = get_ssd_model_similarity(d_models, b_models)

        if pair is not None:
            min_len = min(len(pair[0]), len(pair[1]))
        else:
            min_len = 0

        if similarity >= SSD_FAMILY_MIN_SIMILARITY and min_len >= 4:
            if not is_safe_ssd_family_pair(pair, d, b):
                return None

            if relation not in {"same", "related"} and not common_series:
                return None

            if has_ssd_variant_conflict(
                d["ssd_variant_tokens"],
                b["ssd_variant_tokens"],
            ):
                return None

            score = 820 + int(similarity * 100)
            if relation == "same":
                score += 50
            elif relation == "related":
                score += 20

            score += 10 * len(common_series)

            return {
                "identity_score": score,
                "identity_type": "model_family",
                "model_similarity": similarity,
                "model_pair": pair,
            }

    # -----------------------------------------------------
    # 3) 브랜드 + 시리즈
    # capacity가 없는 KIOXIA-EXCERIA PLUS G4 같은 benchmark를 살리기 위한 단계
    # -----------------------------------------------------
    # 양쪽에 서로 다른 모델 코드가 있으면 시리즈명만으로 붙이지 않는다.
    # 예: LEGEND 960 -> LEGEND 970, MARS 980 BLADE -> GAMMIX S70 BLADE
    if relation == "same" and common_series and not (d_models and b_models):
        meaningful_series = {
            token
            for token in common_series
            if token not in SSD_LOW_VALUE_SERIES
            and series_freq.get(token, 999999) <= 60
        }

        if meaningful_series:
            critical = {"pro", "evo", "plus", "lite", "qvo", "basic", "max"}
            if (
                d["ssd_variant_tokens"] != b["ssd_variant_tokens"]
                and (d["ssd_variant_tokens"] | b["ssd_variant_tokens"]) & critical
            ):
                return None

            score = 650 + 30 * len(meaningful_series)
            score += 15 * len(
                d["ssd_variant_tokens"] & b["ssd_variant_tokens"]
            )

            # 제품명에 모델 코드가 없는데 benchmark 쪽에만 특정 모델 코드가 있으면
            # generic series benchmark보다 낮게 평가한다.
            if not d_models and b_models:
                score -= 120
            elif d_models and not b_models:
                score -= 40

            if d.get("ssd_has_heatsink") == b.get("ssd_has_heatsink"):
                score += 30
            elif d.get("ssd_has_heatsink") or b.get("ssd_has_heatsink"):
                score -= 40

            return {
                "identity_score": score,
                "identity_type": "series_exact",
                "model_similarity": None,
                "model_pair": None,
            }

    return None


def build_ssd_candidate_index(bench_list):
    model_index = defaultdict(set)
    numeric_index = defaultdict(set)
    series_index = defaultdict(set)
    brand_index = defaultdict(set)
    explicit_brand_index = defaultdict(set)

    for idx, benchmark in enumerate(bench_list):
        info = benchmark["info"]

        for token in info["ssd_model_tokens"]:
            model_index[token].add(idx)
            numeric = get_numeric_backbone(token)
            if numeric:
                numeric_index[numeric].add(idx)

        for token in info["ssd_series_tokens"]:
            series_index[token].add(idx)

        for brand in info.get("brands", set()):
            brand_index[brand].add(idx)

        for brand in extract_explicit_ssd_ram_brands(benchmark["name"]):
            explicit_brand_index[brand].add(idx)

    return {
        "model": model_index,
        "numeric": numeric_index,
        "series": series_index,
        "brand": brand_index,
        "explicit_brand": explicit_brand_index,
    }


def _ssd_alpha_prefix(token):
    if not token:
        return ""
    m = re.match(r"^([a-z]+)", token.lower())
    return m.group(1) if m else ""


def _ssd_numeric_distance_bonus(left, right):
    left_num = get_numeric_backbone(left)
    right_num = get_numeric_backbone(right)

    if not left_num or not right_num:
        return 0.0

    # 자릿수가 다르면 SN850 -> SN8100 같은 세대 표기일 수 있으므로
    # 숫자 거리 자체를 신뢰하지 않는다.
    if len(left_num) != len(right_num):
        return 0.0

    try:
        distance = abs(int(left_num) - int(right_num))
    except ValueError:
        return 0.0

    return 18.0 / (1.0 + distance / 10.0)


def find_ssd_brand_model_proxy(danawa_info, bench_list, candidate_index):
    """
    공격 모드의 최종 근접 모델 fallback.

    같은 제조사 + 같은 인터페이스 + 비슷한 모델 접두어를 가진 제품만 대상으로 한다.
    정확히 동일 모델이 아니므로 match_type=brand_model_proxy로 별도 표시한다.
    """
    if not SSD_AGGRESSIVE_MATCHING:
        return None

    if not danawa_info.get("brands"):
        return None

    candidate_ids = set()
    for brand in danawa_info["brands"]:
        candidate_ids.update(candidate_index["brand"].get(brand, set()))

    if not candidate_ids:
        return None

    d_models = danawa_info.get("ssd_model_tokens", set())
    if not d_models:
        return None

    d_interface = danawa_info.get("ssd_interface")
    d_low_series = danawa_info.get("ssd_series_tokens", set()) & SSD_LOW_VALUE_SERIES

    proxy_candidates = []

    for benchmark_id in candidate_ids:
        benchmark = bench_list[benchmark_id]
        b_info = benchmark["info"]
        b_models = b_info.get("ssd_model_tokens", set())

        if not b_models:
            continue

        b_interface = b_info.get("ssd_interface")
        if d_interface and b_interface and d_interface != b_interface:
            continue

        # BLACK/BLUE/RED/GREEN 등 라인명이 양쪽에 명시된 경우 서로 다르면 차단.
        b_low_series = b_info.get("ssd_series_tokens", set()) & SSD_LOW_VALUE_SERIES
        if d_low_series and b_low_series and d_low_series.isdisjoint(b_low_series):
            continue

        # PRO/Lite/EVO 등의 명시적 변형명이 양쪽 모두 있는데 다르면 차단.
        d_variants = danawa_info.get("ssd_variant_tokens", set())
        b_variants = b_info.get("ssd_variant_tokens", set())
        if d_variants and b_variants and d_variants != b_variants:
            continue

        cap_type, d_cap, b_cap, distance = get_ssd_capacity_relation(
            danawa_info.get("ssd_capacities", set()),
            b_info.get("ssd_capacities", set()),
        )

        if cap_type == "capacity_too_far":
            continue

        best_pair = None
        best_similarity = 0.0
        best_pair_score = None

        for d_model in d_models:
            for b_model in b_models:
                d_prefix = _ssd_alpha_prefix(d_model)
                b_prefix = _ssd_alpha_prefix(b_model)

                # SNxxx, PCxxx, AIxxx, Nxxx 같은 동일 계열 접두어가 있어야 한다.
                if not d_prefix or not b_prefix or d_prefix != b_prefix:
                    continue

                similarity = SequenceMatcher(None, d_model, b_model).ratio()
                if similarity < 0.65:
                    continue

                pair_score = similarity * 100.0
                pair_score += _ssd_numeric_distance_bonus(d_model, b_model)

                if best_pair_score is None or pair_score > best_pair_score:
                    best_pair_score = pair_score
                    best_similarity = similarity
                    best_pair = (d_model, b_model)

        if best_pair is None:
            continue

        common_series = (
            danawa_info.get("ssd_series_tokens", set())
            & b_info.get("ssd_series_tokens", set())
        )

        proxy_score = 520 + int(best_pair_score)
        proxy_score += 12 * len(common_series)

        if d_low_series and b_low_series and not d_low_series.isdisjoint(b_low_series):
            proxy_score += 30

        if cap_type == "exact":
            proxy_score += 35
        elif cap_type == "capacity_fallback":
            proxy_score += max(5, 20 - int((distance or 0.0) * 8))
        elif cap_type == "benchmark_capacity_unknown":
            proxy_score += 10

        proxy_candidates.append({
            "benchmark": benchmark,
            "final_score": proxy_score,
            "identity_type": "brand_model_proxy",
            "capacity_match_type": cap_type,
            "matched_capacity": b_cap,
            "capacity_distance": distance,
            "model_similarity": best_similarity,
            "model_pair": best_pair,
        })

    if not proxy_candidates:
        return None

    # 모델 유사도/라인/용량을 우선하고, 동일 tier에서는 최고값이 아니라 중앙값 대표 행을 쓴다.
    top_score = max(item["final_score"] for item in proxy_candidates)
    top = [item for item in proxy_candidates if item["final_score"] == top_score]
    median_score = float(np.median([item["benchmark"]["score"] for item in top]))
    best = min(
        top,
        key=lambda item: (
            abs(item["benchmark"]["score"] - median_score),
            item["benchmark"]["name"],
        ),
    )

    return {
        "benchmark": best["benchmark"],
        "match_score": best["final_score"],
        "match_type": "brand_model_proxy",
        "capacity_match_type": best["capacity_match_type"],
        "matched_capacity": best["matched_capacity"],
        "capacity_distance": best["capacity_distance"],
        "candidate_count": len(proxy_candidates),
        "model_similarity": best["model_similarity"],
        "model_pair": best["model_pair"],
    }


def is_likely_ssd_benchmark_for_proxy(info):
    """brand_spec_proxy 전용: HDD/USB/외장 저장장치가 proxy 후보에 섞이지 않게 한다."""
    normalized = info.get("normalized", "")

    if info.get("ssd_is_external"):
        return False

    if re.search(r"\b(hdd|hard\s*drive|jetflash|usb)\b", normalized, re.I):
        return False

    return bool(
        re.search(
            r"\b(nvme|ssd|sata|pcie|pci-e|solid\s+state)\b|m\s*\.\s*2",
            normalized,
            re.I,
        )
    )


def find_ssd_brand_spec_proxy(danawa_info, bench_list, candidate_index):
    """
    모델/시리즈/근접 모델 매칭이 전부 실패했을 때 사용하는 마지막 제조사 기반 proxy.

    조건:
      1) benchmark 이름에 동일 제조사가 명시되어 있어야 함
      2) NVMe/SATA가 양쪽 모두 식별되면 서로 같아야 함
      3) 용량이 식별되면 SSD_BRAND_SPEC_MAX_CAPACITY_RATIO 이내
      4) BLACK/BLUE/RED 같은 라인이 양쪽에 명시되면 충돌 금지
      5) 최상위 스펙 후보군의 benchmark score 중앙값에 가장 가까운 실제 행을 사용

    이 결과는 실제 동일 제품 벤치가 아니므로 반드시 brand_spec_proxy로 표시한다.
    """
    if not SSD_ALLOW_BRAND_SPEC_PROXY:
        return None

    d_brands = danawa_info.get("brands", set())
    if not d_brands:
        return None

    candidate_ids = set()
    explicit_index = candidate_index.get("explicit_brand", {})

    for brand in d_brands:
        candidate_ids.update(explicit_index.get(brand, set()))

    if not candidate_ids:
        return None

    d_interface = danawa_info.get("ssd_interface")
    d_external = danawa_info.get("ssd_is_external", False)
    d_internal = danawa_info.get("ssd_is_internal", False)
    d_low_series = danawa_info.get("ssd_series_tokens", set()) & SSD_LOW_VALUE_SERIES
    d_variants = danawa_info.get("ssd_variant_tokens", set())

    candidates = []

    for benchmark_id in candidate_ids:
        benchmark = bench_list[benchmark_id]
        b_info = benchmark["info"]

        # 실제 문자열에 나타난 브랜드가 다나와 브랜드와 반드시 겹쳐야 한다.
        explicit_b_brands = extract_explicit_ssd_ram_brands(benchmark["name"])
        if not explicit_b_brands or d_brands.isdisjoint(explicit_b_brands):
            continue

        # proxy 단계에서는 HDD/USB/외장 benchmark를 아예 제외한다.
        if not is_likely_ssd_benchmark_for_proxy(b_info):
            continue

        # 외장/내장 충돌 차단.
        if d_internal and b_info.get("ssd_is_external"):
            continue
        if d_external and b_info.get("ssd_is_internal"):
            continue

        b_interface = b_info.get("ssd_interface")

        # NVMe 제품에 인터페이스를 모르는 benchmark를 섞으면 SATA SSD까지 중앙값에
        # 들어갈 수 있으므로 NVMe는 NVMe가 명시된 benchmark만 사용한다.
        if d_interface == "nvme" and b_interface != "nvme":
            continue

        # SATA는 benchmark 이름에 SATA가 생략되는 경우가 많아 unknown까지 허용하되
        # NVMe로 명시된 후보는 제외한다.
        if d_interface == "sata" and b_interface == "nvme":
            continue

        if d_interface and b_interface and d_interface != b_interface:
            continue

        # 제품 라인이 양쪽에 명시된 경우 BLACK ↔ BLUE 같은 충돌 차단.
        b_low_series = b_info.get("ssd_series_tokens", set()) & SSD_LOW_VALUE_SERIES
        if d_low_series and b_low_series and d_low_series.isdisjoint(b_low_series):
            continue

        # 명시적인 PRO/EVO/Lite 등의 변형이 양쪽에 있는데 다르면 제외.
        b_variants = b_info.get("ssd_variant_tokens", set())
        if d_variants and b_variants and d_variants != b_variants:
            continue

        cap_type, d_cap, b_cap, distance = get_ssd_capacity_relation(
            danawa_info.get("ssd_capacities", set()),
            b_info.get("ssd_capacities", set()),
        )

        if cap_type == "capacity_too_far":
            continue

        if cap_type == "capacity_fallback":
            ratio = math.exp(distance or 0.0)
            if ratio > SSD_BRAND_SPEC_MAX_CAPACITY_RATIO:
                continue

        # proxy 후보 품질 점수. 벤치 점수 자체는 여기에 포함하지 않는다.
        # 높은 benchmark score를 우선하면 제품 성능이 과대평가되기 때문이다.
        quality = 0

        if d_interface and b_interface:
            quality += 80
        elif d_interface or b_interface:
            quality += 35
        else:
            quality += 15

        if cap_type == "exact":
            quality += 100
        elif cap_type == "capacity_fallback":
            ratio = math.exp(distance or 0.0)
            quality += max(25, 75 - int((ratio - 1.0) * 18))
        elif cap_type == "benchmark_capacity_unknown":
            quality += 20
        else:
            quality += 5

        common_series = (
            danawa_info.get("ssd_series_tokens", set())
            & b_info.get("ssd_series_tokens", set())
        )
        meaningful_common_series = common_series - SSD_LOW_VALUE_SERIES
        quality += 15 * len(meaningful_common_series)

        if d_low_series and b_low_series and not d_low_series.isdisjoint(b_low_series):
            quality += 25

        if d_variants and b_variants and d_variants == b_variants:
            quality += 15

        candidates.append({
            "benchmark": benchmark,
            "quality": quality,
            "capacity_match_type": cap_type,
            "matched_capacity": b_cap,
            "capacity_distance": distance,
        })

    if not candidates:
        return None

    # 가장 좋은 스펙군만 남기되 1개짜리 최고 후보가 우연히 튀는 것을 막기 위해
    # 일정 score window 안의 후보를 함께 묶어 중앙값 대표값을 선택한다.
    best_quality = max(item["quality"] for item in candidates)
    pool = [
        item
        for item in candidates
        if item["quality"] >= best_quality - SSD_BRAND_SPEC_POOL_SCORE_WINDOW
    ]

    if not pool:
        return None

    median_score = float(np.median([item["benchmark"]["score"] for item in pool]))
    representative = min(
        pool,
        key=lambda item: (
            abs(item["benchmark"]["score"] - median_score),
            item["benchmark"]["name"],
        ),
    )

    return {
        "benchmark": representative["benchmark"],
        "match_score": 350 + representative["quality"],
        "match_type": "brand_spec_proxy",
        "capacity_match_type": representative["capacity_match_type"],
        "matched_capacity": representative["matched_capacity"],
        "capacity_distance": representative["capacity_distance"],
        "candidate_count": len(pool),
        "model_similarity": None,
        "model_pair": None,
    }


def find_best_ssd_benchmark(
    danawa_info,
    bench_list,
    model_freq,
    series_freq,
    candidate_index,
):
    candidates = []
    candidate_ids = set()

    for token in danawa_info["ssd_model_tokens"]:
        candidate_ids.update(candidate_index["model"].get(token, set()))
        numeric = get_numeric_backbone(token)
        if numeric:
            candidate_ids.update(candidate_index["numeric"].get(numeric, set()))

    # 모델 코드가 benchmark에 없더라도 EXCERIA / FUTURE / WAVE 같은
    # 시리즈 단위 benchmark가 있을 수 있으므로 함께 후보에 넣는다.
    for token in danawa_info["ssd_series_tokens"]:
        candidate_ids.update(candidate_index["series"].get(token, set()))

    for benchmark_id in candidate_ids:
        benchmark = bench_list[benchmark_id]
        b_info = benchmark["info"]

        identity = calc_ssd_identity_score(
            danawa_info,
            b_info,
            model_freq,
            series_freq,
        )
        if identity is None:
            continue

        cap_type, d_cap, b_cap, distance = get_ssd_capacity_relation(
            danawa_info["ssd_capacities"],
            b_info["ssd_capacities"],
        )

        # family/series fallback에 다른 용량 fallback까지 겹치면 오매칭 가능성이 증가한다.
        # 다만 같은 브랜드 계열의 모델 철자가 90% 이상 같고 480/512GB처럼 용량 차이가 작으면 허용한다.
        if identity["identity_type"] == "series_exact":
            if cap_type == "capacity_fallback":
                capacity_ratio = math.exp(distance or 0.0)
                if capacity_ratio > SSD_SERIES_MAX_CAPACITY_RATIO:
                    continue
            elif cap_type not in {"exact", "benchmark_capacity_unknown"}:
                continue
        elif identity["identity_type"] == "model_family":
            if cap_type == "capacity_fallback":
                similarity = identity.get("model_similarity") or 0.0
                capacity_ratio = math.exp(distance or 0.0)
                if similarity < SSD_FAMILY_MIN_SIMILARITY or capacity_ratio > SSD_FAMILY_MAX_CAPACITY_RATIO:
                    continue
            elif cap_type not in {"exact", "benchmark_capacity_unknown"}:
                continue

        if cap_type == "capacity_too_far":
            continue

        if cap_type == "exact":
            capacity_score = 120
        elif cap_type == "benchmark_capacity_unknown":
            capacity_score = 90
        elif cap_type == "capacity_fallback":
            capacity_score = max(10, 70 - int((distance or 0.0) * 35))
        else:
            capacity_score = 0

        final_score = identity["identity_score"] + capacity_score

        candidates.append({
            "benchmark": benchmark,
            "final_score": final_score,
            "identity_score": identity["identity_score"],
            "identity_type": identity["identity_type"],
            "capacity_match_type": cap_type,
            "danawa_capacity": d_cap,
            "matched_capacity": b_cap,
            "capacity_distance": distance,
            "model_similarity": identity["model_similarity"],
            "model_pair": identity["model_pair"],
        })

    if not candidates:
        proxy_result = find_ssd_brand_model_proxy(
            danawa_info,
            bench_list,
            candidate_index,
        )
        if proxy_result is not None:
            return proxy_result

        brand_spec_result = find_ssd_brand_spec_proxy(
            danawa_info,
            bench_list,
            candidate_index,
        )
        if brand_spec_result is not None:
            return brand_spec_result

        return {
            "benchmark": None,
            "match_score": None,
            "match_type": "none",
            "capacity_match_type": "none",
            "matched_capacity": None,
            "capacity_distance": None,
            "candidate_count": 0,
            "model_similarity": None,
            "model_pair": None,
        }

    # identity와 capacity가 같은 동급 후보가 여러 개면 최고 벤치점수를 뽑지 않는다.
    # PassMark의 동일 모델 중 최고값만 선택하면 추천 성능이 과대평가될 수 있으므로
    # 최상위 tier 후보의 score 중앙값에 가장 가까운 대표 행을 선택한다.
    def ranking_key(item):
        return (
            item["final_score"],
            item["capacity_match_type"] == "exact",
            item["identity_type"] == "model_exact",
        )

    top_key = max(ranking_key(item) for item in candidates)
    top_candidates = [item for item in candidates if ranking_key(item) == top_key]

    median_score = float(np.median([item["benchmark"]["score"] for item in top_candidates]))
    best = min(
        top_candidates,
        key=lambda item: (
            abs(item["benchmark"]["score"] - median_score),
            item["benchmark"]["name"],
        ),
    )

    return {
        "benchmark": best["benchmark"],
        "match_score": best["final_score"],
        "match_type": best["identity_type"],
        "capacity_match_type": best["capacity_match_type"],
        "matched_capacity": best["matched_capacity"],
        "capacity_distance": best["capacity_distance"],
        "candidate_count": len(candidates),
        "model_similarity": best["model_similarity"],
        "model_pair": best["model_pair"],
    }


# =========================================================
# RAM
# =========================================================

RAM_SPEEDS = {
    1600, 1866, 2133, 2400, 2666, 2800, 2933, 3000, 3200,
    3333, 3466, 3600, 3666, 3733, 3800, 3866, 4000, 4133,
    4266, 4300, 4400, 4500, 4600, 4800, 5000, 5066, 5100,
    5200, 5333, 5400, 5600, 6000, 6200, 6400, 6600, 6800,
    7000, 7200, 7600, 7800, 8000, 8200, 8400,
}


def extract_ram_ddr_type(text):
    normalized = normalize_text(text)

    m = re.search(r"\bddr\s*([345])\b", normalized, re.I)
    if m:
        return f"DDR{m.group(1)}"

    # benchmark SKU 계열 추론
    if (
        re.search(r"\bf4[-\s]", normalized, re.I)
        or re.search(r"\b(?:ud4|sd4|ed4)[-\s]", normalized, re.I)
        or re.search(r"\bkd4[a-z0-9]", normalized, re.I)
        or re.search(r"\bad4[a-z0-9]", normalized, re.I)
        or re.search(r"\bpc4[-\s]", normalized, re.I)
    ):
        return "DDR4"

    if (
        re.search(r"\bf5[-\s]", normalized, re.I)
        or re.search(r"\b(?:ud5|sd5|ed5)[-\s]", normalized, re.I)
        or re.search(r"\bkd5[a-z0-9]", normalized, re.I)
        or re.search(r"\bad5[a-z0-9]", normalized, re.I)
        or re.search(r"\bpc5[-\s]", normalized, re.I)
    ):
        return "DDR5"

    return None


def extract_ram_speed(text):
    normalized = normalize_text(text)

    # DDR5-6000 / DDR4-3200
    m = re.search(r"\bddr[345][\s\-]*(\d{4})\b", normalized, re.I)
    if m:
        value = int(m.group(1))
        if value in RAM_SPEEDS:
            return value

    # G.Skill F4-3200C16
    m = re.search(r"\bf[345][\s\-]*(\d{4})c\d+", normalized, re.I)
    if m:
        value = int(m.group(1))
        if value in RAM_SPEEDS:
            return value

    # Corsair / 기타 3600C18
    m = re.search(r"(?<!\d)(\d{4})c\d{2}(?!\d)", normalized, re.I)
    if m:
        value = int(m.group(1))
        if value in RAM_SPEEDS:
            return value

    # TEAMGROUP-UD4-3600 등
    values = []
    for raw in re.findall(r"(?<!\d)(\d{4})(?!\d)", normalized):
        value = int(raw)
        if value in RAM_SPEEDS:
            values.append(value)

    return max(values) if values else None


def extract_ram_cl(text):
    normalized = normalize_text(text)

    m = re.search(r"\bcl\s*(\d{1,3})\b", normalized, re.I)
    if m:
        value = int(m.group(1))
        if 5 <= value <= 100:
            return value

    # F4-3200C16 / 3600C18
    m = re.search(r"\d{4}c(\d{2})(?!\d)", normalized, re.I)
    if m:
        value = int(m.group(1))
        if 5 <= value <= 100:
            return value

    return None


def extract_ram_capacity_gb(text):
    if pd.isna(text):
        return None

    matches = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*gb\b", str(text), re.I)
    if not matches:
        return None

    value = float(matches[-1])
    if 1 <= value <= 1024:
        return value

    return None


def extract_ram_strong_tokens(text):
    normalized = normalize_text(text)
    tokens = re.findall(r"[a-z0-9][a-z0-9\-./]+", normalized, re.I)
    result = set()

    noise = {
        "ddr4", "ddr5", "memory", "ram", "rgb", "black", "white",
        "package", "패키지", "정품", "벌크",
    }

    for token in tokens:
        token = token.lower().strip("-./")
        if token in noise or len(token) < 5:
            continue

        # DDR4-3200, DDR5-6000, CL16-20-20 등은 제품 SKU가 아니라 공통 스펙이다.
        if re.fullmatch(r"ddr[345][\-]?\d{4}", token, re.I):
            continue
        if re.fullmatch(r"cl\d+(?:-\d+)*", token, re.I):
            continue
        if re.fullmatch(r"\d{3,4}mhz", token, re.I):
            continue

        if re.search(r"[a-z]", token) and re.search(r"\d", token):
            if re.fullmatch(r"ddr[345]", token):
                continue
            result.add(token)

    return result


def build_ram_info(text, row=None):
    info = {
        "original": str(text),
        "normalized": normalize_text(text),
        "brands": extract_brand(text, "RAM"),
        "ddr_type": extract_ram_ddr_type(text),
        "speed": extract_ram_speed(text),
        "cl": extract_ram_cl(text),
        "capacity_gb": extract_ram_capacity_gb(text),
        "module_count": None,
        "module_capacity_gb": None,
        "strong_tokens": extract_ram_strong_tokens(text),
    }

    if row is not None:
        if "memory_type" in row and not pd.isna(row["memory_type"]):
            info["ddr_type"] = str(row["memory_type"]).upper().strip()

        if "memory_clock" in row and not pd.isna(row["memory_clock"]):
            try:
                info["speed"] = int(float(row["memory_clock"]))
            except (TypeError, ValueError):
                pass

        total_capacity = None
        if "capacity" in row and not pd.isna(row["capacity"]):
            try:
                total_capacity = float(row["capacity"])
                info["capacity_gb"] = total_capacity
            except (TypeError, ValueError):
                pass

        module_count = 1
        if "module_count" in row and not pd.isna(row["module_count"]):
            try:
                module_count = max(1, int(float(row["module_count"])))
            except (TypeError, ValueError):
                module_count = 1

        info["module_count"] = module_count
        if total_capacity is not None:
            info["module_capacity_gb"] = total_capacity / module_count

    if info["module_capacity_gb"] is None:
        info["module_capacity_gb"] = info["capacity_gb"]

    return info


def build_ram_estimator(bench_list):
    # benchmark 자체에서 speed / CL / capacity가 동시에 읽히는 행으로 보정한다.
    rows = []

    for benchmark in bench_list:
        info = benchmark["info"]
        speed = info.get("speed")
        cl = info.get("cl")
        capacity = info.get("capacity_gb")

        if speed is None or cl is None or capacity is None:
            continue
        if speed <= 0 or cl <= 0 or capacity <= 0:
            continue

        rows.append((
            float(benchmark["score"]),
            float(speed),
            float(cl),
            float(capacity),
        ))

    if len(rows) < 50:
        return None

    y = np.array([row[0] for row in rows], dtype=float)
    X = np.array([
        [
            1.0,
            math.log2(row[1] / 2133.0),
            100.0 / row[2],
            math.log2(max(row[3], 1.0) / 8.0),
        ]
        for row in rows
    ], dtype=float)

    coefficients, *_ = np.linalg.lstsq(X, y, rcond=None)

    # 극단적인 과적합/외삽을 방지할 범위
    low = float(np.quantile(y, 0.02))
    high = float(np.quantile(y, 0.98))

    return {
        "coefficients": coefficients,
        "min_score": max(1.0, low - 2.0),
        # DDR5 외삽을 고려하여 상단을 조금 열어둔다.
        "max_score": max(high + 8.0, 30.0),
        "sample_count": len(rows),
    }


def estimate_ram_score(info, estimator):
    if estimator is None:
        return None

    speed = info.get("speed")
    cl = info.get("cl")
    capacity = info.get("module_capacity_gb") or info.get("capacity_gb")

    if speed is None or speed <= 0:
        return None

    if cl is None:
        # JEDEC/일반 제품용 보수적 기본값
        if info.get("ddr_type") == "DDR5":
            if speed >= 6400:
                cl = 48
            elif speed >= 5600:
                cl = 46
            else:
                cl = 40
        else:
            if speed >= 3600:
                cl = 18
            elif speed >= 3200:
                cl = 22
            else:
                cl = 19

    if capacity is None or capacity <= 0:
        capacity = 16.0

    x = np.array([
        1.0,
        math.log2(speed / 2133.0),
        100.0 / cl,
        math.log2(max(capacity, 1.0) / 8.0),
    ])

    value = float(np.dot(estimator["coefficients"], x))
    value = max(estimator["min_score"], min(estimator["max_score"], value))
    return int(round(value))


def build_ram_candidate_index(bench_list):
    strong_index = defaultdict(set)
    spec_index = defaultdict(set)

    for idx, benchmark in enumerate(bench_list):
        info = benchmark["info"]

        for token in info.get("strong_tokens", set()):
            strong_index[token].add(idx)

        ddr_type = info.get("ddr_type")
        speed = info.get("speed")
        capacity = info.get("capacity_gb")

        if ddr_type and speed and capacity:
            cap_key = round(float(capacity), 2)
            spec_index[(ddr_type, int(speed), cap_key)].add(idx)

    return {
        "strong": strong_index,
        "spec": spec_index,
    }


def find_best_ram_benchmark(danawa_info, bench_list, estimator, candidate_index):
    # -----------------------------------------------------
    # 1) strong SKU 직접 매칭
    # -----------------------------------------------------
    direct_candidates = []
    direct_ids = set()

    for token in danawa_info["strong_tokens"]:
        direct_ids.update(candidate_index["strong"].get(token, set()))

    for benchmark_id in direct_ids:
        benchmark = bench_list[benchmark_id]
        b_info = benchmark["info"]

        if (
            danawa_info["brands"]
            and b_info["brands"]
            and danawa_info["brands"].isdisjoint(b_info["brands"])
        ):
            continue

        common_strong = danawa_info["strong_tokens"] & b_info["strong_tokens"]
        if not common_strong:
            continue

        if (
            danawa_info["ddr_type"]
            and b_info["ddr_type"]
            and danawa_info["ddr_type"] != b_info["ddr_type"]
        ):
            continue

        score = 500 + 50 * len(common_strong)
        if danawa_info["brands"] & b_info["brands"]:
            score += 50

        direct_candidates.append((score, benchmark))

    if direct_candidates:
        direct_candidates.sort(key=lambda x: (x[0], x[1]["score"]), reverse=True)
        best_score, best = direct_candidates[0]
        return {
            "benchmark": best,
            "bench_score": best["score"],
            "match_score": best_score,
            "match_type": "ram_direct",
            "candidate_count": len(direct_candidates),
        }

    # -----------------------------------------------------
    # 2) 동일 스펙 benchmark proxy
    # 제품명이 아니라 동일 DDR / 속도 / 모듈 용량의 중앙값을 사용.
    # -----------------------------------------------------
    ddr_type = danawa_info.get("ddr_type")
    speed = danawa_info.get("speed")
    module_capacity = danawa_info.get("module_capacity_gb")

    if ddr_type and speed and module_capacity:
        brand_pool = []
        global_pool = []
        spec_ids = set()
        cap_key = round(float(module_capacity), 2)

        for candidate_speed in RAM_SPEEDS:
            if abs(int(candidate_speed) - int(speed)) <= 100:
                spec_ids.update(
                    candidate_index["spec"].get(
                        (ddr_type, int(candidate_speed), cap_key),
                        set(),
                    )
                )

        for benchmark_id in spec_ids:
            benchmark = bench_list[benchmark_id]
            b_info = benchmark["info"]
            global_pool.append(benchmark)

            if (
                danawa_info["brands"]
                and b_info["brands"]
                and not danawa_info["brands"].isdisjoint(b_info["brands"])
            ):
                brand_pool.append(benchmark)

        pool = brand_pool if brand_pool else global_pool

        if pool:
            scores = np.array([item["score"] for item in pool], dtype=float)
            median_score = float(np.median(scores))
            representative = min(
                pool,
                key=lambda item: abs(item["score"] - median_score),
            )

            return {
                "benchmark": representative,
                "bench_score": int(round(median_score)),
                "match_score": 300 + (50 if brand_pool else 0),
                "match_type": "ram_spec_proxy",
                "candidate_count": len(pool),
            }

    # -----------------------------------------------------
    # 3) benchmark 기반 동적 스펙 추정
    # DDR5 benchmark가 없는 현재 데이터에서 잘못된 DDR4 제품명을 억지로 붙이지 않는다.
    # -----------------------------------------------------
    if RAM_ALLOW_ESTIMATED_FALLBACK:
        estimated_score = estimate_ram_score(danawa_info, estimator)
        if estimated_score is not None:
            return {
                "benchmark": None,
                "bench_score": estimated_score,
                "match_score": 100,
                "match_type": "ram_spec_estimate",
                "candidate_count": 0,
            }

    return {
        "benchmark": None,
        "bench_score": None,
        "match_score": None,
        "match_type": "none",
        "candidate_count": 0,
    }


# =========================================================
# SSD 인터페이스 추론
# =========================================================


def extract_ssd_interface(text):
    normalized = normalize_text(text)
    raw = str(text).lower()

    if re.search(r"\b(nvme|pcie|pci-e)\b", normalized, re.I):
        return "nvme"

    if re.search(r"\bsata\b", normalized, re.I):
        return "sata"

    if re.search(r"\b2\s*\.\s*5\b", raw, re.I):
        return "sata"

    # 이름만으로 SATA임이 거의 확실한 제품군만 보완한다.
    if re.search(r"\b(?:sa510|sa500|cs900|gx2|su650|su800|sl500|as350x?|p220)\b", normalized, re.I):
        return "sata"

    return None


# =========================================================
# 모델 정보
# =========================================================


def extract_model_info(text, part_type, row=None):
    brands = extract_brand(text, part_type)

    info = {
        "original": str(text),
        "normalized": normalize_text(text),
        "brands": brands,
        "tokens": extract_tokens(text, part_type),
        "mobile": is_mobile_product(text),
        "model_key": None,
    }

    if part_type == "CPU":
        info["model_key"] = extract_cpu_model_key(text)

    elif part_type == "GPU":
        info["model_key"] = extract_gpu_model_key(text)
        info["gpu_vram_gb"] = extract_gpu_vram_gb(text)

    elif part_type == "SSD":
        capacities = extract_ssd_capacity_candidates(text)

        # 다나와 CSV에 capacity 컬럼이 있으면 이것을 최우선으로 신뢰한다.
        if row is not None and "capacity" in row and not pd.isna(row["capacity"]):
            row_capacity = canonicalize_ssd_capacity_gb(row["capacity"])
            if row_capacity is not None:
                capacities = {row_capacity}

        normalized_ssd = normalize_text(text)
        raw_ssd = str(text).lower()

        info.update({
            "ssd_capacities": capacities,
            "ssd_capacity": max(capacities) if capacities else None,
            "ssd_model_tokens": extract_ssd_model_tokens(text),
            "ssd_series_tokens": extract_ssd_series_tokens(text),
            "ssd_variant_tokens": extract_ssd_variant_tokens(text),
            "ssd_revision_tokens": extract_ssd_revision_tokens(text),
            "ssd_has_heatsink": (
                "heatsink" in normalized_ssd
                or "히트싱크" in raw_ssd
                or "with heatsink" in normalized_ssd
            ),
            "ssd_is_external": bool(
                re.search(r"\b(portable|external|usb)\b", normalized_ssd, re.I)
            ),
            "ssd_is_internal": bool(
                re.search(r"\b(nvme|sata)\b", normalized_ssd, re.I)
                or re.search(r"\bm\s*\.\s*2\b", raw_ssd, re.I)
            ),
            "ssd_interface": extract_ssd_interface(text),
        })

    elif part_type == "RAM":
        info.update(build_ram_info(text, row=row))

    return info


# =========================================================
# CPU / GPU 매칭
# =========================================================


def calc_cpu_gpu_match_score(d, b, part_type):
    if d["brands"] and b["brands"] and d["brands"].isdisjoint(b["brands"]):
        return -1

    if d["mobile"] != b["mobile"]:
        return -1

    if not d["model_key"] or not b["model_key"]:
        return -1

    if d["model_key"] != b["model_key"]:
        return -1

    score = 100

    if part_type == "GPU":
        d_vram = d.get("gpu_vram_gb")
        b_vram = b.get("gpu_vram_gb")

        # RTX 5060 Ti 8GB / 16GB처럼 같은 GPU라도 벤치가 따로 존재한다.
        if d_vram is not None and b_vram is not None:
            if d_vram != b_vram:
                return -1
            score += 30
        elif d_vram is not None or b_vram is not None:
            score += 5

    common_tokens = d["tokens"] & b["tokens"]
    score += len(common_tokens)

    return score


# =========================================================
# Benchmark score
# =========================================================


def normalize_benchmark_score(value):
    if pd.isna(value):
        return None

    text = str(value).replace(",", "").strip()

    try:
        value = float(text)
        if value <= 0:
            return None
        return int(round(value))
    except (TypeError, ValueError):
        return None


# =========================================================
# benchmark 전처리 / 인덱스
# =========================================================


def prepare_benchmark_list(df_bench, part_type):
    bench_list = []

    for _, row in df_bench.iterrows():
        score = normalize_benchmark_score(row["Score"])
        if score is None:
            continue

        name = str(row["Bench_Name"])
        info = extract_model_info(name, part_type)

        bench_list.append({
            "name": name,
            "score": score,
            "info": info,
        })

    return bench_list


def build_cpu_gpu_index(bench_list):
    index = defaultdict(list)
    for benchmark in bench_list:
        key = benchmark["info"].get("model_key")
        if key:
            index[key].append(benchmark)
    return index


def build_ssd_frequency_tables(bench_list):
    model_freq = Counter()
    series_freq = Counter()

    for benchmark in bench_list:
        info = benchmark["info"]
        for token in info["ssd_model_tokens"]:
            model_freq[token] += 1
        for token in info["ssd_series_tokens"]:
            series_freq[token] += 1

    return model_freq, series_freq


# =========================================================
# 메인
# =========================================================


def match_data(part_type):
    print()
    print("=" * 70)
    print(f"{part_type} 벤치마크 매칭 시작")
    print("=" * 70)

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
    data_dir = os.path.join(project_root, "data")

    danawa_path = os.path.join(data_dir, f"data_{part_type}.csv")
    benchmark_path = os.path.join(data_dir, f"total_bench_{part_type}.csv")

    if not os.path.exists(danawa_path):
        raise FileNotFoundError(f"다나와 CSV 없음: {danawa_path}")

    if not os.path.exists(benchmark_path):
        raise FileNotFoundError(f"벤치마크 CSV 없음: {benchmark_path}")

    df_danawa = pd.read_csv(danawa_path)
    df_bench = pd.read_csv(benchmark_path)

    name_col = next(
        (
            column
            for column in ["name", "product_name", "title", "제품명"]
            if column in df_danawa.columns
        ),
        None,
    )

    if name_col is None:
        raise ValueError(f"{part_type}: 제품명 컬럼 없음")

    if "Bench_Name" not in df_bench.columns or "Score" not in df_bench.columns:
        raise ValueError(f"{part_type}: benchmark CSV는 Bench_Name, Score 컬럼이 필요합니다.")

    bench_list = prepare_benchmark_list(df_bench, part_type)

    cpu_gpu_index = None
    ssd_model_freq = None
    ssd_series_freq = None
    ssd_candidate_index = None
    ram_estimator = None
    ram_candidate_index = None

    if part_type in {"CPU", "GPU"}:
        cpu_gpu_index = build_cpu_gpu_index(bench_list)
    elif part_type == "SSD":
        ssd_model_freq, ssd_series_freq = build_ssd_frequency_tables(bench_list)
        ssd_candidate_index = build_ssd_candidate_index(bench_list)
    elif part_type == "RAM":
        ram_estimator = build_ram_estimator(bench_list)
        ram_candidate_index = build_ram_candidate_index(bench_list)

    print(f"다나와 데이터: {len(df_danawa)}개")
    print(f"벤치마크 데이터: {len(bench_list)}개")

    if part_type == "RAM" and ram_estimator is not None:
        print(f"RAM 스펙 추정 보정 샘플: {ram_estimator['sample_count']}개")

    results = []
    debug_rows = []

    matched_count = 0
    unmatched_count = 0
    match_type_counter = Counter()

    for _, row in df_danawa.iterrows():
        danawa_name = str(row[name_col])
        danawa_info = extract_model_info(danawa_name, part_type, row=row)

        best_benchmark_name = None
        best_benchmark_score = None
        best_match_score = None
        match_type = "none"
        candidate_count = 0

        capacity_match_type = None
        matched_bench_capacity = None
        capacity_distance = None
        model_similarity = None
        model_pair = None

        # -------------------------------------------------
        # CPU / GPU
        # -------------------------------------------------
        if part_type in {"CPU", "GPU"}:
            key = danawa_info.get("model_key")
            candidates = cpu_gpu_index.get(key, []) if key else []

            valid = []
            for benchmark in candidates:
                score = calc_cpu_gpu_match_score(
                    danawa_info,
                    benchmark["info"],
                    part_type,
                )
                if score >= 100:
                    valid.append((score, benchmark))

            candidate_count = len(valid)

            if valid:
                valid.sort(
                    key=lambda item: (item[0], item[1]["score"]),
                    reverse=True,
                )
                best_match_score, best = valid[0]
                best_benchmark_name = best["name"]
                best_benchmark_score = best["score"]
                match_type = "model_key_exact"

        # -------------------------------------------------
        # SSD
        # -------------------------------------------------
        elif part_type == "SSD":
            result = find_best_ssd_benchmark(
                danawa_info,
                bench_list,
                ssd_model_freq,
                ssd_series_freq,
                ssd_candidate_index,
            )

            candidate_count = result["candidate_count"]
            match_type = result["match_type"]
            capacity_match_type = result["capacity_match_type"]
            matched_bench_capacity = result["matched_capacity"]
            capacity_distance = result["capacity_distance"]
            model_similarity = result["model_similarity"]
            model_pair = result["model_pair"]

            if result["benchmark"] is not None:
                best = result["benchmark"]
                best_benchmark_name = best["name"]
                best_benchmark_score = best["score"]
                best_match_score = result["match_score"]

        # -------------------------------------------------
        # RAM
        # -------------------------------------------------
        elif part_type == "RAM":
            result = find_best_ram_benchmark(
                danawa_info,
                bench_list,
                ram_estimator,
                ram_candidate_index,
            )

            candidate_count = result["candidate_count"]
            match_type = result["match_type"]
            best_match_score = result["match_score"]
            best_benchmark_score = result["bench_score"]

            if result["benchmark"] is not None:
                best_benchmark_name = result["benchmark"]["name"]
            elif match_type == "ram_spec_estimate":
                best_benchmark_name = "[ESTIMATED_FROM_BENCHMARK_CALIBRATION]"

        # -------------------------------------------------
        # 결과
        # -------------------------------------------------
        result_row = row.to_dict()

        # 최종 brand 컬럼은 CPU / GPU에만 저장한다.
        # Mainboard는 benchmark 매칭 대상이 아니므로 별도 후처리 함수에서 추가한다.
        if part_type in {"CPU", "GPU"}:
            result_row["brand"] = extract_product_brand(danawa_name, part_type)

        if part_type == "GPU":
            result_row["chipset_brand"] = extract_gpu_chipset_brand(danawa_name)

        if best_benchmark_score is not None:
            result_row["bench_score"] = int(round(best_benchmark_score))
            matched_count += 1
            match_type_counter[match_type] += 1
        else:
            result_row["bench_score"] = None
            unmatched_count += 1
            match_type_counter["none"] += 1

        results.append(result_row)

        debug_row = {
            "danawa_name": danawa_name,
            "danawa_model_key": danawa_info.get("model_key"),
            "matched_bench_name": best_benchmark_name,
            "bench_score": best_benchmark_score,
            "match_score": best_match_score,
            "match_type": match_type,
            "candidate_count": candidate_count,
        }

        if part_type == "GPU":
            debug_row["gpu_vram_gb"] = danawa_info.get("gpu_vram_gb")

        if part_type == "SSD":
            debug_row.update({
                "ssd_capacity": danawa_info.get("ssd_capacity"),
                "ssd_capacity_candidates": ",".join(
                    str(x) for x in sorted(danawa_info.get("ssd_capacities", set()))
                ),
                "ssd_model_tokens": ",".join(
                    sorted(danawa_info.get("ssd_model_tokens", set()))
                ),
                "ssd_series_tokens": ",".join(
                    sorted(danawa_info.get("ssd_series_tokens", set()))
                ),
                "ssd_variant_tokens": ",".join(
                    sorted(danawa_info.get("ssd_variant_tokens", set()))
                ),
                "ssd_revision_tokens": ",".join(
                    sorted(danawa_info.get("ssd_revision_tokens", set()))
                ),
                "ssd_interface": danawa_info.get("ssd_interface"),
                "capacity_match_type": capacity_match_type,
                "matched_bench_capacity": matched_bench_capacity,
                "capacity_distance": capacity_distance,
                "model_similarity": model_similarity,
                "model_pair": str(model_pair) if model_pair else None,
            })

        if part_type == "RAM":
            debug_row.update({
                "ram_ddr_type": danawa_info.get("ddr_type"),
                "ram_speed": danawa_info.get("speed"),
                "ram_cl": danawa_info.get("cl"),
                "ram_total_capacity_gb": danawa_info.get("capacity_gb"),
                "ram_module_count": danawa_info.get("module_count"),
                "ram_module_capacity_gb": danawa_info.get("module_capacity_gb"),
            })

        debug_rows.append(debug_row)

    df_result = pd.DataFrame(results)
    if "bench_score" in df_result.columns:
        df_result["bench_score"] = pd.to_numeric(
            df_result["bench_score"],
            errors="coerce",
        ).astype("Int64")

    output_dir = os.path.join(data_dir, "result")
    os.makedirs(output_dir, exist_ok=True)

    if part_type in {"CPU", "GPU"} and "brand" in df_result.columns:
        brand_missing = int(df_result["brand"].isna().sum())
        print(f"브랜드 추출 실패: {brand_missing}개")

    if part_type == "GPU" and "chipset_brand" in df_result.columns:
        chipset_missing = int(df_result["chipset_brand"].isna().sum())
        print(f"GPU 칩셋 제조사 추출 실패: {chipset_missing}개")
        print(
            "GPU 칩셋 분포: "
            f"{df_result['chipset_brand'].value_counts(dropna=False).to_dict()}"
        )

    save_path = os.path.join(output_dir, f"integrated_{part_type}.csv")
    df_result.to_csv(save_path, index=False, encoding="utf-8-sig")

    if SAVE_MATCH_DEBUG:
        debug_path = os.path.join(output_dir, f"match_debug_{part_type}.csv")
        df_debug = pd.DataFrame(debug_rows)
        df_debug.to_csv(debug_path, index=False, encoding="utf-8-sig")
        print(f"매칭 진단 CSV: {debug_path}")

        if part_type in {"CPU", "GPU"}:
            no_key_count = int(df_debug["danawa_model_key"].isna().sum())
            print(f"모델 키 추출 실패: {no_key_count}개")

        if part_type == "SSD":
            capacity_missing = int(df_debug["ssd_capacity"].isna().sum())
            model_missing = int(df_debug["ssd_model_tokens"].fillna("").eq("").sum())
            print(f"SSD 용량 추출 실패: {capacity_missing}개")
            print(f"SSD 모델 토큰 없음: {model_missing}개")

    total_count = len(df_result)
    match_rate = matched_count / total_count * 100 if total_count else 0.0

    print()
    print(f"{part_type} 통합 완료")
    print(f"전체 제품: {total_count}")
    print(f"점수 확보: {matched_count}")
    print(f"점수 미확보: {unmatched_count}")
    print(f"최종 점수 확보율: {match_rate:.1f}%")

    for match_type_name, count in match_type_counter.most_common():
        print(f"  - {match_type_name}: {count}개")

    print(f"저장 위치: {save_path}")




# =========================================================
# Mainboard 브랜드 후처리
# =========================================================
def enrich_mainboard_brand():
    """
    benchmark 매칭 대상이 아닌 Mainboard CSV에만 brand 컬럼을 추가한다.

    입력/출력:
      data/result/data_Mainboard.csv

    product_code / product_url 및 기존 호환성 스펙은 그대로 보존한다.
    """
    part_type = "Mainboard"

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
    data_dir = os.path.join(project_root, "data")
    path = os.path.join(data_dir, "result", "data_Mainboard.csv")

    if not os.path.exists(path):
        raise FileNotFoundError(f"Mainboard CSV 없음: {path}")

    df = pd.read_csv(path)

    name_col = next(
        (
            column
            for column in ["name", "product_name", "title", "제품명"]
            if column in df.columns
        ),
        None,
    )

    if name_col is None:
        raise ValueError("Mainboard: 제품명 컬럼 없음")

    df["brand"] = df[name_col].apply(
        lambda value: extract_product_brand(value, part_type)
    )

    # name, price, brand, ... 순서로 정리
    columns = list(df.columns)
    if "brand" in columns:
        columns.remove("brand")
        insert_at = columns.index("price") + 1 if "price" in columns else 1
        columns.insert(insert_at, "brand")
        df = df[columns]

    df.to_csv(path, index=False, encoding="utf-8-sig")

    missing = int(df["brand"].isna().sum())

    print()
    print("Mainboard 브랜드 추출 완료")
    print(f"전체 제품: {len(df)}")
    print(f"브랜드 추출 성공: {len(df) - missing}")
    print(f"브랜드 추출 실패: {missing}")
    print(f"저장 위치: {path}")

    return df

# =========================================================
# 단독 실행
# =========================================================

if __name__ == "__main__":
    for part in ["CPU", "GPU", "SSD", "RAM"]:
        try:
            match_data(part)
        except Exception:
            print()
            print(f"[실패] {part} 데이터 매칭")
            traceback.print_exc()

    for part in ["Mainboard", "Power", "Case", "Cooler"]:
        try:
            enrich_metadata_only(part)
        except Exception:
            print()
            print(f"[실패] {part} metadata 보강")
            traceback.print_exc()
