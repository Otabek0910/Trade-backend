from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # === База данных ===
    DATABASE_URL: str = "postgresql://trade_db_sq3t_user:5FYK4R0cYbdWZ4sXcWkr2EXxPbl7UqM8@dpg-d6m0q3haae7s73f99po0-a/trade_db_sq3t"
    
    # === Telegram ===
    TELEGRAM_BOT_TOKEN: str = "8692228116:AAHqzuraz-QFld4ClYf52Es13oRGFy2EUXs"
    TELEGRAM_WEBAPP_SECRET: str = "YOUR_WEBAPP_SECRET_HERE"  # можно оставить пустым, генерируем позже
    
    # === Безопасность ===
    SECRET_KEY: str = "455f151de74ac4576be81a12dfebac1e7d257348ee2eb03b446142e74a0a877a"
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