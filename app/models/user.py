from sqlalchemy import Column, Integer, String, BigInteger, DateTime, Enum
from sqlalchemy.sql import func
from app.db.base import Base
import enum

class UserRole(str, enum.Enum):
    developer = "developer"
    owner_business = "owner_business"
    seller = "seller"
    storekeeper = "storekeeper"

class UserStatus(str, enum.Enum):
    pending = "pending"    # ожидает одобрения владельца
    active = "active"      # активен
    blocked = "blocked"    # заблокирован

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.seller)
    status = Column(Enum(UserStatus), nullable=False, default=UserStatus.pending)
    created_at = Column(DateTime(timezone=True), server_default=func.now())