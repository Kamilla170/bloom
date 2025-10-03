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

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PLANTID_API_KEY = os.getenv("PLANTID_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)

temp_analyses = {}

PLANT_IDENTIFICATION_PROMPT = """
Вы - эксперт-ботаник. Внимательно изучите фотографию растения и дайте максимально точную идентификацию.

ВАЖНО: Анализируйте только то, что ВИДНО на фотографии. Если почва не видна - не давайте советы по поливу. Если корни не видны - не анализируйте корневую систему.

Анализируйте:
1. Форму и текстуру листьев (овальные/длинные/мясистые/глянцевые/матовые)
2. Расположение листьев на стебле
3. Цвет и прожилки листьев
4. Форму роста растения
5. Видимые цветы или плоды
6. Размер растения и горшка

АНАЛИЗ ПОЛИВА - только если почва видна:
- Осмотрите листья на предмет увядания, желтизны, коричневых пятен
- Оцените упругость и тургор листьев
- Проанализируйте признаки переувлажнения или пересушивания
- Посмотрите на состояние почвы (если видно)

Дайте ответ в формате:
РАСТЕНИЕ: [Точное название вида на русском и латинском языке]
УВЕРЕННОСТЬ: [процент уверенности в идентификации]
ПРИЗНАКИ: [ключевые признаки, по которым определили]
СЕМЕЙСТВО: [ботаническое семейство]
РОДИНА: [естественная среда обитания]

СОСТОЯНИЕ: [детальная оценка здоровья по видимым листьям, цвету, упругости]

ПОЛИВ_АНАЛИЗ: [если почва видна - анализ состояния полива, иначе: "Почва не видна - невозможно оценить полив"]
ПОЛИВ_РЕКОМЕНДАЦИИ: [если можете оценить состояние полива - конкретные рекомендации, иначе: "Проверьте влажность почвы пальцем"]
ПОЛИВ_ИНТЕРВАЛ: [если можете оценить - рекомендуемый интервал в днях: 2-15, иначе: число дней для данного вида растения]

СВЕТ: [точные требования к освещению для данного растения]
ТЕМПЕРАТУРА: [оптимальный диапазон для этого вида]
ВЛАЖНОСТЬ: [требования к влажности воздуха]
ПОДКОРМКА: [рекомендации по удобрениям]
ПЕРЕСАДКА: [когда и как пересаживать этот вид]

ПРОБЛЕМЫ: [возможные болезни и вредители характерные для этого вида]
СОВЕТ: [специфический совет для улучшения ухода за этим конкретным растением]

Будьте максимально точными в идентификации. Если почва не видна - честно укажите это в анализе полива.
"""

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

# === СИСТЕМА НАПОМИНАНИЙ ===

async def check_and_send_reminders():
    """Проверка и отправка напоминаний о поливе и этапах выращивания (ежедневно утром)"""
    try:
        db = await get_db()
        
        moscow_now = get_moscow_now()
        moscow_date = moscow_now.date()
        
        # Напоминания о поливе обычных растений
        async with db.pool.acquire() as conn:
            plants_to_water = await conn.fetch("""
                SELECT p.id, p.user_id, 
                       COALESCE(p.custom_name, p.plant_name, 'Растение #' || p.id) as display_name,
                       p.last_watered, 
                       COALESCE(p.watering_interval, 5) as watering_interval, 
                       p.photo_file_id, p.notes
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
        
        # Напоминания по этапам выращивания
        await check_and_send_growing_reminders()
                
    except Exception as e:
        print(f"Ошибка проверки напоминаний: {e}")

async def check_and_send_growing_reminders():
    """Проверка и отправка напоминаний по календарю задач"""
    try:
        db = await get_db()
        moscow_now = get_moscow_now()
        
        # Получаем выращиваемые растения с активными напоминаниями на сегодня
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
        print(f"Ошибка проверки напоминаний выращивания: {e}")

async def send_task_reminder(reminder_row):
    """Отправка напоминания с конкретной задачей из календаря"""
    try:
        user_id = reminder_row['user_id']
        growing_id = reminder_row['growing_id']
        plant_name = reminder_row['plant_name']
        task_day = reminder_row['task_day']
        task_calendar = reminder_row['task_calendar']
        current_stage = reminder_row['current_stage']
        started_date = reminder_row['started_date']
        
        # Находим задачу в календаре
        stage_key = f"stage_{current_stage + 1}"
        task_info = None
        
        if task_calendar and stage_key in task_calendar:
            tasks = task_calendar[stage_key].get('tasks', [])
            for task in tasks:
                if task.get('day') == task_day:
                    task_info = task
                    break
        
        if not task_info:
            print(f"⚠️ Задача на день {task_day} не найдена в календаре")
            return
        
        # Формируем сообщение с задачей
        task_icon = task_info.get('icon', '📋')
        task_title = task_info.get('title', 'Задача')
        task_description = task_info.get('description', '')
        task_type = task_info.get('type', 'care')
        
        # Вычисляем день с начала выращивания
        days_since_start = (get_moscow_now().date() - started_date.date()).days
        
        message_text = f"{task_icon} <b>Время для важного действия!</b>\n\n"
        message_text += f"🌱 <b>{plant_name}</b>\n"
        message_text += f"📅 День {days_since_start} выращивания\n\n"
        message_text += f"📋 <b>{task_title}</b>\n"
        message_text += f"📝 {task_description}\n\n"
        
        # Добавляем дополнительные советы в зависимости от типа задачи
        if task_type == 'watering':
            message_text += f"💡 <b>Совет:</b> Проверьте влажность почвы перед поливом\n"
        elif task_type == 'feeding':
            message_text += f"💡 <b>Совет:</b> Используйте слабый раствор, чтобы не обжечь корни\n"
        elif task_type == 'transplant':
            message_text += f"💡 <b>Совет:</b> Пересаживайте вечером, чтобы растение легче перенесло стресс\n"
        
        message_text += f"\n📸 Не забудьте сфотографировать результат для дневника!"
        
        # Кнопки для управления
        keyboard = [
            [InlineKeyboardButton(text="✅ Выполнено!", callback_data=f"task_done_{growing_id}_{task_day}")],
            [InlineKeyboardButton(text="📸 Добавить фото", callback_data=f"add_diary_photo_{growing_id}")],
            [InlineKeyboardButton(text="📝 Записать заметку", callback_data=f"add_diary_note_{growing_id}")],
            [InlineKeyboardButton(text="⏰ Напомнить завтра", callback_data=f"snooze_growing_{growing_id}")],
        ]
        
        # Отправляем уведомление
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
        
        # Отмечаем что напоминание отправлено
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
        
        # Планируем следующую задачу
        await schedule_next_task_reminder(growing_id, user_id, task_calendar, task_day)
        
        print(f"📤 Отправлено напоминание задачи '{task_title}' для {plant_name}")
        
    except Exception as e:
        print(f"Ошибка отправки напоминания задачи: {e}")

async def schedule_next_task_reminder(growing_id: int, user_id: int, task_calendar: dict, current_day: int):
    """Запланировать следующее напоминание по календарю"""
    try:
        db = await get_db()
        
        # Получаем информацию о растении
        growing_plant = await db.get_growing_plant_by_id(growing_id, user_id)
        if not growing_plant:
            return
        
        current_stage = growing_plant['current_stage']
        stage_key = f"stage_{current_stage + 1}"
        
        # Ищем следующую задачу
        if stage_key in task_calendar and 'tasks' in task_calendar[stage_key]:
            tasks = task_calendar[stage_key]['tasks']
            
            # Сортируем по дню и ищем следующую задачу
            sorted_tasks = sorted(tasks, key=lambda x: x.get('day', 0))
            
            for task in sorted_tasks:
                task_day = task.get('day', 0)
                
                # Берём только будущие задачи
                if task_day > current_day:
                    # Вычисляем дату напоминания
                    started_date = growing_plant['started_date']
                    reminder_date = started_date + timedelta(days=task_day)
                    
                    # Конвертируем в naive для PostgreSQL
                    reminder_date_naive = reminder_date.replace(tzinfo=None) if reminder_date.tzinfo else reminder_date
                    
                    # Создаём напоминание
                    await db.create_growing_reminder(
                        growing_id=growing_id,
                        user_id=user_id,
                        reminder_type="task",
                        next_date=reminder_date_naive,
                        stage_number=current_stage + 1,
                        task_day=task_day
                    )
                    
                    print(f"📅 Запланировано напоминание на день {task_day}: {task.get('title')}")
                    return
        
        print(f"ℹ️ Нет больше задач для этапа {current_stage + 1}")
        
    except Exception as e:
        print(f"Ошибка планирования напоминания: {e}")

async def send_watering_reminder(plant_row):
    """Отправка персонализированного напоминания о поливе"""
    try:
        user_id = plant_row['user_id']
        plant_id = plant_row['id']
        plant_name = plant_row['display_name']
        
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
        
        message_text = f"💧 <b>Время полить растение!</b>\n\n"
        message_text += f"🌱 <b>{plant_name}</b>\n"
        message_text += f"⏰ {time_info}\n"
        
        if plant_info and plant_info.get('notes'):
            notes = plant_info['notes']
            if "Персональные рекомендации по поливу:" in notes:
                personal_rec = notes.replace("Персональные рекомендации по поливу:", "").strip()
                message_text += f"\n💡 <b>Ваши персональные рекомендации:</b>\n{personal_rec}\n"
            else:
                message_text += f"\n📝 <b>Заметка:</b> {notes}\n"
        else:
            message_text += f"\n💡 Проверьте влажность почвы пальцем\n"
        
        interval = plant_row.get('watering_interval', 5)
        message_text += f"\n⏱️ <i>Интервал полива: каждые {interval} дней</i>"
        
        keyboard = [
            [InlineKeyboardButton(text="💧 Полил(а)!", callback_data=f"water_plant_{plant_id}")],
            [InlineKeyboardButton(text="⏰ Напомнить завтра", callback_data=f"snooze_{plant_id}")],
            [InlineKeyboardButton(text="🔧 Настройки растения", callback_data=f"edit_plant_{plant_id}")],
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
        
        print(f"📤 Отправлено персональное напоминание пользователю {user_id} о растении {plant_name}")
        
    except Exception as e:
        print(f"Ошибка отправки напоминания: {e}")

async def create_plant_reminder(plant_id: int, user_id: int, interval_days: int = 5):
    """Создать напоминание для нового растения (московское время)"""
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
        print(f"Ошибка создания напоминания: {e}")

# === CALLBACK ОБРАБОТЧИКИ ДЛЯ НАПОМИНАНИЙ ===

@dp.callback_query(F.data.startswith("task_done_"))
async def task_done_callback(callback: types.CallbackQuery):
    """Отметка задачи как выполненной"""
    try:
        parts = callback.data.split("_")
        growing_id = int(parts[2])
        task_day = int(parts[3])
        user_id = callback.from_user.id
        
        db = await get_db()
        
        # Добавляем запись в дневник
        await db.add_diary_entry(
            growing_id=growing_id,
            user_id=user_id,
            entry_type='task_completed',
            description=f"Выполнена задача дня {task_day}"
        )
        
        await callback.message.answer(
            f"✅ <b>Задача выполнена!</b>\n\n"
            f"Отличная работа! Запись добавлена в дневник роста.\n"
            f"📸 Не забудьте добавить фото результата!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📸 Добавить фото", callback_data=f"add_diary_photo_{growing_id}")],
                [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
            ])
        )
        
    except Exception as e:
        print(f"Ошибка отметки задачи: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("snooze_"))
async def snooze_reminder_callback(callback: types.CallbackQuery):
    """Отложить напоминание на завтра"""
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
                f"🌱 <b>{plant_name}</b>\n"
                f"📅 Завтра напомню полить это растение\n"
                f"💡 Если забудете - можете отметить полив в любой момент",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💧 Полил(а) сейчас", callback_data=f"water_plant_{plant_id}")],
                    [InlineKeyboardButton(text="⚙️ Настройки растения", callback_data=f"edit_plant_{plant_id}")],
                ])
            )
        
    except Exception as e:
        print(f"Ошибка отложения напоминания: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("snooze_growing_"))
async def snooze_growing_reminder_callback(callback: types.CallbackQuery):
    """Отложить напоминание по выращиванию"""
    try:
        growing_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        growing_plant = await db.get_growing_plant_by_id(growing_id, user_id)
        
        if growing_plant:
            plant_name = growing_plant['plant_name']
            
            await callback.message.answer(
                f"⏰ <b>Напоминание по выращиванию отложено</b>\n\n"
                f"🌱 <b>{plant_name}</b>\n"
                f"📅 Завтра напомню о следующем этапе",
                parse_mode="HTML"
            )
        
    except Exception as e:
        print(f"Ошибка отложения напоминания выращивания: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data == "continue_as_question")
async def continue_as_question_callback(callback: types.CallbackQuery, state: FSMContext):
    """Продолжить как обычный вопрос"""
    await callback.message.answer(
        "❓ <b>Хорошо, обрабатываю как вопрос о растениях</b>\n\n"
        "Дайте мне секунду для консультации...",
        parse_mode="HTML"
    )
    await callback.answer()

# === ОБРАБОТЧИКИ ВЫРАЩИВАНИЯ ===

@dp.callback_query(F.data == "grow_from_scratch")
async def grow_from_scratch_callback(callback: types.CallbackQuery, state: FSMContext):
    """Упрощенный флоу - сразу спрашиваем что хотят вырастить"""
    await state.clear()
    
    await callback.message.answer(
        "🌿 <b>Выращиваем растение с нуля!</b>\n\n"
        "Я стану вашим персональным наставником и помогу "
        "вырастить растение от семечка до взрослого!\n\n"
        "🌱 <b>Напишите, что хотите вырастить:</b>\n\n"
        "💡 <b>Примеры:</b>\n"
        "• Базилик\n"
        "• Герань\n"
        "• Тюльпаны\n"
        "• Фикус\n"
        "• Помидоры\n"
        "• Укроп\n"
        "• Фиалка\n"
        "• Кактус\n\n"
        "✍️ Просто напишите название растения, а я подберу лучший способ выращивания и составлю подробный план с календарём ключевых задач!",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.choosing_plant_to_grow)
    await callback.answer()

def create_default_task_calendar(plant_name: str) -> dict:
    """Создает базовый календарь задач если AI не смог"""
    return {
        "stage_1": {
            "tasks": [
                {"day": 0, "type": "preparation", "title": "Подготовка", "description": "Подготовьте горшки, почву и семена/черенки", "icon": "🪴"},
                {"day": 1, "type": "planting", "title": "Посадка", "description": f"Посадите {plant_name}", "icon": "🌱"}
            ]
        },
        "stage_2": {
            "tasks": [
                {"day": 3, "type": "watering", "title": "Первый полив", "description": "Обильно полейте почву", "icon": "💧"},
                {"day": 7, "type": "observation", "title": "Проверка всходов", "description": "Проверьте появление первых ростков", "icon": "👀"}
            ]
        },
        "stage_3": {
            "tasks": [
                {"day": 14, "type": "watering", "title": "Регулярный полив", "description": "Поливайте по мере подсыхания почвы", "icon": "💧"},
                {"day": 21, "type": "feeding", "title": "Первая подкормка", "description": "Внесите слабый раствор удобрения", "icon": "🍽️"}
            ]
        },
        "stage_4": {
            "tasks": [
                {"day": 35, "type": "care", "title": "Финальный уход", "description": "Растение готово!", "icon": "✅"}
            ]
        }
    }

async def get_growing_plan_from_ai(plant_name: str) -> tuple:
    """Получает план выращивания от ИИ с календарём ключевых задач"""
    if not openai_client:
        return None, None
    
    try:
        prompt = f"""
Создайте подробный план выращивания растения "{plant_name}" для начинающего садовода.

Ответьте В ДВУХ ЧАСТЯХ:

ЧАСТЬ 1 - ТЕКСТОВЫЙ ПЛАН (для отображения пользователю):
🌱 РАСТЕНИЕ: {plant_name}
🎯 СПОСОБ ВЫРАЩИВАНИЯ: [семена/черенки/луковицы/другое]
📋 СЛОЖНОСТЬ: [легко/средне/сложно]
⏰ ВРЕМЯ ДО РЕЗУЛЬТАТА: [сроки]

📝 ПОШАГОВЫЙ ПЛАН:
🌱 ЭТАП 1: ПОДГОТОВКА ([сроки])
• [действие 1]
• [действие 2]

🌿 ЭТАП 2: ПОСАДКА/ПОСЕВ ([сроки])
• [действие 1]
• [действие 2]

🌱 ЭТАП 3: УХОД В ПЕРИОД РОСТА ([сроки])
• [действие 1]
• [действие 2]

🌸 ЭТАП 4: ВЗРОСЛОЕ РАСТЕНИЕ ([сроки])
• [действие 1]
• [действие 2]

💡 ВАЖНЫЕ СОВЕТЫ:
• [совет 1]
• [совет 2]

---CALENDAR_JSON---

ЧАСТЬ 2 - КАЛЕНДАРЬ ЗАДАЧ (строго в JSON формате):
{{
  "stage_1": {{
    "tasks": [
      {{"day": 0, "type": "preparation", "title": "Подготовка материалов", "description": "Подготовьте горшки, почву, семена/черенки", "icon": "🪴"}},
      {{"day": 1, "type": "planting", "title": "Посадка", "description": "Посадите семена на глубину X см", "icon": "🌱"}}
    ]
  }},
  "stage_2": {{
    "tasks": [
      {{"day": 3, "type": "watering", "title": "Первый полив", "description": "Обильно полейте после появления всходов", "icon": "💧"}},
      {{"day": 7, "type": "observation", "title": "Проверка всходов", "description": "Убедитесь что всходы здоровые", "icon": "👀"}},
      {{"day": 10, "type": "feeding", "title": "Первая подкормка", "description": "Внесите слабый раствор удобрения", "icon": "🍽️"}}
    ]
  }},
  "stage_3": {{
    "tasks": [
      {{"day": 14, "type": "watering", "title": "Регулярный полив", "description": "Поливайте каждые 2-3 дня", "icon": "💧"}},
      {{"day": 21, "type": "feeding", "title": "Вторая подкормка", "description": "Удобрение для роста", "icon": "🍽️"}},
      {{"day": 28, "type": "transplant", "title": "Пересадка", "description": "Пересадите в больший горшок", "icon": "🪴"}}
    ]
  }},
  "stage_4": {{
    "tasks": [
      {{"day": 35, "type": "care", "title": "Финальный уход", "description": "Проверьте готовность к использованию", "icon": "✅"}}
    ]
  }}
}}

ВАЖНО:
- day - это день с НАЧАЛА выращивания (не с начала этапа!)
- Задачи должны быть только в КЛЮЧЕВЫЕ дни (полив, подкормка, пересадка, важные проверки)
- НЕ создавайте задачи на каждый день
- Используйте типы: preparation, planting, watering, feeding, transplant, observation, care
- Иконки: 🪴 подготовка, 🌱 посадка, 💧 полив, 🍽️ подкормка, 👀 проверка, ✅ готово
"""
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system", 
                    "content": "Вы - эксперт по выращиванию растений. Создавайте практичные планы с календарём ключевых задач. Отвечайте строго по формату с разделителем ---CALENDAR_JSON---"
                },
                {"role": "user", "content": prompt}
            ],
            max_tokens=1500,
            temperature=0.3
        )
        
        full_response = response.choices[0].message.content
        
        # Разделяем на текстовый план и JSON календарь
        if "---CALENDAR_JSON---" in full_response:
            parts = full_response.split("---CALENDAR_JSON---")
            text_plan = parts[0].strip()
            
            # Убираем служебные заголовки из текстового плана
            text_plan = text_plan.replace("ЧАСТЬ 1 - ТЕКСТОВЫЙ ПЛАН (для отображения пользователю):", "").strip()
            text_plan = text_plan.replace("ЧАСТЬ 1 - ТЕКСТОВЫЙ ПЛАН:", "").strip()
            
            calendar_json_str = parts[1].strip() if len(parts) > 1 else None
            
            # Парсим JSON календарь
            task_calendar = None
            if calendar_json_str:
                try:
                    # Ищем JSON блок
                    import re
                    json_match = re.search(r'\{.*\}', calendar_json_str, re.DOTALL)
                    if json_match:
                        task_calendar = json.loads(json_match.group(0))
                except Exception as e:
                    print(f"Ошибка парсинга календаря: {e}")
                    task_calendar = create_default_task_calendar(plant_name)
            
            return text_plan, task_calendar
        else:
            return full_response, create_default_task_calendar(plant_name)
        
    except Exception as e:
        print(f"Ошибка получения плана выращивания: {e}")
        return None, None

@dp.message(StateFilter(PlantStates.choosing_plant_to_grow))
async def handle_plant_choice_for_growing(message: types.Message, state: FSMContext):
    """Обработка выбора растения для выращивания"""
    try:
        plant_name = message.text.strip()
        
        if len(plant_name) < 2:
            await message.reply(
                "🤔 Слишком короткое название растения.\n"
                "Попробуйте еще раз, например: 'базилик' или 'герань'"
            )
            return
        
        if len(plant_name) > 100:
            await message.reply(
                "📝 Слишком длинное описание.\n"
                "Напишите название покороче, например: 'фикус' или 'помидоры'"
            )
            return
        
        processing_msg = await message.reply(
            f"🧠 <b>Готовлю персональный план выращивания...</b>\n\n"
            f"🌱 Растение: {plant_name}\n"
            f"🔍 Анализирую лучший способ выращивания\n"
            f"📅 Составляю календарь ключевых задач...",
            parse_mode="HTML"
        )
        
        # Получаем план И календарь от AI
        growing_plan, task_calendar = await get_growing_plan_from_ai(plant_name)
        
        await processing_msg.delete()
        
        if growing_plan and task_calendar:
            # Сохраняем план И календарь в состояние
            await state.update_data(
                plant_name=plant_name,
                growing_plan=growing_plan,
                task_calendar=task_calendar
            )
            
            # Показываем превью календаря
            calendar_preview = "\n\n📅 <b>Календарь ключевых задач:</b>\n"
            task_count = sum(len(stage.get('tasks', [])) for stage in task_calendar.values())
            calendar_preview += f"✅ Создано {task_count} ключевых напоминаний\n"
            calendar_preview += f"💡 Вы получите уведомления только в важные дни"
            
            keyboard = [
                [InlineKeyboardButton(text="✅ Понятно, начинаем!", callback_data="confirm_growing_plan")],
                [InlineKeyboardButton(text="🔄 Выбрать другое растение", callback_data="grow_from_scratch")],
                [InlineKeyboardButton(text="❓ Задать вопрос по плану", callback_data="ask_about_plan")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
            ]
            
            response_text = f"🌱 <b>Персональный план готов!</b>\n\n{growing_plan}{calendar_preview}\n\n"
            response_text += f"📋 Готовы начать? Я буду помогать на каждом ключевом этапе!"
            
            await message.reply(
                response_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        else:
            fallback_keyboard = [
                [InlineKeyboardButton(text="🔄 Попробовать еще раз", callback_data="grow_from_scratch")],
                [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="question")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
            ]
            
            await message.reply(
                f"🤔 <b>Не удалось составить план для '{plant_name}'</b>\n\n"
                f"💡 Попробуйте написать название по-другому или выберите более популярное растение.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=fallback_keyboard)
            )
            await state.clear()
        
    except Exception as e:
        print(f"Ошибка обработки выбора растения: {e}")
        await message.reply(
            "❌ Произошла ошибка при обработке.\n"
            "Попробуйте еще раз или выберите другое растение.",
            reply_markup=simple_back_menu()
        )
        await state.clear()

@dp.callback_query(F.data == "confirm_growing_plan")
async def confirm_growing_plan_callback(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение плана и запуск выращивания - упрощенный без фото"""
    try:
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        task_calendar = data.get('task_calendar')
        
        if not plant_name or not growing_plan:
            await callback.message.answer(
                "❌ <b>Данные плана не найдены</b>\n\n"
                "Попробуйте создать план заново:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌿 Создать новый план", callback_data="grow_from_scratch")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
                ])
            )
            await callback.answer()
            return
        
        # Сразу создаем план без предложения фото
        user_id = callback.from_user.id
        await finalize_growing_setup(callback.message, state, None, user_id)
        
        await callback.answer()
        
    except Exception as e:
        print(f"Ошибка подтверждения плана: {e}")
        import traceback
        traceback.print_exc()
        
        await callback.message.answer(
            "❌ <b>Техническая ошибка</b>\n\n"
            "Попробуйте создать план заново.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌿 Создать план", callback_data="grow_from_scratch")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
            ])
        )
        await callback.answer("❌ Ошибка обработки")

async def finalize_growing_setup(message_obj, state: FSMContext, photo_file_id: str, user_id: int):
    """Финализация настройки выращивания с календарём задач"""
    try:
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        task_calendar = data.get('task_calendar')
        
        if not plant_name or not growing_plan:
            await message_obj.answer(
                "❌ <b>Данные плана не найдены</b>\n\n"
                "Попробуйте создать план заново.",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            await state.clear()
            return
        
        # Определяем способ выращивания
        growth_method = "семена"
        if growing_plan:
            for line in growing_plan.split('\n'):
                if line.startswith("🎯 СПОСОБ ВЫРАЩИВАНИЯ:"):
                    growth_method = line.replace("🎯 СПОСОБ ВЫРАЩИВАНИЯ:", "").strip()
                    break
        
        # Создаем выращиваемое растение с календарём
        db = await get_db()
        
        try:
            growing_id = await db.create_growing_plant(
                user_id=user_id,
                plant_name=plant_name,
                growth_method=growth_method,
                growing_plan=growing_plan,
                task_calendar=task_calendar,  # Передаём календарь!
                photo_file_id=photo_file_id
            )
            print(f"✅ Создано растение #{growing_id} с календарём задач")
        except Exception as e:
            print(f"ERROR creating growing plant: {e}")
            raise
        
        # Создаем первое напоминание для первой задачи
        if task_calendar:
            try:
                await schedule_next_task_reminder(growing_id, user_id, task_calendar, -1)
            except Exception as e:
                print(f"ERROR creating first reminder: {e}")
        
        success_text = f"🎉 <b>Выращивание {plant_name} началось!</b>\n\n"
        success_text += f"📋 План выращивания создан с календарём ключевых задач\n"
        success_text += f"⏰ Вы получите напоминания только в важные дни\n"
        success_text += f"💧 Полив, подкормка, пересадка - всё по расписанию!\n\n"
        success_text += f"🔔 Удачного выращивания!"
        
        keyboard = [
            [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
            [InlineKeyboardButton(text="📝 Дневник роста", callback_data=f"view_diary_{growing_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ]
        
        await message_obj.answer(
            success_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка финализации выращивания: {e}")
        import traceback
        traceback.print_exc()
        
        await message_obj.answer(
            "❌ Ошибка создания плана выращивания.\n"
            "Попробуйте еще раз позже.",
            reply_markup=simple_back_menu()
        )
        
        await state.clear()

@dp.callback_query(F.data == "add_growing_photo")
async def add_growing_photo_callback(callback: types.CallbackQuery, state: FSMContext):
    """Запрос фото для начала выращивания"""
    await callback.message.answer(
        "📸 <b>Сфотографируйте ваши семена/черенок/луковицы</b>\n\n"
        "💡 <b>Советы для хорошего фото:</b>\n"
        "• Используйте хорошее освещение\n"
        "• Покажите все материалы для посадки\n"
        "• Можете добавить описание в подпись к фото\n\n"
        "📷 Пришлите фото сейчас:",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.waiting_growing_photo)
    await callback.answer()

@dp.callback_query(F.data == "start_growing_no_photo")
async def start_growing_no_photo_callback(callback: types.CallbackQuery, state: FSMContext):
    """Начать выращивание без фото"""
    user_id = callback.from_user.id
    
    try:
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        
        if not plant_name or not growing_plan:
            await callback.message.answer(
                "❌ <b>Данные плана потеряны</b>\n\n"
                "Попробуйте создать план заново.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌿 Создать план", callback_data="grow_from_scratch")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
                ])
            )
            await callback.answer()
            return
        
        await finalize_growing_setup(callback.message, state, None, user_id)
        
    except Exception as e:
        print(f"Ошибка start_growing_no_photo: {e}")
        await callback.message.answer(
            "❌ Техническая ошибка при создании плана",
            reply_markup=main_menu()
        )
        await state.clear()
    
    await callback.answer()

@dp.message(StateFilter(PlantStates.waiting_growing_photo), F.photo)
async def handle_growing_photo(message: types.Message, state: FSMContext):
    """Обработка фото для выращивания"""
    try:
        photo = message.photo[-1]
        user_id = message.from_user.id
        
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        
        if not plant_name or not growing_plan:
            await message.reply(
                "❌ <b>Данные плана потеряны</b>\n\n"
                "Попробуйте создать план заново.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌿 Создать план", callback_data="grow_from_scratch")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
                ])
            )
            return
        
        await finalize_growing_setup(message, state, photo.file_id, user_id)
        
    except Exception as e:
        print(f"Ошибка обработки фото выращивания: {e}")
        import traceback
        traceback.print_exc()
        
        await message.reply(
            "❌ Ошибка обработки фото. Попробуйте еще раз.",
            reply_markup=main_menu()
        )
        await state.clear()

@dp.callback_query(F.data == "ask_about_plan")
async def ask_about_plan_callback(callback: types.CallbackQuery, state: FSMContext):
    """Вопрос о плане выращивания"""
    data = await state.get_data()
    plant_name = data.get('plant_name', 'растение')
    
    await callback.message.answer(
        f"❓ <b>Вопрос о выращивании {plant_name}</b>\n\n"
        f"💡 <b>Популярные вопросы:</b>\n"
        f"• Сколько времени займет выращивание?\n"
        f"• Какие материалы нужны для посадки?\n"
        f"• Как понять, что растение здоровое?\n"
        f"• Что делать, если что-то пошло не так?\n"
        f"• Когда ожидать первые всходы?\n"
        f"• Какой горшок выбрать?\n\n"
        f"✍️ Напишите ваш вопрос о выращивании:",
        parse_mode="HTML"
    )
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()

# === УПРАВЛЕНИЕ ЭТАПАМИ ВЫРАЩИВАНИЯ ===

@dp.callback_query(F.data.startswith("advance_stage_"))
async def advance_stage_callback(callback: types.CallbackQuery):
    """Переход к следующему этапу выращивания"""
    try:
        growing_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        growing_plant = await db.get_growing_plant_by_id(growing_id, user_id)
        
        if not growing_plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = growing_plant['plant_name']
        current_stage = growing_plant['current_stage']
        
        result = await db.advance_growth_stage(growing_id)
        
        if result == "completed":
            await callback.message.answer(
                f"🎉 <b>Поздравляем! Выращивание завершено!</b>\n\n"
                f"🌱 <b>{plant_name}</b> успешно выращен до взрослого состояния!\n\n"
                f"🏆 Теперь можете:\n"
                f"• Пересадить в постоянный горшок\n"
                f"• Добавить в основную коллекцию\n"
                f"• Начать выращивание следующего растения\n\n"
                f"📝 Весь процесс сохранен в дневнике роста!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Дневник роста", callback_data=f"view_diary_{growing_id}")],
                    [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
                ])
            )
        elif result:
            new_stage = current_stage + 1
            
            updated_plant = await db.get_growing_plant_by_id(growing_id, user_id)
            stage_name = updated_plant.get('current_stage_name', f'Этап {new_stage}')
            
            await callback.message.answer(
                f"✅ <b>Этап завершен!</b>\n\n"
                f"🌱 <b>{plant_name}</b>\n"
                f"🎯 Переход: Этап {current_stage} → Этап {new_stage}\n"
                f"📋 <b>Текущий этап:</b> {stage_name}\n\n"
                f"🔔 Я буду напоминать о действиях на новом этапе\n"
                f"📸 Не забывайте добавлять фото прогресса!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📸 Добавить фото", callback_data=f"add_diary_photo_{growing_id}")],
                    [InlineKeyboardButton(text="📝 Записать заметку", callback_data=f"add_diary_note_{growing_id}")],
                    [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
                ])
            )
        else:
            await callback.message.answer("❌ Ошибка перехода к следующему этапу")
        
    except Exception as e:
        print(f"Ошибка перехода этапа: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("add_diary_photo_"))
async def add_diary_photo_callback(callback: types.CallbackQuery, state: FSMContext):
    """Добавление фото в дневник роста"""
    try:
        growing_id = int(callback.data.split("_")[-1])
        
        await state.update_data(
            adding_diary_photo=True,
            diary_growing_id=growing_id
        )
        
        await callback.message.answer(
            "📸 <b>Добавляем фото в дневник роста</b>\n\n"
            "📷 Сфотографируйте текущее состояние растения:\n"
            "• Покажите прогресс роста\n"
            "• Сфокусируйтесь на изменениях\n"
            "• Используйте хорошее освещение\n\n"
            "💬 Можете добавить описание в подписи к фото",
            parse_mode="HTML"
        )
        
        await state.set_state(PlantStates.adding_diary_entry)
        
    except Exception as e:
        print(f"Ошибка запроса фото дневника: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("add_diary_note_"))
async def add_diary_note_callback(callback: types.CallbackQuery, state: FSMContext):
    """Добавление заметки в дневник роста"""
    try:
        growing_id = int(callback.data.split("_")[-1])
        
        await state.update_data(
            adding_diary_note=True,
            diary_growing_id=growing_id
        )
        
        await callback.message.answer(
            "📝 <b>Добавляем заметку в дневник роста</b>\n\n"
            "✍️ Напишите что наблюдаете:\n"
            "• Изменения в растении\n"
            "• Выполненные действия\n"
            "• Проблемы или вопросы\n"
            "• Любые важные моменты\n\n"
            "💭 Просто напишите текст заметки:",
            parse_mode="HTML"
        )
        
        await state.set_state(PlantStates.adding_diary_entry)
        
    except Exception as e:
        print(f"Ошибка запроса заметки дневника: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.message(StateFilter(PlantStates.adding_diary_entry))
async def handle_diary_entry(message: types.Message, state: FSMContext):
    """Обработка добавления записи в дневник"""
    try:
        data = await state.get_data()
        growing_id = data.get('diary_growing_id')
        is_photo = data.get('adding_diary_photo', False)
        is_note = data.get('adding_diary_note', False)
        
        if not growing_id:
            await message.reply("❌ Ошибка: данные не найдены")
            await state.clear()
            return
        
        db = await get_db()
        user_id = message.from_user.id
        
        if is_photo and message.photo:
            photo = message.photo[-1]
            description = message.caption if message.caption else "Фото прогресса роста"
            
            await db.add_diary_entry(
                growing_id=growing_id,
                user_id=user_id,
                entry_type='photo',
                description=description,
                photo_file_id=photo.file_id
            )
            
            await message.reply(
                "✅ <b>Фото добавлено в дневник роста!</b>\n\n"
                "📸 Фото сохранено с отметкой времени\n"
                "📝 Описание записано\n\n"
                "Продолжайте следить за ростом растения!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Дневник роста", callback_data=f"view_diary_{growing_id}")],
                    [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
                ])
            )
            
        elif is_note and message.text:
            await db.add_diary_entry(
                growing_id=growing_id,
                user_id=user_id,
                entry_type='note',
                description=message.text
            )
            
            await message.reply(
                "✅ <b>Заметка добавлена в дневник!</b>\n\n"
                "📝 Запись сохранена с текущим временем\n"
                "📊 Ваши наблюдения помогут отслеживать прогресс\n\n"
                "Отличная работа по ведению дневника роста!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Дневник роста", callback_data=f"view_diary_{growing_id}")],
                    [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
                ])
            )
            
        else:
            if is_photo:
                await message.reply(
                    "📸 Ожидается фотография.\n"
                    "Пришлите фото растения или отмените операцию."
                )
                return
            elif is_note:
                await message.reply(
                    "📝 Ожидается текстовая заметка.\n"
                    "Напишите что наблюдаете или отмените операцию."
                )
                return
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка добавления записи в дневник: {e}")
        await message.reply("❌ Ошибка сохранения записи")
        await state.clear()

@dp.callback_query(F.data.startswith("view_diary_"))
async def view_diary_callback(callback: types.CallbackQuery):
    """Просмотр дневника роста"""
    try:
        growing_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        growing_plant = await db.get_growing_plant_by_id(growing_id, user_id)
        diary_entries = await db.get_growth_diary(growing_id, limit=10)
        
        if not growing_plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = growing_plant['plant_name']
        current_stage = growing_plant['current_stage']
        total_stages = growing_plant['total_stages']
        started_date = growing_plant['started_date']
        
        days_growing = (get_moscow_now().date() - started_date.date()).days
        
        text = f"📝 <b>Дневник роста: {plant_name}</b>\n\n"
        text += f"📊 <b>Прогресс:</b> Этап {current_stage}/{total_stages}\n"
        text += f"📅 <b>Выращивается:</b> {days_growing} дней\n"
        text += f"🌱 <b>Дата начала:</b> {started_date.strftime('%d.%m.%Y')}\n\n"
        
        if diary_entries:
            text += f"📖 <b>Последние записи:</b>\n\n"
            for entry in diary_entries[:5]:
                entry_date = entry['entry_date'].strftime('%d.%m %H:%M')
                entry_type_icon = "📸" if entry['entry_type'] == 'photo' else "📝" if entry['entry_type'] == 'note' else "✅"
                
                text += f"{entry_type_icon} <b>{entry_date}</b>\n"
                description = entry['description'][:50] + "..." if len(entry['description']) > 50 else entry['description']
                text += f"   {description}\n\n"
        else:
            text += "📝 Записей пока нет\n\n"
        
        keyboard = [
            [InlineKeyboardButton(text="📸 Добавить фото", callback_data=f"add_diary_photo_{growing_id}")],
            [InlineKeyboardButton(text="📝 Добавить заметку", callback_data=f"add_diary_note_{growing_id}")],
            [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
        ]
        
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка просмотра дневника: {e}")
        await callback.answer("❌ Ошибка загрузки дневника")
    
    await callback.answer()

# === СОХРАНЕНИЕ РАСТЕНИЙ ===

@dp.callback_query(F.data == "save_plant")
async def save_plant_callback(callback: types.CallbackQuery):
    """Сохранение растения с персональными рекомендациями по поливу"""
    user_id = callback.from_user.id
    
    if user_id in temp_analyses:
        try:
            analysis_data = temp_analyses[user_id]
            raw_analysis = analysis_data.get("analysis", "")
            
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
            
            if watering_info["needs_adjustment"] and watering_info["personal_recommendations"]:
                async with db.pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE plants SET notes = $1 WHERE id = $2
                    """, f"Персональные рекомендации по поливу: {watering_info['personal_recommendations']}", plant_id)
            
            await create_plant_reminder(plant_id, user_id, personal_interval)
            
            del temp_analyses[user_id]
            
            plant_name = analysis_data.get("plant_name", "растение")
            
            success_text = f"✅ <b>Растение успешно добавлено в коллекцию!</b>\n\n"
            success_text += f"🌱 <b>{plant_name}</b> теперь в вашем цифровом саду\n"
            
            if watering_info["current_state"]:
                if watering_info["needs_adjustment"]:
                    success_text += f"⚠️ Текущее состояние: {watering_info['current_state']}\n"
                else:
                    success_text += f"✅ Состояние полива: {watering_info['current_state']}\n"
            
            success_text += f"⏰ Персональный интервал полива: каждые {personal_interval} дней\n\n"
            
            if watering_info["personal_recommendations"]:
                success_text += f"💡 Ваши персональные рекомендации сохранены!\n\n"
            
            if watering_info["needs_adjustment"]:
                success_text += f"🔍 <b>Внимание:</b> Растение нуждается в корректировке полива\n"
                success_text += f"💧 Первое напоминание придет через {personal_interval} дней с учетом текущего состояния"
            else:
                success_text += f"💧 Первое напоминание о поливе придет через {personal_interval} дней"
            
            await callback.message.answer(
                success_text,
                parse_mode="HTML"
            )
            
        except Exception as e:
            print(f"Ошибка сохранения растения: {e}")
            await callback.message.answer("❌ Ошибка сохранения. Попробуйте позже.")
    else:
        await callback.message.answer("❌ Нет данных для сохранения. Сначала проанализируйте растение.")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("water_plant_"))
async def water_single_plant_callback(callback: types.CallbackQuery):
    """Полив отдельного растения с обновлением напоминания"""
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
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
            ])
        )
        
    except Exception as e:
        print(f"Ошибка полива растения: {e}")
        await callback.answer("❌ Ошибка полива")
    
    await callback.answer()

# === ОБРАБОТЧИКИ РЕДАКТИРОВАНИЯ РАСТЕНИЙ ===

@dp.callback_query(F.data.startswith("edit_plant_"))
async def edit_plant_callback(callback: types.CallbackQuery):
    """Меню редактирования растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        
        if str(plant_id).startswith("growing_"):
            actual_id = int(str(plant_id).replace("growing_", ""))
            growing_plant = await db.get_growing_plant_by_id(actual_id, user_id)
            
            if not growing_plant:
                await callback.answer("❌ Растение не найдено")
                return
            
            plant_name = growing_plant['plant_name']
            current_stage = growing_plant['current_stage']
            total_stages = growing_plant['total_stages']
            status = growing_plant['status']
            
            keyboard = [
                [InlineKeyboardButton(text="📝 Дневник роста", callback_data=f"view_diary_{actual_id}")],
                [InlineKeyboardButton(text="📸 Добавить фото", callback_data=f"add_diary_photo_{actual_id}")],
                [InlineKeyboardButton(text="✅ Следующий этап", callback_data=f"advance_stage_{actual_id}")],
                [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
            ]
            
            stage_text = f"Этап {current_stage}/{total_stages}"
            if status == "completed":
                stage_text = "✅ Завершено"
            
            await callback.message.answer(
                f"🌱 <b>Управление выращиванием</b>\n\n"
                f"🌿 <b>{plant_name}</b>\n"
                f"📊 <b>Прогресс:</b> {stage_text}\n\n"
                f"Выберите действие:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        else:
            plant = await db.get_plant_by_id(plant_id, user_id)
            
            if not plant:
                await callback.answer("❌ Растение не найдено")
                return
            
            plant_name = plant['display_name']
            watering_interval = plant.get('watering_interval', 5)
            
            moscow_now = get_moscow_now()
            if plant["last_watered"]:
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
                [InlineKeyboardButton(text="💧 Полить сейчас", callback_data=f"water_plant_{plant_id}")],
                [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"rename_plant_{plant_id}")],
                [InlineKeyboardButton(text="⏰ Настроить интервал", callback_data=f"set_interval_{plant_id}")],
                [InlineKeyboardButton(text="🗑️ Удалить растение", callback_data=f"delete_plant_{plant_id}")],
                [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
            ]
            
            await callback.message.answer(
                f"⚙️ <b>Настройки растения</b>\n\n"
                f"🌱 <b>{plant_name}</b>\n"
                f"{water_status}\n"
                f"⏰ Интервал полива: {watering_interval} дней\n\n"
                f"Выберите действие:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        
    except Exception as e:
        print(f"Ошибка меню редактирования: {e}")
        await callback.answer("❌ Ошибка загрузки")
    
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
            f"✏️ <b>Изменение названия растения</b>\n\n"
            f"🌱 <b>Текущее название:</b> {current_name}\n\n"
            f"✍️ <b>Напишите новое название в чат ниже:</b>\n",
            parse_mode="HTML"
        )
        
    except Exception as e:
        print(f"Ошибка переименования: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_plant_"))
async def delete_plant_callback(callback: types.CallbackQuery):
    """Удаление растения с подтверждением"""
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
            f"🌱 <b>{plant_name}</b>\n\n"
            f"⚠️ <b>Внимание!</b> Это действие нельзя отменить.\n"
            f"Будут удалены:\n"
            f"• Растение из коллекции\n"
            f"• История полива\n"
            f"• Все напоминания\n\n"
            f"❓ Вы уверены что хотите удалить это растение?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка запроса удаления: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_callback(callback: types.CallbackQuery):
    """Подтверждение удаления растения"""
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
                f"❌ <b>{plant_name}</b> удалено из коллекции\n"
                f"🔄 Все связанные напоминания отменены\n\n"
                f"💡 Вы можете добавить новые растения в любое время",
                parse_mode="HTML",
                reply_markup=simple_back_menu()
            )
        else:
            await callback.answer("❌ Растение не найдено")
        
    except Exception as e:
        print(f"Ошибка удаления растения: {e}")
        await callback.answer("❌ Ошибка удаления")
    
    await callback.answer()

# === ОБРАБОТЧИКИ ОБРАТНОЙ СВЯЗИ ===

@dp.callback_query(F.data == "feedback")
async def feedback_callback(callback: types.CallbackQuery, state: FSMContext):
    """Меню обратной связи"""
    keyboard = [
        [InlineKeyboardButton(text="🐛 Сообщить о баге", callback_data="feedback_bug")],
        [InlineKeyboardButton(text="❌ Неточный анализ", callback_data="feedback_analysis_error")],
        [InlineKeyboardButton(text="💡 Предложить улучшение", callback_data="feedback_suggestion")],
        [InlineKeyboardButton(text="⭐ Общий отзыв", callback_data="feedback_review")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    
    await callback.message.answer(
        "📝 <b>Обратная связь</b>\n\n"
        "Ваше мнение помогает улучшать бота!\n"
        "Выберите тип сообщения:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("feedback_"))
async def feedback_type_callback(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа обратной связи"""
    feedback_type = callback.data.replace("feedback_", "")
    
    type_messages = {
        "bug": {
            "title": "🐛 Сообщить о баге",
            "description": "Опишите техническую проблему:\n• Что произошло?\n• Какие действия привели к ошибке?\n• Какой результат ожидали?\n\n✍️ Напишите ваше сообщение в чат и приложите фото, если есть:"
        },
        "analysis_error": {
            "title": "❌ Неточный анализ",
            "description": "Расскажите о неправильном определении растения:\n• Какое растение на самом деле?\n• Что бот определил неверно?\n• Можете приложить фото для примера\n\n✍️ Напишите ваше сообщение в чат и приложите фото, если есть:"
        },
        "suggestion": {
            "title": "💡 Предложить улучшение",
            "description": "Поделитесь идеей по улучшению бота:\n• Какую функцию хотели бы добавить?\n• Что можно сделать лучше?\n• Как это поможет пользователям?\n\n✍️ Напишите ваше сообщение в чат и приложите фото, если есть:"
        },
        "review": {
            "title": "⭐ Общий отзыв",
            "description": "Поделитесь впечатлениями от использования:\n• Что нравится?\n• Что не нравится?\n• Общая оценка работы бота\n\n✍️ Напишите ваше сообщение в чат и приложите фото, если есть:"
        }
    }
    
    type_info = type_messages.get(feedback_type, type_messages["review"])
    
    await state.update_data(feedback_type=feedback_type)
    await state.set_state(FeedbackStates.writing_message)
    
    await callback.message.answer(
        f"{type_info['title']}\n\n"
        f"{type_info['description']}",
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
            await message.reply(
                "📝 <b>Пожалуйста, напишите сообщение или приложите фото</b>\n\n"
                "Ваш отзыв поможет улучшить бота!",
                parse_mode="HTML"
            )
            return
        
        if feedback_text:
            if len(feedback_text) < 5:
                await message.reply(
                    "📝 <b>Сообщение слишком короткое</b>\n\n"
                    "Пожалуйста, опишите подробнее (минимум 5 символов):",
                    parse_mode="HTML"
                )
                return
            
            if len(feedback_text) > 2000:
                await message.reply(
                    "📝 <b>Сообщение слишком длинное</b>\n\n"
                    "Максимум 2000 символов. Сократите текст:",
                    parse_mode="HTML"
                )
                return
        
        if not feedback_text and feedback_photo:
            feedback_text = "Фото без комментария"
        
        data = await state.get_data()
        feedback_type = data.get('feedback_type', 'review')
        
        await send_feedback(message, state, feedback_text, feedback_photo)
        
    except Exception as e:
        print(f"Ошибка обработки сообщения обратной связи: {e}")
        await message.reply("❌ Ошибка обработки сообщения. Попробуйте еще раз.")
        await state.clear()

@dp.callback_query(F.data == "feedback_cancel")
async def feedback_cancel_callback(callback: types.CallbackQuery, state: FSMContext):
    """Отмена обратной связи"""
    await state.clear()
    
    await callback.message.answer(
        "❌ <b>Обратная связь отменена</b>\n\n"
        "Вы можете отправить ее в любое время через главное меню.",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    await callback.answer()

async def send_feedback(message_obj, state: FSMContext, feedback_message: str, feedback_photo: str = None):
    """Отправка обратной связи в БД и уведомление"""
    try:
        data = await state.get_data()
        feedback_type = data.get('feedback_type', 'review')
        
        user_id = message_obj.from_user.id
        username = message_obj.from_user.username or message_obj.from_user.first_name or f"user_{user_id}"
        
        context_data = None
        if user_id in temp_analyses:
            context_data = json.dumps({
                "last_analysis": temp_analyses[user_id].get("plant_name", "Unknown"),
                "confidence": temp_analyses[user_id].get("confidence", 0),
                "source": temp_analyses[user_id].get("source", "unknown")
            })
        
        db = await get_db()
        feedback_id = await db.save_feedback(
            user_id=user_id,
            username=username,
            feedback_type=feedback_type,
            message=feedback_message,
            photo_file_id=feedback_photo,
            context_data=context_data
        )
        
        type_icons = {
            "bug": "🐛",
            "analysis_error": "❌", 
            "suggestion": "💡",
            "review": "⭐"
        }
        
        icon = type_icons.get(feedback_type, "📝")
        print(f"\n{icon} НОВАЯ ОБРАТНАЯ СВЯЗЬ #{feedback_id}")
        print(f"👤 Пользователь: @{username} (ID: {user_id})")
        print(f"📝 Тип: {feedback_type}")
        print(f"💬 Сообщение: {feedback_message[:100]}{'...' if len(feedback_message) > 100 else ''}")
        if feedback_photo:
            print(f"📸 Фото: {feedback_photo}")
        if context_data:
            print(f"🔗 Контекст: {context_data}")
        print("=" * 50)
        
        await message_obj.answer(
            f"✅ <b>Спасибо за ваш отзыв!</b>\n\n"
            f"Ваше сообщение принято и поможет улучшить бота.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка отправки обратной связи: {e}")
        await message_obj.answer(
            "❌ Ошибка отправки обратной связи.\n"
            "Попробуйте позже или напишите разработчику напрямую."
        )

def simple_back_menu():
    """Простое меню с кнопкой "Назад" """
    keyboard = [
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
            InlineKeyboardButton(text="🔔 Настройки", callback_data="notification_settings")
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="📝 Обратная связь", callback_data="feedback")
        ],
        [
            InlineKeyboardButton(text="ℹ️ Справка", callback_data="help")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def after_analysis():
    keyboard = [
        [InlineKeyboardButton(text="✅ Добавить в коллекцию", callback_data="save_plant")],
        [InlineKeyboardButton(text="❓ Вопрос о растении", callback_data="ask_about")],
        [InlineKeyboardButton(text="🔄 Повторный анализ", callback_data="reanalyze")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def extract_personal_watering_info(analysis_text: str) -> dict:
    """Извлекает персональную информацию о поливе из анализа"""
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

def format_plant_analysis(raw_text: str, confidence: float = None) -> str:
    """Форматирование анализа растения"""
    
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
    
    if confidence_level >= 80:
        formatted += "\n\n🏆 <i>Высокая точность распознавания</i>"
    elif confidence_level >= 60:
        formatted += "\n\n👍 <i>Хорошее распознавание</i>"
    else:
        formatted += "\n\n🤔 <i>Требуется дополнительная идентификация</i>"
    
    formatted += "\n💾 <i>Сохраните для персональных напоминаний!</i>"
    
    return formatted

async def optimize_image_for_analysis(image_data: bytes, high_quality: bool = True) -> bytes:
    """Оптимизация изображения для анализа"""
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
        print(f"Ошибка оптимизации изображения: {e}")
        return image_data

async def analyze_with_openai_advanced(image_data: bytes, user_question: str = None) -> dict:
    """Продвинутый анализ через OpenAI GPT-4 Vision"""
    if not openai_client:
        return {"success": False, "error": "OpenAI API недоступен"}
    
    try:
        optimized_image = await optimize_image_for_analysis(image_data, high_quality=True)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        prompt = PLANT_IDENTIFICATION_PROMPT
        
        if user_question:
            prompt += f"\n\nДополнительно ответьте на вопрос: {user_question}"
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "Вы - ведущий эксперт-ботаник с 30-летним опытом идентификации растений. Анализируйте только видимые элементы, честно указывайте если что-то не видно на фото."
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
            max_tokens=1200,
            temperature=0.2
        )
        
        raw_analysis = response.choices[0].message.content
        
        if len(raw_analysis) < 100 or "не могу" in raw_analysis.lower() or "sorry" in raw_analysis.lower():
            raise Exception("Некачественный ответ от OpenAI")
        
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
        
        formatted_analysis = format_plant_analysis(raw_analysis, confidence)
        
        print(f"✅ Анализ завершен. Уверенность: {confidence}%")
        
        return {
            "success": True,
            "analysis": formatted_analysis,
            "raw_analysis": raw_analysis,
            "plant_name": plant_name,
            "confidence": confidence,
            "source": "openai_advanced"
        }
        
    except Exception as e:
        print(f"❌ OpenAI Advanced API error: {e}")
        return {"success": False, "error": str(e)}

async def analyze_plant_image(image_data: bytes, user_question: str = None, retry_count: int = 0) -> dict:
    """Интеллектуальный анализ изображения растения"""
    
    print("🔍 Попытка анализа через OpenAI GPT-4 Vision...")
    openai_result = await analyze_with_openai_advanced(image_data, user_question)
    
    if openai_result["success"] and openai_result.get("confidence", 0) >= 50:
        print(f"✅ OpenAI успешно распознал растение с {openai_result.get('confidence')}% уверенностью")
        return openai_result
    
    if retry_count == 0:
        print("🔄 Повторная попытка анализа...")
        return await analyze_plant_image(image_data, user_question, retry_count + 1)
    
    if openai_result["success"]:
        print(f"⚠️ Используем результат с низкой уверенностью: {openai_result.get('confidence')}%")
        openai_result["needs_retry"] = True
        return openai_result
    
    print("⚠️ Анализ не дал результата, используем fallback")
    
    fallback_text = """
РАСТЕНИЕ: Комнатное растение (требуется дополнительная идентификация)
УВЕРЕННОСТЬ: 20%
ПРИЗНАКИ: Недостаточно данных для точной идентификации
СЕМЕЙСТВО: Не определено
РОДИНА: Не определено

СОСТОЯНИЕ: Требуется визуальный осмотр листьев, стебля и корневой системы
ПОЛИВ_АНАЛИЗ: Почва не видна - невозможно оценить полив
ПОЛИВ_РЕКОМЕНДАЦИИ: Проверяйте влажность почвы пальцем - поливайте когда верхний слой подсох на 2-3 см
ПОЛИВ_ИНТЕРВАЛ: 5
СВЕТ: Большинство комнатных растений предпочитают яркий рассеянный свет
ТЕМПЕРАТУРА: 18-24°C - стандартный диапазон для комнатных растений
ВЛАЖНОСТЬ: 40-60% влажности воздуха
ПОДКОРМКА: В весенне-летний период раз в 2-4 недели
ПЕРЕСАДКА: Молодые растения ежегодно, взрослые - по мере необходимости

ПРОБЛЕМЫ: Наблюдайте за изменениями листьев - они покажут проблемы с уходом
СОВЕТ: Для точной идентификации сделайте фото при хорошем освещении, показав листья крупным планом
    """.strip()
    
    formatted_analysis = format_plant_analysis(fallback_text, 20)
    
    return {
        "success": True,
        "analysis": formatted_analysis,
        "raw_analysis": fallback_text,
        "plant_name": "Неопознанное растение",
        "confidence": 20,
        "source": "fallback_improved",
        "needs_retry": True
    }

# === ОБРАБОТЧИКИ КОМАНД ===

@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Команда /start с онбордингом для новых пользователей"""
    user_id = message.from_user.id
    
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
                
                await start_onboarding(message)
                return
            else:
                await show_returning_user_welcome(message)
                return
                
    except Exception as e:
        print(f"Ошибка команды /start: {e}")
        await show_returning_user_welcome(message)

async def start_onboarding(message: types.Message):
    """Новый онбординг - сразу к делу без дублирования"""
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
    """Простое приветствие для возвращающихся пользователей"""
    first_name = message.from_user.first_name or "друг"
    
    await message.answer(
        f"🌱 С возвращением, {first_name}!\n\n"
        "Что будем делать с растениями сегодня?",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "onboarding_demo")
async def onboarding_demo_callback(callback: types.CallbackQuery):
    """Показ демо анализа"""
    
    demo_text = (
        "🔍 <b>Смотрите! Вот как я анализирую растения:</b>\n\n"
        "🌿 <b>Фикус Бенджамина</b> (Ficus benjamina)\n"
        "🎯 <b>Уверенность:</b> 95%\n\n"
        "🔍 <b>Что видно на фото:</b>\n"
        "✅ Листья: здоровые, зеленые\n"
        "❌ Почва: не видна в кадре\n\n"
        "🍃 <b>Состояние листьев:</b> Здоровые, правильного цвета\n"
        "❓ <b>Полив:</b> Невозможно оценить - почва не видна\n\n"
        "📸 <b>Для точной диагностики сфотографируйте:</b>\n"
        "• Почву в горшке (для оценки полива)\n"
        "• Обратную сторону листьев\n\n"
        "💡 <b>Честный анализ - только то, что видно!</b>"
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
        "🎯 <b>Отлично! С чего начнем ваше садовое приключение?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(F.data == "onboarding_try_analyze")
async def onboarding_try_analyze_callback(callback: types.CallbackQuery):
    """Попробовать анализ из онбординга"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "📸 <b>Отлично! Пришлите фото вашего растения</b>\n\n"
        "💡 <b>Советы для лучшего результата:</b>\n"
        "• Фотографируйте при дневном свете\n"
        "• Покажите листья и общий вид растения\n"
        "• По возможности включите почву в горшке\n"
        "• Избегайте размытых и тёмных снимков\n\n"
        "📱 Жду ваше фото!",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "onboarding_try_grow")
async def onboarding_try_grow_callback(callback: types.CallbackQuery, state: FSMContext):
    """Попробовать выращивание из онбординга"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "🌿 <b>Отлично! Выращиваем растение с нуля!</b>\n\n"
        "🌱 <b>Напишите, что хотите вырастить:</b>\n\n"
        "💡 <b>Примеры:</b> Базилик, Герань, Тюльпаны, Фикус, Помидоры, Укроп, Фиалка\n\n"
        "✍️ Просто напишите название растения!",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.choosing_plant_to_grow)
    await callback.answer()

@dp.callback_query(F.data == "onboarding_try_question")
async def onboarding_try_question_callback(callback: types.CallbackQuery, state: FSMContext):
    """Попробовать вопрос из онбординга"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "❓ <b>Задайте ваш вопрос о растениях</b>\n\n"
        "💡 <b>Я могу помочь с:</b>\n"
        "• Проблемами с листьями (желтеют, сохнут, опадают)\n"
        "• Режимом полива и подкормки\n"
        "• Пересадкой и размножением\n"
        "• Болезнями и вредителями\n"
        "• Выбором места для растения\n\n"
        "✍️ Напишите ваш вопрос:",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()

async def mark_onboarding_completed(user_id: int):
    """Отметить онбординг как завершенный"""
    try:
        db = await get_db()
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET onboarding_completed = TRUE WHERE user_id = $1",
                user_id
            )
    except Exception as e:
        print(f"Ошибка отметки онбординга: {e}")

@dp.message(Command("grow"))
async def grow_command(message: types.Message, state: FSMContext):
    """Команда /grow - выращивание с нуля"""
    await message.answer(
        "🌿 <b>Выращиваем растение с нуля!</b>\n\n"
        "Я стану вашим персональным наставником и помогу "
        "вырастить растение от семечка до взрослого!\n\n"
        "🌱 <b>Напишите, что хотите вырастить:</b>\n\n"
        "💡 <b>Примеры:</b> Базилик, Герань, Тюльпаны, Фикус, Помидоры\n\n"
        "✍️ Просто напишите название растения!",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.choosing_plant_to_grow)

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Команда /help"""
    help_text = """
🌱 <b>Как пользоваться ботом:</b>

🌱 <b>Добавление растения:</b>
• Нажми "🌱 Добавить растение"
• Пришли фото растения
• Получи персональные рекомендации

🌿 <b>Выращивание с нуля:</b>
• Нажми "🌿 Вырастить с нуля"
• Напиши название растения
• Получи персональный план с календарём задач
• Напоминания только в ключевые дни

📸 <b>Анализ растения:</b>
• Пришли фото растения
• Получи полный анализ

⏰ <b>Умные напоминания:</b>
• Ежедневная проверка в 9:00 МСК
• Персональный график
• Только важные дни для выращивания

<b>Быстрые команды:</b>
/start - Главное меню
/grow - Вырастить с нуля
/add - Добавить растение
/analyze - Анализ растения
/question - Задать вопрос
/plants - Мои растения
/stats - Статистика
/feedback - Обратная связь
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
async def feedback_command(message: types.Message, state: FSMContext):
    """Команда /feedback - обратная связь"""
    keyboard = [
        [InlineKeyboardButton(text="🐛 Сообщить о баге", callback_data="feedback_bug")],
        [InlineKeyboardButton(text="❌ Неточный анализ", callback_data="feedback_analysis_error")],
        [InlineKeyboardButton(text="💡 Предложить улучшение", callback_data="feedback_suggestion")],
        [InlineKeyboardButton(text="⭐ Общий отзыв", callback_data="feedback_review")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    
    await message.answer(
        "📝 <b>Обратная связь</b>\n\n"
        "Ваше мнение помогает улучшать бота!\n"
        "Выберите тип сообщения:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

@dp.message(Command("add"))
async def add_command(message: types.Message):
    """Команда /add - добавить растение"""
    await message.answer(
        "🌱 <b>Добавьте растение в коллекцию</b>\n\n"
        "📸 <b>Пришлите фото вашего растения</b>",
        parse_mode="HTML"
    )

@dp.message(Command("analyze"))
async def analyze_command(message: types.Message):
    """Команда /analyze - анализ растения"""
    await message.answer(
        "📸 <b>Отправьте фото растения для анализа</b>\n\n"
        "💡 <b>Советы:</b> дневной свет, листья крупным планом",
        parse_mode="HTML"
    )

@dp.message(Command("question"))
async def question_command(message: types.Message, state: FSMContext):
    """Команда /question - задать вопрос"""
    await message.answer(
        "❓ <b>Задайте ваш вопрос о растениях</b>",
        parse_mode="HTML"
    )
    await state.set_state(PlantStates.waiting_question)

@dp.message(Command("plants"))
async def plants_command(message: types.Message):
    """Команда /plants - мои растения"""
    await my_plants_callback(types.CallbackQuery(
        id="fake",
        from_user=message.from_user,
        chat_instance="fake",
        message=message,
        data="my_plants"
    ))

@dp.message(Command("stats"))
async def stats_command(message: types.Message):
    """Команда /stats - статистика"""
    await stats_callback(types.CallbackQuery(
        id="fake",
        from_user=message.from_user,
        chat_instance="fake",
        message=message,
        data="stats"
    ))

# === ОБРАБОТКА СОСТОЯНИЙ ===

@dp.message(StateFilter(PlantStates.editing_plant_name))
async def handle_plant_rename(message: types.Message, state: FSMContext):
    """Обработка нового названия растения"""
    try:
        new_name = message.text.strip()
        
        if len(new_name) < 2:
            await message.reply("❌ Название слишком короткое")
            return
        
        if len(new_name) > 50:
            await message.reply("❌ Название слишком длинное")
            return
        
        data = await state.get_data()
        plant_id = data.get('editing_plant_id')
        
        if not plant_id:
            await message.reply("❌ Ошибка: ID растения не найден")
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
        print(f"Ошибка переименования: {e}")
        await message.reply("❌ Ошибка сохранения")
        await state.clear()

@dp.message(StateFilter(PlantStates.waiting_question))
async def handle_question(message: types.Message, state: FSMContext):
    """Обработка текстовых вопросов"""
    try:
        processing_msg = await message.reply("🤔 <b>Консультируюсь...</b>", parse_mode="HTML")
        
        user_id = message.from_user.id
        user_context = ""
        
        if user_id in temp_analyses:
            plant_info = temp_analyses[user_id]
            plant_name = plant_info.get("plant_name", "растение")
            user_context = f"\n\nКонтекст: Пользователь недавно анализировал {plant_name}."
        
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
                print(f"OpenAI error: {e}")
        
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
        print(f"Ошибка ответа на вопрос: {e}")
        await message.reply("❌ Ошибка обработки", reply_markup=main_menu())
        await state.clear()

# === ОБРАБОТКА ФОТОГРАФИЙ ===

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработка фотографий растений"""
    try:
        processing_msg = await message.reply(
            "🔍 <b>Анализирую растение...</b>",
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
                "needs_retry": result.get("needs_retry", False)
            }
            
            retry_text = ""
            if result.get("needs_retry"):
                retry_text = "\n\n📸 <b>Для лучшего результата сделайте фото при ярком освещении</b>"
            
            response_text = f"🌱 <b>Результат анализа:</b>\n\n{result['analysis']}{retry_text}"
            
            await message.reply(
                response_text,
                parse_mode="HTML",
                reply_markup=after_analysis()
            )
        else:
            await message.reply(
                "❌ Ошибка анализа. Попробуйте другое фото.",
                reply_markup=simple_back_menu()
            )
            
    except Exception as e:
        print(f"Ошибка обработки фото: {e}")
        await message.reply("❌ Техническая ошибка", reply_markup=simple_back_menu())

# === CALLBACK ОБРАБОТЧИКИ ===

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

@dp.callback_query(F.data == "my_plants")
async def my_plants_callback(callback: types.CallbackQuery):
    """Просмотр коллекции"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=15)
        
        if not plants:
            await callback.message.answer(
                "🌱 <b>Коллекция пуста</b>\n\n"
                "Добавьте первое растение!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            await callback.answer()
            return
        
        text = f"🌿 <b>Ваша коллекция ({len(plants)} растений):</b>\n\n"
        
        keyboard_buttons = []
        
        for i, plant in enumerate(plants, 1):
            plant_name = plant['display_name']
            saved_date = plant["saved_date"].strftime("%d.%m.%Y")
            
            if plant['type'] == 'growing':
                stage_info = plant.get('stage_info', 'В процессе')
                text += f"{i}. 🌱 <b>{plant_name}</b>\n"
                text += f"   📅 Начато: {saved_date}\n"
                text += f"   🌿 {stage_info}\n\n"
            else:
                moscow_now = get_moscow_now()
                
                if plant["last_watered"]:
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
                        water_status = f"💧 {days_ago}д назад"
                else:
                    water_status = "🆕 Новое"
                
                text += f"{i}. 🌱 <b>{plant_name}</b>\n"
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
        print(f"Ошибка загрузки коллекции: {e}")
        await callback.message.answer("❌ Ошибка загрузки")
    
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
        print(f"Ошибка статистики: {e}")
        await callback.message.answer("❌ Ошибка", reply_markup=main_menu())
    
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    """Справка"""
    await help_command(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "notification_settings")
async def notification_settings_callback(callback: types.CallbackQuery):
    """Настройки уведомлений"""
    await callback.message.answer(
        "🔔 <b>Настройки</b>\n\nФункция в разработке",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    await callback.message.answer("🌱 <b>Главное меню</b>", parse_mode="HTML", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "ask_about")
async def ask_about_callback(callback: types.CallbackQuery, state: FSMContext):
    """Вопрос о растении"""
    await callback.message.answer("❓ <b>Напишите ваш вопрос</b>", parse_mode="HTML")
    await state.set_state(PlantStates.waiting_question)
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
        print(f"Ошибка полива: {e}")
        await callback.message.answer("❌ Ошибка")
    
    await callback.answer()

def format_openai_response(text: str) -> str:
    """Форматирование ответа"""
    if not text:
        return text
    
    import re
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
    
    return text.strip()

# === WEBHOOK И ЗАПУСК ===

async def on_startup():
    """Инициализация"""
    await init_database()
    
    scheduler.add_job(
        check_and_send_reminders,
        'cron',
        hour=9,
        minute=0,
        id='reminder_check',
        replace_existing=True
    )
    scheduler.start()
    print("🔔 Планировщик запущен (9:00 МСК)")
    
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        print(f"Webhook: {WEBHOOK_URL}/webhook")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Polling mode")

async def on_shutdown():
    """Завершение"""
    if scheduler.running:
        scheduler.shutdown()
    
    try:
        db = await get_db()
        await db.close()
    except:
        pass
    
    try:
        await bot.session.close()
    except:
        pass

async def webhook_handler(request):
    """Webhook"""
    try:
        url = str(request.url)
        index = url.rfind('/')
        token = url[index + 1:]
        
        if token == BOT_TOKEN.split(':')[1]:
            update = types.Update.model_validate(await request.json(), strict=False)
            await dp.feed_update(bot, update)
            return web.Response()
        else:
            return web.Response(status=403)
    except Exception as e:
        print(f"Webhook error: {e}")
        return web.Response(status=500)

async def health_check(request):
    """Health"""
    return web.json_response({
        "status": "healthy", 
        "bot": "Bloom AI", 
        "version": "3.5"
    })

async def main():
    """Main"""
    logging.basicConfig(level=logging.INFO)
    
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
        
        print(f"🚀 Bloom AI v3.5 на порту {PORT}")
        print(f"📅 Календарь задач активен!")
        
        try:
            await asyncio.Future()
        except KeyboardInterrupt:
            print("🛑 Остановка")
        finally:
            await runner.cleanup()
            await on_shutdown()
    else:
        print("🤖 Polling mode")
        print(f"📅 Календарь задач активен!")
        try:
            await dp.start_polling(bot, drop_pending_updates=True)
        except KeyboardInterrupt:
            print("🛑 Остановка")
        finally:
            await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    except KeyboardInterrupt:
        print("🛑 Стоп")
