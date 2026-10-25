import csv
import logging
import math
import statistics
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
INPUT_FILE = DATA_DIR / "compuzone_estimates.csv"
OUTPUT_FILE = DATA_DIR / "recommendation_budget_ratios.csv"

CATEGORIES = ["CPU", "GPU", "MAINBOARD", "RAM", "SSD", "POWER", "CASE", "COOLER"]
USAGES = ["OFFICE", "GAMING", "VIDEO_EDITING"]
BUDGET_STEP = 500_000
LOCAL_RADIUS = 250_000
MIN_LOCAL_SAMPLES = 8
NEAREST_SAMPLES = 12
OFFICE_USE_GLOBAL = True
REMOVE_DUPLICATE_CONFIGS = True
MEAN_WEIGHT = Decimal("0.60")
MEDIAN_WEIGHT = Decimal("0.40")

GPU_REDUCTION_RATE = Decimal("0.05")
EDITING_DISTRIBUTION = {
    "CPU": Decimal("0.45"),
    "RAM": Decimal("0.35"),
    "SSD": Decimal("0.20"),
}
PRECISION = Decimal("0.000001")
PRICE_TOLERANCE = 1
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ratio_analyzer")


def to_int(value):
    value = str(value or "").strip().replace(",", "")
    if not value:
        raise ValueError("숫자 값 없음")
    return int(Decimal(value))


def normalize_name(value):
    return " ".join(str(value or "").split()).lower()


def normalize_ratios(ratios):
    total = sum(ratios.values(), Decimal("0"))
    if total <= 0:
        raise ValueError("비율 총합이 0 이하입니다.")
    return {category: ratios[category] / total for category in CATEGORIES}


def round_ratios(ratios):
    ratios = normalize_ratios(ratios)
    rounded = {
        category: value.quantize(PRECISION, rounding=ROUND_HALF_UP)
        for category, value in ratios.items()
    }
    difference = Decimal("1.000000") - sum(rounded.values(), Decimal("0"))
    largest = max(CATEGORIES, key=lambda category: rounded[category])
    rounded[largest] += difference
    if any(value < 0 or value > 1 for value in rounded.values()):
        raise ValueError("비정상적인 부품 비율 발생")
    if sum(rounded.values(), Decimal("0")) != Decimal("1.000000"):
        raise ValueError("비율 합계가 1이 아닙니다")
    return rounded


def read_source():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"원본 견적 CSV 없음: {INPUT_FILE}")
    with INPUT_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = [row for row in csv.reader(f) if row and row[0].strip().lower() != "usage"]
    log.info("원본 견적 수: %d개", len(rows))
    return rows


def parse_estimate(row):
    if len(row) < 46:
        raise ValueError(f"컬럼 부족: {len(row)}개")
    usage = row[0].strip().upper()
    if usage not in ("OFFICE", "GAMING"):
        raise ValueError(f"지원하지 않는 용도: {usage}")
    product_id, product_name = row[1].strip(), row[2].strip()
    parts_total = to_int(row[3])
    if parts_total <= 0:
        raise ValueError("부품 합계가 0 이하")

    part_names, part_prices, part_quantities = {}, {}, {}
    for index, category in enumerate(CATEGORIES):
        start = 4 + index * 5
        name = row[start].strip()
        unit_price_text = row[start + 1].strip()
        quantity = to_int(row[start + 2])
        subtotal = to_int(row[start + 3])
        if quantity < 0 or subtotal < 0:
            raise ValueError(f"{category} 수량 또는 가격 음수")
        if subtotal == 0 and category not in ("GPU", "COOLER"):
            raise ValueError(f"{category} 부품 가격 0원")
        if unit_price_text:
            expected = to_int(unit_price_text) * quantity
            if abs(expected - subtotal) > PRICE_TOLERANCE:
                raise ValueError(f"{category} 단가·수량 불일치")
        part_names[category] = name
        part_prices[category] = subtotal
        part_quantities[category] = quantity
    if abs(sum(part_prices.values()) - parts_total) > PRICE_TOLERANCE:
        raise ValueError("부품 가격 합계 불일치")
    if usage == "GAMING" and part_prices["GPU"] <= 0:
        raise ValueError("게이밍 GPU 가격 없음")
    ratios = {category: Decimal(part_prices[category]) / Decimal(parts_total)
              for category in CATEGORIES}
    return {
        "usage": usage, "product_id": product_id, "product_name": product_name,
        "budget": parts_total, "part_names": part_names,
        "part_prices": part_prices, "part_quantities": part_quantities,
        "ratios": ratios,
    }


def make_config_key(estimate):
    return (estimate["usage"], tuple((normalize_name(estimate["part_names"][c]),
             estimate["part_prices"][c], estimate["part_quantities"][c])
             for c in CATEGORIES))


def clean_estimates(rows):
    cleaned, seen_ids, seen_configs = [], set(), set()
    rejected = Counter()
    for row_number, row in enumerate(rows, 1):
        try:
            estimate = parse_estimate(row)
        except Exception as exc:
            rejected["INVALID"] += 1
            log.warning("%d행 데이터 제외: %s", row_number, exc)
            continue
        identity = (estimate["usage"], estimate["product_id"])
        if identity in seen_ids:
            rejected["DUPLICATE_ID"] += 1
            continue
        seen_ids.add(identity)
        if REMOVE_DUPLICATE_CONFIGS:
            key = make_config_key(estimate)
            if key in seen_configs:
                rejected["DUPLICATE_CONFIG"] += 1
                continue
            seen_configs.add(key)
        cleaned.append(estimate)
    log.info("정제 완료: %d개 / 제외 내역: %s", len(cleaned), dict(rejected))
    return cleaned


def make_budget_points(estimates, usage):
    group = [item for item in estimates if item["usage"] == usage]
    if not group:
        return []
    prices = [item["budget"] for item in group]
    minimum, maximum = min(prices), max(prices)
    if usage == "OFFICE":
        lower = max(BUDGET_STEP, minimum // BUDGET_STEP * BUDGET_STEP)
        upper = math.ceil(maximum / BUDGET_STEP) * BUDGET_STEP
    else:
        lower = math.ceil(minimum / BUDGET_STEP) * BUDGET_STEP
        upper = maximum // BUDGET_STEP * BUDGET_STEP
    if upper < lower:
        return [max(BUDGET_STEP,
                    round(statistics.median(prices) / BUDGET_STEP) * BUDGET_STEP)]
    return list(range(lower, upper + BUDGET_STEP, BUDGET_STEP))


def select_samples(estimates, usage, budget):
    group = [item for item in estimates if item["usage"] == usage]
    if not group:
        return [], "NO_DATA", 0
    if usage == "OFFICE" and OFFICE_USE_GLOBAL:
        return group, "GLOBAL_OBSERVED", len(group)
    local = [item for item in group if abs(item["budget"] - budget) <= LOCAL_RADIUS]
    if len(local) >= MIN_LOCAL_SAMPLES:
        return local, "LOCAL_OBSERVED", len(local)
    nearest = sorted(group, key=lambda item: (abs(item["budget"] - budget), item["budget"]))
    return nearest[:NEAREST_SAMPLES], "NEAREST_ESTIMATED", len(local)


def calculate_ratios(samples, budget, usage):
    if not samples:
        raise ValueError("산정할 표본이 없습니다")
    weights = [(1.0 if usage == "OFFICE" else
                1.0 / (1.0 + (abs(item["budget"] - budget) / BUDGET_STEP) ** 2))
               for item in samples]
    total_weight = sum(weights)
    result = {}
    for category in CATEGORIES:
        values = [float(item["ratios"][category]) for item in samples]
        weighted_mean = sum(value * weight for value, weight in zip(values, weights)) / total_weight
        median = statistics.median(values)
        result[category] = (Decimal(str(weighted_mean)) * MEAN_WEIGHT
                            + Decimal(str(median)) * MEDIAN_WEIGHT)
    return normalize_ratios(result)


def derive_video_editing(gaming_ratios):
    """GPU 예산의 5%만 줄이고 CPU 45%, RAM 35%, SSD 20%로 재분배."""
    editing = dict(gaming_ratios)
    moved = editing["GPU"] * GPU_REDUCTION_RATE
    editing["GPU"] -= moved
    for category, weight in EDITING_DISTRIBUTION.items():
        editing[category] += moved * weight
    return normalize_ratios(editing)


def build_ratio_records(estimates):
    records, gaming_points = [], []
    for usage in ("OFFICE", "GAMING"):
        budgets = make_budget_points(estimates, usage)
        log.info("[%s] 기준 예산: %s", usage, budgets)
        for budget in budgets:
            samples, method, local_count = select_samples(estimates, usage, budget)
            if not samples:
                continue
            raw_ratios = calculate_ratios(samples, budget, usage)
            records.append({"usage": usage, "budget": budget,
                            "ratios": round_ratios(raw_ratios)})
            if usage == "GAMING":
                gaming_points.append((budget, raw_ratios, len(samples)))
            log.info("[%s] %s원 | 표본 %d개 | 근접표본 %d개 | %s",
                     usage, f"{budget:,}", len(samples), local_count, method)
    for budget, gaming_ratios, count in gaming_points:
        editing = derive_video_editing(gaming_ratios)
        records.append({"usage": "VIDEO_EDITING", "budget": budget,
                        "ratios": round_ratios(editing)})
    order = {usage: index for index, usage in enumerate(USAGES)}
    records.sort(key=lambda item: (order[item["usage"]], item["budget"]))
    return records


def save_ratio_csv(records):
    fields = ["usage", "budget"] + [f"{c.lower()}_ratio" for c in CATEGORIES]
    with OUTPUT_FILE.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = {"usage": record["usage"], "budget": record["budget"]}
            for category in CATEGORIES:
                row[f"{category.lower()}_ratio"] = f'{record["ratios"][category]:.6f}'
            writer.writerow(row)
    log.info("비율 CSV 저장: %s", OUTPUT_FILE)


def validate_records(records):
    if not records:
        raise ValueError("생성된 예산 비율이 없습니다")
    seen = set()
    for record in records:
        key = (record["usage"], record["budget"])
        if key in seen:
            raise ValueError(f"중복된 용도·예산: {key}")
        seen.add(key)
        ratios = record["ratios"]
        if set(ratios) != set(CATEGORIES):
            raise ValueError(f"부품 카테고리 누락: {key}")
        if sum(ratios.values(), Decimal("0")) != Decimal("1.000000"):
            raise ValueError(f"비율 합계 오류: {key}")
        if any(ratio < 0 or ratio > 1 for ratio in ratios.values()):
            raise ValueError(f"비율 범위 오류: {key}")
    gaming_budgets = {r["budget"] for r in records if r["usage"] == "GAMING"}
    editing_budgets = {r["budget"] for r in records if r["usage"] == "VIDEO_EDITING"}
    if gaming_budgets != editing_budgets:
        raise ValueError("게이밍과 영상편집 기준 예산 불일치")
    by_key = {(r["usage"], r["budget"]): r["ratios"] for r in records}
    for budget in gaming_budgets:
        gaming = by_key[("GAMING", budget)]
        editing = by_key[("VIDEO_EDITING", budget)]
        if abs(editing["GPU"] - gaming["GPU"] * Decimal("0.95")) > Decimal("0.000002"):
            raise ValueError(f"영상편집 GPU 5% 감소 규칙 불일치: {budget}")
        for category in ("MAINBOARD", "POWER", "CASE", "COOLER"):
            if editing[category] != gaming[category]:
                raise ValueError(f"영상편집 기타 부품 배분 변경: {budget}/{category}")
        if any(editing[c] < gaming[c] for c in ("CPU", "RAM", "SSD")):
            raise ValueError(f"영상편집 핵심 부품 예산 감소: {budget}")
    log.info("최종 비율 검증 완료: %d개", len(records))


def print_summary(records):
    log.info("=" * 75)
    for record in records:
        result = " | ".join(
            f'{category}={record["ratios"][category] * 100:.2f}%'
            for category in CATEGORIES
        )
        log.info("%s | %s원 | %s", record["usage"], f'{record["budget"]:,}', result)
    for usage in USAGES:
        log.info("%s: %d개 기준 예산", usage,
                 sum(record["usage"] == usage for record in records))
    log.info("=" * 75)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info("컴퓨존 견적 분석 시작 (영상편집 GPU 배정분 5%% 재분배)")
    rows = read_source()
    estimates = clean_estimates(rows)
    if not estimates:
        raise RuntimeError("유효 견적 데이터 없음")
    counts = Counter(item["usage"] for item in estimates)
    log.info("유효 표본: OFFICE=%d, GAMING=%d", counts["OFFICE"], counts["GAMING"])
    if not counts["OFFICE"] or not counts["GAMING"]:
        raise RuntimeError("사무용 또는 게이밍 표본이 없습니다")
    records = build_ratio_records(estimates)
    validate_records(records)
    save_ratio_csv(records)
    print_summary(records)
    log.info("비율 분석 파이프라인 정상 완료")


if __name__ == "__main__":
    main()
