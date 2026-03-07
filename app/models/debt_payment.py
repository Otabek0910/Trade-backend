# app/models/debt_payment.py
# Создать этот файл в папке app/models/

from sqlalchemy import Column, Integer, Numeric, DateTime, ForeignKey, String
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.base import Base


class DebtPayment(Base):
    __tablename__ = "debt_payments"

    id          = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    amount      = Column(Numeric(12, 2), nullable=False)
    note        = Column(String, nullable=True)
    created_by  = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at  = Column(DateTime, default=datetime.now)

    customer = relationship("Customer", foreign_keys=[customer_id])
    creator  = relationship("User",     foreign_keys=[created_by])