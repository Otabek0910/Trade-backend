"""
app/api/v1/dashboard.py

Изменения:
- get_period_details() — возвращает top_products, expenses, cash_by_type, recent_returns
  для любого диапазона дат (не жёстко month_start)
- get_dashboard() — каждый period (today/week/month) теперь содержит свои детали
- get_dashboard_history() — history тоже возвращает expenses_by_category + cash_by_type
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, Date, and_
from datetime import date, timedelta

from app.db.session import get_db
from app.models.sale import Sale, SaleStatus, PaymentType
from app.models.sale_item import SaleItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.expense import Expense
from app.models.return_model import Return
from app.models.user import User
from app.models.supplier import Supplier
from typing import Optional
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/dashboard", tags=["Дашборд"])


# ─── Базовая статистика за период ─────────────────────────────────────────────
def get_period_stats(db: Session, date_from: date, date_to: date) -> dict:
    completed_sales = db.query(Sale).filter(
        cast(Sale.created_at, Date) >= date_from,
        cast(Sale.created_at, Date) <= date_to,
        Sale.status == SaleStatus.completed,
    ).all()

    returned_sales = db.query(Sale).filter(
        cast(Sale.created_at, Date) >= date_from,
        cast(Sale.created_at, Date) <= date_to,
        Sale.status == SaleStatus.returned,
    ).all()

    sales_count = len(completed_sales) + len(returned_sales)
    revenue_gross = sum(float(s.total_amount) for s in completed_sales + returned_sales)
    paid = sum(float(s.paid_amount) for s in completed_sales)

    gross_margin = 0.0
    for s in completed_sales + returned_sales:
        for item in s.items:
            gross_margin += (
                float(item.selling_price) - float(item.purchase_price_at_sale)
            ) * item.quantity

    returns_in_period = (
        db.query(Return, SaleItem.purchase_price_at_sale)
        .join(Sale, Return.sale_id == Sale.id)
        .join(SaleItem, and_(
            SaleItem.sale_id == Return.sale_id,
            SaleItem.product_id == Return.product_id,
        ))
        .filter(
            cast(Return.created_at, Date) >= date_from,
            cast(Return.created_at, Date) <= date_to,
            Sale.status.in_([SaleStatus.completed, SaleStatus.returned]),
        )
        .all()
    )

    returns_revenue = sum(float(r.return_amount) for r, _ in returns_in_period)
    returns_margin = sum(
        float(r.return_amount) - float(pp) * r.quantity
        for r, pp in returns_in_period
    )

    revenue = revenue_gross - returns_revenue
    margin = gross_margin - returns_margin
    debt_new = max(0.0, revenue - paid)

    expenses_total = float(
        db.query(func.sum(Expense.amount))
        .filter(Expense.date >= date_from, Expense.date <= date_to)
        .scalar() or 0.0
    )

    net_profit = margin - expenses_total

    return {
        "sales_count": sales_count,
        "revenue": round(revenue, 0),
        "paid": round(paid, 0),
        "debt_new": round(debt_new, 0),
        "margin": round(margin, 0),
        "margin_percent": round(margin / revenue * 100, 1) if revenue > 0 else 0,
        "expenses": round(expenses_total, 0),
        "returns": round(returns_revenue, 0),
        "net_profit": round(net_profit, 0),
    }


# ─── Детали периода (топ товаров, расходы, касса, возвраты) ──────────────────
def get_period_details(db: Session, date_from: date, date_to: date) -> dict:
    """
    Возвращает top_products, expenses_by_category, cash_by_type, recent_returns
    для конкретного диапазона дат. Используется для каждого периода отдельно.
    """
    # ── Топ товаров ──────────────────────────────────────────────────────────
    top_raw = (
        db.query(
            Product.id,
            Product.name,
            Product.brand,
            Product.category,
            Product.unit,
            Product.unit_value,
            func.sum(SaleItem.quantity).label("total_qty"),
            func.sum(SaleItem.selling_price * SaleItem.quantity).label("total_revenue"),
        )
        .join(SaleItem, SaleItem.product_id == Product.id)
        .join(Sale, and_(Sale.id == SaleItem.sale_id, Sale.status == SaleStatus.completed))
        .filter(
            cast(Sale.created_at, Date) >= date_from,
            cast(Sale.created_at, Date) <= date_to,
        )
        .group_by(Product.id, Product.name, Product.brand, Product.category, Product.unit, Product.unit_value)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(10)
        .all()
    )

    # Корректировка топ-товаров на возвраты
    prod_returns: dict[int, dict] = {}
    for pid, qty, amt in (
        db.query(Return.product_id, func.sum(Return.quantity), func.sum(Return.return_amount))
        .join(Sale, Return.sale_id == Sale.id)
        .filter(
            cast(Return.created_at, Date) >= date_from,
            cast(Return.created_at, Date) <= date_to,
            Sale.status == SaleStatus.completed,
        )
        .group_by(Return.product_id)
        .all()
    ):
        prod_returns[pid] = {"qty": int(qty or 0), "amount": float(amt or 0)}

    top_products = []
    for p in top_raw:
        ret = prod_returns.get(p.id, {"qty": 0, "amount": 0.0})
        net_qty = int(p.total_qty) - ret["qty"]
        net_rev = float(p.total_revenue) - ret["amount"]
        if net_qty > 0:
            top_products.append({
                "name": p.name, "brand": p.brand, "category": p.category,
                "total_qty": net_qty,
                "total_revenue": round(net_rev, 0),
                "unit": p.unit or "шт", "unit_value": p.unit_value,
            })
    top_products = sorted(top_products, key=lambda x: -x["total_revenue"])[:8]

    # ── Расходы по категориям ─────────────────────────────────────────────────
    expenses_by_category = [
        {"category": r.category, "total": float(r.total)}
        for r in (
            db.query(Expense.category, func.sum(Expense.amount).label("total"))
            .filter(Expense.date >= date_from, Expense.date <= date_to)
            .group_by(Expense.category)
            .order_by(func.sum(Expense.amount).desc())
            .all()
        )
    ]

    # ── Касса по типам оплаты ─────────────────────────────────────────────────
    cash_raw = (
        db.query(Sale.payment_type, func.sum(Sale.paid_amount), func.count(Sale.id))
        .filter(
            cast(Sale.created_at, Date) >= date_from,
            cast(Sale.created_at, Date) <= date_to,
            Sale.status == SaleStatus.completed,
        )
        .group_by(Sale.payment_type)
        .all()
    )
    cash_by_type: dict = {}
    for ptype, total, count in cash_raw:
        key = ptype.value if hasattr(ptype, 'value') else str(ptype)
        cash_by_type[key] = {"total": round(float(total or 0), 0), "count": int(count or 0)}

    # ── Последние возвраты периода ────────────────────────────────────────────
    recent_returns_raw = (
        db.query(Return, Product.name, Product.brand, Product.category,
                 Product.unit, Product.unit_value, Customer.name)
        .join(Product, Return.product_id == Product.id)
        .join(Sale, Return.sale_id == Sale.id)
        .join(Customer, Sale.customer_id == Customer.id)
        .filter(
            cast(Return.created_at, Date) >= date_from,
            cast(Return.created_at, Date) <= date_to,
        )
        .order_by(Return.created_at.desc())
        .limit(5)
        .all()
    )
    recent_returns = [
        {
            "id": ret.id,
            "product_name": prod_name,
            "product_brand": prod_brand,
            "product_category": prod_category,
            "customer_name": cust_name,
            "quantity": ret.quantity,
            "return_amount": float(ret.return_amount),
            "reason": ret.reason,
            "created_at": ret.created_at.isoformat(),
            "unit": prod_unit or "шт",
            "unit_value": prod_unit_value,
        }
        for ret, prod_name, prod_brand, prod_category, prod_unit, prod_unit_value, cust_name
        in recent_returns_raw
    ]

    return {
        "top_products": top_products,
        "expenses_by_category": expenses_by_category,
        "cash_by_type": cash_by_type,
        "recent_returns": recent_returns,
    }


# ─── Главный дашборд ──────────────────────────────────────────────────────────
@router.get("")
def get_dashboard(
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    # Статистика + детали для каждого периода
    stats_today   = get_period_stats(db, today, today)
    details_today = get_period_details(db, today, today)

    stats_week   = get_period_stats(db, week_start, today)
    details_week = get_period_details(db, week_start, today)

    stats_month   = get_period_stats(db, month_start, today)
    details_month = get_period_details(db, month_start, today)

    # ── Глобальные данные (не зависят от периода) ─────────────────────────────
    top_debtors = (
        db.query(Customer)
        .filter(Customer.total_debt > 0, Customer.is_active.is_(True))
        .order_by(Customer.total_debt.desc())
        .limit(10)
        .all()
    )

    # Продавцы — за текущий месяц
    from sqlalchemy import distinct
    seller_stats_raw = (
        db.query(
            User,
            func.count(distinct(Sale.id)).label("sales_count"),
            func.sum(Sale.total_amount).label("revenue"),
        )
        .join(Sale, Sale.seller_id == User.id)
        .filter(
            cast(Sale.created_at, Date) >= month_start,
            Sale.status == SaleStatus.completed,
        )
        .group_by(User.id)
        .all()
    )

    seller_returns: dict[int, float] = {}
    for uid, amt in (
        db.query(Sale.seller_id, func.sum(Return.return_amount))
        .join(Return, Return.sale_id == Sale.id)
        .filter(cast(Return.created_at, Date) >= month_start)
        .group_by(Sale.seller_id)
        .all()
    ):
        seller_returns[uid] = float(amt or 0)

    seller_net_paid: dict[int, float] = {}
    for uid, paid in (
        db.query(Sale.seller_id, func.sum(Sale.paid_amount))
        .filter(
            cast(Sale.created_at, Date) >= month_start,
            Sale.status == SaleStatus.completed,
        )
        .group_by(Sale.seller_id)
        .all()
    ):
        seller_net_paid[uid] = float(paid or 0)

    # Касса за всё время
    all_sales_raw = db.query(Sale).filter(Sale.status == SaleStatus.completed).all()
    all_returns_map: dict[int, float] = {}
    for sid, amt in (
        db.query(Return.sale_id, func.sum(Return.return_amount))
        .group_by(Return.sale_id)
        .all()
    ):
        all_returns_map[sid] = float(amt or 0)

    cash_alltime = 0.0
    for s in all_sales_raw:
        debt = float(s.total_amount) - float(s.paid_amount)
        total_ret = all_returns_map.get(s.id, 0.0)
        cash_refunded = max(0.0, total_ret - debt)
        cash_alltime += float(s.paid_amount) - cash_refunded

    total_customer_debt = float(db.query(func.sum(Customer.total_debt)).scalar() or 0)
    total_supplier_debt = float(db.query(func.sum(Supplier.total_debt)).scalar() or 0)
    stock_value = float(
        db.query(func.sum(Product.purchase_price * Product.current_stock)).scalar() or 0
    )

    low_stock = (
        db.query(Product)
        .filter(Product.current_stock <= Product.min_stock)
        .order_by(Product.current_stock.asc())
        .limit(5)
        .all()
    )

    returns_month_total = float(
        db.query(func.sum(Return.return_amount))
        .join(Sale, Return.sale_id == Sale.id)
        .filter(
            cast(Return.created_at, Date) >= month_start,
            Sale.status == SaleStatus.completed,
        )
        .scalar() or 0
    )

    return {
        # ── Периоды — каждый содержит stats + details ─────────────────────────
        "today": {**stats_today, **details_today},
        "week":  {**stats_week,  **details_week},
        "month": {**stats_month, **details_month},

        # ── Глобальное (не зависит от периода) ───────────────────────────────
        "top_debtors": [
            {
                "id": c.id, "name": c.name, "phone": c.phone,
                "total_debt": float(c.total_debt),
                "total_purchases": float(getattr(c, 'total_purchases', 0) or 0),
            }
            for c in top_debtors
        ],
        "seller_stats": [
            {
                "name": s.full_name,
                "sales_count": int(s.sales_count),
                "revenue": round(float(s.revenue or 0) - seller_returns.get(s.id, 0), 0),
                "paid": round(seller_net_paid.get(s.id, 0.0), 0),
                "debt": round(
                    float(s.revenue or 0) - seller_returns.get(s.id, 0) - seller_net_paid.get(s.id, 0.0), 0
                ),
            }
            for s in seller_stats_raw
        ],
        "returns_month_total": round(returns_month_total, 0),
        "cash_alltime": round(cash_alltime, 0),
        "total_customer_debt": round(total_customer_debt, 0),
        "total_supplier_debt": round(total_supplier_debt, 0),
        "total_supplier_credit": round(float(db.query(func.sum(Supplier.total_credit)).scalar() or 0), 0),
        "stock_value": round(stock_value, 0),
        "low_stock_count": db.query(Product).filter(Product.current_stock <= Product.min_stock).count(),
        "low_stock_items": [
            {
                "name": p.name, "brand": p.brand, "category": p.category,
                "current_stock": p.current_stock, "min_stock": p.min_stock,
                "unit": p.unit or "шт", "unit_value": p.unit_value,
            }
            for p in low_stock
        ],
    }


# ─── История (год / месяц / всё время) ────────────────────────────────────────
@router.get("/history")
def get_dashboard_history(
    year: int,
    month: Optional[int] = None,   # None = весь год
    alltime: bool = False,          # True = вся история
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """
    GET /dashboard/history?year=2026&month=4   ← апрель 2026
    GET /dashboard/history?year=2026            ← весь 2026 год
    GET /dashboard/history?year=2026&alltime=true ← всё время (year игнорируется)
    """
    from datetime import date as date_type

    if alltime:
        date_from = date_type(2000, 1, 1)
        date_to   = date_type.today()
    elif month:
        import calendar
        last_day  = calendar.monthrange(year, month)[1]
        date_from = date_type(year, month, 1)
        date_to   = date_type(year, month, last_day)
    else:
        date_from = date_type(year, 1, 1)
        date_to   = date_type(year, 12, 31)

    stats   = get_period_stats(db, date_from, date_to)
    details = get_period_details(db, date_from, date_to)

    return {
        **stats,
        **details,
        "period": {
            "type":      "alltime" if alltime else ("month" if month else "year"),
            "year":      year,
            "month":     month,
            "date_from": str(date_from),
            "date_to":   str(date_to),
        },
    }


# ─── Quick stats (для mini-виджета на главной) ────────────────────────────────
@router.get("/quick-stats")
def quick_stats(
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    from app.models.user import UserRole
    today = date.today()
    low_stock = db.query(Product).filter(Product.current_stock <= Product.min_stock).count()

    current_user = db.query(User).filter(User.telegram_id == telegram_id).first()
    role = current_user.role if current_user else None

    if role in (UserRole.owner_business, UserRole.developer):
        stats = get_period_stats(db, today, today)
        total_debt = float(db.query(func.sum(Customer.total_debt)).scalar() or 0)
        return {
            "today_revenue": stats["revenue"],
            "today_sales":   stats["sales_count"],
            "total_debt":    round(total_debt, 0),
            "low_stock_count": low_stock,
        }

    if role == UserRole.seller and current_user:
        my_sales = db.query(Sale).filter(
            cast(Sale.created_at, Date) == today,
            Sale.seller_id == current_user.id,
            Sale.status == SaleStatus.completed,
        ).all()
        my_revenue = sum(float(s.total_amount) for s in my_sales)
        my_debt    = sum(max(0.0, float(s.total_amount) - float(s.paid_amount)) for s in my_sales)
        return {
            "today_revenue": round(my_revenue, 0),
            "today_sales":   len(my_sales),
            "total_debt":    round(my_debt, 0),
            "low_stock_count": low_stock,
        }

    return {
        "today_revenue": 0,
        "today_sales":   0,
        "total_debt":    0,
        "low_stock_count": low_stock,
    }


# ─── История продаж (список) ──────────────────────────────────────────────────
@router.get("/sales-history")
def sales_history(
    limit:  int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    sales = (
        db.query(Sale)
        .order_by(Sale.created_at.desc())
        .offset(offset).limit(limit).all()
    )
    total = db.query(func.count(Sale.id)).scalar()
    return {
        "total": total,
        "items": [
            {
                "id": s.id,
                "customer":        s.customer.name if s.customer else "Розница",
                "seller":          s.seller.full_name if s.seller else "—",
                "total_amount":    float(s.total_amount),
                "paid_amount":     float(s.paid_amount),
                "debt":            float(s.total_amount - s.paid_amount),
                "payment_type":    s.payment_type.value,
                "discount_percent":float(s.discount_percent),
                "status":          s.status.value,
                "items_count":     len(s.items),
                "items": [
                    {
                        "product_name": item.product.name if item.product else "—",
                        "quantity":     item.quantity,
                        "selling_price":float(item.selling_price),
                    }
                    for item in s.items
                ],
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in sales
        ],
    }