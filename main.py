from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import text
import os

from app.db.session import engine
from app.db.base import Base

from app.models import (
    User, Product, Supplier, Customer,
    Receipt, Sale, SaleItem, Return,
    Expense, AuditLog
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Создаём таблицы...")
    Base.metadata.create_all(bind=engine)

    # Автоматические миграции новых колонок — безопасно (IF NOT EXISTS)
    with engine.connect() as conn:
        # Products
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS photo_url VARCHAR"))
        # Customers
        conn.execute(text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS photo_url VARCHAR"))
        conn.execute(text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS lat FLOAT"))
        conn.execute(text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS lng FLOAT"))
        # Suppliers
        conn.execute(text("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS lat FLOAT"))
        conn.execute(text("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS lng FLOAT"))
        conn.commit()
        conn.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS is_reverted BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS reverted_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS reverted_by INTEGER"))
        conn.execute(text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
        # Добавляем статус пользователя
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE userstatus AS ENUM ('pending', 'active', 'blocked');
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
        """))
        conn.execute(text("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS status userstatus NOT NULL DEFAULT 'active'
        """))
        # Мигрируем старые is_active → status
        conn.execute(text("""
            UPDATE users SET status = CASE
                WHEN is_active = FALSE THEN 'blocked'::userstatus
                ELSE 'active'::userstatus
            END
            WHERE status = 'active'
        """))
        conn.commit()
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS debt_payments (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                amount NUMERIC(12,2) NOT NULL,
                note VARCHAR,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        conn.commit()

        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS brand VARCHAR"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS unit VARCHAR DEFAULT 'шт'"))
        conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS unit_value FLOAT"))
        conn.commit()

    print("✅ Таблицы и миграции готовы!")



    # Папки для загрузок
    os.makedirs("uploads/products", exist_ok=True)
    os.makedirs("uploads/customers", exist_ok=True)
    os.makedirs("uploads/suppliers", exist_ok=True)

    yield

app = FastAPI(
    title="Trade Telegram MVP",
    description="Система учёта для Telegram Web App",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Раздача статических файлов (фото товаров, клиентов, поставщиков)
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="uploads"), name="static")

# ─── Роутеры ──────────────────────────────────────────────────────────────────
from app.api.v1.auth import router as auth_router
from app.api.v1.protected import router as protected_router
from app.api.v1.test import router as test_router
from app.api.v1.products import router as products_router
from app.api.v1.receipts import router as receipts_router
from app.api.v1.suppliers import router as suppliers_router
from app.api.v1.sales import router as sales_router
from app.api.v1.customers import router as customers_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.export import router as export_router
from app.api.v1.users import router as users_router
from app.api.v1.expenses import router as expenses_router
from app.api.v1.returns import router as returns_router
from app.api.v1.audit import router as audit_router

app.include_router(auth_router)
app.include_router(protected_router)
app.include_router(test_router)
app.include_router(products_router)
app.include_router(receipts_router)
app.include_router(suppliers_router)
app.include_router(sales_router)
app.include_router(customers_router)
app.include_router(dashboard_router)
app.include_router(export_router)
app.include_router(users_router)
app.include_router(expenses_router)
app.include_router(returns_router)
app.include_router(audit_router)

@app.get("/")
async def root():
    return {"status": "✅ Работает!"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)