from sqlalchemy import Column, Integer, Text, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.sql import func
from app.db.base import Base

class AuditLog(Base):
    __tablename__ = "audit_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    action       = Column(Text, nullable=False)      # "create_sale", "create_receipt" и т.д.
    entity       = Column(Text, nullable=False)      # "sale", "receipt", "expense", "return"
    entity_id    = Column(Integer, nullable=True)
    old_values   = Column(JSON, nullable=True)
    new_values   = Column(JSON, nullable=True)
    is_reverted  = Column(Boolean, default=False, nullable=False)
    reverted_at  = Column(DateTime(timezone=True), nullable=True)
    reverted_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())