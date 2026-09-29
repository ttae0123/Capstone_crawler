import os

import pandas as pd
from sqlalchemy import text

from logic.connection import get_engine


# =========================================================
# 테이블별 실제 DB 저장 컬럼
# =========================================================

TABLE_COLUMNS = {
    "cpu": [
        "name",
        "price",
        "brand",
        "socket_type",
        "memory_type",
        "bench_score",
        "product_code",
        "product_url",
    ],

    "gpu": [
        "name",
        "price",
        "brand",
        "chipset_brand",
        "recommended_power",
        "pcie_type",
        "gpu_length",
        "bench_score",
        "product_code",
        "product_url",
    ],

    "mainboard": [
        "name",
        "price",
        "brand",
        "socket_type",
        "memory_type",
        "pcie_type",
        "size",
        "memory_clock",
        "product_code",
        "product_url",
    ],

    "ram": [
        "name",
        "price",
        "memory_type",
        "memory_clock",
        "capacity",
        "module_count",
        "bench_score",
        "product_code",
        "product_url",
    ],

    "ssd": [
        "name",
        "price",
        "capacity",
        "bench_score",
        "product_code",
        "product_url",
    ],

    "power": [
        "name",
        "price",
        "size",
        "wattage",
        "product_code",
        "product_url",
    ],

    "pc_case": [
        "name",
        "price",
        "size",
        "gpu_length",
        "cooler_length",
        "product_code",
        "product_url",
    ],

    "cooler": [
        "name",
        "price",
        "socket_type",
        "cooler_length",
        "product_code",
        "product_url",
    ],
}


# =========================================================
# 정수 컬럼
# =========================================================

INTEGER_COLUMNS = [
    "price",
    "product_code",
    "recommended_power",
    "gpu_length",
    "memory_clock",
    "capacity",
    "module_count",
    "cooler_length",
    "wattage",
]


# =========================================================
# benchmark 타입
# =========================================================

INTEGER_BENCHMARK_TABLES = {
    "cpu",
    "gpu",
    "ssd",
}


FLOAT_BENCHMARK_TABLES = {
    "ram",
}


# =========================================================
# 파일명 → DB 테이블명
# =========================================================

def get_table_name(file_name):
    table_name = (
        file_name
        .replace("data_", "")
        .replace("integrated_", "")
        .replace(".csv", "")
        .lower()
    )

    if table_name == "case":
        return "pc_case"

    return table_name


# =========================================================
# 테이블 초기화
# =========================================================

def clear_table(engine, table_name):
    with engine.begin() as conn:
        conn.execute(
            text(
                f"TRUNCATE TABLE `{table_name}`"
            )
        )

    print(
        f"{table_name} 기존 데이터 삭제 완료"
    )


# =========================================================
# 컬럼 검증
# =========================================================

def validate_columns(
    df,
    table_name,
    file_name
):
    required_columns = TABLE_COLUMNS.get(
        table_name
    )

    if required_columns is None:
        print(
            f"{file_name}: 지원하지 않는 테이블"
        )
        return False

    missing_columns = [
        column
        for column in required_columns
        if column not in df.columns
    ]

    if missing_columns:
        print(
            f"{file_name} 필수 컬럼 누락: "
            f"{missing_columns}"
        )
        return False

    return True


# =========================================================
# 숫자 타입 변환
# =========================================================

def convert_numeric_columns(
    df,
    table_name
):
    # -----------------------------------------------------
    # 일반 정수 컬럼
    # -----------------------------------------------------

    for column in INTEGER_COLUMNS:

        if column not in df.columns:
            continue

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

        # 소수가 들어오면 강제로 정수 변환하지 않고 NULL 처리
        invalid_decimal_mask = (
            df[column].notna()
            & (df[column] % 1 != 0)
        )

        if invalid_decimal_mask.any():

            invalid_count = int(
                invalid_decimal_mask.sum()
            )

            print()
            print(
                f"[{column} 정수 검증]"
            )
            print(
                f"정수가 아닌 값: "
                f"{invalid_count}개"
            )

            print(
                "샘플:",
                df.loc[
                    invalid_decimal_mask,
                    column
                ]
                .head(10)
                .tolist()
            )

            df.loc[
                invalid_decimal_mask,
                column
            ] = pd.NA

        df[column] = (
            df[column]
            .astype("Int64")
        )

    # -----------------------------------------------------
    # benchmark
    # -----------------------------------------------------

    if "bench_score" in df.columns:

        df["bench_score"] = pd.to_numeric(
            df["bench_score"],
            errors="coerce"
        )

        # RAM benchmark
        if table_name in FLOAT_BENCHMARK_TABLES:

            df["bench_score"] = (
                df["bench_score"]
                .astype("Float64")
            )

        # CPU / GPU / SSD benchmark
        elif table_name in INTEGER_BENCHMARK_TABLES:

            invalid_decimal_mask = (
                df["bench_score"].notna()
                & (
                    df["bench_score"] % 1 != 0
                )
            )

            if invalid_decimal_mask.any():

                print()
                print(
                    "[benchmark 정수 검증]"
                )

                print(
                    f"{table_name}: "
                    "소수 benchmark 발견"
                )

                print(
                    "샘플:",
                    df.loc[
                        invalid_decimal_mask,
                        "bench_score"
                    ]
                    .head(10)
                    .tolist()
                )

                df.loc[
                    invalid_decimal_mask,
                    "bench_score"
                ] = pd.NA

            df["bench_score"] = (
                df["bench_score"]
                .astype("Int64")
            )

    return df


# =========================================================
# NULL 행 제거
# =========================================================

def remove_null_rows(df):
    before_count = len(df)

    df = df.dropna().copy()

    after_count = len(df)

    removed_count = (
        before_count
        - after_count
    )

    print()
    print("[NULL 데이터 필터]")

    print(
        f"필터 전 개수: "
        f"{before_count}"
    )

    print(
        f"필터 후 개수: "
        f"{after_count}"
    )

    print(
        f"제거된 개수: "
        f"{removed_count}"
    )

    return df


# =========================================================
# 숫자 값 유효성 검사
# =========================================================

def remove_invalid_numeric_rows(
    df,
    table_name
):
    before_count = len(df)

    valid_mask = pd.Series(
        True,
        index=df.index
    )

    # -----------------------------------------------------
    # 공통 가격
    # -----------------------------------------------------

    if "price" in df.columns:
        valid_mask &= (
            df["price"] > 0
        )

    # -----------------------------------------------------
    # benchmark
    # -----------------------------------------------------

    if "bench_score" in df.columns:
        valid_mask &= (
            df["bench_score"] > 0
        )

    # -----------------------------------------------------
    # RAM
    # -----------------------------------------------------

    if table_name == "ram":

        valid_mask &= (
            df["memory_clock"] > 0
        )

        valid_mask &= (
            df["capacity"] > 0
        )

        valid_mask &= (
                df["module_count"] > 0
        )

    # -----------------------------------------------------
    # SSD
    # -----------------------------------------------------

    elif table_name == "ssd":

        valid_mask &= (
            df["capacity"] > 0
        )

    # -----------------------------------------------------
    # GPU
    # -----------------------------------------------------

    elif table_name == "gpu":

        valid_mask &= (
            df["recommended_power"] > 0
        )

        valid_mask &= (
            df["gpu_length"] > 0
        )

    # -----------------------------------------------------
    # Mainboard
    # -----------------------------------------------------

    elif table_name == "mainboard":

        valid_mask &= (
            df["memory_clock"] > 0
        )

    # -----------------------------------------------------
    # Power
    # -----------------------------------------------------

    elif table_name == "power":

        valid_mask &= (
            df["wattage"] > 0
        )

    # -----------------------------------------------------
    # Case
    # -----------------------------------------------------

    elif table_name == "pc_case":

        valid_mask &= (
            df["gpu_length"] > 0
        )

        valid_mask &= (
            df["cooler_length"] > 0
        )

    # -----------------------------------------------------
    # Cooler
    # -----------------------------------------------------

    elif table_name == "cooler":

        valid_mask &= (
            df["cooler_length"] > 0
        )

    df = df[
        valid_mask
    ].copy()

    after_count = len(df)

    removed_count = (
        before_count
        - after_count
    )

    print()
    print("[숫자 유효성 필터]")

    print(
        f"필터 전 개수: "
        f"{before_count}"
    )

    print(
        f"필터 후 개수: "
        f"{after_count}"
    )

    print(
        f"제거된 개수: "
        f"{removed_count}"
    )

    return df


# =========================================================
# 중복 제거
# =========================================================

def remove_duplicates(df):
    before_count = len(df)

    df = df.drop_duplicates(
        subset=[
            "name",
            "price"
        ],
        keep="first"
    ).copy()

    after_count = len(df)

    removed_count = (
        before_count
        - after_count
    )

    if removed_count > 0:

        print()
        print("[중복 데이터 필터]")

        print(
            f"제거된 개수: "
            f"{removed_count}"
        )

    return df


# =========================================================
# 최종 데이터 검증 출력
# =========================================================

def print_validation_summary(
    df,
    table_name
):
    print()
    print("[최종 데이터 검증]")

    print(
        f"행 수: "
        f"{len(df)}"
    )

    print(
        f"NULL 개수: "
        f"{int(df.isna().sum().sum())}"
    )

    if "price" in df.columns:

        print(
            f"가격 범위: "
            f"{int(df['price'].min()):,}"
            f" ~ "
            f"{int(df['price'].max()):,}"
        )

    if table_name == "ram":

        print(
            f"RAM 용량 범위: "
            f"{int(df['capacity'].min())}"
            f" ~ "
            f"{int(df['capacity'].max())}"
            f" GB"
        )

        print(
            f"RAM 클럭 범위: "
            f"{int(df['memory_clock'].min())}"
            f" ~ "
            f"{int(df['memory_clock'].max())}"
            f" MHz"
        )

        print(
            f"RAM benchmark 범위: "
            f"{df['bench_score'].min()}"
            f" ~ "
            f"{df['bench_score'].max()}"
        )

        print(
            f"RAM 모듈 개수 범위: "
            f"{int(df['module_count'].min())}"
            f" ~ "
            f"{int(df['module_count'].max())}"
        )

        print(
            "RAM 모듈 개수 분포: "
            f"{df['module_count'].value_counts().sort_index().to_dict()}"
        )

    elif table_name == "ssd":

        print(
            f"SSD 용량 범위: "
            f"{int(df['capacity'].min())}"
            f" ~ "
            f"{int(df['capacity'].max())}"
            f" GB"
        )


# =========================================================
# CSV 하나 DB 적재
# =========================================================

def load_csv_to_db(file_path):
    engine = get_engine()

    file_name = os.path.basename(
        file_path
    )

    table_name = get_table_name(
        file_name
    )

    try:
        print()
        print("=" * 60)

        print(
            f"[{table_name}] 처리 시작"
        )

        print("=" * 60)

        if table_name not in TABLE_COLUMNS:

            print(
                f"{file_name}: "
                "지원하지 않는 CSV 파일, 스킵"
            )

            return True

        # -------------------------------------------------
        # CSV 읽기
        # -------------------------------------------------

        df = pd.read_csv(
            file_path
        )

        print(
            f"CSV 원본 데이터: "
            f"{len(df)} rows"
        )

        if df.empty:

            print(
                f"{file_name}: 데이터 없음"
            )

            return False

        # -------------------------------------------------
        # 필수 컬럼 검사
        # -------------------------------------------------

        if not validate_columns(
            df,
            table_name,
            file_name
        ):
            return False

        # -------------------------------------------------
        # DB 저장 컬럼만 선택
        # -------------------------------------------------

        df = df[
            TABLE_COLUMNS[
                table_name
            ]
        ].copy()

        # -------------------------------------------------
        # 숫자 타입 변환
        # -------------------------------------------------

        df = convert_numeric_columns(
            df,
            table_name
        )

        # -------------------------------------------------
        # NULL 제거
        # -------------------------------------------------

        df = remove_null_rows(
            df
        )

        if df.empty:

            print(
                f"{file_name}: "
                "NULL 제거 후 유효 데이터 없음"
            )

            return False

        # -------------------------------------------------
        # 비정상 숫자 제거
        # -------------------------------------------------

        df = remove_invalid_numeric_rows(
            df,
            table_name
        )

        if df.empty:

            print(
                f"{file_name}: "
                "숫자 필터 후 유효 데이터 없음"
            )

            return False

        # -------------------------------------------------
        # 중복 제거
        # -------------------------------------------------

        df = remove_duplicates(
            df
        )

        if df.empty:

            print(
                f"{file_name}: "
                "최종 유효 데이터 없음"
            )

            return False

        # -------------------------------------------------
        # 최종 검증 출력
        # -------------------------------------------------

        print_validation_summary(
            df,
            table_name
        )

        print()

        print(
            f"최종 DB 적재 대상: "
            f"{len(df)} rows"
        )

        # -------------------------------------------------
        # 검증 완료 후 기존 테이블 비우기
        # -------------------------------------------------

        clear_table(
            engine,
            table_name
        )

        # -------------------------------------------------
        # DB INSERT
        # -------------------------------------------------

        df.to_sql(
            name=table_name,
            con=engine,
            if_exists="append",
            index=False
        )

        print(
            f"{table_name} 테이블 삽입 완료 "
            f"({len(df)} rows)"
        )

        return True

    except Exception as e:

        print()
        print(
            f"{file_name} 처리 중 오류: "
            f"{e}"
        )

        return False


# =========================================================
# MAIN
# =========================================================

def main():
    data_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "data",
            "result"
        )
    )

    if not os.path.exists(
        data_dir
    ):

        raise RuntimeError(
            "데이터 폴더를 찾을 수 없습니다: "
            f"{data_dir}"
        )

    csv_files = sorted(
        [
            file
            for file in os.listdir(
                data_dir
            )
            if file
            .lower()
            .endswith(".csv")
        ]
    )

    if not csv_files:

        raise RuntimeError(
            "적재할 CSV 파일이 없습니다."
        )

    print(
        f"CSV 파일 "
        f"{len(csv_files)}개 발견"
    )

    success_files = []
    failed_files = []

    for file in csv_files:

        file_path = os.path.join(
            data_dir,
            file
        )

        success = load_csv_to_db(
            file_path
        )

        if success:

            success_files.append(
                file
            )

        else:

            failed_files.append(
                file
            )

    print()
    print("=" * 60)
    print("[CSV → DB 적재 결과]")
    print("=" * 60)

    print(
        f"성공: "
        f"{len(success_files)}개"
    )

    print(
        f"실패: "
        f"{len(failed_files)}개"
    )

    if failed_files:

        print()
        print("[실패 파일]")

        for file in failed_files:

            print(
                f"- {file}"
            )

        raise RuntimeError(
            "CSV → DB 적재 중 "
            f"{len(failed_files)}개 파일 실패"
        )

    print()
    print("=" * 60)
    print("전체 CSV → DB 적재 완료")
    print("=" * 60)


if __name__ == "__main__":
    main()