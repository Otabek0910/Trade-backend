"""
app/api/v1/expenses.py

Подключить в main.py:
    from app.api.v1.expenses import router as expenses_router
    app.include_router(expenses_router)

Добавить в vite.config.ts прокси:
    '/expenses': { target: 'http://127.0.0.1:8000', changeOrigin: true }
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime
from decimal import Decimal

from app.db.session import get_db
from app.models.expense import Expense
from app.models.audit import AuditLog
from app.models.user import User, UserRole
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/expenses", tags=["Расходы"])

# ─── Схемы ────────────────────────────────────────────────────────────────────

CATEGORIES = [
    "Аренда",
    "Зарплата",
    "Налоги",
    "Коммунальные",
    "Транспорт",
    "Реклама",
    "Оборудование",
    "Прочее",
]

class ExpenseCreate(BaseModel):
    amount: Decimal
    category: str
    description: Optional[str] = None
    date: date

class ExpenseOut(BaseModel):
    id: int
    amount: Decimal
    category: str
    description: Optional[str]
    date: date
    created_by: int
    created_at: datetime
    creator_name: Optional[str] = None

    class Config:
        from_attributes = True


# ─── Хелпер: проверка роли ────────────────────────────────────────────────────

def require_owner_or_dev(telegram_id: int, db: Session):
    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user or user.role not in (UserRole.developer, UserRole.owner_business):
        raise HTTPException(status_code=403, detail="Только владелец или разработчик")
    return user


# ─── Эндпоинты ────────────────────────────────────────────────────────────────

@router.get("/categories")
def get_categories():
    """Список доступных категорий расходов"""
    return CATEGORIES


@router.get("", response_model=list[ExpenseOut])
def list_expenses(
    month: Optional[int] = Query(None, ge=1, le=12),
    year: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """
    Список расходов.
    Фильтры: ?month=3&year=2026&category=Аренда
    """
    require_owner_or_dev(telegram_id, db)

    q = db.query(Expense, User.full_name).join(
        User, Expense.created_by == User.id
    )

    if year:
        q = q.filter(extract("year", Expense.date) == year)
    if month:
        q = q.filter(extract("month", Expense.date) == month)
    if category:
        q = q.filter(Expense.category == category)

    rows = q.order_by(Expense.date.desc(), Expense.id.desc()).all()

    result = []
    for exp, creator_name in rows:
        out = ExpenseOut(
            id=exp.id,
            amount=exp.amount,
            category=exp.category,
            description=exp.description,
            date=exp.date,
            created_by=exp.created_by,
            created_at=exp.created_at,
            creator_name=creator_name,
        )
        result.append(out)
    return result


@router.get("/summary")
def expenses_summary(
    month: Optional[int] = Query(None, ge=1, le=12),
    year: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """
    Итого по категориям за период.
    Возвращает: { total, by_category: [{category, amount}] }
    """
    require_owner_or_dev(telegram_id, db)

    now = datetime.now()
    y = year or now.year
    m = month or now.month

    rows = (
        db.query(Expense.category, func.sum(Expense.amount).label("total"))
        .filter(
            extract("year", Expense.date) == y,
            extract("month", Expense.date) == m,
        )
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .all()
    )

    total = sum(r.total for r in rows)
    return {
        "year": y,
        "month": m,
        "total": float(total),
        "by_category": [
            {"category": r.category, "amount": float(r.total)} for r in rows
        ],
    }


@router.post("", response_model=ExpenseOut, status_code=201)
def create_expense(
    body: ExpenseCreate,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Добавить расход"""
    user = require_owner_or_dev(telegram_id, db)

    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Сумма должна быть больше 0")
    if body.category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестная категория. Допустимые: {', '.join(CATEGORIES)}",
        )

    exp = Expense(
        amount=body.amount,
        category=body.category,
        description=body.description,
        date=body.date,
        created_by=user.id,
    )
    db.add(exp)

    audit = AuditLog(
        user_id=user.id,
        action="create_expense",
        entity="expense",
        entity_id=None,
        new_values={
            "amount": float(body.amount),
            "category": body.category,
            "description": body.description,
            "date": body.date.isoformat(),
        },
    )
    db.add(audit)
    db.flush()
    audit.entity_id = exp.id

    db.commit()
    db.refresh(exp)

    return ExpenseOut(
        id=exp.id,
        amount=exp.amount,
        category=exp.category,
        description=exp.description,
        date=exp.date,
        created_by=exp.created_by,
        created_at=exp.created_at,
        creator_name=user.full_name,
    )


@router.delete("/{expense_id}", status_code=204)
def delete_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Удалить расход (только owner/developer)"""
    require_owner_or_dev(telegram_id, db)

    exp = db.query(Expense).filter(Expense.id == expense_id).first()
    if not exp:
        raise HTTPException(status_code=404, detail="Расход не найден")
    db.delete(exp)
    db.commit()