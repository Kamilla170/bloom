import logging
from datetime import datetime, timedelta
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import STATE_EMOJI, STATE_NAMES
from utils.time_utils import get_moscow_now
from database import get_db
from keyboards.plant_menu import watering_reminder_actions

logger = logging.getLogger(__name__)


async def check_and_send_reminders(bot):
    """Проверка и отправка всех напоминаний"""
    try:
        logger.info("=" * 60)
        logger.info("🔔 НАЧАЛО ПРОВЕРКИ НАПОМИНАНИЙ")
        logger.info(f"🕐 Текущее время (МСК): {get_moscow_now()}")
        logger.info("=" * 60)
        
        await send_watering_reminders(bot)
        await send_growing_reminders(bot)
        
        logger.info("=" * 60)
        logger.info("✅ ПРОВЕРКА НАПОМИНАНИЙ ЗАВЕРШЕНА")
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА проверки напоминаний: {e}", exc_info=True)


async def send_watering_reminders(bot):
    """Отправка напоминаний о поливе"""
    try:
        db = await get_db()
        moscow_now = get_moscow_now()
        moscow_date = moscow_now.date()
        
        logger.info("")
        logger.info("💧 ПРОВЕРКА НАПОМИНАНИЙ О ПОЛИВЕ")
        logger.info(f"📅 Дата проверки: {moscow_date}")
        
        async with db.pool.acquire() as conn:
            # Сначала проверим, есть ли вообще растения с напоминаниями
            total_plants = await conn.fetchval("""
                SELECT COUNT(*) FROM plants p
                JOIN reminders r ON r.plant_id = p.id AND r.reminder_type = 'watering' AND r.is_active = TRUE
                WHERE p.plant_type = 'regular'
            """)
            logger.info(f"📊 Всего растений с активными напоминаниями: {total_plants}")
            
            # ИСПРАВЛЕНО: Используем next_date из reminders
            plants_to_water = await conn.fetch("""
                SELECT p.id, p.user_id, 
                       COALESCE(p.custom_name, p.plant_name, 'Растение #' || p.id) as display_name,
                       p.last_watered, 
                       COALESCE(p.watering_interval, 5) as watering_interval, 
                       p.photo_file_id, p.notes, p.current_state, p.growth_stage,
                       r.id as reminder_id,
                       r.next_date,
                       r.last_sent,
                       us.reminder_enabled as user_reminder_enabled,
                       p.reminder_enabled as plant_reminder_enabled
                FROM plants p
                JOIN user_settings us ON p.user_id = us.user_id
                JOIN reminders r ON r.plant_id = p.id 
                                AND r.reminder_type = 'watering' 
                                AND r.is_active = TRUE
                WHERE p.reminder_enabled = TRUE 
                  AND us.reminder_enabled = TRUE
                  AND p.plant_type = 'regular'
                  AND r.next_date::date <= $1::date
                  AND (r.last_sent IS NULL OR r.last_sent::date < $1::date)
                ORDER BY r.next_date ASC
            """, moscow_date)
            
            logger.info(f"🔍 Найдено растений для напоминания: {len(plants_to_water)}")
            
            if len(plants_to_water) > 0:
                logger.info("📋 СПИСОК РАСТЕНИЙ ДЛЯ НАПОМИНАНИЙ:")
                for i, plant in enumerate(plants_to_water, 1):
                    logger.info(f"   {i}. ID={plant['id']}, User={plant['user_id']}, "
                              f"Название='{plant['display_name']}', "
                              f"NextDate={plant['next_date']}, "
                              f"LastSent={plant['last_sent']}")
            else:
                logger.info("✅ Нет растений требующих напоминания на эту дату")
            
            sent_count = 0
            error_count = 0
            
            for plant in plants_to_water:
                try:
                    await send_single_watering_reminder(bot, plant)
                    sent_count += 1
                except Exception as e:
                    error_count += 1
                    logger.error(f"❌ Ошибка отправки напоминания для растения {plant['id']}: {e}")
            
            logger.info(f"📊 ИТОГО: Отправлено {sent_count}, Ошибок {error_count}")
                
    except Exception as e:
        logger.error(f"❌ ОШИБКА send_watering_reminders: {e}", exc_info=True)


async def send_single_watering_reminder(bot, plant_row):
    """Отправка одного напоминания о поливе"""
    try:
        user_id = plant_row['user_id']
        plant_id = plant_row['id']
        plant_name = plant_row['display_name']
        current_state = plant_row.get('current_state', 'healthy')
        
        moscow_now = get_moscow_now()
        
        if plant_row['last_watered']:
            days_ago = (moscow_now.date() - plant_row['last_watered'].date()).days
            if days_ago == 0:
                time_info = f"Последний полив был сегодня"
            elif days_ago == 1:
                time_info = f"Последний полив был вчера"
            else:
                time_info = f"Последний полив был {days_ago} дней назад"
        else:
            time_info = "Растение еще ни разу не поливали"
        
        state_emoji = STATE_EMOJI.get(current_state, '🌱')
        state_name = STATE_NAMES.get(current_state, 'Здоровое')
        
        message_text = f"💧 <b>Время полить растение!</b>\n\n"
        message_text += f"{state_emoji} <b>{plant_name}</b>\n"
        message_text += f"📊 Состояние: {state_name}\n"
        message_text += f"⏰ {time_info}\n\n"
        
        # Рекомендации по состоянию
        if current_state == 'flowering':
            message_text += f"💐 Растение цветет - поливайте чаще!\n"
        elif current_state == 'dormancy':
            message_text += f"😴 Период покоя - поливайте реже\n"
        elif current_state == 'stress':
            message_text += f"⚠️ Растение в стрессе - проверьте влажность почвы!\n"
        
        interval = plant_row.get('watering_interval', 5)
        message_text += f"\n⏱️ Интервал: каждые {interval} дней"
        
        keyboard = watering_reminder_actions(plant_id)
        
        logger.info(f"📤 Отправка напоминания: User={user_id}, Plant='{plant_name}' (ID={plant_id})")
        
        await bot.send_photo(
            chat_id=user_id,
            photo=plant_row['photo_file_id'],
            caption=message_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        
        # Обновляем last_sent и планируем следующее напоминание
        db = await get_db()
        moscow_now_naive = moscow_now.replace(tzinfo=None)
        next_reminder = moscow_now + timedelta(days=interval)
        next_reminder_naive = next_reminder.replace(tzinfo=None)
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE reminders
                SET last_sent = $1,
                    send_count = COALESCE(send_count, 0) + 1,
                    next_date = $2
                WHERE id = $3
            """, moscow_now_naive, next_reminder_naive, plant_row['reminder_id'])
        
        logger.info(f"✅ Напоминание отправлено успешно! Следующее: {next_reminder.date()}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки напоминания для растения {plant_row.get('id')}: {e}", exc_info=True)
        raise  # Пробрасываем ошибку выше для подсчета


async def send_growing_reminders(bot):
    """Отправка напоминаний по выращиванию"""
    try:
        db = await get_db()
        moscow_now = get_moscow_now()
        
        logger.info("")
        logger.info("🌱 ПРОВЕРКА НАПОМИНАНИЙ ПО ВЫРАЩИВАНИЮ")
        
        async with db.pool.acquire() as conn:
            reminders = await conn.fetch("""
                SELECT r.id as reminder_id, r.task_day, r.stage_number,
                       gp.id as growing_id, gp.user_id, gp.plant_name, 
                       gp.task_calendar, gp.current_stage, gp.started_date,
                       gp.photo_file_id
                FROM reminders r
                JOIN growing_plants gp ON r.growing_plant_id = gp.id
                JOIN user_settings us ON gp.user_id = us.user_id
                WHERE r.reminder_type = 'task'
                  AND r.is_active = TRUE
                  AND us.reminder_enabled = TRUE
                  AND gp.status = 'active'
                  AND r.next_date::date <= $1::date
                  AND (r.last_sent IS NULL OR r.last_sent::date < $1::date)
            """, moscow_now.date())
            
            logger.info(f"🔍 Найдено напоминаний по выращиванию: {len(reminders)}")
            
            for reminder in reminders:
                await send_task_reminder(bot, reminder)
                
    except Exception as e:
        logger.error(f"❌ ОШИБКА send_growing_reminders: {e}", exc_info=True)


async def send_task_reminder(bot, reminder_row):
    """Отправка напоминания о задаче"""
    try:
        user_id = reminder_row['user_id']
        growing_id = reminder_row['growing_id']
        plant_name = reminder_row['plant_name']
        task_day = reminder_row['task_day']
        
        message_text = f"🌱 <b>Задача по выращиванию</b>\n\n"
        message_text += f"<b>{plant_name}</b>\n"
        message_text += f"📅 День {task_day}\n"
        message_text += f"\n📋 Проверьте задачи на сегодня!"
        
        keyboard = [
            [InlineKeyboardButton(text="✅ Выполнено!", callback_data=f"task_done_{growing_id}_{task_day}")],
            [InlineKeyboardButton(text="📸 Добавить фото", callback_data=f"add_diary_photo_{growing_id}")],
        ]
        
        if reminder_row['photo_file_id']:
            await bot.send_photo(
                chat_id=user_id,
                photo=reminder_row['photo_file_id'],
                caption=message_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        else:
            await bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        
        # Отмечаем как отправленное
        db = await get_db()
        moscow_now = get_moscow_now().replace(tzinfo=None)
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE reminders
                SET last_sent = $1,
                    send_count = COALESCE(send_count, 0) + 1
                WHERE id = $2
            """, moscow_now, reminder_row['reminder_id'])
        
        logger.info(f"🌱 Напоминание о задаче отправлено: {plant_name} (пользователь {user_id})")
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки задачи: {e}", exc_info=True)


async def create_plant_reminder(plant_id: int, user_id: int, interval_days: int = 5):
    """Создать напоминание о поливе"""
    try:
        db = await get_db()
        moscow_now = get_moscow_now()
        next_watering = moscow_now + timedelta(days=interval_days)
        next_watering_naive = next_watering.replace(tzinfo=None)
        
        async with db.pool.acquire() as conn:
            # Деактивируем все старые напоминания для этого растения
            deactivated = await conn.fetchval("""
                UPDATE reminders 
                SET is_active = FALSE 
                WHERE user_id = $1 
                AND plant_id = $2 
                AND reminder_type = 'watering'
                AND is_active = TRUE
                RETURNING id
            """, user_id, plant_id)
            
            if deactivated:
                logger.info(f"⚙️ Деактивировано старое напоминание для растения {plant_id}")
            
            # Создаем новое напоминание
            reminder_id = await conn.fetchval("""
                INSERT INTO reminders (user_id, plant_id, reminder_type, next_date, is_active)
                VALUES ($1, $2, 'watering', $3, TRUE)
                RETURNING id
            """, user_id, plant_id, next_watering_naive)
        
        logger.info(f"✅ Создано напоминание ID={reminder_id} для растения {plant_id} (user {user_id}) на {next_watering.date()} (через {interval_days} дней)")
        
    except Exception as e:
        logger.error(f"❌ Ошибка создания напоминания для растения {plant_id}: {e}", exc_info=True)
        raise


async def check_monthly_photo_reminders(bot):
    """Проверка месячных напоминаний об обновлении фото"""
    try:
        logger.info("")
        logger.info("📸 ПРОВЕРКА МЕСЯЧНЫХ НАПОМИНАНИЙ")
        
        db = await get_db()
        plants = await db.get_plants_for_monthly_reminder()
        
        logger.info(f"🔍 Найдено {len(plants)} растений для месячного напоминания")
        
        # Группируем по пользователям
        users_plants = {}
        for plant in plants:
            user_id = plant['user_id']
            if user_id not in users_plants:
                users_plants[user_id] = []
            users_plants[user_id].append(plant)
        
        # Отправляем по одному сообщению на пользователя
        for user_id, user_plants in users_plants.items():
            await send_monthly_photo_reminder(bot, user_id, user_plants)
            await db.mark_monthly_reminder_sent(user_id)
        
    except Exception as e:
        logger.error(f"❌ Ошибка месячных напоминаний: {e}", exc_info=True)


async def send_monthly_photo_reminder(bot, user_id: int, plants: list):
    """Отправить месячное напоминание об обновлении фото"""
    try:
        if not plants:
            return
        
        plants_text = ""
        for i, plant in enumerate(plants[:5], 1):
            plant_name = plant.get('custom_name') or plant.get('plant_name') or f"Растение #{plant['id']}"
            days_ago = (get_moscow_now() - plant['last_photo_analysis']).days
            current_state = STATE_EMOJI.get(plant.get('current_state', 'healthy'), '🌱')
            plants_text += f"{i}. {current_state} {plant_name} (фото {days_ago} дней назад)\n"
        
        if len(plants) > 5:
            plants_text += f"...и еще {len(plants) - 5} растений\n"
        
        message_text = f"""
📸 <b>Время обновить фото ваших растений!</b>

Прошел месяц с последнего обновления:

{plants_text}

💡 <b>Зачем это нужно?</b>
• Отслеживание изменений и роста
• Своевременное выявление проблем
• История развития ваших растений
• Корректировка ухода по состоянию

📷 <b>Что делать:</b>
Просто пришлите новое фото каждого растения!
"""
        
        keyboard = [
            [InlineKeyboardButton(text="🌿 К моей коллекции", callback_data="my_plants")],
            [InlineKeyboardButton(text="⏰ Напомнить через неделю", callback_data="snooze_monthly_reminder")],
            [InlineKeyboardButton(text="🔕 Отключить", callback_data="disable_monthly_reminders")],
        ]
        
        await bot.send_message(
            chat_id=user_id,
            text=message_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        logger.info(f"📸 Месячное напоминание отправлено: {user_id} ({len(plants)} растений)")
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки месячного напоминания: {e}", exc_info=True)
