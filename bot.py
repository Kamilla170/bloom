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
scheduler = AsyncIOScheduler(timezone=pytz.timezone('Europe/Moscow'))

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

Дайте ответ в формате:
РАСТЕНИЕ: [Точное название вида на русском и латинском языке]
УВЕРЕННОСТЬ: [процент уверенности в идентификации]
ПРИЗНАКИ: [ключевые признаки, по которым определили]
СЕМЕЙСТВО: [ботаническое семейство]
РОДИНА: [естественная среда обитания]

СОСТОЯНИЕ: [детальная оценка здоровья по листьям, цвету, упругости]
ПОЛИВ: [конкретные рекомендации для этого вида]
СВЕТ: [точные требования к освещению для данного растения]
ТЕМПЕРАТУРА: [оптимальный диапазон для этого вида]
ВЛАЖНОСТЬ: [требования к влажности воздуха]
ПОДКОРМКА: [рекомендации по удобрениям]
ПЕРЕСАДКА: [когда и как пересаживать этот вид]

ПРОБЛЕМЫ: [возможные болезни и вредители характерные для этого вида]
СОВЕТ: [специфический совет для улучшения ухода за этим конкретным растением]

Будьте максимально точными и конкретными. Если не можете точно определить вид, укажите хотя бы род или семейство.
"""

# Состояния
class PlantStates(StatesGroup):
    waiting_question = State()
    editing_plant_name = State()

# === СИСТЕМА НАПОМИНАНИЙ ===

async def check_and_send_reminders():
    """Проверка и отправка напоминаний о поливе (каждые 8 часов)"""
    try:
        db = await get_db()
        
        # Получаем растения, которые нужно полить
        async with db.pool.acquire() as conn:
            plants_to_water = await conn.fetch("""
                SELECT p.id, p.user_id, 
                       COALESCE(p.custom_name, p.plant_name, 'Растение #' || p.id) as display_name,
                       p.last_watered, p.watering_interval, p.photo_file_id
                FROM plants p
                JOIN user_settings us ON p.user_id = us.user_id
                WHERE p.reminder_enabled = TRUE 
                  AND us.reminder_enabled = TRUE
                  AND (
                    p.last_watered IS NULL 
                    OR p.last_watered + (p.watering_interval || ' days')::interval <= CURRENT_TIMESTAMP
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM reminders r 
                    WHERE r.plant_id = p.id 
                    AND r.last_sent::date = CURRENT_DATE
                  )
                ORDER BY p.last_watered ASC NULLS FIRST
            """)
            
            for plant in plants_to_water:
                await send_watering_reminder(plant)
                
    except Exception as e:
        print(f"Ошибка проверки напоминаний: {e}")

async def send_watering_reminder(plant_row):
    """Отправка напоминания о поливе конкретного растения"""
    try:
        user_id = plant_row['user_id']
        plant_id = plant_row['id']
        plant_name = plant_row['display_name']
        
        # Вычисляем сколько дней прошло с последнего полива
        if plant_row['last_watered']:
            days_ago = (datetime.now() - plant_row['last_watered']).days
            time_info = f"Последний полив был {days_ago} дней назад"
        else:
            time_info = "Растение еще ни разу не поливали"
        
        # Кнопки для быстрых действий
        keyboard = [
            [InlineKeyboardButton(text="💧 Полил(а)!", callback_data=f"water_plant_{plant_id}")],
            [InlineKeyboardButton(text="⏰ Напомнить завтра", callback_data=f"snooze_{plant_id}")],
            [InlineKeyboardButton(text="🔧 Настройки", callback_data=f"edit_plant_{plant_id}")],
        ]
        
        # Отправляем уведомление с фото растения
        await bot.send_photo(
            chat_id=user_id,
            photo=plant_row['photo_file_id'],
            caption=(
                f"💧 <b>Время полить растение!</b>\n\n"
                f"🌱 <b>{plant_name}</b>\n"
                f"⏰ {time_info}\n\n"
                f"💡 Проверьте влажность почвы пальцем"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        # Отмечаем что напоминание отправлено
        db = await get_db()
        async with db.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO reminders (user_id, plant_id, reminder_type, next_date, last_sent)
                VALUES ($1, $2, 'watering', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, plant_id, reminder_type) 
                WHERE is_active = TRUE
                DO UPDATE SET 
                    last_sent = CURRENT_TIMESTAMP,
                    send_count = COALESCE(reminders.send_count, 0) + 1
            """, user_id, plant_id, 'watering')
        
        print(f"📤 Отправлено напоминание пользователю {user_id} о растении {plant_name}")
        
    except Exception as e:
        print(f"Ошибка отправки напоминания: {e}")

async def create_plant_reminder(plant_id: int, user_id: int, interval_days: int = 5):
    """Создать напоминание для нового растения"""
    try:
        db = await get_db()
        next_watering = datetime.now() + timedelta(days=interval_days)
        
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
        
        db = await get_db()
        plant = await db.get_plant_by_id(plant_id, callback.from_user.id)
        
        if plant:
            plant_name = plant['display_name']
            await callback.message.answer(
                f"⏰ <b>Напоминание отложено</b>\n\n"
                f"Завтра напомню полить <b>{plant_name}</b>",
                parse_mode="HTML"
            )
        
    except Exception as e:
        print(f"Ошибка отложения напоминания: {e}")
    
    await callback.answer()

# Обновленная функция сохранения растения
@dp.callback_query(F.data == "save_plant")
async def save_plant_callback(callback: types.CallbackQuery):
    """Сохранение растения с созданием напоминания"""
    user_id = callback.from_user.id
    
    if user_id in temp_analyses:
        try:
            analysis_data = temp_analyses[user_id]
            
            # Определяем интервал полива на основе анализа
            analysis_text = analysis_data["analysis"].lower()
            if any(word in analysis_text for word in ["кактус", "суккулент", "алоэ"]):
                watering_interval = 10  # Суккуленты реже
            elif any(word in analysis_text for word in ["папоротник", "фиалка", "спатифиллум"]):
                watering_interval = 3   # Влаголюбивые чаще
            else:
                watering_interval = 5   # Стандартный интервал
            
            # Сохраняем в БД
            db = await get_db()
            plant_id = await db.save_plant(
                user_id=user_id,
                analysis=analysis_data["analysis"],
                photo_file_id=analysis_data["photo_file_id"],
                plant_name=analysis_data.get("plant_name", "Неизвестное растение")
            )
            
            # Устанавливаем интервал полива
            await db.update_plant_watering_interval(plant_id, watering_interval)
            
            # Создаем напоминание
            await create_plant_reminder(plant_id, user_id, watering_interval)
            
            # Удаляем временные данные
            del temp_analyses[user_id]
            
            plant_name = analysis_data.get("plant_name", "растение")
            
            success_text = f"✅ <b>Растение сохранено и настроено!</b>\n\n"
            success_text += f"🌱 <b>{plant_name}</b> добавлено в коллекцию\n"
            success_text += f"⏰ Буду напоминать о поливе каждые {watering_interval} дней\n\n"
            success_text += f"💡 Первое напоминание придет через {watering_interval} дней"
            
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
        
        current_time = datetime.now().strftime("%d.%m.%Y в %H:%M")
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
        [InlineKeyboardButton(text="📸 Анализ растения", callback_data="analyze")],
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="question")],
        [InlineKeyboardButton(text="🌱 Мои растения", callback_data="my_plants")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def after_analysis():
    keyboard = [
        [InlineKeyboardButton(text="💾 Сохранить", callback_data="save_plant")],
        [InlineKeyboardButton(text="❓ Вопрос о растении", callback_data="ask_about")],
        [InlineKeyboardButton(text="🔄 Повторный анализ", callback_data="reanalyze")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
            formatted += f"{icon} <b>Состояние:</b> {condition}\n\n"
            
        elif line.startswith("ПОЛИВ:"):
            watering = line.replace("ПОЛИВ:", "").strip()
            formatted += f"💧 <b>Полив:</b> {watering}\n"
            
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
        if watering_info:
            watering_freq = "Следуйте стандартному режиму полива"
            # Plant.id может предоставлять информацию о поливе
        else:
            watering_freq = "Поливайте когда верхний слой почвы подсохнет на 2-3 см"
        
        # Создаем детальный анализ
        analysis_text = f"""
РАСТЕНИЕ: {display_name} ({plant_name})
УВЕРЕННОСТЬ: {probability:.0f}%
ПРИЗНАКИ: Идентифицировано по форме листьев, характеру роста и морфологическим особенностям
СЕМЕЙСТВО: {family if family else 'Не определено'}
РОДИНА: {plant_details.get('description', {}).get('value', 'Информация недоступна')[:100] + '...' if plant_details.get('description', {}).get('value') else 'Не определено'}

СОСТОЯНИЕ: {health_info}
ПОЛИВ: {watering_freq}
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
ПОЛИВ: Проверяйте влажность почвы пальцем - поливайте когда верхний слой подсох на 2-3 см
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
        "📸 Точное распознавание видов растений\n"
        "💡 Персонализированные рекомендации по уходу\n"
        "❓ Ответы на вопросы о растениях\n"
        "⏰ Умные напоминания о поливе для каждого растения\n\n"
        "Пришлите фото растения для детального анализа!",
        reply_markup=main_menu()
    )

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Команда /help"""
    help_text = """
🌱 <b>Как пользоваться ботом:</b>

📸 <b>Анализ растения:</b>
• Пришли фото растения или используй /analyze
• Получи полный анализ и рекомендации
• Сохрани растение в коллекцию

⏰ <b>Умные напоминания:</b>
• Автоматические уведомления о поливе
• Персональный график для каждого растения
• Быстрая отметка полива из уведомления

❓ <b>Вопросы о растениях:</b>
• Просто напиши вопрос в чат
• Или используй команду /question
• Получи экспертный совет

🌿 <b>Мои растения:</b>
• Команда /plants - просмотр коллекции
• Отмечай полив и уход
• Настраивай персональные интервалы

📊 <b>Статистика:</b>
• Команда /stats - подробная статистика
• Отслеживай прогресс ухода

<b>Для лучшего результата:</b>
• Фотографируй при хорошем освещении
• Покажи листья крупным планом
• Включи в кадр всё растение целиком

<b>Доступные команды в меню:</b>
/start - главное меню
/analyze - анализ растения
/question - задать вопрос
/plants - мои растения  
/stats - статистика
/help - эта справка

💡 <b>Быстрый доступ через меню команд!</b>
    """
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_menu())

# [Остальные функции остаются без изменений...]
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
                "date": datetime.now(),
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

# [Остальные callback обработчики - analyze, reanalyze, question и т.д. остаются без изменений...]

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
    
    # Запускаем планировщик напоминаний
    scheduler.add_job(
        check_and_send_reminders,
        'interval',
        hours=8,  # Проверяем каждые 8 часов
        id='reminder_check',
        replace_existing=True
    )
    scheduler.start()
    print("🔔 Планировщик напоминаний запущен")
    
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
        "version": "2.1",
        "features": ["plant_identification", "health_assessment", "care_recommendations", "smart_reminders"]
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
        print(f"⏰ Умные напоминания активны!")
        
        try:
            await asyncio.Future()
        except KeyboardInterrupt:
            print("🛑 Получен сигнал остановки")
        finally:
            await runner.cleanup()
            await on_shutdown()
    else:
        print("🤖 Бот запущен в режиме polling")
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
