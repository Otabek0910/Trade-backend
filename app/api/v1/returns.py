"""
app/api/v1/returns.py
"""

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from decimal import Decimal

from app.db.session import get_db
from app.models.return_model import Return
from app.models.sale import Sale, SaleStatus
from app.models.sale_item import SaleItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.user import User
from app.models.audit import AuditLog
from app.core.telegram_auth import get_current_user
from app.core.notify import notify_return

router = APIRouter(prefix="/returns", tags=["Возвраты"])

# ─── Схемы ────────────────────────────────────────────────────────────────────

class ReturnCreate(BaseModel):
    sale_id: int
    product_id: int
    quantity: int
    reason: Optional[str] = None


class ReturnOut(BaseModel):
    id: int
    sale_id: int
    product_id: int
    product_name: Optional[str]
    customer_name: Optional[str]
    quantity: int
    return_amount: Decimal
    reason: Optional[str]
    created_by: int
    creator_name: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


def get_user(telegram_id: int, db: Session) -> User:
    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


# ─── Эндпоинты ────────────────────────────────────────────────────────────────

@router.get("")
def list_returns(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    rows = (
        db.query(Return, Product, Customer.name, User.full_name)
        .join(Product, Return.product_id == Product.id)
        .join(Sale, Return.sale_id == Sale.id)
        .outerjoin(Customer, Sale.customer_id == Customer.id)
        .join(User, Return.created_by == User.id)
        .order_by(Return.created_at.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id": ret.id, "sale_id": ret.sale_id, "product_id": ret.product_id,
            "product_name": product.name, "brand": product.brand,
            "unit": product.unit, "unit_value": product.unit_value,
            "customer_name": cust_name or "Розница",
            "quantity": ret.quantity, "return_amount": float(ret.return_amount),
            "reason": ret.reason, "created_by": ret.created_by,
            "creator_name": creator_name,
            "created_at": ret.created_at.isoformat(),
        }
        for ret, product, cust_name, creator_name in rows
    ]


@router.get("/recent-sales")
def get_recent_sales(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Последние продажи для быстрого выбора при возврате"""
    rows = (
        db.query(Sale, Customer.name, Customer.phone)
        .outerjoin(Customer, Sale.customer_id == Customer.id)
        .filter(Sale.status.in_([SaleStatus.completed, SaleStatus.returned]))
        .order_by(Sale.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "sale_id": sale.id,
            "customer_name": cust_name or "Розница",
            "customer_phone": cust_phone or "",
            "total_amount": float(sale.total_amount),
            "status": sale.status.value,
            "created_at": sale.created_at.isoformat(),
        }
        for sale, cust_name, cust_phone in rows
    ]


@router.get("/search-sales")
def search_sales_by_customer(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """
    Поиск продаж по имени или телефону клиента.
    ?q=Алишер  или  ?q=998901234567
    Возвращает последние 20 совпадающих продаж.
    """
    pattern = f"%{q}%"

    rows = (
        db.query(Sale, Customer.name, Customer.phone)
        .join(Customer, Sale.customer_id == Customer.id)
        .filter(
            (Customer.name.ilike(pattern)) | (Customer.phone.ilike(pattern)),
            Sale.status.in_([SaleStatus.completed, SaleStatus.returned]),
        )
        .order_by(Sale.created_at.desc())
        .limit(20)
        .all()
    )

    return [
        {
            "sale_id": sale.id,
            "customer_name": cust_name,
            "customer_phone": cust_phone,
            "total_amount": float(sale.total_amount),
            "status": sale.status.value,
            "created_at": sale.created_at.isoformat(),
        }
        for sale, cust_name, cust_phone in rows
    ]


@router.get("/sale/{sale_id}")
def get_sale_for_return(
    sale_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    sale = db.query(Sale).filter(Sale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Продажа не найдена")

    customer = db.query(Customer).filter(Customer.id == sale.customer_id).first()
    items = (
        db.query(SaleItem, Product)
        .join(Product, SaleItem.product_id == Product.id)
        .filter(SaleItem.sale_id == sale_id)
        .all()
    )

    return {
        "sale_id": sale.id,
        "customer_name": customer.name if customer else "—",
        "customer_phone": customer.phone if customer else "",
        "total_amount": float(sale.total_amount),
        "status": sale.status.value,
        "created_at": sale.created_at.isoformat(),
        "items": [
            {
                "sale_item_id": item.id,
                "product_id": item.product_id,
                "product_name": product.name,
                "brand": product.brand,
                "unit": product.unit,
                "unit_value": product.unit_value,
                "quantity": item.quantity,
                "selling_price": float(item.selling_price),
            }
            for item, product in items
        ],
    }


@router.post("", response_model=ReturnOut, status_code=201)
def create_return(
    body: ReturnCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    user = get_user(telegram_id, db)

    sale = db.query(Sale).filter(Sale.id == body.sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Продажа не найдена")

    sale_item = (
        db.query(SaleItem)
        .filter(SaleItem.sale_id == body.sale_id, SaleItem.product_id == body.product_id)
        .first()
    )
    if not sale_item:
        raise HTTPException(status_code=400, detail="Этот товар не был в данной продаже")

    if body.quantity <= 0:
        raise HTTPException(status_code=400, detail="Количество должно быть больше 0")
    if body.quantity > sale_item.quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Нельзя вернуть больше чем продано ({sale_item.quantity} шт.)",
        )

    # Сколько уже вернули по этому товару из этой продажи
    already_returned = db.query(func.sum(Return.quantity)).filter(
        Return.sale_id == body.sale_id,
        Return.product_id == body.product_id,
    ).scalar() or 0
    remaining = sale_item.quantity - already_returned
    if remaining <= 0:
        raise HTTPException(status_code=400, detail="Все единицы этого товара уже возвращены")
    if body.quantity > remaining:
        raise HTTPException(
            status_code=400,
            detail=f"Можно вернуть ещё максимум {remaining} шт."
        )

    return_amount = sale_item.selling_price * body.quantity

    ret = Return(
        sale_id=body.sale_id, product_id=body.product_id,
        quantity=body.quantity, return_amount=return_amount,
        reason=body.reason, created_by=user.id,
    )
    db.add(ret)
    db.flush()  # получаем ret.id до commit

    product = db.query(Product).filter(Product.id == body.product_id).first()
    if product:
        product.current_stock += body.quantity

    # Уменьшаем долг клиента — но только на ту часть, которую он ещё не заплатил
    debt_reduction = 0.0
    customer = None
    if sale.customer_id:
        customer = db.query(Customer).filter(Customer.id == sale.customer_id).first()
        if customer and float(customer.total_debt or 0) > 0:
            debt_reduction = min(float(return_amount), float(customer.total_debt))
            customer.total_debt = max(0.0, float(customer.total_debt) - debt_reduction)

    # Запоминаем статус продажи ДО изменения (для корректной отмены через журнал)
    sale_status_before = sale.status.value

    # Статус продажи: returned если все позиции полностью возвращены
    # Не делаем повторный запрос (он кэшируется) — считаем вручную с учётом текущего возврата
    all_items = db.query(SaleItem).filter(SaleItem.sale_id == body.sale_id).all()
    all_fully_returned = True
    for si in all_items:
        if si.product_id == body.product_id:
            # Для текущего товара: уже возвращено + текущий возврат
            total = already_returned + body.quantity
        else:
            # Для других товаров: запрашиваем свежо с execution_options
            total = db.query(func.sum(Return.quantity))\
                .filter(Return.sale_id == body.sale_id, Return.product_id == si.product_id)\
                .execution_options(populate_existing=True)\
                .scalar() or 0
        if total < si.quantity:
            all_fully_returned = False
            break
    if all_fully_returned:
        sale.status = SaleStatus.returned

    # Журнал событий
    audit = AuditLog(
        user_id=user.id,
        action="create_return",
        entity="return",
        entity_id=ret.id,
        old_values={
            "sale_status": sale_status_before,       # статус продажи ДО возврата
            "debt_reduction": debt_reduction,         # на сколько уменьшили долг
            "stock_before": (product.current_stock - body.quantity) if product else 0,
        },
        new_values={
            "product_name": product.name if product else None,
            "product_id": body.product_id,
            "sale_id": body.sale_id,
            "quantity": body.quantity,
            "return_amount": float(return_amount),
            "reason": body.reason,
        },
    )
    db.add(audit)

    db.commit()
    db.refresh(ret)

    background_tasks.add_task(
        notify_return, db=db, creator_name=user.full_name,
        product_name=product.name if product else "—",
        customer_name=customer.name if customer else "Розница",
        quantity=body.quantity, amount=float(return_amount),
    )

    return ReturnOut(
        id=ret.id, sale_id=ret.sale_id, product_id=ret.product_id,
        product_name=product.name if product else None,
        customer_name=None, quantity=ret.quantity,
        return_amount=ret.return_amount, reason=ret.reason,
        created_by=ret.created_by, creator_name=user.full_name,
        created_at=ret.created_at,
    )