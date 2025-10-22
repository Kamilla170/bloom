import logging
import base64
from openai import AsyncOpenAI

from config import OPENAI_API_KEY, PLANT_IDENTIFICATION_PROMPT
from utils.image_utils import optimize_image_for_analysis
from utils.formatters import format_plant_analysis
from utils.season_utils import get_current_season, get_seasonal_care_tips

logger = logging.getLogger(__name__)

# Инициализация OpenAI клиента
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


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


def extract_watering_info(analysis_text: str) -> dict:
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
                    if 2 <= interval <= 20:
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


async def analyze_with_openai_advanced(image_data: bytes, user_question: str = None, previous_state: str = None) -> dict:
    """Продвинутый анализ с определением состояния через OpenAI"""
    if not openai_client:
        return {"success": False, "error": "OpenAI API недоступен"}
    
    try:
        # Получаем информацию о текущем сезоне
        season_data = get_current_season()
        
        optimized_image = await optimize_image_for_analysis(image_data, high_quality=True)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        # Форматируем промпт с учетом сезона
        prompt = PLANT_IDENTIFICATION_PROMPT.format(
            season_name=season_data['name'],
            season_description=season_data['description'],
            season_water_note=f"Корректировка интервала: {season_data['water_adjustment']:+d} дня" if season_data['water_adjustment'] != 0 else "Стандартный режим",
            season_light_note=season_data['light_note'],
            season_temperature_note=season_data['temperature_note'],
            season_feeding_note=season_data['feeding_note'],
            season_water_adjustment=f"{season_data['water_adjustment']:+d} дня к базовому интервалу"
        )
        
        if previous_state:
            prompt += f"\n\nПредыдущее состояние растения: {previous_state}. Определите что изменилось с учетом сезонных факторов."
        
        if user_question:
            prompt += f"\n\nДополнительный вопрос пользователя: {user_question}"
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "Вы - профессиональный ботаник-диагност с 30-летним опытом. Проводите точную идентификацию и профессиональную оценку состояния растений. Все выводы обосновывайте наблюдаемыми признаками. ОБЯЗАТЕЛЬНО учитывайте сезонность при рекомендациях по поливу."
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
        
        # Извлекаем состояние
        state_info = extract_plant_state_from_analysis(raw_analysis)
        
        # Применяем сезонную корректировку к интервалу полива
        state_info['season_adjustment'] = season_data['water_adjustment']
        
        formatted_analysis = format_plant_analysis(raw_analysis, confidence, state_info)
        
        logger.info(f"✅ Анализ завершен. Сезон: {season_data['name']}, Состояние: {state_info['current_state']}, Уверенность: {confidence}%")
        
        return {
            "success": True,
            "analysis": formatted_analysis,
            "raw_analysis": raw_analysis,
            "plant_name": plant_name,
            "confidence": confidence,
            "source": "openai_advanced",
            "state_info": state_info,
            "season_data": season_data
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
    
    # Fallback текст с учетом сезона
    season_data = get_current_season()
    
    fallback_text = f"""
РАСТЕНИЕ: Комнатное растение (требуется идентификация)
УВЕРЕННОСТЬ: 20%
ТЕКУЩЕЕ_СОСТОЯНИЕ: healthy
ПРИЧИНА_СОСТОЯНИЯ: Недостаточно данных
ЭТАП_РОСТА: young
СОСТОЯНИЕ: Требуется визуальный осмотр
ПОЛИВ_АНАЛИЗ: Субстрат не виден на фото
ПОЛИВ_РЕКОМЕНДАЦИИ: Проверяйте влажность почвы. Сейчас {season_data['name']} - {season_data['description'].lower()}
ПОЛИВ_ИНТЕРВАЛ: {5 + season_data['water_adjustment']}
СВЕТ: Яркий рассеянный свет. {season_data['light_note']}
ТЕМПЕРАТУРА: {season_data['temperature_note']}
ВЛАЖНОСТЬ: 40-60%
ПОДКОРМКА: {season_data['feeding_note']}
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
        "state_info": state_info,
        "season_data": season_data
    }


async def answer_plant_question(question: str, plant_context: str = None) -> str:
    """Ответить на вопрос о растении с контекстом"""
    if not openai_client:
        return "❌ OpenAI API недоступен"
    
    try:
        # Получаем информацию о сезоне
        season_info = get_current_season()
        
        seasonal_context = f"""
ТЕКУЩИЙ СЕЗОН: {season_info['season_ru']} ({season_info['month_name_ru']})
ФАЗА РОСТА: {season_info['growth_phase']}
СВЕТОВОЙ ДЕНЬ: {season_info['light_hours']}
КОРРЕКТИРОВКА ПОЛИВА: {season_info['watering_adjustment']}

СЕЗОННЫЕ ОСОБЕННОСТИ:
{season_info['recommendations']}
"""
        
        system_prompt = """Вы - профессиональный ботаник-консультант с многолетним опытом диагностики и ухода за растениями.

СТИЛЬ ОБЩЕНИЯ:
- Авторитетный, экспертный, но доступный
- Конкретные рекомендации на основе фактов
- Обращение на "вы" (профессиональное)
- Структурированные ответы: диагноз → причина → решение
- Используйте профессиональную терминологию, но объясняйте её
- Без излишних эмоций - факты и практические действия

ВАЖНО: НЕ ИСПОЛЬЗУЙТЕ markdown форматирование (**, *, _). Пишите обычным текстом.

КРИТИЧЕСКИ ВАЖНО: Всегда учитывайте текущий сезон и время года при рекомендациях по поливу и уходу!
Зимой полив значительно сокращается, летом увеличивается. Игнорирование сезона может погубить растение.

У вас есть полная история растения: все анализы, проблемы, паттерны ухода пользователя.
Основывайте рекомендации на этих данных.

ПРИМЕРЫ ПРАВИЛЬНОГО СТИЛЯ:
❌ Неправильно: "Твой рипсалис чувствует себя отлично! Продолжай в том же духе!"
✅ Правильно: "Рипсалис находится в хорошем состоянии. Текущий режим полива оптимален - продолжайте поливать раз в 7 дней."

❌ Неправильно: "Просто проверяй почву перед поливом - она должна быть сухой."
✅ Правильно: "Перед поливом проверяйте влажность почвы на глубине 2-3 см. Поливайте только когда субстрат просохнет."

СТРУКТУРА ОТВЕТА (БЕЗ НУМЕРАЦИИ):
Первый абзац: Оценка текущего состояния и диагноз ситуации

Второй абзац: Объяснение причины проблемы или текущей ситуации  

Третий абзац: Конкретные действия с точными параметрами (температура, частота, количество)

Четвертый абзац (при необходимости): Контроль результата и когда ожидать изменений

Будьте кратким (2-4 абзаца), но исчерпывающим. Каждое утверждение должно быть обоснованным."""

        user_prompt = f"""ИСТОРИЯ РАСТЕНИЯ:
{plant_context if plant_context else "Контекст отсутствует"}

{seasonal_context}

ВОПРОС ПОЛЬЗОВАТЕЛЯ:
{question}

Дайте профессиональный ответ (2-4 абзаца) БЕЗ нумерации и markdown:

Абзац 1: Оценка - диагноз текущей ситуации
Абзац 2: Причина - что вызвало проблему или текущее состояние (УЧИТЫВАЙТЕ СЕЗОН!)
Абзац 3: Решение - конкретные действия с параметрами (АДАПТИРУЙТЕ К СЕЗОНУ!)
Абзац 4: Контроль - когда ожидать результат (если применимо)

Используйте данные из истории растения для персонализации рекомендаций.
НЕ используйте markdown форматирование (**, *, _) и нумерованные списки.

ОБЯЗАТЕЛЬНО учитывайте текущий сезон в рекомендациях по поливу и уходу!"""
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=500,  # Достаточно для полного экспертного ответа
            temperature=0.3  # Более точные, менее творческие ответы
        )
        
        answer = response.choices[0].message.content
        
        logger.info(f"✅ OpenAI ответил с контекстом (сезон: {season_info['season_ru']})")
        return answer
        
    except Exception as e:
        logger.error(f"❌ Ошибка ответа на вопрос: {e}")
        return "❌ Не могу дать ответ. Попробуйте переформулировать вопрос."


async def generate_growing_plan(plant_name: str) -> tuple:
    """Генерация плана выращивания через OpenAI"""
    if not openai_client:
        return None, None
    
    try:
        # Получаем информацию о сезоне
        season_info = get_current_season()
        
        prompt = f"""
Составьте профессиональный агротехнический план выращивания для: {plant_name}

ТЕКУЩИЙ СЕЗОН: {season_info['season_ru']} ({season_info['month_name_ru']})
УЧИТЫВАЙТЕ: {season_info['recommendations']}

Требования к плану:
- Научно обоснованные рекомендации с учетом текущего сезона
- Конкретные сроки и параметры
- Учет критических факторов успеха
- Превентивные меры против типичных проблем
- Адаптация под {season_info['season_ru'].lower()}

Формат ответа:

🌱 ЭТАП 1: Название (продолжительность X дней)
• Конкретная агротехническая задача с параметрами
• Следующая задача с обоснованием
• Критические факторы этапа

🌿 ЭТАП 2: Название (продолжительность X дней)
• Задача с точными параметрами
• Контрольные признаки успеха

🌸 ЭТАП 3: Название (продолжительность X дней)
• Задачи с учетом развития растения
• Корректировки ухода

🌳 ЭТАП 4: Название (продолжительность X дней)
• Финальные агротехнические мероприятия
• Критерии готовности растения

В конце добавьте:
КАЛЕНДАРЬ_ЗАДАЧ: [структурированный JSON с задачами по дням]
"""
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system", 
                    "content": f"Вы - агроном-консультант с опытом выращивания широкого спектра растений. Составляйте практичные, научно обоснованные планы. Учитывайте, что сейчас {season_info['season_ru']} - {season_info['growth_phase'].lower()}."
                },
                {"role": "user", "content": prompt}
            ],
            max_tokens=1200,
            temperature=0.2
        )
        
        plan_text = response.choices[0].message.content
        
        # Создаем календарь задач (упрощенная версия)
        task_calendar = {
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
        
        return plan_text, task_calendar
        
    except Exception as e:
        logger.error(f"Ошибка генерации плана: {e}")
        return None, None
