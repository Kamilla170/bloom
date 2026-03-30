"""
Сервис загрузки фото в Cloudinary
"""

import os
import logging
import hashlib
import cloudinary
import cloudinary.uploader

logger = logging.getLogger(__name__)

# Инициализация Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)


async def upload_plant_photo(image_bytes: bytes, user_id: int, plant_name: str = "plant") -> str | None:
    """
    Загрузить фото растения в Cloudinary.
    
    Сжимает до 800px, quality 75%.
    Возвращает публичный URL или None при ошибке.
    """
    try:
        # Генерируем уникальный public_id
        image_hash = hashlib.md5(image_bytes[:1024]).hexdigest()[:8]
        public_id = f"bloom/user_{user_id}/{plant_name}_{image_hash}"

        result = cloudinary.uploader.upload(
            image_bytes,
            public_id=public_id,
            folder="bloom_plants",
            overwrite=True,
            resource_type="image",
            transformation=[
                {"width": 800, "height": 800, "crop": "limit", "quality": 75}
            ],
        )

        url = result.get("secure_url")
        logger.info(f"✅ Фото загружено в Cloudinary: {url}")
        return url

    except Exception as e:
        logger.error(f"❌ Ошибка загрузки в Cloudinary: {e}", exc_info=True)
        return None


def get_photo_url(photo_field: str | None, width: int = 400) -> str | None:
    """
    Получить URL фото с нужным размером.
    
    Если photo_field — уже URL (Cloudinary), подставляем трансформацию.
    Если photo_field — Telegram file_id, возвращаем None (не можем показать).
    """
    if not photo_field:
        return None

    if photo_field.startswith("http"):
        # Cloudinary URL — подставляем размер на лету
        # https://res.cloudinary.com/xxx/image/upload/v123/folder/file.jpg
        # → https://res.cloudinary.com/xxx/image/upload/w_400,h_400,c_fill,q_auto/v123/folder/file.jpg
        if "/upload/" in photo_field:
            return photo_field.replace(
                "/upload/",
                f"/upload/w_{width},h_{width},c_fill,q_auto/"
            )
        return photo_field

    # Telegram file_id — не можем показать в приложении
    return None
