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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault
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

# Состояния
class PlantStates(StatesGroup):
    waiting_question = State()
    editing_plant_name = State()

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

# Функция извлечения названия растения из анализа
def extract_plant_name_from_analysis(analysis_text: str) -> str:
    """Извлекает название растения из текста анализа"""
    if not analysis_text:
        return None
    
    lines = analysis_text.split('\n')
    for line in lines:
        if line.startswith("РАСТЕНИЕ:"):
            plant_name = line.replace("РАСТЕНИЕ:", "").strip()
            # Убираем лишнюю информацию в скобках и проценты
            if "(" in plant_name:
                plant_name = plant_name.split("(")[0].strip()
            # Убираем информацию о достоверности
            plant_name = plant_name.split("достоверность:")[0].strip()
            plant_name = plant_name.split("%")[0].strip()
            
            # Проверяем длину и разумность названия
            if 3 <= len(plant_name) <= 50 and not plant_name.lower().startswith(("неизвестн", "комнатн", "растение")):
                return plant_name
    
    return None

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

# Клавиатуры с улучшенным функционалом
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

def plant_management_keyboard(plant_id: int):
    """Клавиатура для управления конкретным растением"""
    keyboard = [
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"edit_name_{plant_id}")],
        [InlineKeyboardButton(text="💧 Отметить полив", callback_data=f"water_{plant_id}")],
        [InlineKeyboardButton(text="📊 История растения", callback_data=f"history_{plant_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить растение", callback_data=f"delete_{plant_id}")],
        [InlineKeyboardButton(text="🔙 К коллекции", callback_data="my_plants")],
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
        if len(raw_analysis) < 100 or "sorry" in raw_analysis.lower() or "can't help" in raw_analysis.lower():
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
        disease_name = None
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

ПРОБЛЕМЫ: {disease_name if disease_name else 'Следите за типичными для данного вида вредителями и болезнями'}
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

# Функция-заглушка для случаев, когда API недоступны
async def fallback_plant_analysis(user_question: str = None) -> dict:
    """Резервная функция анализа с общими советами"""
    
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
    
    if user_question:
        fallback_text += f"\n\nПо вашему вопросу '{
