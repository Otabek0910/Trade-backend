from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from decimal import Decimal
from typing import Optional
import os, uuid, shutil

from app.db.session import get_db
from app.models.product import Product
from app.models.sale_item import SaleItem
from app.models.sale import Sale
from app.models.receipt import Receipt
from app.models.return_model import Return
from app.models.audit import AuditLog
from app.models.user import User, UserRole
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/products", tags=["Товары"])

UPLOAD_DIR = "uploads/products"
os.makedirs(UPLOAD_DIR, exist_ok=True)


class ProductCreate(BaseModel):
    sku: str
    name: str
    category: Optional[str] = None
    brand: Optional[str] = None
    unit: Optional[str] = "шт"
    unit_value: Optional[float] = None
    supplier_id: Optional[int] = None
    purchase_price: Decimal
    selling_price: Decimal
    min_stock: int = 5
    current_stock: int = 0


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


def product_to_dict(p: Product) -> dict:
    from app.models.supplier import Supplier
    from app.db.session import get_db as _get_db
    return {
        "id": p.id,
        "sku": p.sku,
        "name": p.name,
        "category": p.category,
        "brand": p.brand,
        "unit": p.unit,
        "unit_value": p.unit_value,
        "supplier_id": p.supplier_id,
        "supplier_name": p.supplier.name if p.supplier else None,
        "purchase_price": float(p.purchase_price),
        "selling_price": float(p.selling_price),
        "min_stock": p.min_stock,
        "current_stock": p.current_stock,
        "photo_url": p.photo_url,
        "low_stock": p.current_stock <= p.min_stock,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def get_current_user_obj(telegram_id: int, db: Session) -> User:
    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user


# ─── GET /products ─────────────────────────────────────────────────────────
@router.get("")
def list_products(
    search: Optional[str] = None,
    low_stock: Optional[bool] = None,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    get_current_user_obj(telegram_id, db)
    q = db.query(Product)
    if search:
        like = f"%{search}%"
        q = q.filter(
            Product.name.ilike(like) |
            Product.sku.ilike(like) |
            Product.brand.ilike(like) |
            Product.category.ilike(like)
        )
    if low_stock:
        q = q.filter(Product.current_stock <= Product.min_stock)
    products = q.order_by(Product.name).all()
    return {"items": [product_to_dict(p) for p in products]}


# ─── GET /products/{id} ────────────────────────────────────────────────────
@router.get("/{product_id}")
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    get_current_user_obj(telegram_id, db)
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return product_to_dict(p)


# ─── POST /products ────────────────────────────────────────────────────────
@router.post("")
def create_product(
    data: ProductCreate,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    user = get_current_user_obj(telegram_id, db)
    if user.role not in (UserRole.developer, UserRole.owner_business, UserRole.storekeeper):
        raise HTTPException(status_code=403, detail="Нет доступа")

    exists = db.query(Product).filter(Product.sku == data.sku).first()
    if exists:
        raise HTTPException(status_code=400, detail=f"Артикул «{data.sku}» уже занят")

    p = Product(**data.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return product_to_dict(p)


# ─── PATCH /products/{id} ──────────────────────────────────────────────────
@router.patch("/{product_id}")
def update_product(
    product_id: int,
    data: ProductUpdate,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    user = get_current_user_obj(telegram_id, db)
    if user.role not in (UserRole.developer, UserRole.owner_business, UserRole.storekeeper):
        raise HTTPException(status_code=403, detail="Нет доступа")

    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")

    # storekeeper не может менять цены
    if user.role == UserRole.storekeeper:
        data.purchase_price = None
        data.selling_price = None

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(p, field, value)

    db.commit()
    db.refresh(p)
    return product_to_dict(p)


# ─── DELETE /products/{id} ─────────────────────────────────────────────────
@router.delete("/{product_id}")
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    user = get_current_user_obj(telegram_id, db)
    if user.role not in (UserRole.developer, UserRole.owner_business):
        raise HTTPException(status_code=403, detail="Нет доступа")

    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")

    if user.role == UserRole.developer:
        # Разработчик: полное каскадное удаление всех связанных записей
        db.query(AuditLog).filter(
            AuditLog.entity == "product",
            AuditLog.entity_id == product_id,
        ).delete(synchronize_session=False)

        # Удаляем audit записи связанных продаж
        sale_items = db.query(SaleItem).filter(SaleItem.product_id == product_id).all()
        for si in sale_items:
            db.query(AuditLog).filter(
                AuditLog.entity == "sale",
                AuditLog.entity_id == si.sale_id,
            ).delete(synchronize_session=False)

        # Удаляем возвраты по этому товару
        db.query(Return).filter(Return.product_id == product_id).delete(
            synchronize_session=False
        )

        # Удаляем позиции продаж
        db.query(SaleItem).filter(SaleItem.product_id == product_id).delete(
            synchronize_session=False
        )

        # Удаляем продажи у которых больше нет ни одной позиции
        orphan_sale_ids = [sid for (sid,) in db.query(Sale.id).all()
                          if not db.query(SaleItem).filter(SaleItem.sale_id == sid).first()]
        if orphan_sale_ids:
            db.query(AuditLog).filter(
                AuditLog.entity == "sale",
                AuditLog.entity_id.in_(orphan_sale_ids),
            ).delete(synchronize_session=False)
            db.query(Sale).filter(Sale.id.in_(orphan_sale_ids)).delete(
                synchronize_session=False
            )

        # Удаляем приёмки
        db.query(Receipt).filter(Receipt.product_id == product_id).delete(
            synchronize_session=False
        )

        db.delete(p)
        db.commit()
        return {"deleted": True, "hard": True}
    else:
        # Владелец: удаление только если нет истории продаж
        has_sales = db.query(SaleItem).filter(SaleItem.product_id == product_id).first()
        if has_sales:
            raise HTTPException(
                status_code=400,
                detail="Нельзя удалить товар с историей продаж. Обратитесь к разработчику."
            )
        db.query(Receipt).filter(Receipt.product_id == product_id).delete(
            synchronize_session=False
        )
        db.delete(p)
        db.commit()
        return {"deleted": True, "hard": False}


# ─── POST /products/{id}/photo ─────────────────────────────────────────────
@router.post("/{product_id}/photo")
def upload_photo(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    user = get_current_user_obj(telegram_id, db)
    if user.role not in (UserRole.developer, UserRole.owner_business, UserRole.storekeeper):
        raise HTTPException(status_code=403, detail="Нет доступа")

    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")

    ext = os.path.splitext(file.filename or "")[1] or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Удаляем старое фото
    if p.photo_url:
        old_path = p.photo_url.replace("/static/", "uploads/", 1)
        if os.path.exists(old_path):
            os.remove(old_path)

    p.photo_url = f"/static/products/{filename}"
    db.commit()
    return {"photo_url": p.photo_url}


# ─── DELETE /products/{id}/photo ───────────────────────────────────────────
@router.delete("/{product_id}/photo")
def delete_photo(
    product_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    user = get_current_user_obj(telegram_id, db)
    if user.role not in (UserRole.developer, UserRole.owner_business, UserRole.storekeeper):
        raise HTTPException(status_code=403, detail="Нет доступа")

    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Товар не найден")

    if p.photo_url:
        old_path = p.photo_url.replace("/static/", "uploads/", 1)
        if os.path.exists(old_path):
            os.remove(old_path)
        p.photo_url = None
        db.commit()

    return {"deleted": True}