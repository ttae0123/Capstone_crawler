from logic.connection import get_engine

def get_db_connection(part_type,df_result):

    engine = get_engine()

    try:
        table_name = f"{part_type.lower()}"

        df_result = df_result.dropna()

        if "name" in df_result.columns:
            df_result = df_result.drop_duplicates(subset=["name", "price"])

        df_result.to_sql(
            name=table_name,
            con=engine,
            if_exists='replace',
            index=False
        )

        print(f"{table_name} 테이블이 MariaDB에 성공적으로 저장되었습니다.")

    except Exception as e:
        print(f"DB 저장 중 오류 발생: {e}")