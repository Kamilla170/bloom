"""
Эндпоинты пользователя: профиль, настройки, подписка, напоминания
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from database import get_db
from api.auth.dependencies import get_current_user
from api.schemas import (
    UserProfile, UserSettings, UpdateSettingsRequest,
    PlanInfo, UsageStats, SuccessResponse, RegisterDeviceRequest,
)
from api.services.cloudinary_service import get_photo_url
from services.subscription_service import get_user_plan, get_usage_stats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["user"])


@router.get("", response_model=UserProfile)
async def get_profile(user_id: int = Depends(get_current_user)):
    """Профиль пользователя"""
    db = await get_db()

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT user_id, email, first_name, created_at,
                   plants_count, total_waterings, questions_asked
            FROM users WHERE user_id = $1
        """, user_id)

    if not row:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    return UserProfile(
        user_id=row["user_id"],
        email=row.get("email"),
        first_name=row.get("first_name"),
        created_at=row.get("created_at"),
        plants_count=row.get("plants_count", 0),
        total_waterings=row.get("total_waterings", 0),
        questions_asked=row.get("questions_asked", 0),
    )


@router.get("/settings", response_model=UserSettings)
async def get_settings(user_id: int = Depends(get_current_user)):
    """Настройки пользователя"""
    db = await get_db()
    settings = await db.get_user_reminder_settings(user_id)

    if not settings:
        return UserSettings()

    return UserSettings(
        reminder_enabled=settings.get("reminder_enabled", True),
        reminder_time=settings.get("reminder_time", "09:00"),
        monthly_photo_reminder=settings.get("monthly_photo_reminder", True),
    )


@router.patch("/settings", response_model=SuccessResponse)
async def update_settings(
    req: UpdateSettingsRequest,
    user_id: int = Depends(get_current_user),
):
    """Обновить настройки"""
    db = await get_db()

    async with db.pool.acquire() as conn:
        if req.reminder_enabled is not None:
            await conn.execute(
                "UPDATE user_settings SET reminder_enabled = $1 WHERE user_id = $2",
                req.reminder_enabled, user_id,
            )
        if req.reminder_time is not None:
            await conn.execute(
                "UPDATE user_settings SET reminder_time = $1 WHERE user_id = $2",
                req.reminder_time, user_id,
            )
        if req.monthly_photo_reminder is not None:
            await conn.execute(
                "UPDATE user_settings SET monthly_photo_reminder = $1 WHERE user_id = $2",
                req.monthly_photo_reminder, user_id,
            )

    return SuccessResponse(message="Настройки обновлены")


@router.get("/subscription", response_model=PlanInfo)
async def get_subscription(user_id: int = Depends(get_current_user)):
    """Статус подписки"""
    plan_info = await get_user_plan(user_id)
    return PlanInfo(**plan_info)


@router.get("/usage", response_model=UsageStats)
async def get_usage(user_id: int = Depends(get_current_user)):
    """Статистика использования"""
    stats = await get_usage_stats(user_id)
    return UsageStats(
        plan=stats["plan"],
        plants_count=stats["plants_count"],
        plants_limit=str(stats["plants_limit"]),
        analyses_used=stats["analyses_used"],
        analyses_limit=str(stats["analyses_limit"]),
        questions_used=stats["questions_used"],
        questions_limit=str(stats["questions_limit"]),
    )


@router.post("/device", response_model=SuccessResponse)
async def register_device(
    req: RegisterDeviceRequest,
    user_id: int = Depends(get_current_user),
):
    """Регистрация FCM-токена для пуш-уведомлений"""
    db = await get_db()

    async with db.pool.acquire() as conn:
        # Создаём таблицу при первом вызове (миграция)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_devices (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                fcm_token TEXT NOT NULL,
                platform TEXT DEFAULT 'android',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_user_devices_token
            ON user_devices (fcm_token)
        """)

        # Upsert токен
        await conn.execute("""
            INSERT INTO user_devices (user_id, fcm_token, platform, updated_at)
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (fcm_token)
            DO UPDATE SET user_id = $1, platform = $3, updated_at = CURRENT_TIMESTAMP
        """, user_id, req.fcm_token, req.platform)

    logger.info(f"📱 FCM токен зарегистрирован: user_id={user_id}, platform={req.platform}")
    return SuccessResponse(message="Устройство зарегистрировано")


@router.get("/reminders")
async def get_reminders(user_id: int = Depends(get_current_user)):
    """Список напоминаний о поливе для приложения"""
    db = await get_db()

    async with db.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                p.id as plant_id,
                COALESCE(p.custom_name, p.plant_name, 'Растение #' || p.id) as plant_name,
                p.photo_file_id,
                p.current_state,
                p.last_watered,
                COALESCE(p.watering_interval, 5) as watering_interval,
                p.reminder_enabled,
                r.next_date
            FROM plants p
            LEFT JOIN reminders r
                ON r.plant_id = p.id
                AND r.reminder_type = 'watering'
                AND r.is_active = TRUE
            WHERE p.user_id = $1
              AND p.plant_type = 'regular'
            ORDER BY r.next_date ASC NULLS LAST
        """, user_id)

    result = []
    for row in rows:
        photo_fid = row.get("photo_file_id")
        photo_url = get_photo_url(photo_fid, 200) if photo_fid else None

        result.append({
            "plant_id": row["plant_id"],
            "plant_name": row["plant_name"],
            "photo_url": photo_url,
            "current_state": row.get("current_state", "healthy"),
            "last_watered": row["last_watered"].isoformat() if row.get("last_watered") else None,
            "watering_interval": row["watering_interval"],
            "reminder_enabled": row.get("reminder_enabled", True),
            "next_date": row["next_date"].isoformat() if row.get("next_date") else None,
        })

    return result


@router.post("/test-push", response_model=SuccessResponse)
async def test_push(user_id: int = Depends(get_current_user)):
    """Отправить тестовый пуш на все устройства пользователя"""
    from services.fcm_service import send_push_to_user, is_initialized

    # Проверяем инициализацию Firebase
    if not is_initialized():
        raise HTTPException(
            status_code=500,
            detail="Firebase Admin SDK не инициализирован. Проверь переменную FIREBASE_SERVICE_ACCOUNT в Railway.",
        )

    # Проверяем наличие устройств
    db = await get_db()
    async with db.pool.acquire() as conn:
        devices_count = await conn.fetchval(
            "SELECT COUNT(*) FROM user_devices WHERE user_id = $1",
            user_id,
        )

    if devices_count == 0:
        raise HTTPException(
            status_code=400,
            detail=f"Нет зарегистрированных устройств для user_id={user_id}. Войди в приложение заново.",
        )

    sent = await send_push_to_user(
        user_id=user_id,
        title="🌱 Bloom AI",
        body="Тестовое уведомление! Пуши работают 🎉",
        data={"type": "test"},
    )

    if sent == 0:
        raise HTTPException(
            status_code=500,
            detail=f"Устройств найдено: {devices_count}, но отправить не удалось. Проверь логи Railway.",
        )

    return SuccessResponse(message=f"Отправлено на {sent} из {devices_count} устройств")
