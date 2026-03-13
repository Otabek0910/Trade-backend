from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal

from app.db.session import get_db
from app.models.sale import Sale, PaymentType, SaleStatus
from app.models.sale_item import SaleItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.user import User
from app.models.audit import AuditLog
from app.models.return_model import Return
from app.core.telegram_auth import get_current_user
from app.core.notify import notify_sale

router = APIRouter(prefix="/sales", tags=["Продажи"])


# ─── Schemas ─────────────────────────────────────────────────────

class SaleItemIn(BaseModel):
    product_id: int
    quantity: int
    selling_price: Decimal

class SaleCreate(BaseModel):
    customer_id: Optional[int] = None
    items: list[SaleItemIn]
    payment_type: PaymentType
    discount_percent: Decimal = Decimal("0")
    paid_amount: Decimal


# ─── Endpoints ───────────────────────────────────────────────────

@router.post("")
def create_sale(
    data: SaleCreate,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    if not data.items:
        raise HTTPException(status_code=400, detail="Корзина пуста")

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Проверяем остатки и считаем сумму
    total = Decimal("0")
    items_data = []
    for item in data.items:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Товар {item.product_id} не найден")
        if product.current_stock < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Недостаточно товара «{product.name}»: есть {product.current_stock} шт, нужно {item.quantity}"
            )
        total += item.selling_price * item.quantity
        items_data.append((product, item))

    # Применяем скидку
    if data.discount_percent > 0:
        total = total * (1 - data.discount_percent / 100)
    total = total.quantize(Decimal("0.01"))

    # Ограничиваем оплату — нельзя заплатить больше суммы
    actual_paid = min(data.paid_amount, total)

    # Создаём продажу
    sale = Sale(
        seller_id=user.id,
        customer_id=data.customer_id,
        total_amount=total,
        payment_type=data.payment_type,
        discount_percent=data.discount_percent,
        paid_amount=actual_paid,
        status=SaleStatus.completed,
    )
    db.add(sale)
    db.flush()  # получаем sale.id

    # Создаём позиции и списываем остатки
    for product, item in items_data:
        sale_item = SaleItem(
            sale_id=sale.id,
            product_id=product.id,
            quantity=item.quantity,
            selling_price=item.selling_price,
            purchase_price_at_sale=product.purchase_price,
        )
        db.add(sale_item)
        product.current_stock -= item.quantity

    # Обновляем долг клиента
    debt = total - actual_paid
    if data.customer_id and debt > 0:
        customer = db.query(Customer).filter(Customer.id == data.customer_id).first()
        if customer:
            customer.total_debt = (customer.total_debt or 0) + debt
            customer.total_purchases = (customer.total_purchases or 0) + total

    # Журнал событий
    audit = AuditLog(
        user_id=user.id,
        action="create_sale",
        entity="sale",
        entity_id=sale.id,
        new_values={
            "total_amount": float(total),
            "paid_amount": float(actual_paid),
            "debt": float(debt) if debt > 0 else 0,
            "customer_id": data.customer_id,
            "payment_type": data.payment_type.value,
            "items": [
                {
                    "product_id": p.id,
                    "product_name": p.name,
                    "quantity": i.quantity,
                    "selling_price": float(i.selling_price),
                }
                for p, i in items_data
            ],
        },
    )
    db.add(audit)

    db.commit()
    db.refresh(sale)

    # Уведомление подписчикам
    import asyncio
    customer_name = "Розница"
    if data.customer_id:
        cust = db.query(Customer).filter(Customer.id == data.customer_id).first()
        if cust:
            customer_name = cust.name
    try:
        asyncio.create_task(notify_sale(
            db=db, seller_name=user.full_name,
            customer_name=customer_name,
            total=float(total), items_count=len(data.items), sale_id=sale.id,
        ))
    except RuntimeError:
        pass  # не в async контексте

    return {
        "id": sale.id,
        "total_amount": float(total),
        "paid_amount": float(actual_paid),
        "debt": float(debt) if debt > 0 else 0,
        "payment_type": data.payment_type.value,
        "items_count": len(data.items),
        "message": f"✅ Продажа #{sale.id} оформлена на {total:,.0f} сум",
    }


@router.get("")
def get_sales(
    limit: int = 30,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    from sqlalchemy import func
    sales = db.query(Sale).order_by(Sale.created_at.desc()).limit(limit).all()

    # Считаем возвраты по каждой продаже одним запросом
    sale_ids = [s.id for s in sales]
    returns_map: dict[int, float] = {}
    if sale_ids:
        for sid, total in db.query(Return.sale_id, func.sum(Return.return_amount))\
                .filter(Return.sale_id.in_(sale_ids))\
                .group_by(Return.sale_id).all():
            returns_map[sid] = float(total or 0)

    result = []
    for s in sales:
        returned_amount = returns_map.get(s.id, 0.0)
        total = float(s.total_amount)
        is_partial_return = returned_amount > 0 and s.status.value != 'returned'
        result.append({
            "id": s.id,
            "seller": s.seller.full_name if s.seller else "—",
            "customer": s.customer.name if s.customer else "Розница",
            "total_amount": total,
            "paid_amount": float(s.paid_amount),
            "debt": float(s.total_amount - s.paid_amount),
            "returned_amount": returned_amount,
            "is_partial_return": is_partial_return,
            "payment_type": s.payment_type.value,
            "discount_percent": float(s.discount_percent),
            "status": s.status.value,
            "items_count": len(s.items),
            "items": [
                {
                    "product_name": item.product.name if item.product else "—",
                    "quantity": item.quantity,
                    "selling_price": float(item.selling_price),
                }
                for item in s.items
            ],
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })
    return result


@router.get("/today-stats")
def get_today_stats(
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    from datetime import date
    from sqlalchemy import func, cast, Date

    today = date.today()
    sales = db.query(Sale).filter(
        cast(Sale.created_at, Date) == today,
        Sale.status == SaleStatus.completed,
    ).all()

    total_revenue = sum(float(s.total_amount) for s in sales)
    total_paid = sum(float(s.paid_amount) for s in sales)

    # Считаем маржу
    margin = 0.0
    for s in sales:
        for item in s.items:
            margin += (float(item.selling_price) - float(item.purchase_price_at_sale)) * item.quantity

    return {
        "sales_count": len(sales),
        "total_revenue": total_revenue,
        "total_paid": total_paid,
        "total_debt": total_revenue - total_paid,
        "margin": round(margin, 2),
    }