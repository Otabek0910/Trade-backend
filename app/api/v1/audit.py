"""
app/api/v1/audit.py

Подключить в main.py:
    from app.api.v1.audit import router as audit_router
    app.include_router(audit_router)

Добавить в vite.config.ts прокси:
    '/audit': { target: 'http://127.0.0.1:8000', changeOrigin: true }

Добавить в main.py lifespan (automigrations):
    conn.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS is_reverted BOOLEAN DEFAULT FALSE"))
    conn.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS reverted_at TIMESTAMPTZ"))
    conn.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS reverted_by INTEGER"))
    conn.commit()
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime

from app.db.session import get_db
from app.models.audit import AuditLog
from app.models.user import User, UserRole
from app.models.sale import Sale, SaleStatus
from app.models.sale_item import SaleItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.receipt import Receipt
from app.models.expense import Expense
from app.models.return_model import Return
from app.models.supplier import Supplier
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/audit", tags=["Журнал событий"])

ACTION_LABELS = {
    "create_sale":    "💰 Продажа",
    "create_receipt": "📥 Приёмка",
    "create_expense": "💸 Расход",
    "create_return":  "↩️ Возврат",
}


def require_owner_or_dev(telegram_id: int, db: Session) -> User:
    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user or user.role not in (UserRole.developer, UserRole.owner_business):
        raise HTTPException(status_code=403, detail="Только владелец или разработчик")
    return user


# ─── GET /audit ───────────────────────────────────────────────────────────────

@router.get("")
def list_audit(
    action: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    require_owner_or_dev(telegram_id, db)

    # Join с таблицей users для получения имени
    q = (
        db.query(AuditLog, User.full_name)
        .join(User, AuditLog.user_id == User.id)
    )
    if action:
        q = q.filter(AuditLog.action == action)

    rows = q.order_by(AuditLog.created_at.desc()).limit(limit).all()

    result = []
    for log, user_name in rows:
        # Имя отменившего
        reverted_by_name = None
        if log.reverted_by:
            rev_user = db.query(User).filter(User.id == log.reverted_by).first()
            reverted_by_name = rev_user.full_name if rev_user else None

        # Для возвратов добавляем номер продажи в label
        action_label = ACTION_LABELS.get(log.action, log.action)
        if log.action == "create_return" and log.new_values:
            sale_id = log.new_values.get("sale_id")
            if sale_id:
                action_label = f"↩️ Возврат (из продажи #{sale_id})"

        result.append({
            "id":               log.id,
            "action":           log.action,
            "action_label":     action_label,
            "entity":           log.entity,
            "entity_id":        log.entity_id,
            "user_name":        user_name,
            "new_values":       log.new_values,
            "old_values":       log.old_values,
            "is_reverted":      bool(log.is_reverted),
            "reverted_at":      log.reverted_at.isoformat() if log.reverted_at else None,
            "reverted_by_name": reverted_by_name,
            "created_at":       log.created_at.isoformat() if log.created_at else None,
        })
    return result


# ─── POST /audit/{id}/revert ──────────────────────────────────────────────────

@router.post("/{audit_id}/revert")
def revert_action(
    audit_id: int,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    user = require_owner_or_dev(telegram_id, db)

    entry = db.query(AuditLog).filter(AuditLog.id == audit_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Запись журнала не найдена")
    if entry.is_reverted:
        raise HTTPException(status_code=400, detail="Это действие уже было отменено")

    # Выполняем отмену в зависимости от типа действия
    if entry.action == "create_sale":
        _revert_sale(entry, db)
    elif entry.action == "create_receipt":
        _revert_receipt(entry, db)
    elif entry.action == "create_expense":
        _revert_expense(entry, db)
    elif entry.action == "create_return":
        _revert_return(entry, db)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Отмена «{entry.action}» не поддерживается"
        )

    # Помечаем запись как отменённую
    entry.is_reverted = True
    entry.reverted_at = datetime.now()
    entry.reverted_by = user.id
    db.commit()

    return {"message": "✅ Действие отменено"}


# ─── Revert helpers ───────────────────────────────────────────────────────────

def _revert_sale(entry: AuditLog, db: Session):
    """Отменить продажу: восстановить остатки и долг клиента"""
    sale = db.query(Sale).filter(Sale.id == entry.entity_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Продажа не найдена")
    if sale.status == SaleStatus.cancelled:
        raise HTTPException(status_code=400, detail="Продажа уже отменена")

    # Блокируем отмену если по продаже есть активные (не отменённые) возвраты
    active_returns = db.query(Return).filter(Return.sale_id == sale.id).all()
    if active_returns:
        # Проверяем — есть ли хоть один возврат без записи об отмене
        for ret in active_returns:
            ret_audit = (
                db.query(AuditLog)
                .filter(
                    AuditLog.action == "create_return",
                    AuditLog.entity_id == ret.id,
                    AuditLog.is_reverted == False,
                )
                .first()
            )
            if ret_audit:
                raise HTTPException(
                    status_code=400,
                    detail="Сначала отмените все возвраты по этой продаже в журнале",
                )

    # Восстанавливаем остатки товаров
    # Не учитываем уже возвращённые позиции — их сток уже восстановлен через _revert_return
    already_returned_qty: dict[int, int] = {}
    for ret in active_returns:
        already_returned_qty[ret.product_id] = (
            already_returned_qty.get(ret.product_id, 0) + ret.quantity
        )

    items = db.query(SaleItem).filter(SaleItem.sale_id == sale.id).all()
    for item in items:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if product:
            # Добавляем только то, что ещё не вернули через возвраты
            net_qty = item.quantity - already_returned_qty.get(item.product_id, 0)
            if net_qty > 0:
                product.current_stock += net_qty

    # Восстанавливаем статистику клиента
    if sale.customer_id:
        debt = float(sale.total_amount) - float(sale.paid_amount)
        customer = db.query(Customer).filter(Customer.id == sale.customer_id).first()
        if customer:
            if debt > 0:
                customer.total_debt = max(0.0, float(customer.total_debt or 0) - debt)
            customer.total_purchases = max(
                0.0, float(customer.total_purchases or 0) - float(sale.total_amount)
            )

    sale.status = SaleStatus.cancelled


def _revert_receipt(entry: AuditLog, db: Session):
    """Отменить приёмку: уменьшить остаток, восстановить цену закупки, убрать долг"""
    receipt = db.query(Receipt).filter(Receipt.id == entry.entity_id).first()
    if not receipt:
        raise HTTPException(status_code=404, detail="Приёмка не найдена или уже удалена")

    product = db.query(Product).filter(Product.id == receipt.product_id).first()
    if product:
        new_stock = product.current_stock - receipt.quantity
        if new_stock < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Нельзя отменить: остаток уйдёт в минус "
                       f"(сейчас {product.current_stock} шт, приёмка {receipt.quantity} шт)"
            )
        product.current_stock = new_stock

        # Восстанавливаем цену закупки до приёмки
        if entry.old_values and "purchase_price" in entry.old_values:
            product.purchase_price = entry.old_values["purchase_price"]

    # Восстанавливаем долг поставщику — убираем ту часть что была в долг
    debt = float(getattr(receipt, 'debt', 0) or 0)
    if debt > 0:
        supplier = db.query(Supplier).filter(Supplier.id == receipt.supplier_id).first()
        if supplier:
            supplier.total_debt = max(0.0, float(supplier.total_debt or 0) - debt)

    db.delete(receipt)


def _revert_expense(entry: AuditLog, db: Session):
    """Отменить расход: удалить запись"""
    exp = db.query(Expense).filter(Expense.id == entry.entity_id).first()
    if not exp:
        raise HTTPException(status_code=404, detail="Расход не найден или уже удалён")
    db.delete(exp)


def _revert_return(entry: AuditLog, db: Session):
    """Отменить возврат: убрать товар со склада, восстановить долг и статус продажи"""
    ret = db.query(Return).filter(Return.id == entry.entity_id).first()
    if not ret:
        raise HTTPException(status_code=404, detail="Возврат не найден или уже удалён")

    # Убираем товар обратно со склада
    product = db.query(Product).filter(Product.id == ret.product_id).first()
    if product:
        new_stock = product.current_stock - ret.quantity
        if new_stock < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Нельзя отменить: остаток уйдёт в минус "
                       f"(сейчас {product.current_stock} шт, возврат {ret.quantity} шт)"
            )
        product.current_stock = new_stock

    # Восстанавливаем долг клиента на ту сумму, которую он был уменьшен при возврате
    sale = db.query(Sale).filter(Sale.id == ret.sale_id).first()
    if sale and sale.customer_id:
        customer = db.query(Customer).filter(Customer.id == sale.customer_id).first()
        if customer:
            debt_reduction = float(
                (entry.old_values or {}).get("debt_reduction", 0)
            )
            if debt_reduction > 0:
                customer.total_debt = float(customer.total_debt or 0) + debt_reduction

    # Восстанавливаем статус продажи (был completed до возврата)
    if sale:
        old_status = (entry.old_values or {}).get("sale_status")
        if old_status:
            try:
                sale.status = SaleStatus(old_status)
            except ValueError:
                sale.status = SaleStatus.completed
        elif sale.status == SaleStatus.returned:
            # Если old_values не было (старые записи), ставим completed
            sale.status = SaleStatus.completed

    db.delete(ret)