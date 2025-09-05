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

# Обработчики команд (без изменений)
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
        "🌿 Напоминания о поливе и уходе\n\n"
        "Пришлите фото растения для детального анализа!",
        reply_markup=main_menu()
    )

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Команда /help"""
    help_text = """
🌱 <b>Возможности бота:</b>

📸 <b>Точное распознавание растений:</b>
• Определяю вид растения по фото
• Показываю латинское название  
• Указываю семейство и родину
• Оцениваю уверенность распознавания

💡 <b>Персональные рекомендации:</b>
• Конкретные советы по поливу
• Требования к освещению и температуре
• Рекомендации по подкормке
• Советы по пересадке

🩺 <b>Диагностика проблем:</b>
• Оценка здоровья растения
• Выявление болезней и вредителей
• Рекомендации по лечению

❓ <b>Экспертные консультации:</b>
• Ответы на любые вопросы о растениях
• Помощь в решении проблем
• Советы по улучшению ухода

<b>Для лучшего результата:</b>
• Фотографируйте при хорошем освещении
• Покажите листья крупным планом
• Включите в кадр всё растение целиком

<b>Команды:</b>
/start - главное меню
/help - эта справка
    """
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_menu())

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
            
            # Показываем источник и качество анализа
            source_text = ""
            confidence = result.get("confidence", 0)
            
            if result.get("source") == "openai_advanced":
                source_text = f"\n\n🤖 <i>Анализ ИИ-ботаника (GPT-4 Vision)</i>"
            elif result.get("source") == "plantid_advanced":
                source_text = f"\n\n🌿 <i>Анализ Plant.id Database</i>"
            elif result.get("source") == "fallback_improved":
                source_text = f"\n\n💡 <i>Общие рекомендации - попробуйте повторить фото</i>"
            
            # Добавляем рекомендации по улучшению фото если нужно
            retry_text = ""
            if result.get("needs_retry"):
                retry_text = ("\n\n📸 <b>Для лучшего результата:</b>\n"
                            "• Сфотографируйте при ярком освещении\n"
                            "• Покажите листья крупным планом\n"
                            "• Уберите лишние предметы из кадра")
            
            # Отправляем результат
            response_text = f"🌱 <b>Результат анализа:</b>\n\n{result['analysis']}{source_text}{retry_text}"
            
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

# Улучшенная обработка вопросов
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

Используйте эмодзи для наглядности.
Давайте конкретные, применимые советы.
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

@dp.callback_query(F.data == "save_plant")
async def save_plant_callback(callback: types.CallbackQuery):
    """Сохранение растения с улучшенной информацией"""
    user_id = callback.from_user.id
    
    if user_id in temp_analyses:
        try:
            analysis_data = temp_analyses[user_id]
            
            # Сохраняем в БД с дополнительной информацией
            db = await get_db()
            plant_id = await db.save_plant(
                user_id=user_id,
                analysis=analysis_data["analysis"],
                photo_file_id=analysis_data["photo_file_id"],
                plant_name=analysis_data.get("plant_name", "Неизвестное растение")
            )
            
            # Удаляем временные данные
            del temp_analyses[user_id]
            
            confidence = analysis_data.get("confidence", 0)
            plant_name = analysis_data.get("plant_name", "растение")
            
            success_text = f"✅ <b>Растение сохранено!</b>\n\n"
            success_text += f"🌱 <b>{plant_name}</b> добавлено в вашу коллекцию\n"
            
            if confidence >= 80:
                success_text += f"🎯 Высокая точность распознавания ({confidence:.0f}%)\n\n"
            elif confidence >= 60:
                success_text += f"👍 Хорошее распознавание ({confidence:.0f}%)\n\n" 
            else:
                success_text += f"💡 Для уточнения можете добавить новое фото позже\n\n"
            
            success_text += (
                "🌿 <b>Теперь вы можете:</b>\n"
                "• Отмечать полив и уход\n"
                "• Просматривать историю растения\n"
                "• Получать персональные напоминания\n"
                "• Задавать вопросы об этом растении\n\n"
                "Перейдите в 'Мои растения' чтобы управлять коллекцией!"
            )
            
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

@dp.callback_query(F.data == "my_plants")
async def my_plants_callback(callback: types.CallbackQuery):
    """Просмотр сохраненных растений с улучшенной информацией"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=5)
        
        if not plants:
            await callback.message.answer(
                "🌱 <b>Ваша коллекция пуста</b>\n\n"
                "📸 Сфотографируйте растение для:\n"
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
        
        for i, plant in enumerate(plants, 1):
            # Используем сохраненное название или извлекаем из анализа
            plant_name = plant.get('plant_name') or f"Растение #{plant['id']}"
            if not plant.get('plant_name') and "РАСТЕНИЕ:" in str(plant['analysis']):
                lines = plant['analysis'].split('\n')
                for line in lines:
                    if line.startswith("РАСТЕНИЕ:"):
                        extracted_name = line.replace("РАСТЕНИЕ:", "").strip()
                        if len(extracted_name) < 50:  # Разумная длина
                            plant_name = extracted_name.split("(")[0].strip()
                        break
            
            saved_date = plant["saved_date"].strftime("%d.%m.%Y")
            
            # Более информативный статус полива
            if plant["last_watered"]:
                watered = plant["last_watered"].strftime("%d.%m")
                days_ago = (datetime.now() - plant["last_watered"]).days
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
        
        # Улучшенные кнопки управления
        keyboard = [
            [InlineKeyboardButton(text="💧 Отметить полив всех", callback_data="water_plants")],
            [InlineKeyboardButton(text="📊 Подробная статистика", callback_data="stats")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ]
        
        await callback.message.answer(
            text, 
            parse_mode="HTML", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка загрузки растений: {e}")
        await callback.message.answer("❌ Ошибка загрузки коллекции растений.")
    
    await callback.answer()

@dp.callback_query(F.data == "water_plants")
async def water_plants_callback(callback: types.CallbackQuery):
    """Отметка полива с улучшенной обратной связью"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        await db.update_watering(user_id)
        
        current_time = datetime.now().strftime("%d.%m.%Y в %H:%M")
        
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
            
            # Временные показатели
            if stats['first_plant_date']:
                first_date = stats['first_plant_date'].strftime("%d.%m.%Y")
                days_gardening = (datetime.now() - stats['first_plant_date']).days
                text += f"📅 <b>Садовничаете с:</b> {first_date} ({days_gardening} дней)\n"
            
            if stats['last_watered_date']:
                last_watered = stats['last_watered_date'].strftime("%d.%m.%Y")
                days_since_watering = (datetime.now().date() - stats['last_watered_date'].date()).days
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
            text += f"• Сфотографируйте свое первое растение\n"
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

# Webhook setup и остальной код остается без изменений
async def on_startup():
    """Инициализация при запуске"""
    await init_database()
    
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        print(f"Webhook установлен: {WEBHOOK_URL}/webhook")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Webhook удален, используется polling")

async def on_shutdown():
    """Очистка при завершении"""
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
        "version": "2.0",
        "features": ["plant_identification", "health_assessment", "care_recommendations"]
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
