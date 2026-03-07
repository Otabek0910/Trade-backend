from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.user import User, UserRole

router = APIRouter(prefix="/test", tags=["Тест"])

@router.get("/create-test-user")   # ← изменил на GET
def create_test_user(db: Session = Depends(get_db)):
    telegram_id = 123456789
    
    existing = db.query(User).filter(User.telegram_id == telegram_id).first()
    if existing:
        return {"message": "Пользователь уже существует", "telegram_id": telegram_id}

    user = User(
        telegram_id=telegram_id,
        username="test_owner",
        full_name="Отабек Тестовый (Разработчик)",
        role=UserRole.developer,
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    return {
        "message": "✅ Тестовый пользователь создан!",
        "telegram_id": telegram_id,
        "role": "developer"
    }

@router.get("/create-test-supplier")
def create_test_supplier(db: Session = Depends(get_db)):
    from app.models.supplier import Supplier
    
    # Проверяем что не существует
    existing = db.query(Supplier).filter(Supplier.name == "Тест Поставщик").first()
    if existing:
        return {"message": "Уже существует", "id": existing.id}
    
    supplier = Supplier(
        name="Тест Поставщик",
        phone="+998901234567",
        address="г. Ташкент"
    )
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return {"message": "✅ Поставщик создан!", "id": supplier.id, "name": supplier.name}