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
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/dashboard", tags=["Дашборд"])


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


@router.get("")
def get_dashboard(
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    stats_today = get_period_stats(db, today, today)
    stats_week = get_period_stats(db, week_start, today)
    stats_month = get_period_stats(db, month_start, today)

    returns_month_total = float(
        db.query(func.sum(Return.return_amount))
        .join(Sale, Return.sale_id == Sale.id)
        .filter(
            cast(Return.created_at, Date) >= month_start,
            Sale.status == SaleStatus.completed,
        )
        .scalar() or 0
    )

    # ── Топ товаров за месяц — ДОБАВЛЕНЫ unit, unit_value ──
    top_products_raw = (
        db.query(
            Product.id,
            Product.name,
            Product.unit,
            Product.unit_value,
            func.sum(SaleItem.quantity).label("total_qty"),
            func.sum(SaleItem.selling_price * SaleItem.quantity).label("total_revenue"),
        )
        .join(SaleItem, SaleItem.product_id == Product.id)
        .join(Sale, and_(Sale.id == SaleItem.sale_id, Sale.status == SaleStatus.completed))
        .filter(cast(Sale.created_at, Date) >= month_start)
        .group_by(Product.id, Product.name, Product.unit, Product.unit_value)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(10)
        .all()
    )

    product_returns: dict[int, dict] = {}
    for pid, qty, amt in (
        db.query(Return.product_id, func.sum(Return.quantity), func.sum(Return.return_amount))
        .join(Sale, Return.sale_id == Sale.id)
        .filter(
            cast(Return.created_at, Date) >= month_start,
            Sale.status == SaleStatus.completed,
        )
        .group_by(Return.product_id)
        .all()
    ):
        product_returns[pid] = {"qty": int(qty or 0), "amount": float(amt or 0)}

    top_products_adj = []
    for p in top_products_raw:
        ret = product_returns.get(p.id, {"qty": 0, "amount": 0.0})
        net_qty = int(p.total_qty) - ret["qty"]
        net_rev = float(p.total_revenue) - ret["amount"]
        if net_qty > 0:
            top_products_adj.append({
                "name": p.name,
                "total_qty": net_qty,
                "total_revenue": round(net_rev, 0),
                "unit": p.unit or "шт",
                "unit_value": p.unit_value,
            })
    top_products_adj = sorted(top_products_adj, key=lambda x: -x["total_qty"])[:5]

    top_debtors = (
        db.query(Customer)
        .filter(Customer.total_debt > 0)
        .order_by(Customer.total_debt.desc())
        .limit(5)
        .all()
    )

    seller_stats_raw = (
        db.query(
            User.id.label("seller_id"),
            User.full_name,
            func.count(Sale.id).label("sales_count"),
            func.sum(Sale.total_amount).label("revenue"),
            func.sum(Sale.paid_amount).label("paid"),
        )
        .join(User, Sale.seller_id == User.id)
        .filter(
            cast(Sale.created_at, Date) >= month_start,
            Sale.status == SaleStatus.completed,
        )
        .group_by(User.id, User.full_name)
        .order_by(func.sum(Sale.total_amount).desc())
        .all()
    )

    seller_returns: dict[int, float] = {}
    seller_net_paid: dict[int, float] = {}

    seller_sales_raw = (
        db.query(Sale.id, Sale.seller_id, Sale.paid_amount, Sale.total_amount)
        .filter(
            cast(Sale.created_at, Date) >= month_start,
            Sale.status == SaleStatus.completed,
        )
        .all()
    )
    seller_sale_ids = [s.id for s in seller_sales_raw]

    seller_sale_returns: dict[int, float] = {}
    if seller_sale_ids:
        for sid, amt in (
            db.query(Return.sale_id, func.sum(Return.return_amount))
            .filter(Return.sale_id.in_(seller_sale_ids))
            .group_by(Return.sale_id)
            .all()
        ):
            seller_sale_returns[sid] = float(amt or 0)

    for s in seller_sales_raw:
        debt = float(s.total_amount) - float(s.paid_amount)
        total_ret = seller_sale_returns.get(s.id, 0.0)
        cash_refunded = max(0.0, total_ret - debt)
        net = float(s.paid_amount) - cash_refunded
        seller_net_paid[s.seller_id] = seller_net_paid.get(s.seller_id, 0.0) + net
        seller_returns[s.seller_id] = seller_returns.get(s.seller_id, 0.0) + total_ret

    month_sales_raw = (
        db.query(Sale.id, Sale.payment_type, Sale.paid_amount, Sale.total_amount)
        .filter(
            cast(Sale.created_at, Date) >= month_start,
            Sale.status.in_([SaleStatus.completed, SaleStatus.returned]),
        )
        .all()
    )

    month_sale_ids = [s.id for s in month_sales_raw]
    sale_returns_map: dict[int, float] = {}
    if month_sale_ids:
        for sid, amt in (
            db.query(Return.sale_id, func.sum(Return.return_amount))
            .filter(Return.sale_id.in_(month_sale_ids))
            .group_by(Return.sale_id)
            .all()
        ):
            sale_returns_map[sid] = float(amt or 0)

    cash_by_type_data: dict[str, dict] = {}
    for s in month_sales_raw:
        debt = float(s.total_amount) - float(s.paid_amount)
        total_ret = sale_returns_map.get(s.id, 0.0)
        cash_refunded = max(0.0, total_ret - debt)
        net = float(s.paid_amount) - cash_refunded
        pt = s.payment_type.value
        if pt not in cash_by_type_data:
            cash_by_type_data[pt] = {"total": 0.0, "count": 0}
        cash_by_type_data[pt]["total"] += net
        cash_by_type_data[pt]["count"] += 1

    cash_by_type_data = {
        k: {"total": round(v["total"], 0), "count": v["count"]}
        for k, v in cash_by_type_data.items()
        if v["total"] != 0 or v["count"] > 0
    }

    all_sales_raw = (
        db.query(Sale.id, Sale.paid_amount, Sale.total_amount)
        .filter(Sale.status.in_([SaleStatus.completed, SaleStatus.returned]))
        .all()
    )
    all_sale_ids = [s.id for s in all_sales_raw]
    all_returns_map: dict[int, float] = {}
    if all_sale_ids:
        for sid, amt in (
            db.query(Return.sale_id, func.sum(Return.return_amount))
            .filter(Return.sale_id.in_(all_sale_ids))
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
    stock_value = float(
        db.query(func.sum(Product.purchase_price * Product.current_stock)).scalar() or 0
    )

    # ── Товары с низким остатком — ДОБАВЛЕНЫ unit, unit_value ──
    low_stock = (
        db.query(Product)
        .filter(Product.current_stock <= Product.min_stock)
        .order_by(Product.current_stock.asc())
        .limit(5)
        .all()
    )

    expenses_by_category = (
        db.query(Expense.category, func.sum(Expense.amount).label("total"))
        .filter(Expense.date >= month_start)
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .all()
    )

    # ── Последние возвраты — ДОБАВЛЕНЫ unit, unit_value ──
    recent_returns = (
        db.query(Return, Product.name, Product.unit, Product.unit_value, Customer.name)
        .join(Product, Return.product_id == Product.id)
        .join(Sale, Return.sale_id == Sale.id)
        .join(Customer, Sale.customer_id == Customer.id)
        .order_by(Return.created_at.desc())
        .limit(5)
        .all()
    )

    return {
        "today": stats_today,
        "week": stats_week,
        "month": stats_month,
        "top_products": top_products_adj,
        "top_debtors": [
            {
                "id": c.id,
                "name": c.name,
                "phone": c.phone,
                "total_debt": float(c.total_debt),
                "total_purchases": float(c.total_purchases or 0),
            }
            for c in top_debtors
        ],
        "recent_sales": [],
        "seller_stats": [
            {
                "name": s.full_name,
                "sales_count": int(s.sales_count),
                "revenue": round(float(s.revenue or 0) - seller_returns.get(s.seller_id, 0), 0),
                "paid": round(seller_net_paid.get(s.seller_id, 0.0), 0),
                "debt": round(
                    float(s.revenue or 0) - seller_returns.get(s.seller_id, 0) - seller_net_paid.get(s.seller_id, 0.0), 0
                ),
            }
            for s in seller_stats_raw
        ],
        "cash_by_type": {
            k: {"total": v["total"], "count": v["count"]}
            for k, v in cash_by_type_data.items()
        },
        "returns_month_total": round(returns_month_total, 0),
        "cash_alltime": round(cash_alltime, 0),
        "total_customer_debt": round(total_customer_debt, 0),
        "stock_value": round(stock_value, 0),
        "low_stock_count": db.query(Product).filter(Product.current_stock <= Product.min_stock).count(),
        "low_stock_items": [
            {
                "name": p.name,
                "current_stock": p.current_stock,
                "min_stock": p.min_stock,
                "unit": p.unit or "шт",
                "unit_value": p.unit_value,
            }
            for p in low_stock
        ],
        "expenses_by_category": [
            {"category": r.category, "total": float(r.total)}
            for r in expenses_by_category
        ],
        "recent_returns": [
            {
                "id": ret.id,
                "product_name": prod_name,
                "customer_name": cust_name,
                "quantity": ret.quantity,
                "return_amount": float(ret.return_amount),
                "reason": ret.reason,
                "created_at": ret.created_at.isoformat(),
                "unit": prod_unit or "шт",
                "unit_value": prod_unit_value,
            }
            for ret, prod_name, prod_unit, prod_unit_value, cust_name in recent_returns
        ],
    }


@router.get("/quick-stats")
def quick_stats(
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    today = date.today()
    stats = get_period_stats(db, today, today)
    total_debt = float(db.query(func.sum(Customer.total_debt)).scalar() or 0)
    low_stock = db.query(Product).filter(Product.current_stock <= Product.min_stock).count()
    return {
        "today_revenue": stats["revenue"],
        "today_sales": stats["sales_count"],
        "total_debt": round(total_debt, 0),
        "low_stock_count": low_stock,
    }


@router.get("/sales-history")
def sales_history(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    sales = (
        db.query(Sale)
        .order_by(Sale.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    total = db.query(func.count(Sale.id)).scalar()
    return {
        "total": total,
        "items": [
            {
                "id": s.id,
                "customer": s.customer.name if s.customer else "Розница",
                "seller": s.seller.full_name if s.seller else "—",
                "total_amount": float(s.total_amount),
                "paid_amount": float(s.paid_amount),
                "debt": float(s.total_amount - s.paid_amount),
                "payment_type": s.payment_type.value,
                "discount_percent": float(s.discount_percent),
                "status": s.status.value,
                "items_count": len(s.items),
                "items": [
                    {
                        "product_name": item.product.name if item.product else "—",
                        "quantity": item.quantity,
                        "selling_price": float(item.selling_price),
                    }
                    for item in s.items
                ],
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in sales
        ],
    }