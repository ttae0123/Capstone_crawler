import os
import pandas as pd
from sqlalchemy import text
from logic.connection import get_engine


def get_table_name(file_name):
    return (
        file_name
        .replace("data_", "")
        .replace("integrated_", "")
        .replace(".csv", "")
        .lower()
    )


def clear_table(engine, table_name):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE `{table_name}`"))
    print(f"{table_name} 기존 데이터 삭제 완료")


def load_csv_to_db(file_path):
    engine = get_engine()

    file_name = os.path.basename(file_path)

    try:
        # 1. 테이블명 결정
        table_name = get_table_name(file_name)
        print(f"\n[{table_name}] 처리 시작")

        # 2. CSV 읽기
        df = pd.read_csv(file_path)

        if df.empty:
            print(f"{file_name} 데이터 없음, 스킵")
            return

        # 3. 전처리
        # 의도적으로 NULL이 있는 행은 제거
        df = df.dropna()

        if df.empty:
            print(f"{file_name} 유효 데이터 없음, 스킵")
            return

        if "name" in df.columns:
            df = df.drop_duplicates(subset=["name", "price"])

        # 4. DB에 필요 없는 컬럼 제거
        drop_cols = ["id", "created_at"]
        df = df.drop(columns=[c for c in drop_cols if c in df.columns])

        # 5. 기존 테이블 데이터 삭제
        clear_table(engine, table_name)

        # 6. DB 저장
        df.to_sql(
            name=table_name,
            con=engine,
            if_exists="append",
            index=False
        )

        print(f"{table_name} 테이블 삽입 완료 ({len(df)} rows)")

    except Exception as e:
        print(f"{file_name} 처리 중 오류: {e}")


if __name__ == "__main__":

    data_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "result")
    )

    for file in os.listdir(data_dir):
        if file.endswith(".csv"):
            file_path = os.path.join(data_dir, file)
            load_csv_to_db(file_path)

    print("\n전체 CSV → DB 적재 완료")