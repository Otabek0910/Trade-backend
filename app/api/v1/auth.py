from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.user import User, UserRole, UserStatus
from app.core.security import create_access_token
from app.core.config import settings
import hashlib
import hmac
import json
from urllib.parse import parse_qsl

router = APIRouter(prefix="/auth", tags=["Авторизация"])

def validate_telegram_init_data(init_data: str) -> dict | None:
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_value = data.pop("hash", None)
        if not hash_value:
            return None
            
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(data.items())
        )
        secret_key = hmac.new(
            b"WebAppData",
            settings.TELEGRAM_BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, hash_value):
            return None

        user_json = data.get("user", "{}")
        return json.loads(user_json)
    except Exception as e:
        print(f"❌ Ошибка валидации initData: {e}")
        return None

@router.post("/login")
async def login(payload: dict, db: Session = Depends(get_db)):
    init_data_str = payload.get("init_data")
    if not init_data_str:
        raise HTTPException(status_code=400, detail="Нет initData")

    user_data = validate_telegram_init_data(init_data_str)
    if not user_data:
        raise HTTPException(status_code=403, detail="Неверная подпись Telegram")

    telegram_id = int(user_data.get("id"))
    first_name = user_data.get("first_name", "")
    last_name = user_data.get("last_name", "")
    username = user_data.get("username", None)
    full_name = f"{first_name} {last_name}".strip()

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        # Первый пользователь в системе → сразу owner_business + active
        is_first = db.query(User).count() == 0
        user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            role=UserRole.owner_business if is_first else UserRole.seller,
            status=UserStatus.active if is_first else UserStatus.pending,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    # Обновляем имя/username если изменились
    if user.username != username or user.full_name != full_name:
        user.username = username
        user.full_name = full_name
        db.commit()

    if user.status == UserStatus.pending:
        raise HTTPException(
            status_code=403,
            detail="⏳ Ваша заявка на рассмотрении. Ожидайте одобрения владельца."
        )
    if user.status == UserStatus.blocked:
        raise HTTPException(status_code=403, detail="🚫 Аккаунт заблокирован")

    access_token = create_access_token(data={"sub": str(telegram_id)})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "telegram_id": user.telegram_id,
            "full_name": user.full_name,
            "role": user.role.value,
            "status": user.status.value,
        }
    }