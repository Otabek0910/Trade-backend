from fastapi import APIRouter, Depends, HTTPException
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/protected", tags=["Тест защиты"])

@router.get("/me")
async def get_me(telegram_id: int = Depends(get_current_user)):
    return {
        "message": "✅ Токен работает!",
        "your_telegram_id": telegram_id,
        "status": "Вы успешно авторизованы через Telegram"
    }