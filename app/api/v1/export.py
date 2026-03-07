from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import cast, Date, text
from datetime import date, timedelta
from io import BytesIO
import httpx
import subprocess
import os
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from app.db.session import get_db
from app.models.sale import Sale, SaleStatus
from app.models.sale_item import SaleItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.supplier import Supplier
from app.models.user import User, UserRole
from app.models.expense import Expense
from app.models.return_model import Return
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/export", tags=["Экспорт"])

# ─── Стили ───────────────────────────────────────────────────────

HEADER_FILL = PatternFill("solid", start_color="1A4B8C")
HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial", size=10)
SUBHEADER_FILL = PatternFill("solid", start_color="E8F0FE")
SUBHEADER_FONT = Font(bold=True, color="1A4B8C", name="Arial", size=10)
NORMAL_FONT = Font(name="Arial", size=10)
RED_FONT = Font(name="Arial", size=10, color="C0392B", bold=True)
GREEN_FONT = Font(name="Arial", size=10, color="1A6B3C", bold=True)
WARN_FILL = PatternFill("solid", start_color="FFF3CD")
RED_FILL = PatternFill("solid", start_color="FDECEA")
CENTER = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")

def thin_border():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)

def header_row(ws, row, values, widths=None):
    for col, val in enumerate(values, 1):
        cell = ws.cell(row=row, column=col, value=val)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = thin_border()
    if widths:
        for col, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(col)].width = w

def data_cell(ws, row, col, value, number_format=None, font=None, fill=None, align=None):
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = font or NORMAL_FONT
    cell.border = thin_border()
    cell.alignment = align or LEFT
    if number_format:
        cell.number_format = number_format
    if fill:
        cell.fill = fill
    return cell


# ─── Лист 1: Продажи ─────────────────────────────────────────────

def build_sales_sheet(ws, db: Session, date_from: date, date_to: date):
    ws.title = "Продажи"
    ws.freeze_panes = "A3"

    # Заголовок
    ws.merge_cells("A1:I1")
    title = ws["A1"]
    title.value = f"📦 Продажи за период {date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}"
    title.font = Font(bold=True, size=13, name="Arial", color="1A4B8C")
    title.alignment = CENTER
    ws.row_dimensions[1].height = 28

    cols = ["№", "Дата", "Статус", "Клиент", "Товар", "Кол-во", "Цена продажи", "Сумма", "Оплачено", "Долг"]
    widths = [5, 16, 12, 22, 30, 8, 16, 16, 16, 14]
    header_row(ws, 2, cols, widths)
    ws.merge_cells("A1:J1")
    ws.row_dimensions[2].height = 20

    sales = (
        db.query(Sale)
        .filter(
            cast(Sale.created_at, Date) >= date_from,
            cast(Sale.created_at, Date) <= date_to,
            Sale.status.in_([SaleStatus.completed, SaleStatus.returned]),
        )
        .order_by(Sale.created_at.desc())
        .all()
    )

    r = 3
    total_revenue = total_paid = total_debt = 0

    for sale in sales:
        is_returned = sale.status == SaleStatus.returned
        for item in sale.items:
            debt = float(sale.total_amount) - float(sale.paid_amount)
            item_debt = debt / len(sale.items) if sale.items else 0

            data_cell(ws, r, 1, sale.id, align=CENTER)
            data_cell(ws, r, 2, sale.created_at.strftime("%d.%m.%Y %H:%M") if sale.created_at else "")
            status_cell = data_cell(ws, r, 3, "↩️ Возврат" if is_returned else "✅ Продажа")
            if is_returned:
                status_cell.font = Font(name="Arial", size=10, color="E08030", bold=True)
            data_cell(ws, r, 4, sale.customer.name if sale.customer else "Розница")
            data_cell(ws, r, 5, item.product.name if item.product else "—")
            data_cell(ws, r, 6, item.quantity, align=CENTER)
            data_cell(ws, r, 7, float(item.selling_price), number_format='#,##0', align=RIGHT)
            item_total = float(item.selling_price) * item.quantity
            data_cell(ws, r, 8, item_total, number_format='#,##0', align=RIGHT,
                      font=Font(name="Arial", size=10, color="888888") if is_returned else GREEN_FONT)
            data_cell(ws, r, 9, float(sale.paid_amount), number_format='#,##0', align=RIGHT)

            debt_cell = data_cell(ws, r, 10, item_debt if item_debt > 0 else 0, number_format='#,##0', align=RIGHT)
            if item_debt > 0:
                debt_cell.font = RED_FONT
            ws.row_dimensions[r].height = 18

            if not is_returned:
                total_revenue += item_total
            r += 1

        if not is_returned:
            total_paid += float(sale.paid_amount)

    total_debt = total_revenue - total_paid

    # Итоги
    ws.row_dimensions[r].height = 22
    for col in range(1, 10):
        ws.cell(row=r, column=col).fill = SUBHEADER_FILL
        ws.cell(row=r, column=col).border = thin_border()

    ws.cell(row=r, column=1).value = "ИТОГО"
    ws.cell(row=r, column=1).font = SUBHEADER_FONT
    ws.cell(row=r, column=1).alignment = CENTER
    ws.merge_cells(f"A{r}:G{r}")

    data_cell(ws, r, 8, total_revenue, number_format='#,##0', align=RIGHT,
              font=Font(bold=True, name="Arial", size=10, color="1A6B3C"), fill=SUBHEADER_FILL)
    data_cell(ws, r, 9, total_paid, number_format='#,##0', align=RIGHT,
              font=Font(bold=True, name="Arial", size=10), fill=SUBHEADER_FILL)
    data_cell(ws, r, 10, total_debt, number_format='#,##0', align=RIGHT,
              font=Font(bold=True, name="Arial", size=10, color="C0392B") if total_debt > 0 else Font(bold=True, name="Arial", size=10),
              fill=SUBHEADER_FILL)


# ─── Лист 2: Остатки ─────────────────────────────────────────────

def build_stock_sheet(ws, db: Session):
    ws.title = "Остатки"
    ws.freeze_panes = "A3"

    ws.merge_cells("A1:H1")
    title = ws["A1"]
    title.value = f"📦 Остатки на складе по состоянию на {date.today().strftime('%d.%m.%Y')}"
    title.font = Font(bold=True, size=13, name="Arial", color="1A6B3C")
    title.alignment = CENTER
    ws.row_dimensions[1].height = 28

    cols = ["SKU", "Название", "Категория", "Поставщик", "Цена закупки", "Цена продажи", "Маржа %", "Остаток"]
    widths = [14, 32, 18, 22, 16, 16, 10, 10]
    header_row(ws, 2, cols, widths)
    ws.row_dimensions[2].height = 20

    products = db.query(Product).order_by(Product.name).all()

    r = 3
    for p in products:
        margin = round((float(p.selling_price) - float(p.purchase_price)) / float(p.purchase_price) * 100, 1) if float(p.purchase_price) > 0 else 0
        low = p.current_stock <= p.min_stock
        row_fill = RED_FILL if low else None

        data_cell(ws, r, 1, p.sku, fill=row_fill)
        data_cell(ws, r, 2, p.name, fill=row_fill)
        data_cell(ws, r, 3, p.category or "—", fill=row_fill)
        data_cell(ws, r, 4, p.supplier.name if p.supplier else "—", fill=row_fill)
        data_cell(ws, r, 5, float(p.purchase_price), number_format='#,##0', align=RIGHT, fill=row_fill)
        data_cell(ws, r, 6, float(p.selling_price), number_format='#,##0', align=RIGHT, fill=row_fill)
        data_cell(ws, r, 7, margin, number_format='0.0"%"', align=CENTER,
                  font=GREEN_FONT if margin >= 20 else NORMAL_FONT, fill=row_fill)
        stock_cell = data_cell(ws, r, 8, p.current_stock, align=CENTER, fill=row_fill)
        if low:
            stock_cell.font = RED_FONT
        ws.row_dimensions[r].height = 18
        r += 1

    # Итог
    ws.row_dimensions[r].height = 22
    ws.cell(row=r, column=1).value = "ИТОГО"
    ws.cell(row=r, column=1).font = SUBHEADER_FONT
    ws.cell(row=r, column=1).alignment = CENTER
    ws.merge_cells(f"A{r}:G{r}")
    for col in range(1, 9):
        ws.cell(row=r, column=col).fill = SUBHEADER_FILL
        ws.cell(row=r, column=col).border = thin_border()
    ws.cell(row=r, column=8).value = f'=SUM(H3:H{r-1})'
    ws.cell(row=r, column=8).font = SUBHEADER_FONT
    ws.cell(row=r, column=8).alignment = CENTER


# ─── Лист 3: Долги ───────────────────────────────────────────────

def build_debts_sheet(ws, db: Session):
    ws.title = "Долги клиентов"
    ws.freeze_panes = "A3"

    ws.merge_cells("A1:F1")
    title = ws["A1"]
    title.value = f"⏳ Долги клиентов на {date.today().strftime('%d.%m.%Y')}"
    title.font = Font(bold=True, size=13, name="Arial", color="C0392B")
    title.alignment = CENTER
    ws.row_dimensions[1].height = 28

    cols = ["Клиент", "Телефон", "Адрес", "Всего покупок", "Долг", "% от покупок"]
    widths = [28, 18, 24, 18, 18, 14]
    header_row(ws, 2, cols, widths)
    ws.row_dimensions[2].height = 20

    debtors = (
        db.query(Customer)
        .filter(Customer.total_debt > 0)
        .order_by(Customer.total_debt.desc())
        .all()
    )

    r = 3
    for c in debtors:
        purchases = float(c.total_purchases or 0)
        debt = float(c.total_debt)
        debt_pct = round(debt / purchases * 100, 1) if purchases > 0 else 0

        data_cell(ws, r, 1, c.name, fill=RED_FILL if debt > 1_000_000 else None)
        data_cell(ws, r, 2, c.phone)
        data_cell(ws, r, 3, c.address or "—")
        data_cell(ws, r, 4, purchases, number_format='#,##0', align=RIGHT)
        data_cell(ws, r, 5, debt, number_format='#,##0', align=RIGHT, font=RED_FONT)
        data_cell(ws, r, 6, debt_pct, number_format='0.0"%"', align=CENTER,
                  font=RED_FONT if debt_pct > 50 else NORMAL_FONT)
        ws.row_dimensions[r].height = 18
        r += 1

    if r == 3:
        ws.merge_cells(f"A3:F3")
        ws.cell(row=3, column=1).value = "✅ Долгов нет"
        ws.cell(row=3, column=1).font = Font(name="Arial", size=11, color="1A6B3C", bold=True)
        ws.cell(row=3, column=1).alignment = CENTER
        r = 4

    # Итог
    ws.row_dimensions[r].height = 22
    for col in range(1, 7):
        ws.cell(row=r, column=col).fill = SUBHEADER_FILL
        ws.cell(row=r, column=col).border = thin_border()
    ws.cell(row=r, column=1).value = "ИТОГО ДОЛГ"
    ws.cell(row=r, column=1).font = SUBHEADER_FONT
    ws.cell(row=r, column=1).alignment = CENTER
    ws.merge_cells(f"A{r}:D{r}")
    ws.cell(row=r, column=5).value = f'=SUM(E3:E{r-1})'
    ws.cell(row=r, column=5).font = Font(bold=True, name="Arial", size=10, color="C0392B")
    ws.cell(row=r, column=5).alignment = RIGHT
    ws.cell(row=r, column=5).number_format = '#,##0'
    ws.cell(row=r, column=5).border = thin_border()


# ─── Лист 4: Расходы ─────────────────────────────────────────────

def build_expenses_sheet(ws, db: Session, date_from: date, date_to: date):
    ws.title = "Расходы"
    ws.freeze_panes = "A3"

    ws.merge_cells("A1:E1")
    title = ws["A1"]
    title.value = f"💸 Расходы за период {date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}"
    title.font = Font(bold=True, size=13, name="Arial", color="C0392B")
    title.alignment = CENTER
    ws.row_dimensions[1].height = 28

    cols = ["Дата", "Категория", "Описание", "Сумма", "Добавил"]
    widths = [16, 20, 36, 16, 22]
    header_row(ws, 2, cols, widths)
    ws.row_dimensions[2].height = 20

    expenses = (
        db.query(Expense)
        .filter(Expense.date >= date_from, Expense.date <= date_to)
        .order_by(Expense.date.desc())
        .all()
    )

    r = 3
    total = 0.0
    for e in expenses:
        data_cell(ws, r, 1, e.date.strftime("%d.%m.%Y") if e.date else "")
        data_cell(ws, r, 2, e.category or "—")
        data_cell(ws, r, 3, e.description or "—")
        data_cell(ws, r, 4, float(e.amount), number_format='#,##0', align=RIGHT, font=RED_FONT)
        data_cell(ws, r, 5, e.created_by or "—")
        ws.row_dimensions[r].height = 18
        total += float(e.amount)
        r += 1

    if r == 3:
        ws.merge_cells("A3:E3")
        ws.cell(row=3, column=1).value = "Расходов нет"
        ws.cell(row=3, column=1).font = Font(name="Arial", size=11, color="888888")
        ws.cell(row=3, column=1).alignment = CENTER
        r = 4

    ws.row_dimensions[r].height = 22
    for col in range(1, 6):
        ws.cell(row=r, column=col).fill = SUBHEADER_FILL
        ws.cell(row=r, column=col).border = thin_border()
    ws.cell(row=r, column=1).value = "ИТОГО"
    ws.cell(row=r, column=1).font = SUBHEADER_FONT
    ws.cell(row=r, column=1).alignment = CENTER
    ws.merge_cells(f"A{r}:C{r}")
    data_cell(ws, r, 4, total, number_format='#,##0', align=RIGHT,
              font=Font(bold=True, name="Arial", size=10, color="C0392B"), fill=SUBHEADER_FILL)


# ─── Лист 5: Возвраты ────────────────────────────────────────────

def build_returns_sheet(ws, db: Session, date_from: date, date_to: date):
    ws.title = "Возвраты"
    ws.freeze_panes = "A3"

    ws.merge_cells("A1:G1")
    title = ws["A1"]
    title.value = f"↩️ Возвраты за период {date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}"
    title.font = Font(bold=True, size=13, name="Arial", color="E08030")
    title.alignment = CENTER
    ws.row_dimensions[1].height = 28

    cols = ["Дата", "Клиент", "Товар", "Кол-во", "Сумма возврата", "Причина", "№ продажи"]
    widths = [16, 22, 30, 8, 18, 30, 12]
    header_row(ws, 2, cols, widths)
    ws.row_dimensions[2].height = 20

    returns = (
        db.query(Return)
        .join(Sale, Return.sale_id == Sale.id)
        .filter(cast(Return.created_at, Date) >= date_from,
                cast(Return.created_at, Date) <= date_to)
        .order_by(Return.created_at.desc())
        .all()
    )

    r = 3
    total = 0.0
    for ret in returns:
        sale = db.query(Sale).filter(Sale.id == ret.sale_id).first()
        product = db.query(Product).filter(Product.id == ret.product_id).first()
        customer_name = sale.customer.name if sale and sale.customer else "Розница"

        data_cell(ws, r, 1, ret.created_at.strftime("%d.%m.%Y %H:%M") if ret.created_at else "")
        data_cell(ws, r, 2, customer_name)
        data_cell(ws, r, 3, product.name if product else "—")
        data_cell(ws, r, 4, ret.quantity, align=CENTER)
        data_cell(ws, r, 5, float(ret.return_amount), number_format='#,##0', align=RIGHT,
                  font=Font(name="Arial", size=10, color="E08030", bold=True))
        data_cell(ws, r, 6, ret.reason or "—")
        data_cell(ws, r, 7, ret.sale_id, align=CENTER)
        ws.row_dimensions[r].height = 18
        total += float(ret.return_amount)
        r += 1

    if r == 3:
        ws.merge_cells("A3:G3")
        ws.cell(row=3, column=1).value = "Возвратов нет"
        ws.cell(row=3, column=1).font = Font(name="Arial", size=11, color="888888")
        ws.cell(row=3, column=1).alignment = CENTER
        r = 4

    ws.row_dimensions[r].height = 22
    for col in range(1, 8):
        ws.cell(row=r, column=col).fill = SUBHEADER_FILL
        ws.cell(row=r, column=col).border = thin_border()
    ws.cell(row=r, column=1).value = "ИТОГО ВОЗВРАТОВ"
    ws.cell(row=r, column=1).font = SUBHEADER_FONT
    ws.cell(row=r, column=1).alignment = CENTER
    ws.merge_cells(f"A{r}:D{r}")
    data_cell(ws, r, 5, total, number_format='#,##0', align=RIGHT,
              font=Font(bold=True, name="Arial", size=10, color="E08030"), fill=SUBHEADER_FILL)


# ─── Endpoint ────────────────────────────────────────────────────

@router.get("")
def export_excel(
    days: int = 30,
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    date_to = date.today()
    date_from = date_to - timedelta(days=days - 1)

    wb = openpyxl.Workbook()
    ws1 = wb.active
    build_sales_sheet(ws1, db, date_from, date_to)

    ws2 = wb.create_sheet()
    build_stock_sheet(ws2, db)

    ws3 = wb.create_sheet()
    build_debts_sheet(ws3, db)

    ws4 = wb.create_sheet()
    build_expenses_sheet(ws4, db, date_from, date_to)

    ws5 = wb.create_sheet()
    build_returns_sheet(ws5, db, date_from, date_to)

    # Сохраняем в память
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"trade_report_{date_to.strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/send")
def send_to_telegram(
    days: int = 30,
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Отправляет Excel файл в чат пользователя через бота"""
    from app.core.config import settings

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    date_to = date.today()
    date_from = date_to - timedelta(days=days - 1)

    wb = openpyxl.Workbook()
    ws1 = wb.active
    build_sales_sheet(ws1, db, date_from, date_to)
    ws2 = wb.create_sheet()
    build_stock_sheet(ws2, db)
    ws3 = wb.create_sheet()
    build_debts_sheet(ws3, db)
    ws4 = wb.create_sheet()
    build_expenses_sheet(ws4, db, date_from, date_to)
    ws5 = wb.create_sheet()
    build_returns_sheet(ws5, db, date_from, date_to)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"trade_report_{date_to.strftime('%Y%m%d')}.xlsx"

    # Отправляем через Bot API (httpx — уже есть в FastAPI)
    from app.core.config import settings
    bot_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendDocument"

    with httpx.Client(timeout=30, verify=False) as client:
        response = client.post(
            bot_url,
            data={
                "chat_id": str(telegram_id),
                "caption": f"📊 Отчёт за {days} дней\n📅 {date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}\n\n📋 Листы: Продажи · Остатки · Долги · Расходы · Возвраты",
            },
            files={"document": (filename, buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

    tg_result = response.json()
    if not tg_result.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка Telegram: {tg_result.get('description', 'неизвестная ошибка')}"
        )

    return {"message": "✅ Файл отправлен в ваш Telegram!"}

@router.get("/db-backup")
def db_backup(
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Создаёт дамп всей БД через Python (без pg_dump) и отдаёт .sql файл"""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user or user.role not in (UserRole.developer, UserRole.owner_business):
        raise HTTPException(status_code=403, detail="Только владелец или разработчик")

    from app.core.config import settings
    import psycopg2
    from urllib.parse import urlparse

    try:
        parsed = urlparse(settings.DATABASE_URL)
        conn = psycopg2.connect(
            dbname=parsed.path.lstrip("/"),
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname,
            port=parsed.port or 5432,
        )
        conn.autocommit = True
        cur = conn.cursor()

        lines = []
        lines.append("-- Tradi backup\n")
        lines.append("-- Generated by Python psycopg2\n\n")
        lines.append("SET client_encoding = 'UTF8';\n")
        lines.append("SET standard_conforming_strings = on;\n\n")

        # Получаем все таблицы
        cur.execute("""
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename
        """)
        tables = [r[0] for r in cur.fetchall()]

        for table in tables:
            lines.append(f"\n-- Table: {table}\n")
            lines.append(f"DELETE FROM {table};\n")

            cur.execute(f"SELECT * FROM {table}")
            rows = cur.fetchall()
            if not rows:
                continue

            cols = [desc[0] for desc in cur.description]
            cols_str = ", ".join(cols)

            for row in rows:
                values = []
                for v in row:
                    if v is None:
                        values.append("NULL")
                    elif isinstance(v, bool):
                        values.append("TRUE" if v else "FALSE")
                    elif isinstance(v, (int, float)):
                        values.append(str(v))
                    else:
                        escaped = str(v).replace("'", "''")
                        values.append(f"'{escaped}'")
                vals_str = ", ".join(values)
                lines.append(f"INSERT INTO {table} ({cols_str}) VALUES ({vals_str});\n")

        # Сбрасываем sequences
        cur.execute("""
            SELECT sequence_name FROM information_schema.sequences
            WHERE sequence_schema = 'public'
        """)
        for (seq,) in cur.fetchall():
            cur.execute(f"SELECT last_value FROM {seq}")
            last_val = cur.fetchone()[0]
            lines.append(f"SELECT setval('{seq}', {last_val});\n")

        cur.close()
        conn.close()

        content = "".join(lines).encode("utf-8")
        filename = f"tradi_backup_{date.today().strftime('%Y%m%d')}.sql"
        return StreamingResponse(
            iter([content]),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        print(f"❌ Ошибка бэкапа: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка бэкапа: {e}")


@router.post("/db-restore")
async def db_restore(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """Восстанавливает БД из .sql дампа через Python psycopg2"""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user or user.role not in (UserRole.developer, UserRole.owner_business):
        raise HTTPException(status_code=403, detail="Только владелец или разработчик")

    if not file.filename or not file.filename.endswith(".sql"):
        raise HTTPException(status_code=400, detail="Нужен файл .sql")

    from app.core.config import settings
    import psycopg2
    from urllib.parse import urlparse

    contents = await file.read()
    sql_text = contents.decode("utf-8")

    try:
        parsed = urlparse(settings.DATABASE_URL)
        conn = psycopg2.connect(
            dbname=parsed.path.lstrip("/"),
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname,
            port=parsed.port or 5432,
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Выполняем SQL построчно, пропускаем комментарии
        statements = [s.strip() for s in sql_text.split(";\n") if s.strip() and not s.strip().startswith("--")]
        errors = []
        for stmt in statements:
            try:
                cur.execute(stmt)
            except Exception as e:
                errors.append(str(e))

        cur.close()
        conn.close()

        if errors:
            print(f"⚠️ Restore warnings: {errors[:5]}")

        return {"message": "✅ База данных восстановлена. Перезайдите в приложение."}

    except Exception as e:
        print(f"❌ Ошибка восстановления: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка восстановления: {e}")


# ─── Сброс всех данных ───────────────────────────────────────────

@router.post("/reset")
def reset_all_data(
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """
    ⚠️ Удаляет все данные и сбрасывает нумерацию с 1.
    Пользователи не удаляются. Сделай экспорт перед сбросом!
    """
    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user or user.role not in (UserRole.developer, UserRole.owner_business):
        raise HTTPException(status_code=403, detail="Только владелец или разработчик")

    tables = [
        "audit_log", "returns", "sale_items", "sales",
        "receipts", "expenses", "products", "customers", "suppliers",
    ]
    for table in tables:
        try:
            db.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"))
        except Exception:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Ошибка при очистке таблицы {table}")
    db.commit()
    return {"message": "✅ Все данные удалены. Нумерация сброшена до 1."}


# ─── Импорт / Восстановление из Excel ───────────────────────────

@router.post("/import")
async def import_from_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    telegram_id: int = Depends(get_current_user),
):
    """
    Восстанавливает товары, поставщиков и клиентов из экспортированного Excel.
    Лист 'Остатки'        → товары (SKU, Название, Категория, Поставщик, Цена закупки, Цена продажи, Остаток)
    Лист 'Долги клиентов' → клиенты (Клиент, Телефон, Адрес, Всего покупок, Долг)
    Существующие записи (по SKU / телефону) обновляются, новые создаются.
    """
    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user or user.role not in (UserRole.developer, UserRole.owner_business):
        raise HTTPException(status_code=403, detail="Только владелец или разработчик")

    if not file.filename or not file.filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Нужен файл .xlsx")

    from decimal import Decimal
    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(BytesIO(contents), data_only=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Не удалось открыть файл Excel")

    result = {
        "products_created": 0, "products_updated": 0,
        "customers_created": 0, "customers_updated": 0,
        "suppliers_created": 0, "errors": [],
    }

    # ── Лист "Остатки" → товары ──────────────────────────────────
    if "Остатки" in wb.sheetnames:
        ws = wb["Остатки"]
        # Найти строку заголовков по слову SKU
        hrow = next(
            (r[0].row for r in ws.iter_rows()
             if any(str(c.value or "").strip().upper() == "SKU" for c in r)),
            None
        )
        if hrow:
            hdrs = {str(ws.cell(hrow, c).value or "").strip(): c
                    for c in range(1, ws.max_column + 1)}

            for ri in range(hrow + 1, ws.max_row + 1):
                def g(name):
                    return ws.cell(ri, hdrs[name]).value if name in hdrs else None

                sku  = str(g("SKU")      or "").strip()
                name = str(g("Название") or "").strip()
                if not sku or not name or name == "None":
                    continue
                try:
                    purchase_price = Decimal(str(g("Цена закупки") or 0))
                    selling_price  = Decimal(str(g("Цена продажи") or 0))
                    current_stock  = int(g("Остаток") or 0)
                    category       = str(g("Категория") or "").strip() or None
                    sup_name       = str(g("Поставщик") or "").strip() or None
                except (ValueError, TypeError) as e:
                    result["errors"].append(f"Строка {ri}: {e}")
                    continue

                supplier_id = None
                if sup_name and sup_name != "—":
                    sup = db.query(Supplier).filter(Supplier.name == sup_name).first()
                    if not sup:
                        sup = Supplier(name=sup_name)
                        db.add(sup); db.flush()
                        result["suppliers_created"] += 1
                    supplier_id = sup.id

                prod = db.query(Product).filter(Product.sku == sku).first()
                if prod:
                    prod.name = name; prod.category = category
                    prod.supplier_id = supplier_id
                    prod.purchase_price = purchase_price
                    prod.selling_price  = selling_price
                    prod.current_stock  = current_stock
                    result["products_updated"] += 1
                else:
                    db.add(Product(
                        sku=sku, name=name, category=category,
                        supplier_id=supplier_id,
                        purchase_price=purchase_price,
                        selling_price=selling_price,
                        current_stock=current_stock,
                        min_stock=5,
                    ))
                    result["products_created"] += 1

    # ── Лист "Долги клиентов" → клиенты ─────────────────────────
    if "Долги клиентов" in wb.sheetnames:
        ws = wb["Долги клиентов"]
        hrow = next(
            (r[0].row for r in ws.iter_rows()
             if any(str(c.value or "").strip() == "Клиент" for c in r)),
            None
        )
        if hrow:
            hdrs = {str(ws.cell(hrow, c).value or "").strip(): c
                    for c in range(1, ws.max_column + 1)}

            for ri in range(hrow + 1, ws.max_row + 1):
                def cv(name):
                    return ws.cell(ri, hdrs[name]).value if name in hdrs else None

                cname = str(cv("Клиент")  or "").strip()
                phone = str(cv("Телефон") or "").strip()
                if not cname or cname in ("None", "ИТОГО ДОЛГ"):
                    continue
                try:
                    total_purchases = Decimal(str(cv("Всего покупок") or 0))
                    total_debt      = Decimal(str(cv("Долг")          or 0))
                    address = str(cv("Адрес") or "").strip() or None
                except (ValueError, TypeError) as e:
                    result["errors"].append(f"Клиент строка {ri}: {e}")
                    continue

                cust = db.query(Customer).filter(Customer.phone == phone).first()
                if cust:
                    cust.name = cname; cust.address = address
                    cust.total_purchases = total_purchases
                    cust.total_debt = total_debt
                    result["customers_updated"] += 1
                else:
                    db.add(Customer(
                        name=cname, phone=phone, address=address,
                        total_purchases=total_purchases,
                        total_debt=total_debt,
                    ))
                    result["customers_created"] += 1

    db.commit()
    return {"message": "✅ Импорт завершён", **result}