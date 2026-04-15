import logging
import base64
import re
from openai import AsyncOpenAI

from config import OPENAI_API_KEY, PLANT_IDENTIFICATION_PROMPT, LEGACY_STATE_MAPPING
from utils.image_utils import optimize_image_for_analysis
from utils.formatters import format_plant_analysis
from utils.season_utils import get_current_season, get_seasonal_care_tips

logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

GPT_5_1_MODEL = "gpt-5.1-2025-11-13"

# Допустимые статусы Этапа 3
ALLOWED_STATES = {'healthy', 'flowering', 'growing', 'needs_care', 'dormancy'}


def _normalize_state(state_text: str) -> str:
    """Нормализация статуса с учётом старых значений и алиасов"""
    if not state_text:
        return 'healthy'

    state_text = state_text.strip().lower()

    # Прямое совпадение
    if state_text in ALLOWED_STATES:
        return state_text

    # Маппинг старых статусов
    if state_text in LEGACY_STATE_MAPPING:
        return LEGACY_STATE_MAPPING[state_text]

    # Эвристика по русским ключевым словам
    if 'цвет' in state_text:
        return 'flowering'
    if 'актив' in state_text or 'рост' in state_text or 'growing' in state_text:
        return 'growing'
    if 'покой' in state_text or 'dormanc' in state_text:
        return 'dormancy'
    if 'стресс' in state_text or 'болезн' in state_text or 'проблем' in state_text or 'адаптац' in state_text:
        return 'needs_care'

    return 'healthy'


def extract_plant_state_from_analysis(raw_analysis: str) -> dict:
    """Извлечь информацию о состоянии и подкормке из анализа AI"""
    state_info = {
        'current_state': 'healthy',
        'state_reason': '',
        'recommendations': '',
        'fertilizing_enabled': False,
        'fertilizing_interval': None,
    }

    if not raw_analysis:
        return state_info

    lines = raw_analysis.split('\n')

    for line in lines:
        line = line.strip()

        if line.startswith("ТЕКУЩЕЕ_СОСТОЯНИЕ:"):
            state_text = line.replace("ТЕКУЩЕЕ_СОСТОЯНИЕ:", "").strip()
            state_info['current_state'] = _normalize_state(state_text)

        elif line.startswith("ПРИЧИНА_СОСТОЯНИЯ:"):
            state_info['state_reason'] = line.replace("ПРИЧИНА_СОСТОЯНИЯ:", "").strip()

        elif line.startswith("ПОДКОРМКА_НУЖНА:"):
            value = line.replace("ПОДКОРМКА_НУЖНА:", "").strip().lower()
            state_info['fertilizing_enabled'] = value.startswith('y') or 'да' in value or 'нужн' in value

        elif line.startswith("ПОДКОРМКА_ИНТЕРВАЛ:"):
            interval_text = line.replace("ПОДКОРМКА_ИНТЕРВАЛ:", "").strip()
            numbers = re.findall(r'\d+', interval_text)
            if numbers:
                try:
                    interval = int(numbers[0])
                    if 7 <= interval <= 90:
                        state_info['fertilizing_interval'] = interval
                except:
                    pass

    # Если подкормка нужна, но интервал не указан — ставим дефолт 21 день
    if state_info['fertilizing_enabled'] and not state_info['fertilizing_interval']:
        state_info['fertilizing_interval'] = 21

    # Если подкормка не нужна — обнуляем интервал
    if not state_info['fertilizing_enabled']:
        state_info['fertilizing_interval'] = None

    return state_info


def extract_watering_info(analysis_text: str) -> dict:
    """Извлечь информацию о поливе"""
    watering_info = {
        "interval_days": 7,
        "personal_recommendations": "",
        "current_state": "",
        "needs_adjustment": False
    }

    if not analysis_text:
        return watering_info

    for line in analysis_text.split('\n'):
        line = line.strip()

        if line.startswith("ПОЛИВ_ИНТЕРВАЛ:"):
            interval_text = line.replace("ПОЛИВ_ИНТЕРВАЛ:", "").strip()
            numbers = re.findall(r'\d+', interval_text)
            if numbers:
                try:
                    interval = int(numbers[0])
                    if 2 <= interval <= 28:
                        watering_info["interval_days"] = interval
                except:
                    pass

        elif line.startswith("ПОЛИВ_АНАЛИЗ:"):
            current_state = line.replace("ПОЛИВ_АНАЛИЗ:", "").strip()
            watering_info["current_state"] = current_state

        elif line.startswith("ПОЛИВ_РЕКОМЕНДАЦИИ:"):
            watering_info["personal_recommendations"] = line.replace("ПОЛИВ_РЕКОМЕНДАЦИИ:", "").strip()

    return watering_info


def extract_and_remove_watering_interval(text: str, season_info: dict) -> tuple:
    """Извлечь интервал полива из текста и удалить эту строку."""
    default_interval = 10
    if season_info.get('season') == 'summer':
        default_interval = 7
    elif season_info.get('season') == 'winter':
        default_interval = 12

    interval = default_interval
    clean_text = text

    pattern = r'\n?ПОЛИВ_ИНТЕРВАЛ:\s*(\d+)\s*'
    match = re.search(pattern, text)

    if match:
        try:
            interval = int(match.group(1))
            interval = max(3, min(28, interval))
            logger.info(f"💧 Извлечён интервал полива: {interval} дней")
        except:
            interval = default_interval

        clean_text = re.sub(pattern, '', text).strip()
    else:
        logger.warning(f"⚠️ ПОЛИВ_ИНТЕРВАЛ не найден, default: {default_interval}")

    return interval, clean_text


async def analyze_vision_step(image_data: bytes, user_question: str = None, previous_state: str = None) -> dict:
    """ШАГ 1: Vision анализ через GPT-4o"""
    if not openai_client:
        return {"success": False, "error": "OpenAI API недоступен"}

    try:
        optimized_image = await optimize_image_for_analysis(image_data, high_quality=True)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')

        vision_prompt = """Вы - профессиональный ботаник-диагност. Проанализируйте фотографию растения и опишите ТОЛЬКО то, что видно на изображении.

ВАША ЗАДАЧА:
1. Опишите что видно на фото
2. Выявите возможные проблемы
3. Оцените уровень уверенности (0-100%)

ФОРМАТ ОТВЕТА:
РАСТЕНИЕ: [конкретное название растения]
УВЕРЕННОСТЬ: [число от 0 до 100]%

ЧТО ВИДНО:
- [детальное описание]

ВОЗМОЖНЫЕ ПРОБЛЕМЫ:
- [список проблем или "Проблем не обнаружено"]"""

        if previous_state:
            vision_prompt += f"\n\nПредыдущее состояние: {previous_state}. Обратите внимание на изменения."
        if user_question:
            vision_prompt += f"\n\nВопрос пользователя: {user_question}"

        logger.info("📸 Vision анализ: GPT-4o")
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Вы - профессиональный ботаник-диагност."},
                {"role": "user", "content": [
                    {"type": "text", "text": vision_prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "high"
                    }}
                ]}
            ],
            max_tokens=1000,
            temperature=0.2
        )

        raw_vision = response.choices[0].message.content

        if len(raw_vision) < 50:
            raise Exception("Некачественный ответ")

        plant_name = "Неизвестное растение"
        confidence = 50
        vision_analysis = ""
        possible_problems = ""

        lines = raw_vision.split('\n')
        current_section = None

        for line in lines:
            line = line.strip()
            if line.startswith("РАСТЕНИЕ:"):
                raw_name = line.replace("РАСТЕНИЕ:", "").strip()
                if "неизвестное растение" in raw_name.lower() and "(" in raw_name:
                    match = re.search(r'\((?:возможно,?\s*)?([^)]+)\)', raw_name, re.IGNORECASE)
                    plant_name = match.group(1).strip() if match else raw_name
                else:
                    plant_name = re.sub(r'\s*\(возможно[^)]*\)\s*', '', raw_name, flags=re.IGNORECASE).strip() or raw_name
            elif line.startswith("УВЕРЕННОСТЬ:"):
                try:
                    confidence = float(line.replace("УВЕРЕННОСТЬ:", "").strip().replace("%", ""))
                except:
                    confidence = 50
            elif line.startswith("ЧТО ВИДНО:"):
                current_section = "vision"
                vision_analysis = line.replace("ЧТО ВИДНО:", "").strip() + "\n"
            elif line.startswith("ВОЗМОЖНЫЕ ПРОБЛЕМЫ:"):
                current_section = "problems"
                possible_problems = line.replace("ВОЗМОЖНЫЕ ПРОБЛЕМЫ:", "").strip() + "\n"
            elif current_section == "vision":
                vision_analysis += line + "\n"
            elif current_section == "problems":
                possible_problems += line + "\n"

        if not vision_analysis:
            vision_analysis = raw_vision

        logger.info(f"✅ Vision: {plant_name}, уверенность {confidence}%")

        return {
            "success": True,
            "vision_analysis": vision_analysis.strip(),
            "possible_problems": possible_problems.strip() if possible_problems else "Проблем не обнаружено",
            "confidence": confidence,
            "plant_name": plant_name,
            "raw_observations": raw_vision
        }

    except Exception as e:
        logger.error(f"❌ Vision ошибка: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def analyze_with_openai_advanced(image_data: bytes, user_question: str = None, previous_state: str = None) -> dict:
    """Полный анализ через единый промпт (для парсинга подкормки и состояния)"""
    if not openai_client:
        return {"success": False, "error": "OpenAI API недоступен"}

    try:
        season_data = get_current_season()

        feeding_recommendations = {
            'winter': 'Прекратить подкормки или минимизировать',
            'spring': 'Начать подкормки с половинной дозы',
            'summer': 'Регулярные подкормки каждые 1-2 недели',
            'autumn': 'Постепенно сокращать подкормки'
        }

        water_adjustment_days = 0
        if season_data['season'] == 'winter':
            water_adjustment_days = +5
        elif season_data['season'] == 'summer':
            water_adjustment_days = -2
        elif season_data['season'] == 'autumn':
            water_adjustment_days = +2

        optimized_image = await optimize_image_for_analysis(image_data, high_quality=True)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')

        prompt = PLANT_IDENTIFICATION_PROMPT.format(
            season_name=season_data['season_ru'],
            season_description=season_data['growth_phase'],
            season_water_note=season_data['watering_adjustment'],
            season_light_note=season_data['light_hours'],
            season_temperature_note=season_data['temperature_note'],
            season_feeding_note=feeding_recommendations.get(season_data['season'], 'Стандартный режим'),
            season_water_adjustment=f"{water_adjustment_days:+d} дня к базовому интервалу"
        )

        if previous_state:
            prompt += f"\n\nПредыдущее состояние растения: {previous_state}."
        if user_question:
            prompt += f"\n\nВопрос пользователя: {user_question}"

        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Вы - профессиональный ботаник-диагност с 30-летним опытом. Используйте только 5 статусов: healthy, flowering, growing, needs_care, dormancy."},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "high"
                    }}
                ]}
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
                    confidence = float(line.replace("УВЕРЕННОСТЬ:", "").strip().replace("%", ""))
                except:
                    confidence = 70
                break

        plant_name = "Неизвестное растение"
        for line in raw_analysis.split('\n'):
            if line.startswith("РАСТЕНИЕ:"):
                raw_name = line.replace("РАСТЕНИЕ:", "").strip()
                if "неизвестное растение" in raw_name.lower() and "(" in raw_name:
                    match = re.search(r'\((?:возможно,?\s*)?([^)]+)\)', raw_name, re.IGNORECASE)
                    plant_name = match.group(1).strip() if match else raw_name
                else:
                    plant_name = re.sub(r'\s*\(возможно[^)]*\)\s*', '', raw_name, flags=re.IGNORECASE).strip() or raw_name
                break

        state_info = extract_plant_state_from_analysis(raw_analysis)
        watering_info = extract_watering_info(raw_analysis)

        formatted_analysis = format_plant_analysis(raw_analysis, confidence, state_info)

        logger.info(
            f"✅ Анализ: {plant_name}, состояние={state_info['current_state']}, "
            f"подкормка={state_info['fertilizing_enabled']} ({state_info['fertilizing_interval']}д)"
        )

        return {
            "success": True,
            "analysis": formatted_analysis,
            "raw_analysis": raw_analysis,
            "plant_name": plant_name,
            "confidence": confidence,
            "source": "openai_advanced",
            "state_info": state_info,
            "watering_interval": watering_info["interval_days"],
            "season_data": season_data
        }

    except Exception as e:
        logger.error(f"❌ OpenAI error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def analyze_plant_image(image_data: bytes, user_question: str = None,
                              previous_state: str = None, retry_count: int = 0,
                              plant_context: str = None) -> dict:
    """
    Главная функция анализа.
    Этап 3: используем единый промпт через analyze_with_openai_advanced —
    он корректно парсит состояние, интервал полива и подкормку.
    """
    logger.info("🔍 Запуск анализа растения")

    result = await analyze_with_openai_advanced(image_data, user_question, previous_state)

    if result["success"]:
        # Гарантируем, что watering_interval и поля подкормки попадут наверх
        if 'watering_interval' not in result:
            watering_info = extract_watering_info(result.get('raw_analysis', ''))
            result['watering_interval'] = watering_info['interval_days']

        result['needs_retry'] = result.get('confidence', 50) < 50
        return result

    return {"success": False, "error": result.get("error", "Анализ не удался")}


async def answer_plant_question(question: str, plant_context: str = None) -> dict:
    """Ответить на вопрос о растении с контекстом"""
    if not openai_client:
        return {"error": "❌ OpenAI API недоступен"}

    try:
        season_info = get_current_season()

        seasonal_context = f"""
ТЕКУЩИЙ СЕЗОН: {season_info['season_ru']} ({season_info['month_name_ru']})
ФАЗА РОСТА: {season_info['growth_phase']}
КОРРЕКТИРОВКА ПОЛИВА: {season_info['watering_adjustment']}
"""

        system_prompt = """Вы - опытный ботаник-консультант. Отвечайте на вопросы естественно и по существу.

ПРАВИЛА:
- Отвечайте именно на тот вопрос, который задан
- Простой вопрос → короткий ответ (3-5 предложений)
- Сложный вопрос → можно структурировать
- Конкретные цифры где уместно
- Учитывайте текущий сезон
- Используйте HTML-теги <b></b> для выделения, НЕ markdown
- НЕ начинайте с шаблонных заголовков"""

        if plant_context:
            user_prompt = f"ИСТОРИЯ РАСТЕНИЯ:\n{plant_context}\n\n{seasonal_context}\n\nВОПРОС:\n{question}"
        else:
            user_prompt = f"{seasonal_context}\n\nВОПРОС:\n{question}"

        models_to_try = [GPT_5_1_MODEL, "gpt-4o"]

        for model_name in models_to_try:
            try:
                logger.info(f"🔄 Модель: {model_name}")

                api_params = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                }

                if model_name == GPT_5_1_MODEL:
                    api_params["max_completion_tokens"] = 4000
                    api_params["extra_body"] = {"reasoning_effort": "low"}
                else:
                    api_params["max_tokens"] = 1000
                    api_params["temperature"] = 0.4

                response = await openai_client.chat.completions.create(**api_params)
                answer = response.choices[0].message.content

                if answer and len(answer) > 10:
                    logger.info(f"✅ Ответ от {model_name}")
                    return {"answer": answer, "model": model_name}

            except Exception as model_error:
                logger.warning(f"⚠️ {model_name}: {model_error}")
                if model_name == models_to_try[-1]:
                    raise
                continue

        raise Exception("Все модели вернули пустой ответ")

    except Exception as e:
        logger.error(f"❌ Ошибка ответа: {e}", exc_info=True)
        return {"error": "❌ Не могу дать ответ. Попробуйте переформулировать вопрос."}


async def generate_growing_plan(plant_name: str) -> tuple:
    """Генерация плана выращивания (без изменений Этапа 3)"""
    if not openai_client:
        return None, None

    try:
        season_info = get_current_season()

        prompt = f"""
Составьте профессиональный план выращивания для: {plant_name}
ТЕКУЩИЙ СЕЗОН: {season_info['season_ru']}

Формат:
🌱 ЭТАП 1: Название (X дней)
• Задача
🌿 ЭТАП 2: Название (X дней)
🌸 ЭТАП 3: Название (X дней)
🌳 ЭТАП 4: Название (X дней)
"""

        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"Вы - агроном. Сейчас {season_info['season_ru']}."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1200,
            temperature=0.2
        )

        plan_text = response.choices[0].message.content

        task_calendar = {
            "stage_1": {"name": "Подготовка", "duration_days": 7, "tasks": []},
            "stage_2": {"name": "Прорастание", "duration_days": 14, "tasks": []},
            "stage_3": {"name": "Активный рост", "duration_days": 30, "tasks": []},
            "stage_4": {"name": "Взрослое растение", "duration_days": 30, "tasks": []}
        }

        return plan_text, task_calendar

    except Exception as e:
        logger.error(f"Ошибка генерации плана: {e}")
        return None, None
