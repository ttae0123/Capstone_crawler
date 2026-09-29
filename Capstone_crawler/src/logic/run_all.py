import sys
import os
import traceback


current_dir = os.path.dirname(
    os.path.abspath(__file__)
)

sys.path.append(current_dir)


from crawler.benchmark_crawler import dump_all_benchmarks
from crawler.danawa_crawler import crawl_danawa
from logic.data_processor import match_data, enrich_mainboard_brand
from logic.create_DB import init_db
from logic.csv_to_db import main as csv_to_db_main


BENCHMARK_PARTS = [
    "CPU",
    "GPU",
    "SSD",
    "RAM",
]


parts_list = [
    {
        "name": "CPU",
        "code": "112747",
        "page": 12,
    },
    {
        "name": "GPU",
        "code": "112753",
        "page": 12,
    },
    {
        "name": "Mainboard",
        "code": "112751",
        "page": 12,
    },
    {
        "name": "Power",
        "code": "112777",
        "page": 12,
    },
    {
        "name": "RAM",
        "code": "112752",
        "page": 15,
    },
    {
        "name": "SSD",
        "code": "112760",
        "page": 15,
    },
    {
        "name": "Case",
        "code": "112775",
        "page": 12,
    },
    {
        "name": "Cooler",
        "code": "11336857",
        "page": 12,
    },
]


def run_benchmark():
    print()
    print("[1단계] 벤치마크 크롤링 시작")

    for part in BENCHMARK_PARTS:
        try:
            dump_all_benchmarks(part)

        except Exception as e:
            print()
            print(
                f"[실패] benchmark {part}"
            )

            traceback.print_exc()

            raise RuntimeError(
                f"{part} benchmark 크롤링 실패"
            ) from e


def run_danawa():
    print()
    print("[2단계] 다나와 크롤링 시작")

    for part in parts_list:
        name = part["name"]
        code = part["code"]
        page = part["page"]

        try:
            success = crawl_danawa(
                name,
                code,
                page,
            )

            if success is not True:
                raise RuntimeError(
                    f"{name} 크롤링 결과가 "
                    f"정상 완료되지 않았습니다."
                )

        except Exception as e:
            print()
            print(
                f"[실패] Danawa {name}"
            )

            traceback.print_exc()

            raise RuntimeError(
                f"{name} 다나와 크롤링 실패"
            ) from e


def run_processing():
    print()
    print("[3단계] 데이터 매칭 및 브랜드 처리 시작")

    # -----------------------------------------------------
    # 1) 벤치마크 매칭
    # CPU / GPU / SSD / RAM
    #
    # 최종 brand 컬럼은 여기서 CPU / GPU에만 생성된다.
    # GPU는 chipset_brand도 함께 생성된다.
    # SSD / RAM은 벤치마크 매칭 내부에서만 제조사 정보를 사용하며
    # 최종 CSV에는 brand 컬럼을 저장하지 않는다.
    # -----------------------------------------------------
    for part in BENCHMARK_PARTS:
        try:
            match_data(part)

        except Exception as e:
            print()
            print(
                f"[실패] processing {part}"
            )

            traceback.print_exc()

            raise RuntimeError(
                f"{part} benchmark 매칭 실패"
            ) from e

    # -----------------------------------------------------
    # 2) Mainboard 브랜드 후처리
    # Mainboard는 benchmark 대상이 아니므로 다나와 결과 CSV에
    # brand 컬럼만 별도로 추가한다.
    # -----------------------------------------------------
    try:
        enrich_mainboard_brand()

    except Exception as e:
        print()
        print(
            "[실패] Mainboard 브랜드 처리"
        )

        traceback.print_exc()

        raise RuntimeError(
            "Mainboard 브랜드 처리 실패"
        ) from e


def run_csv_to_db():
    print()
    print("[4단계] CSV → DB 적재 시작")

    try:
        csv_to_db_main()

    except Exception as e:
        print()
        print(
            "[실패] CSV → DB 적재 실패"
        )

        traceback.print_exc()

        raise RuntimeError(
            "CSV → DB 적재 실패"
        ) from e


def main():
    print("=" * 60)
    print("전체 파이프라인 시작")
    print("=" * 60)

    init_db()

    run_benchmark()
    run_danawa()
    run_processing()
    run_csv_to_db()

    print()
    print("=" * 60)
    print("전체 작업 완료")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()

    except Exception as e:
        print()
        print("=" * 60)
        print("전체 파이프라인 실패")
        print("=" * 60)
        print(
            f"원인: {e}"
        )

        sys.exit(1)
