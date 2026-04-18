"""
app/api/v1/pdf_report.py  —  PDF-отчёт с поддержкой кириллицы (DejaVu Sans)
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

def _register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os
    candidates_regular = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    candidates_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    reg  = next((p for p in candidates_regular if os.path.exists(p)), None)
    bold = next((p for p in candidates_bold    if os.path.exists(p)), None)
    if reg:
        pdfmetrics.registerFont(TTFont("DejaVu",      reg))
    if bold:
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))
    return bool(reg)

def _fmt(n):
    return f"{round(n):,}".replace(",", " ")

def _fmtM(n):
    if abs(n) >= 1_000_000: return f"{n/1_000_000:.1f} млн."
    if abs(n) >= 1_000:     return f"{n/1_000:.0f}К"
    return str(round(n))

def build_pdf(db: Session) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.charts.barcharts import VerticalBarChart, HorizontalBarChart

    has_cyr = _register_fonts()
    REG  = "DejaVu"      if has_cyr else "Helvetica"
    BOLD = "DejaVu-Bold" if has_cyr else "Helvetica-Bold"

    today       = date.today()
    month_start = today.replace(day=1)
    first_sale  = db.query(func.min(cast(Sale.created_at, Date))).scalar()
    date_from   = first_sale if first_sale else month_start

    completed_sales = db.query(Sale).filter(Sale.status == SaleStatus.completed).all()
    revenue_gross   = sum(float(s.total_amount) for s in completed_sales)
    paid_total      = sum(float(s.paid_amount)   for s in completed_sales)
    cogs = sum(float(item.purchase_price_at_sale)*item.quantity for s in completed_sales for item in s.items)

    returns_all    = db.query(Return).all()
    returns_amount = sum(float(r.return_amount) for r in returns_all)
    returns_cogs   = 0.0
    for r in returns_all:
        si = db.query(SaleItem).filter(SaleItem.sale_id==r.sale_id, SaleItem.product_id==r.product_id).first()
        if si: returns_cogs += float(si.purchase_price_at_sale)*r.quantity

    net_revenue  = revenue_gross - returns_amount
    net_cogs     = cogs - returns_cogs
    gross_profit = net_revenue - net_cogs
    margin_pct   = round(gross_profit/net_revenue*100,1) if net_revenue > 0 else 0
    expenses_total = float(db.query(func.sum(Expense.amount)).scalar() or 0)
    net_profit   = gross_profit - expenses_total

    total_purchased = float(db.query(func.sum(Receipt.purchase_price*Receipt.quantity)).scalar() or 0)
    total_sup_debt  = float(db.query(func.sum(Supplier.total_debt)).scalar() or 0)
    total_paid_sup  = total_purchased - total_sup_debt
    total_cust_debt = float(db.query(func.sum(Customer.total_debt)).scalar() or 0)
    cash_in_hand    = paid_total - max(0, returns_amount - total_cust_debt)
    stock_value     = float(db.query(func.sum(Product.purchase_price*Product.current_stock)).scalar() or 0)
    stock_potential = float(db.query(func.sum(Product.selling_price *Product.current_stock)).scalar() or 0)

    top_debtors = db.query(Customer).filter(Customer.total_debt>0, Customer.is_active.is_(True)).order_by(Customer.total_debt.desc()).limit(8).all()

    top_products_raw = (
        db.query(Product.name, func.sum(SaleItem.selling_price*SaleItem.quantity).label("rev"), func.sum(SaleItem.quantity).label("qty"))
        .join(SaleItem, SaleItem.product_id==Product.id)
        .join(Sale, and_(Sale.id==SaleItem.sale_id, Sale.status==SaleStatus.completed))
        .group_by(Product.name).order_by(func.sum(SaleItem.selling_price*SaleItem.quantity).desc()).limit(8).all()
    )

    from collections import defaultdict
    weekly: dict = defaultdict(float)
    for s in completed_sales:
        if s.created_at:
            d = s.created_at.date()
            weekly[(d - timedelta(days=d.weekday())).strftime("%d.%m")] += float(s.total_amount)
    weekly_sorted = sorted(weekly.items())[-8:]

    sold_ids = {item.product_id for s in completed_sales for item in s.items}
    frozen   = db.query(Product).filter(Product.current_stock>0, ~Product.id.in_(sold_ids or {-1})).all()
    frozen_value = sum(float(p.purchase_price)*p.current_stock for p in frozen)

    CB = colors.HexColor
    BG = CB('#f8f9fa'); BORDER = CB('#e8eaed')

    def ps(name, fn=None, size=9, col='#333333', align=0, leading=13):
        return ParagraphStyle(name, fontName=fn or REG, fontSize=size, textColor=CB(col), alignment=align, leading=leading)

    S  = ps; Sb = lambda n,s=9,c='#1a1a1a': ps(n, BOLD, s, c)
    S_title = Sb('t', 17, '#1a1a1a'); S_title.leading = 21
    S_sub   = ps('sub', size=8, col='#888888')
    S_h2    = Sb('h2', 10, '#1a1a1a'); S_h2.spaceBefore=6; S_h2.spaceAfter=3
    S_body  = ps('body', size=8.5)
    S_bold  = Sb('sb', 8.5)
    S_muted = ps('mu', size=7.5, col='#888888')
    S_red   = Sb('rd', 8.5, '#cc2222')
    S_green = Sb('gr', 8.5, '#1a6b3c')

    buf = BytesIO()
    W   = A4[0] - 30*mm
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15*mm, rightMargin=15*mm, topMargin=12*mm, bottomMargin=12*mm)

    def hr(): return HRFlowable(width=W, thickness=0.5, color=BORDER, spaceAfter=4)
    def sec(t): return [Spacer(1,6), Paragraph(t, S_h2), hr()]

    def kpi(items):
        n = len(items); cw = W/n
        top = [Paragraph(v, ParagraphStyle('kv', fontName=BOLD, fontSize=15, textColor=CB(c), leading=18)) for _,v,c in items]
        bot = [Paragraph(l, S_muted) for l,_,_ in items]
        t = Table([top,bot], colWidths=[cw]*n)
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),BG),('BOX',(0,0),(-1,-1),.5,BORDER),('INNERGRID',(0,0),(-1,-1),.5,BORDER),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),('LEFTPADDING',(0,0),(-1,-1),10)]))
        return t

    def money(rows, hl=False):
        data = []
        for lbl,val,bold,col in rows:
            fn = BOLD if bold else REG
            data.append([
                Paragraph(lbl, ParagraphStyle('l',fontName=fn,fontSize=8.5,textColor=CB('#1a1a1a'),leading=12)),
                Paragraph(val, ParagraphStyle('v',fontName=fn,fontSize=8.5,textColor=CB(col),leading=12,alignment=2)),
            ])
        t = Table(data, colWidths=[W*.64, W*.34])
        sty = [('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3),('LINEBELOW',(0,-1),(-1,-1),.5,BORDER)]
        if hl: sty.append(('BACKGROUND',(0,-1),(-1,-1),CB('#eafaf1')))
        t.setStyle(TableStyle(sty))
        return t

    def two_col(l, r):
        t = Table([[l,r]], colWidths=[W*.49,W*.49])
        t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0)]))
        return t

    def wbar(pairs):
        if not pairs: return Spacer(1,1)
        labels=[p[0] for p in pairs]; vals=[p[1] for p in pairs]
        d = Drawing(W, 95); bc = VerticalBarChart()
        bc.x=35; bc.y=22; bc.width=W-50; bc.height=65
        bc.data=[vals]; bc.bars[0].fillColor=CB('#2481cc'); bc.bars[0].strokeColor=None
        bc.categoryAxis.categoryNames=labels; bc.categoryAxis.labels.fontSize=7; bc.categoryAxis.labels.fontName=REG
        bc.valueAxis.labels.fontSize=7; bc.valueAxis.labels.fontName=REG; bc.valueAxis.forceZero=True
        bc.valueAxis.labelTextFormat=lambda v: _fmtM(v) if v>0 else ''
        d.add(bc); return d

    def hbar(prods):
        if not prods: return Spacer(1,1)
        names=[p.name[:28] for p in prods]; vals=[float(p.rev) for p in prods]
        h=max(55, len(names)*13); d=Drawing(W, h+10); bc=HorizontalBarChart()
        bc.x=105; bc.y=5; bc.width=W-120; bc.height=h
        bc.data=[vals]; bc.bars[0].fillColor=CB('#1a6b3c'); bc.bars[0].strokeColor=None
        bc.categoryAxis.categoryNames=names; bc.categoryAxis.labels.fontSize=7; bc.categoryAxis.labels.fontName=REG
        bc.valueAxis.labels.fontSize=7; bc.valueAxis.labels.fontName=REG; bc.valueAxis.forceZero=True
        bc.valueAxis.labelTextFormat=lambda v: _fmtM(v) if v>0 else ''
        d.add(bc); return d

    story = [
        Paragraph("Отчёт по продажам — TRADI", S_title),
        Paragraph(f"Период: с {date_from.strftime('%d.%m.%Y')} по {today.strftime('%d.%m.%Y')} · Сформировано: {today.strftime('%d %B %Y')}", S_sub),
        Spacer(1,8),
        kpi([('Вложили (закупки)',_fmtM(total_purchased),'#7a3b8c'),('Чистая выручка',_fmtM(net_revenue),'#1a6b3c'),('В кассе реально',_fmtM(cash_in_hand),'#2481cc'),(f'Прибыль {margin_pct}%',_fmtM(net_profit),'#34c759' if net_profit>=0 else '#cc2222'),('Долг поставщику',_fmtM(total_sup_debt),'#ff3b30')]),
        Spacer(1,10),
        two_col(
            [Paragraph("ЗАКУПКИ У ПОСТАВЩИКА", S_bold), Spacer(1,4), money([('Куплено товара всего',_fmt(total_purchased)+' сум',False,'#1a1a1a'),('Уже оплатили',_fmt(total_paid_sup)+' сум',False,'#1a6b3c'),('Ещё должны поставщику',_fmt(total_sup_debt)+' сум',True,'#cc2222')])],
            [Paragraph("ТОВАР НА СКЛАДЕ СЕЙЧАС", S_bold), Spacer(1,4), money([('Стоимость по закупке',_fmt(stock_value)+' сум',False,'#1a1a1a'),('Потенциал по продажам',_fmt(stock_potential)+' сум',False,'#7a3b8c'),('Заморожено (нет продаж)',_fmt(frozen_value)+' сум',True,'#e08030')])]
        ),
        Spacer(1,10),
    ]

    story += sec("ВЫРУЧКА И ПРИБЫЛЬ")
    story.append(money([('Продажи (завершённые сделки)',_fmt(revenue_gross)+' сум',False,'#1a1a1a'),('Возвраты','−'+_fmt(returns_amount)+' сум',False,'#e08030'),('= Чистая выручка',_fmt(net_revenue)+' сум',True,'#1a6b3c'),('Минус себестоимость','−'+_fmt(net_cogs)+' сум',False,'#555555'),(f'= Валовая прибыль  {margin_pct}%',_fmt(gross_profit)+' сум',True,'#34c759'),('Минус расходы','−'+_fmt(expenses_total)+' сум',False,'#e05555'),('Чистая прибыль',_fmt(net_profit)+' сум',True,'#34c759' if net_profit>=0 else '#cc2222')], hl=True))
    story.append(Spacer(1,10))

    story += sec("ГДЕ СЕЙЧАС ДЕНЬГИ?")
    story.append(money([('Чистая выручка',_fmt(net_revenue)+' сум',False,'#1a1a1a'),('Клиенты не заплатили','−'+_fmt(total_cust_debt)+' сум',False,'#cc2222'),('Фактически в кассе',_fmt(cash_in_hand)+' сум',True,'#2481cc')]))
    if net_revenue > 0:
        pct = round(cash_in_hand/net_revenue*100,1)
        story.append(Paragraph(f"Сбор долгов: {pct}% — {'критически низко' if pct<30 else 'нормально'}", S_red if pct<30 else S_green))
    story.append(Spacer(1,10))

    if top_debtors:
        story += sec(f"ТОП ДОЛЖНИКОВ  (итого {_fmtM(total_cust_debt)} сум)")
        rows = [[Paragraph('#',S_muted),Paragraph('Клиент',S_bold),Paragraph('Телефон',S_bold),Paragraph('Долг',S_bold)]]
        for i,c in enumerate(top_debtors,1):
            rows.append([Paragraph(str(i),S_body),Paragraph(c.name,S_body),Paragraph(c.phone or '—',S_body),Paragraph(_fmt(float(c.total_debt))+' сум',S_red)])
        t = Table(rows, colWidths=[8*mm,W*.33,W*.30,W*.24])
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),CB('#f0f2f5')),('FONTNAME',(0,0),(-1,0),BOLD),('FONTSIZE',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,CB('#fafafa')]),('GRID',(0,0),(-1,-1),.4,BORDER)]))
        story.append(t); story.append(Spacer(1,10))

    if weekly_sorted:
        story += sec("АКТИВНОСТЬ ПРОДАЖ — ПО НЕДЕЛЯМ")
        story.append(wbar(weekly_sorted)); story.append(Spacer(1,10))

    if top_products_raw:
        story += sec("ТОП ТОВАРОВ ПО ВЫРУЧКЕ")
        story.append(hbar(top_products_raw)); story.append(Spacer(1,10))

    story += sec("ЗАМОРОЖЕННЫЕ ДЕНЬГИ В ТОВАРЕ")
    story.append(money([('Товар без единой продажи',_fmt(frozen_value)+' сум',True,'#e08030')]))
    if frozen:
        fp = [[Paragraph('SKU',S_bold),Paragraph('Название',S_bold),Paragraph('Шт.',S_bold),Paragraph('Стоимость',S_bold)]]
        for p in frozen[:12]:
            fp.append([Paragraph(p.sku,S_body),Paragraph(p.name[:32],S_body),Paragraph(str(p.current_stock),S_body),Paragraph(_fmt(float(p.purchase_price)*p.current_stock)+' сум',S_body)])
        t = Table(fp, colWidths=[20*mm,W*.43,10*mm,W*.25])
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),CB('#fff3e0')),('FONTSIZE',(0,0),(-1,-1),7.5),('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3),('GRID',(0,0),(-1,-1),.4,BORDER)]))
        story.append(Spacer(1,4)); story.append(t)

    cash_pct = round(cash_in_hand/net_revenue*100,1) if net_revenue>0 else 0
    story += [
        Spacer(1,10),
        HRFlowable(width=W, thickness=1.5, color=CB('#1a6b3c')),
        Spacer(1,4),
        Paragraph(f"Главный вывод: Из {_fmtM(net_revenue)} чистой выручки реальными деньгами получено {_fmtM(cash_in_hand)} ({cash_pct}%) — остальное у клиентов в долг.  |  Сами должны поставщику {_fmtM(total_sup_debt)}.  |  В непроданном товаре заморожено {_fmtM(frozen_value)}.  |  Маржа {margin_pct}%",
                  ParagraphStyle('foot',fontName=REG,fontSize=8,textColor=CB('#1a1a1a'),backColor=CB('#eafaf1'),borderPad=8,leading=13)),
    ]

    doc.build(story)
    return buf.getvalue()


@router.get("/pdf-report")
def download_pdf_report(db: Session = Depends(get_db), _: int = Depends(get_current_user)):
    pdf_bytes = build_pdf(db)
    filename  = f"tradi_report_{date.today().strftime('%Y%m%d')}.pdf"
    return StreamingResponse(BytesIO(pdf_bytes), media_type="application/pdf",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})