# app/api/v1/products.py
import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal

from app.db.session import get_db
from app.models.product import Product
from app.models.sale import Sale, SaleStatus
from app.models.sale_item import SaleItem
from app.models.user import User, UserRole
from app.core.telegram_auth import get_current_user
from app.core.telegram_media import upload_photo_to_telegram

router = APIRouter(prefix="/products", tags=["Товары"])

UPLOAD_DIR = "uploads/products"
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_SIZE_MB = 5

UNITS = ["шт", "л", "кг", "м", "м²", "уп", "пар", "рул"]


# ─── Schemas ──────────────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    sku: str
    name: str
    category: Optional[str] = None
    brand: Optional[str] = None          # ← Марка: Mobil, Shell, Castrol
    unit: Optional[str] = "шт"          # ← Единица: шт/л/кг/м/уп...
    unit_value: Optional[float] = None  # ← Объём упаковки: 3 (для канистры 3л)
    supplier_id: Optional[int] = None
    purchase_price: Decimal
    selling_price: Decimal
    min_stock: int = 5

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    brand: Optional[str] = None
    unit: Optional[str] = None
    unit_value: Optional[float] = None
    supplier_id: Optional[int] = None
    purchase_price: Optional[Decimal] = None
    selling_price: Optional[Decimal] = None
    min_stock: Optional[int] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_photo(url: str | None) -> str | None:
    if url and url.startswith("tg:"):
        return f"/media/photo/{url[3:]}"
    return url

def product_to_dict(p: Product) -> dict:
    unit = getattr(p, 'unit', 'шт') or 'шт'
    unit_value = getattr(p, 'unit_value', None)
    return {
        "id": p.id,
        "sku": p.sku,
        "name": p.name,
        "category": p.category,
        "brand": getattr(p, 'brand', None),
        "unit": unit,
        "unit_value": unit_value,
        "supplier_id": p.supplier_id,
        "supplier_name": p.supplier.name if p.supplier else None,
        "purchase_price": float(p.purchase_price),
        "selling_price": float(p.selling_price),
        "min_stock": p.min_stock,
        "current_stock": p.current_stock,
        "photo_url": _resolve_photo(getattr(p, 'photo_url', None)),
        "low_stock": p.current_stock <= p.min_stock,
        "margin_percent": round(
            (float(p.selling_price) - float(p.purchase_price))
            / float(p.purchase_price) * 100, 1
        ) if float(p.purchase_price) > 0 else 0,
    }

def require_stock_access(telegram_id: int, db: Session) -> User:
    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user or user.role not in (UserRole.developer, UserRole.owner_business, UserRole.storekeeper):
        raise HTTPException(status_code=403, detail="Нет доступа")
    return user


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/units")
def get_units():
    """Список доступных единиц измерения"""
    return UNITS


@router.get("")
def get_products(
    search: Optional[str] = None,
    category: Optional[str] = None,
    low_stock: Optional[bool] = None,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    q = db.query(Product)
    if search:
        q = q.filter(or_(
            Product.name.ilike(f"%{search}%"),
            Product.sku.ilike(f"%{search}%"),
            Product.brand.ilike(f"%{search}%"),  # ← поиск по марке тоже
        ))
    if category:
        q = q.filter(Product.category == category)
    if low_stock is True:
        q = q.filter(Product.current_stock <= Product.min_stock)
    products = q.order_by(Product.name).all()
    return {"total": len(products), "items": [product_to_dict(p) for p in products]}


@router.get("/categories")
def get_categories(db: Session = Depends(get_db), _: int = Depends(get_current_user)):
    rows = db.query(Product.category).filter(Product.category.isnot(None)).distinct().all()
    return [r[0] for r in rows if r[0]]


@router.get("/brands")
def get_brands(db: Session = Depends(get_db), _: int = Depends(get_current_user)):
    """Список всех марок для фильтрации"""
    rows = db.query(Product.brand).filter(Product.brand.isnot(None)).distinct().all()
    return [r[0] for r in rows if r[0]]


@router.get("/{product_id}")
def get_product(product_id: int, db: Session = Depends(get_db), _: int = Depends(get_current_user)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return product_to_dict(p)


@router.post("")
def create_product(data: ProductCreate, db: Session = Depends(get_db), _: int = Depends(get_current_user)):
    if db.query(Product).filter(Product.sku == data.sku).first():
        raise HTTPException(status_code=400, detail="SKU уже существует")
    if data.unit and data.unit not in UNITS:
        raise HTTPException(status_code=400, detail=f"Единица должна быть одной из: {', '.join(UNITS)}")
    product = Product(**data.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product_to_dict(product)


@router.patch("/{product_id}")
def update_product(product_id: int, data: ProductUpdate, db: Session = Depends(get_db), _: int = Depends(get_current_user)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")
    if data.unit and data.unit not in UNITS:
        raise HTTPException(status_code=400, detail=f"Единица должна быть одной из: {', '.join(UNITS)}")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(p, field, value)
    db.commit()
    db.refresh(p)
    return product_to_dict(p)


@router.delete("/{product_id}")
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")
    active = (
        db.query(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .filter(SaleItem.product_id == product_id, Sale.status == SaleStatus.completed)
        .first()
    )
    if active:
        raise HTTPException(status_code=400, detail="Нельзя удалить товар с активными продажами")
    if p.photo_url:
        path = os.path.join("uploads", p.photo_url.lstrip("/static/"))
        if os.path.exists(path):
            os.remove(path)
    db.delete(p)
    db.commit()
    return {"message": "✅ Товар удалён"}


# ─── Фото ─────────────────────────────────────────────────────────────────────

@router.post("/{product_id}/photo")
async def upload_product_photo(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    require_stock_access(telegram_id, db)
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Только JPEG, PNG или WebP")
    contents = await file.read()
    if len(contents) > MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Файл больше {MAX_SIZE_MB} МБ")
    file_id = await upload_photo_to_telegram(contents, file.filename or "photo.jpg")
    p.photo_url = f"tg:{file_id}"
    db.commit()
    return {"photo_url": f"/media/photo/{file_id}"}


@router.delete("/{product_id}/photo", status_code=204)
def delete_product_photo(
    product_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    require_stock_access(telegram_id, db)
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")
    p.photo_url = None
    db.commit()