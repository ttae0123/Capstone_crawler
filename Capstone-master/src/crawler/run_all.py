import sys
import os
import traceback

# 현재 파일 기준 경로 설정
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))

# import 경로 추가
sys.path.append(current_dir)
sys.path.append(os.path.join(project_root, "src", "logic"))

# 크롤러 import
from benchmark_crawler import dump_all_benchmarks
from danawa_crawler import crawl_danawa  # 함수명 확인 필요

# 데이터 처리 import
from data_processor import match_data  # 함수명 확인 필요


PARTS = ["CPU", "GPU", "SSD", "RAM"]


def run_benchmark():
    print("\n🔥 [1단계] 벤치마크 크롤링 시작")
    for part in PARTS:
        try:
            dump_all_benchmarks(part)
        except Exception as e:
            print(f"❌ benchmark {part} 실패")
            traceback.print_exc()


def run_danawa():
    print("\n🔥 [2단계] 다나와 크롤링 시작")
    for part in PARTS:
        try:
            crawl_danawa(part)  # 여기 함수명 맞게 수정 필요
        except Exception as e:
            print(f"❌ danawa {part} 실패")
            traceback.print_exc()


def run_processing():
    print("\n🔥 [3단계] 데이터 매칭 시작")
    for part in PARTS:
        try:
            match_data(part)
        except Exception as e:
            print(f"❌ processing {part} 실패")
            traceback.print_exc()


if __name__ == "__main__":
    print("🚀 전체 자동 파이프라인 시작")

    run_benchmark()
    run_danawa()
    run_processing()

    print("\n✅ 전체 작업 완료")