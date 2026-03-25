from sqlalchemy import Column, Integer, Numeric, Date, DateTime
from sqlalchemy.sql import func
from app.db.base import Base

class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    date       = Column(Date, nullable=False, unique=True, index=True)
    cbu_rate   = Column(Numeric(12, 2), nullable=False)   # официальный курс ЦБУ
    created_at = Column(DateTime(timezone=True), server_default=func.now())