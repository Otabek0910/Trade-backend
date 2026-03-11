"""
app/core/telegram_media.py
Хранение фото через приватный Telegram-канал.
Фото загружается в канал → сохраняется file_id → отдаётся через прокси.
"""
import httpx
from fastapi import HTTPException
from app.core.config import settings

BOT_TOKEN = settings.TELEGRAM_BOT_TOKEN
CHANNEL_ID = settings.TELEGRAM_MEDIA_CHANNEL
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def upload_photo_to_telegram(file_bytes: bytes, filename: str) -> str:
    """
    Загружает фото в приватный канал.
    Возвращает file_id — строку для хранения в БД.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TG_API}/sendPhoto",
            data={"chat_id": CHANNEL_ID, "disable_notification": "true"},
            files={"photo": (filename, file_bytes, "image/jpeg")},
        )
        data = resp.json()
        if not data.get("ok"):
            raise HTTPException(status_code=500, detail=f"Telegram upload error: {data.get('description')}")

        # Берём наибольшее фото (последний элемент массива)
        photos = data["result"]["photo"]
        file_id = photos[-1]["file_id"]
        return file_id  # сохраняем в БД как photo_url = "tg:{file_id}"


async def get_photo_url(file_id: str) -> str:
    """
    Получает временный прямой URL файла через getFile.
    URL живёт ~1 час — используем только внутри прокси.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{TG_API}/getFile", params={"file_id": file_id})
        data = resp.json()
        if not data.get("ok"):
            raise HTTPException(status_code=404, detail="Фото не найдено")
        file_path = data["result"]["file_path"]
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


async def delete_photo_from_telegram(file_id: str) -> None:
    """
    Telegram не позволяет удалять файлы через API —
    просто игнорируем, старый file_id станет неактивным.
    """
    pass