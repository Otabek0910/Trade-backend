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
    Expense, AuditLog, SupplierPayment
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Создаём таблицы...")
    try:
        Base.metadata.create_all(bind=engine)

        migrations = [
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS photo_url VARCHAR",
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS photo_url VARCHAR",
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS lat FLOAT",
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS lng FLOAT",
            "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS lat FLOAT",
            "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS lng FLOAT",
            "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS photo_url VARCHAR",
            "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS is_reverted BOOLEAN DEFAULT FALSE",
            "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS reverted_at TIMESTAMPTZ",
            "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS reverted_by INTEGER",
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
            """DO $$ BEGIN
                CREATE TYPE userstatus AS ENUM ('pending', 'active', 'blocked');
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$""",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS status userstatus NOT NULL DEFAULT 'active'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
            """UPDATE users SET status = CASE
                WHEN is_active = FALSE THEN 'blocked'::userstatus
                ELSE 'active'::userstatus
            END WHERE status = 'active'""",
            """CREATE TABLE IF NOT EXISTS debt_payments (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                amount NUMERIC(12,2) NOT NULL,
                note VARCHAR,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS brand VARCHAR",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS unit VARCHAR DEFAULT 'шт'",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS unit_value FLOAT",
            # Долг поставщику
            "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS total_debt NUMERIC(12,2) NOT NULL DEFAULT 0",
            "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(12,2) NOT NULL DEFAULT 0",
            "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS debt NUMERIC(12,2) NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS supplier_payments (
                id SERIAL PRIMARY KEY,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                amount NUMERIC(12,2) NOT NULL,
                note VARCHAR,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""",
        ]

        with engine.connect() as conn:
            for sql in migrations:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                except Exception as e:
                    print(f"⚠️ Миграция пропущена: {e}")
                    conn.rollback()

        print("✅ Таблицы и миграции готовы!")
    except Exception as e:
        print(f"❌ Ошибка при старте БД: {e}")
        print("⚠️ Приложение запускается без миграций")

    # Папки для загрузок
    os.makedirs("uploads/products", exist_ok=True)
    os.makedirs("uploads/customers", exist_ok=True)
    os.makedirs("uploads/suppliers", exist_ok=True)

    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://trade-frontend-s36l.onrender.com",
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ],
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
from app.api.v1.media import router as media_router

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
app.include_router(media_router)

@app.get("/")
async def root():
    return {"status": "✅ Работает!"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)