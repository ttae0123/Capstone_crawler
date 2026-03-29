from sqlalchemy import create_engine

def get_db_connection(part_type,df_result):

    user = "root"
    password = "1893"
    host = "127.0.0.1"
    port = "3306"
    db_name = "pc_db"

    engine = create_engine(f"mariadb+mariadbconnector://{user}:{password}@{host}:{port}/{db_name}")
    try:
        table_name = f"{part_type.lower()}"
        df_result.to_sql(name=table_name, con=engine, if_exists='replace', index=False)

        print(f"{table_name} 테이블이 MariaDB에 성공적으로 저장되었습니다.")

    except Exception as e:
        print(f"DB 저장 중 오류 발생: {e}")