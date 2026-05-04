from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, BigInteger, Text
from logic.connection import get_engine

Base = declarative_base()


class Cpu(Base):
    __tablename__ = "cpu"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    socket_type = Column(Text)
    memory_type = Column(Text)
    bench_score = Column(BigInteger, nullable=True)


class Gpu(Base):
    __tablename__ = "gpu"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    recommended_power = Column(BigInteger)
    pcie_type = Column(Text)
    gpu_length = Column(BigInteger)
    bench_score = Column(BigInteger, nullable=True)


class Mainboard(Base):
    __tablename__ = "mainboard"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    socket_type = Column(Text)
    memory_type = Column(Text)
    pcie_type = Column(Text)
    size = Column(Text)
    memory_clock = Column(BigInteger)


class Ram(Base):
    __tablename__ = "ram"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    memory_type = Column(Text)
    memory_clock = Column(BigInteger)
    bench_score = Column(BigInteger, nullable=True)


class Case(Base):
    __tablename__ = "case"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    size = Column(Text)
    gpu_length = Column(BigInteger)
    cooler_length = Column(BigInteger)


class SSD(Base):
    __tablename__ = "ssd"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    bench_score = Column(BigInteger, nullable=True)


class Power(Base):
    __tablename__ = "power"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    size = Column(Text)
    wattage = Column(BigInteger)


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("모든 테이블 생성 완료")


if __name__ == "__main__":
    init_db()