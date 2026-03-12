from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from decimal import Decimal
from typing import Optional

from app.db.session import get_db
from app.models.receipt import Receipt
from app.models.product import Product
from app.models.supplier import Supplier
from app.models.user import User
from app.models.audit import AuditLog
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/receipts", tags=["Приёмка товара"])


class ReceiptCreate(BaseModel):
    product_id: int
    supplier_id: int
    quantity: int
    purchase_price: Decimal
    paid_amount: Optional[Decimal] = None  # если None — значит оплачено полностью


@router.post("")
def create_receipt(
    data: ReceiptCreate,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    product = db.query(Product).filter(Product.id == data.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    supplier = db.query(Supplier).filter(Supplier.id == data.supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    total_cost = float(data.purchase_price) * data.quantity
    paid = float(data.paid_amount) if data.paid_amount is not None else total_cost
    paid = max(0.0, min(paid, total_cost))  # не может быть меньше 0 или больше total
    debt = round(total_cost - paid, 2)

    # Средневзвешенная цена закупки
    old_stock = product.current_stock
    old_price = float(product.purchase_price)
    new_qty = data.quantity
    new_price = float(data.purchase_price)

    if old_stock + new_qty > 0:
        weighted_avg = (old_stock * old_price + new_qty * new_price) / (old_stock + new_qty)
        product.purchase_price = round(weighted_avg, 2)

    product.current_stock = old_stock + new_qty

    receipt = Receipt(
        product_id=data.product_id,
        supplier_id=data.supplier_id,
        quantity=data.quantity,
        purchase_price=data.purchase_price,
        paid_amount=round(paid, 2),
        debt=debt,
        storekeeper_id=user.id,
    )
    db.add(receipt)

    # Обновляем долг поставщику
    if debt > 0:
        supplier.total_debt = float(supplier.total_debt or 0) + debt

    audit = AuditLog(
        user_id=user.id,
        action="create_receipt",
        entity="receipt",
        entity_id=None,
        old_values={"purchase_price": old_price, "old_stock": old_stock},
        new_values={
            "product_id": data.product_id,
            "product_name": product.name,
            "supplier_name": supplier.name,
            "quantity": data.quantity,
            "purchase_price": float(data.purchase_price),
            "paid_amount": paid,
            "debt": debt,
            "new_stock": product.current_stock,
        },
    )
    db.add(audit)
    db.flush()
    audit.entity_id = receipt.id

    db.commit()
    db.refresh(receipt)

    debt_msg = f" · Долг поставщику: {debt:,.0f} сум" if debt > 0 else ""
    return {
        "id": receipt.id,
        "product_name": product.name,
        "supplier_name": supplier.name,
        "quantity": data.quantity,
        "purchase_price": float(data.purchase_price),
        "total_cost": total_cost,
        "paid_amount": paid,
        "debt": debt,
        "new_stock": product.current_stock,
        "new_avg_price": float(product.purchase_price),
        "message": f"✅ Принято {data.quantity} шт. Остаток: {product.current_stock}{debt_msg}",
    }


@router.get("")
def get_receipts(
    limit: int = 20,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    receipts = db.query(Receipt).order_by(Receipt.created_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "product_name": r.product.name,
            "supplier_name": r.supplier.name,
            "quantity": r.quantity,
            "purchase_price": float(r.purchase_price),
            "paid_amount": float(getattr(r, 'paid_amount', 0) or 0),
            "debt": float(getattr(r, 'debt', 0) or 0),
            "storekeeper": r.storekeeper.full_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in receipts
    ]

@router.get("/product/{product_id}")
def get_product_price_history(
    product_id: int,
    page: int = 1,
    limit: int = 10,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """История приёмок и изменений цены закупки по товару"""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    total = db.query(Receipt).filter(Receipt.product_id == product_id).count()
    receipts = (
        db.query(Receipt)
        .filter(Receipt.product_id == product_id)
        .order_by(Receipt.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
        "current_price": float(product.purchase_price),
        "items": [
            {
                "id": r.id,
                "quantity": r.quantity,
                "purchase_price": float(r.purchase_price),
                "supplier_name": r.supplier.name if r.supplier else "—",
                "storekeeper": r.storekeeper.full_name if r.storekeeper else "—",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in receipts
        ],
    }