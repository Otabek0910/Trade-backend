from sqlalchemy import Column, Integer, ForeignKey, Numeric, DateTime, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
from app.db.base import Base

class PaymentType(str, enum.Enum):
    cash = "cash"
    card = "card"
    transfer = "transfer"

class SaleStatus(str, enum.Enum):
    completed = "completed"
    cancelled = "cancelled"
    returned = "returned"

class Sale(Base):
    __tablename__ = "sales"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    total_amount = Column(Numeric(12, 2), nullable=False)
    payment_type = Column(Enum(PaymentType), nullable=False)
    discount_percent = Column(Numeric(5, 2), default=0)
    paid_amount = Column(Numeric(12, 2), nullable=False)  # если меньше total → долг
    status = Column(Enum(SaleStatus), default=SaleStatus.completed)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # ── Курсы валют на момент продажи ───────────────────────────────────────
    cbu_rate       = Column(Numeric(12, 2), nullable=True)   # официальный курс ЦБУ
    market_rate    = Column(Numeric(12, 2), nullable=True)   # рыночный курс (если указан)
    effective_rate = Column(Numeric(12, 2), nullable=True)   # фактический = market_rate ?? cbu_rate
    
    seller = relationship("User")
    customer = relationship("Customer")
    items = relationship("SaleItem", back_populates="sale")