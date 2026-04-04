import json
import logging
from typing import Optional

import firebase_admin
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)

_initialized = False


def init_firebase():
    """Инициализация Firebase Admin SDK из переменной окружения"""
    global _initialized
    if _initialized:
        return

    import os
    cred_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    if not cred_json:
        logger.warning("⚠️ FIREBASE_SERVICE_ACCOUNT не задан — FCM пуши отключены")
        return

    try:
        cred_dict = json.loads(cred_json)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        _initialized = True
        logger.info("✅ Firebase Admin SDK инициализирован")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Firebase: {e}")


async def send_push_notification(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """Отправить пуш-уведомление на одно устройство"""
    if not _initialized:
        return False

    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            token=fcm_token,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    icon="ic_notification",
                    color="#2E7D32",
                    channel_id="watering_reminders",
                ),
            ),
        )

        response = messaging.send(message)
        logger.info(f"✅ FCM отправлено: {response}")
        return True

    except messaging.UnregisteredError:
        logger.warning(f"⚠️ FCM токен невалиден, удаляем: {fcm_token[:20]}...")
        await _remove_invalid_token(fcm_token)
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка FCM: {e}")
        return False


async def send_push_to_user(user_id: int, title: str, body: str, data: Optional[dict] = None) -> int:
    """Отправить пуш всем устройствам пользователя. Возвращает кол-во успешных отправок."""
    if not _initialized:
        return 0

    from database import get_db
    db = await get_db()

    async with db.pool.acquire() as conn:
        tokens = await conn.fetch(
            "SELECT fcm_token FROM user_devices WHERE user_id = $1",
            user_id,
        )

    sent = 0
    for row in tokens:
        ok = await send_push_notification(row["fcm_token"], title, body, data)
        if ok:
            sent += 1

    return sent


async def _remove_invalid_token(fcm_token: str):
    """Удалить невалидный FCM-токен из базы"""
    try:
        from database import get_db
        db = await get_db()
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_devices WHERE fcm_token = $1",
                fcm_token,
            )
    except Exception as e:
        logger.error(f"❌ Ошибка удаления токена: {e}")
