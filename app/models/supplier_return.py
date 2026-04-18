from sqlalchemy import Column, Integer, ForeignKey, Numeric, DateTime, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base


class SupplierReturn(Base):
    __tablename__ = "supplier_returns"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    supplier_id    = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    product_id     = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity       = Column(Integer, nullable=False)
    purchase_price = Column(Numeric(12, 2), nullable=False)   # цена за единицу при возврате
    return_amount  = Column(Numeric(12, 2), nullable=False)   # quantity × purchase_price
    debt_reduced   = Column(Numeric(12, 2), nullable=False, server_default='0')  # на сколько уменьшили наш долг
    credit_added   = Column(Numeric(12, 2), nullable=False, server_default='0')  # поставщик стал должен нам
    reason         = Column(Text, nullable=True)
    created_by     = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    supplier = relationship("Supplier")
    product  = relationship("Product")
    creator  = relationship("User", foreign_keys=[created_by])
