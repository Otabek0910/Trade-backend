from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from decimal import Decimal

from app.db.session import get_db
from app.models.receipt import Receipt
from app.models.product import Product
from app.models.supplier import Supplier
from app.models.user import User
from app.models.audit import AuditLog
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/receipts", tags=["Приёмка товара"])


# ─── Schemas ─────────────────────────────────────────────────────

class ReceiptCreate(BaseModel):
    product_id: int
    supplier_id: int
    quantity: int
    purchase_price: Decimal  # цена этой партии


# ─── Endpoints ───────────────────────────────────────────────────

@router.post("")
def create_receipt(
    data: ReceiptCreate,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Приёмка товара — увеличивает остаток и пересчитывает среднюю цену"""

    product = db.query(Product).filter(Product.id == data.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    supplier = db.query(Supplier).filter(Supplier.id == data.supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Средневзвешенная цена закупки
    old_stock = product.current_stock
    old_price = float(product.purchase_price)
    new_qty = data.quantity
    new_price = float(data.purchase_price)

    if old_stock + new_qty > 0:
        weighted_avg = (
            (old_stock * old_price) + (new_qty * new_price)
        ) / (old_stock + new_qty)
        product.purchase_price = round(weighted_avg, 2)

    product.current_stock = old_stock + new_qty

    receipt = Receipt(
        product_id=data.product_id,
        supplier_id=data.supplier_id,
        quantity=data.quantity,
        purchase_price=data.purchase_price,
        storekeeper_id=user.id,
    )
    db.add(receipt)

    # Журнал событий (сохраняем старую цену для возможности отмены)
    audit = AuditLog(
        user_id=user.id,
        action="create_receipt",
        entity="receipt",
        entity_id=None,  # получим после flush
        old_values={
            "purchase_price": old_price,
            "old_stock": old_stock,
        },
        new_values={
            "product_id": data.product_id,
            "product_name": product.name,
            "supplier_name": supplier.name,
            "quantity": data.quantity,
            "purchase_price": float(data.purchase_price),
            "new_stock": product.current_stock,
        },
    )
    db.add(audit)
    db.flush()  # получаем receipt.id и audit.id
    audit.entity_id = receipt.id

    db.commit()
    db.refresh(receipt)

    return {
        "id": receipt.id,
        "product_name": product.name,
        "supplier_name": supplier.name,
        "quantity": data.quantity,
        "purchase_price": float(data.purchase_price),
        "new_stock": product.current_stock,
        "new_avg_price": float(product.purchase_price),
        "message": f"✅ Принято {data.quantity} шт. Остаток: {product.current_stock}",
    }


@router.get("")
def get_receipts(
    limit: int = 20,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """История приёмок"""
    receipts = db.query(Receipt).order_by(
        Receipt.created_at.desc()
    ).limit(limit).all()

    return [
        {
            "id": r.id,
            "product_name": r.product.name,
            "supplier_name": r.supplier.name,
            "quantity": r.quantity,
            "purchase_price": float(r.purchase_price),
            "storekeeper": r.storekeeper.full_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in receipts
    ]