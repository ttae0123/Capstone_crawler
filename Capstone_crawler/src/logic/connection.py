from sqlalchemy import create_engine

def get_engine():
    user = "root"
    password = "1893"
    host = "127.0.0.1"
    port = "3306"
    db_name = "pc_db"

    engine = create_engine(f"mariadb+mariadbconnector://{user}:{password}@{host}:{port}/{db_name}")
    return engine