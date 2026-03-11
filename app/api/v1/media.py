"""
app/api/v1/media.py
Прокси для фото из Telegram — токен бота не покидает сервер.
Использование: <img src="/media/photo/{file_id}" />
"""
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.core.telegram_media import get_photo_url

router = APIRouter(prefix="/media", tags=["Медиа"])


@router.get("/photo/{file_id:path}")
async def proxy_photo(file_id: str):
    """Отдаёт фото из Telegram по file_id, скрывая Bot Token."""
    try:
        url = await get_photo_url(file_id)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise HTTPException(status_code=404, detail="Фото недоступно")
            content_type = resp.headers.get("content-type", "image/jpeg")
            return StreamingResponse(
                iter([resp.content]),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},  # кэш 24ч
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Фото не найдено")