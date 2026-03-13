"""
app/core/notify.py — отправка Telegram уведомлений подписанным пользователям
"""
import httpx
from datetime import date
from sqlalchemy.orm import Session
from app.core.config import settings

BOT_URL = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

ROLE_EVENTS = {
    "sale":    ["developer", "owner_business", "seller"],
    "receipt": ["developer", "owner_business", "storekeeper"],
    "return":  ["developer", "owner_business", "seller"],
    "daily":   ["developer", "owner_business", "seller", "storekeeper"],
}


def _send(chat_id: int, text: str):
    """Синхронная отправка — работает из обычных FastAPI роутов"""
    try:
        with httpx.Client(timeout=5) as client:
            client.post(f"{BOT_URL}/sendMessage", json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
    except Exception:
        pass


def _get_subscribers(db: Session, event_type: str) -> list:
    from app.models.user import User, UserStatus
    allowed_roles = ROLE_EVENTS.get(event_type, [])
    return db.query(User).filter(
        User.notify == True,
        User.status == UserStatus.active,
        User.role.in_(allowed_roles),
    ).all()


def notify_sale(db: Session, seller_name: str, customer_name: str,
                total: float, items_count: int, sale_id: int):
    subs = _get_subscribers(db, "sale")
    if not subs:
        return
    text = (
        f"💰 <b>Новая продажа #{sale_id}</b>\n"
        f"👤 Продавец: {seller_name}\n"
        f"🛍 Клиент: {customer_name}\n"
        f"📦 Позиций: {items_count}\n"
        f"💵 Сумма: <b>{total:,.0f} сум</b>"
    )
    for u in subs:
        _send(u.telegram_id, text)


def notify_receipt(db: Session, storekeeper_name: str, product_name: str,
                   supplier_name: str, quantity: int, price: float):
    subs = _get_subscribers(db, "receipt")
    if not subs:
        return
    text = (
        f"📥 <b>Приёмка товара</b>\n"
        f"👤 Кладовщик: {storekeeper_name}\n"
        f"📦 Товар: {product_name}\n"
        f"🚚 Поставщик: {supplier_name}\n"
        f"🔢 Количество: {quantity} шт\n"
        f"💵 Цена: {price:,.0f} сум/шт"
    )
    for u in subs:
        _send(u.telegram_id, text)


def notify_return(db: Session, creator_name: str, product_name: str,
                  customer_name: str, quantity: int, amount: float):
    subs = _get_subscribers(db, "return")
    if not subs:
        return
    text = (
        f"↩️ <b>Возврат товара</b>\n"
        f"👤 Оформил: {creator_name}\n"
        f"📦 Товар: {product_name}\n"
        f"🛍 Клиент: {customer_name}\n"
        f"🔢 Количество: {quantity} шт\n"
        f"💵 Сумма возврата: {amount:,.0f} сум"
    )
    for u in subs:
        _send(u.telegram_id, text)


async def send_daily_summary(db: Session):
    """Утренняя сводка — вызывается из APScheduler (async контекст)"""
    from app.models.user import User, UserStatus, UserRole
    from app.models.sale import Sale, SaleStatus
    from app.models.product import Product
    from app.models.customer import Customer
    from app.models.supplier import Supplier
    from sqlalchemy import func, cast, Date

    today = date.today()
    yesterday = date.fromordinal(today.toordinal() - 1)

    subs = db.query(User).filter(
        User.notify == True,
        User.status == UserStatus.active,
    ).all()

    for u in subs:
        if u.role in (UserRole.developer, UserRole.owner_business):
            sales = db.query(Sale).filter(
                cast(Sale.created_at, Date) == yesterday,
                Sale.status == SaleStatus.completed,
            ).all()
            revenue = sum(float(s.total_amount) for s in sales)
            paid = sum(float(s.paid_amount) for s in sales)
            low = db.query(Product).filter(Product.current_stock <= Product.min_stock).count()
            cust_debt = float(db.query(func.sum(Customer.total_debt)).scalar() or 0)
            sup_debt = float(db.query(func.sum(Supplier.total_debt)).scalar() or 0)
            text = (
                f"☀️ <b>Доброе утро! Сводка за вчера</b>\n\n"
                f"💰 Выручка: <b>{revenue:,.0f} сум</b>\n"
                f"✅ Оплачено: {paid:,.0f} сум\n"
                f"⏳ Долг клиентов: {cust_debt:,.0f} сум\n"
                f"🚚 Долг поставщикам: {sup_debt:,.0f} сум\n"
                f"📦 Продаж: {len(sales)}\n"
                f"⚠️ Мало остатка: {low} товаров"
            )
        elif u.role == UserRole.seller:
            my_sales = db.query(Sale).filter(
                cast(Sale.created_at, Date) == yesterday,
                Sale.seller_id == u.id,
                Sale.status == SaleStatus.completed,
            ).all()
            revenue = sum(float(s.total_amount) for s in my_sales)
            text = (
                f"☀️ <b>Доброе утро, {u.full_name}!</b>\n\n"
                f"📊 Ваши продажи за вчера:\n"
                f"🔢 Количество: {len(my_sales)}\n"
                f"💰 Сумма: <b>{revenue:,.0f} сум</b>"
            )
        elif u.role == UserRole.storekeeper:
            low = db.query(Product).filter(Product.current_stock <= Product.min_stock).count()
            low_items = db.query(Product).filter(
                Product.current_stock <= Product.min_stock
            ).limit(5).all()
            items_text = "\n".join(f"  • {p.name}: {p.current_stock}/{p.min_stock}" for p in low_items)
            text = (
                f"☀️ <b>Доброе утро, {u.full_name}!</b>\n\n"
                f"📦 Товаров с низким остатком: {low}\n"
                + (items_text if items_text else "Всё в норме ✅")
            )
        else:
            continue

        _send(u.telegram_id, text)