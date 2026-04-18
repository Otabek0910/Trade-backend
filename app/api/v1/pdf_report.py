"""
app/api/v1/pdf_report.py
PDF-отчёт по инвестиции/продажам с кириллицей (DejaVu Sans)
Визуально близок к примеру: KPI карточки, двухколоночный блок,
P&L таблица, должники, графики, замороженные деньги, итоговый вывод.
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
from app.models.supplier_return import SupplierReturn
from app.core.telegram_auth import get_current_user

router = APIRouter(prefix="/export", tags=["Экспорт"])


# ── Шрифты ────────────────────────────────────────────────────────────────────
def _register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os
    reg  = next((p for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                              "/usr/share/fonts/dejavu/DejaVuSans.ttf"] if os.path.exists(p)), None)
    bold = next((p for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"] if os.path.exists(p)), None)
    if reg:  pdfmetrics.registerFont(TTFont("Dv",  reg))
    if bold: pdfmetrics.registerFont(TTFont("DvB", bold))
    return bool(reg)

def _fmt(n):  return f"{round(n):,}".replace(",", " ")
def _fmtM(n):
    if abs(n) >= 1_000_000: return f"{n/1_000_000:.1f} млн."
    if abs(n) >= 1_000:     return f"{n/1_000:.0f}К"
    return str(round(n))


# ── Построение PDF ─────────────────────────────────────────────────────────────
def build_pdf(db: Session) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether
    )
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.graphics.charts.barcharts import VerticalBarChart, HorizontalBarChart

    has_cyr = _register_fonts()
    R  = "Dv"  if has_cyr else "Helvetica"
    B  = "DvB" if has_cyr else "Helvetica-Bold"
    CB = colors.HexColor

    today      = date.today()
    first_sale = db.query(func.min(cast(Sale.created_at, Date))).scalar()
    date_from  = first_sale if first_sale else today.replace(day=1)

    # ── Данные ────────────────────────────────────────────────────────────────
    completed = db.query(Sale).filter(Sale.status == SaleStatus.completed).all()
    all_sales = completed  # для некоторых расчётов

    revenue_gross = sum(float(s.total_amount) for s in completed)
    paid_total    = sum(float(s.paid_amount)  for s in completed)
    sales_count   = len(completed)

    cogs = sum(float(i.purchase_price_at_sale)*i.quantity for s in completed for i in s.items)

    returns_all    = db.query(Return).all()
    returns_amount = sum(float(r.return_amount) for r in returns_all)
    returns_count  = len(returns_all)
    returns_cogs   = 0.0
    for r in returns_all:
        si = db.query(SaleItem).filter(SaleItem.sale_id==r.sale_id,
                                       SaleItem.product_id==r.product_id).first()
        if si: returns_cogs += float(si.purchase_price_at_sale) * r.quantity

    net_revenue  = revenue_gross - returns_amount
    net_cogs     = cogs - returns_cogs
    gross_profit = net_revenue - net_cogs
    margin_pct   = round(gross_profit/net_revenue*100, 1) if net_revenue > 0 else 0
    expenses_total = float(db.query(func.sum(Expense.amount)).scalar() or 0)
    net_profit   = gross_profit - expenses_total

    total_purchased_raw = float(db.query(func.sum(Receipt.paid_amount+Receipt.debt)).scalar() or 0)
    total_returned_sup  = float(db.query(func.sum(SupplierReturn.return_amount)).scalar() or 0)
    total_purchased     = total_purchased_raw - total_returned_sup
    total_sup_debt      = float(db.query(func.sum(Supplier.total_debt)).scalar() or 0)
    total_paid_sup      = total_purchased - total_sup_debt

    total_cust_debt = float(db.query(func.sum(Customer.total_debt)).scalar() or 0)
    cash_in_hand    = paid_total - max(0, returns_amount - total_cust_debt)
    cash_pct        = round(cash_in_hand/net_revenue*100, 1) if net_revenue > 0 else 0

    stock_value     = float(db.query(func.sum(Product.purchase_price*Product.current_stock)).scalar() or 0)
    stock_potential = float(db.query(func.sum(Product.selling_price *Product.current_stock)).scalar() or 0)

    top_debtors = (db.query(Customer)
        .filter(Customer.total_debt > 0, Customer.is_active.is_(True))
        .order_by(Customer.total_debt.desc()).limit(8).all())

    top_products_raw = (db.query(
            Product.name, Product.sku,
            func.sum(SaleItem.selling_price*SaleItem.quantity).label("rev"),
            func.sum(SaleItem.quantity).label("qty"))
        .join(SaleItem, SaleItem.product_id==Product.id)
        .join(Sale, and_(Sale.id==SaleItem.sale_id, Sale.status==SaleStatus.completed))
        .group_by(Product.name, Product.sku)
        .order_by(func.sum(SaleItem.selling_price*SaleItem.quantity).desc())
        .limit(8).all())

    from collections import defaultdict
    weekly: dict = defaultdict(float)
    for s in completed:
        if s.created_at:
            d = s.created_at.date()
            weekly[(d - timedelta(days=d.weekday())).strftime("%d.%m")] += float(s.total_amount)
    weekly_sorted = sorted(weekly.items())[-8:]

    sold_ids = {i.product_id for s in completed for i in s.items}
    frozen   = db.query(Product).filter(
        Product.current_stock > 0,
        ~Product.id.in_(sold_ids or {-1})
    ).order_by((Product.purchase_price * Product.current_stock).desc()).all()
    frozen_value = sum(float(p.purchase_price)*p.current_stock for p in frozen)

    # Счётчики продаж
    all_s = db.query(Sale).all()
    cnt_created   = len(all_s)
    cnt_completed = sum(1 for s in all_s if s.status.value == "completed")
    cnt_cancelled = sum(1 for s in all_s if s.status.value == "cancelled")
    cnt_returned  = sum(1 for s in all_s if s.status.value == "returned")

    # ── Документ ──────────────────────────────────────────────────────────────
    buf = BytesIO()
    W   = A4[0] - 28*mm
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=14*mm, rightMargin=14*mm,
                            topMargin=12*mm,  bottomMargin=12*mm)

    # ── Стили ─────────────────────────────────────────────────────────────────
    def ps(name, fn=R, size=9, col='#333333', align=0, leading=12, bold=False, space_before=0, space_after=2):
        return ParagraphStyle(name, fontName=B if bold else fn, fontSize=size,
                              textColor=CB(col), alignment=align, leading=leading,
                              spaceBefore=space_before, spaceAfter=space_after)

    S_h1    = ps('h1', B, 17, '#1a1a1a', leading=21)
    S_sub   = ps('sub', size=8, col='#888888')
    S_h2    = ps('h2', B, 9,  '#1a1a1a', space_before=8, space_after=3)
    S_body  = ps('body', size=8.5)
    S_bold  = ps('sbold', B, 8.5, '#1a1a1a')
    S_muted = ps('mu',  size=7.5, col='#888888')
    S_red   = ps('rd',  B, 8.5, '#cc2222')
    S_green = ps('gr',  B, 8.5, '#1a6b3c')
    S_tag   = ps('tag', B, 7,  '#ffffff', align=1)

    BG    = colors.HexColor('#f8f9fa')
    BORD  = colors.HexColor('#e8eaed')
    BORD2 = colors.HexColor('#1a6b3c')

    def hr(color='#e8eaed', thick=0.5):
        return HRFlowable(width=W, thickness=thick, color=CB(color), spaceAfter=4)

    def sec(title, color='#1a1a1a'):
        return [Spacer(1, 5), Paragraph(title, ps('sh', B, 9, color)), hr()]

    # ── KPI карточки ──────────────────────────────────────────────────────────
    def kpi_row(items):
        """items = [(label, value_str, sub, color)]"""
        n = len(items); cw = W/n
        rows = [
            [Paragraph(v, ParagraphStyle('kv', fontName=B, fontSize=14,
                                         textColor=CB(c), leading=17))
             for _,v,_,c in items],
            [Paragraph(l, S_muted) for l,_,_,_ in items],
            [Paragraph(s, ParagraphStyle('ks', fontName=R, fontSize=7,
                                         textColor=CB('#aaaaaa'), leading=9, fontStyle='italic' if hasattr(ParagraphStyle,'fontStyle') else 'normal'))
             for _,_,s,_ in items],
        ]
        t = Table(rows, colWidths=[cw]*n)
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0),(-1,-1), BG),
            ('BOX',           (0,0),(-1,-1), .5, BORD),
            ('INNERGRID',     (0,0),(-1,-1), .5, BORD),
            ('TOPPADDING',    (0,0),(-1,-1), 9),
            ('BOTTOMPADDING', (0,0),(-1,-1), 9),
            ('LEFTPADDING',   (0,0),(-1,-1), 10),
        ]))
        return t

    # ── Таблица денег (label | value) ─────────────────────────────────────────
    def money_tbl(rows, hl_last=False, left_w=0.64):
        data = []
        for lbl, val, bold, col in rows:
            fn = B if bold else R
            lp = Paragraph(lbl, ParagraphStyle('l', fontName=fn, fontSize=8.5,
                            textColor=CB('#1a1a1a'), leading=12))
            vp = Paragraph(val, ParagraphStyle('v', fontName=fn, fontSize=8.5,
                            textColor=CB(col), leading=12, alignment=2))
            data.append([lp, vp])
        t = Table(data, colWidths=[W*left_w, W*(1-left_w-0.02)])
        sty = [('TOPPADDING',(0,0),(-1,-1),3), ('BOTTOMPADDING',(0,0),(-1,-1),3),
               ('LINEBELOW',(0,-1),(-1,-1),.5,BORD)]
        if hl_last:
            sty.append(('BACKGROUND',(0,-1),(-1,-1),CB('#eafaf1')))
        t.setStyle(TableStyle(sty))
        return t

    # ── Двухколоночный блок ───────────────────────────────────────────────────
    def two_col(left, right, ratios=(0.48, 0.48)):
        t = Table([[left, right]], colWidths=[W*ratios[0], W*ratios[1]])
        t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),
                                ('LEFTPADDING',(0,0),(-1,-1),0),
                                ('RIGHTPADDING',(0,0),(-1,-1),7),
                                ('TOPPADDING',(0,0),(-1,-1),0),
                                ('BOTTOMPADDING',(0,0),(-1,-1),0)]))
        return t

    # ── Бейдж (цветная метка) ─────────────────────────────────────────────────
    def badge(text, bg_color, text_color='#ffffff'):
        t = Table([[Paragraph(text, ParagraphStyle('b', fontName=B, fontSize=7,
                              textColor=CB(text_color), alignment=1, leading=9))]],
                  colWidths=[28*mm])
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),CB(bg_color)),
                                ('TOPPADDING',(0,0),(-1,-1),3),
                                ('BOTTOMPADDING',(0,0),(-1,-1),3),
                                ('ROUNDEDCORNERS',[3])]))
        return t

    # ── Бар-чарт по неделям ───────────────────────────────────────────────────
    def weekly_bar(pairs, chart_w=None):
        if not pairs: return Spacer(1, 1)
        cw   = chart_w or W
        vals = [p[1] for p in pairs]
        labs = [p[0] for p in pairs]
        # Раскраска: последняя неделя — акцент
        h = 75
        d = Drawing(cw, h+22)
        bc = VerticalBarChart()
        bc.x = 32; bc.y = 20; bc.width = cw-44; bc.height = h
        bc.data = [vals]
        # Разные цвета: последний столбец зелёный, остальные синие
        for i in range(len(vals)):
            bc.bars[(0, i)].fillColor = CB('#34c759') if i == len(vals)-1 else CB('#2481cc')
            bc.bars[(0, i)].strokeColor = None
        bc.categoryAxis.categoryNames   = labs
        bc.categoryAxis.labels.fontSize = 6.5
        bc.categoryAxis.labels.fontName = R
        bc.valueAxis.labels.fontSize    = 6.5
        bc.valueAxis.labels.fontName    = R
        bc.valueAxis.forceZero          = True
        bc.valueAxis.labelTextFormat    = lambda v: _fmtM(v) if v > 0 else ''
        d.add(bc)
        return d

    # ── Горизонтальный бар топ-товаров ────────────────────────────────────────
    def hbar_products(prods, chart_w=None):
        if not prods: return Spacer(1, 1)
        cw    = chart_w or W
        names = [f"{p.sku}" for p in prods]
        vals  = [float(p.rev) for p in prods]
        h     = max(55, len(names)*13)
        d = Drawing(cw, h+10)
        bc = HorizontalBarChart()
        bc.x = 48; bc.y = 5; bc.width = cw-60; bc.height = h
        bc.data = [vals]
        bc.bars[0].fillColor   = CB('#2481cc')
        bc.bars[0].strokeColor = None
        bc.categoryAxis.categoryNames   = names
        bc.categoryAxis.labels.fontSize = 7
        bc.categoryAxis.labels.fontName = R
        bc.valueAxis.labels.fontSize    = 7
        bc.valueAxis.labels.fontName    = R
        bc.valueAxis.forceZero          = True
        bc.valueAxis.labelTextFormat    = lambda v: _fmtM(v) if v > 0 else ''
        d.add(bc)
        return d

    # ── Story ─────────────────────────────────────────────────────────────────
    story = []

    # Заголовок
    story.append(Paragraph("Отчёт по инвестиции в продажи", S_h1))
    story.append(Paragraph(
        f"TRADI · с {date_from.strftime('%d.%m.%Y')}",
        S_sub))
    story.append(Spacer(1, 2))
    story.append(Paragraph(
        f"<b>{today.strftime('%d %B %Y')}</b>",
        ps('dt', B, 8, '#888888', align=0)))
    story.append(Spacer(1, 8))

    # KPI строка
    story.append(kpi_row([
        ('ВЛОЖИЛИ',          _fmtM(total_purchased),  'куплено товара',          '#7a3b8c'),
        ('ЧИСТАЯ ВЫРУЧКА',   _fmtM(net_revenue),       f'после возвратов −{_fmtM(returns_amount)}', '#1a6b3c'),
        ('В КАССЕ РЕАЛЬНО',  _fmtM(cash_in_hand),      f'из {_fmtM(net_revenue)} ({cash_pct}%)',    '#2481cc'),
        ('ЧИСТАЯ ПРИБЫЛЬ',   _fmtM(net_profit),        f'маржа {margin_pct}% · на бумаге',         '#34c759' if net_profit >= 0 else '#cc2222'),
        ('ДОЛГ ПОСТАВЩИКУ',  _fmtM(total_sup_debt),    'не оплачено',             '#cc2222'),
    ]))
    story.append(Spacer(1, 10))

    # ── Двухколоночный блок: Закупки + Выручка ────────────────────────────────

    # Левая: Закупки + Склад
    left_items = (
        sec("ЗАКУПКИ У ПОСТАВЩИКА") +
        [money_tbl([
            ('Куплено товара всего',          _fmt(total_purchased)     + ' сум', False, '#1a1a1a'),
            ('Уже оплатили',                  _fmt(total_paid_sup)      + ' сум', False, '#1a6b3c'),
            ('Ещё должны · НЕ ОПЛАЧЕНО',      _fmt(total_sup_debt)      + ' сум', True,  '#cc2222'),
        ])] +
        [Spacer(1, 8)] +
        sec("ТОВАР НА СКЛАДЕ СЕЙЧАС") +
        [money_tbl([
            ('Масла и фильтры (закупочная)',   _fmt(stock_value)         + ' сум', False, '#1a1a1a'),
            ('↳ потенциал по ценам продажи',   _fmt(stock_potential)     + ' сум', False, '#7a3b8c'),
        ])]
    )

    # Правая: Выручка + Где деньги
    right_items = (
        sec("ВЫРУЧКА И ПРИБЫЛЬ") +
        [money_tbl([
            (f'Продали ({sales_count} завершённых сделок)', _fmt(revenue_gross)   + ' сум', False, '#1a1a1a'),
            (f'Возвраты — {returns_count} позиций',         '−' + _fmt(returns_amount) + ' сум', False, '#e08030'),
            ('= Чистая выручка',                             _fmt(net_revenue)    + ' сум', True,  '#1a6b3c'),
            ('Минус себестоимость',                          '−' + _fmt(net_cogs)      + ' сум', False, '#555555'),
            (f'= Валовая прибыль  {margin_pct}%',           _fmt(gross_profit)   + ' сум', True,  '#1a6b3c'),
            ('Минус расходы',                                '−' + _fmt(expenses_total)+ ' сум', False, '#cc2222'),
            ('Чистая прибыль',                               _fmt(net_profit)     + ' сум', True,  '#34c759' if net_profit >= 0 else '#cc2222'),
        ], hl_last=True)] +
        [Spacer(1, 3),
         Paragraph(
             f"{cnt_created} создано · {cnt_completed} завершено · {cnt_cancelled} отменено · {cnt_returned} полный возврат",
             S_muted)] +
        [Spacer(1, 8)] +
        sec("ГДЕ СЕЙЧАС ДЕНЬГИ?", '#cc2222') +
        [money_tbl([
            ('Чистая выручка',               _fmt(net_revenue)        + ' сум', False, '#1a1a1a'),
            ('Клиенты не заплатили',         '−' + _fmt(total_cust_debt) + ' сум', False, '#cc2222'),
            (f'Фактически в кассе',          _fmt(cash_in_hand)       + ' сум', True,  '#2481cc'),
        ], hl_last=True)] +
        ([Paragraph(f"Сбор долгов: только {cash_pct}% — критически низко", S_red)] if cash_pct < 30 else
         [Paragraph(f"Сбор долгов: {cash_pct}%", S_green)])
    )

    story.append(two_col(left_items, right_items))
    story.append(Spacer(1, 10))

    # ── Топ должников ─────────────────────────────────────────────────────────
    story += sec(f"ТОП ДОЛЖНИКОВ (итого {_fmtM(total_cust_debt)} сум)")
    debt_data = [[
        Paragraph('#', S_muted),
        Paragraph('Клиент', S_bold),
        Paragraph('Телефон', S_bold),
        Paragraph('Долг', S_bold),
    ]]
    for i, c in enumerate(top_debtors, 1):
        debt_data.append([
            Paragraph(str(i), S_body),
            Paragraph(c.name, S_body),
            Paragraph(c.phone or '—', S_muted),
            Paragraph(_fmt(float(c.total_debt)) + ' сум', S_red),
        ])
    t = Table(debt_data, colWidths=[8*mm, W*0.36, W*0.30, W*0.22])
    t.setStyle(TableStyle([
        ('BACKGROUND',     (0,0),(-1,0),  CB('#f0f2f5')),
        ('FONTNAME',       (0,0),(-1,0),  B),
        ('FONTSIZE',       (0,0),(-1,-1), 8),
        ('TOPPADDING',     (0,0),(-1,-1), 4),
        ('BOTTOMPADDING',  (0,0),(-1,-1), 4),
        ('ROWBACKGROUNDS', (0,1),(-1,-1), [colors.white, CB('#fafafa')]),
        ('GRID',           (0,0),(-1,-1), .4, BORD),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))

    # ── Два графика рядом: недели + топ товаров ───────────────────────────────
    half = W * 0.5 - 3*mm

    left_chart = (
        [Paragraph("АКТИВНОСТЬ ПРОДАЖ — ПО НЕДЕЛЯМ", ps('lch', B, 8, '#1a1a1a'))] +
        [weekly_bar(weekly_sorted, half)]
    )

    right_chart = (
        [Paragraph("ТОП ТОВАРОВ ПО ВЫРУЧКЕ (МЛН. СУМ)", ps('rch', B, 8, '#1a1a1a'))] +
        [hbar_products(top_products_raw, half)]
    )
    if weekly_sorted and top_products_raw:
        story.append(two_col(left_chart, right_chart))
        story.append(Spacer(1, 10))
    elif weekly_sorted:
        story += sec("АКТИВНОСТЬ ПРОДАЖ — ПО НЕДЕЛЯМ")
        story.append(weekly_bar(weekly_sorted))
        story.append(Spacer(1, 10))

    # ── Замороженные деньги ───────────────────────────────────────────────────
    story += sec("ЗАМОРОЖЕННЫЕ ДЕНЬГИ В ТОВАРЕ")
    story.append(money_tbl([
        ('Товар без единой продажи',  _fmt(frozen_value) + ' сум', True, '#e08030'),
        ('Итого заморожено',          _fmt(frozen_value) + ' сум', True, '#cc2222'),
    ]))
    story.append(Spacer(1, 4))

    if frozen:
        story.append(Paragraph("МАСЛА, КОТОРЫЕ НИ РАЗУ НЕ ПРОДАВАЛИСЬ:", ps('fh', B, 8, '#e08030')))
        story.append(Spacer(1, 3))
        for p in frozen[:8]:
            val = float(p.purchase_price) * p.current_stock
            story.append(Paragraph(
                f"<b>{p.name}</b>",
                ps(f'fn{p.id}', B, 8.5, '#1a1a1a')))
            story.append(Paragraph(
                f"Артикул {p.sku} · {p.current_stock} шт на складе · цена закупки {_fmt(float(p.purchase_price))} сум/шт",
                S_muted))
            story.append(Paragraph(
                _fmtM(val),
                ps(f'fv{p.id}', B, 9, '#e08030', align=2)))
            story.append(hr('#f0f2f5'))

    # ── Итоговый вывод ────────────────────────────────────────────────────────
    story.append(Spacer(1, 8))
    story.append(hr('#1a6b3c', thick=1.5))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Главный вывод: Из {_fmtM(net_revenue)} чистой выручки реальными деньгами получено "
        f"только {_fmtM(cash_in_hand)} ({cash_pct}%) — остальное у клиентов в долг.  |  "
        f"Сами должны поставщику {_fmtM(total_sup_debt)}.  |  "
        f"В непроданном товаре заморожено {_fmtM(frozen_value)}.  |  "
        f"Наценка стабильна — маржа {margin_pct}%",
        ParagraphStyle('foot', fontName=R, fontSize=8,
                       textColor=CB('#1a1a1a'),
                       backColor=CB('#f0faf4'),
                       borderPad=8, leading=13)
    ))

    doc.build(story)
    return buf.getvalue()


# ── Эндпоинт ──────────────────────────────────────────────────────────────────
@router.get("/pdf-report")
def download_pdf_report(
    db: Session = Depends(get_db),
    _: int = Depends(get_current_user),
):
    pdf_bytes = build_pdf(db)
    filename  = f"tradi_report_{date.today().strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )