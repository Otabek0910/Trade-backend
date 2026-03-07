from sqlalchemy import Column, Integer, ForeignKey, Numeric
from sqlalchemy.orm import relationship
from app.db.base import Base

class SaleItem(Base):
    __tablename__ = "sale_items"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    selling_price = Column(Numeric(12, 2), nullable=False)
    purchase_price_at_sale = Column(Numeric(12, 2), nullable=False)  # для точной маржи
    
    sale = relationship("Sale", back_populates="items")
    product = relationship("Product")