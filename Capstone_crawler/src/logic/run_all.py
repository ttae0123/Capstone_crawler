import sys
import os
import traceback



current_dir = os.path.dirname(os.path.abspath(__file__))

sys.path.append(current_dir)

from crawler.benchmark_crawler import dump_all_benchmarks
from crawler.danawa_crawler import crawl_danawa
from logic.data_processor import match_data
from logic.create_DB import init_db 
from logic.csv_to_db import load_csv_to_db


PARTS = ["CPU", "GPU", "SSD", "RAM"]
parts_list = [
        {"name": "CPU", "code": "112747", "page": 18},
        {"name": "GPU", "code": "112753", "page": 20},
        {"name": "Mainboard", "code": "112751", "page": 20},
        {"name": "Power", "code": "112777", "page": 20},
        {"name": "RAM", "code": "112752", "page": 20},
        {"name": "SSD", "code": "112760", "page": 20},
        {"name": "Case", "code": "112775", "page": 20}
    ]



def run_benchmark():
    print("\n[1단계] 벤치마크 크롤링 시작")
    for part in PARTS:
        try:
            dump_all_benchmarks(part)
        except Exception as e:
            print(f"benchmark {part} 실패")
            traceback.print_exc()


def run_danawa():
    print("\n[2단계] 다나와 크롤링 시작")
    for part in parts_list:
        try:
            crawl_danawa(part["name"], part["code"], part["page"])
        except Exception as e:
            print(f"danawa {part} 실패")
            traceback.print_exc()


def run_processing():
    print("\n[3단계] 데이터 매칭 시작")
    for part in PARTS:
        try:
            match_data(part)
        except Exception as e:
            print(f"processing {part} 실패")
            traceback.print_exc()

def run_csvToDB():
    data_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "result")
    )
    for file in os.listdir(data_dir):
        if file.endswith(".csv"):
            file_path = os.path.join(data_dir, file)
            load_csv_to_db(file_path)

    print("\n전체 CSV → DB 적재 완료")


if __name__ == "__main__":

    print("전체 파이프라인 시작")
    init_db()
    run_benchmark()
    run_danawa()
    run_processing()
    run_csvToDB()

    print("\n전체 작업 완료")