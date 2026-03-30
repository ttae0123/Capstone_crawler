from sqlalchemy import create_engine

def get_engine(): #여기 내용 니 DB에 맞게 설정하면 됨
    user = "root"
    password = "1893"
    host = "127.0.0.1"
    port = "3306"
    db_name = "pc_db" #얘는 DB에서 데이터베이스 미리 생성해야됨

    engine = create_engine(f"mariadb+mariadbconnector://{user}:{password}@{host}:{port}/{db_name}")
    return engine