from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, BigInteger, Text, Float

from logic.connection import get_engine


Base = declarative_base()


class Cpu(Base):
    __tablename__ = "cpu"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        Text,
        nullable=False
    )

    price = Column(
        BigInteger,
        nullable=False
    )

    brand = Column(
        Text,
        nullable=False
    )

    socket_type = Column(
        Text,
        nullable=False
    )

    memory_type = Column(
        Text,
        nullable=False
    )

    bench_score = Column(
        BigInteger,
        nullable=False
    )

    product_code = Column(
        BigInteger,
        nullable=False
    )

    product_url = Column(
        Text,
        nullable=False
    )


class Gpu(Base):
    __tablename__ = "gpu"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        Text,
        nullable=False
    )

    price = Column(
        BigInteger,
        nullable=False
    )

    brand = Column(
        Text,
        nullable=False
    )

    chipset_brand = Column(
        Text,
        nullable=False
    )

    recommended_power = Column(
        BigInteger,
        nullable=False
    )

    pcie_type = Column(
        Text,
        nullable=False
    )

    gpu_length = Column(
        BigInteger,
        nullable=False
    )

    bench_score = Column(
        BigInteger,
        nullable=False
    )

    product_code = Column(
        BigInteger,
        nullable=False
    )

    product_url = Column(
        Text,
        nullable=False
    )


class Mainboard(Base):
    __tablename__ = "mainboard"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        Text,
        nullable=False
    )

    price = Column(
        BigInteger,
        nullable=False
    )

    brand = Column(
        Text,
        nullable=False
    )

    socket_type = Column(
        Text,
        nullable=False
    )

    memory_type = Column(
        Text,
        nullable=False
    )

    pcie_type = Column(
        Text,
        nullable=False
    )

    size = Column(
        Text,
        nullable=False
    )

    memory_clock = Column(
        BigInteger,
        nullable=False
    )

    product_code = Column(
        BigInteger,
        nullable=False
    )

    product_url = Column(
        Text,
        nullable=False
    )


class Ram(Base):
    __tablename__ = "ram"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        Text,
        nullable=False
    )

    price = Column(
        BigInteger,
        nullable=False
    )

    memory_type = Column(
        Text,
        nullable=False
    )

    memory_clock = Column(
        BigInteger,
        nullable=False
    )

    capacity = Column(
        BigInteger,
        nullable=False
    )

    module_count = Column(
        BigInteger,
        nullable=False
    )

    bench_score = Column(
        Float,
        nullable=False
    )

    product_code = Column(
        BigInteger,
        nullable=False
    )

    product_url = Column(
        Text,
        nullable=False
    )


class SSD(Base):
    __tablename__ = "ssd"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        Text,
        nullable=False
    )

    price = Column(
        BigInteger,
        nullable=False
    )

    capacity = Column(
        BigInteger,
        nullable=False
    )

    bench_score = Column(
        BigInteger,
        nullable=False
    )

    product_code = Column(
        BigInteger,
        nullable=False
    )

    product_url = Column(
        Text,
        nullable=False
    )


class Power(Base):
    __tablename__ = "power"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        Text,
        nullable=False
    )

    price = Column(
        BigInteger,
        nullable=False
    )

    size = Column(
        Text,
        nullable=False
    )

    wattage = Column(
        BigInteger,
        nullable=False
    )

    product_code = Column(
        BigInteger,
        nullable=False
    )

    product_url = Column(
        Text,
        nullable=False
    )


class Case(Base):
    __tablename__ = "pc_case"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        Text,
        nullable=False
    )

    price = Column(
        BigInteger,
        nullable=False
    )

    size = Column(
        Text,
        nullable=False
    )

    gpu_length = Column(
        BigInteger,
        nullable=False
    )

    cooler_length = Column(
        BigInteger,
        nullable=False
    )

    product_code = Column(
        BigInteger,
        nullable=False
    )

    product_url = Column(
        Text,
        nullable=False
    )


class Cooler(Base):
    __tablename__ = "cooler"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )

    name = Column(
        Text,
        nullable=False
    )

    price = Column(
        BigInteger,
        nullable=False
    )

    socket_type = Column(
        Text,
        nullable=False
    )

    cooler_length = Column(
        BigInteger,
        nullable=False
    )

    product_code = Column(
        BigInteger,
        nullable=False
    )

    product_url = Column(
        Text,
        nullable=False
    )


def init_db():
    engine = get_engine()

    Base.metadata.create_all(engine)

    print("모든 테이블 생성 완료")


if __name__ == "__main__":
    init_db()
