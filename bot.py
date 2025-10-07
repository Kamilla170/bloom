import asyncio
import os
import logging
from datetime import datetime, timedelta
import json
import base64
from io import BytesIO

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web
from openai import AsyncOpenAI
from PIL import Image
from database import init_database, get_db

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)

temp_analyses = {}

# РАСШИРЕННЫЙ ПРОМПТ С ОПРЕДЕЛЕНИЕМ СОСТОЯНИЯ
PLANT_IDENTIFICATION_PROMPT = """
Вы - эксперт-ботаник. Внимательно изучите фотографию растения и дайте максимально точную идентификацию.

ВАЖНО: Анализируйте только то, что ВИДНО на фотографии. Если почва не видна - не давайте советы по поливу.

Анализируйте:
1. Форму и текстуру листьев (овальные/длинные/мясистые/глянцевые/матовые)
2. Расположение листьев на стебле
3. Цвет и прожилки листьев
4. Форму роста растения
5. Видимые цветы или плоды
6. Размер растения и горшка

ОПРЕДЕЛЕНИЕ СОСТОЯНИЯ РАСТЕНИЯ (выберите одно):
- healthy (здоровое) - нормальный рост, здоровый вид
- flowering (цветение) - активное цветение или бутоны
- active_growth (активный рост) - новые побеги, быстрый рост
- dormancy (период покоя) - замедленный рост, зимний покой
- stress (стресс/болезнь) - желтые листья, вредители, проблемы
- adaptation (адаптация) - после пересадки, смены места

ПРИЗНАКИ ДЛЯ ОПРЕДЕЛЕНИЯ СОСТОЯНИЯ:
- Цветение: бутоны, цветы, яркие цвета
- Активный рост: молодые листья, новые побеги светло-зеленого цвета
- Период покоя: медленный рост, старые листья, зимнее время
- Стресс: желтизна, коричневые пятна, вялость, вредители
- Адаптация: недавняя пересадка (если упомянуто пользователем)

АНАЛИЗ ПОЛИВА - только если почва видна:
- Осмотрите листья на предмет увядания, желтизны, коричневых пятен
- Оцените упругость и тургор листьев
- Проанализируйте признаки переувлажнения или пересушивания
- Посмотрите на состояние почвы (если видно)

Дайте ответ в формате:
РАСТЕНИЕ: [Точное название на русском и латинском]
УВЕРЕННОСТЬ: [процент]
ПРИЗНАКИ: [ключевые признаки]
СЕМЕЙСТВО: [ботаническое семейство]
РОДИНА: [естественная среда]

ТЕКУЩЕЕ_СОСТОЯНИЕ: [одно из: healthy, flowering, active_growth, dormancy, stress, adaptation]
ПРИЧИНА_СОСТОЯНИЯ: [почему определили это состояние, что видно на фото]
ЭТАП_РОСТА: [young/mature/old - молодое/взрослое/старое растение]

СОСТОЯНИЕ: [детальная оценка здоровья по листьям]

ПОЛИВ_АНАЛИЗ: [если почва видна - анализ, иначе: "Почва не видна"]
ПОЛИВ_РЕКОМЕНДАЦИИ: [конкретные рекомендации]
ПОЛИВ_ИНТЕРВАЛ: [число дней: 2-15]

СВЕТ: [требования к освещению]
ТЕМПЕРАТУРА: [оптимальный диапазон]
ВЛАЖНОСТЬ: [требования]
ПОДКОРМКА: [рекомендации]
ПЕРЕСАДКА: [когда пересаживать]

ПРОБЛЕМЫ: [возможные болезни]
СОВЕТ: [специфический совет для ТЕКУЩЕГО состояния растения]

ДИНАМИЧЕСКИЕ_РЕКОМЕНДАЦИИ: [если состояние НЕ healthy - дайте специальные советы:
- Для flowering: увеличить полив на 2 дня, подкормка для цветения
- Для active_growth: увеличить подкормку, больше света
- Для dormancy: уменьшить полив на 5 дней, снизить температуру
- Для stress: срочные действия для решения проблемы
- Для adaptation: щадящий режим, не тревожить]

Будьте максимально точными в идентификации состояния.
"""

# FSM States
class PlantStates(StatesGroup):
    waiting_question = State()
    editing_plant_name = State()
    choosing_plant_to_grow = State()
    planting_setup = State()
    waiting_growing_photo = State()
    adding_diary_entry = State()
    onboarding_welcome = State()
    onboarding_demo = State()
    onboarding_quick_start = State()
    waiting_state_update_photo = State()

class FeedbackStates(StatesGroup):
    choosing_type = State()
    writing_message = State()

def get_moscow_now():
    return datetime.now(MOSCOW_TZ)

def get_moscow_date():
    return get_moscow_now().date()

def moscow_to_naive(moscow_datetime):
    if moscow_datetime.tzinfo is not None:
        return moscow_datetime.replace(tzinfo=None)
    return moscow_datetime

# МАППИНГ СОСТОЯНИЙ
STATE_EMOJI = {
    'healthy': '🌱',
    'flowering': '💐',
    'active_growth': '🌿',
    'dormancy': '😴',
    'stress': '⚠️',
    'adaptation': '🔄'
}

STATE_NAMES = {
    'healthy': 'Здоровое',
    'flowering': 'Цветение',
    'active_growth': 'Активный рост',
    'dormancy': 'Период покоя',
    'stress': 'Стресс/Болезнь',
    'adaptation': 'Адаптация'
}

def get_state_recommendations(state: str, plant_name: str = "растение") -> str:
    """Получить рекомендации для состояния"""
    recommendations = {
        'flowering': f"""
💐 <b>{plant_name} цветет!</b>

<b>Изменения в уходе:</b>
• 💧 <b>Полив:</b> Чаще на 2 дня (больше воды при цветении)
• 🍽️ <b>Подкормка:</b> Удобрение для цветения 1 раз в неделю
• ☀️ <b>Свет:</b> Больше света, но избегайте прямых лучей
• 🌡️ <b>Температура:</b> Стабильная, без перепадов

⚠️ <b>Важно:</b> Не перемещайте растение во время цветения!
💡 <b>Совет:</b> Удаляйте увядшие цветы для продления цветения
""",
        'active_growth': f"""
🌿 <b>{plant_name} активно растет!</b>

<b>Изменения в уходе:</b>
• 💧 <b>Полив:</b> Регулярный, не допускайте пересыхания
• 🍽️ <b>Подкормка:</b> Каждые 2 недели удобрением для роста
• ☀️ <b>Свет:</b> Максимум света для фотосинтеза
• 🪴 <b>Пересадка:</b> Если корням тесно - пересадите

💡 <b>Совет:</b> Это лучшее время для формирования кроны
""",
        'dormancy': f"""
😴 <b>{plant_name} в периоде покоя</b>

<b>Изменения в уходе:</b>
• 💧 <b>Полив:</b> Реже на 5 дней (минимальный полив)
• 🍽️ <b>Подкормка:</b> Прекратить до весны
• 🌡️ <b>Температура:</b> Прохладнее 15-18°C
• ☀️ <b>Свет:</b> Меньше света - это нормально

💡 <b>Совет:</b> Весной растение проснется с новыми силами!
⚠️ Не тревожьте растение в этот период
""",
        'stress': f"""
⚠️ <b>Внимание! {plant_name} в стрессе</b>

<b>Срочные действия:</b>
• 🔍 <b>Диагностика:</b> Определите причину (полив/свет/вредители)
• 💧 <b>Полив:</b> Проверьте влажность - корректируйте режим
• ✂️ <b>Обрезка:</b> Удалите поврежденные листья
• 🦠 <b>Вредители:</b> Осмотрите листья с двух сторон
• 💨 <b>Проветривание:</b> Улучшите циркуляцию воздуха

📸 <b>Важно:</b> Загрузите фото через 3-5 дней для контроля!
❓ Если не помогает - задайте вопрос с фото проблемы
""",
        'adaptation': f"""
🔄 <b>{plant_name} адаптируется</b>

<b>Щадящий режим:</b>
• 💧 <b>Полив:</b> Умеренный, без переувлажнения
• ☀️ <b>Свет:</b> Не ставьте на яркое солнце сразу
• 🌡️ <b>Температура:</b> Стабильная, без стресса
• ⏰ <b>Время:</b> Дайте 2-3 недели на привыкание

💡 <b>Совет:</b> Не пересаживайте и не тревожьте растение
📸 Сфотографируйте через неделю для контроля состояния
""",
        'healthy': f"""
🌱 <b>{plant_name} здоровое!</b>

<b>Продолжайте текущий уход:</b>
• 💧 Регулярный полив по графику
• 🍽️ Подкормка по сезону
• ☀️ Достаточно света
• 🌡️ Комфортная температура

💡 <b>Совет:</b> Продолжайте в том же духе!
📸 Обновляйте фото раз в месяц для отслеживания
"""
    }
    
    return recommendations.get(state, recommendations['healthy'])

# === СИСТЕМА НАПОМИНАНИЙ ===

async def check_and_send_reminders():
    """Проверка и отправка напоминаний"""
    try:
        db = await get_db()
        
        moscow_now = get_moscow_now()
        moscow_date = moscow_now.date()
        
        # Напоминания о поливе
        async with db.pool.acquire() as conn:
            plants_to_water = await conn.fetch("""
                SELECT p.id, p.user_id, 
                       COALESCE(p.custom_name, p.plant_name, 'Растение #' || p.id) as display_name,
                       p.last_watered, 
                       COALESCE(p.watering_interval, 5) as watering_interval, 
                       p.photo_file_id, p.notes, p.current_state, p.growth_stage
                FROM plants p
                JOIN user_settings us ON p.user_id = us.user_id
                WHERE p.reminder_enabled = TRUE 
                  AND us.reminder_enabled = TRUE
                  AND p.plant_type = 'regular'
                  AND (
                    p.last_watered IS NULL 
                    OR p.last_watered::date + (COALESCE(p.watering_interval, 5) || ' days')::interval <= $1::date
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM reminders r 
                    WHERE r.plant_id = p.id 
                    AND r.last_sent::date = $1::date
                  )
                ORDER BY p.last_watered ASC NULLS FIRST
            """, moscow_date)
            
            for plant in plants_to_water:
                await send_watering_reminder(plant)
        
        await check_and_send_growing_reminders()
        
    except Exception as e:
        logger.error(f"Ошибка напоминаний: {e}")

async def check_monthly_photo_reminders():
    """Проверка месячных напоминаний об обновлении фото"""
    try:
        db = await get_db()
        plants_for_reminder = await db.get_plants_for_monthly_reminder()
        
        # Группируем по пользователям
        users_plants = {}
        for plant in plants_for_reminder:
            user_id = plant['user_id']
            if user_id not in users_plants:
                users_plants[user_id] = []
            users_plants[user_id].append(plant)
        
        # Отправляем по одному сообщению на пользователя
        for user_id, plants in users_plants.items():
            await send_monthly_photo_reminder(user_id, plants)
            await db.mark_monthly_reminder_sent(user_id)
        
    except Exception as e:
        logger.error(f"Ошибка месячных напоминаний: {e}")

async def send_monthly_photo_reminder(user_id: int, plants: list):
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
Просто пришлите новое фото каждого растения, и я сравню с предыдущим состоянием!

🌱 Нажмите на растение в коллекции чтобы обновить его фото
"""
        
        keyboard = [
            [InlineKeyboardButton(text="🌿 К моей коллекции", callback_data="my_plants")],
            [InlineKeyboardButton(text="⏰ Напомнить через неделю", callback_data="snooze_monthly_reminder")],
            [InlineKeyboardButton(text="🔕 Отключить такие напоминания", callback_data="disable_monthly_reminders")],
        ]
        
        await bot.send_message(
            chat_id=user_id,
            text=message_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        logger.info(f"📸 Месячное напоминание отправлено пользователю {user_id} ({len(plants)} растений)")
        
    except Exception as e:
        logger.error(f"Ошибка отправки месячного напоминания: {e}")

async def check_and_send_growing_reminders():
    """Напоминания по этапам выращивания"""
    try:
        db = await get_db()
        moscow_now = get_moscow_now()
        
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
            
            for reminder in reminders:
                await send_task_reminder(reminder)
                
    except Exception as e:
        logger.error(f"Ошибка напоминаний выращивания: {e}")

async def send_task_reminder(reminder_row):
    """Отправка напоминания задачи"""
    try:
        user_id = reminder_row['user_id']
        growing_id = reminder_row['growing_id']
        plant_name = reminder_row['plant_name']
        task_day = reminder_row['task_day']
        task_calendar = reminder_row['task_calendar']
        current_stage = reminder_row['current_stage']
        started_date = reminder_row['started_date']
        
        stage_key = f"stage_{current_stage + 1}"
        task_info = None
        
        if task_calendar and stage_key in task_calendar:
            tasks = task_calendar[stage_key].get('tasks', [])
            for task in tasks:
                if task.get('day') == task_day:
                    task_info = task
                    break
        
        if not task_info:
            return
        
        task_icon = task_info.get('icon', '📋')
        task_title = task_info.get('title', 'Задача')
        task_description = task_info.get('description', '')
        
        days_since_start = (get_moscow_now().date() - started_date.date()).days
        
        message_text = f"{task_icon} <b>Время для важного действия!</b>\n\n"
        message_text += f"🌱 <b>{plant_name}</b>\n"
        message_text += f"📅 День {days_since_start} выращивания\n\n"
        message_text += f"📋 <b>{task_title}</b>\n"
        message_text += f"📝 {task_description}\n\n"
        message_text += f"📸 Не забудьте сфотографировать результат!"
        
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
        
        moscow_now = get_moscow_now()
        moscow_now_naive = moscow_now.replace(tzinfo=None)
        
        db = await get_db()
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE reminders
                SET last_sent = $1,
                    send_count = COALESCE(send_count, 0) + 1
                WHERE id = $2
            """, moscow_now_naive, reminder_row['reminder_id'])
        
        await schedule_next_task_reminder(growing_id, user_id, task_calendar, task_day)
        
    except Exception as e:
        logger.error(f"Ошибка отправки задачи: {e}")

async def schedule_next_task_reminder(growing_id: int, user_id: int, task_calendar: dict, current_day: int):
    """Запланировать следующую задачу"""
    try:
        db = await get_db()
        
        growing_plant = await db.get_growing_plant_by_id(growing_id, user_id)
        if not growing_plant:
            return
        
        current_stage = growing_plant['current_stage']
        stage_key = f"stage_{current_stage + 1}"
        
        if stage_key in task_calendar and 'tasks' in task_calendar[stage_key]:
            tasks = task_calendar[stage_key]['tasks']
            sorted_tasks = sorted(tasks, key=lambda x: x.get('day', 0))
            
            for task in sorted_tasks:
                task_day = task.get('day', 0)
                
                if task_day > current_day:
                    started_date = growing_plant['started_date']
                    reminder_date = started_date + timedelta(days=task_day)
                    reminder_date_naive = reminder_date.replace(tzinfo=None) if reminder_date.tzinfo else reminder_date
                    
                    await db.create_growing_reminder(
                        growing_id=growing_id,
                        user_id=user_id,
                        reminder_type="task",
                        next_date=reminder_date_naive,
                        stage_number=current_stage + 1,
                        task_day=task_day
                    )
                    
                    return
        
    except Exception as e:
        logger.error(f"Ошибка планирования задачи: {e}")

async def send_watering_reminder(plant_row):
    """Персональное напоминание с учетом состояния"""
    try:
        user_id = plant_row['user_id']
        plant_id = plant_row['id']
        plant_name = plant_row['display_name']
        current_state = plant_row.get('current_state', 'healthy')
        
        db = await get_db()
        plant_info = await db.get_plant_by_id(plant_id)
        
        moscow_now = get_moscow_now()
        
        if plant_row['last_watered']:
            last_watered_utc = plant_row['last_watered']
            if last_watered_utc.tzinfo is None:
                last_watered_utc = pytz.UTC.localize(last_watered_utc)
            last_watered_moscow = last_watered_utc.astimezone(MOSCOW_TZ)
            
            days_ago = (moscow_now.date() - last_watered_moscow.date()).days
            if days_ago == 1:
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
        
        # Добавляем рекомендации по состоянию
        if current_state == 'flowering':
            message_text += f"💐 Растение цветет - поливайте чаще!\n"
        elif current_state == 'dormancy':
            message_text += f"😴 Период покоя - поливайте реже\n"
        elif current_state == 'stress':
            message_text += f"⚠️ Растение в стрессе - проверьте влажность почвы!\n"
        
        interval = plant_row.get('watering_interval', 5)
        message_text += f"\n⏱️ Интервал: каждые {interval} дней"
        
        keyboard = [
            [InlineKeyboardButton(text="💧 Полил(а)!", callback_data=f"water_plant_{plant_id}")],
            [InlineKeyboardButton(text="⏰ Напомнить завтра", callback_data=f"snooze_{plant_id}")],
            [InlineKeyboardButton(text="📸 Обновить состояние", callback_data=f"update_state_{plant_id}")],
        ]
        
        await bot.send_photo(
            chat_id=user_id,
            photo=plant_row['photo_file_id'],
            caption=message_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        moscow_now_str = moscow_now.strftime('%Y-%m-%d %H:%M:%S')
        async with db.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO reminders (user_id, plant_id, reminder_type, next_date, last_sent)
                VALUES ($1, $2, 'watering', $3::timestamp, $3::timestamp)
                ON CONFLICT (user_id, plant_id, reminder_type) 
                WHERE is_active = TRUE
                DO UPDATE SET 
                    last_sent = $3::timestamp,
                    send_count = COALESCE(reminders.send_count, 0) + 1
            """, user_id, plant_id, moscow_now_str)
        
    except Exception as e:
        logger.error(f"Ошибка напоминания: {e}")

async def create_plant_reminder(plant_id: int, user_id: int, interval_days: int = 5):
    """Создать напоминание"""
    try:
        db = await get_db()
        moscow_now = get_moscow_now()
        next_watering = moscow_now + timedelta(days=interval_days)
        next_watering_naive = next_watering.replace(tzinfo=None)
        
        await db.create_reminder(
            user_id=user_id,
            plant_id=plant_id,
            reminder_type='watering',
            next_date=next_watering_naive
        )
        
    except Exception as e:
        logger.error(f"Ошибка создания напоминания: {e}")

# === АНАЛИЗ РАСТЕНИЙ С ОПРЕДЕЛЕНИЕМ СОСТОЯНИЯ ===

def extract_plant_state_from_analysis(raw_analysis: str) -> dict:
    """Извлечь информацию о состоянии из анализа AI"""
    state_info = {
        'current_state': 'healthy',
        'state_reason': '',
        'growth_stage': 'young',
        'watering_adjustment': 0,
        'feeding_adjustment': None,
        'recommendations': ''
    }
    
    if not raw_analysis:
        return state_info
    
    lines = raw_analysis.split('\n')
    
    for line in lines:
        line = line.strip()
        
        if line.startswith("ТЕКУЩЕЕ_СОСТОЯНИЕ:"):
            state_text = line.replace("ТЕКУЩЕЕ_СОСТОЯНИЕ:", "").strip().lower()
            # Определяем состояние
            if 'flowering' in state_text or 'цветен' in state_text:
                state_info['current_state'] = 'flowering'
                state_info['watering_adjustment'] = -2  # Поливать чаще
            elif 'active_growth' in state_text or 'активн' in state_text:
                state_info['current_state'] = 'active_growth'
                state_info['feeding_adjustment'] = 7  # Подкормка раз в неделю
            elif 'dormancy' in state_text or 'покой' in state_text:
                state_info['current_state'] = 'dormancy'
                state_info['watering_adjustment'] = 5  # Поливать реже
            elif 'stress' in state_text or 'стресс' in state_text or 'болезн' in state_text:
                state_info['current_state'] = 'stress'
            elif 'adaptation' in state_text or 'адаптац' in state_text:
                state_info['current_state'] = 'adaptation'
            else:
                state_info['current_state'] = 'healthy'
        
        elif line.startswith("ПРИЧИНА_СОСТОЯНИЯ:"):
            state_info['state_reason'] = line.replace("ПРИЧИНА_СОСТОЯНИЯ:", "").strip()
        
        elif line.startswith("ЭТАП_РОСТА:"):
            stage_text = line.replace("ЭТАП_РОСТА:", "").strip().lower()
            if 'young' in stage_text or 'молод' in stage_text:
                state_info['growth_stage'] = 'young'
            elif 'mature' in stage_text or 'взросл' in stage_text:
                state_info['growth_stage'] = 'mature'
            elif 'old' in stage_text or 'стар' in stage_text:
                state_info['growth_stage'] = 'old'
        
        elif line.startswith("ДИНАМИЧЕСКИЕ_РЕКОМЕНДАЦИИ:"):
            state_info['recommendations'] = line.replace("ДИНАМИЧЕСКИЕ_РЕКОМЕНДАЦИИ:", "").strip()
    
    return state_info

def extract_personal_watering_info(analysis_text: str) -> dict:
    """Извлечь информацию о поливе"""
    watering_info = {
        "interval_days": 5,
        "personal_recommendations": "",
        "current_state": "",
        "needs_adjustment": False
    }
    
    if not analysis_text:
        return watering_info
    
    lines = analysis_text.split('\n')
    
    for line in lines:
        line = line.strip()
        
        if line.startswith("ПОЛИВ_ИНТЕРВАЛ:"):
            interval_text = line.replace("ПОЛИВ_ИНТЕРВАЛ:", "").strip()
            import re
            numbers = re.findall(r'\d+', interval_text)
            if numbers:
                try:
                    interval = int(numbers[0])
                    if 1 <= interval <= 15:
                        watering_info["interval_days"] = interval
                except:
                    pass
        
        elif line.startswith("ПОЛИВ_АНАЛИЗ:"):
            current_state = line.replace("ПОЛИВ_АНАЛИЗ:", "").strip()
            watering_info["current_state"] = current_state
            if "не видна" in current_state.lower() or "невозможно оценить" in current_state.lower():
                watering_info["needs_adjustment"] = True
            elif any(word in current_state.lower() for word in ["переувлажн", "перелив", "недополит", "пересушен", "проблем"]):
                watering_info["needs_adjustment"] = True
        
        elif line.startswith("ПОЛИВ_РЕКОМЕНДАЦИИ:"):
            recommendations = line.replace("ПОЛИВ_РЕКОМЕНДАЦИИ:", "").strip()
            watering_info["personal_recommendations"] = recommendations
            
    return watering_info

def format_plant_analysis(raw_text: str, confidence: float = None, state_info: dict = None) -> str:
    """Форматирование анализа с состоянием"""
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    formatted = ""
    
    plant_name = "Неизвестное растение"
    confidence_level = confidence or 0
    
    for line in lines:
        if line.startswith("РАСТЕНИЕ:"):
            plant_name = line.replace("РАСТЕНИЕ:", "").strip()
            display_name = plant_name.split("(")[0].strip()
            formatted += f"🌿 <b>{display_name}</b>\n"
            if "(" in plant_name:
                latin_name = plant_name[plant_name.find("(")+1:plant_name.find(")")]
                formatted += f"🏷️ <i>{latin_name}</i>\n"
            
        elif line.startswith("УВЕРЕННОСТЬ:"):
            conf = line.replace("УВЕРЕННОСТЬ:", "").strip()
            try:
                confidence_level = float(conf.replace("%", ""))
                if confidence_level >= 80:
                    conf_icon = "🎯"
                elif confidence_level >= 60:
                    conf_icon = "🎪"
                else:
                    conf_icon = "🤔"
                formatted += f"{conf_icon} <b>Уверенность:</b> {conf}\n\n"
            except:
                formatted += f"🎪 <b>Уверенность:</b> {conf}\n\n"
        
        elif line.startswith("ТЕКУЩЕЕ_СОСТОЯНИЕ:"):
            pass
        
        elif line.startswith("СОСТОЯНИЕ:"):
            condition = line.replace("СОСТОЯНИЕ:", "").strip()
            if any(word in condition.lower() for word in ["здоров", "хорош", "отличн", "норм"]):
                icon = "✅"
            elif any(word in condition.lower() for word in ["проблем", "болен", "плох", "стресс"]):
                icon = "⚠️"
            else:
                icon = "ℹ️"
            formatted += f"{icon} <b>Общее состояние:</b> {condition}\n\n"
        
        elif line.startswith("ПОЛИВ_АНАЛИЗ:"):
            analysis = line.replace("ПОЛИВ_АНАЛИЗ:", "").strip()
            if "невозможно" in analysis.lower() or "не видна" in analysis.lower():
                icon = "❓"
            else:
                icon = "💧"
            formatted += f"{icon} <b>Анализ полива:</b> {analysis}\n"
            
        elif line.startswith("ПОЛИВ_РЕКОМЕНДАЦИИ:"):
            recommendations = line.replace("ПОЛИВ_РЕКОМЕНДАЦИИ:", "").strip()
            formatted += f"💡 <b>Рекомендации:</b> {recommendations}\n"
            
        elif line.startswith("ПОЛИВ_ИНТЕРВАЛ:"):
            interval = line.replace("ПОЛИВ_ИНТЕРВАЛ:", "").strip()
            formatted += f"⏰ <b>Интервал полива:</b> каждые {interval} дней\n\n"
            
        elif line.startswith("СВЕТ:"):
            light = line.replace("СВЕТ:", "").strip()
            formatted += f"☀️ <b>Освещение:</b> {light}\n"
            
        elif line.startswith("ТЕМПЕРАТУРА:"):
            temp = line.replace("ТЕМПЕРАТУРА:", "").strip()
            formatted += f"🌡️ <b>Температура:</b> {temp}\n"
            
        elif line.startswith("ВЛАЖНОСТЬ:"):
            humidity = line.replace("ВЛАЖНОСТЬ:", "").strip()
            formatted += f"💨 <b>Влажность:</b> {humidity}\n"
            
        elif line.startswith("ПОДКОРМКА:"):
            feeding = line.replace("ПОДКОРМКА:", "").strip()
            formatted += f"🍽️ <b>Подкормка:</b> {feeding}\n"
        
        elif line.startswith("СОВЕТ:"):
            advice = line.replace("СОВЕТ:", "").strip()
            formatted += f"\n💡 <b>Персональный совет:</b> {advice}"
    
    if state_info:
        current_state = state_info.get('current_state', 'healthy')
        state_emoji = STATE_EMOJI.get(current_state, '🌱')
        state_name = STATE_NAMES.get(current_state, 'Здоровое')
        
        formatted = f"\n{state_emoji} <b>Текущее состояние:</b> {state_name}\n" + formatted
        
        if state_info.get('state_reason'):
            formatted += f"\n📋 <b>Почему:</b> {state_info['state_reason']}"
    
    if confidence_level >= 80:
        formatted += "\n\n🏆 <i>Высокая точность распознавания</i>"
    elif confidence_level >= 60:
        formatted += "\n\n👍 <i>Хорошее распознавание</i>"
    else:
        formatted += "\n\n🤔 <i>Требуется дополнительная идентификация</i>"
    
    formatted += "\n💾 <i>Сохраните для отслеживания изменений!</i>"
    
    return formatted

async def optimize_image_for_analysis(image_data: bytes, high_quality: bool = True) -> bytes:
    """Оптимизация изображения"""
    try:
        image = Image.open(BytesIO(image_data))
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        if high_quality:
            if max(image.size) < 1024:
                ratio = 1024 / max(image.size)
                new_size = (int(image.size[0] * ratio), int(image.size[1] * ratio))
                image = image.resize(new_size, Image.Resampling.LANCZOS)
            elif max(image.size) > 2048:
                image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        else:
            if max(image.size) > 1024:
                image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        
        output = BytesIO()
        quality = 95 if high_quality else 85
        image.save(output, format='JPEG', quality=quality, optimize=True)
        return output.getvalue()
    except Exception as e:
        logger.error(f"Ошибка оптимизации: {e}")
        return image_data

async def analyze_with_openai_advanced(image_data: bytes, user_question: str = None, previous_state: str = None) -> dict:
    """Продвинутый анализ с определением состояния"""
    if not openai_client:
        return {"success": False, "error": "OpenAI API недоступен"}
    
    try:
        optimized_image = await optimize_image_for_analysis(image_data, high_quality=True)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        prompt = PLANT_IDENTIFICATION_PROMPT
        
        if previous_state:
            prompt += f"\n\nПредыдущее состояние растения: {previous_state}. Определите что изменилось."
        
        if user_question:
            prompt += f"\n\nДополнительный вопрос: {user_question}"
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "Вы - эксперт-ботаник с 30-летним опытом. Определяйте состояние растения максимально точно."
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "high"
                            }
                        }
                    ]
                }
            ],
            max_tokens=1500,
            temperature=0.2
        )
        
        raw_analysis = response.choices[0].message.content
        
        if len(raw_analysis) < 100:
            raise Exception("Некачественный ответ")
        
        confidence = 0
        for line in raw_analysis.split('\n'):
            if line.startswith("УВЕРЕННОСТЬ:"):
                try:
                    conf_str = line.replace("УВЕРЕННОСТЬ:", "").strip().replace("%", "")
                    confidence = float(conf_str)
                except:
                    confidence = 70
                break
        
        plant_name = "Неизвестное растение"
        for line in raw_analysis.split('\n'):
            if line.startswith("РАСТЕНИЕ:"):
                plant_name = line.replace("РАСТЕНИЕ:", "").strip()
                break
        
        state_info = extract_plant_state_from_analysis(raw_analysis)
        formatted_analysis = format_plant_analysis(raw_analysis, confidence, state_info)
        
        logger.info(f"✅ Анализ завершен. Состояние: {state_info['current_state']}, Уверенность: {confidence}%")
        
        return {
            "success": True,
            "analysis": formatted_analysis,
            "raw_analysis": raw_analysis,
            "plant_name": plant_name,
            "confidence": confidence,
            "source": "openai_advanced",
            "state_info": state_info
        }
        
    except Exception as e:
        logger.error(f"❌ OpenAI error: {e}")
        return {"success": False, "error": str(e)}

async def analyze_plant_image(image_data: bytes, user_question: str = None, 
                             previous_state: str = None, retry_count: int = 0) -> dict:
    """Анализ изображения растения с состоянием"""
    
    logger.info("🔍 Анализ через OpenAI GPT-4 Vision...")
    openai_result = await analyze_with_openai_advanced(image_data, user_question, previous_state)
    
    if openai_result["success"] and openai_result.get("confidence", 0) >= 50:
        logger.info(f"✅ Успешно: {openai_result.get('confidence')}%")
        return openai_result
    
    if retry_count == 0:
        logger.info("🔄 Повторная попытка...")
        return await analyze_plant_image(image_data, user_question, previous_state, retry_count + 1)
    
    if openai_result["success"]:
        logger.warning(f"⚠️ Низкая уверенность: {openai_result.get('confidence')}%")
        openai_result["needs_retry"] = True
        return openai_result
    
    logger.warning("⚠️ Fallback")
    
    fallback_text = """
РАСТЕНИЕ: Комнатное растение (требуется идентификация)
УВЕРЕННОСТЬ: 20%
ТЕКУЩЕЕ_СОСТОЯНИЕ: healthy
ПРИЧИНА_СОСТОЯНИЯ: Недостаточно данных
ЭТАП_РОСТА: young
СОСТОЯНИЕ: Требуется визуальный осмотр
ПОЛИВ_АНАЛИЗ: Почва не видна
ПОЛИВ_РЕКОМЕНДАЦИИ: Проверяйте влажность почвы
ПОЛИВ_ИНТЕРВАЛ: 5
СВЕТ: Яркий рассеянный свет
ТЕМПЕРАТУРА: 18-24°C
ВЛАЖНОСТЬ: 40-60%
ПОДКОРМКА: Раз в 2-4 недели весной-летом
СОВЕТ: Сделайте фото при хорошем освещении для точной идентификации
    """.strip()
    
    state_info = extract_plant_state_from_analysis(fallback_text)
    formatted_analysis = format_plant_analysis(fallback_text, 20, state_info)
    
    return {
        "success": True,
        "analysis": formatted_analysis,
        "raw_analysis": fallback_text,
        "plant_name": "Неопознанное растение",
        "confidence": 20,
        "source": "fallback",
        "needs_retry": True,
        "state_info": state_info
    }

# === ФУНКЦИЯ ГЕНЕРАЦИИ ПЛАНА ВЫРАЩИВАНИЯ ===

async def get_growing_plan_from_ai(plant_name: str) -> tuple:
    """Генерация плана выращивания через OpenAI"""
    if not openai_client:
        return None, None
    
    try:
        prompt = f"""
Создай подробный план выращивания для: {plant_name}

Формат ответа:

🌱 ЭТАП 1: Название (X дней)
• Задача 1
• Задача 2
• Задача 3

🌿 ЭТАП 2: Название (X дней)
• Задача 1
• Задача 2

🌸 ЭТАП 3: Название (X дней)
• Задача 1
• Задача 2

🌳 ЭТАП 4: Название (X дней)
• Задача 1
• Задача 2

В конце добавь:
КАЛЕНДАРЬ_ЗАДАЧ: [JSON с структурой задач по дням]
"""
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Вы - эксперт по выращиванию растений. Создавайте практичные планы."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1200,
            temperature=0.3
        )
        
        plan_text = response.choices[0].message.content
        
        # Создаем календарь задач
        task_calendar = create_default_task_calendar(plant_name)
        
        return plan_text, task_calendar
        
    except Exception as e:
        logger.error(f"Ошибка генерации плана: {e}")
        return None, None

def create_default_task_calendar(plant_name: str) -> dict:
    """Создать стандартный календарь задач"""
    return {
        "stage_1": {
            "name": "Подготовка и посадка",
            "duration_days": 7,
            "tasks": [
                {"day": 1, "title": "Посадка", "description": "Посадите семена/черенок", "icon": "🌱"},
                {"day": 3, "title": "Первый полив", "description": "Умеренно полейте", "icon": "💧"},
                {"day": 7, "title": "Проверка", "description": "Проверьте влажность", "icon": "🔍"},
            ]
        },
        "stage_2": {
            "name": "Прорастание",
            "duration_days": 14,
            "tasks": [
                {"day": 10, "title": "Первые всходы", "description": "Проверьте появление ростков", "icon": "🌱"},
                {"day": 14, "title": "Регулярный полив", "description": "Поддерживайте влажность", "icon": "💧"},
            ]
        },
        "stage_3": {
            "name": "Активный рост",
            "duration_days": 30,
            "tasks": [
                {"day": 21, "title": "Первая подкормка", "description": "Внесите удобрение", "icon": "🍽️"},
                {"day": 35, "title": "Проверка роста", "description": "Оцените развитие растения", "icon": "📊"},
            ]
        },
        "stage_4": {
            "name": "Взрослое растение",
            "duration_days": 30,
            "tasks": [
                {"day": 50, "title": "Пересадка", "description": "Пересадите в больший горшок", "icon": "🪴"},
                {"day": 60, "title": "Формирование", "description": "При необходимости обрежьте", "icon": "✂️"},
            ]
        }
    }

# === МЕНЮ НАВИГАЦИИ ===

def main_menu():
    keyboard = [
        [
            InlineKeyboardButton(text="🌱 Добавить растение", callback_data="add_plant"),
            InlineKeyboardButton(text="🌿 Вырастить с нуля", callback_data="grow_from_scratch")
        ],
        [
            InlineKeyboardButton(text="📸 Анализ растения", callback_data="analyze"),
            InlineKeyboardButton(text="❓ Задать вопрос", callback_data="question")
        ],
        [
            InlineKeyboardButton(text="🌿 Мои растения", callback_data="my_plants"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats")
        ],
        [
            InlineKeyboardButton(text="📝 Обратная связь", callback_data="feedback"),
            InlineKeyboardButton(text="ℹ️ Справка", callback_data="help")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def simple_back_menu():
    keyboard = [
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# === ОБРАБОТЧИКИ КОМАНД ===

@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Команда /start с онбордингом"""
    user_id = message.from_user.id
    
    logger.info(f"📩 Получена команда /start от пользователя {user_id}")
    
    try:
        db = await get_db()
        
        async with db.pool.acquire() as conn:
            existing_user = await conn.fetchrow(
                "SELECT user_id FROM users WHERE user_id = $1", user_id
            )
            
            if not existing_user:
                await db.add_user(
                    user_id=user_id,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name
                )
                
                logger.info(f"✅ Новый пользователь {user_id} добавлен")
                await start_onboarding(message)
                return
            else:
                logger.info(f"✅ Возвращающийся пользователь {user_id}")
                await show_returning_user_welcome(message)
                return
                
    except Exception as e:
        logger.error(f"❌ Ошибка /start: {e}", exc_info=True)
        await show_returning_user_welcome(message)

async def start_onboarding(message: types.Message):
    """Онбординг для новых пользователей"""
    first_name = message.from_user.first_name or "друг"
    
    keyboard = [
        [InlineKeyboardButton(text="✨ Покажи пример", callback_data="onboarding_demo")],
        [InlineKeyboardButton(text="🚀 Хочу попробовать сразу", callback_data="onboarding_quick_start")],
    ]
    
    await message.answer(
        f"🌱 Отлично, {first_name}! Готов стать вашим садовым помощником!\n\n"
        "Давайте я покажу, как это работает на примере, а потом вы попробуете сами?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

async def show_returning_user_welcome(message: types.Message):
    """Приветствие для возвращающихся"""
    first_name = message.from_user.first_name or "друг"
    
    await message.answer(
        f"🌱 С возвращением, {first_name}!\n\n"
        "Что будем делать с растениями сегодня?",
        reply_markup=main_menu()
    )

@dp.message(Command("grow"))
async def grow_command(message: types.Message, state: FSMContext):
    """Команда /grow"""
    await message.answer(
        "🌿 <b>Выращиваем с нуля!</b>\n\n"
        "🌱 Напишите, что хотите вырастить:",
        parse_mode="HTML"
    )
    await state.set_state(PlantStates.choosing_plant_to_grow)

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Команда /help"""
    help_text = """
🌱 <b>Как пользоваться ботом:</b>

🌱 <b>Добавление растения:</b>
• Пришли фото
• Получи анализ состояния
• Отслеживай изменения

📊 <b>Система состояний:</b>
• 💐 Цветение - особый уход
• 🌿 Активный рост - больше питания
• 😴 Период покоя - меньше полива
• ⚠️ Стресс - срочные действия

📸 <b>Месячные напоминания:</b>
• Обновляйте фото раз в месяц
• Отслеживайте изменения
• Адаптивные рекомендации

⏰ <b>Умные напоминания:</b>
• Адаптированы под состояние
• Учитывают этап роста
• Персональный график

<b>Команды:</b>
/start - Главное меню
/grow - Вырастить с нуля
/help - Справка
    """
    
    keyboard = [
        [InlineKeyboardButton(text="📝 Обратная связь", callback_data="feedback")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    
    await message.answer(
        help_text, 
        parse_mode="HTML", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

@dp.message(Command("feedback"))
async def feedback_command(message: types.Message):
    """Команда /feedback"""
    keyboard = [
        [InlineKeyboardButton(text="🐛 Сообщить о баге", callback_data="feedback_bug")],
        [InlineKeyboardButton(text="❌ Неточный анализ", callback_data="feedback_analysis_error")],
        [InlineKeyboardButton(text="💡 Предложение", callback_data="feedback_suggestion")],
        [InlineKeyboardButton(text="⭐ Отзыв", callback_data="feedback_review")],
    ]
    
    await message.answer(
        "📝 <b>Обратная связь</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

# === ОБРАБОТКА ФОТОГРАФИЙ ===

@dp.message(StateFilter(PlantStates.waiting_state_update_photo), F.photo)
async def handle_state_update_photo(message: types.Message, state: FSMContext):
    """Обработка фото для обновления состояния"""
    try:
        data = await state.get_data()
        plant_id = data.get('state_plant_id')
        user_id = message.from_user.id
        
        if not plant_id:
            await message.reply("❌ Ошибка: данные потеряны")
            await state.clear()
            return
        
        processing_msg = await message.reply(
            "🔍 <b>Анализирую изменения...</b>\n\n"
            "• Сравниваю с предыдущим фото\n"
            "• Определяю текущее состояние\n"
            "• Готовлю рекомендации...",
            parse_mode="HTML"
        )
        
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await processing_msg.delete()
            await message.reply("❌ Растение не найдено")
            await state.clear()
            return
        
        previous_state = plant.get('current_state', 'healthy')
        plant_name = plant['display_name']
        
        result = await analyze_plant_image(
            file_data.read(), 
            previous_state=previous_state
        )
        
        await processing_msg.delete()
        
        if result["success"]:
            state_info = result.get("state_info", {})
            new_state = state_info.get('current_state', 'healthy')
            state_reason = state_info.get('state_reason', 'Анализ AI')
            
            state_changed = (new_state != previous_state)
            
            await db.update_plant_state(
                plant_id=plant_id,
                user_id=user_id,
                new_state=new_state,
                change_reason=state_reason,
                photo_file_id=photo.file_id,
                ai_analysis=result.get("raw_analysis", ""),
                watering_adjustment=state_info.get('watering_adjustment', 0),
                feeding_adjustment=state_info.get('feeding_adjustment'),
                recommendations=state_info.get('recommendations', '')
            )
            
            async with db.pool.acquire() as conn:
                await conn.execute("""
                    UPDATE plants 
                    SET last_photo_analysis = CURRENT_TIMESTAMP,
                        photo_file_id = $1
                    WHERE id = $2
                """, photo.file_id, plant_id)
            
            response_text = f"📊 <b>Состояние обновлено!</b>\n\n"
            response_text += result['analysis']
            
            if state_changed:
                prev_emoji = STATE_EMOJI.get(previous_state, '🌱')
                new_emoji = STATE_EMOJI.get(new_state, '🌱')
                prev_name = STATE_NAMES.get(previous_state, 'Здоровое')
                new_name = STATE_NAMES.get(new_state, 'Здоровое')
                
                response_text += f"\n\n🔄 <b>ИЗМЕНЕНИЕ СОСТОЯНИЯ!</b>\n"
                response_text += f"{prev_emoji} {prev_name} → {new_emoji} {new_name}\n\n"
                
                recommendations = get_state_recommendations(new_state, plant_name)
                response_text += f"\n{recommendations}"
            
            keyboard = [
                [InlineKeyboardButton(text="📊 История изменений", callback_data=f"view_state_history_{plant_id}")],
                [InlineKeyboardButton(text="🌿 К растению", callback_data=f"edit_plant_{plant_id}")],
            ]
            
            await message.reply(
                response_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
            
            await state.clear()
            
        else:
            await message.reply("❌ Ошибка анализа. Попробуйте другое фото.")
            await state.clear()
            
    except Exception as e:
        logger.error(f"Ошибка обновления состояния: {e}")
        await message.reply("❌ Техническая ошибка")
        await state.clear()

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработка фотографий - ГЛАВНЫЙ АНАЛИЗ"""
    try:
        processing_msg = await message.reply(
            "🔍 <b>Анализирую растение...</b>\n\n"
            "• Определяю вид\n"
            "• Анализирую состояние\n"
            "• Готовлю рекомендации...",
            parse_mode="HTML"
        )
        
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)
        
        user_question = message.caption if message.caption else None
        result = await analyze_plant_image(file_data.read(), user_question)
        
        await processing_msg.delete()
        
        if result["success"]:
            user_id = message.from_user.id
            
            temp_analyses[user_id] = {
                "analysis": result.get("raw_analysis", result["analysis"]),
                "formatted_analysis": result["analysis"],
                "photo_file_id": photo.file_id,
                "date": get_moscow_now(),
                "source": result.get("source", "unknown"),
                "plant_name": result.get("plant_name", "Неизвестное растение"),
                "confidence": result.get("confidence", 0),
                "needs_retry": result.get("needs_retry", False),
                "state_info": result.get("state_info", {})
            }
            
            state_info = result.get("state_info", {})
            current_state = state_info.get('current_state', 'healthy')
            
            state_recommendations = get_state_recommendations(
                current_state, 
                result.get("plant_name", "растение")
            )
            
            response_text = f"🌱 <b>Результат анализа:</b>\n\n{result['analysis']}"
            
            if current_state != 'healthy':
                response_text += f"\n\n{state_recommendations}"
            
            keyboard_buttons = [
                [InlineKeyboardButton(text="✅ Добавить в коллекцию", callback_data="save_plant")],
                [InlineKeyboardButton(text="❓ Вопрос о растении", callback_data="ask_about")],
            ]
            
            if result.get("needs_retry"):
                response_text += "\n\n📸 <b>Для лучшего результата сделайте фото при ярком свете</b>"
                keyboard_buttons.insert(1, [InlineKeyboardButton(text="🔄 Повторный анализ", callback_data="reanalyze")])
            
            keyboard_buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])
            
            await message.reply(
                response_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            )
        else:
            await message.reply("❌ Ошибка анализа", reply_markup=simple_back_menu())
            
    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}")
        await message.reply("❌ Техническая ошибка", reply_markup=simple_back_menu())

# === СОХРАНЕНИЕ РАСТЕНИЙ ===

@dp.callback_query(F.data == "save_plant")
async def save_plant_callback(callback: types.CallbackQuery):
    """Сохранение растения с состоянием"""
    user_id = callback.from_user.id
    
    if user_id in temp_analyses:
        try:
            analysis_data = temp_analyses[user_id]
            raw_analysis = analysis_data.get("analysis", "")
            state_info = analysis_data.get("state_info", {})
            
            watering_info = extract_personal_watering_info(raw_analysis)
            
            db = await get_db()
            plant_id = await db.save_plant(
                user_id=user_id,
                analysis=raw_analysis,
                photo_file_id=analysis_data["photo_file_id"],
                plant_name=analysis_data.get("plant_name", "Неизвестное растение")
            )
            
            personal_interval = watering_info["interval_days"]
            await db.update_plant_watering_interval(plant_id, personal_interval)
            
            current_state = state_info.get('current_state', 'healthy')
            state_reason = state_info.get('state_reason', 'Первичный анализ AI')
            
            await db.update_plant_state(
                plant_id=plant_id,
                user_id=user_id,
                new_state=current_state,
                change_reason=state_reason,
                photo_file_id=analysis_data["photo_file_id"],
                ai_analysis=raw_analysis,
                watering_adjustment=state_info.get('watering_adjustment', 0),
                feeding_adjustment=state_info.get('feeding_adjustment'),
                recommendations=state_info.get('recommendations', '')
            )
            
            await create_plant_reminder(plant_id, user_id, personal_interval)
            
            del temp_analyses[user_id]
            
            plant_name = analysis_data.get("plant_name", "растение")
            state_emoji = STATE_EMOJI.get(current_state, '🌱')
            state_name = STATE_NAMES.get(current_state, 'Здоровое')
            
            success_text = f"✅ <b>Растение добавлено!</b>\n\n"
            success_text += f"🌱 <b>{plant_name}</b> в вашей коллекции\n"
            success_text += f"{state_emoji} <b>Состояние:</b> {state_name}\n"
            success_text += f"⏰ Интервал полива: {personal_interval} дней\n\n"
            success_text += f"📸 Не забывайте обновлять фото раз в месяц!\n"
            success_text += f"💡 Я буду отслеживать изменения и адаптировать уход"
            
            await callback.message.answer(success_text, parse_mode="HTML", reply_markup=main_menu())
            
        except Exception as e:
            logger.error(f"Ошибка сохранения: {e}")
            await callback.message.answer("❌ Ошибка сохранения")
    else:
        await callback.message.answer("❌ Нет данных. Сначала проанализируйте растение")
    
    await callback.answer()

# === CALLBACK ОБРАБОТЧИКИ ===

@dp.callback_query(F.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    await callback.message.answer("🌱 <b>Главное меню</b>", parse_mode="HTML", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "my_plants")
async def my_plants_callback(callback: types.CallbackQuery):
    """Просмотр коллекции"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=15)
        
        if not plants:
            await callback.message.answer(
                "🌱 <b>Коллекция пуста</b>\n\nДобавьте первое растение!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            await callback.answer()
            return
        
        text = f"🌿 <b>Ваша коллекция ({len(plants)} растений):</b>\n\n"
        
        keyboard_buttons = []
        
        for i, plant in enumerate(plants, 1):
            plant_name = plant['display_name']
            
            if plant.get('type') == 'growing':
                stage_info = plant.get('stage_info', 'В процессе')
                text += f"{i}. 🌱 <b>{plant_name}</b>\n"
                text += f"   {stage_info}\n\n"
            else:
                current_state = plant.get('current_state', 'healthy')
                state_emoji = STATE_EMOJI.get(current_state, '🌱')
                
                moscow_now = get_moscow_now()
                
                if plant.get("last_watered"):
                    last_watered_utc = plant["last_watered"]
                    if last_watered_utc.tzinfo is None:
                        last_watered_utc = pytz.UTC.localize(last_watered_utc)
                    last_watered_moscow = last_watered_utc.astimezone(MOSCOW_TZ)
                    
                    days_ago = (moscow_now.date() - last_watered_moscow.date()).days
                    if days_ago == 0:
                        water_status = "💧 Сегодня"
                    elif days_ago == 1:
                        water_status = "💧 Вчера"
                    else:
                        water_status = f"💧 {days_ago}д"
                else:
                    water_status = "🆕 Новое"
                
                text += f"{i}. {state_emoji} <b>{plant_name}</b>\n"
                text += f"   {water_status}\n\n"
            
            short_name = plant_name[:15] + "..." if len(plant_name) > 15 else plant_name
            keyboard_buttons.append([
                InlineKeyboardButton(text=f"⚙️ {short_name}", callback_data=f"edit_plant_{plant['id']}")
            ])
        
        keyboard_buttons.extend([
            [InlineKeyboardButton(text="💧 Полить все", callback_data="water_plants")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ])
        
        await callback.message.answer(
            text, 
            parse_mode="HTML", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        )
        
    except Exception as e:
        logger.error(f"Ошибка коллекции: {e}")
        await callback.message.answer("❌ Ошибка загрузки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_plant_"))
async def edit_plant_callback(callback: types.CallbackQuery):
    """Меню редактирования растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_with_state(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = plant['display_name']
        current_state = plant.get('current_state', 'healthy')
        state_emoji = STATE_EMOJI.get(current_state, '🌱')
        state_name = STATE_NAMES.get(current_state, 'Здоровое')
        watering_interval = plant.get('watering_interval', 5)
        state_changes = plant.get('state_changes_count', 0)
        
        moscow_now = get_moscow_now()
        if plant.get("last_watered"):
            last_watered_utc = plant["last_watered"]
            if last_watered_utc.tzinfo is None:
                last_watered_utc = pytz.UTC.localize(last_watered_utc)
            last_watered_moscow = last_watered_utc.astimezone(MOSCOW_TZ)
            
            days_ago = (moscow_now.date() - last_watered_moscow.date()).days
            if days_ago == 0:
                water_status = "💧 Полито сегодня"
            elif days_ago == 1:
                water_status = "💧 Полито вчера"
            else:
                water_status = f"💧 Полито {days_ago} дней назад"
        else:
            water_status = "🆕 Еще не поливали"
        
        keyboard = [
            [InlineKeyboardButton(text="📸 Обновить состояние", callback_data=f"update_state_{plant_id}")],
            [InlineKeyboardButton(text="📊 История изменений", callback_data=f"view_state_history_{plant_id}")],
            [InlineKeyboardButton(text="💧 Полить сейчас", callback_data=f"water_plant_{plant_id}")],
            [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"rename_plant_{plant_id}")],
            [InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_plant_{plant_id}")],
            [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
        ]
        
        await callback.message.answer(
            f"⚙️ <b>Управление растением</b>\n\n"
            f"🌱 <b>{plant_name}</b>\n"
            f"{state_emoji} <b>Состояние:</b> {state_name}\n"
            f"{water_status}\n"
            f"⏰ Интервал: {watering_interval} дней\n"
            f"🔄 Изменений: {state_changes}\n\n"
            f"Выберите действие:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        logger.error(f"Ошибка меню: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("water_plant_"))
async def water_single_plant_callback(callback: types.CallbackQuery):
    """Полив растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        await db.update_watering(user_id, plant_id)
        
        interval = plant.get('watering_interval', 5)
        await create_plant_reminder(plant_id, user_id, interval)
        
        current_time = get_moscow_now().strftime("%d.%m.%Y в %H:%M")
        plant_name = plant['display_name']
        
        await callback.message.answer(
            f"💧 <b>Полив отмечен!</b>\n\n"
            f"🌱 <b>{plant_name}</b> полито {current_time}\n"
            f"⏰ Следующее напоминание через {interval} дней",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка полива: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()

@dp.callback_query(F.data == "water_plants")
async def water_plants_callback(callback: types.CallbackQuery):
    """Полив всех растений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        await db.update_watering(user_id)
        
        await callback.message.answer(
            "💧 <b>Полив отмечен!</b>\n\nВсе растения политы",
            parse_mode="HTML",
            reply_markup=simple_back_menu()
        )
        
    except Exception as e:
        logger.error(f"Ошибка полива: {e}")
        await callback.message.answer("❌ Ошибка")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("update_state_"))
async def update_state_callback(callback: types.CallbackQuery, state: FSMContext):
    """Обновить состояние растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        await state.update_data(
            updating_plant_state=True,
            state_plant_id=plant_id
        )
        
        await callback.message.answer(
            "📸 <b>Обновление состояния растения</b>\n\n"
            "Пришлите новое фото растения, и я:\n"
            "• Сравню с предыдущим состоянием\n"
            "• Определю изменения\n"
            "• Дам актуальные рекомендации\n\n"
            "📷 Пришлите фото сейчас:",
            parse_mode="HTML"
        )
        
        await state.set_state(PlantStates.waiting_state_update_photo)
        
    except Exception as e:
        logger.error(f"Ошибка обновления состояния: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("view_state_history_"))
async def view_state_history_callback(callback: types.CallbackQuery):
    """Просмотр истории состояний"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_with_state(plant_id, user_id)
        history = await db.get_plant_state_history(plant_id, limit=10)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = plant['display_name']
        current_state = plant.get('current_state', 'healthy')
        state_emoji = STATE_EMOJI.get(current_state, '🌱')
        state_name = STATE_NAMES.get(current_state, 'Здоровое')
        
        text = f"📊 <b>История состояний: {plant_name}</b>\n\n"
        text += f"{state_emoji} <b>Текущее:</b> {state_name}\n"
        text += f"📅 <b>Изменено:</b> {plant['state_changed_date'].strftime('%d.%m.%Y')}\n"
        text += f"🔄 <b>Всего изменений:</b> {plant.get('state_changes_count', 0)}\n\n"
        
        if history:
            text += f"📖 <b>История изменений:</b>\n\n"
            for entry in history[:5]:
                entry_date = entry['change_date'].strftime('%d.%m %H:%M')
                prev_emoji = STATE_EMOJI.get(entry['previous_state'], '🌱') if entry['previous_state'] else ''
                new_emoji = STATE_EMOJI.get(entry['new_state'], '🌱')
                
                text += f"📅 <b>{entry_date}</b>\n"
                if entry['previous_state']:
                    text += f"   {prev_emoji} → {new_emoji}\n"
                else:
                    text += f"   {new_emoji} Добавлено\n"
                
                if entry['change_reason']:
                    reason = entry['change_reason'][:50] + "..." if len(entry['change_reason']) > 50 else entry['change_reason']
                    text += f"   💬 {reason}\n"
                
                text += "\n"
        else:
            text += "📝 История пока пуста\n\n"
        
        keyboard = [
            [InlineKeyboardButton(text="📸 Обновить состояние", callback_data=f"update_state_{plant_id}")],
            [InlineKeyboardButton(text="🌿 К растению", callback_data=f"edit_plant_{plant_id}")],
        ]
        
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        logger.error(f"Ошибка просмотра истории: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("rename_plant_"))
async def rename_plant_callback(callback: types.CallbackQuery, state: FSMContext):
    """Переименование растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        current_name = plant['display_name']
        
        await state.update_data(editing_plant_id=plant_id)
        await state.set_state(PlantStates.editing_plant_name)
        
        await callback.message.answer(
            f"✏️ <b>Изменение названия</b>\n\n"
            f"🌱 Текущее: {current_name}\n\n"
            f"✍️ Напишите новое название:",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка переименования: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()

@dp.message(StateFilter(PlantStates.editing_plant_name))
async def handle_plant_rename(message: types.Message, state: FSMContext):
    """Обработка нового названия"""
    try:
        new_name = message.text.strip()
        
        if len(new_name) < 2:
            await message.reply("❌ Слишком короткое")
            return
        
        data = await state.get_data()
        plant_id = data.get('editing_plant_id')
        
        if not plant_id:
            await message.reply("❌ Ошибка данных")
            await state.clear()
            return
        
        user_id = message.from_user.id
        
        db = await get_db()
        await db.update_plant_name(plant_id, user_id, new_name)
        
        await message.reply(
            f"✅ <b>Название изменено!</b>\n\n"
            f"🌱 Новое название: <b>{new_name}</b>",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка переименования: {e}")
        await message.reply("❌ Ошибка сохранения")
        await state.clear()

@dp.callback_query(F.data.startswith("delete_plant_"))
async def delete_plant_callback(callback: types.CallbackQuery):
    """Удаление растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = plant['display_name']
        
        keyboard = [
            [InlineKeyboardButton(text="❌ Да, удалить", callback_data=f"confirm_delete_{plant_id}")],
            [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"edit_plant_{plant_id}")],
        ]
        
        await callback.message.answer(
            f"🗑️ <b>Удаление растения</b>\n\n"
            f"🌱 {plant_name}\n\n"
            f"⚠️ Это действие нельзя отменить\n\n"
            f"❓ Вы уверены?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_callback(callback: types.CallbackQuery):
    """Подтверждение удаления"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if plant:
            plant_name = plant['display_name']
            await db.delete_plant(user_id, plant_id)
            
            await callback.message.answer(
                f"🗑️ <b>Растение удалено</b>\n\n"
                f"❌ {plant_name} удалено из коллекции",
                parse_mode="HTML",
                reply_markup=simple_back_menu()
            )
        else:
            await callback.answer("❌ Растение не найдено")
        
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def stats_callback(callback: types.CallbackQuery):
    """Статистика"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        stats = await db.get_user_stats(user_id)
        
        stats_text = f"📊 <b>Статистика</b>\n\n"
        stats_text += f"🌱 Растений: {stats['total_plants']}\n"
        stats_text += f"💧 Поливов: {stats['total_waterings']}\n"
        
        if stats['total_growing'] > 0:
            stats_text += f"\n🌿 <b>Выращивание:</b>\n"
            stats_text += f"• Активных: {stats['active_growing']}\n"
            stats_text += f"• Завершенных: {stats['completed_growing']}\n"
        
        await callback.message.answer(
            stats_text,
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
    except Exception as e:
        logger.error(f"Ошибка статистики: {e}")
        await callback.message.answer("❌ Ошибка", reply_markup=main_menu())
    
    await callback.answer()

@dp.callback_query(F.data == "add_plant")
async def add_plant_callback(callback: types.CallbackQuery):
    await callback.message.answer("📸 <b>Пришлите фото растения</b>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "analyze")
async def analyze_callback(callback: types.CallbackQuery):
    await callback.message.answer("📸 <b>Пришлите фото для анализа</b>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "reanalyze")
async def reanalyze_callback(callback: types.CallbackQuery):
    await callback.message.answer("📸 <b>Пришлите новое фото</b>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "question")
async def question_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("❓ <b>Напишите ваш вопрос</b>", parse_mode="HTML")
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()

@dp.callback_query(F.data == "ask_about")
async def ask_about_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("❓ <b>Напишите вопрос о растении</b>", parse_mode="HTML")
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    """Справка"""
    await help_command(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("snooze_"))
async def snooze_reminder_callback(callback: types.CallbackQuery):
    """Отложить напоминание"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if plant:
            plant_name = plant['display_name']
            await create_plant_reminder(plant_id, user_id, 1)
            
            await callback.message.answer(
                f"⏰ <b>Напоминание отложено</b>\n\n"
                f"🌱 {plant_name}\n"
                f"📅 Завтра напомню полить",
                parse_mode="HTML"
            )
        
    except Exception as e:
        logger.error(f"Ошибка отложения: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()

@dp.callback_query(F.data == "disable_monthly_reminders")
async def disable_monthly_reminders_callback(callback: types.CallbackQuery):
    """Отключить месячные напоминания"""
    try:
        user_id = callback.from_user.id
        db = await get_db()
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_settings
                SET monthly_photo_reminder = FALSE
                WHERE user_id = $1
            """, user_id)
        
        await callback.message.answer(
            "🔕 <b>Месячные напоминания об обновлении фото отключены</b>\n\n"
            "Вы можете включить их обратно в настройках.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка отключения напоминаний: {e}")
    
    await callback.answer()

@dp.callback_query(F.data == "snooze_monthly_reminder")
async def snooze_monthly_reminder_callback(callback: types.CallbackQuery):
    """Отложить месячное напоминание"""
    try:
        user_id = callback.from_user.id
        db = await get_db()
        
        week_ago = datetime.now() - timedelta(days=23)
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_settings
                SET last_monthly_reminder = $1
                WHERE user_id = $2
            """, week_ago, user_id)
        
        await callback.message.answer(
            "⏰ <b>Напомню через неделю!</b>\n\n"
            "Тогда еще раз предложу обновить фото растений.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка отложения напоминания: {e}")
    
    await callback.answer()

# === ОНБОРДИНГ CALLBACKS ===

@dp.callback_query(F.data == "onboarding_demo")
async def onboarding_demo_callback(callback: types.CallbackQuery):
    """Демо анализа"""
    demo_text = (
        "🔍 <b>Смотрите! Вот как я анализирую растения:</b>\n\n"
        "🌿 <b>Фикус Бенджамина</b> (Ficus benjamina)\n"
        "🎯 <b>Уверенность:</b> 95%\n"
        "🌱 <b>Состояние:</b> Здоровое\n\n"
        "🔍 <b>Что видно:</b>\n"
        "✅ Листья: здоровые, зеленые\n"
        "❌ Почва: не видна в кадре\n\n"
        "💡 <b>Я отслеживаю изменения:</b>\n"
        "• Цветение → меняю режим полива\n"
        "• Стресс → даю срочные рекомендации\n"
        "• Активный рост → увеличиваю подкормку\n\n"
        "📸 <b>Месячные напоминания</b> обновить фото!"
    )
    
    keyboard = [
        [InlineKeyboardButton(text="📸 Проанализировать мое растение", callback_data="onboarding_try_analyze")],
        [InlineKeyboardButton(text="🌿 Вырастить что-то новое", callback_data="onboarding_try_grow")],
        [InlineKeyboardButton(text="❓ Задать вопрос о растениях", callback_data="onboarding_try_question")],
    ]
    
    await callback.message.answer(
        demo_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(F.data == "onboarding_quick_start")
async def onboarding_quick_start_callback(callback: types.CallbackQuery):
    """Быстрый старт"""
    keyboard = [
        [InlineKeyboardButton(text="📸 Проанализировать растение", callback_data="onboarding_try_analyze")],
        [InlineKeyboardButton(text="🌿 Вырастить с нуля", callback_data="onboarding_try_grow")],
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="onboarding_try_question")],
        [InlineKeyboardButton(text="💡 Сначала покажи пример", callback_data="onboarding_demo")],
    ]
    
    await callback.message.answer(
        "🎯 <b>Отлично! С чего начнем?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(F.data == "onboarding_try_analyze")
async def onboarding_try_analyze_callback(callback: types.CallbackQuery):
    """Попробовать анализ"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "📸 <b>Отлично! Пришлите фото вашего растения</b>\n\n"
        "💡 <b>Советы:</b>\n"
        "• Дневной свет\n"
        "• Покажите листья и общий вид\n"
        "• Включите почву если возможно\n\n"
        "📱 Жду ваше фото!",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "onboarding_try_grow")
async def onboarding_try_grow_callback(callback: types.CallbackQuery, state: FSMContext):
    """Попробовать выращивание"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "🌿 <b>Отлично! Выращиваем с нуля!</b>\n\n"
        "🌱 <b>Напишите, что хотите вырастить:</b>\n\n"
        "💡 Примеры: Базилик, Герань, Тюльпаны, Фикус\n\n"
        "✍️ Напишите название:",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.choosing_plant_to_grow)
    await callback.answer()

@dp.callback_query(F.data == "onboarding_try_question")
async def onboarding_try_question_callback(callback: types.CallbackQuery, state: FSMContext):
    """Попробовать вопрос"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "❓ <b>Задайте вопрос о растениях</b>\n\n"
        "💡 Помогу с:\n"
        "• Проблемами листьев\n"
        "• Режимом полива\n"
        "• Пересадкой\n"
        "• Болезнями\n\n"
        "✍️ Напишите вопрос:",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()

async def mark_onboarding_completed(user_id: int):
    """Отметить онбординг завершенным"""
    try:
        db = await get_db()
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET onboarding_completed = TRUE WHERE user_id = $1",
                user_id
            )
    except Exception as e:
        logger.error(f"Ошибка онбординга: {e}")

# === ВЫРАЩИВАНИЕ РАСТЕНИЙ ===

@dp.callback_query(F.data == "grow_from_scratch")
async def grow_from_scratch_callback(callback: types.CallbackQuery, state: FSMContext):
    """Выращивание с нуля"""
    await state.clear()
    
    await callback.message.answer(
        "🌿 <b>Выращиваем растение с нуля!</b>\n\n"
        "🌱 <b>Напишите, что хотите вырастить:</b>\n\n"
        "💡 <b>Примеры:</b> Базилик, Герань, Тюльпаны, Фикус, Помидоры\n\n"
        "✍️ Просто напишите название!",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.choosing_plant_to_grow)
    await callback.answer()

@dp.message(StateFilter(PlantStates.choosing_plant_to_grow))
async def handle_plant_choice_for_growing(message: types.Message, state: FSMContext):
    """Обработка выбора растения для выращивания"""
    try:
        plant_name = message.text.strip()
        
        if len(plant_name) < 2:
            await message.reply("🤔 Слишком короткое название")
            return
        
        processing_msg = await message.reply(
            f"🧠 <b>Готовлю план выращивания...</b>\n\n"
            f"🌱 Растение: {plant_name}",
            parse_mode="HTML"
        )
        
        growing_plan, task_calendar = await get_growing_plan_from_ai(plant_name)
        
        await processing_msg.delete()
        
        if growing_plan and task_calendar:
            await state.update_data(
                plant_name=plant_name,
                growing_plan=growing_plan,
                task_calendar=task_calendar
            )
            
            keyboard = [
                [InlineKeyboardButton(text="✅ Понятно, начинаем!", callback_data="confirm_growing_plan")],
                [InlineKeyboardButton(text="🔄 Другое растение", callback_data="grow_from_scratch")],
            ]
            
            await message.reply(
                f"🌱 <b>План готов!</b>\n\n{growing_plan}\n\n📋 Готовы начать?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        else:
            await message.reply(
                f"🤔 Не удалось составить план для '{plant_name}'",
                reply_markup=simple_back_menu()
            )
            await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка выбора растения: {e}")
        await message.reply("❌ Ошибка обработки", reply_markup=simple_back_menu())
        await state.clear()

@dp.callback_query(F.data == "confirm_growing_plan")
async def confirm_growing_plan_callback(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение плана выращивания"""
    try:
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        task_calendar = data.get('task_calendar')
        
        if not all([plant_name, growing_plan, task_calendar]):
            await callback.message.answer("❌ Данные потеряны. Начните заново.")
            await state.clear()
            await callback.answer()
            return
        
        user_id = callback.from_user.id
        db = await get_db()
        
        growing_id = await db.create_growing_plant(
            user_id=user_id,
            plant_name=plant_name,
            growth_method="from_seed",
            growing_plan=growing_plan,
            task_calendar=task_calendar
        )
        
        # Создаем первое напоминание
        first_task_date = datetime.now() + timedelta(days=1)
        await db.create_growing_reminder(
            growing_id=growing_id,
            user_id=user_id,
            reminder_type="task",
            next_date=first_task_date,
            stage_number=1,
            task_day=1
        )
        
        await callback.message.answer(
            f"✅ <b>Выращивание началось!</b>\n\n"
            f"🌱 <b>{plant_name}</b> добавлено в коллекцию\n\n"
            f"📅 Первое напоминание завтра\n"
            f"📸 Не забывайте фотографировать прогресс!",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка создания выращивания: {e}")
        await callback.message.answer("❌ Ошибка создания")
        await state.clear()
    
    await callback.answer()

# === ОБРАТНАЯ СВЯЗЬ ===

@dp.callback_query(F.data == "feedback")
async def feedback_callback(callback: types.CallbackQuery):
    """Меню обратной связи"""
    keyboard = [
        [InlineKeyboardButton(text="🐛 Сообщить о баге", callback_data="feedback_bug")],
        [InlineKeyboardButton(text="❌ Неточный анализ", callback_data="feedback_analysis_error")],
        [InlineKeyboardButton(text="💡 Предложение", callback_data="feedback_suggestion")],
        [InlineKeyboardButton(text="⭐ Общий отзыв", callback_data="feedback_review")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    
    await callback.message.answer(
        "📝 <b>Обратная связь</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("feedback_"))
async def feedback_type_callback(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа обратной связи"""
    feedback_type = callback.data.replace("feedback_", "")
    
    type_messages = {
        "bug": "🐛 <b>Сообщить о баге</b>\n\nОпишите проблему:",
        "analysis_error": "❌ <b>Неточный анализ</b>\n\nРасскажите о неправильном определении:",
        "suggestion": "💡 <b>Предложение</b>\n\nПоделитесь идеей:",
        "review": "⭐ <b>Общий отзыв</b>\n\nПоделитесь впечатлениями:"
    }
    
    await state.update_data(feedback_type=feedback_type)
    await state.set_state(FeedbackStates.writing_message)
    
    await callback.message.answer(
        type_messages.get(feedback_type, "Напишите сообщение:"),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(StateFilter(FeedbackStates.writing_message))
async def handle_feedback_message(message: types.Message, state: FSMContext):
    """Обработка сообщения обратной связи"""
    try:
        feedback_text = message.text.strip() if message.text else ""
        feedback_photo = None
        if message.photo:
            feedback_photo = message.photo[-1].file_id
        
        if not feedback_text and not feedback_photo:
            await message.reply("📝 Напишите сообщение или приложите фото")
            return
        
        if feedback_text and len(feedback_text) < 5:
            await message.reply("📝 Слишком короткое (минимум 5 символов)")
            return
        
        data = await state.get_data()
        feedback_type = data.get('feedback_type', 'review')
        
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.first_name or f"user_{user_id}"
        
        db = await get_db()
        await db.save_feedback(
            user_id=user_id,
            username=username,
            feedback_type=feedback_type,
            message=feedback_text or "Фото без комментария",
            photo_file_id=feedback_photo
        )
        
        await message.answer(
            "✅ <b>Спасибо за отзыв!</b>\n\nВаше сообщение поможет улучшить бота.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка обратной связи: {e}")
        await message.reply("❌ Ошибка обработки")
        await state.clear()

# === ВОПРОСЫ О РАСТЕНИЯХ ===

@dp.message(StateFilter(PlantStates.waiting_question))
async def handle_question(message: types.Message, state: FSMContext):
    """Обработка вопросов"""
    try:
        processing_msg = await message.reply("🤔 <b>Консультируюсь...</b>", parse_mode="HTML")
        
        user_id = message.from_user.id
        user_context = ""
        
        if user_id in temp_analyses:
            plant_info = temp_analyses[user_id]
            plant_name = plant_info.get("plant_name", "растение")
            user_context = f"\n\nКонтекст: Недавно анализировал {plant_name}."
        
        answer = None
        
        if openai_client:
            try:
                response = await openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "Вы - эксперт по растениям. Отвечайте практично."},
                        {"role": "user", "content": f"{message.text}{user_context}"}
                    ],
                    max_tokens=800,
                    temperature=0.3
                )
                answer = response.choices[0].message.content
            except Exception as e:
                logger.error(f"OpenAI error: {e}")
        
        await processing_msg.delete()
        
        if answer and len(answer) > 50:
            await message.reply(answer, parse_mode="HTML")
        else:
            await message.reply(
                "🤔 Не могу дать ответ. Попробуйте переформулировать.",
                reply_markup=main_menu()
            )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка ответа: {e}")
        await message.reply("❌ Ошибка обработки", reply_markup=main_menu())
        await state.clear()

# === ДИАГНОСТИЧЕСКИЙ ОБРАБОТЧИК ===

@dp.message()
async def catch_all_messages(message: types.Message):
    """Диагностический обработчик всех сообщений"""
    logger.info(f"📨 Получено сообщение от {message.from_user.id}: {message.text[:50] if message.text else 'не текст'}")
    
    # Если сообщение текстовое и не обработано выше
    if message.text:
        await message.reply(
            "🤔 Не понял команду. Используйте /start для начала работы.",
            reply_markup=main_menu()
        )
    else:
        # Если это медиа без обработчика
        await message.reply(
            "📸 Пришлите фото растения для анализа, или используйте /start",
            reply_markup=main_menu()
        )

# === WEBHOOK И ЗАПУСК ===

async def on_startup():
    """Инициализация"""
    try:
        await init_database()
        logger.info("✅ База данных инициализирована")
        
        # КРИТИЧЕСКИ ВАЖНО: Удаляем webhook перед polling
        logger.info("🔧 Удаление старого webhook...")
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            logger.warning(f"⚠️ Найден активный webhook: {webhook_info.url}")
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Webhook удален")
        else:
            logger.info("ℹ️ Webhook не был установлен")
        
        # Ежедневные напоминания в 9:00
        scheduler.add_job(
            check_and_send_reminders,
            'cron',
            hour=9,
            minute=0,
            id='reminder_check',
            replace_existing=True
        )
        
        # Месячные напоминания в 10:00
        scheduler.add_job(
            check_monthly_photo_reminders,
            'cron',
            hour=10,
            minute=0,
            id='monthly_reminder_check',
            replace_existing=True
        )
        
        scheduler.start()
        logger.info("🔔 Планировщик запущен")
        logger.info("⏰ Ежедневные напоминания: 9:00 МСК")
        logger.info("📸 Месячные напоминания: 10:00 МСК")
        
        if WEBHOOK_URL:
            await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
            logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}/webhook")
        else:
            logger.info("✅ Polling mode активирован")
            
    except Exception as e:
        logger.error(f"❌ Ошибка запуска: {e}")
        raise

async def on_shutdown():
    """Завершение"""
    logger.info("🛑 Остановка бота...")
    
    if scheduler.running:
        scheduler.shutdown()
        logger.info("⏰ Планировщик остановлен")
    
    try:
        db = await get_db()
        await db.close()
        logger.info("✅ База данных закрыта")
    except:
        pass
    
    try:
        await bot.session.close()
        logger.info("✅ Сессия бота закрыта")
    except:
        pass

async def webhook_handler(request):
    """Webhook обработчик"""
    try:
        url = str(request.url)
        index = url.rfind('/')
        token = url[index + 1:]
        
        if token == BOT_TOKEN.split(':')[1]:
            update = types.Update.model_validate(await request.json(), strict=False)
            await dp.feed_update(bot, update)
            return web.Response()
        else:
            logger.warning("⚠️ Неверный токен в webhook")
            return web.Response(status=403)
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return web.Response(status=500)

async def health_check(request):
    """Health check endpoint"""
    return web.json_response({
        "status": "healthy", 
        "bot": "Bloom AI", 
        "version": "4.0 - State System"
    })

async def main():
    """Main функция"""
    try:
        logger.info("🚀 Запуск Bloom AI...")
        logger.info(f"🔑 BOT_TOKEN: {'✅ Установлен' if BOT_TOKEN else '❌ Отсутствует'}")
        logger.info(f"🔑 OPENAI_API_KEY: {'✅ Установлен' if OPENAI_API_KEY else '❌ Отсутствует'}")
        logger.info(f"🌐 WEBHOOK_URL: {WEBHOOK_URL if WEBHOOK_URL else '❌ Не установлен (polling режим)'}")
        
        await on_startup()
        
        if WEBHOOK_URL:
            app = web.Application()
            app.router.add_post('/webhook', webhook_handler)
            app.router.add_get('/health', health_check)
            app.router.add_get('/', health_check)
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', PORT)
            await site.start()
            
            logger.info(f"🚀 Bloom AI v4.0 запущен на порту {PORT}")
            logger.info(f"📊 Система отслеживания состояний активна!")
            logger.info(f"📸 Месячные напоминания включены!")
            
            try:
                await asyncio.Future()
            except KeyboardInterrupt:
                logger.info("🛑 Остановка через KeyboardInterrupt")
            finally:
                await runner.cleanup()
                await on_shutdown()
        else:
            logger.info("🤖 Запуск в режиме polling")
            logger.info("⏳ Ожидание сообщений от пользователей...")
            try:
                await dp.start_polling(bot, drop_pending_updates=True)
            except KeyboardInterrupt:
                logger.info("🛑 Остановка через KeyboardInterrupt")
            finally:
                await on_shutdown()
                
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Ошибка запуска: {e}", exc_info=True)
