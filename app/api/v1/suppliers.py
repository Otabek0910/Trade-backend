from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal
import os, uuid

from app.db.session import get_db
from app.models.supplier import Supplier
from app.models.product import Product
from app.models.receipt import Receipt
from app.models.supplier_payment import SupplierPayment
from app.models.user import User
from app.core.telegram_auth import get_current_user
from app.core.telegram_media import upload_photo_to_telegram

router = APIRouter(prefix="/suppliers", tags=["Поставщики"])

UPLOAD_DIR = "uploads/suppliers"
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_SIZE_MB = 5


class SupplierCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None

class SupplierUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

class LocationUpdate(BaseModel):
    lat: float
    lng: float

class PayDebtSchema(BaseModel):
    amount: Decimal
    note: Optional[str] = None


def _resolve_photo(url: str | None) -> str | None:
    if url and url.startswith("tg:"):
        return f"https://trade-backend-k71d.onrender.com/media/photo/{url[3:]}"
    return url

def supplier_to_dict(s: Supplier, db: Session) -> dict:
    products_count = db.query(func.count(Product.id)).filter(Product.supplier_id == s.id).scalar() or 0
    total_receipts = db.query(func.count(Receipt.id)).filter(Receipt.supplier_id == s.id).scalar() or 0
    total_purchased = db.query(
        func.sum(Receipt.purchase_price * Receipt.quantity)
    ).filter(Receipt.supplier_id == s.id).scalar() or 0

    return {
        "id": s.id,
        "name": s.name,
        "phone": s.phone,
        "address": s.address,
        "notes": s.notes,
        "photo_url": _resolve_photo(getattr(s, 'photo_url', None)),
        "lat": getattr(s, 'lat', None),
        "lng": getattr(s, 'lng', None),
        "products_count": products_count,
        "total_receipts": total_receipts,
        "total_purchased": float(total_purchased),
        "total_debt": float(getattr(s, 'total_debt', 0) or 0),
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


@router.get("")
def get_suppliers(
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return [supplier_to_dict(s, db) for s in suppliers]


@router.get("/{supplier_id}")
def get_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    receipts = (
        db.query(Receipt)
        .filter(Receipt.supplier_id == supplier_id)
        .order_by(Receipt.created_at.desc())
        .limit(10).all()
    )

    return {
        **supplier_to_dict(s, db),
        "recent_receipts": [
            {
                "id": r.id,
                "product_name": r.product.name if r.product else "—",
                "quantity": r.quantity,
                "purchase_price": float(r.purchase_price),
                "total": float(r.purchase_price) * r.quantity,
                "paid_amount": float(getattr(r, 'paid_amount', 0) or 0),
                "debt": float(getattr(r, 'debt', 0) or 0),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "unit": r.product.unit or "шт" if r.product else "шт",
                "unit_value": r.product.unit_value if r.product else None,
            }
            for r in receipts
        ],
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "sku": p.sku,
                "current_stock": p.current_stock,
                "purchase_price": float(p.purchase_price),
                "selling_price": float(p.selling_price),
                "unit": p.unit or "шт",
                "unit_value": p.unit_value,
            }
            for p in s.products
        ],
        "debt_payments": [
            {
                "id": p.id,
                "amount": float(p.amount),
                "note": p.note,
                "user_name": p.user.full_name if p.user else "—",
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in db.query(SupplierPayment)
                .filter(SupplierPayment.supplier_id == supplier_id)
                .order_by(SupplierPayment.created_at.desc())
                .limit(20).all()
        ],
    }


@router.post("/{supplier_id}/pay-debt")
def pay_supplier_debt(
    supplier_id: int,
    data: PayDebtSchema,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Погасить долг поставщику"""
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    current_debt = float(s.total_debt or 0)
    amount = float(data.amount)

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Сумма должна быть больше 0")
    if amount > current_debt:
        raise HTTPException(status_code=400, detail=f"Сумма больше долга ({current_debt:,.0f} сум)")

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    s.total_debt = round(current_debt - amount, 2)

    payment = SupplierPayment(
        supplier_id=supplier_id,
        amount=amount,
        note=data.note,
        created_by=user.id,
    )
    db.add(payment)
    db.commit()
    db.refresh(s)

    return {
        "message": f"✅ Оплачено {amount:,.0f} сум. Остаток долга: {float(s.total_debt):,.0f} сум",
        "total_debt": float(s.total_debt),
        "payment_id": payment.id,
    }


@router.post("")
def create_supplier(
    data: SupplierCreate,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    supplier = Supplier(**data.model_dump())
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier_to_dict(supplier, db)


@router.patch("/{supplier_id}")
def update_supplier(
    supplier_id: int,
    data: SupplierUpdate,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(s, field, value)
    db.commit()
    db.refresh(s)
    return supplier_to_dict(s, db)


@router.delete("/{supplier_id}")
def delete_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    products_count = db.query(func.count(Product.id)).filter(Product.supplier_id == supplier_id).scalar() or 0
    if products_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Нельзя удалить: у поставщика {products_count} товаров. Сначала переназначьте их."
        )

    db.delete(s)
    db.commit()
    return {"message": "✅ Поставщик удалён"}


# ─── Фото поставщика ──────────────────────────────────────────────────────────

@router.post("/{supplier_id}/photo")
async def upload_supplier_photo(
    supplier_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Только JPEG, PNG или WebP")
    contents = await file.read()
    if len(contents) > MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Файл больше {MAX_SIZE_MB} МБ")
    file_id = await upload_photo_to_telegram(contents, file.filename or "photo.jpg")
    s.photo_url = f"tg:{file_id}"
    db.commit()
    return {"photo_url": f"/media/photo/{file_id}"}


@router.delete("/{supplier_id}/photo", status_code=204)
def delete_supplier_photo(
    supplier_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    s.photo_url = None
    db.commit()


# ─── Локация поставщика ───────────────────────────────────────────────────────

@router.post("/{supplier_id}/location")
def update_supplier_location(
    supplier_id: int,
    data: LocationUpdate,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """Сохранить GPS-координаты поставщика"""
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    s.lat = data.lat
    s.lng = data.lng
    db.commit()
    return {"lat": s.lat, "lng": s.lng}


@router.delete("/{supplier_id}/location", status_code=204)
def delete_supplier_location(
    supplier_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """Удалить GPS-координаты поставщика"""
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    s.lat = None
    s.lng = None
    db.commit()