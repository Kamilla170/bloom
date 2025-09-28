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

# Планировщик для напоминаний
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# Московская временная зона (UTC+3)
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PLANTID_API_KEY = os.getenv("PLANTID_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Планировщик напоминаний
scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)

# Временное хранилище для анализов
temp_analyses = {}

# База знаний для распознавания растений по характеристикам - с честным анализом
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

# Состояния
class PlantStates(StatesGroup):
    waiting_question = State()
    editing_plant_name = State()
    choosing_plant_to_grow = State()
    planting_setup = State()
    waiting_growing_photo = State()
    adding_diary_entry = State()
    # Онбординг
    onboarding_welcome = State()
    onboarding_demo = State()
    onboarding_quick_start = State()

# Состояния для обратной связи
class FeedbackStates(StatesGroup):
    choosing_type = State()
    writing_message = State()

# Функция для получения текущего московского времени
def get_moscow_now():
    """Получить текущее время в московской зоне"""
    return datetime.now(MOSCOW_TZ)

def get_moscow_date():
    """Получить текущую дату в московской зоне"""
    return get_moscow_now().date()

def moscow_to_naive(moscow_datetime):
    """Конвертировать московское время в naive datetime для PostgreSQL"""
    if moscow_datetime.tzinfo is not None:
        return moscow_datetime.replace(tzinfo=None)
    return moscow_datetime

# === СИСТЕМА НАПОМИНАНИЙ ===

async def check_and_send_reminders():
    """Проверка и отправка напоминаний о поливе и этапах выращивания (ежедневно утром)"""
    try:
        db = await get_db()
        
        # Получаем текущее московское время
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
    """Проверка и отправка напоминаний по этапам выращивания"""
    try:
        db = await get_db()
        moscow_now = get_moscow_now()
        
        # Получаем выращиваемые растения, которые нуждаются в напоминаниях
        async with db.pool.acquire() as conn:
            growing_plants = await conn.fetch("""
                SELECT gp.id, gp.user_id, gp.plant_name, gp.current_stage, gp.total_stages,
                       gp.started_date, gs.stage_name, gs.stage_description, gs.estimated_duration_days,
                       gp.photo_file_id
                FROM growing_plants gp
                JOIN growth_stages gs ON gp.id = gs.growing_plant_id AND gs.stage_number = gp.current_stage + 1
                JOIN user_settings us ON gp.user_id = us.user_id
                WHERE gp.status = 'active'
                  AND us.reminder_enabled = TRUE
                  AND (gp.started_date::date + (gs.estimated_duration_days || ' days')::interval <= $1::date
                       OR (gp.current_stage = 0 AND gp.started_date::date + INTERVAL '3 days' <= $1::date))
                  AND NOT EXISTS (
                    SELECT 1 FROM reminders r 
                    WHERE r.growing_plant_id = gp.id 
                    AND r.stage_number = gp.current_stage + 1
                    AND r.last_sent::date = $1::date
                  )
            """, moscow_now.date())
            
            for growing_plant in growing_plants:
                await send_growing_reminder(growing_plant)
                
    except Exception as e:
        print(f"Ошибка проверки напоминаний выращивания: {e}")

async def send_growing_reminder(growing_row):
    """Отправка напоминания по этапу выращивания"""
    try:
        user_id = growing_row['user_id']
        growing_id = growing_row['id']
        plant_name = growing_row['plant_name']
        current_stage = growing_row['current_stage']
        next_stage = current_stage + 1
        stage_name = growing_row['stage_name']
        stage_description = growing_row['stage_description']
        
        # Определяем тип напоминания
        if current_stage == 0:
            reminder_type = "start_stage"
            message_text = f"🌱 <b>Время начать выращивание!</b>\n\n"
            message_text += f"🌿 <b>{plant_name}</b>\n"
            message_text += f"📋 <b>Этап {next_stage}: {stage_name}</b>\n\n"
            message_text += f"📝 <b>Что нужно сделать:</b>\n{stage_description}\n\n"
            
            # Добавляем визуальные ориентиры
            message_text += f"👀 <b>Как должно выглядеть:</b>\n"
            message_text += f"Подготовленные материалы: горшки с дренажем, качественная почва, семена/черенки\n\n"
            message_text += f"📸 <b>Совет для фото:</b> Сфотографируйте все подготовленные материалы перед началом работы\n\n"
            message_text += f"💡 Готовы начать этот этап?"
        else:
            reminder_type = "next_stage"
            message_text = f"🌿 <b>Время перейти к следующему этапу!</b>\n\n"
            message_text += f"🌱 <b>{plant_name}</b>\n"
            message_text += f"✅ Этап {current_stage} завершен\n"
            message_text += f"📋 <b>Следующий этап {next_stage}: {stage_name}</b>\n\n"
            message_text += f"📝 <b>Что нужно сделать:</b>\n{stage_description}\n\n"
            
            # Добавляем визуальные ориентиры в зависимости от этапа
            if next_stage == 2:  # Посадка
                message_text += f"👀 <b>Как должно выглядеть:</b>\n"
                message_text += f"Посеянные семена в увлажненной почве, ровная поверхность грунта\n\n"
                message_text += f"📸 <b>Сфотографируйте:</b> Процесс посева и готовые политые горшки\n\n"
            elif next_stage == 3:  # Рост
                message_text += f"👀 <b>Как должно выглядеть:</b>\n"
                message_text += f"Первые всходы 1-3 см высотой, зеленые семядольные листочки\n\n"
                message_text += f"📸 <b>Сфотографируйте:</b> Первые всходы - это важный момент!\n\n"
            elif next_stage == 4:  # Взрослое растение
                message_text += f"👀 <b>Как должно выглядеть:</b>\n"
                message_text += f"Развитое растение с настоящими листьями, готовое к использованию\n\n"
                message_text += f"📸 <b>Сфотографируйте:</b> Красивое взрослое растение во всей красе!\n\n"
            
            message_text += f"📸 Сфотографируйте результат предыдущего этапа!"
        
        # Кнопки для управления
        keyboard = [
            [InlineKeyboardButton(text="✅ Перейти к этапу", callback_data=f"advance_stage_{growing_id}")],
            [InlineKeyboardButton(text="📸 Добавить фото", callback_data=f"add_diary_photo_{growing_id}")],
            [InlineKeyboardButton(text="📝 Записать заметку", callback_data=f"add_diary_note_{growing_id}")],
            [InlineKeyboardButton(text="⏰ Напомнить завтра", callback_data=f"snooze_growing_{growing_id}")],
        ]
        
        # Отправляем уведомление
        if growing_row['photo_file_id']:
            await bot.send_photo(
                chat_id=user_id,
                photo=growing_row['photo_file_id'],
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
        moscow_now_naive = moscow_now.replace(tzinfo=None)  # Конвертируем в naive
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO reminders (user_id, growing_plant_id, reminder_type, next_date, last_sent, stage_number)
                VALUES ($1, $2, $3, $4, $4, $5)
                ON CONFLICT (user_id, growing_plant_id, reminder_type, stage_number) 
                WHERE is_active = TRUE
                DO UPDATE SET 
                    last_sent = $4,
                    send_count = COALESCE(reminders.send_count, 0) + 1
            """, user_id, growing_id, reminder_type, moscow_now_naive, next_stage)
        
        print(f"📤 Отправлено напоминание по выращиванию пользователю {user_id} для {plant_name}")
        
    except Exception as e:
        print(f"Ошибка отправки напоминания по выращиванию: {e}")

async def send_watering_reminder(plant_row):
    """Отправка персонализированного напоминания о поливе"""
    try:
        user_id = plant_row['user_id']
        plant_id = plant_row['id']
        plant_name = plant_row['display_name']
        
        # Получаем полную информацию о растении для персональных рекомендаций
        db = await get_db()
        plant_info = await db.get_plant_by_id(plant_id)
        
        # Вычисляем сколько дней прошло с последнего полива (по московскому времени)
        moscow_now = get_moscow_now()
        
        if plant_row['last_watered']:
            # Конвертируем UTC время из БД в московское
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
        
        # Формируем персональное сообщение
        message_text = f"💧 <b>Время полить растение!</b>\n\n"
        message_text += f"🌱 <b>{plant_name}</b>\n"
        message_text += f"⏰ {time_info}\n"
        
        # Добавляем персональные рекомендации если есть
        if plant_info and plant_info.get('notes'):
            notes = plant_info['notes']
            if "Персональные рекомендации по поливу:" in notes:
                personal_rec = notes.replace("Персональные рекомендации по поливу:", "").strip()
                message_text += f"\n💡 <b>Ваши персональные рекомендации:</b>\n{personal_rec}\n"
            else:
                message_text += f"\n📝 <b>Заметка:</b> {notes}\n"
        else:
            message_text += f"\n💡 Проверьте влажность почвы пальцем\n"
        
        # Интервал полива для информации
        interval = plant_row.get('watering_interval', 5)
        message_text += f"\n⏱️ <i>Интервал полива: каждые {interval} дней</i>"
        
        # Кнопки для быстрых действий
        keyboard = [
            [InlineKeyboardButton(text="💧 Полил(а)!", callback_data=f"water_plant_{plant_id}")],
            [InlineKeyboardButton(text="⏰ Напомнить завтра", callback_data=f"snooze_{plant_id}")],
            [InlineKeyboardButton(text="🔧 Настройки растения", callback_data=f"edit_plant_{plant_id}")],
        ]
        
        # Отправляем уведомление с фото растения
        await bot.send_photo(
            chat_id=user_id,
            photo=plant_row['photo_file_id'],
            caption=message_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        # Отмечаем что напоминание отправлено (московское время)
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
        
        # Конвертируем в naive datetime для PostgreSQL
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
            
            # Создаем напоминание на завтра (через 1 день)
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
    # Очищаем предыдущее состояние если есть
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
        "✍️ Просто напишите название растения, а я подберу лучший способ выращивания и составлю подробный план!",
        parse_mode="HTML"
    )
    
    # Переводим в состояние выбора растения для выращивания
    await state.set_state(PlantStates.choosing_plant_to_grow)
    await callback.answer()

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
        
        # Показываем процесс подготовки
        processing_msg = await message.reply(
            f"🧠 <b>Готовлю персональный план выращивания...</b>\n\n"
            f"🌱 Растение: {plant_name}\n"
            f"🔍 Анализирую лучший способ выращивания\n"
            f"⏳ Составляю подробную инструкцию...",
            parse_mode="HTML"
        )
        
        # Получаем план выращивания от AI
        growing_plan = await get_growing_plan_from_ai(plant_name)
        
        await processing_msg.delete()
        
        if growing_plan:
            # Сохраняем план в состояние
            await state.update_data(
                plant_name=plant_name,
                growing_plan=growing_plan
            )
            
            keyboard = [
                [InlineKeyboardButton(text="✅ Понятно, начинаем!", callback_data="confirm_growing_plan")],
                [InlineKeyboardButton(text="🔄 Выбрать другое растение", callback_data="grow_from_scratch")],
                [InlineKeyboardButton(text="❓ Задать вопрос по плану", callback_data="ask_about_plan")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
            ]
            
            response_text = f"🌱 <b>Персональный план готов!</b>\n\n{growing_plan}\n\n"
            response_text += f"📋 Этот план создан специально для выращивания {plant_name}.\n"
            response_text += f"Готовы начать? Я буду помогать на каждом этапе!"
            
            await message.reply(
                response_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
            # НЕ очищаем состояние здесь - данные нужны для confirm_growing_plan_callback
        else:
            # Если AI не смог создать план
            fallback_keyboard = [
                [InlineKeyboardButton(text="🔄 Попробовать еще раз", callback_data="grow_from_scratch")],
                [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="question")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
            ]
            
            await message.reply(
                f"🤔 <b>Не удалось составить план для '{plant_name}'</b>\n\n"
                f"💡 <b>Возможные причины:</b>\n"
                f"• Слишком редкое или экзотичное растение\n"
                f"• Неточное название\n"
                f"• Временные проблемы с AI\n\n"
                f"📝 <b>Попробуйте:</b>\n"
                f"• Написать название по-другому\n"
                f"• Выбрать более популярное растение\n"
                f"• Задать вопрос в свободной форме",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=fallback_keyboard)
            )
            # Очищаем состояние только если план не создался
            await state.clear()
        
    except Exception as e:
        print(f"Ошибка обработки выбора растения: {e}")
        await message.reply(
            "❌ Произошла ошибка при обработке.\n"
            "Попробуйте еще раз или выберите другое растение.",
            reply_markup=simple_back_menu()
        )
        await state.clear()

async def get_growing_plan_from_ai(plant_name: str) -> str:
    """Получает план выращивания от ИИ"""
    if not openai_client:
        return None
    
    try:
        prompt = f"""
Создайте подробный план выращивания растения "{plant_name}" для начинающего садовода с ДЕТАЛЬНЫМИ ВИЗУАЛЬНЫМИ ОПИСАНИЯМИ того, как должно выглядеть растение на каждом этапе.

Автоматически определите лучший способ выращивания (семена, черенки, луковицы и т.д.) и создайте пошаговую инструкцию.

Структура ответа:
🌱 РАСТЕНИЕ: {plant_name}
🎯 СПОСОБ ВЫРАЩИВАНИЯ: [семена/черенки/луковицы/другое]
📋 СЛОЖНОСТЬ: [легко/средне/сложно]
⏰ ВРЕМЯ ДО РЕЗУЛЬТАТА: [сроки от посадки до взрослого растения]

📝 ПОШАГОВЫЙ ПЛАН:

🌱 ЭТАП 1: ПОДГОТОВКА ([сроки])
👀 КАК ВЫГЛЯДИТ: [детальное описание того, что видно - семена, материалы, подготовленные горшки]
• [действие 1]
• [действие 2] 
• [действие 3]

🌿 ЭТАП 2: ПОСАДКА/ПОСЕВ ([сроки])
👀 КАК ВЫГЛЯДИТ: [как выглядят посеянные семена, почва после посадки, первые признаки жизни]
• [действие 1]
• [действие 2]
• [действие 3]

🌱 ЭТАП 3: УХОД В ПЕРИОД РОСТА ([сроки])
👀 КАК ВЫГЛЯДИТ: [детальное описание всходов, листьев, высоты растения, цвета, формы]
• [действие 1]
• [действие 2]
• [действие 3]

🌸 ЭТАП 4: ВЗРОСЛОЕ РАСТЕНИЕ ([сроки])
👀 КАК ВЫГЛЯДИТ: [описание взрослого растения - размер, форма куста, листья, цветы если есть]
• [действие 1]
• [действие 2]

💡 ВАЖНЫЕ СОВЕТЫ:
• [совет 1]
• [совет 2]
• [совет 3]

⚠️ ЧАСТЫЕ ОШИБКИ:
• [ошибка 1]
• [ошибка 2]

📸 ОРИЕНТИРЫ ДЛЯ ФОТО:
• ЭТАП 1: Сфотографируйте подготовленные материалы и место посадки
• ЭТАП 2: Покажите процесс посева и политую почву
• ЭТАП 3: Фото первых всходов и роста листьев
• ЭТАП 4: Готовое взрослое растение во всей красе

Отвечайте практично и конкретно на русском языке. НЕ используйте ** для выделения. ОБЯЗАТЕЛЬНО включайте детальные визуальные описания для каждого этапа.
        """
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system", 
                    "content": "Вы - эксперт по выращиванию растений с 20-летним опытом. Создавайте практичные, понятные планы выращивания для домашних условий. Автоматически выбирайте лучший метод выращивания для каждого растения."
                },
                {"role": "user", "content": prompt}
            ],
            max_tokens=1200,
            temperature=0.3
        )
        
        return response.choices[0].message.content
        
    except Exception as e:
        print(f"Ошибка получения плана выращивания: {e}")
        return None

@dp.callback_query(F.data == "confirm_growing_plan")
async def confirm_growing_plan_callback(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение плана и запуск выращивания - упрощенный без фото"""
    try:
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        
        print(f"DEBUG: State data = {data}")  # Для отладки
        
        if not plant_name or not growing_plan:
            await callback.message.answer(
                "❌ <b>Данные плана не найдены</b>\n\n"
                "Это могло произойти из-за:\n"
                "• Долгого ожидания (данные устарели)\n"
                "• Технической ошибки\n\n"
                "🔄 Попробуйте создать план заново:",
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
        
        print(f"DEBUG start_growing_no_photo: plant_name={plant_name}, plan_exists={bool(growing_plan)}")
        
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
        photo = message.photo[-1]  # Лучшее качество
        user_id = message.from_user.id
        
        # Проверяем что данные состояния доступны
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        
        print(f"DEBUG handle_growing_photo: plant_name={plant_name}, plan_exists={bool(growing_plan)}")
        
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

async def finalize_growing_setup(message_obj, state: FSMContext, photo_file_id: str, user_id: int):
    """Финализация настройки выращивания - упрощенная версия"""
    try:
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        
        print(f"DEBUG finalize_growing_setup: user_id={user_id}")
        print(f"DEBUG finalize_growing_setup: plant_name={plant_name}")
        print(f"DEBUG finalize_growing_setup: plan_exists={bool(growing_plan)}")
        
        if not plant_name or not growing_plan:
            print("ERROR: Missing plant_name or growing_plan in finalize_growing_setup")
            await message_obj.answer(
                "❌ <b>Критическая ошибка</b>\n\n"
                "Данные плана не найдены.\n"
                "Попробуйте создать план заново.",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            await state.clear()
            return
        
        # Определяем способ выращивания из плана
        growth_method = "семена"  # по умолчанию
        if growing_plan:
            for line in growing_plan.split('\n'):
                if line.startswith("🎯 СПОСОБ ВЫРАЩИВАНИЯ:"):
                    growth_method = line.replace("🎯 СПОСОБ ВЫРАЩИВАНИЯ:", "").strip()
                    break
        
        print(f"DEBUG: growth_method={growth_method}")
        
        # Создаем выращиваемое растение в БД
        db = await get_db()
        print("DEBUG: Got database connection")
        
        try:
            growing_id = await db.create_growing_plant(
                user_id=user_id,
                plant_name=plant_name,
                growth_method=growth_method,
                growing_plan=growing_plan,
                photo_file_id=photo_file_id
            )
            print(f"DEBUG: Created growing plant with id={growing_id}")
        except Exception as e:
            print(f"ERROR creating growing plant: {e}")
            raise
        
        # Создаем первоначальное напоминание (через 3 дня) - исправленная версия
        try:
            moscow_now = get_moscow_now()
            next_reminder = moscow_now + timedelta(days=3)
            
            # Конвертируем в naive datetime для PostgreSQL
            next_reminder_naive = next_reminder.replace(tzinfo=None)
            print(f"DEBUG: Creating reminder for {next_reminder_naive}")
            
            await db.create_growing_reminder(
                growing_id=growing_id,
                user_id=user_id,
                reminder_type="start_stage",
                next_date=next_reminder_naive,
                stage_number=1
            )
            print("DEBUG: Created reminder successfully")
        except Exception as e:
            print(f"ERROR creating reminder: {e}")
            # Не блокируем создание растения если не удалось создать напоминание
            print("WARNING: Plant created but reminder failed - continuing")
        
        success_text = f"🎉 <b>Выращивание {plant_name} началось!</b>\n\n"
        success_text += f"📋 План выращивания создан с учетом всех этапов\n"
        success_text += f"⏰ Первое напоминание придет через 3 дня\n"
        success_text += f"🌱 <b>Что теперь:</b>\n"
        success_text += f"• Следуйте инструкциям из плана\n"
        success_text += f"• Добавляйте фото в дневник роста\n"
        success_text += f"• Растение появится в вашей коллекции\n\n"
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
        
        print("DEBUG: Success message sent, clearing state")
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка финализации выращивания: {e}")
        import traceback
        traceback.print_exc()
        
        try:
            await message_obj.answer(
                "❌ Ошибка создания плана выращивания.\n"
                "Попробуйте еще раз позже.",
                reply_markup=simple_back_menu()
            )
        except Exception as e2:
            print(f"Ошибка отправки сообщения об ошибке: {e2}")
        
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
        
        # Переводим на следующий этап
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
            
            # Получаем информацию о новом этапе
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
            # Добавляем фото в дневник
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
            # Добавляем текстовую заметку
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
            
            # Извлекаем персональную информацию о поливе
            watering_info = extract_personal_watering_info(raw_analysis)
            
            # Сохраняем в БД
            db = await get_db()
            plant_id = await db.save_plant(
                user_id=user_id,
                analysis=raw_analysis,
                photo_file_id=analysis_data["photo_file_id"],
                plant_name=analysis_data.get("plant_name", "Неизвестное растение")
            )
            
            # Устанавливаем персональный интервал полива
            personal_interval = watering_info["interval_days"]
            await db.update_plant_watering_interval(plant_id, personal_interval)
            
            # Если растение нуждается в корректировке полива, устанавливаем заметку
            if watering_info["needs_adjustment"] and watering_info["personal_recommendations"]:
                async with db.pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE plants SET notes = $1 WHERE id = $2
                    """, f"Персональные рекомендации по поливу: {watering_info['personal_recommendations']}", plant_id)
            
            # Создаем напоминание с персональным интервалом
            await create_plant_reminder(plant_id, user_id, personal_interval)
            
            # Удаляем временные данные
            del temp_analyses[user_id]
            
            plant_name = analysis_data.get("plant_name", "растение")
            
            # Формируем сообщение с персональной информацией
            success_text = f"✅ <b>Растение успешно добавлено в коллекцию!</b>\n\n"
            success_text += f"🌱 <b>{plant_name}</b> теперь в вашем цифровом саду\n"
            
            # Добавляем информацию о персональном интервале
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

# Обновленная функция полива
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
        
        # Обновляем полив
        await db.update_watering(user_id, plant_id)
        
        # Создаем следующее напоминание
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
        
        # Проверяем, это обычное растение или выращиваемое
        if str(plant_id).startswith("growing_"):
            # Это выращиваемое растение
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
            # Обычное растение
            plant = await db.get_plant_by_id(plant_id, user_id)
            
            if not plant:
                await callback.answer("❌ Растение не найдено")
                return
            
            plant_name = plant['display_name']
            watering_interval = plant.get('watering_interval', 5)
            
            # Статус полива
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
        # Получаем текст сообщения
        feedback_text = message.text.strip() if message.text else ""
        
        # Получаем фото если есть
        feedback_photo = None
        if message.photo:
            feedback_photo = message.photo[-1].file_id
        
        # Если нет ни текста ни фото
        if not feedback_text and not feedback_photo:
            await message.reply(
                "📝 <b>Пожалуйста, напишите сообщение или приложите фото</b>\n\n"
                "Ваш отзыв поможет улучшить бота!",
                parse_mode="HTML"
            )
            return
        
        # Проверяем длину текста если он есть
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
        
        # Если только фото без текста
        if not feedback_text and feedback_photo:
            feedback_text = "Фото без комментария"
        
        # Получаем тип обратной связи из состояния
        data = await state.get_data()
        feedback_type = data.get('feedback_type', 'review')
        
        # Отправляем обратную связь
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
        
        # Подготавливаем контекст если есть последний анализ
        context_data = None
        if user_id in temp_analyses:
            context_data = json.dumps({
                "last_analysis": temp_analyses[user_id].get("plant_name", "Unknown"),
                "confidence": temp_analyses[user_id].get("confidence", 0),
                "source": temp_analyses[user_id].get("source", "unknown")
            })
        
        # Сохраняем в БД
        db = await get_db()
        feedback_id = await db.save_feedback(
            user_id=user_id,
            username=username,
            feedback_type=feedback_type,
            message=feedback_message,
            photo_file_id=feedback_photo,
            context_data=context_data
        )
        
        # Логируем в консоль для разработчика
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
        
        # Благодарим пользователя
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
        "interval_days": 5,  # по умолчанию
        "personal_recommendations": "",
        "current_state": "",
        "needs_adjustment": False
    }
    
    if not analysis_text:
        return watering_info
    
    lines = analysis_text.split('\n')
    
    for line in lines:
        line = line.strip()
        
        # Извлекаем персональный интервал полива
        if line.startswith("ПОЛИВ_ИНТЕРВАЛ:"):
            interval_text = line.replace("ПОЛИВ_ИНТЕРВАЛ:", "").strip()
            # Ищем числа в тексте
            import re
            numbers = re.findall(r'\d+', interval_text)
            if numbers:
                try:
                    interval = int(numbers[0])
                    # Ограничиваем разумными пределами
                    if 1 <= interval <= 15:
                        watering_info["interval_days"] = interval
                except:
                    pass
        
        # Извлекаем анализ текущего состояния полива
        elif line.startswith("ПОЛИВ_АНАЛИЗ:"):
            current_state = line.replace("ПОЛИВ_АНАЛИЗ:", "").strip()
            watering_info["current_state"] = current_state
            # Проверяем, нужна ли корректировка
            if "не видна" in current_state.lower() or "невозможно оценить" in current_state.lower():
                watering_info["needs_adjustment"] = True
            elif any(word in current_state.lower() for word in ["переувлажн", "перелив", "недополит", "пересушен", "проблем"]):
                watering_info["needs_adjustment"] = True
        
        # Извлекаем персональные рекомендации
        elif line.startswith("ПОЛИВ_РЕКОМЕНДАЦИИ:"):
            recommendations = line.replace("ПОЛИВ_РЕКОМЕНДАЦИИ:", "").strip()
            watering_info["personal_recommendations"] = recommendations
            
    return watering_info

def format_plant_analysis(raw_text: str, confidence: float = None) -> str:
    """Обновленная функция форматирования для честного анализа"""
    
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    formatted = ""
    
    # Парсим структурированный ответ
    plant_name = "Неизвестное растение"
    confidence_level = confidence or 0
    
    for line in lines:
        if line.startswith("РАСТЕНИЕ:"):
            plant_name = line.replace("РАСТЕНИЕ:", "").strip()
            # Убираем лишнюю информацию в скобках для заголовка
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
        
        # === БЛОК ВИДИМОСТИ ===
        elif line.startswith("🔍 ВИДИМОСТЬ ЭЛЕМЕНТОВ:"):
            formatted += f"🔍 <b>Что видно на фото:</b>\n"
            
        elif line.startswith("ЛИСТЬЯ:"):
            visibility = line.replace("ЛИСТЬЯ:", "").strip()
            icon = "✅" if "видны четко" in visibility else "⚠️" if "частично" in visibility else "❌"
            formatted += f"{icon} Листья: {visibility}\n"
            
        elif line.startswith("ПОЧВА:"):
            visibility = line.replace("ПОЧВА:", "").strip()
            icon = "✅" if "видна" in visibility and "не видна" not in visibility else "❌"
            formatted += f"{icon} Почва: {visibility}\n"
            
        elif line.startswith("ГОРШОК:"):
            visibility = line.replace("ГОРШОК:", "").strip()
            icon = "✅" if "виден" in visibility else "❌"
            formatted += f"{icon} Горшок: {visibility}\n"
            
        elif line.startswith("ЦВЕТЫ_БУТОНЫ:"):
            visibility = line.replace("ЦВЕТЫ_БУТОНЫ:", "").strip()
            icon = "🌸" if "есть" in visibility else "◯"
            formatted += f"{icon} Цветы: {visibility}\n\n"
        
        # === АНАЛИЗ ВИДИМЫХ ЭЛЕМЕНТОВ ===
        elif line.startswith("ЛИСТЬЯ_АНАЛИЗ:"):
            analysis = line.replace("ЛИСТЬЯ_АНАЛИЗ:", "").strip()
            formatted += f"🍃 <b>Состояние листьев:</b> {analysis}\n"
            
        elif line.startswith("ПОЧВА_АНАЛИЗ:"):
            analysis = line.replace("ПОЧВА_АНАЛИЗ:", "").strip()
            formatted += f"🪴 <b>Состояние почвы:</b> {analysis}\n"
            
        elif line.startswith("ПОЛИВ_СОСТОЯНИЕ:"):
            state = line.replace("ПОЛИВ_СОСТОЯНИЕ:", "").strip()
            if "НЕВОЗМОЖНО_ОЦЕНИТЬ" in state:
                icon = "❓"
                formatted += f"{icon} <b>Полив:</b> Невозможно оценить по фото\n"
            elif any(word in state.lower() for word in ["переувлажн", "перелив"]):
                icon = "🔴"
                formatted += f"{icon} <b>Полив:</b> {state}\n"
            elif any(word in state.lower() for word in ["недополит", "пересушен"]):
                icon = "🟡"
                formatted += f"{icon} <b>Полив:</b> {state}\n"
            else:
                icon = "🟢"
                formatted += f"{icon} <b>Полив:</b> {state}\n"
        
        elif line.startswith("ОБЩЕЕ_СОСТОЯНИЕ:"):
            condition = line.replace("ОБЩЕЕ_СОСТОЯНИЕ:", "").strip()
            if any(word in condition.lower() for word in ["здоров", "хорош", "отличн", "норм"]):
                icon = "✅"
            elif any(word in condition.lower() for word in ["проблем", "болен", "плох", "стресс"]):
                icon = "⚠️"
            else:
                icon = "ℹ️"
            formatted += f"{icon} <b>Общее состояние:</b> {condition}\n\n"
        
        # === ОГРАНИЧЕНИЯ АНАЛИЗА ===
        elif line.startswith("⚠️ ОГРАНИЧЕНИЯ АНАЛИЗА:"):
            formatted += f"⚠️ <b>Ограничения анализа:</b>\n"
            
        elif line.startswith("ПОЛИВ:") and "Невозможно оценить" in line:
            limitation = line.replace("ПОЛИВ:", "").strip()
            formatted += f"💧 Полив: {limitation}\n"
            
        elif line.startswith("КОРНЕВАЯ_СИСТЕМА:") and "Невозможно оценить" in line:
            limitation = line.replace("КОРНЕВАЯ_СИСТЕМА:", "").strip()
            formatted += f"🌱 Корни: {limitation}\n"
            
        elif line.startswith("ВРЕДИТЕЛИ:") and ("Возможны" in line or "невозможно" in line.lower()):
            limitation = line.replace("ВРЕДИТЕЛИ:", "").strip()
            formatted += f"🐛 Вредители: {limitation}\n"
            
        elif line.startswith("ДРЕНАЖ:") and "Невозможно оценить" in line:
            limitation = line.replace("ДРЕНАЖ:", "").strip()
            formatted += f"🕳️ Дренаж: {limitation}\n"
        
        # === РЕКОМЕНДАЦИИ НА ОСНОВЕ ВИДИМОГО ===
        elif line.startswith("УХОД_ПО_ВИДУ:"):
            care = line.replace("УХОД_ПО_ВИДУ:", "").strip()
            formatted += f"\n🌿 <b>Общий уход для этого вида:</b>\n{care}\n"
            
        elif line.startswith("ПОЛИВ_РЕКОМЕНДАЦИИ:"):
            watering_rec = line.replace("ПОЛИВ_РЕКОМЕНДАЦИИ:", "").strip()
            if "общие советы" in watering_rec.lower() or "невозможно" in watering_rec.lower():
                formatted += f"💧 <b>Общие советы по поливу:</b> {watering_rec}\n"
            else:
                formatted += f"💧 <b>Персональные рекомендации:</b> {watering_rec}\n"
            
        elif line.startswith("ПОЛИВ_ИНТЕРВАЛ:"):
            interval = line.replace("ПОЛИВ_ИНТЕРВАЛ:", "").strip()
            if "требует диагностики" in interval.lower():
                formatted += f"⏰ <b>Интервал полива:</b> {interval}\n"
            else:
                formatted += f"⏰ <b>Рекомендуемый интервал:</b> {interval} дней\n"
            
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
        
        # === ПРОБЛЕМЫ ===
        elif line.startswith("🚨 ПРОБЛЕМЫ_ВИДИМЫЕ:"):
            problems = line.replace("🚨 ПРОБЛЕМЫ_ВИДИМЫЕ:", "").strip()
            if problems and problems.lower() != "нет":
                formatted += f"\n🚨 <b>Видимые проблемы:</b> {problems}\n"
                
        elif line.startswith("ВОЗМОЖНЫЕ_СКРЫТЫЕ:"):
            hidden = line.replace("ВОЗМОЖНЫЕ_СКРЫТЫЕ:", "").strip()
            if hidden and hidden.lower() != "нет":
                formatted += f"👁️ <b>Возможные скрытые проблемы:</b> {hidden}\n"
        
        # === РЕКОМЕНДАЦИИ ДЛЯ ДИАГНОСТИКИ ===
        elif line.startswith("📸 ДЛЯ_ТОЧНОЙ_ДИАГНОСТИКИ:"):
            formatted += f"\n📸 <b>Для точной диагностики сфотографируйте:</b>\n"
            
        elif line.startswith("•") and "📸" in formatted and "Для точной диагностики" in formatted:
            suggestion = line.replace("•", "").strip()
            if suggestion:
                formatted += f"• {suggestion}\n"
        
        # === ИТОГОВЫЙ СОВЕТ ===
        elif line.startswith("💬 ИТОГОВЫЙ_СОВЕТ:"):
            advice = line.replace("💬 ИТОГОВЫЙ_СОВЕТ:", "").strip()
            formatted += f"\n💡 <b>Персональный совет:</b> {advice}"
    
    # Добавляем индикатор качества распознавания
    if confidence_level >= 80:
        formatted += "\n\n🏆 <i>Высокая точность распознавания</i>"
    elif confidence_level >= 60:
        formatted += "\n\n👍 <i>Хорошее распознавание</i>"
    else:
        formatted += "\n\n🤔 <i>Требуется дополнительная идентификация</i>"
    
    formatted += "\n💾 <i>Сохраните для персональных напоминаний!</i>"
    
    return formatted

# Обработка изображений
async def optimize_image_for_analysis(image_data: bytes, high_quality: bool = True) -> bytes:
    """Оптимизация изображения для анализа"""
    try:
        image = Image.open(BytesIO(image_data))
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        # Для анализа используем более высокое качество
        if high_quality:
            # Увеличиваем размер для лучшего анализа
            if max(image.size) < 1024:
                # Увеличиваем маленькие изображения
                ratio = 1024 / max(image.size)
                new_size = (int(image.size[0] * ratio), int(image.size[1] * ratio))
                image = image.resize(new_size, Image.Resampling.LANCZOS)
            elif max(image.size) > 2048:
                # Уменьшаем очень большие
                image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        else:
            # Стандартная оптимизация
            if max(image.size) > 1024:
                image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        
        output = BytesIO()
        # Повышенное качество для анализа
        quality = 95 if high_quality else 85
        image.save(output, format='JPEG', quality=quality, optimize=True)
        return output.getvalue()
    except Exception as e:
        print(f"Ошибка оптимизации изображения: {e}")
        return image_data

# Улучшенный анализ через OpenAI GPT-4 Vision
async def analyze_with_openai_advanced(image_data: bytes, user_question: str = None) -> dict:
    """Продвинутый анализ через OpenAI GPT-4 Vision с честным подходом"""
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
        
        # Проверяем качество ответа
        if len(raw_analysis) < 100 or "не могу" in raw_analysis.lower() or "sorry" in raw_analysis.lower():
            raise Exception("Некачественный ответ от OpenAI")
        
        # Извлекаем уверенность
        confidence = 0
        for line in raw_analysis.split('\n'):
            if line.startswith("УВЕРЕННОСТЬ:"):
                try:
                    conf_str = line.replace("УВЕРЕННОСТЬ:", "").strip().replace("%", "")
                    confidence = float(conf_str)
                except:
                    confidence = 70
                break
        
        # Извлекаем название растения
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

# Основная функция анализа с умным fallback
async def analyze_plant_image(image_data: bytes, user_question: str = None, retry_count: int = 0) -> dict:
    """Интеллектуальный анализ изображения растения"""
    
    # Попытка 1: OpenAI GPT-4 Vision (приоритет)
    print("🔍 Попытка анализа через OpenAI GPT-4 Vision...")
    openai_result = await analyze_with_openai_advanced(image_data, user_question)
    
    # Возвращаем более строгие требования к качеству для исходного промпта
    if openai_result["success"] and openai_result.get("confidence", 0) >= 50:
        print(f"✅ OpenAI успешно распознал растение с {openai_result.get('confidence')}% уверенностью")
        return openai_result
    
    # Повторная попытка только один раз
    if retry_count == 0:
        print("🔄 Повторная попытка анализа...")
        return await analyze_plant_image(image_data, user_question, retry_count + 1)
    
    # Если OpenAI дал результат, но с низкой уверенностью - все равно используем его
    if openai_result["success"]:
        print(f"⚠️ Используем результат с низкой уверенностью: {openai_result.get('confidence')}%")
        openai_result["needs_retry"] = True
        return openai_result
    
    # Fallback только если OpenAI полностью не ответил
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
        
        # Проверяем, новый ли это пользователь
        async with db.pool.acquire() as conn:
            existing_user = await conn.fetchrow(
                "SELECT user_id FROM users WHERE user_id = $1", user_id
            )
            
            if not existing_user:
                # Новый пользователь - добавляем в БД
                await db.add_user(
                    user_id=user_id,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name
                )
                
                # Запускаем онбординг
                await start_onboarding(message)
                return
            else:
                # Существующий пользователь - обычное приветствие
                await show_returning_user_welcome(message)
                return
                
    except Exception as e:
        print(f"Ошибка команды /start: {e}")
        # Fallback - показываем обычное меню
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
    """Показ демо анализа - новая версия"""
    
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
    """Быстрый старт - обновленная версия"""
    
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

# Обработчики действий из онбординга - обновленные
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
        "💡 <b>Примеры:</b>\n"
        "• Базилик\n"
        "• Герань\n"
        "• Тюльпаны\n"
        "• Фикус\n"
        "• Помидоры\n"
        "• Укроп\n"
        "• Фиалка\n"
        "• Кактус\n\n"
        "✍️ Просто напишите название растения, а я подберу лучший способ выращивания и составлю подробный план!",
        parse_mode="HTML"
    )
    
    # Переводим в состояние выбора растения для выращивания
    await state.set_state(PlantStates.choosing_plant_to_grow)

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Команда /help"""
    help_text = """
🌱 <b>Как пользоваться ботом:</b>

🌱 <b>Добавление растения в коллекцию:</b>
• Нажми "🌱 Добавить растение" в главном меню
• Пришли фото растения для анализа и автоматического добавления
• Получи персональные рекомендации и настройки напоминаний

🌿 <b>Выращивание с нуля:</b>
• Нажми "🌿 Вырастить с нуля"
• Напиши название растения которое хочешь вырастить
• Получи персональный план выращивания от ИИ
• Пошаговое сопровождение от посадки до взрослого растения

📸 <b>Анализ растения (без сохранения):</b>
• Пришли фото растения
• Получи полный анализ и рекомендации
• Можешь сохранить результат в коллекцию

⏰ <b>Умные напоминания:</b>
• Ежедневная проверка растений в 9:00 утра (МСК)
• Персональный график для каждого растения
• Быстрая отметка полива из уведомления

🔔 <b>Настройки уведомлений:</b>
• Глобальное включение/выключение всех уведомлений
• Индивидуальные настройки для каждого растения

❓ <b>Вопросы о растениях:</b>
• Просто напиши вопрос в чат
• Получи экспертный совет

🌿 <b>Мои растения:</b>
• Просматривай коллекцию
• Отмечай полив и уход
• Настраивай персональные интервалы

<b>🎯 НОВИНКА - Честный анализ:</b>
• Анализирую только то, что видно на фото
• Честно указываю ограничения анализа
• Рекомендую дополнительные фото для точной диагностики

<b>Для лучшего результата:</b>
• Фотографируй при хорошем освещении
• Покажи листья крупным планом
• Включи почву в горшке для анализа полива
• Сфотографируй обратную сторону листьев при проблемах

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
        "📸 <b>Пришлите фото вашего растения:</b>\n"
        "• Я определю вид и состояние растения\n"
        "• Дам персональные рекомендации по уходу\n"
        "• Автоматически добавлю в вашу коллекцию\n"
        "• Настрою индивидуальные напоминания о поливе\n\n"
        "💡 <b>Советы для лучшего результата:</b>\n"
        "• Фотографируйте при дневном свете\n"
        "• Покажите листья и общий вид растения\n" 
        "• По возможности включите почву в горшке\n"
        "• Избегайте размытых и тёмных снимков\n"
        "• Можете добавить вопрос в описании к фото",
        parse_mode="HTML"
    )

@dp.message(Command("analyze"))
async def analyze_command(message: types.Message):
    """Команда /analyze - анализ растения"""
    await message.answer(
        "📸 <b>Отправьте фото растения для анализа</b>\n\n"
        "💡 <b>Советы для лучшего результата:</b>\n"
        "• Фотографируйте при дневном свете\n"
        "• Покажите листья и общий вид растения\n" 
        "• По возможности включите почву в горшке\n"
        "• Избегайте размытых и тёмных снимков\n"
        "• Можете добавить вопрос в описании к фото\n\n"
        "🎯 <b>Я проанализирую только то, что видно на фото, и честно укажу ограничения!</b>",
        parse_mode="HTML"
    )

@dp.message(Command("question"))
async def question_command(message: types.Message, state: FSMContext):
    """Команда /question - задать вопрос"""
    await message.answer(
        "❓ <b>Задайте ваш вопрос о растениях</b>\n\n"
        "💡 <b>Я могу помочь с:</b>\n"
        "• Проблемами с листьями (желтеют, сохнут, опадают)\n"
        "• Режимом полива и подкормки\n" 
        "• Пересадкой и размножением\n"
        "• Болезнями и вредителями\n"
        "• Выбором места для растения\n"
        "• Любыми другими вопросами по уходу",
        parse_mode="HTML"
    )
    await state.set_state(PlantStates.waiting_question)

@dp.message(Command("plants"))
async def plants_command(message: types.Message):
    """Команда /plants - мои растения"""
    await my_plants_callback(types.CallbackQuery(
        id="fake_callback",
        from_user=message.from_user,
        chat_instance="fake",
        message=message,
        data="my_plants"
    ))

@dp.message(Command("stats"))
async def stats_command(message: types.Message):
    """Команда /stats - статистика"""
    await stats_callback(types.CallbackQuery(
        id="fake_callback",
        from_user=message.from_user,
        chat_instance="fake",
        message=message,
        data="stats"
    ))

@dp.message(Command("notifications"))
async def notifications_command(message: types.Message):
    """Команда /notifications - настройки уведомлений"""
    await notification_settings_callback(types.CallbackQuery(
        id="fake_callback",
        from_user=message.from_user,
        chat_instance="fake",
        message=message,
        data="notification_settings"
    ))

# === ОБРАБОТКА СОСТОЯНИЙ ===

@dp.message(StateFilter(PlantStates.editing_plant_name))
async def handle_plant_rename(message: types.Message, state: FSMContext):
    """Обработка нового названия растения"""
    try:
        new_name = message.text.strip()
        
        # Валидация названия
        if len(new_name) < 2:
            await message.reply(
                "❌ <b>Название слишком короткое</b>\n"
                "Минимум 2 символа. Попробуйте еще раз:",
                parse_mode="HTML"
            )
            return
        
        if len(new_name) > 50:
            await message.reply(
                "❌ <b>Название слишком длинное</b>\n"
                "Максимум 50 символов. Попробуйте еще раз:",
                parse_mode="HTML"
            )
            return
        
        # Проверка на недопустимые символы
        if any(char in new_name for char in ['<', '>', '"', "'"]):
            await message.reply(
                "❌ <b>Недопустимые символы</b>\n"
                "Название не может содержать < > \" '\n"
                "Попробуйте еще раз:",
                parse_mode="HTML"
            )
            return
        
        # Получаем ID растения из состояния
        data = await state.get_data()
        plant_id = data.get('editing_plant_id')
        
        if not plant_id:
            await message.reply("❌ Ошибка: ID растения не найден")
            await state.clear()
            return
        
        user_id = message.from_user.id
        
        # Обновляем название в БД
        db = await get_db()
        await db.update_plant_name(plant_id, user_id, new_name)
        
        success_keyboard = [
            [InlineKeyboardButton(text="⚙️ Настройки растения", callback_data=f"edit_plant_{plant_id}")],
            [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ]
        
        await message.reply(
            f"✅ <b>Название успешно изменено!</b>\n\n"
            f"🌱 Новое название: <b>{new_name}</b>\n\n"
            f"Растение обновлено в вашей коллекции.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=success_keyboard)
        )
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка переименования: {e}")
        await message.reply("❌ Ошибка сохранения названия.")
        await state.clear()

@dp.message(StateFilter(PlantStates.waiting_question))
async def handle_question(message: types.Message, state: FSMContext):
    """Обработка текстовых вопросов с улучшенным контекстом"""
    try:
        processing_msg = await message.reply("🤔 <b>Консультируюсь с экспертом...</b>", parse_mode="HTML")
        
        user_id = message.from_user.id
        user_context = ""
        
        # Добавляем контекст из последнего анализа если есть
        if user_id in temp_analyses:
            plant_info = temp_analyses[user_id]
            plant_name = plant_info.get("plant_name", "растение")
            user_context = f"\n\nКонтекст: Пользователь недавно анализировал {plant_name}. Учтите это в ответе."
        
        answer = None
        
        # Улучшенный промпт для OpenAI
        if openai_client:
            try:
                enhanced_prompt = f"""
Вы - ведущий эксперт по комнатным и садовым растениям с 30-летним опытом.
Ответьте подробно и практично на вопрос пользователя о растениях.

Структура ответа:
1. Краткий диагноз/ответ на вопрос
2. Подробные рекомендации по решению
3. Дополнительные советы по профилактике
4. При необходимости - когда обращаться к специалисту

Форматирование:
- Используйте эмодзи для наглядности
- НЕ используйте ** для выделения текста  
- Давайте конкретные, применимые советы
{user_context}

Вопрос: {message.text}
                """
                
                response = await openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "system",
                            "content": "Вы - профессиональный ботаник и консультант по растениям. Отвечайте экспертно, но доступным языком на русском."
                        },
                        {
                            "role": "user",
                            "content": enhanced_prompt
                        }
                    ],
                    max_tokens=1000,
                    temperature=0.3
                )
                answer = response.choices[0].message.content
            except Exception as e:
                print(f"OpenAI question error: {e}")
        
        await processing_msg.delete()
        
        if answer and len(answer) > 50:
            # Применяем правильное форматирование
            answer = format_openai_response(answer)
            
            # Улучшаем форматирование ответа
            if not answer.startswith(('🌿', '💡', '🔍', '⚠️', '✅')):
                answer = f"🌿 <b>Экспертный ответ:</b>\n\n{answer}"
            
            await message.reply(answer, parse_mode="HTML")
        else:
            # Улучшенный fallback
            fallback_answer = f"""
🤔 <b>По вашему вопросу:</b> "{message.text}"

К сожалению, сейчас не могу дать полный экспертный ответ. 

💡 <b>Рекомендую:</b>
• Сфотографируйте растение для точной диагностики
• Опишите симптомы более подробно
• Обратитесь в ботанический сад или садовый центр
• Попробуйте переформулировать вопрос

🌱 <b>Общие советы:</b>
• Проверьте освещение и полив
• Осмотрите листья на предмет вредителей  
• Убедитесь в подходящей влажности воздуха

Попробуйте задать вопрос позже или пришлите фото для анализа!
            """
            
            await message.reply(fallback_answer, parse_mode="HTML", reply_markup=simple_back_menu())
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка ответа на вопрос: {e}")
        await message.reply(
            "❌ Произошла ошибка при обработке вопроса.\n"
            "🔄 Попробуйте переформулировать или задать вопрос позже.", 
            reply_markup=main_menu()
        )
        await state.clear()

# === ОБРАБОТКА ФОТОГРАФИЙ ===

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработка фотографий растений с улучшенным анализом"""
    try:
        # Показываем прогресс
        processing_msg = await message.reply(
            "🔍 <b>Анализирую ваше растение...</b>\n"
            "⏳ Определяю вид и состояние растения\n"
            "🧠 Готовлю персональные рекомендации\n\n"
            "🎯 <i>Анализирую только то, что видно на фото!</i>",
            parse_mode="HTML"
        )
        
        # Получаем фото в лучшем качестве
        photo = message.photo[-1]  # Самое высокое разрешение
        file = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)
        
        # Обновляем статус анализа
        await processing_msg.edit_text(
            "🔍 <b>Анализирую ваше растение...</b>\n"
            "🌿 Сравниваю с базой растений\n"
            "📊 Оцениваю состояние здоровья\n\n"
            "🎯 <i>Только честный анализ видимых элементов!</i>",
            parse_mode="HTML"
        )
        
        # Анализируем с вопросом пользователя если есть
        user_question = message.caption if message.caption else None
        result = await analyze_plant_image(file_data.read(), user_question)
        
        await processing_msg.delete()
        
        if result["success"]:
            # Сохраняем детальный анализ
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
            
            # Добавляем рекомендации по улучшению фото если нужно
            retry_text = ""
            if result.get("needs_retry"):
                retry_text = ("\n\n📸 <b>Для лучшего результата:</b>\n"
                            "• Сфотографируйте при ярком освещении\n"
                            "• Покажите листья крупным планом\n"
                            "• Включите почву в горшке для анализа полива\n"
                            "• Уберите лишние предметы из кадра")
            
            # Отправляем результат
            response_text = f"🌱 <b>Результат анализа:</b>\n\n{result['analysis']}{retry_text}"
            
            # Выбираем клавиатуру в зависимости от качества анализа
            if result.get("needs_retry"):
                keyboard = [
                    [InlineKeyboardButton(text="🔄 Повторить фото", callback_data="reanalyze")],
                    [InlineKeyboardButton(text="💾 Сохранить как есть", callback_data="save_plant")],
                    [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="ask_about")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
                ]
                reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
            else:
                reply_markup = after_analysis()
            
            await message.reply(
                response_text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        else:
            error_msg = result.get('error', 'Неизвестная ошибка')
            await message.reply(
                f"❌ <b>Ошибка анализа:</b> {error_msg}\n\n"
                f"🔄 Попробуйте:\n"
                f"• Сделать фото при лучшем освещении\n"
                f"• Показать растение целиком\n"
                f"• Включить почву в горшке\n"
                f"• Повторить попытку через минуту",
                parse_mode="HTML",
                reply_markup=simple_back_menu()
            )
            
    except Exception as e:
        print(f"Ошибка обработки фото: {e}")
        await message.reply(
            "❌ Произошла техническая ошибка при анализе.\n"
            "🔄 Пожалуйста, попробуйте позже или обратитесь в поддержку.",
            reply_markup=simple_back_menu()
        )

# === ПРОСТЫЕ CALLBACK ОБРАБОТЧИКИ ===

@dp.callback_query(F.data == "add_plant")
async def add_plant_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "🌱 <b>Добавьте растение в коллекцию</b>\n\n"
        "📸 <b>Пришлите фото вашего растения:</b>\n"
        "• Я определю вид и состояние растения\n"
        "• Дам персональные рекомендации по уходу\n"
        "• Автоматически добавлю в вашу коллекцию\n"
        "• Настрою индивидуальные напоминания о поливе\n\n"
        "💡 <b>Советы для лучшего результата:</b>\n"
        "• Фотографируйте при дневном свете\n"
        "• Покажите листья и общий вид растения\n" 
        "• По возможности включите почву в горшке\n"
        "• Избегайте размытых и тёмных снимков\n"
        "• Можете добавить вопрос в описании к фото",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "analyze")
async def analyze_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "📸 <b>Отправьте фото растения для анализа</b>\n\n"
        "💡 <b>Советы для лучшего результата:</b>\n"
        "• Фотографируйте при дневном свете\n"
        "• Покажите листья и общий вид растения\n" 
        "• По возможности включите почву в горшке\n"
        "• Избегайте размытых и тёмных снимков\n"
        "• Можете добавить вопрос в описании к фото\n\n"
        "🎯 <b>Я проанализирую только то, что видно на фото, и честно укажу ограничения!</b>",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "reanalyze")
async def reanalyze_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "📸 <b>Повторный анализ</b>\n\n"
        "Пришлите новое фото растения для более точного определения:\n\n"
        "🎯 <b>Рекомендации:</b>\n"
        "• Используйте естественное освещение\n"
        "• Сфотографируйте листья крупным планом\n"
        "• Обязательно включите почву в горшке\n"
        "• Покажите характерные особенности растения\n"
        "• Уберите из кадра посторонние предметы\n\n"
        "💡 <b>Чем больше видно - тем точнее анализ!</b>",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "question")
async def question_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "❓ <b>Задайте ваш вопрос о растениях</b>\n\n"
        "💡 <b>Я могу помочь с:</b>\n"
        "• Проблемами с листьями (желтеют, сохнут, опадают)\n"
        "• Режимом полива и подкормки\n" 
        "• Пересадкой и размножением\n"
        "• Болезнями и вредителями\n"
        "• Выбором места для растения\n"
        "• Любыми другими вопросами по уходу",
        parse_mode="HTML"
    )
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()

@dp.callback_query(F.data == "my_plants")
async def my_plants_callback(callback: types.CallbackQuery):
    """Просмотр сохраненных растений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=15)
        
        if not plants:
            await callback.message.answer(
                "🌱 <b>Ваша коллекция пуста</b>\n\n"
                "Нажмите <b>\"🌱 Добавить растение\"</b> в главном меню для:\n"
                "• Точного определения вида\n"
                "• Персональных рекомендаций по уходу\n"
                "• Напоминаний о поливе\n"
                "• Отслеживания состояния здоровья\n\n"
                "Или попробуйте <b>\"🌿 Вырастить с нуля\"</b> для пошагового выращивания!",
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
            
            # Разная обработка для обычных и выращиваемых растений
            if plant['type'] == 'growing':
                stage_info = plant.get('stage_info', 'В процессе')
                text += f"{i}. 🌱 <b>{plant_name}</b>\n"
                text += f"   📅 Начато: {saved_date}\n"
                text += f"   🌿 {stage_info}\n\n"
            else:
                # Статус полива для обычных растений
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
                    elif days_ago <= 3:
                        water_status = f"💧 Полито {days_ago} дня назад"
                    elif days_ago <= 7:
                        water_status = f"🌊 Полито {days_ago} дней назад"
                    else:
                        water_status = f"🌵 Давно не поливали ({days_ago} дней)"
                else:
                    water_status = "🆕 Еще не поливали"
                
                text += f"{i}. 🌱 <b>{plant_name}</b>\n"
                text += f"   📅 Добавлено: {saved_date}\n"
                text += f"   {water_status}\n\n"
            
            # Добавляем кнопку для каждого растения
            short_name = plant_name[:15] + "..." if len(plant_name) > 15 else plant_name
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"⚙️ {short_name}", 
                    callback_data=f"edit_plant_{plant['id']}"
                )
            ])
        
        # Общие кнопки управления
        keyboard_buttons.extend([
            [InlineKeyboardButton(text="💧 Отметить полив всех", callback_data="water_plants")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ])
        
        await callback.message.answer(
            text, 
            parse_mode="HTML", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        )
        
    except Exception as e:
        print(f"Ошибка загрузки растений: {e}")
        await callback.message.answer("❌ Ошибка загрузки коллекции растений.")
    
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def stats_callback(callback: types.CallbackQuery):
    """Статистика пользователя"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        stats = await db.get_user_stats(user_id)
        
        # Формируем красивую статистику
        stats_text = f"📊 <b>Ваша статистика</b>\n\n"
        
        # Обычные растения
        stats_text += f"🌱 <b>Коллекция растений:</b>\n"
        stats_text += f"• Всего растений: {stats['total_plants']}\n"
        stats_text += f"• Политых растений: {stats['watered_plants']}\n"
        stats_text += f"• Общее количество поливов: {stats['total_waterings']}\n"
        stats_text += f"• Растений с напоминаниями: {stats['plants_with_reminders']}\n\n"
        
        # Выращивание
        if stats['total_growing'] > 0:
            stats_text += f"🌿 <b>Выращивание с нуля:</b>\n"
            stats_text += f"• Всего проектов: {stats['total_growing']}\n"
            stats_text += f"• Активных: {stats['active_growing']}\n"
            stats_text += f"• Завершенных: {stats['completed_growing']}\n\n"
        
        # Даты
        if stats['first_plant_date']:
            first_date = stats['first_plant_date'].strftime("%d.%m.%Y")
            stats_text += f"📅 <b>Первое растение:</b> {first_date}\n"
        
        if stats['last_watered_date']:
            last_date = stats['last_watered_date'].strftime("%d.%m.%Y")
            stats_text += f"💧 <b>Последний полив:</b> {last_date}\n"
        
        # Мотивационные сообщения
        if stats['total_plants'] == 0:
            stats_text += f"\n🌱 <b>Начните свой путь садовода!</b>\n"
            stats_text += f"Добавьте первое растение в коллекцию или вырастите с нуля."
        elif stats['total_waterings'] > 50:
            stats_text += f"\n🏆 <b>Отличная работа!</b>\n"
            stats_text += f"Вы настоящий мастер ухода за растениями!"
        elif stats['total_waterings'] > 20:
            stats_text += f"\n👍 <b>Хорошо идете!</b>\n"
            stats_text += f"Ваши растения в надежных руках."
        
        keyboard = [
            [InlineKeyboardButton(text="🌿 К коллекции", callback_data="my_plants")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ]
        
        await callback.message.answer(
            stats_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка загрузки статистики: {e}")
        await callback.message.answer(
            "❌ Ошибка загрузки статистики.\n"
            "Попробуйте позже.",
            reply_markup=simple_back_menu()
        )
    
    # Не вызываем callback.answer() для команд
    if hasattr(callback, 'answer'):
        await callback.answer()

@dp.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    """Справка - callback версия"""
    help_text = """
🌱 <b>Как пользоваться ботом:</b>

🌱 <b>Добавление растения в коллекцию:</b>
• Нажми "🌱 Добавить растение" в главном меню
• Пришли фото растения для анализа и автоматического добавления
• Получи персональные рекомендации и настройки напоминаний

🌿 <b>Выращивание с нуля:</b>
• Нажми "🌿 Вырастить с нуля"
• Напиши название растения которое хочешь вырастить
• Получи персональный план выращивания от ИИ
• Пошаговое сопровождение от посадки до взрослого растения

📸 <b>Анализ растения (без сохранения):</b>
• Пришли фото растения
• Получи полный анализ и рекомендации
• Можешь сохранить результат в коллекцию

⏰ <b>Умные напоминания:</b>
• Ежедневная проверка растений в 9:00 утра (МСК)
• Персональный график для каждого растения
• Быстрая отметка полива из уведомления

🔔 <b>Настройки уведомлений:</b>
• Глобальное включение/выключение всех уведомлений
• Индивидуальные настройки для каждого растения

❓ <b>Вопросы о растениях:</b>
• Просто напиши вопрос в чат
• Получи экспертный совет

🌿 <b>Мои растения:</b>
• Просматривай коллекцию
• Отмечай полив и уход
• Настраивай персональные интервалы

<b>🎯 НОВИНКА - Честный анализ:</b>
• Анализирую только то, что видно на фото
• Честно указываю ограничения анализа
• Рекомендую дополнительные фото для точной диагностики

<b>Для лучшего результата:</b>
• Фотографируй при хорошем освещении
• Покажи листья крупным планом
• Включи почву в горшке для анализа полива
• Сфотографируй обратную сторону листьев при проблемах

<b>Быстрые команды:</b>
/start - Главное меню
/grow - Вырастить с нуля
/add - Добавить растение
/analyze - Анализ растения
/question - Задать вопрос
/plants - Мои растения
/stats - Статистика
/help - Справка
    """
    
    keyboard = [
        [InlineKeyboardButton(text="📝 Обратная связь", callback_data="feedback")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    
    await callback.message.answer(
        help_text, 
        parse_mode="HTML", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(F.data == "notification_settings")
async def notification_settings_callback(callback: types.CallbackQuery):
    """Настройки уведомлений - заглушка"""
    await callback.message.answer(
        "🔔 <b>Настройки уведомлений</b>\n\n"
        "Функция в разработке. Скоро будет доступно:\n"
        "• Глобальное включение/выключение уведомлений\n"
        "• Настройки времени напоминаний\n"
        "• Индивидуальные настройки для каждого растения\n\n"
        "⏰ Сейчас напоминания приходят ежедневно в 9:00 МСК",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "🌱 <b>Главное меню</b>\n\n"
        "Выберите действие:",
        parse_mode="HTML", 
        reply_markup=main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "ask_about")
async def ask_about_callback(callback: types.CallbackQuery, state: FSMContext):
    """Вопрос о проанализированном растении"""
    user_id = callback.from_user.id
    
    if user_id in temp_analyses:
        plant_name = temp_analyses[user_id].get("plant_name", "растении")
        await callback.message.answer(
            f"❓ <b>Вопрос о {plant_name}</b>\n\n"
            f"💡 <b>Популярные вопросы:</b>\n"
            f"• Почему желтеют/сохнут листья?\n"
            f"• Как часто поливать это растение?\n"
            f"• Нужна ли пересадка?\n"
            f"• Почему не растет/не цветёт?\n"
            f"• Как размножить это растение?\n"
            f"• Какие удобрения использовать?\n\n"
            f"✍️ Напишите ваш вопрос:",
            parse_mode="HTML"
        )
        await state.set_state(PlantStates.waiting_question)
    else:
        await callback.message.answer(
            "❌ Данные анализа не найдены.\n"
            "📸 Сначала сфотографируйте растение для анализа."
        )
    
    await callback.answer()

@dp.callback_query(F.data == "water_plants")
async def water_plants_callback(callback: types.CallbackQuery):
    """Отметка полива всех растений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        await db.update_watering(user_id)
        
        current_time = get_moscow_now().strftime("%d.%m.%Y в %H:%M")
        
        await callback.message.answer(
            f"💧 <b>Отлично! Полив отмечен</b>\n\n"
            f"🌱 Все растения в коллекции политы {current_time}\n\n"
            f"📅 <b>Рекомендации по следующему поливу:</b>\n"
            f"• Большинство комнатных растений: 3-7 дней\n"
            f"• Суккуленты и кактусы: 7-14 дней\n"
            f"• Орхидеи: 5-10 дней\n"
            f"• Папоротники: 2-4 дня\n\n"
            f"💡 <b>Помните:</b> Проверяйте влажность почвы пальцем!\n"
            f"🌡️ В жару поливайте чаще, зимой - реже",
            parse_mode="HTML",
            reply_markup=simple_back_menu()
        )
        
    except Exception as e:
        print(f"Ошибка отметки полива: {e}")
        await callback.message.answer("❌ Ошибка отметки полива.")
    
    await callback.answer()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

def format_openai_response(text: str) -> str:
    """Конвертирует Markdown форматирование в HTML для Telegram"""
    if not text:
        return text
    
    # Заменяем **текст** на <b>текст</b>
    import re
    
    # Обрабатываем жирный текст
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # Обрабатываем курсив *текст* на <i>текст</i> 
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    
    # Убираем лишние пробелы и переносы
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)  # Максимум 2 переноса подряд
    
    return text.strip()

# === WEBHOOK И ЗАПУСК ===

async def on_startup():
    """Инициализация при запуске"""
    await init_database()
    
    # Запускаем планировщик напоминаний
    scheduler.add_job(
        check_and_send_reminders,
        'cron',
        hour=9,     # Каждый день в 9:00 утра по московскому времени (UTC+3)
        minute=0,
        id='reminder_check',
        replace_existing=True
    )
    scheduler.start()
    print("🔔 Планировщик напоминаний запущен (ежедневно в 9:00 МСК)")
    
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        print(f"Webhook установлен: {WEBHOOK_URL}/webhook")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Webhook удален, используется polling")

async def on_shutdown():
    """Очистка при завершении"""
    if scheduler.running:
        scheduler.shutdown()
        print("🔔 Планировщик остановлен")
        
    try:
        db = await get_db()
        await db.close()
    except Exception as e:
        print(f"Ошибка закрытия БД: {e}")
    
    try:
        await bot.session.close()
    except Exception as e:
        print(f"Ошибка закрытия сессии бота: {e}")

async def webhook_handler(request):
    """Обработчик webhook запросов"""
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
        print(f"Ошибка webhook: {e}")
        return web.Response(status=500)

async def health_check(request):
    """Проверка здоровья сервиса"""
    return web.json_response({
        "status": "healthy", 
        "bot": "Bloom AI Plant Care Assistant", 
        "version": "3.4",
        "features": [
            "smart_plant_analysis", 
            "honest_limitations_reporting", 
            "visible_elements_focus", 
            "smart_reminders", 
            "easy_plant_adding", 
            "grow_from_scratch",
            "growth_diary",
            "stage_tracking",
            "user_feedback_system"
        ],
        "analysis_approach": "visible_elements_with_honest_limitations",
        "feedback_types": ["bug", "analysis_error", "suggestion", "review"],
        "reminder_schedule": "daily_at_09:00_MSK_UTC+3"
    })

async def main():
    """Основная функция запуска бота"""
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
        
        print(f"🚀 Bloom AI Plant Bot v3.4 запущен на порту {PORT}")
        print(f"🎯 Умный анализ растений с честным подходом!")
        print(f"🌿 Функция выращивания с нуля активна!")
        print(f"📝 Дневник роста и отслеживание этапов!")
        print(f"💬 Система обратной связи работает!")
        print(f"⏰ Умные напоминания активны (МСК UTC+3)!")
        
        try:
            await asyncio.Future()
        except KeyboardInterrupt:
            print("🛑 Получен сигнал остановки")
        finally:
            await runner.cleanup()
            await on_shutdown()
    else:
        print("🤖 Бот запущен в режиме polling")
        print(f"🎯 Умный анализ растений с честным подходом!")
        print(f"🌿 Функция выращивания с нуля активна!")
        print(f"📝 Дневник роста и отслеживание этапов!")
        print(f"💬 Система обратной связи работает!")
        print(f"⏰ Умные напоминания активны (МСК UTC+3)!")
        try:
            await dp.start_polling(bot, drop_pending_updates=True)
        except KeyboardInterrupt:
            print("🛑 Получен сигнал остановки")
        finally:
            await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
    except KeyboardInterrupt:
        print("🛑 Принудительная остановка")
