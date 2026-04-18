"""
app/api/v1/supplier_returns.py

Возвраты товара поставщику.
Логика баланса:
  return_amount = qty × purchase_price
  if return_amount <= total_debt:
      total_debt -= return_amount          # просто уменьшаем долг
  else:
      credit = return_amount - total_debt  # поставщик начинает должен нам
      total_debt = 0
      total_credit += credit

Подключить в main.py:
    from app.api.v1.supplier_returns import router as supplier_returns_router
    app.include_router(supplier_returns_router)

Миграции в main.py lifespan:
    "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS total_credit NUMERIC(12,2) NOT NULL DEFAULT 0",
    CREATE TABLE IF NOT EXISTS supplier_returns (...)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from decimal import Decimal
from typing import Optional

from app.db.session import get_db
from app.models.supplier_return import SupplierReturn
from app.models.supplier import Supplier
from app.models.product import Product
from app.models.user import User
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/supplier-returns", tags=["Возвраты поставщикам"])


class SupplierReturnCreate(BaseModel):
    supplier_id: int
    product_id: int
    quantity: int
    purchase_price: Decimal
    reason: Optional[str] = None


@router.post("")
def create_supplier_return(
    data: SupplierReturnCreate,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """
    Возврат товара поставщику.
    - Уменьшает сток товара
    - Уменьшает наш долг поставщику (total_debt)
    - Если возврат больше долга — поставщик начинает быть нам должен (total_credit)
    """
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    supplier = db.query(Supplier).filter(Supplier.id == data.supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    product = db.query(Product).filter(Product.id == data.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    if data.quantity <= 0:
        raise HTTPException(status_code=400, detail="Количество должно быть больше 0")

    if product.current_stock < data.quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Недостаточно товара на складе: есть {product.current_stock} шт, "
                   f"возвращаете {data.quantity} шт",
        )

    return_amount = round(float(data.purchase_price) * data.quantity, 2)
    current_debt   = round(float(supplier.total_debt or 0), 2)
    current_credit = round(float(supplier.total_credit or 0), 2)

    # ── Пересчёт баланса ─────────────────────────────────────────────────────
    if return_amount <= current_debt:
        # Возврат полностью покрывается нашим долгом
        debt_reduced = return_amount
        credit_added = 0.0
        supplier.total_debt = round(current_debt - return_amount, 2)
    else:
        # Возврат превышает долг → поставщик начинает нам должен
        debt_reduced = current_debt
        credit_added = round(return_amount - current_debt, 2)
        supplier.total_debt   = 0.0
        supplier.total_credit = round(current_credit + credit_added, 2)

    # ── Списываем товар со склада ────────────────────────────────────────────
    product.current_stock -= data.quantity

    # ── Запись в историю ─────────────────────────────────────────────────────
    ret = SupplierReturn(
        supplier_id    = data.supplier_id,
        product_id     = data.product_id,
        quantity       = data.quantity,
        purchase_price = data.purchase_price,
        return_amount  = return_amount,
        debt_reduced   = round(debt_reduced, 2),
        credit_added   = round(credit_added, 2),
        reason         = data.reason,
        created_by     = user.id,
    )
    db.add(ret)
    db.commit()
    db.refresh(ret)

    # ── Сообщение ────────────────────────────────────────────────────────────
    parts = [f"✅ Возвращено {data.quantity} шт · {return_amount:,.0f} сум."]
    if debt_reduced > 0:
        parts.append(f"Наш долг уменьшен на {debt_reduced:,.0f} сум.")
    if credit_added > 0:
        parts.append(f"💚 Поставщик теперь должен вам {credit_added:,.0f} сум.")

    return {
        "id":               ret.id,
        "return_amount":    return_amount,
        "debt_reduced":     debt_reduced,
        "credit_added":     credit_added,
        "new_total_debt":   float(supplier.total_debt),
        "new_total_credit": float(supplier.total_credit),
        "new_stock":        product.current_stock,
        "message":          " ".join(parts),
    }


@router.get("/supplier/{supplier_id}")
def get_supplier_returns(
    supplier_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """История возвратов по поставщику"""
    rows = (
        db.query(SupplierReturn, Product.name, Product.unit, Product.unit_value, User.full_name)
        .join(Product, SupplierReturn.product_id == Product.id)
        .join(User, SupplierReturn.created_by == User.id)
        .filter(SupplierReturn.supplier_id == supplier_id)
        .order_by(SupplierReturn.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id":             r.id,
            "product_name":   prod_name,
            "unit":           unit or "шт",
            "unit_value":     unit_value,
            "quantity":       r.quantity,
            "purchase_price": float(r.purchase_price),
            "return_amount":  float(r.return_amount),
            "debt_reduced":   float(r.debt_reduced),
            "credit_added":   float(r.credit_added),
            "reason":         r.reason,
            "creator_name":   creator_name,
            "created_at":     r.created_at.isoformat(),
        }
        for r, prod_name, unit, unit_value, creator_name in rows
    ]
