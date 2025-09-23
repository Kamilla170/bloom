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

# База знаний для распознавания растений по характеристикам
PLANT_IDENTIFICATION_PROMPT = """
Вы - эксперт-ботаник. Внимательно изучите фотографию растения и дайте максимально точную идентификацию.

Анализируйте:
1. Форму и текстуру листьев (овальные/длинные/мясистые/глянцевые/матовые)
2. Расположение листьев на стебле
3. Цвет и прожилки листьев
4. Форму роста растения
5. Видимые цветы или плоды
6. Размер растения и горшка

ОСОБОЕ ВНИМАНИЕ К СОСТОЯНИЮ ПОЛИВА:
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

СОСТОЯНИЕ: [детальная оценка здоровья по листьям, цвету, упругости]
ПОЛИВ_АНАЛИЗ: [подробный анализ текущего состояния полива - переувлажнено/недополито/норма]
ПОЛИВ_РЕКОМЕНДАЦИИ: [конкретные рекомендации по частоте и объему полива именно для ЭТОГО экземпляра]
ПОЛИВ_ИНТЕРВАЛ: [рекомендуемый интервал в днях для ЭТОГО конкретного растения: 2-15]
СВЕТ: [точные требования к освещению для данного растения]
ТЕМПЕРАТУРА: [оптимальный диапазон для этого вида]
ВЛАЖНОСТЬ: [требования к влажности воздуха]
ПОДКОРМКА: [рекомендации по удобрениям]
ПЕРЕСАДКА: [когда и как пересаживать этот вид]

ПРОБЛЕМЫ: [возможные болезни и вредители характерные для этого вида]
СОВЕТ: [специфический совет для улучшения ухода за этим конкретным растением]

Будьте максимально точными и конкретными в анализе полива. Если не можете точно определить вид, укажите хотя бы род или семейство.
"""

# Состояния
class PlantStates(StatesGroup):
    waiting_question = State()
    editing_plant_name = State()

# Функция для получения текущего московского времени
def get_moscow_now():
    """Получить текущее время в московской зоне"""
    return datetime.now(MOSCOW_TZ)

def get_moscow_date():
    """Получить текущую дату в московской зоне"""
    return get_moscow_now().date()

# === СИСТЕМА НАПОМИНАНИЙ ===

async def check_and_send_reminders():
    """Проверка и отправка напоминаний о поливе (ежедневно утром)"""
    try:
        db = await get_db()
        
        # Получаем текущее московское время
        moscow_now = get_moscow_now()
        moscow_date = moscow_now.date()
        
        # Получаем растения, которые нужно полить
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
                
    except Exception as e:
        print(f"Ошибка проверки напоминаний: {e}")

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
        
        await db.create_reminder(
            user_id=user_id,
            plant_id=plant_id,
            reminder_type='watering',
            next_date=next_watering
        )
        
    except Exception as e:
        print(f"Ошибка создания напоминания: {e}")

# === ОБНОВЛЕННЫЕ CALLBACK ОБРАБОТЧИКИ ===

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

# Обновленная функция сохранения растения
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
                parse_mode="HTML",
                reply_markup=main_menu()
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

# === ОСТАЛЬНЫЕ ФУНКЦИИ (без изменений) ===

# Клавиатуры
def main_menu():
    keyboard = [
        [InlineKeyboardButton(text="🌱 Добавить растение", callback_data="add_plant")],
        [InlineKeyboardButton(text="📸 Анализ растения", callback_data="analyze")],
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="question")],
        [InlineKeyboardButton(text="🌿 Мои растения", callback_data="my_plants")],
        [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="notification_settings")],
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

# Функция для извлечения персональных рекомендаций по поливу
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
        
        # Извлекаем персональный интервал
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
        
        # Извлекаем анализ текущего состояния
        elif line.startswith("ПОЛИВ_АНАЛИЗ:"):
            current_state = line.replace("ПОЛИВ_АНАЛИЗ:", "").strip()
            watering_info["current_state"] = current_state
            # Проверяем, нужна ли корректировка
            if any(word in current_state.lower() for word in ["переувлажн", "перелив", "недополит", "пересушен", "проблем"]):
                watering_info["needs_adjustment"] = True
        
        # Извлекаем персональные рекомендации
        elif line.startswith("ПОЛИВ_РЕКОМЕНДАЦИИ:"):
            recommendations = line.replace("ПОЛИВ_РЕКОМЕНДАЦИИ:", "").strip()
            watering_info["personal_recommendations"] = recommendations
            
    return watering_info

# Улучшенное форматирование анализа
def format_plant_analysis(raw_text: str, confidence: float = None) -> str:
    """Форматирование детального анализа растения"""
    
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
                
        elif line.startswith("ПРИЗНАКИ:"):
            signs = line.replace("ПРИЗНАКИ:", "").strip()
            formatted += f"🔍 <b>Признаки:</b> {signs}\n"
            
        elif line.startswith("СЕМЕЙСТВО:"):
            family = line.replace("СЕМЕЙСТВО:", "").strip()
            formatted += f"👨‍👩‍👧‍👦 <b>Семейство:</b> {family}\n"
            
        elif line.startswith("РОДИНА:"):
            origin = line.replace("РОДИНА:", "").strip()
            formatted += f"🌍 <b>Родина:</b> {origin}\n\n"
            
        elif line.startswith("СОСТОЯНИЕ:"):
            condition = line.replace("СОСТОЯНИЕ:", "").strip()
            if any(word in condition.lower() for word in ["здоров", "хорош", "отличн", "норм"]):
                icon = "✅"
            elif any(word in condition.lower() for word in ["проблем", "болен", "плох", "стресс"]):
                icon = "⚠️"
            else:
                icon = "ℹ️"
            formatted += f"{icon} <b>Состояние:</b> {condition}\n"
            
        elif line.startswith("ПОЛИВ_АНАЛИЗ:"):
            watering_analysis = line.replace("ПОЛИВ_АНАЛИЗ:", "").strip()
            if any(word in watering_analysis.lower() for word in ["переувлажн", "перелив"]):
                icon = "🔴"
            elif any(word in watering_analysis.lower() for word in ["недополит", "пересушен"]):
                icon = "🟡"
            else:
                icon = "🟢"
            formatted += f"{icon} <b>Анализ полива:</b> {watering_analysis}\n"
            
        elif line.startswith("ПОЛИВ_РЕКОМЕНДАЦИИ:"):
            watering_rec = line.replace("ПОЛИВ_РЕКОМЕНДАЦИИ:", "").strip()
            formatted += f"💧 <b>Персональные рекомендации по поливу:</b> {watering_rec}\n"
            
        elif line.startswith("ПОЛИВ_ИНТЕРВАЛ:"):
            interval = line.replace("ПОЛИВ_ИНТЕРВАЛ:", "").strip()
            formatted += f"⏰ <b>Рекомендуемый интервал:</b> {interval} дней\n\n"
            
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
            
        elif line.startswith("ПЕРЕСАДКА:"):
            repot = line.replace("ПЕРЕСАДКА:", "").strip()
            formatted += f"🪴 <b>Пересадка:</b> {repot}\n"
            
        elif line.startswith("ПРОБЛЕМЫ:"):
            problems = line.replace("ПРОБЛЕМЫ:", "").strip()
            formatted += f"\n⚠️ <b>Возможные проблемы:</b> {problems}\n"
            
        elif line.startswith("СОВЕТ:"):
            advice = line.replace("СОВЕТ:", "").strip()
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
    """Продвинутый анализ через OpenAI GPT-4 Vision"""
    if not openai_client:
        return {"success": False, "error": "OpenAI API недоступен"}
    
    try:
        optimized_image = await optimize_image_for_analysis(image_data, high_quality=True)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        prompt = PLANT_IDENTIFICATION_PROMPT
        
        if user_question:
            prompt += f"\n\nДополнительно ответьте на вопрос пользователя: {user_question}"
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",  # Используем последнюю модель
            messages=[
                {
                    "role": "system",
                    "content": "Вы - ведущий эксперт-ботаник с 30-летним опытом идентификации комнатных и садовых растений. Вы способны точно определять виды растений по фотографиям и давать профессиональные рекомендации по уходу."
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "high"  # Высокое качество анализа
                            }
                        }
                    ]
                }
            ],
            max_tokens=1200,
            temperature=0.1  # Низкая температура для более точных ответов
        )
        
        raw_analysis = response.choices[0].message.content
        
        # Проверяем качество ответа
        if len(raw_analysis) < 100 or "не могу" in raw_analysis.lower() or "sorry" in raw_analysis.lower():
            raise Exception("Некачественный ответ от OpenAI")
        
        # Извлекаем уверенность из ответа
        confidence = 0
        for line in raw_analysis.split('\n'):
            if line.startswith("УВЕРЕННОСТЬ:"):
                try:
                    conf_str = line.replace("УВЕРЕННОСТЬ:", "").strip().replace("%", "")
                    confidence = float(conf_str)
                except:
                    confidence = 70  # По умолчанию
                break
        
        # Извлекаем название растения
        plant_name = "Неизвестное растение"
        for line in raw_analysis.split('\n'):
            if line.startswith("РАСТЕНИЕ:"):
                plant_name = line.replace("РАСТЕНИЕ:", "").strip()
                break
        
        formatted_analysis = format_plant_analysis(raw_analysis, confidence)
        
        return {
            "success": True,
            "analysis": formatted_analysis,
            "raw_analysis": raw_analysis,
            "plant_name": plant_name,
            "confidence": confidence,
            "source": "openai_advanced"
        }
        
    except Exception as e:
        print(f"OpenAI Advanced API error: {e}")
        return {"success": False, "error": str(e)}

# Улучшенный анализ через Plant.id
async def analyze_with_plantid_advanced(image_data: bytes) -> dict:
    """Продвинутый анализ через Plant.id API"""
    if not PLANTID_API_KEY:
        return {"success": False, "error": "Plant.id API недоступен"}
    
    try:
        import httpx
        
        optimized_image = await optimize_image_for_analysis(image_data, high_quality=True)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        # Более детальный запрос к Plant.id
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                "https://api.plant.id/v2/identify",
                json={
                    "images": [f"data:image/jpeg;base64,{base64_image}"],
                    "modifiers": [
                        "crops_fast", 
                        "similar_images", 
                        "health_assessment",
                        "disease_similar_images"
                    ],
                    "plant_language": "ru",
                    "plant_net": "auto",
                    "plant_details": [
                        "common_names",
                        "url", 
                        "description",
                        "taxonomy",
                        "rank",
                        "gbif_id",
                        "inaturalist_id",
                        "image",
                        "synonyms",
                        "edible_parts",
                        "watering",
                        "propagation_methods"
                    ]
                },
                headers={
                    "Content-Type": "application/json",
                    "Api-Key": PLANTID_API_KEY
                }
            )
        
        if response.status_code != 200:
            return {"success": False, "error": f"Plant.id API error: {response.status_code}"}
        
        data = response.json()
        
        if not data.get("suggestions") or len(data["suggestions"]) == 0:
            return {"success": False, "error": "Растение не распознано"}
        
        # Берем лучший результат
        suggestion = data["suggestions"][0]
        plant_details = suggestion.get("plant_details", {})
        
        # Формируем детальный анализ
        plant_name = suggestion.get("plant_name", "Неизвестное растение")
        probability = suggestion.get("probability", 0) * 100
        
        # Получаем общие названия
        common_names = plant_details.get("common_names", {})
        russian_names = common_names.get("ru", [])
        if russian_names:
            display_name = russian_names[0]
        else:
            display_name = plant_name
        
        # Таксономия
        taxonomy = plant_details.get("taxonomy", {})
        family = taxonomy.get("family", "")
        
        # Оценка здоровья
        health_info = "Требуется визуальная оценка"
        if data.get("health_assessment"):
            health = data["health_assessment"]
            if health.get("is_healthy"):
                health_prob = health["is_healthy"]["probability"]
                if health_prob > 0.8:
                    health_info = f"Растение выглядит здоровым ({health_prob*100:.0f}% уверенности)"
                elif health_prob > 0.5:
                    health_info = f"Возможны незначительные проблемы ({health_prob*100:.0f}% здоровья)"
                else:
                    health_info = f"Обнаружены проблемы со здоровьем ({health_prob*100:.0f}% здоровья)"
                    
                # Проверяем болезни
                if health.get("diseases"):
                    diseases = health["diseases"]
                    if diseases:
                        top_disease = diseases[0]
                        disease_name = top_disease.get("name", "неизвестная проблема")
                        disease_prob = top_disease.get("probability", 0) * 100
                        if disease_prob > 30:
                            health_info += f". Возможна проблема: {disease_name} ({disease_prob:.0f}%)"
        
        # Формируем специализированные рекомендации на основе Plant.id данных
        watering_info = plant_details.get("watering", {})
        personal_watering_rec = "Следуйте персональному режиму полива для этого экземпляра"
        watering_analysis = "Состояние полива требует визуальной оценки"
        
        # Пытаемся определить интервал на основе типа растения
        if "succulent" in plant_name.lower() or "cactus" in plant_name.lower():
            watering_interval = 10
            personal_watering_rec = "Как суккулент, поливайте редко но обильно, когда почва полностью высохнет"
        elif any(word in display_name.lower() for word in ["папоротник", "спатифиллум", "фиалка"]):
            watering_interval = 3
            personal_watering_rec = "Как влаголюбивое растение, поддерживайте постоянную умеренную влажность почвы"
        else:
            watering_interval = 5
            personal_watering_rec = "Поливайте когда верхний слой почвы подсохнет на 2-3 см"
        
        # Создаем детальный анализ
        analysis_text = f"""
РАСТЕНИЕ: {display_name} ({plant_name})
УВЕРЕННОСТЬ: {probability:.0f}%
ПРИЗНАКИ: Идентифицировано по форме листьев, характеру роста и морфологическим особенностям
СЕМЕЙСТВО: {family if family else 'Не определено'}
РОДИНА: {plant_details.get('description', {}).get('value', 'Информация недоступна')[:100] + '...' if plant_details.get('description', {}).get('value') else 'Не определено'}

СОСТОЯНИЕ: {health_info}
ПОЛИВ_АНАЛИЗ: {watering_analysis}
ПОЛИВ_РЕКОМЕНДАЦИИ: {personal_watering_rec}
ПОЛИВ_ИНТЕРВАЛ: {watering_interval}
СВЕТ: Подберите освещение согласно требованиям данного вида
ТЕМПЕРАТУРА: 18-24°C (уточните для конкретного вида)
ВЛАЖНОСТЬ: Умеренная влажность воздуха 40-60%
ПОДКОРМКА: В период роста каждые 2-4 недели комплексным удобрением
ПЕРЕСАДКА: Молодые растения ежегодно, взрослые - каждые 2-3 года

ПРОБЛЕМЫ: {disease_name if 'disease_name' in locals() else 'Следите за типичными для данного вида вредителями и болезнями'}
СОВЕТ: Изучите конкретные потребности {display_name} для оптимального ухода - это поможет растению полноценно развиваться
        """.strip()
        
        formatted_analysis = format_plant_analysis(analysis_text, probability)
        
        return {
            "success": True,
            "analysis": formatted_analysis,
            "raw_analysis": analysis_text,
            "plant_name": display_name,
            "confidence": probability,
            "source": "plantid_advanced",
            "plant_details": plant_details
        }
        
    except Exception as e:
        print(f"Plant.id Advanced API error: {e}")
        return {"success": False, "error": str(e)}

# Основная функция анализа с умным fallback
async def analyze_plant_image(image_data: bytes, user_question: str = None, retry_count: int = 0) -> dict:
    """Интеллектуальный анализ изображения растения"""
    
    # Попытка 1: OpenAI GPT-4 Vision (приоритет)
    print("🔍 Попытка анализа через OpenAI GPT-4 Vision...")
    openai_result = await analyze_with_openai_advanced(image_data, user_question)
    
    if openai_result["success"] and openai_result.get("confidence", 0) >= 60:
        print(f"✅ OpenAI успешно распознал растение с {openai_result.get('confidence')}% уверенностью")
        return openai_result
    
    # Попытка 2: Plant.id API 
    print("🌿 Попытка анализа через Plant.id...")
    plantid_result = await analyze_with_plantid_advanced(image_data)
    
    if plantid_result["success"] and plantid_result.get("confidence", 0) >= 50:
        print(f"✅ Plant.id успешно распознал растение с {plantid_result.get('confidence')}% уверенностью")
        return plantid_result
    
    # Попытка 3: Комбинированный подход - используем лучший из результатов
    best_result = None
    best_confidence = 0
    
    if openai_result["success"]:
        openai_conf = openai_result.get("confidence", 0)
        if openai_conf > best_confidence:
            best_result = openai_result
            best_confidence = openai_conf
    
    if plantid_result["success"]:
        plantid_conf = plantid_result.get("confidence", 0)
        if plantid_conf > best_confidence:
            best_result = plantid_result  
            best_confidence = plantid_conf
    
    if best_result and best_confidence > 30:
        print(f"📊 Использую лучший результат с {best_confidence}% уверенностью")
        return best_result
    
    # Повторная попытка с измененными параметрами (если еще не пробовали)
    if retry_count == 0:
        print("🔄 Повторная попытка анализа...")
        return await analyze_plant_image(image_data, user_question, retry_count + 1)
    
    # Fallback с указанием проблемы
    print("⚠️ Все методы анализа не дали уверенного результата")
    
    fallback_text = """
РАСТЕНИЕ: Комнатное растение (требуется дополнительная идентификация)
УВЕРЕННОСТЬ: Низкая - рекомендуется повторная фотография
ПРИЗНАКИ: Недостаточно данных для точной идентификации
СЕМЕЙСТВО: Не определено
РОДИНА: Не определено

СОСТОЯНИЕ: Требуется визуальный осмотр листьев, стебля и корневой системы
ПОЛИВ_АНАЛИЗ: Невозможно оценить состояние полива без качественного фото
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
    
    formatted_analysis = format_plant_analysis(fallback_text, 25)
    
    return {
        "success": True,
        "analysis": formatted_analysis,
        "raw_analysis": fallback_text,
        "plant_name": "Неопознанное растение",
        "confidence": 25,
        "source": "fallback_improved",
        "needs_retry": True
    }

@dp.message(Command("notifications"))
async def notifications_command(message: types.Message):
    """Команда /notifications - быстрый доступ к настройкам уведомлений"""
    # Вызываем тот же обработчик что и кнопка
    callback_query = types.CallbackQuery(
        id="cmd_notifications",
        from_user=message.from_user,
        chat_instance="",
        message=message,
        data="notification_settings"
    )
    await notification_settings_callback(callback_query)

@dp.message(Command("plants"))
async def plants_command(message: types.Message):
    """Команда /plants - быстрый доступ к коллекции"""
    callback_query = types.CallbackQuery(
        id="cmd_plants",
        from_user=message.from_user,
        chat_instance="",
        message=message,
        data="my_plants"
    )
    await my_plants_callback(callback_query)

@dp.message(Command("add"))
async def add_command(message: types.Message):
    """Команда /add - быстрый доступ к добавлению растения"""
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
        "• Избегайте размытых и тёмных снимков\n"
        "• Можете добавить вопрос в описании к фото",
        parse_mode="HTML"
    )

@dp.message(Command("analyze"))
async def analyze_command(message: types.Message):
    """Команда /analyze - быстрый доступ к анализу"""
    await message.answer(
        "📸 <b>Отправьте фото растения для анализа</b>\n\n"
        "💡 <b>Советы для лучшего результата:</b>\n"
        "• Фотографируйте при дневном свете\n"
        "• Покажите листья и общий вид растения\n" 
        "• Избегайте размытых и тёмных снимков\n"
        "• Можете добавить вопрос в описании к фото",
        parse_mode="HTML"
    )

@dp.message(Command("question"))
async def question_command(message: types.Message, state: FSMContext):
    """Команда /question - быстрый доступ к вопросам"""
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

@dp.message(Command("stats"))
async def stats_command(message: types.Message):
    """Команда /stats - быстрый доступ к статистике"""
    callback_query = types.CallbackQuery(
        id="cmd_stats",
        from_user=message.from_user,
        chat_instance="",
        message=message,
        data="stats"
    )
    await stats_callback(callback_query)

# Обработчики команд
@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Команда /start"""
    user_id = message.from_user.id
    
    try:
        db = await get_db()
        await db.add_user(
            user_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name
        )
    except Exception as e:
        print(f"Ошибка добавления пользователя: {e}")
    
    await message.answer(
        f"🌱 Привет, {message.from_user.first_name}!\n\n"
        "Я умный помощник по уходу за растениями:\n"
        "🌱 Простое добавление растений в коллекцию\n"
        "📸 Точное распознавание видов растений\n"
        "💡 Персонализированные рекомендации по уходу\n"
        "❓ Ответы на вопросы о растениях\n"
        "⏰ Умные напоминания о поливе для каждого растения\n"
        "🔔 Гибкие настройки уведомлений\n\n"
        "Начните с кнопки <b>\"🌱 Добавить растение\"</b>!",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Команда /help"""
    help_text = """
🌱 <b>Как пользоваться ботом:</b>

🌱 <b>Добавление растения в коллекцию:</b>
• Нажми "🌱 Добавить растение" в главном меню
• Или используй команду /add
• Пришли фото растения для анализа и автоматического добавления
• Получи персональные рекомендации и настройки напоминаний

📸 <b>Анализ растения (без сохранения):</b>
• Пришли фото растения или используй /analyze
• Получи полный анализ и рекомендации
• Можешь сохранить результат в коллекцию

⏰ <b>Умные напоминания:</b>
• Ежедневная проверка растений в 9:00 утра (МСК)
• Персональный график для каждого растения
• Быстрая отметка полива из уведомления

🔔 <b>Настройки уведомлений:</b>
• Глобальное включение/выключение всех уведомлений
• Индивидуальные настройки для каждого растения
• Массовое управление уведомлениями коллекции

❓ <b>Вопросы о растениях:</b>
• Просто напиши вопрос в чат
• Или используй команду /question
• Получи экспертный совет

🌿 <b>Мои растения:</b>
• Команда /plants - просмотр коллекции
• Отмечай полив и уход
• Настраивай персональные интервалы
• Полные карточки растений с историей

📊 <b>Статистика:</b>
• Команда /stats - подробная статистика
• Отслеживай прогресс ухода

<b>Для лучшего результата:</b>
• Фотографируй при хорошем освещении
• Покажи листья крупным планом
• Включи в кадр всё растение целиком

<b>Доступные команды в меню:</b>
/start - главное меню
/add - добавить растение  
/analyze - анализ растения
/question - задать вопрос
/plants - мои растения  
/notifications - настройки уведомлений
/stats - статистика
/help - эта справка

💡 <b>Быстрый доступ через меню команд!</b>
    """
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_menu())

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

# Обработка всех текстовых сообщений (кроме команд и состояний)
@dp.message(F.text, ~StateFilter(PlantStates.waiting_question, PlantStates.editing_plant_name))
async def handle_text_message(message: types.Message):
    """Обработка произвольных текстовых сообщений"""
    try:
        text = message.text.strip()
        
        # Пропускаем команды
        if text.startswith('/'):
            return
        
        # Проверяем, связан ли текст с растениями и безопасен ли он
        is_safe_plant_topic, reason = is_plant_related_and_safe(text)
        
        if reason == "illegal":
            await message.reply(
                "⚠️ Извините, я не могу предоставить информацию о таких растениях.\n\n"
                "🌱 Я помогаю только с комнатными, садовыми и декоративными растениями!\n"
                "📸 Пришлите фото своего домашнего растения для анализа.",
                reply_markup=main_menu()
            )
            return
        
        if not is_safe_plant_topic:
            await message.reply(
                "🌱 Я специализируюсь только на вопросах о растениях!\n\n"
                "💡 <b>Могу помочь с:</b>\n"
                "• Уходом за комнатными растениями\n"
                "• Проблемами с листьями и цветением\n"
                "• Поливом и подкормкой\n"
                "• Болезнями и вредителями\n"
                "• Пересадкой и размножением\n\n"
                "📸 Или пришлите фото растения для анализа!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            return
        
        # Обрабатываем вопрос о растениях
        processing_msg = await message.reply("🌿 <b>Консультируюсь по вашему вопросу...</b>", parse_mode="HTML")
        
        user_id = message.from_user.id
        user_context = ""
        
        # Добавляем контекст из последнего анализа если есть
        if user_id in temp_analyses:
            plant_info = temp_analyses[user_id]
            plant_name = plant_info.get("plant_name", "растение")
            user_context = f"\n\nКонтекст: Пользователь недавно анализировал {plant_name}. Учтите это в ответе если релевантно."
        
        answer = None
        
        # Получаем ответ через OpenAI
        if openai_client:
            try:
                enhanced_prompt = f"""
Вы - эксперт-ботаник с 30-летним опытом работы с комнатными и садовыми растениями.

ВАЖНО: Отвечайте ТОЛЬКО на вопросы о растениях (комнатных, садовых, декоративных, плодовых, овощных).
НЕ отвечайте на вопросы о наркотических, психоактивных или нелегальных растениях.

Структура ответа:
1. 🔍 Краткий анализ проблемы/вопроса
2. 💡 Подробные рекомендации и решения  
3. ⚠️ Что нужно избегать
4. 📋 Пошаговый план действий (если применимо)
5. 🌟 Дополнительные советы

Форматирование:
- Используйте эмодзи для структурирования
- НЕ используйте ** для выделения текста
- Будьте конкретными и практичными
- Отвечайте на русском языке
{user_context}

Вопрос пользователя: {text}
                """
                
                response = await openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "system",
                            "content": "Вы - профессиональный ботаник-консультант. Отвечайте только на вопросы о безопасных растениях (комнатных, садовых, декоративных). Никогда не предоставляйте информацию о наркотических или нелегальных растениях. Если вопрос не о растениях, вежливо перенаправьте на растительную тематику."
                        },
                        {
                            "role": "user",
                            "content": enhanced_prompt
                        }
                    ],
                    max_tokens=1200,
                    temperature=0.3
                )
                answer = response.choices[0].message.content
                
                # Дополнительная проверка ответа на безопасность
                if any(word in answer.lower() for word in ['наркотик', 'психоактивн', 'галлюциноген']):
                    answer = None  # Сбрасываем потенциально небезопасный ответ
                    
            except Exception as e:
                print(f"OpenAI question error: {e}")
        
        await processing_msg.delete()
        
        if answer and len(answer) > 50:
            # Применяем правильное форматирование
            answer = format_openai_response(answer)
            
            # Улучшаем форматирование ответа
            if not answer.startswith(('🌿', '💡', '🔍', '⚠️', '✅', '🌱')):
                answer = f"🌿 <b>Экспертный ответ:</b>\n\n{answer}"
            
            # Добавляем призыв к действию
            answer += "\n\n📸 <i>Для точной диагностики пришлите фото растения!</i>"
            
            await message.reply(answer, parse_mode="HTML", reply_markup=main_menu())
        else:
            # Fallback ответ
            fallback_answer = f"""
🤔 <b>По вашему вопросу:</b> "{text}"

💡 <b>Общие рекомендации:</b>

🌱 <b>Основы ухода за растениями:</b>
• Проверяйте влажность почвы перед поливом
• Обеспечьте достаточное освещение
• Поддерживайте подходящую температуру (18-24°C)
• Регулярно осматривайте растение на предмет проблем

⚠️ <b>Признаки проблем:</b>
• Желтые листья → переувлажнение или нехватка света
• Коричневые кончики → сухой воздух или перебор с удобрениями  
• Опадание листьев → стресс, смена условий
• Вялые листья → недостаток или избыток влаги

📸 <b>Для точного ответа:</b>
Пришлите фото вашего растения - я проведу детальный анализ и дам персональные рекомендации!

🆘 <b>Экстренные случаи:</b>
При серьезных проблемах обратитесь в садовый центр или к специалисту-ботанику.
            """
            
            await message.reply(fallback_answer, parse_mode="HTML", reply_markup=main_menu())
        
    except Exception as e:
        print(f"Ошибка обработки текстового сообщения: {e}")
        await message.reply(
            "❌ Произошла ошибка при обработке вашего сообщения.\n"
            "🔄 Попробуйте переформулировать вопрос или пришлите фото растения.", 
            reply_markup=main_menu()
        )

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
            
            await message.reply(answer, parse_mode="HTML", reply_markup=main_menu())
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
            
            await message.reply(fallback_answer, parse_mode="HTML", reply_markup=main_menu())
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка ответа на вопрос: {e}")
        await message.reply(
            "❌ Произошла ошибка при обработке вопроса.\n"
            "🔄 Попробуйте переформулировать или задать вопрос позже.", 
            reply_markup=main_menu()
        )
        await state.clear()
# Улучшенная обработка фотографий
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработка фотографий растений с улучшенным анализом"""
    try:
        # Показываем прогресс
        processing_msg = await message.reply(
            "🔍 <b>Анализирую ваше растение...</b>\n"
            "⏳ Определяю вид и состояние растения\n"
            "🧠 Готовлю персональные рекомендации",
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
            "📊 Оцениваю состояние здоровья",
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
                f"• Повторить попытку через минуту",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            
    except Exception as e:
        print(f"Ошибка обработки фото: {e}")
        await message.reply(
            "❌ Произошла техническая ошибка при анализе.\n"
            "🔄 Пожалуйста, попробуйте позже или обратитесь в поддержку.",
            reply_markup=main_menu()
        )

# Callback обработчики
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
        "• Избегайте размытых и тёмных снимков\n"
        "• Можете добавить вопрос в описании к фото",
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
        "• Покажите характерные особенности растения\n"
        "• Уберите из кадра посторонние предметы",
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
    """Просмотр сохраненных растений с возможностью редактирования"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=10)
        
        if not plants:
            await callback.message.answer(
                "🌱 <b>Ваша коллекция пуста</b>\n\n"
                "Нажмите <b>\"🌱 Добавить растение\"</b> в главном меню для:\n"
                "• Точного определения вида\n"
                "• Персональных рекомендаций по уходу\n"
                "• Напоминаний о поливе\n"
                "• Отслеживания состояния здоровья\n\n"
                "Начните создавать свой цифровой сад!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            await callback.answer()
            return
        
        text = f"🌿 <b>Ваша коллекция ({len(plants)} растений):</b>\n\n"
        
        keyboard_buttons = []
        
        for i, plant in enumerate(plants, 1):
            # Используем display_name из БД
            plant_name = plant['display_name']
            saved_date = plant["saved_date"].strftime("%d.%m.%Y")
            
            # Статус полива (по московскому времени)
            moscow_now = get_moscow_now()
            
            if plant["last_watered"]:
                # Конвертируем UTC время из БД в московское
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
            
            # Добавляем кнопку редактирования для каждого растения
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
            [InlineKeyboardButton(text="📊 Подробная статистика", callback_data="stats")],
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

# Обработчики редактирования растений
@dp.callback_query(F.data.startswith("edit_plant_"))
async def edit_plant_callback(callback: types.CallbackQuery):
    """Показать меню редактирования растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = plant['display_name']
        saved_date = plant["saved_date"].strftime("%d.%m.%Y")
        watered_count = plant.get('watering_count', 0)
        
        # Статус полива
        if plant["last_watered"]:
            last_watered = plant["last_watered"].strftime("%d.%m.%Y")
            water_info = f"💧 Последний полив: {last_watered} (всего: {watered_count} раз)"
        else:
            water_info = "🆕 Еще не поливали"
        
        info_text = f"⚙️ <b>Управление растением</b>\n\n"
        info_text += f"🌱 <b>{plant_name}</b>\n\n"
        info_text += f"📅 Добавлено: {saved_date}\n"
        info_text += f"{water_info}\n"
        
        # Добавляем интервал полива
        interval = plant.get('watering_interval', 5)
        info_text += f"⏰ Интервал полива: каждые {interval} дней\n"
        
        # Показываем персональные рекомендации если есть
        if plant.get('notes'):
            notes = plant['notes']
            if "Персональные рекомендации по поливу:" in notes:
                personal_rec = notes.replace("Персональные рекомендации по поливу:", "").strip()
                info_text += f"\n💡 <b>Персональные рекомендации:</b>\n{personal_rec}\n"
            else:
                info_text += f"\n📝 Заметки: {notes}\n"
        
        # Показываем статус уведомлений
        reminder_status = "🔔 включены" if plant.get('reminder_enabled', True) else "🔕 выключены"
        info_text += f"\n🔔 Уведомления: {reminder_status}"
        
        # Кнопка для переключения уведомлений
        reminder_enabled = plant.get('reminder_enabled', True)
        reminder_button_text = "🔕 Выключить уведомления" if reminder_enabled else "🔔 Включить уведомления"
        
        keyboard = [
            [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"rename_plant_{plant_id}")],
            [InlineKeyboardButton(text="💧 Отметить полив", callback_data=f"water_plant_{plant_id}")],
            [InlineKeyboardButton(text=reminder_button_text, callback_data=f"toggle_reminder_{plant_id}")],
            [InlineKeyboardButton(text="📷 Показать фото", callback_data=f"show_photo_{plant_id}")],
            [InlineKeyboardButton(text="📋 Полный анализ", callback_data=f"show_analysis_{plant_id}")],
            [InlineKeyboardButton(text="🗑️ Удалить растение", callback_data=f"delete_plant_{plant_id}")],
            [InlineKeyboardButton(text="🔙 К коллекции", callback_data="my_plants")],
        ]
        
        await callback.message.answer(
            info_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка редактирования растения: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("rename_plant_"))
async def rename_plant_callback(callback: types.CallbackQuery, state: FSMContext):
    """Начать процесс переименования растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        # Сохраняем ID растения в состояние
        await state.update_data(editing_plant_id=plant_id)
        await state.set_state(PlantStates.editing_plant_name)
        
        current_name = plant['display_name']
        
        await callback.message.answer(
            f"✏️ <b>Переименование растения</b>\n\n"
            f"📝 Текущее название: <b>{current_name}</b>\n\n"
            f"💬 Введите новое название растения:\n"
            f"<i>(от 2 до 50 символов)</i>",
            parse_mode="HTML"
        )
        
    except Exception as e:
        print(f"Ошибка начала переименования: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("show_analysis_"))
async def show_plant_analysis_callback(callback: types.CallbackQuery):
    """Показать полную карточку растения с актуальной информацией"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = plant['display_name']
        analysis_text = plant.get('analysis', '')
        
        # Извлекаем информацию из анализа
        plant_info = extract_plant_info_from_analysis(analysis_text)
        
        # Вычисляем актуальную информацию (по московскому времени)
        moscow_now = get_moscow_now()
        added_date = plant["saved_date"].strftime("%d.%m.%Y")
        
        # Конвертируем дату добавления в московское время для правильного расчета дней
        saved_date_utc = plant["saved_date"]
        if saved_date_utc.tzinfo is None:
            saved_date_utc = pytz.UTC.localize(saved_date_utc)
        saved_date_moscow = saved_date_utc.astimezone(MOSCOW_TZ)
        
        days_since_added = (moscow_now.date() - saved_date_moscow.date()).days
        
        # Статус полива (по московскому времени)
        if plant["last_watered"]:
            # Конвертируем UTC время полива в московское
            last_watered_utc = plant["last_watered"]
            if last_watered_utc.tzinfo is None:
                last_watered_utc = pytz.UTC.localize(last_watered_utc)
            last_watered_moscow = last_watered_utc.astimezone(MOSCOW_TZ)
            
            last_watered_date = last_watered_moscow.strftime("%d.%m.%Y")
            days_since_watered = (moscow_now.date() - last_watered_moscow.date()).days
            interval = plant.get('watering_interval', 5)
            next_watering_in = max(0, interval - days_since_watered)
            
            if days_since_watered == 0:
                watering_status = "💧 Полито сегодня"
                next_watering = f"⏰ Следующий полив через {interval} дней"
            elif next_watering_in <= 0:
                watering_status = f"🔴 Пора поливать! (прошло {days_since_watered} дней)"
                next_watering = "⚠️ Требует немедленного полива"
            elif next_watering_in == 1:
                watering_status = f"🟡 Полито {days_since_watered} дней назад"
                next_watering = "⏰ Полив завтра"
            else:
                watering_status = f"🟢 Полито {days_since_watered} дней назад"
                next_watering = f"⏰ Следующий полив через {next_watering_in} дней"
        else:
            watering_status = "🆕 Еще не поливали"
            interval = plant.get('watering_interval', 5)
            next_watering = f"⏰ Рекомендуем полить через {interval} дней после добавления"
        
        # Формируем полную карточку
        full_analysis = f"📋 <b>Полная карточка растения</b>\n\n"
        
        # Основная информация
        full_analysis += f"🌱 <b>Название:</b> {plant_name}\n"
        if plant_info.get('latin_name'):
            full_analysis += f"🏷️ <i>{plant_info['latin_name']}</i>\n"
        if plant_info.get('family'):
            full_analysis += f"👨‍👩‍👧‍👦 <b>Семейство:</b> {plant_info['family']}\n"
        if plant_info.get('origin'):
            full_analysis += f"🌍 <b>Родина:</b> {plant_info['origin']}\n"
        
        full_analysis += f"\n📅 <b>В коллекции:</b> {added_date} ({days_since_added} дней)\n"
        
        # Текущий статус
        full_analysis += f"\n📊 <b>ТЕКУЩИЙ СТАТУС:</b>\n"
        full_analysis += f"{watering_status}\n"
        full_analysis += f"{next_watering}\n"
        full_analysis += f"🔄 Всего поливов: {plant.get('watering_count', 0)}\n"
        
        # Персональные рекомендации
        if plant.get('notes') and "Персональные рекомендации по поливу:" in plant['notes']:
            personal_rec = plant['notes'].replace("Персональные рекомендации по поливу:", "").strip()
            full_analysis += f"\n💡 <b>ВАШИ ПЕРСОНАЛЬНЫЕ РЕКОМЕНДАЦИИ:</b>\n{personal_rec}\n"
        
        # Условия содержания из анализа
        full_analysis += f"\n🏠 <b>УСЛОВИЯ СОДЕРЖАНИЯ:</b>\n"
        if plant_info.get('light'):
            full_analysis += f"☀️ <b>Свет:</b> {plant_info['light']}\n"
        if plant_info.get('temperature'):
            full_analysis += f"🌡️ <b>Температура:</b> {plant_info['temperature']}\n"
        if plant_info.get('humidity'):
            full_analysis += f"💨 <b>Влажность:</b> {plant_info['humidity']}\n"
        
        # Уход
        full_analysis += f"\n🌿 <b>РЕКОМЕНДАЦИИ ПО УХОДУ:</b>\n"
        if plant_info.get('feeding'):
            full_analysis += f"🍽️ <b>Подкормка:</b> {plant_info['feeding']}\n"
        if plant_info.get('repotting'):
            full_analysis += f"🪴 <b>Пересадка:</b> {plant_info['repotting']}\n"
        
        # Возможные проблемы
        if plant_info.get('problems'):
            full_analysis += f"\n⚠️ <b>СЛЕДИТЕ ЗА:</b>\n{plant_info['problems']}\n"
        
        # Персональный совет
        if plant_info.get('advice'):
            full_analysis += f"\n🎯 <b>СОВЕТ ЭКСПЕРТА:</b>\n{plant_info['advice']}\n"
        
        # Получаем краткую историю ухода
        try:
            history = await db.get_plant_history(plant_id, limit=5)
            if history:
                full_analysis += f"\n📈 <b>ПОСЛЕДНИЕ ДЕЙСТВИЯ:</b>\n"
                for action in history[:3]:  # Показываем только последние 3
                    # Конвертируем время действия в московское
                    action_date_utc = action['action_date']
                    if action_date_utc.tzinfo is None:
                        action_date_utc = pytz.UTC.localize(action_date_utc)
                    action_date_moscow = action_date_utc.astimezone(MOSCOW_TZ)
                    action_date_str = action_date_moscow.strftime("%d.%m")
                    
                    action_type = action['action_type']
                    if action_type == 'watered':
                        full_analysis += f"💧 {action_date_str} - Полив\n"
                    elif action_type == 'added':
                        full_analysis += f"➕ {action_date_str} - Добавлено в коллекцию\n"
                    elif action_type == 'renamed':
                        full_analysis += f"✏️ {action_date_str} - Переименовано\n"
        except:
            pass
        
        # Разбиваем на части если слишком длинный
        max_length = 4000
        if len(full_analysis) > max_length:
            # Первая часть
            await callback.message.answer(
                full_analysis[:max_length] + "...\n\n<i>Продолжение следует ↓</i>",
                parse_mode="HTML"
            )
            # Вторая часть
            await callback.message.answer(
                f"📋 <b>Продолжение карточки:</b>\n\n" + full_analysis[max_length:],
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚙️ Настройки растения", callback_data=f"edit_plant_{plant_id}")],
                    [InlineKeyboardButton(text="💧 Отметить полив", callback_data=f"water_plant_{plant_id}")],
                    [InlineKeyboardButton(text="🔙 К коллекции", callback_data="my_plants")]
                ])
            )
        else:
            await callback.message.answer(
                full_analysis,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚙️ Настройки растения", callback_data=f"edit_plant_{plant_id}")],
                    [InlineKeyboardButton(text="💧 Отметить полив", callback_data=f"water_plant_{plant_id}")],
                    [InlineKeyboardButton(text="🔙 К коллекции", callback_data="my_plants")]
                ])
            )
        
    except Exception as e:
        print(f"Ошибка показа карточки растения: {e}")
        await callback.answer("❌ Ошибка загрузки информации")
    
    await callback.answer()

# Функция для извлечения структурированной информации из анализа
def extract_plant_info_from_analysis(analysis_text: str) -> dict:
    """Извлекает структурированную информацию о растении из текста анализа"""
    info = {}
    
    if not analysis_text:
        return info
    
    lines = analysis_text.split('\n')
    
    for line in lines:
        line = line.strip()
        
        if line.startswith("РАСТЕНИЕ:"):
            plant_name = line.replace("РАСТЕНИЕ:", "").strip()
            if "(" in plant_name and ")" in plant_name:
                info['latin_name'] = plant_name[plant_name.find("(")+1:plant_name.find(")")]
                
        elif line.startswith("СЕМЕЙСТВО:"):
            info['family'] = line.replace("СЕМЕЙСТВО:", "").strip()
            
        elif line.startswith("РОДИНА:"):
            info['origin'] = line.replace("РОДИНА:", "").strip()
            
        elif line.startswith("СВЕТ:"):
            info['light'] = line.replace("СВЕТ:", "").strip()
            
        elif line.startswith("ТЕМПЕРАТУРА:"):
            info['temperature'] = line.replace("ТЕМПЕРАТУРА:", "").strip()
            
        elif line.startswith("ВЛАЖНОСТЬ:"):
            info['humidity'] = line.replace("ВЛАЖНОСТЬ:", "").strip()
            
        elif line.startswith("ПОДКОРМКА:"):
            info['feeding'] = line.replace("ПОДКОРМКА:", "").strip()
            
        elif line.startswith("ПЕРЕСАДКА:"):
            info['repotting'] = line.replace("ПЕРЕСАДКА:", "").strip()
            
        elif line.startswith("ПРОБЛЕМЫ:"):
            info['problems'] = line.replace("ПРОБЛЕМЫ:", "").strip()
            
        elif line.startswith("СОВЕТ:"):
            info['advice'] = line.replace("СОВЕТ:", "").strip()
    
    return info

@dp.callback_query(F.data == "notification_settings")
async def notification_settings_callback(callback: types.CallbackQuery):
    """Настройки уведомлений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        
        # Получаем текущие настройки пользователя
        user_settings = await db.get_user_reminder_settings(user_id)
        if not user_settings:
            # Создаем настройки по умолчанию
            await db.update_user_reminder_settings(user_id, reminder_enabled=True)
            user_settings = {'reminder_enabled': True, 'reminder_time': '09:00'}
        
        # Получаем статистику растений
        async with db.pool.acquire() as conn:
            plants_stats = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_plants,
                    COUNT(CASE WHEN reminder_enabled = TRUE THEN 1 END) as plants_with_reminders
                FROM plants 
                WHERE user_id = $1
            """, user_id)
        
        total_plants = plants_stats['total_plants'] or 0
        plants_with_reminders = plants_stats['plants_with_reminders'] or 0
        
        global_enabled = user_settings.get('reminder_enabled', True)
        global_status = "🔔 включены" if global_enabled else "🔕 выключены"
        
        settings_text = f"🔔 <b>Настройки уведомлений</b>\n\n"
        settings_text += f"🌍 <b>Глобальные уведомления:</b> {global_status}\n"
        settings_text += f"🌱 <b>Растений в коллекции:</b> {total_plants}\n"
        settings_text += f"🔔 <b>С включенными уведомлениями:</b> {plants_with_reminders}\n\n"
        
        if global_enabled:
            settings_text += f"✅ Вы получаете уведомления о поливе\n"
            settings_text += f"⏰ Проверяем растения каждый день в 9:00 (МСК)\n"
            if plants_with_reminders < total_plants:
                settings_text += f"\n💡 У {total_plants - plants_with_reminders} растений уведомления выключены индивидуально"
        else:
            settings_text += f"❌ Все уведомления отключены\n"
            settings_text += f"🔕 Напоминания о поливе не приходят"
        
        # Кнопки управления
        global_button_text = "🔕 Выключить все уведомления" if global_enabled else "🔔 Включить все уведомления"
        
        keyboard = [
            [InlineKeyboardButton(text=global_button_text, callback_data="toggle_global_reminders")],
            [InlineKeyboardButton(text="🌱 Настройки по растениям", callback_data="plant_reminders_list")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ]
        
        await callback.message.answer(
            settings_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка настроек уведомлений: {e}")
        await callback.message.answer("❌ Ошибка загрузки настроек.")
    
    await callback.answer()

@dp.callback_query(F.data == "toggle_global_reminders")
async def toggle_global_reminders_callback(callback: types.CallbackQuery):
    """Переключение глобальных уведомлений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        
        # Получаем текущие настройки
        user_settings = await db.get_user_reminder_settings(user_id)
        current_enabled = user_settings.get('reminder_enabled', True) if user_settings else True
        
        # Переключаем
        new_enabled = not current_enabled
        await db.update_user_reminder_settings(user_id, reminder_enabled=new_enabled)
        
        if new_enabled:
            status_text = "✅ <b>Глобальные уведомления включены!</b>\n\n"
            status_text += "🔔 Теперь вы будете получать напоминания о поливе\n"
            status_text += "⏰ Проверяем растения каждый день в 9:00 утра (МСК)\n"
            status_text += "🌱 Уведомления придут для всех растений с включенными напоминаниями"
        else:
            status_text = "🔕 <b>Все уведомления отключены</b>\n\n"
            status_text += "❌ Напоминания о поливе не будут приходить\n"
            status_text += "💡 Вы можете включить их в любой момент\n"
            status_text += "🌱 Настройки отдельных растений сохранены"
        
        keyboard = [
            [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="notification_settings")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ]
        
        await callback.message.answer(
            status_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка переключения глобальных уведомлений: {e}")
        await callback.message.answer("❌ Ошибка изменения настроек.")
    
    await callback.answer()

@dp.callback_query(F.data == "plant_reminders_list")
async def plant_reminders_list_callback(callback: types.CallbackQuery):
    """Список растений с настройками уведомлений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=20)
        
        if not plants:
            await callback.message.answer(
                "🌱 У вас пока нет растений в коллекции.\n"
                "Нажмите <b>\"🌱 Добавить растение\"</b> в главном меню для настройки уведомлений!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            await callback.answer()
            return
        
        text = f"🌱 <b>Настройки уведомлений по растениям:</b>\n\n"
        
        keyboard_buttons = []
        
        for plant in plants:
            plant_name = plant['display_name']
            reminder_enabled = plant.get('reminder_enabled', True)
            interval = plant.get('watering_interval', 5)
            
            status_icon = "🔔" if reminder_enabled else "🔕"
            short_name = plant_name[:20] + "..." if len(plant_name) > 20 else plant_name
            
            text += f"{status_icon} <b>{plant_name}</b>\n"
            if reminder_enabled:
                text += f"   ⏰ Напоминания каждые {interval} дней\n"
            else:
                text += f"   🔕 Уведомления выключены\n"
            text += "\n"
            
            # Кнопка переключения
            button_text = f"{status_icon} {short_name}"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=button_text, 
                    callback_data=f"toggle_reminder_{plant['id']}"
                )
            ])
        
        # Общие кнопки
        keyboard_buttons.extend([
            [InlineKeyboardButton(text="🔔 Включить все", callback_data="enable_all_plant_reminders"),
             InlineKeyboardButton(text="🔕 Выключить все", callback_data="disable_all_plant_reminders")],
            [InlineKeyboardButton(text="🔙 К настройкам", callback_data="notification_settings")],
        ])
        
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        )
        
    except Exception as e:
        print(f"Ошибка списка растений: {e}")
        await callback.message.answer("❌ Ошибка загрузки списка растений.")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_reminder_"))
async def toggle_plant_reminder_callback(callback: types.CallbackQuery):
    """Переключение уведомлений для отдельного растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        # Переключаем состояние
        current_enabled = plant.get('reminder_enabled', True)
        new_enabled = not current_enabled
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE plants 
                SET reminder_enabled = $1 
                WHERE id = $2 AND user_id = $3
            """, new_enabled, plant_id, user_id)
            
            if not new_enabled:
                # Если выключаем, деактивируем активные напоминания
                await conn.execute("""
                    UPDATE reminders 
                    SET is_active = FALSE 
                    WHERE plant_id = $1 AND is_active = TRUE
                """, plant_id)
        
        plant_name = plant['display_name']
        
        if new_enabled:
            # Создаем новое напоминание
            interval = plant.get('watering_interval', 5)
            await create_plant_reminder(plant_id, user_id, interval)
            
            status_text = f"🔔 <b>Уведомления включены!</b>\n\n"
            status_text += f"🌱 <b>{plant_name}</b>\n"
            status_text += f"⏰ Будете получать напоминания каждые {interval} дней\n"
            status_text += f"📱 Следующее уведомление придет в положенное время"
        else:
            status_text = f"🔕 <b>Уведомления выключены</b>\n\n"
            status_text += f"🌱 <b>{plant_name}</b>\n"
            status_text += f"❌ Напоминания о поливе больше не будут приходить\n"
            status_text += f"💡 Можете включить в любой момент"
        
        # Определяем, откуда пришел запрос для правильной кнопки "Назад"
        if "plant_reminders_list" in callback.message.text:
            back_button = InlineKeyboardButton(text="🔙 К списку растений", callback_data="plant_reminders_list")
        else:
            back_button = InlineKeyboardButton(text="⚙️ Настройки растения", callback_data=f"edit_plant_{plant_id}")
        
        keyboard = [
            [back_button],
            [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="notification_settings")],
        ]
        
        await callback.message.answer(
            status_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка переключения уведомлений растения: {e}")
        await callback.answer("❌ Ошибка изменения настроек")
    
    await callback.answer()

@dp.callback_query(F.data == "enable_all_plant_reminders")
async def enable_all_plant_reminders_callback(callback: types.CallbackQuery):
    """Включить уведомления для всех растений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        
        # Включаем уведомления для всех растений пользователя
        async with db.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE plants 
                SET reminder_enabled = TRUE 
                WHERE user_id = $1 AND reminder_enabled = FALSE
            """, user_id)
            
            updated_count = result.split()[-1] if result else "0"
        
        await callback.message.answer(
            f"🔔 <b>Уведомления включены для всех растений!</b>\n\n"
            f"✅ Обновлено растений: {updated_count}\n"
            f"📱 Теперь вы будете получать напоминания о поливе для всей коллекции",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌱 К списку растений", callback_data="plant_reminders_list")],
                [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="notification_settings")],
            ])
        )
        
    except Exception as e:
        print(f"Ошибка включения всех уведомлений: {e}")
        await callback.message.answer("❌ Ошибка изменения настроек.")
    
    await callback.answer()

@dp.callback_query(F.data == "disable_all_plant_reminders")
async def disable_all_plant_reminders_callback(callback: types.CallbackQuery):
    """Выключить уведомления для всех растений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        
        # Выключаем уведомления для всех растений пользователя
        async with db.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE plants 
                SET reminder_enabled = FALSE 
                WHERE user_id = $1 AND reminder_enabled = TRUE
            """, user_id)
            
            # Деактивируем все активные напоминания
            await conn.execute("""
                UPDATE reminders 
                SET is_active = FALSE 
                WHERE user_id = $1 AND is_active = TRUE
            """, user_id)
            
            updated_count = result.split()[-1] if result else "0"
        
        await callback.message.answer(
            f"🔕 <b>Уведомления выключены для всех растений</b>\n\n"
            f"❌ Обновлено растений: {updated_count}\n"
            f"💡 Напоминания о поливе больше не будут приходить\n"
            f"🔔 Можете включить в любой момент",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌱 К списку растений", callback_data="plant_reminders_list")],
                [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="notification_settings")],
            ])
        )
        
    except Exception as e:
        print(f"Ошибка выключения всех уведомлений: {e}")
        await callback.message.answer("❌ Ошибка изменения настроек.")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("show_photo_"))
async def show_plant_photo_callback(callback: types.CallbackQuery):
    """Показать фото растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = plant['display_name']
        saved_date = plant["saved_date"].strftime("%d.%m.%Y")
        
        # Статус полива для подписи
        if plant["last_watered"]:
            last_watered = plant["last_watered"].strftime("%d.%m.%Y")
            water_info = f" • Полито: {last_watered}"
        else:
            water_info = " • Еще не поливали"
        
        caption = f"📷 <b>{plant_name}</b>\n📅 Добавлено: {saved_date}{water_info}"
        
        await bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=plant['photo_file_id'],
            caption=caption,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"edit_plant_{plant_id}")],
                [InlineKeyboardButton(text="🔙 К коллекции", callback_data="my_plants")]
            ])
        )
        
    except Exception as e:
        print(f"Ошибка показа фото: {e}")
        await callback.answer("❌ Ошибка загрузки фото")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_plant_"))
async def delete_plant_callback(callback: types.CallbackQuery):
    """Подтверждение удаления растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = plant['display_name']
        saved_date = plant["saved_date"].strftime("%d.%m.%Y")
        
        keyboard = [
            [InlineKeyboardButton(text="🗑️ Да, удалить навсегда", callback_data=f"confirm_delete_{plant_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"edit_plant_{plant_id}")],
        ]
        
        await callback.message.answer(
            f"🗑️ <b>Подтверждение удаления</b>\n\n"
            f"🌱 <b>Растение:</b> {plant_name}\n"
            f"📅 <b>Добавлено:</b> {saved_date}\n\n"
            f"⚠️ <b>Внимание!</b> Это действие нельзя отменить.\n"
            f"Вся история ухода будет потеряна.\n\n"
            f"Вы действительно хотите удалить это растение?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка удаления растения: {e}")
        await callback.answer("❌ Ошибка обработки")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_plant_callback(callback: types.CallbackQuery):
    """Окончательное удаление растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено")
            return
        
        plant_name = plant['display_name']
        await db.delete_plant(user_id, plant_id)
        
        await callback.message.answer(
            f"🗑️ <b>Растение удалено</b>\n\n"
            f"<b>{plant_name}</b> успешно удалено из коллекции.\n\n"
            f"📸 Вы всегда можете добавить новое растение,\n"
            f"сфотографировав его для анализа!",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
    except Exception as e:
        print(f"Ошибка окончательного удаления: {e}")
        await callback.answer("❌ Ошибка удаления")
    
    await callback.answer()

@dp.callback_query(F.data == "water_plants")
async def water_plants_callback(callback: types.CallbackQuery):
    """Отметка полива с улучшенной обратной связью"""
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
            reply_markup=main_menu()
        )
        
    except Exception as e:
        print(f"Ошибка отметки полива: {e}")
        await callback.message.answer("❌ Ошибка отметки полива.")
    
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def stats_callback(callback: types.CallbackQuery):
    """Подробная статистика пользователя"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        stats = await db.get_user_stats(user_id)
        
        text = f"📊 <b>Подробная статистика вашего сада:</b>\n\n"
        
        # Основные показатели
        text += f"🌱 <b>Растений в коллекции:</b> {stats['total_plants']}\n"
        
        if stats['total_plants'] > 0:
            watered_count = stats['watered_plants']
            watered_percent = int((watered_count / stats['total_plants']) * 100)
            
            # Статус ухода
            if watered_percent == 100:
                care_status = "🏆 Превосходный уход!"
                care_icon = "🏆"
            elif watered_percent >= 80:
                care_status = "⭐ Отличный уход!"  
                care_icon = "⭐"
            elif watered_percent >= 60:
                care_status = "👍 Хороший уход"
                care_icon = "👍"
            elif watered_percent >= 40:
                care_status = "💪 Можно лучше"
                care_icon = "💪"
            else:
                care_status = "🌵 Нужно больше внимания"
                care_icon = "🌵"
            
            text += f"💧 <b>Политых растений:</b> {watered_count} из {stats['total_plants']} ({watered_percent}%)\n"
            text += f"{care_icon} <b>Оценка ухода:</b> {care_status}\n\n"
            
            # Временные показатели (по московскому времени)
            moscow_now = get_moscow_now()
            
            if stats['first_plant_date']:
                # Конвертируем дату первого растения в московское время
                first_plant_utc = stats['first_plant_date']
                if first_plant_utc.tzinfo is None:
                    first_plant_utc = pytz.UTC.localize(first_plant_utc)
                first_plant_moscow = first_plant_utc.astimezone(MOSCOW_TZ)
                
                first_date = first_plant_moscow.strftime("%d.%m.%Y")
                days_gardening = (moscow_now.date() - first_plant_moscow.date()).days
                text += f"📅 <b>Садовничаете с:</b> {first_date} ({days_gardening} дней)\n"
            
            if stats['last_watered_date']:
                # Конвертируем дату последнего полива
                last_watered_utc = stats['last_watered_date']
                if last_watered_utc.tzinfo is None:
                    last_watered_utc = pytz.UTC.localize(last_watered_utc)
                last_watered_moscow = last_watered_utc.astimezone(MOSCOW_TZ)
                
                last_watered = last_watered_moscow.strftime("%d.%m.%Y")
                days_since_watering = (moscow_now.date() - last_watered_moscow.date()).days
                if days_since_watering == 0:
                    text += f"💧 <b>Последний полив:</b> сегодня\n"
                elif days_since_watering == 1:
                    text += f"💧 <b>Последний полив:</b> вчера\n"
                else:
                    text += f"💧 <b>Последний полив:</b> {days_since_watering} дней назад\n"
            
            # Рекомендации
            text += f"\n💡 <b>Рекомендации:</b>\n"
            if watered_percent == 100:
                text += f"• Отличная работа! Продолжайте в том же духе\n"
                text += f"• Не забывайте проверять состояние листьев\n"
                text += f"• Подумайте о добавлении новых растений"
            elif watered_percent >= 70:
                text += f"• Хорошо справляетесь с уходом\n"
                text += f"• Обратите внимание на не политые растения\n"
                text += f"• Следите за регулярностью полива"
            else:
                text += f"• Уделите больше внимания поливу\n"
                text += f"• Установите напоминания\n"
                text += f"• Проверьте состояние всех растений"
        else:
            text += f"\n🌟 <b>Добро пожаловать в мир растений!</b>\n"
            text += f"• Нажмите \"🌱 Добавить растение\" в главном меню\n"
            text += f"• Получите персональные рекомендации\n"
            text += f"• Начните вести цифровой дневник ухода"
        
        await callback.message.answer(text, parse_mode="HTML", reply_markup=main_menu())
        
    except Exception as e:
        print(f"Ошибка загрузки статистики: {e}")
        await callback.message.answer("❌ Ошибка загрузки статистики.")
    
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

# Функция для обработки форматирования ответов OpenAI
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

# Проверка на растительную тематику и безопасность
def is_plant_related_and_safe(text: str) -> tuple[bool, str]:
    """Проверяет, связан ли вопрос с растениями и безопасен ли он"""
    text_lower = text.lower()
    
    # Запрещенные темы (наркотические и нелегальные растения)
    forbidden_keywords = [
        'марихуана', 'каннабис', 'конопля', 'гашиш', 'травка', 'план', 'дурь',
        'кока', 'кокаин', 'мак', 'опиум', 'героин', 'псилоцибин', 'грибы галлюциногенные',
        'дурман', 'белена', 'красавка', 'аяуаска', 'салвия дивинорум',
        'наркотик', 'наркотический', 'психоактивн', 'галлюциноген', 'опьянен'
    ]
    
    # Проверяем на запрещенные темы
    for keyword in forbidden_keywords:
        if keyword in text_lower:
            return False, "illegal"
    
    # Ключевые слова растительной тематики
    plant_keywords = [
        'растение', 'цветок', 'дерево', 'куст', 'трава', 'листья', 'лист', 'корни', 'корень',
        'стебель', 'ствол', 'ветки', 'ветка', 'плод', 'фрукт', 'овощ', 'ягода', 'семена', 'семя',
        'полив', 'поливать', 'удобрение', 'подкормка', 'пересадка', 'почва', 'грунт', 'земля',
        'горшок', 'кашпо', 'освещение', 'свет', 'солнце', 'тень', 'влажность', 'температура',
        'болезнь', 'вредитель', 'желтеют', 'сохнут', 'вянут', 'опадают', 'гниют',
        'фикус', 'роза', 'орхидея', 'кактус', 'суккулент', 'фиалка', 'герань', 'драцена',
        'спатифиллум', 'монстера', 'филодендрон', 'алоэ', 'хлорофитум', 'пальма', 'папоротник',
        'бегония', 'петуния', 'тюльпан', 'нарцисс', 'лилия', 'ромашка', 'подсолнух',
        'томат', 'огурец', 'перец', 'баклажан', 'капуста', 'морковь', 'лук', 'чеснок',
        'яблоня', 'груша', 'вишня', 'слива', 'виноград', 'малина', 'клубника', 'смородина',
        'комнатный', 'домашний', 'садовый', 'огородный', 'декоративный', 'плодовый',
        'цветение', 'цветет', 'бутон', 'соцветие', 'лепесток', 'тычинка', 'пестик',
        'фотосинтез', 'хлорофилл', 'прививка', 'черенок', 'размножение', 'посадка', 'выращивание'
    ]
    
    # Проверяем наличие растительных ключевых слов
    for keyword in plant_keywords:
        if keyword in text_lower:
            return True, "plant_related"
    
    # Дополнительная проверка на вопросительные конструкции о растениях
    question_patterns = [
        'как ухаживать', 'как поливать', 'как выращивать', 'как сажать', 'как пересадить',
        'почему желтеют', 'почему сохнут', 'почему не растет', 'почему не цветет',
        'что с растением', 'что делать если', 'можно ли', 'нужно ли'
    ]
    
    for pattern in question_patterns:
        if pattern in text_lower:
            return True, "plant_question"
    
    return False, "not_plant_related"

# [Остальные обработчики текстовых сообщений остаются без изменений...]

# Webhook setup и остальной код
async def on_startup():
    """Инициализация при запуске"""
    await init_database()
    
    # Устанавливаем команды бота для меню
    commands = [
        types.BotCommand(command="start", description="🌱 Начать работу"),
        types.BotCommand(command="add", description="🌱 Добавить растение"),
        types.BotCommand(command="analyze", description="📸 Анализ растения"),
        types.BotCommand(command="question", description="❓ Задать вопрос"),
        types.BotCommand(command="plants", description="🌿 Мои растения"),
        types.BotCommand(command="notifications", description="🔔 Настройки уведомлений"),
        types.BotCommand(command="stats", description="📊 Статистика"),
        types.BotCommand(command="help", description="ℹ️ Справка"),
    ]
    
    await bot.set_my_commands(commands)
    print("📋 Команды бота установлены")
    
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
    
    # Для тестирования можно запустить проверку сразу (закомментировано в продакшене)
    # await check_and_send_reminders()
    
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
        "version": "2.6",
        "features": ["plant_identification", "health_assessment", "care_recommendations", "smart_reminders", "notification_management", "easy_plant_adding", "bot_commands"],
        "reminder_schedule": "daily_at_09:00_MSK_UTC+3",
        "commands": ["start", "add", "analyze", "question", "plants", "notifications", "stats", "help"]
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
        
        print(f"🚀 Bloom AI Plant Bot запущен на порту {PORT}")
        print(f"🌱 Готов к точному распознаванию растений!")
        print(f"⏰ Умные напоминания активны (МСК UTC+3)!")
        print(f"🔔 Система управления уведомлениями готова!")
        print(f"📋 Все команды бота настроены и доступны!")
        
        try:
            await asyncio.Future()
        except KeyboardInterrupt:
            print("🛑 Получен сигнал остановки")
        finally:
            await runner.cleanup()
            await on_shutdown()
    else:
        print("🤖 Бот запущен в режиме polling")
        print("🌱 Готов к точному распознаванию растений!")
        print("⏰ Умные напоминания активны (МСК UTC+3)!")
        print("🔔 Система управления уведомлениями готова!")
        print("📋 Все команды бота настроены и доступны!")
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
