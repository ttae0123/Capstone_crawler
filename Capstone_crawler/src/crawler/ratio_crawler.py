
import csv
import logging
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# 1. 기본 설정

BASE = "https://www.compuzone.co.kr"
LIST_URL = BASE + "/product/recommend_list.htm"

HEADLESS = True
TIMEOUT = 30000
DELAY = 1.2

MAX_PER_USAGE = 150
MAX_PAGES = 10

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DETAIL_FILE = DATA_DIR / "compuzone_estimates.csv"

# 실제 HTML에서 확인된 필터 ID
FILTERS = {
    "OFFICE": [
        "chk_3423_60230"
    ],
    "GAMING": [
        "chk_3423_60241",
        "chk_3423_60242",
        "chk_3423_60243"
    ],
}

FILTER_NAMES = {
    "chk_3423_60230": "사무용추천PC",
    "chk_3423_60241": "게이밍추천PC i3/R3",
    "chk_3423_60242": "게이밍추천PC i5/R5",
    "chk_3423_60243": "게이밍추천PC i7/R7"
}

PARTS = [
    "CPU", "GPU", "MAINBOARD", "RAM",
    "SSD", "POWER", "CASE", "COOLER"
]

REQUIRED = [
    "CPU", "MAINBOARD", "RAM",
    "SSD", "POWER", "CASE"
]

CATEGORY_MAP = {
    "CPU": "CPU",
    "그래픽카드": "GPU",
    "메인보드": "MAINBOARD",
    "메모리": "RAM",
    "RAM": "RAM",
    "SSD": "SSD",
    "파워": "POWER",
    "파워서플라이": "POWER",
    "케이스": "CASE",
    "쿨러": "COOLER",
    "CPU쿨러": "COOLER",
    "CPU 쿨러": "COOLER"
}

KEYWORDS = {
    "OFFICE": [
        "사무용", "업무용", "오피스용"
    ],
    "GAMING": [
        "게이밍", "게임용"
    ],
    "VIDEO_EDITING": [
        "영상편집", "영상 편집",
        "이미지/영상편집", "영상작업"
    ]
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("ratio_crawler")


# 2. 공통 함수

def clean(value):
    return re.sub(
        r"\s+", " ", str(value or "")
    ).strip()


def normalize(value):
    return re.sub(
        r"\s+", "", clean(value)
    ).lower()


def parse_money(value):
    text = clean(value)

    match = re.fullmatch(
        r"([\d,]+)\s*원?",
        text
    )

    if not match:
        return None

    return int(
        match.group(1).replace(",", "")
    )


def classify(text):
    text = normalize(text)

    matched = []

    for usage, keywords in KEYWORDS.items():
        if any(normalize(k) in text for k in keywords):
            matched.append(usage)

    return matched[0] if len(matched) == 1 else None


def get_product_id(url):
    parsed = urlparse(url)

    if not parsed.netloc.endswith("compuzone.co.kr"):
        return None

    params = parse_qs(parsed.query)

    return (
        params.get("ProductNo")
        or params.get("productNo")
        or [None]
    )[0]


def make_product_url(pid):
    return (
        f"{BASE}/product/product_detail.htm"
        f"?ProductNo={pid}"
    )


def page_signature(page):
    """
    현재 상품 목록의 ID 목록으로 페이지 변경 검사
    """
    soup = BeautifulSoup(
        page.content(), "html.parser"
    )

    container = soup.select_one("#recom_search_ul")

    if container is None:
        return ()

    ids = []

    for a in container.select("a[href]"):
        pid = get_product_id(
            urljoin(BASE, a.get("href", ""))
        )

        if pid and pid not in ids:
            ids.append(pid)

    return tuple(ids)


# 3. 실제 체크박스 ID로 용도 필터 적용

def select_filter(page, filter_id):
    """
    컴퓨존의 실제 체크박스에 click 이벤트 발생.

    필터는 display:none인 팝업 내부에 있으므로
    Playwright의 일반적인 visible click 대신
    브라우저 DOM의 click()을 사용한다.

    DOM click은 사이트에 등록된 onclick=add_opt()
    이벤트를 실행한다.
    """

    selector = f"#{filter_id}"

    locator = page.locator(selector)

    if locator.count() == 0:
        log.error(
            "체크박스 없음: %s",
            filter_id
        )

        return False

    try:
        result = locator.evaluate(
            """el => {
                if (el.checked) {
                    return true;
                }

                el.click();

                return el.checked;
            }"""
        )

        if not result:
            log.error(
                "체크박스 선택 실패: %s",
                filter_id
            )
            return False

        # add_opt()의 내부 상태 갱신 확인
        code = filter_id.replace("chk_", "")

        page.wait_for_function(
            """code => {
                const values = [
                    ...document.querySelectorAll(
                        'input[name="SChkBottomDet[]"]'
                    )
                ].map(el => el.value);

                return values.includes(code);
            }""",
            arg=code,
            timeout=5000
        )

        log.info(
            "필터 선택 성공: %s",
            FILTER_NAMES[filter_id]
        )

        return True

    except Exception as exc:
        log.error(
            "필터 적용 오류 %s: %s",
            filter_id, exc
        )

        return False


def wait_for_products(page):
    """
    AJAX 목록이 갱신될 때까지 대기.
    """
    try:
        page.wait_for_function(
            """() => {
                const list = document.querySelector(
                    '#recom_search_ul'
                );

                const loading = document.querySelector(
                    '#ajax_img'
                );

                const loadingVisible = (
                    loading &&
                    getComputedStyle(loading).display !== 'none'
                );

                return list &&
                       !loadingVisible &&
                       list.querySelectorAll('li').length > 0;
            }""",
            timeout=12000
        )

        page.wait_for_timeout(400)

        return True

    except Exception:
        return False


def apply_usage_filters(page, filter_ids):
    """
    페이지를 새로 열고 용도별 필터 적용.
    """
    page.goto(
        LIST_URL,
        wait_until="domcontentloaded",
        timeout=TIMEOUT
    )

    page.wait_for_timeout(1500)

    selected = []

    for filter_id in filter_ids:
        if select_filter(page, filter_id):
            selected.append(filter_id)

        # add_opt()가 AJAX 요청을 호출하므로
        # 이전 요청 갱신 시간을 확보한다.
        page.wait_for_timeout(700)

    if len(selected) != len(filter_ids):
        return False

    if not wait_for_products(page):
        log.warning("필터 결과 상품 목록 대기 실패")


        log.info(
            "VIDEO_EDITING 상태 | TotCount=%s | ListCount=%s | recom_array=%s",
            page.locator("#TotCount").input_value()
            if page.locator("#TotCount").count() else "없음",
            page.locator("#ListCount").input_value()
            if page.locator("#ListCount").count() else "없음",
            page.locator("#recom_array").input_value()
            if page.locator("#recom_array").count() else "없음"
        )

        return False
    # recom_array는 실제 선택된 필터 코드를 저장함
    try:
        selected_values = page.locator(
            "#recom_array"
        ).input_value()

        codes = [
            filter_id.replace("chk_", "")
            for filter_id in filter_ids
        ]

        # 선택한 코드가 모두 존재해야 정상 적용 인정
        if not all(
            code in selected_values.split("|")
            for code in codes
        ):
            log.warning(
                "필터 상태 불일치: %s",
                selected_values
            )
            return False

        log.info(
            "적용된 필터 값: %s",
            selected_values
        )

    except Exception as exc:
        log.warning(
            "필터 상태 검증 오류: %s", exc
        )
        return False

    return True


# 4. 필터링된 실제 상품 목록 추출

def extract_listing_products(html, usage):
    soup = BeautifulSoup(html, "html.parser")

    container = soup.select_one(
        "#recom_search_ul"
    )

    if container is None:
        return {}

    result = {}

    # 상품 목록 최상위 li만 읽는다.
    for li in container.find_all(
        "li", recursive=False
    ):
        link = li.select_one(
            'a[href*="ProductNo="]'
        )

        if not link:
            continue

        href = urljoin(
            BASE,
            link.get("href", "")
        )

        pid = get_product_id(href)

        if not pid:
            continue

        # 상품 카드의 텍스트
        title = ""

        for selector in [
            ".txt_box .name",
            ".txt_box .tit",
            ".txt_box .product_name",
            ".txt_box"
        ]:
            element = li.select_one(selector)

            if element:
                candidate = clean(
                    element.get_text(" ", strip=True)
                )

                if candidate:
                    title = candidate[:300]
                    break

        if not title:
            title = clean(
                link.get_text(" ", strip=True)
            )[:300]

        # 현재 필터와 제목이 명확하게 충돌하면 제외
        detected = classify(title)

        if detected and detected != usage:
            continue

        result[pid] = {
            "id": pid,
            "usage": usage,
            "title": title,
            "url": make_product_url(pid)
        }

    return result


# 5. 실제 페이지네이션 함수 사용

def go_to_page(page, page_number):
    """
    업로드된 HTML에서 확인한 실제 함수:
    recom_go(nums, startnum)

    한 페이지 28개
    """

    before = page_signature(page)

    start_num = (page_number - 1) * 28

    try:
        page.evaluate(
            """args => {
                recom_go(
                    args.page,
                    args.start
                );
            }""",
            {
                "page": page_number,
                "start": start_num
            }
        )

        page.wait_for_function(
            """args => {
                const current = document.querySelector(
                    '#PageNum'
                );

                const loading = document.querySelector(
                    '#ajax_img'
                );

                const visible = (
                    loading &&
                    getComputedStyle(loading).display !== 'none'
                );

                return (
                    current &&
                    current.value === String(args.page) &&
                    !visible
                );
            }""",
            arg={"page": page_number},
            timeout=12000
        )

        page.wait_for_timeout(600)

        after = page_signature(page)

        if not after or after == before:
            return False

        return True

    except Exception as exc:
        log.warning(
            "페이지 이동 실패: %d / %s",
            page_number, exc
        )
        return False


# 6. 용도별 상품 수집

def collect_usage(page, usage):
    log.info("=" * 60)
    log.info("[%s] 수집 시작", usage)
    log.info("=" * 60)

    filters = FILTERS[usage]

    if not apply_usage_filters(
        page, filters
    ):

        return {}, False

    log.info(
        "[%s] 필터 적용 성공",
        usage
    )

    products = {}
    visited = set()

    for page_no in range(1, MAX_PAGES + 1):
        current = extract_listing_products(
            page.content(),
            usage
        )

        signature = tuple(
            sorted(current.keys())
        )

        if not signature:
            log.warning(
                "[%s] %d페이지 상품 없음",
                usage, page_no
            )
            break

        if signature in visited:
            break

        visited.add(signature)

        before = len(products)
        products.update(current)

        log.info(
            "[%s] 목록 %d페이지 / 신규 %d개 / 누적 %d개",
            usage,
            page_no,
            len(products) - before,
            len(products)
        )

        if len(products) >= MAX_PER_USAGE:
            break

        total = page.locator(
            "#TotCount"
        ).get_attribute("value")

        if total and total.isdigit():
            max_pages = (
                int(total) + 27
            ) // 28

            if page_no >= max_pages:
                break

        if not go_to_page(
            page,
            page_no + 1
        ):
            break

        time.sleep(0.5)


    log.info(
        "[%s] 목록 수집 완료: %d개",
        usage, len(products)
    )

    return dict(
        list(products.items())[:MAX_PER_USAGE]
    ), True


# 7. 상세 페이지 가격표 탐색

def find_part_table(soup):
    for table in soup.select("table"):
        rows = table.select("tr")[:3]

        text = clean(
            " ".join(
                tr.get_text(" ", strip=True)
                for tr in rows
            )
        )

        if (
            "개당 가격" in text
            and "수량" in text
            and (
                "PC 주요부품" in text
                or "변동사항" in text
            )
        ):
            return table

    return None


def extract_name(cell):
    for a in cell.select("a[href]"):
        name = clean(
            a.get("title")
            or a.get_text(" ", strip=True)
        )

        if len(name) >= 7:
            return name[:250]

    clone = BeautifulSoup(
        str(cell),
        "html.parser"
    )

    for tag in clone.select(
        "script,style,select,option,"
        "button,input,ul,ol"
    ):
        tag.decompose()

    return clean(
        clone.get_text(" ", strip=True)
    )[:250]


def extract_quantity(cell):
    for inp in cell.select("input[value]"):
        value = clean(
            inp.get("value", "")
        )

        if value.isdigit():
            quantity = int(value)

            if 1 <= quantity <= 32:
                return quantity

    text = clean(
        cell.get_text(" ", strip=True)
    )

    match = re.fullmatch(
        r"(\d+)\s*(개|EA)?",
        text,
        re.I
    )

    if match:
        quantity = int(match.group(1))

        if 1 <= quantity <= 32:
            return quantity

    return None


# 8. 부품별 가격 추출

def parse_parts(table):
    result = {}

    for tr in table.select("tr"):
        cells = tr.find_all(
            ["td", "th"],
            recursive=False
        )

        if len(cells) < 4:
            continue

        category_name = clean(
            cells[0].get_text(" ", strip=True)
        )

        category = CATEGORY_MAP.get(
            category_name
        )

        if not category:
            continue

        price_idx = None
        price = None

        for index in range(1, len(cells)):
            value = parse_money(
                cells[index].get_text(
                    " ", strip=True
                )
            )

            if value is not None:
                price_idx = index
                price = value
                break

        if price_idx is None:
            continue

        if price_idx + 1 >= len(cells):
            continue

        quantity = extract_quantity(
            cells[price_idx + 1]
        )

        if quantity is None:
            continue

        name = max(
            [
                extract_name(c)
                for c in cells[1:price_idx]
            ],
            key=len,
            default=category
        )

        total = price * quantity

        if category in result:
            old = result[category]

            old["name"] += " | " + name
            old["quantity"] += quantity
            old["total"] += total
            old["unit_price"] = ""

        else:
            result[category] = {
                "name": name,
                "unit_price": price,
                "quantity": quantity,
                "total": total
            }

    return result


# 9. 부품 가격 검증

def validate_parts(parts, table_text, usage):
    for part in REQUIRED:
        if part not in parts:
            return f"MISSING_{part}"

        if parts[part]["total"] <= 0:
            return f"ZERO_{part}"

    if "GPU" not in parts:
        # 내장그래픽 PC는 별도 그래픽카드 가격 0원
        if (
            usage == "OFFICE"
            and re.search(
                r"내장\s*(그래픽|VGA)",
                table_text,
                re.I
            )
        ):
            parts["GPU"] = {
                "name": "내장그래픽",
                "unit_price": 0,
                "quantity": 0,
                "total": 0
            }
        else:
            return "MISSING_GPU"

    elif parts["GPU"]["total"] <= 0:
        return "ZERO_GPU"

    if "COOLER" not in parts:
        evidence = (
            parts["CPU"]["name"]
            + " "
            + table_text
        )

        if re.search(
            r"기본\s*쿨러|번들\s*쿨러|"
            r"쿨러\s*포함|쿨러포함",
            evidence,
            re.I
        ):
            parts["COOLER"] = {
                "name": "CPU 기본 쿨러",
                "unit_price": 0,
                "quantity": 0,
                "total": 0
            }
        else:
            return "MISSING_COOLER"

    elif parts["COOLER"]["total"] <= 0:
        return "ZERO_COOLER"

    return None


# 10. 상세 상품 크롤링

def fetch_detail(page, item):
    pid = item["id"]

    page.goto(
        item["url"],
        wait_until="domcontentloaded",
        timeout=TIMEOUT
    )

    page.wait_for_timeout(1100)

    html = page.content()

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    titles = [
        clean(tag.get_text(" ", strip=True))
        for tag in soup.select(
            "h1,.product_title,"
            ".prd_name,.product_name"
        )
    ]

    detected = {
        classify(title)
        for title in titles
        if classify(title)
    }

    if len(detected) > 1:
        return None, "USAGE_CONFLICT"

    if (
        detected
        and item["usage"] not in detected
    ):
        return None, "USAGE_CONFLICT"

    table = find_part_table(soup)

    if table is None:
        return None, "NO_TABLE"

    parts = parse_parts(table)

    reason = validate_parts(
        parts,
        table.get_text(" ", strip=True),
        item["usage"]
    )

    if reason:
        return None, reason

    total = sum(
        parts[part]["total"]
        for part in PARTS
    )

    if total <= 0:
        return None, "ZERO_TOTAL"

    record = {
        "usage": item["usage"],
        "product_id": pid,
        "product_name": item["title"],
        "parts_total": total,
        "product_url": item["url"],
        "collected_at": datetime.now().isoformat(
            timespec="seconds"
        )
    }

    for part in PARTS:
        info = parts[part]

        record[f"{part}_name"] = info["name"]
        record[f"{part}_unit_price"] = info["unit_price"]
        record[f"{part}_quantity"] = info["quantity"]
        record[f"{part}_price"] = info["total"]
        record[f"{part}_ratio"] = round(
            info["total"] / total * 100,
            4
        )

    return record, None


# 11. CSV 저장

def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def save_results(records):
    if not records:
        return

    fields = ["usage", "product_id", "product_name", "parts_total"]
    for part in PARTS:
        fields.extend([
            f"{part}_name", f"{part}_unit_price", f"{part}_quantity",
            f"{part}_price", f"{part}_ratio",
        ])
    fields.extend(["product_url", "collected_at"])
    write_csv(DETAIL_FILE, fields, records)


# 12. 메인 실행

def main():
    log.info("=" * 65)
    log.info("컴퓨존 용도별 부품 예산 비율 수집 시작")
    log.info("=" * 65)

    gathered = {}
    conflicts = set()

    records = []
    failures = []
    stats = Counter()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS
        )

        context = browser.new_context(
            locale="ko-KR",
            viewport={
                "width": 1440,
                "height": 1000
            }
        )

        page = context.new_page()
        page.set_default_timeout(TIMEOUT)

        try:
            # 1단계: 용도별 상품 목록 수집

            for usage in FILTERS:
                try:
                    products, success = collect_usage(
                        page,
                        usage
                    )

                    if not success:
                        log.error(
                            "[%s] 용도 필터 적용 실패",
                            usage
                        )
                        continue

                    for pid, item in products.items():
                        if pid in gathered:
                            if (
                                gathered[pid]["usage"]
                                != usage
                            ):
                                conflicts.add(pid)
                        else:
                            gathered[pid] = item

                except Exception as exc:
                    log.exception(
                        "[%s] 목록 수집 오류: %s",
                        usage, exc
                    )

                time.sleep(DELAY)

            # 다른 용도에도 등장한 상품 제외
            items = [
                item
                for pid, item in gathered.items()
                if pid not in conflicts
            ]

            log.info("=" * 65)
            log.info(
                "고유 상품: %d개",
                len(gathered)
            )
            log.info(
                "용도 충돌: %d개",
                len(conflicts)
            )
            log.info(
                "상세 수집 대상: %d개",
                len(items)
            )
            log.info("=" * 65)

            if not items:
                log.error(
                    "수집 대상 0개. 기존 CSV 보존."
                )
                return

            # 2단계: 각 상품의 실제 부품 가격 수집

            for index, item in enumerate(items, 1):
                try:
                    record, reason = fetch_detail(
                        page,
                        item
                    )

                except Exception as exc:
                    log.error(
                        "상품 %s 오류: %s",
                        item["id"], exc
                    )

                    record = None
                    reason = "ERROR"

                if record:
                    records.append(record)
                    stats["SUCCESS"] += 1

                    log.info(
                        "[%d/%d] 성공 | %s | %s | %s원",
                        index,
                        len(items),
                        item["usage"],
                        item["id"],
                        f'{record["parts_total"]:,}'
                    )

                else:
                    stats[reason] += 1

                    failures.append({
                        "usage": item["usage"],
                        "product_id": item["id"],
                        "reason": reason,
                        "product_url": item["url"]
                    })

                    log.warning(
                        "[%d/%d] 제외 | %s | %s | %s",
                        index,
                        len(items),
                        item["usage"],
                        item["id"],
                        reason
                    )

                # 수집 중에도 정상 데이터 저장

                time.sleep(DELAY)

        finally:
            browser.close()

    save_results(records)

    # 3단계: 결과 출력

    log.info("=" * 65)
    log.info(
        "전체 유효 견적: %d개",
        len(records)
    )

    for usage in FILTERS:
        count = sum(
            row["usage"] == usage
            for row in records
        )

        log.info(
            "%s: %d개",
            usage, count
        )

    log.info(
        "처리 통계: %s",
        dict(stats)
    )

    log.info(
        "상세 CSV: %s",
        DETAIL_FILE
    )


    log.info("=" * 65)


if __name__ == "__main__":
    main()
