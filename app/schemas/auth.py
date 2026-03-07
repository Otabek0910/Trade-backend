from pydantic import BaseModel

class TelegramAuth(BaseModel):
    initData: str          # то, что приходит из Telegram WebApp
    telegram_id: int       # для теста

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"