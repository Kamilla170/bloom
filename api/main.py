"""
Bloom AI REST API — точка входа
Запуск: uvicorn api.main:app --host 0.0.0.0 --port 8001
"""

import os
import sys
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Добавляем корневую директорию проекта в sys.path,
# чтобы импорты вида `from database import ...` работали
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from database import init_database, get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# === Миграция: колонки для app-авторизации ===

async def run_app_migrations():
    """Добавить колонки email / password_hash в таблицу users"""
    db = await get_db()
    async with db.pool.acquire() as conn:
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT UNIQUE")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL")
    logger.info("✅ App-миграции применены (email, password_hash)")


# === Lifecycle ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация при старте, очистка при завершении"""
    logger.info("🚀 Bloom AI REST API — запуск...")
    await init_database()
    await run_app_migrations()

    # Инициализация Firebase (FCM пуши)
    from services.fcm_service import init_firebase
    init_firebase()

    logger.info("✅ API готов к работе")
    yield
    logger.info("🛑 API — завершение")
    try:
        db = await get_db()
        await db.close()
    except Exception:
        pass


# === App ===

app = FastAPI(
    title="Bloom AI API",
    description="REST API для мобильного приложения Bloom AI",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — разрешаем запросы с мобильного приложения
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене заменить на конкретные домены
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# === Роутеры ===

from api.auth.router import router as auth_router
from api.plants.router import router as plants_router
from api.ai.router import router as ai_router
from api.users.router import router as users_router
from api.payments.router import router as payments_router

app.include_router(auth_router)
app.include_router(plants_router)
app.include_router(ai_router)
app.include_router(users_router)
app.include_router(payments_router)


# === Health check ===

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "Bloom AI REST API",
        "version": "1.0.0",
    }


@app.get("/")
async def root():
    return {"message": "Bloom AI API", "docs": "/docs"}
