from sqlalchemy import Column, Integer, String, DateTime, Float, Numeric
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base

class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    photo_url = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    total_debt = Column(Numeric(12, 2), default=0, nullable=False, server_default='0')
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    products = relationship("Product", back_populates="supplier")