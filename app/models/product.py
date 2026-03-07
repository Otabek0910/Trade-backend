# app/models/product.py
from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base

class Product(Base):
    __tablename__ = "products"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=True)
    brand = Column(String, nullable=True)        # ← Марка: Mobil, Shell, Castrol
    unit = Column(String, nullable=True, default='шт')  # ← шт/л/кг/м/уп...
    unit_value = Column(Float, nullable=True)    # ← объём упаковки: 3 для канистры 3л
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    purchase_price = Column(Numeric(12, 2), nullable=False)
    selling_price = Column(Numeric(12, 2), nullable=False)
    min_stock = Column(Integer, default=5)
    current_stock = Column(Integer, default=0)
    photo_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    supplier = relationship("Supplier", back_populates="products")