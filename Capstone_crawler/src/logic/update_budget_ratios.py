import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from logic.create_DB import init_db
from logic.ratio_analyzer import main as analyze
from logic.csv_to_db import load_budget_ratio_csv


def main():
    analyze()
    init_db()
    ratio_file = Path(__file__).resolve().parents[2] / "data" / "recommendation_budget_ratios.csv"
    if not load_budget_ratio_csv(ratio_file):
        raise RuntimeError("예산 비율 DB 갱신 실패")
    print("예산 비율 DB 갱신 완료")


if __name__ == "__main__":
    main()
