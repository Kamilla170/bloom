import logging
from datetime import datetime, timedelta
from database import get_db
from services.ai_service import extract_watering_info
from services.reminder_service import create_plant_reminder
from utils.time_utils import get_moscow_now, format_days_ago
from config import STATE_EMOJI, STATE_NAMES

logger = logging.getLogger(__name__)

# Временное хранилище для анализов
temp_analyses = {}


async def save_analyzed_plant(user_id: int, analysis_data: dict, last_watered: datetime = None) -> dict:
    """
    Сохранение проанализированного растения (Этап 3).
    Теперь пробрасываем fertilizing_enabled, fertilizing_interval и next_watering_date.
    """
    try:
        raw_analysis = analysis_data.get("analysis", "")
        state_info = analysis_data.get("state_info", {})

        # Интервал полива
        ai_interval = analysis_data.get("watering_interval")
        if ai_interval is None:
            watering_info = extract_watering_info(raw_analysis)
            ai_interval = watering_info["interval_days"]

        ai_interval = max(3, min(28, ai_interval))
        logger.info(f"💧 Интервал полива: {ai_interval} дней")

        # Подкормка
        fertilizing_enabled = state_info.get('fertilizing_enabled', False)
        fertilizing_interval = state_info.get('fertilizing_interval')

        logger.info(f"🍽️ Подкормка: enabled={fertilizing_enabled}, interval={fertilizing_interval}")

        db = await get_db()
        plant_id = await db.save_plant(
            user_id=user_id,
            analysis=raw_analysis,
            photo_file_id=analysis_data["photo_file_id"],
            plant_name=analysis_data.get("plant_name", "Неизвестное растение")
        )

        # Устанавливаем интервалы
        await db.update_plant_watering_interval(plant_id, ai_interval)
        await db.set_base_watering_interval(plant_id, ai_interval)

        # Расчёт next_watering_date и last_watered
        today = datetime.now().date()
        next_watering_days = ai_interval

        if last_watered:
            days_since_watered = (datetime.now() - last_watered).days
            next_watering_days = max(1, ai_interval - days_since_watered)
            next_watering_date = today + timedelta(days=next_watering_days)
        else:
            # Если не указано — считаем, что полили "сейчас" концептуально
            next_watering_date = today + timedelta(days=ai_interval)

        # Записываем все поля разом
        async with db.pool.acquire() as conn:
            params = [plant_id, ai_interval, next_watering_date, fertilizing_enabled]
            sql = """
                UPDATE plants
                SET watering_interval = $2,
                    next_watering_date = $3,
                    fertilizing_enabled = $4
            """
            param_idx = 5

            if fertilizing_interval:
                sql += f", fertilizing_interval = ${param_idx}"
                params.append(fertilizing_interval)
                param_idx += 1
                # next_fertilizing_date считаем от сегодня
                sql += f", next_fertilizing_date = ${param_idx}"
                params.append(today + timedelta(days=fertilizing_interval))
                param_idx += 1

            if last_watered:
                sql += f", last_watered = ${param_idx}"
                params.append(last_watered)
                param_idx += 1

            sql += " WHERE id = $1"
            await conn.execute(sql, *params)

        # Состояние
        current_state = state_info.get('current_state', 'healthy')
        state_reason = state_info.get('state_reason', 'Первичный анализ AI')

        await db.update_plant_state(
            plant_id=plant_id,
            user_id=user_id,
            new_state=current_state,
            change_reason=state_reason,
            photo_file_id=analysis_data["photo_file_id"],
            ai_analysis=raw_analysis,
        )

        # Полный анализ в историю
        await db.save_full_analysis(
            plant_id=plant_id,
            user_id=user_id,
            photo_file_id=analysis_data["photo_file_id"],
            full_analysis=raw_analysis,
            confidence=analysis_data.get("confidence", 0),
            identified_species=analysis_data.get("plant_name"),
            detected_state=current_state,
            watering_advice=None,
            lighting_advice=None
        )

        # Напоминание
        await create_plant_reminder(plant_id, user_id, next_watering_days)

        plant_name = analysis_data.get("plant_name", "растение")
        state_emoji = STATE_EMOJI.get(current_state, '🌱')
        state_name = STATE_NAMES.get(current_state, 'Здоровое')

        logger.info(f"✅ Растение сохранено: {plant_name}, id={plant_id}")

        return {
            "success": True,
            "plant_id": plant_id,
            "plant_name": plant_name,
            "state": current_state,
            "state_emoji": state_emoji,
            "state_name": state_name,
            "interval": ai_interval,
            "next_watering_days": next_watering_days,
            "fertilizing_enabled": fertilizing_enabled,
            "fertilizing_interval": fertilizing_interval,
        }

    except Exception as e:
        logger.error(f"Ошибка сохранения растения: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def update_plant_state_from_photo(plant_id: int, user_id: int,
                                        photo_file_id: str, state_info: dict,
                                        raw_analysis: str) -> dict:
    """
    Обновление состояния растения по новому фото (Этап 3).
    Старое главное фото уезжает в plant_photos history.
    """
    try:
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)

        if not plant:
            return {"success": False, "error": "Растение не найдено"}

        previous_state = plant.get('current_state', 'healthy')
        new_state = state_info.get('current_state', 'healthy')
        state_reason = state_info.get('state_reason', 'Анализ AI')
        state_changed = (new_state != previous_state)

        # Старое фото → в историю (если это URL)
        old_photo = plant.get('photo_file_id')
        if old_photo and old_photo.startswith('http'):
            try:
                await db.add_plant_photo_to_history(plant_id, old_photo)
                logger.info(f"📸 Старое фото добавлено в историю растения {plant_id}")
            except Exception as e:
                logger.error(f"Ошибка сохранения фото в историю: {e}")

        # Обновляем состояние
        await db.update_plant_state(
            plant_id=plant_id,
            user_id=user_id,
            new_state=new_state,
            change_reason=state_reason,
            photo_file_id=photo_file_id,
            ai_analysis=raw_analysis,
        )

        # Обновляем главное фото и дату последнего анализа
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE plants 
                SET last_photo_analysis = CURRENT_TIMESTAMP,
                    photo_file_id = $1
                WHERE id = $2
            """, photo_file_id, plant_id)

        return {
            "success": True,
            "state_changed": state_changed,
            "previous_state": previous_state,
            "new_state": new_state,
            "plant_name": plant['display_name']
        }

    except Exception as e:
        logger.error(f"Ошибка обновления состояния: {e}")
        return {"success": False, "error": str(e)}


async def get_user_plants_list(user_id: int, limit: int = 15) -> list:
    """Получить список растений с форматированием"""
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=limit)

        formatted_plants = []

        for plant in plants:
            plant_data = {
                "id": plant.get('id'),
                "display_name": plant.get('display_name'),
                "type": plant.get('type', 'regular'),
                "emoji": '🌱',
                "photo_file_id": plant.get('photo_file_id'),
            }

            if plant.get('type') == 'growing':
                plant_data["emoji"] = '🌱'
                plant_data["stage_info"] = plant.get('stage_info', 'В процессе')
                plant_data["growing_id"] = plant.get('growing_id')
            else:
                current_state = plant.get('current_state', 'healthy')
                plant_data["emoji"] = STATE_EMOJI.get(current_state, '🌱')
                plant_data["current_state"] = current_state
                plant_data["water_status"] = format_days_ago(plant.get('last_watered'))
                # Прокидываем все новые поля Этапа 3
                plant_data["last_watered"] = plant.get('last_watered')
                plant_data["watering_interval"] = plant.get('watering_interval', 7)
                plant_data["next_watering_date"] = plant.get('next_watering_date')
                plant_data["needs_watering"] = plant.get('needs_watering', False)
                plant_data["current_streak"] = plant.get('current_streak', 0)
                plant_data["max_streak"] = plant.get('max_streak', 0)
                plant_data["fertilizing_enabled"] = plant.get('fertilizing_enabled', False)
                plant_data["fertilizing_interval"] = plant.get('fertilizing_interval')
                plant_data["last_fertilized"] = plant.get('last_fertilized')
                plant_data["next_fertilizing_date"] = plant.get('next_fertilizing_date')
                plant_data["plant_name"] = plant.get('plant_name')
                plant_data["saved_date"] = plant.get('saved_date')

            formatted_plants.append(plant_data)

        return formatted_plants

    except Exception as e:
        logger.error(f"Ошибка получения списка растений: {e}", exc_info=True)
        return []


async def water_plant(user_id: int, plant_id: int) -> dict:
    """Полив растения с расчётом серии (Этап 3)"""
    try:
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)

        if not plant:
            return {"success": False, "error": "Растение не найдено"}

        # Используем новый метод с серией
        result = await db.water_plant_with_streak(user_id, plant_id)

        if not result["success"]:
            return result

        # Пересоздаём напоминание
        await create_plant_reminder(plant_id, user_id, result["interval"])

        current_time = get_moscow_now().strftime("%d.%m.%Y в %H:%M")

        return {
            "success": True,
            "plant_name": plant['display_name'],
            "time": current_time,
            "next_watering_days": result["interval"],
            "next_watering_date": result["next_watering_date"],
            "current_streak": result["current_streak"],
            "max_streak": result["max_streak"],
        }

    except Exception as e:
        logger.error(f"Ошибка полива: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def water_all_plants(user_id: int) -> dict:
    """Полить все растения (legacy для бота)"""
    try:
        db = await get_db()
        await db.update_watering(user_id)
        return {"success": True}
    except Exception as e:
        logger.error(f"Ошибка массового полива: {e}")
        return {"success": False, "error": str(e)}


async def fertilize_plant_action(user_id: int, plant_id: int) -> dict:
    """Подкормить растение (Этап 3)"""
    try:
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)

        if not plant:
            return {"success": False, "error": "Растение не найдено"}

        result = await db.fertilize_plant(user_id, plant_id)

        if not result["success"]:
            return result

        return {
            "success": True,
            "plant_name": plant['display_name'],
            "next_fertilizing_date": result["next_fertilizing_date"],
            "interval": result["interval"],
        }

    except Exception as e:
        logger.error(f"Ошибка подкормки: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def delete_plant(user_id: int, plant_id: int) -> dict:
    try:
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)

        if not plant:
            return {"success": False, "error": "Растение не найдено"}

        plant_name = plant['display_name']
        await db.delete_plant(user_id, plant_id)
        return {"success": True, "plant_name": plant_name}

    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
        return {"success": False, "error": str(e)}


async def rename_plant(user_id: int, plant_id: int, new_name: str) -> dict:
    try:
        if len(new_name.strip()) < 2:
            return {"success": False, "error": "Слишком короткое название"}

        db = await get_db()
        await db.update_plant_name(plant_id, user_id, new_name.strip())
        return {"success": True, "new_name": new_name.strip()}

    except Exception as e:
        logger.error(f"Ошибка переименования: {e}")
        return {"success": False, "error": str(e)}


async def get_plant_details(plant_id: int, user_id: int) -> dict:
    """Получить детали растения"""
    try:
        db = await get_db()
        plant = await db.get_plant_with_state(plant_id, user_id)

        if not plant:
            return None

        plant_name = plant['display_name']
        current_state = plant.get('current_state', 'healthy')
        state_emoji = STATE_EMOJI.get(current_state, '🌱')
        state_name = STATE_NAMES.get(current_state, 'Здоровое')

        return {
            "plant_id": plant_id,
            "plant_name": plant_name,
            "current_state": current_state,
            "state_emoji": state_emoji,
            "state_name": state_name,
            "watering_interval": plant.get('watering_interval', 7),
            "state_changes_count": 0,  # legacy для совместимости с ботом
            "water_status": format_days_ago(plant.get('last_watered')),
        }

    except Exception as e:
        logger.error(f"Ошибка получения деталей: {e}")
        return None


async def get_plant_state_history(plant_id: int, limit: int = 10) -> list:
    """Заглушка для бота: после удаления plant_state_history возвращаем пустой список"""
    return []
