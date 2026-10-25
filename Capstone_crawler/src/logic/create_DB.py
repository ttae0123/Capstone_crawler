from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, BigInteger, Text, Float, String, Numeric, DateTime, UniqueConstraint, CheckConstraint, func
import sys
from pathlib import Path as _Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from logic.connection import get_engine

Base = declarative_base()


class Cpu(Base):
    __tablename__ = "cpu"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    brand = Column(Text, nullable=False)
    socket_type = Column(Text, nullable=False)
    memory_type = Column(Text, nullable=False)
    bench_score = Column(BigInteger, nullable=False)
    product_code = Column(BigInteger, nullable=False)
    product_url = Column(Text, nullable=False)


class Gpu(Base):
    __tablename__ = "gpu"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    brand = Column(Text, nullable=False)
    chipset_brand = Column(Text, nullable=False)
    recommended_power = Column(BigInteger, nullable=False)
    pcie_type = Column(Text, nullable=False)
    gpu_length = Column(BigInteger, nullable=False)
    bench_score = Column(BigInteger, nullable=False)
    product_code = Column(BigInteger, nullable=False)
    product_url = Column(Text, nullable=False)


class Mainboard(Base):
    __tablename__ = "mainboard"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    brand = Column(Text, nullable=False)
    socket_type = Column(Text, nullable=False)
    memory_type = Column(Text, nullable=False)
    pcie_type = Column(Text, nullable=False)
    size = Column(Text, nullable=False)
    memory_clock = Column(BigInteger, nullable=False)
    product_code = Column(BigInteger, nullable=False)
    product_url = Column(Text, nullable=False)


class Ram(Base):
    __tablename__ = "ram"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    memory_type = Column(Text, nullable=False)
    memory_clock = Column(BigInteger, nullable=False)
    capacity = Column(BigInteger, nullable=False)
    module_count = Column(BigInteger, nullable=False)
    bench_score = Column(Float, nullable=False)
    product_code = Column(BigInteger, nullable=False)
    product_url = Column(Text, nullable=False)


class SSD(Base):
    __tablename__ = "ssd"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    capacity = Column(BigInteger, nullable=False)
    bench_score = Column(BigInteger, nullable=False)
    product_code = Column(BigInteger, nullable=False)
    product_url = Column(Text, nullable=False)


class Power(Base):
    __tablename__ = "power"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    size = Column(Text, nullable=False)
    wattage = Column(BigInteger, nullable=False)
    product_code = Column(BigInteger, nullable=False)
    product_url = Column(Text, nullable=False)


class Case(Base):
    __tablename__ = "pc_case"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    size = Column(Text, nullable=False)
    gpu_length = Column(BigInteger, nullable=False)
    cooler_length = Column(BigInteger, nullable=False)
    product_code = Column(BigInteger, nullable=False)
    product_url = Column(Text, nullable=False)


class Cooler(Base):
    __tablename__ = "cooler"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    price = Column(BigInteger, nullable=False)
    socket_type = Column(Text, nullable=False)
    cooler_length = Column(BigInteger, nullable=False)
    product_code = Column(BigInteger, nullable=False)
    product_url = Column(Text, nullable=False)


class RecommendationBudgetRatio(Base):
    __tablename__ = "recommendation_budget_ratios"
    __table_args__ = (
        UniqueConstraint("usage", "budget", name="uk_budget_ratio_usage_budget"),
        CheckConstraint("budget > 0", name="chk_budget_ratio_budget"),
        CheckConstraint(
            "cpu_ratio + gpu_ratio + mainboard_ratio + ram_ratio + ssd_ratio + "
            "power_ratio + case_ratio + cooler_ratio BETWEEN 0.999999 AND 1.000001",
            name="chk_budget_ratio_sum",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    usage = Column(String(30), nullable=False)
    budget = Column(BigInteger, nullable=False)
    cpu_ratio = Column(Numeric(8, 6), nullable=False)
    gpu_ratio = Column(Numeric(8, 6), nullable=False)
    mainboard_ratio = Column(Numeric(8, 6), nullable=False)
    ram_ratio = Column(Numeric(8, 6), nullable=False)
    ssd_ratio = Column(Numeric(8, 6), nullable=False)
    power_ratio = Column(Numeric(8, 6), nullable=False)
    case_ratio = Column(Numeric(8, 6), nullable=False)
    cooler_ratio = Column(Numeric(8, 6), nullable=False)
    updated_at = Column(DateTime, nullable=False, server_default=func.current_timestamp(), onupdate=func.current_timestamp())


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("모든 테이블 생성 완료")


if __name__ == "__main__":
    init_db()
