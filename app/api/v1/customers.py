from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal
import os, uuid

from app.db.session import get_db
from app.models.customer import Customer
from app.models.sale import Sale, SaleStatus
from app.models.return_model import Return
from app.models.product import Product
from app.models.debt_payment import DebtPayment
from app.models.user import User
from app.core.telegram_auth import get_current_user
from app.core.telegram_media import upload_photo_to_telegram

router = APIRouter(prefix="/customers", tags=["Клиенты"])

UPLOAD_DIR = "uploads/customers"
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_SIZE_MB = 5


class CustomerCreate(BaseModel):
    name: str
    phone: str
    address: Optional[str] = None

class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

class DebtPaymentIn(BaseModel):
    amount: Decimal
    note: Optional[str] = None  # ← новое поле: заметка (напр. "наличные", "перевод")

class LocationUpdate(BaseModel):
    lat: float
    lng: float


def _resolve_photo(url: str | None) -> str | None:
    if url and url.startswith("tg:"):
        return f"/media/photo/{url[3:]}"
    return url

def customer_to_dict(c: Customer) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "phone": c.phone,
        "address": c.address,
        "photo_url": _resolve_photo(getattr(c, 'photo_url', None)),
        "lat": getattr(c, 'lat', None),
        "lng": getattr(c, 'lng', None),
        "total_purchases": float(c.total_purchases or 0),
        "total_debt": float(c.total_debt or 0),
        "is_active": bool(getattr(c, 'is_active', True)),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("")
def get_customers(
    search: Optional[str] = None,
    has_debt: Optional[bool] = None,
    show_inactive: bool = False,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    q = db.query(Customer)
    if not show_inactive:
        q = q.filter(Customer.is_active == True)
    if search:
        q = q.filter(or_(
            Customer.name.ilike(f"%{search}%"),
            Customer.phone.ilike(f"%{search}%"),
        ))
    if has_debt is True:
        q = q.filter(Customer.total_debt > 0)
    return [customer_to_dict(c) for c in q.order_by(Customer.name).all()]


@router.get("/{customer_id}/history")
def get_customer_history(
    customer_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    sales = (
        db.query(Sale)
        .filter(
            Sale.customer_id == customer_id,
            Sale.status.in_([SaleStatus.completed, SaleStatus.returned]),
        )
        .order_by(Sale.created_at.desc())
        .limit(30).all()
    )

    returns = (
        db.query(Return, Product.name)
        .join(Sale, Return.sale_id == Sale.id)
        .join(Product, Return.product_id == Product.id)
        .filter(Sale.customer_id == customer_id)
        .order_by(Return.created_at.desc())
        .limit(20).all()
    )

    # ── История погашений долга ──────────────────────────────────
    payments = (
        db.query(DebtPayment, User.full_name)
        .join(User, DebtPayment.created_by == User.id)
        .filter(DebtPayment.customer_id == customer_id)
        .order_by(DebtPayment.created_at.desc())
        .limit(30).all()
    )

    return {
        "customer": customer_to_dict(c),
        "sales": [
            {
                "id": s.id,
                "total_amount": float(s.total_amount),
                "paid_amount": float(s.paid_amount),
                "debt": float(s.total_amount - s.paid_amount),
                "payment_type": s.payment_type.value,
                "discount_percent": float(s.discount_percent),
                "status": s.status.value,
                "items": [
                    {
                        "product_name": item.product.name if item.product else "—",
                        "brand": item.product.brand if item.product else None,
                        "unit": item.product.unit if item.product else None,
                        "unit_value": item.product.unit_value if item.product else None,
                        "quantity": item.quantity,
                        "selling_price": float(item.selling_price),
                        "total": float(item.selling_price) * item.quantity,
                    }
                    for item in s.items
                ],
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in sales
        ],
        "returns": [
            {
                "id": ret.id,
                "sale_id": ret.sale_id,
                "product_name": prod_name,
                "quantity": ret.quantity,
                "return_amount": float(ret.return_amount),
                "reason": ret.reason,
                "created_at": ret.created_at.isoformat(),
            }
            for ret, prod_name in returns
        ],
        # ── Новый блок: погашения долга ──
        "debt_payments": [
            {
                "id": dp.id,
                "amount": float(dp.amount),
                "note": dp.note,
                "paid_by": user_name,
                "created_at": dp.created_at.isoformat(),
            }
            for dp, user_name in payments
        ],
    }


@router.post("")
def create_customer(
    data: CustomerCreate,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    if db.query(Customer).filter(Customer.phone == data.phone).first():
        raise HTTPException(status_code=400, detail="Клиент с таким телефоном уже существует")
    customer = Customer(**data.model_dump())
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer_to_dict(customer)


@router.patch("/{customer_id}")
def update_customer(
    customer_id: int,
    data: CustomerUpdate,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(c, field, value)
    db.commit()
    db.refresh(c)
    return customer_to_dict(c)


@router.post("/{customer_id}/pay-debt")
def pay_debt(
    customer_id: int,
    data: DebtPaymentIn,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """
    Погашение долга клиента.
    - Обновляет paid_amount у самых старых неоплаченных продаж (FIFO)
    - Логирует платёж в debt_payments (виден в истории клиента)
    - Уменьшает customer.total_debt
    - После этого статистика по продавцам автоматически обновляется
    """
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    current_debt = float(c.total_debt or 0)
    payment = float(data.amount)

    if payment <= 0:
        raise HTTPException(status_code=400, detail="Сумма должна быть больше нуля")
    if payment > current_debt + 0.01:  # допуск на округление
        raise HTTPException(status_code=400, detail=f"Превышает долг ({current_debt:,.0f} сум)")

    # ── Распределяем платёж по продажам (FIFO — самые старые первыми) ──
    unpaid_sales = (
        db.query(Sale)
        .filter(
            Sale.customer_id == customer_id,
            Sale.status == SaleStatus.completed,
            Sale.paid_amount < Sale.total_amount,
        )
        .order_by(Sale.created_at.asc())
        .all()
    )

    remaining = payment
    for sale in unpaid_sales:
        if remaining <= 0:
            break
        sale_debt = float(sale.total_amount) - float(sale.paid_amount)
        pay_this = min(remaining, sale_debt)
        sale.paid_amount = float(sale.paid_amount) + pay_this
        remaining -= pay_this

    # ── Уменьшаем суммарный долг клиента ──
    c.total_debt = max(0.0, current_debt - payment)

    # ── Получаем пользователя для лога ──
    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # ── Логируем погашение ──
    dp = DebtPayment(
        customer_id=customer_id,
        amount=data.amount,
        note=data.note,
        created_by=user.id,
    )
    db.add(dp)
    db.commit()

    return {
        "message": f"✅ Принято {payment:,.0f} сум",
        "previous_debt": current_debt,
        "paid": payment,
        "remaining_debt": float(c.total_debt),
    }


@router.delete("/{customer_id}")
def delete_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    has_sales = db.query(Sale).filter(Sale.customer_id == customer_id).first() is not None

    if has_sales:
        c.is_active = False
        db.commit()
        return {"deleted": False, "deactivated": True, "message": "Клиент скрыт (есть история продаж)"}
    else:
        old_url = getattr(c, 'photo_url', None)
        if old_url:
            path = os.path.join("uploads", old_url.lstrip("/static/"))
            if os.path.exists(path):
                os.remove(path)
        db.delete(c)
        db.commit()
        return {"deleted": True, "deactivated": False, "message": "Клиент удалён"}


@router.post("/{customer_id}/restore")
def restore_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    c.is_active = True
    db.commit()
    return customer_to_dict(c)


# ─── Фото клиента ─────────────────────────────────────────────────────────────

@router.post("/{customer_id}/photo")
async def upload_customer_photo(
    customer_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Только JPEG, PNG или WebP")
    contents = await file.read()
    if len(contents) > MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Файл больше {MAX_SIZE_MB} МБ")
    file_id = await upload_photo_to_telegram(contents, file.filename or "photo.jpg")
    c.photo_url = f"tg:{file_id}"
    db.commit()
    return {"photo_url": f"/media/photo/{file_id}"}


@router.delete("/{customer_id}/photo", status_code=204)
def delete_customer_photo(
    customer_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    c.photo_url = None
    db.commit()


# ─── Локация клиента ──────────────────────────────────────────────────────────

@router.post("/{customer_id}/location")
def update_customer_location(
    customer_id: int,
    data: LocationUpdate,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    c.lat = data.lat
    c.lng = data.lng
    db.commit()
    return {"lat": c.lat, "lng": c.lng}


@router.delete("/{customer_id}/location", status_code=204)
def delete_customer_location(
    customer_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    c.lat = None
    c.lng = None
    db.commit()