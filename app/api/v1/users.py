from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.db.session import get_db
from app.models.user import User, UserRole, UserStatus
from app.models.sale import Sale
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/users", tags=["Пользователи"])


class UserUpdate(BaseModel):
    role: Optional[UserRole] = None
    status: Optional[UserStatus] = None
    full_name: Optional[str] = None


def user_to_dict(u: User) -> dict:
    return {
        "id": u.id,
        "telegram_id": u.telegram_id,
        "username": u.username,
        "full_name": u.full_name,
        "role": u.role.value,
        "status": u.status.value,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def require_owner(telegram_id: int, db: Session):
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user or user.role not in (UserRole.owner_business, UserRole.developer):
        raise HTTPException(status_code=403, detail="Доступ только для владельца")
    return user


@router.get("")
def get_users(
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    require_owner(telegram_id, db)
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [user_to_dict(u) for u in users]


@router.post("/{user_id}/approve")
def approve_user(
    user_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Одобрить заявку пользователя → status: active"""
    require_owner(telegram_id, db)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target.status != UserStatus.pending:
        raise HTTPException(status_code=400, detail="Пользователь не в статусе pending")
    target.status = UserStatus.active
    db.commit()
    db.refresh(target)
    return user_to_dict(target)


@router.post("/{user_id}/reject")
def reject_user(
    user_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Отклонить заявку → удалить пользователя из БД"""
    require_owner(telegram_id, db)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target.status != UserStatus.pending:
        raise HTTPException(status_code=400, detail="Пользователь не в статусе pending")
    db.delete(target)
    db.commit()
    return {"deleted": True}


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """
    Удалить сотрудника.
    - Если есть продажи в истории → 400 (нельзя, данные нужны для отчётов)
    - Если продаж не было → удалить безвозвратно
    """
    current = require_owner(telegram_id, db)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target.telegram_id == telegram_id:
        raise HTTPException(status_code=400, detail="Нельзя удалить себя")
    if target.role == UserRole.developer and current.role != UserRole.developer:
        raise HTTPException(status_code=403, detail="Нельзя удалить разработчика")
    if target.role == UserRole.owner_business and current.role != UserRole.developer:
        raise HTTPException(status_code=403, detail="Нельзя удалить другого владельца бизнеса")

    has_sales = db.query(Sale).filter(Sale.seller_id == target.id).first() is not None
    if has_sales:
        raise HTTPException(
            status_code=400,
            detail="У сотрудника есть история продаж. Заблокируйте вместо удаления."
        )

    db.delete(target)
    db.commit()
    return {"deleted": True}


@router.patch("/{user_id}")
def update_user(
    user_id: int,
    data: UserUpdate,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    current = require_owner(telegram_id, db)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if target.telegram_id == telegram_id:
        raise HTTPException(status_code=400, detail="Нельзя изменить свою учётную запись")
    if target.role == UserRole.developer and current.role != UserRole.developer:
        raise HTTPException(status_code=403, detail="Нельзя изменить разработчика")
    if target.role == UserRole.owner_business and current.role != UserRole.developer:
        raise HTTPException(status_code=403, detail="Нельзя изменить другого владельца бизнеса")

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(target, field, value)
    db.commit()
    db.refresh(target)
    return user_to_dict(target)