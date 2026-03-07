from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # === База данных ===
    DATABASE_URL: str = "postgresql://postgres:123@127.0.0.1:5432/trade_mvp"
    
    # === Telegram ===
    TELEGRAM_BOT_TOKEN: str = "8614801447:AAG5IR9gW34bMDAvLE2YelUQmBIun-04pdw"
    TELEGRAM_WEBAPP_SECRET: str = "YOUR_WEBAPP_SECRET_HERE"  # можно оставить пустым, генерируем позже
    
    # === Безопасность ===
    SECRET_KEY: str = "super-secret-key-change-in-production-2026"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 часа
    
    # === Redis (для кэша и алертов) ===
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # === Режим разработки ===
    DEBUG: bool = True
    ENVIRONMENT: str = "development"  # production / development

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()