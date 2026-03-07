from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.security import verify_token

security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    telegram_id = verify_token(token)
    if telegram_id is None:
        raise HTTPException(status_code=401, detail="Неверный или просроченный токен")
    return telegram_id