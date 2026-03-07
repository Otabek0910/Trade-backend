from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

# Синхронный движок — самый стабильный на Windows
engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20
)

SessionLocal = sessionmaker(
    bind=engine, 
    autocommit=False, 
    autoflush=False
)

def get_db():
    """Зависимость для FastAPI"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()