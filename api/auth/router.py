"""
Эндпоинты авторизации: регистрация, логин, обновление токена
"""

import logging
from fastapi import APIRouter, HTTPException, status

from database import get_db
from api.schemas import (
    RegisterRequest, LoginRequest, RefreshRequest, TokenResponse
)
from api.auth.jwt import hash_password, verify_password, create_tokens, decode_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Стартовый user_id для app-пользователей (чтобы не пересекаться с Telegram ID)
APP_USER_ID_START = 5_000_000_000


async def _get_next_app_user_id(conn) -> int:
    """Получить следующий свободный user_id для app-пользователя"""
    max_id = await conn.fetchval("""
        SELECT COALESCE(MAX(user_id), $1) FROM users WHERE user_id >= $1
    """, APP_USER_ID_START)
    return max_id + 1


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest):
    """Регистрация нового пользователя"""
    db = await get_db()

    async with db.pool.acquire() as conn:
        # Проверяем что email не занят
        existing = await conn.fetchval(
            "SELECT user_id FROM users WHERE email = $1", req.email
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Пользователь с таким email уже существует",
            )

        # Генерируем user_id для app-пользователя
        user_id = await _get_next_app_user_id(conn)

        # Создаём пользователя
        hashed = hash_password(req.password)
        await conn.execute("""
            INSERT INTO users (user_id, email, password_hash, first_name, last_activity, last_action)
            VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP, 'registered')
        """, user_id, req.email, hashed, req.first_name)

        # Создаём настройки
        await conn.execute("""
            INSERT INTO user_settings (user_id) VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)

        # Создаём подписку (free)
        await conn.execute("""
            INSERT INTO subscriptions (user_id, plan) VALUES ($1, 'free')
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)

    logger.info(f"✅ Новый app-пользователь: user_id={user_id}, email={req.email}")

    tokens = create_tokens(user_id)
    return TokenResponse(**tokens)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    """Вход по email + пароль"""
    db = await get_db()

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, password_hash FROM users WHERE email = $1",
            req.email,
        )

    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный email или пароль",
        )

    if not row["password_hash"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Этот аккаунт привязан к Telegram. Используйте бота.",
        )

    if not verify_password(req.password, row["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный email или пароль",
        )

    user_id = row["user_id"]

    # Обновляем активность
    async with (await get_db()).pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET last_activity = CURRENT_TIMESTAMP, last_action = 'app_login'
            WHERE user_id = $1
        """, user_id)

    logger.info(f"✅ App login: user_id={user_id}")

    tokens = create_tokens(user_id)
    return TokenResponse(**tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest):
    """Обновление пары токенов по refresh_token"""
    payload = decode_token(req.refresh_token)

    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невалидный refresh token",
        )

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невалидный токен",
        )

    user_id = int(user_id_str)

    # Проверяем что пользователь существует
    db = await get_db()
    async with db.pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE user_id = $1", user_id
        )

    if not exists:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь не найден",
        )

    tokens = create_tokens(user_id)
    return TokenResponse(**tokens)
