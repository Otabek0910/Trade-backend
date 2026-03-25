from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import cast, Date
from datetime import date
import httpx

from app.db.session import get_db
from app.models.exchange_rate import ExchangeRate
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/rates", tags=["Курсы валют"])

CBU_API = "https://cbu.uz/common/json/"


async def fetch_cbu_rate() -> float | None:
    """Тянет курс USD с ЦБУ Узбекистана (бесплатный открытый API)"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(CBU_API)
            data = resp.json()
            for item in data:
                if item.get("Ccy") == "USD":
                    return float(item["Rate"])
    except Exception as e:
        print(f"⚠️ Ошибка получения курса ЦБУ: {e}")
    return None


@router.get("/today")
async def get_today_rate(
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """
    Возвращает курс USD/UZS на сегодня.
    Если в БД уже есть — отдаёт из кэша.
    Если нет — тянет с ЦБУ, сохраняет и отдаёт.
    Если ЦБУ недоступен — отдаёт последний сохранённый.
    """
    today = date.today()

    # Проверяем кэш в БД
    existing = db.query(ExchangeRate).filter(
        cast(ExchangeRate.date, Date) == today
    ).first()

    if existing:
        return {
            "date": str(existing.date),
            "cbu_rate": float(existing.cbu_rate),
            "source": "cache",
        }

    # Тянем с ЦБУ
    rate = await fetch_cbu_rate()

    if not rate:
        # ЦБУ недоступен — берём последний сохранённый
        last = db.query(ExchangeRate).order_by(ExchangeRate.date.desc()).first()
        if last:
            return {
                "date": str(last.date),
                "cbu_rate": float(last.cbu_rate),
                "source": "last_cached",
            }
        raise HTTPException(status_code=503, detail="Курс ЦБУ недоступен и нет сохранённых данных")

    # Сохраняем в БД
    record = ExchangeRate(date=today, cbu_rate=rate)
    db.add(record)
    db.commit()

    return {
        "date": str(today),
        "cbu_rate": rate,
        "source": "cbu",
    }


@router.get("/history")
def get_rate_history(
    limit: int = 30,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """История курсов за последние N дней — для графика или справки"""
    rows = (
        db.query(ExchangeRate)
        .order_by(ExchangeRate.date.desc())
        .limit(limit)
        .all()
    )
    return [{"date": str(r.date), "cbu_rate": float(r.cbu_rate)} for r in rows]