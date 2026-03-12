from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.security import verify_token
from app.db.session import get_db

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    token = credentials.credentials
    telegram_id = verify_token(token)
    if telegram_id is None:
        raise HTTPException(status_code=401, detail="Неверный или просроченный токен")

    from app.models.user import User
    user = db.query(User).filter(User.telegram_id == telegram_id).first()

    # Пользователь удалён — немедленный выход
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден. Войдите заново.")

    # Флаг needs_reauth — срабатывает один раз после смены роли/статуса
    if getattr(user, 'needs_reauth', False):
        user.needs_reauth = False
        db.commit()
        raise HTTPException(status_code=401, detail="Роль изменена. Войдите заново.")

    return telegram_id