from sqlalchemy import Column, Integer, Numeric, Text, Date, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.db.base import Base

class Expense(Base):
    __tablename__ = "expenses"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    amount = Column(Numeric(12, 2), nullable=False)
    category = Column(Text, nullable=False)  # аренда, зп, налоги и т.д.
    description = Column(Text, nullable=True)
    date = Column(Date, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())