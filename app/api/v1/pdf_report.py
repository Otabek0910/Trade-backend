"""
app/api/v1/pdf_report.py

Генерация PDF-отчёта по инвестиции/продажам.
Подключить в main.py:
    from app.api.v1.pdf_report import router as pdf_report_router
    app.include_router(pdf_report_router)
"""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, Date, and_
from datetime import date, timedelta
from io import BytesIO

from app.db.session import get_db
from app.models.sale import Sale, SaleStatus
from app.models.sale_item import SaleItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.expense import Expense
from app.models.return_model import Return
from app.models.supplier import Supplier
from app.models.receipt import Receipt
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/export", tags=["Экспорт"])


def _fmt(n: float) -> str:
    """1 924 500"""
    return f"{round(n):,}".replace(",", " ")

def _fmtM(n: float) -> str:
    """1.9 млн."""
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f} млн."
    if abs(n) >= 1_000:
        return f"{n/1_000:.0f}К"
    return str(round(n))


def build_pdf(db: Session) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether
    )
    from reportlab.graphics.shapes import Drawing, Rect, String, Line
    from reportlab.graphics.charts.barcharts import VerticalBarChart, HorizontalBarChart
    from reportlab.graphics import renderPDF

    today = date.today()
    month_start = today.replace(day=1)
    # Дата первой продажи
    first_sale = db.query(func.min(cast(Sale.created_at, Date))).scalar()
    date_from = first_sale if first_sale else month_start

    # ── Собираем данные ────────────────────────────────────────────────────────
    completed_sales = db.query(Sale).filter(Sale.status == SaleStatus.completed).all()

    # Выручка и себестоимость
    revenue_gross = sum(float(s.total_amount) for s in completed_sales)
    paid_total    = sum(float(s.paid_amount)   for s in completed_sales)

    cogs = sum(
        float(item.purchase_price_at_sale) * item.quantity
        for s in completed_sales for item in s.items
    )

    # Возвраты (всё время)
    returns_all = db.query(Return).all()
    returns_amount = sum(float(r.return_amount) for r in returns_all)
    returns_cogs   = 0.0
    for r in returns_all:
        si = db.query(SaleItem).filter(
            SaleItem.sale_id == r.sale_id,
            SaleItem.product_id == r.product_id
        ).first()
        if si:
            returns_cogs += float(si.purchase_price_at_sale) * r.quantity

    net_revenue  = revenue_gross - returns_amount
    net_cogs     = cogs - returns_cogs
    gross_profit = net_revenue - net_cogs

    # Расходы
    expenses_total = float(db.query(func.sum(Expense.amount)).scalar() or 0)
    net_profit     = gross_profit - expenses_total

    # Закупки у поставщиков
    total_purchased = float(
        db.query(func.sum(Receipt.purchase_price * Receipt.quantity)).scalar() or 0
    )
    total_paid_to_suppliers = total_purchased - float(
        db.query(func.sum(Supplier.total_debt)).scalar() or 0
    )
    total_supplier_debt = float(db.query(func.sum(Supplier.total_debt)).scalar() or 0)

    # Долг клиентов
    total_customer_debt = float(db.query(func.sum(Customer.total_debt)).scalar() or 0)
    cash_in_hand        = paid_total - max(0, returns_amount - total_customer_debt)

    # Склад
    stock_value = float(
        db.query(func.sum(Product.purchase_price * Product.current_stock)).scalar() or 0
    )
    stock_potential = float(
        db.query(func.sum(Product.selling_price * Product.current_stock)).scalar() or 0
    )

    # Топ должников
    top_debtors = (
        db.query(Customer)
        .filter(Customer.total_debt > 0, Customer.is_active.is_(True))
        .order_by(Customer.total_debt.desc())
        .limit(8).all()
    )

    # Топ товаров по выручке
    top_products_raw = (
        db.query(
            Product.name,
            func.sum(SaleItem.selling_price * SaleItem.quantity).label("rev"),
            func.sum(SaleItem.quantity).label("qty"),
        )
        .join(SaleItem, SaleItem.product_id == Product.id)
        .join(Sale, and_(Sale.id == SaleItem.sale_id, Sale.status == SaleStatus.completed))
        .group_by(Product.name)
        .order_by(func.sum(SaleItem.selling_price * SaleItem.quantity).desc())
        .limit(8).all()
    )

    # Продажи по неделям
    from collections import defaultdict
    weekly: dict[str, float] = defaultdict(float)
    for s in completed_sales:
        if s.created_at:
            # Понедельник той недели
            d = s.created_at.date()
            monday = d - timedelta(days=d.weekday())
            key = monday.strftime("%d.%m")
            weekly[key] += float(s.total_amount)
    weekly_sorted = sorted(weekly.items())[-8:]  # последние 8 недель

    # Непродающиеся товары
    sold_product_ids = {item.product_id for s in completed_sales for item in s.items}
    frozen_products = (
        db.query(Product)
        .filter(Product.current_stock > 0, ~Product.id.in_(sold_product_ids or {-1}))
        .all()
    )
    frozen_value = sum(float(p.purchase_price) * p.current_stock for p in frozen_products)

    # ── Reportlab PDF ──────────────────────────────────────────────────────────
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
    )
    W = A4[0] - 30*mm  # ширина контента

    styles = getSampleStyleSheet()
    # Кастомные стили
    H1  = ParagraphStyle('H1',  fontName='Helvetica-Bold', fontSize=18, textColor=colors.HexColor('#1a1a1a'), spaceAfter=2)
    H2  = ParagraphStyle('H2',  fontName='Helvetica-Bold', fontSize=11, textColor=colors.HexColor('#1a1a1a'), spaceAfter=4, spaceBefore=8)
    H3  = ParagraphStyle('H3',  fontName='Helvetica-Bold', fontSize=9,  textColor=colors.HexColor('#555555'))
    BODY= ParagraphStyle('BODY',fontName='Helvetica',      fontSize=9,  textColor=colors.HexColor('#333333'), leading=14)
    MUTED=ParagraphStyle('MUTED',fontName='Helvetica',     fontSize=8,  textColor=colors.HexColor('#888888'))
    RED  = ParagraphStyle('RED', fontName='Helvetica-Bold',fontSize=9,  textColor=colors.HexColor('#cc2222'))
    GREEN= ParagraphStyle('GREEN',fontName='Helvetica-Bold',fontSize=9, textColor=colors.HexColor('#1a6b3c'))
    BLUE = ParagraphStyle('BLUE', fontName='Helvetica-Bold',fontSize=9, textColor=colors.HexColor('#2481cc'))

    def section_title(text):
        return [
            Spacer(1, 6),
            Paragraph(text, H2),
            HRFlowable(width=W, thickness=1, color=colors.HexColor('#e8eaed')),
            Spacer(1, 4),
        ]

    def kpi_table(items):
        """Строка KPI-карточек: items = [(label, value, color)]"""
        n = len(items)
        col_w = W / n
        data = [[Paragraph(v, ParagraphStyle('kv', fontName='Helvetica-Bold', fontSize=14, textColor=colors.HexColor(c), leading=16)) for _, v, c in items],
                [Paragraph(l, MUTED) for l, _, _ in items]]
        t = Table(data, colWidths=[col_w]*n)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f8f9fa')),
            ('BOX',        (0,0), (-1,-1), 0.5, colors.HexColor('#e8eaed')),
            ('INNERGRID',  (0,0), (-1,-1), 0.5, colors.HexColor('#e8eaed')),
            ('TOPPADDING', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING',(0,0),(-1,-1),8),
            ('LEFTPADDING',(0,0),(-1,-1), 10),
        ]))
        return t

    def money_table(rows, highlight_last=False):
        """Таблица двух колонок: label | value"""
        data = []
        for label, value, is_bold, color in rows:
            lp = Paragraph(label, ParagraphStyle('l', fontName='Helvetica-Bold' if is_bold else 'Helvetica', fontSize=9, textColor=colors.HexColor('#1a1a1a')))
            vp = Paragraph(value, ParagraphStyle('v', fontName='Helvetica-Bold' if is_bold else 'Helvetica', fontSize=9, textColor=colors.HexColor(color), alignment=2))
            data.append([lp, vp])
        t = Table(data, colWidths=[W*0.65, W*0.35])
        style = [
            ('TOPPADDING',  (0,0), (-1,-1), 4),
            ('BOTTOMPADDING',(0,0),(-1,-1), 4),
            ('LINEBELOW',  (0,-1), (-1,-1), 1, colors.HexColor('#e8eaed')),
        ]
        if highlight_last:
            style.append(('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#f0faf4')))
        t.setStyle(TableStyle(style))
        return t

    def bar_chart_weekly(data_pairs):
        """Вертикальный бар-чарт по неделям"""
        if not data_pairs:
            return Spacer(1, 0)
        labels = [d[0] for d in data_pairs]
        values = [d[1] for d in data_pairs]
        chart_h = 60
        chart_w = W
        drawing = Drawing(chart_w, chart_h + 20)
        bc = VerticalBarChart()
        bc.x = 30; bc.y = 20
        bc.width = chart_w - 50; bc.height = chart_h - 10
        bc.data = [values]
        bc.bars[0].fillColor = colors.HexColor('#2481cc')
        bc.bars[0].strokeColor = None
        bc.categoryAxis.categoryNames = labels
        bc.categoryAxis.labels.fontSize = 7
        bc.categoryAxis.labels.angle = 0
        bc.valueAxis.labels.fontSize = 7
        bc.valueAxis.forceZero = True
        bc.valueAxis.labelTextFormat = lambda v: _fmtM(v) if v > 0 else ''
        drawing.add(bc)
        return drawing

    def hbar_chart_products(products):
        """Горизонтальный бар-чарт по товарам"""
        if not products:
            return Spacer(1, 0)
        names  = [p.name[:25] for p in products]
        values = [float(p.rev) for p in products]
        chart_h = max(60, len(names) * 14)
        drawing = Drawing(W, chart_h + 10)
        bc = HorizontalBarChart()
        bc.x = 100; bc.y = 5
        bc.width = W - 120; bc.height = chart_h
        bc.data = [values]
        bc.bars[0].fillColor = colors.HexColor('#2481cc')
        bc.bars[0].strokeColor = None
        bc.categoryAxis.categoryNames = names
        bc.categoryAxis.labels.fontSize = 7
        bc.valueAxis.labels.fontSize = 7
        bc.valueAxis.forceZero = True
        bc.valueAxis.labelTextFormat = lambda v: _fmtM(v) if v > 0 else ''
        drawing.add(bc)
        return drawing

    # ── Строим PDF ────────────────────────────────────────────────────────────
    margin_pct = round(gross_profit / net_revenue * 100, 1) if net_revenue > 0 else 0

    story = []

    # ── Заголовок ──
    story.append(Paragraph("Отчёт по продажам — TRADI", H1))
    story.append(Paragraph(
        f"Период: с {date_from.strftime('%d.%m.%Y')} по {today.strftime('%d.%m.%Y')} · Сформировано: {today.strftime('%d %B %Y')}",
        MUTED
    ))
    story.append(Spacer(1, 8))

    # ── KPI карточки ──
    story.append(kpi_table([
        ('Вложили (закупки)',    _fmtM(total_purchased),       '#7a3b8c'),
        ('Чистая выручка',      _fmtM(net_revenue),            '#1a6b3c'),
        ('В кассе реально',     _fmtM(cash_in_hand),           '#2481cc'),
        (f'Прибыль {margin_pct}%', _fmtM(net_profit),          '#34c759' if net_profit >= 0 else '#cc2222'),
        ('Долг поставщику',     _fmtM(total_supplier_debt),    '#ff3b30'),
    ]))
    story.append(Spacer(1, 10))

    # ── Два столбца: Закупки | Товар на складе ──
    col_data = [[
        # Левый: Закупки
        [
            Paragraph("ЗАКУПКИ У ПОСТАВЩИКА", H3),
            Spacer(1, 4),
            money_table([
                ('Куплено товара всего',  _fmt(total_purchased) + ' сум',   False, '#1a1a1a'),
                ('Уже оплатили',         _fmt(total_paid_to_suppliers) + ' сум', False, '#1a6b3c'),
                ('Ещё должны поставщику', _fmt(total_supplier_debt) + ' сум',True, '#cc2222'),
            ]),
        ],
        # Правый: Склад
        [
            Paragraph("ТОВАР НА СКЛАДЕ СЕЙЧАС", H3),
            Spacer(1, 4),
            money_table([
                ('Стоимость по закупке', _fmt(stock_value) + ' сум',    False, '#1a1a1a'),
                ('Потенциал по продажам', _fmt(stock_potential) + ' сум', False, '#7a3b8c'),
                ('Заморожено (нет продаж)', _fmt(frozen_value) + ' сум', True, '#e08030'),
            ]),
        ],
    ]]

    from reportlab.platypus import BalancedColumns
    # Используем Table для двух колонок
    left_items  = col_data[0][0]
    right_items = col_data[0][1]

    two_col = Table(
        [[left_items, right_items]],
        colWidths=[W * 0.48, W * 0.48],
        hAlign='LEFT'
    )
    two_col.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING',  (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING',   (0,0), (-1,-1), 0),
        ('BOTTOMPADDING',(0,0), (-1,-1), 0),
    ]))
    story.append(two_col)
    story.append(Spacer(1, 10))

    # ── Выручка и прибыль ──
    story += section_title("ВЫРУЧКА И ПРИБЫЛЬ")
    story.append(money_table([
        ('Продажи (завершённые сделки)',  _fmt(revenue_gross) + ' сум',  False, '#1a1a1a'),
        ('Возвраты по браку',             '−' + _fmt(returns_amount) + ' сум', False, '#e08030'),
        ('= Чистая выручка',              _fmt(net_revenue) + ' сум',    True,  '#1a6b3c'),
        ('Минус себестоимость',           '−' + _fmt(net_cogs) + ' сум', False, '#555555'),
        (f'= Валовая прибыль  {margin_pct}%', _fmt(gross_profit) + ' сум', True, '#34c759'),
        ('Минус расходы (операционные)',  '−' + _fmt(expenses_total) + ' сум', False, '#e05555'),
        ('Чистая прибыль',                _fmt(net_profit) + ' сум',     True,  '#34c759' if net_profit >= 0 else '#cc2222'),
    ], highlight_last=True))
    story.append(Spacer(1, 10))

    # ── Где деньги ──
    story += section_title("ГДЕ СЕЙЧАС ДЕНЬГИ?")
    story.append(money_table([
        ('Чистая выручка',         _fmt(net_revenue) + ' сум',         False, '#1a1a1a'),
        ('Клиенты не заплатили',   '−' + _fmt(total_customer_debt) + ' сум', False, '#cc2222'),
        ('Фактически в кассе',     _fmt(cash_in_hand) + ' сум',        True,  '#2481cc'),
    ]))
    if net_revenue > 0:
        pct = round(cash_in_hand / net_revenue * 100, 1)
        story.append(Paragraph(
            f"Сбор долгов: {pct}% — {'⚠️ критически низко' if pct < 30 else 'нормально'}",
            RED if pct < 30 else GREEN
        ))
    story.append(Spacer(1, 10))

    # ── Топ должников ──
    if top_debtors:
        story += section_title(f"ТОП ДОЛЖНИКОВ (итого {_fmtM(total_customer_debt)} сум)")
        debt_data = [['#', 'Клиент', 'Телефон', 'Долг']]
        for i, c in enumerate(top_debtors, 1):
            debt_data.append([
                str(i), c.name, c.phone or '—',
                _fmt(float(c.total_debt)) + ' сум'
            ])
        t = Table(debt_data, colWidths=[8*mm, W*0.35, W*0.3, W*0.25])
        t.setStyle(TableStyle([
            ('BACKGROUND',  (0,0), (-1,0),  colors.HexColor('#f0f2f5')),
            ('FONTNAME',    (0,0), (-1,0),  'Helvetica-Bold'),
            ('FONTSIZE',    (0,0), (-1,-1), 8),
            ('TEXTCOLOR',   (3,1), (3,-1),  colors.HexColor('#cc2222')),
            ('FONTNAME',    (3,1), (3,-1),  'Helvetica-Bold'),
            ('TOPPADDING',  (0,0), (-1,-1), 4),
            ('BOTTOMPADDING',(0,0),(-1,-1), 4),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#fafafa')]),
            ('GRID',        (0,0), (-1,-1), 0.5, colors.HexColor('#e8eaed')),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))

    # ── Активность по неделям ──
    if weekly_sorted:
        story += section_title("АКТИВНОСТЬ ПРОДАЖ — ПО НЕДЕЛЯМ")
        story.append(bar_chart_weekly(weekly_sorted))
        story.append(Spacer(1, 10))

    # ── Топ товаров ──
    if top_products_raw:
        story += section_title("ТОП ТОВАРОВ ПО ВЫРУЧКЕ")
        story.append(hbar_chart_products(top_products_raw))
        story.append(Spacer(1, 10))

    # ── Замороженные деньги ──
    story += section_title("ЗАМОРОЖЕННЫЕ ДЕНЬГИ В ТОВАРЕ")
    story.append(money_table([
        ('Товар без единой продажи', _fmt(frozen_value) + ' сум',              True,  '#e08030'),
        ('Итого заморожено',         _fmt(frozen_value + float(0)) + ' сум',   True,  '#cc2222'),
    ]))
    if frozen_products:
        fp_data = [['SKU', 'Название', 'Шт.', 'Стоимость']]
        for p in frozen_products[:10]:
            fp_data.append([
                p.sku,
                p.name[:30],
                str(p.current_stock),
                _fmt(float(p.purchase_price) * p.current_stock) + ' сум',
            ])
        t = Table(fp_data, colWidths=[20*mm, W*0.45, 10*mm, W*0.25])
        t.setStyle(TableStyle([
            ('BACKGROUND',  (0,0), (-1,0),  colors.HexColor('#fff3e0')),
            ('FONTNAME',    (0,0), (-1,0),  'Helvetica-Bold'),
            ('FONTSIZE',    (0,0), (-1,-1), 7.5),
            ('TEXTCOLOR',   (3,1), (3,-1),  colors.HexColor('#e08030')),
            ('TOPPADDING',  (0,0), (-1,-1), 3),
            ('BOTTOMPADDING',(0,0),(-1,-1), 3),
            ('GRID',        (0,0), (-1,-1), 0.5, colors.HexColor('#e8eaed')),
        ]))
        story.append(Spacer(1, 4))
        story.append(t)

    # ── Вывод (footer) ──
    story.append(Spacer(1, 12))
    story.append(HRFlowable(width=W, thickness=1.5, color=colors.HexColor('#1a6b3c')))
    story.append(Spacer(1, 4))
    conclusion = (
        f"<b>Главный вывод:</b> Из {_fmtM(net_revenue)} чистой выручки реальными деньгами получено "
        f"{_fmtM(cash_in_hand)} ({round(cash_in_hand/net_revenue*100, 1) if net_revenue > 0 else 0}%) — "
        f"остальное у клиентов в долг.  |  "
        f"Сами должны поставщику {_fmtM(total_supplier_debt)}.  |  "
        f"В непроданном товаре заморожено {_fmtM(frozen_value)}.  |  "
        f"Маржа {margin_pct}%"
    )
    story.append(Paragraph(conclusion, ParagraphStyle(
        'FOOTER', fontName='Helvetica', fontSize=8,
        textColor=colors.HexColor('#1a1a1a'),
        backColor=colors.HexColor('#f0faf4'),
        borderPad=8, leading=13,
    )))

    doc.build(story)
    return buf.getvalue()


# ── Эндпоинт ──────────────────────────────────────────────────────────────────
@router.get("/pdf-report")
def download_pdf_report(
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    """Генерирует PDF отчёт по всей истории продаж"""
    pdf_bytes = build_pdf(db)
    filename = f"tradi_report_{date.today().strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
