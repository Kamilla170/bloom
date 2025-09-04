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
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
PLANTID_API_KEY = os.getenv("PLANTID_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Временное хранилище для анализов (до сохранения)
temp_analyses = {}

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
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# Форматирование анализа
def format_plant_analysis(raw_text: str) -> str:
    """Форматирование анализа для красивого вывода"""
    
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    formatted = ""
    
    for line in lines:
        if line.startswith("РАСТЕНИЕ:"):
            plant_name = line.replace("РАСТЕНИЕ:", "").strip()
            formatted += f"🌿 <b>{plant_name}</b>\n\n"
            
        elif line.startswith("СОСТОЯНИЕ:"):
            condition = line.replace("СОСТОЯНИЕ:", "").strip()
            # Выбираем эмодзи в зависимости от состояния
            if any(word in condition.lower() for word in ["здоров", "хорош", "норм", "отличн"]):
                icon = "✅"
            elif any(word in condition.lower() for word in ["проблем", "болен", "плох"]):
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
            
        elif line.startswith("СОВЕТ:"):
            advice = line.replace("СОВЕТ:", "").strip()
            formatted += f"\n💡 <b>Совет:</b> {advice}"
    
    # Если структура не распознана, делаем базовое форматирование
    if len(formatted) < 50:
        # Ищем название растения в тексте
        plant_name = "Растение"
        plant_keywords = ["орхидея", "фикус", "роза", "кактус", "фиалка", "драцена", "спатифиллум", "монстера"]
        
        for line in lines:
            line_lower = line.lower()
            for keyword in plant_keywords:
                if keyword in line_lower:
                    plant_name = keyword.capitalize()
                    break
            if plant_name != "Растение":
                break
        
        # Простое форматирование с разбивкой на абзацы
        paragraphs = raw_text.split('\n\n')
        short_text = ""
        
        for para in paragraphs[:3]:  # Берем первые 3 абзаца
            if len(short_text) + len(para) > 400:
                break
            short_text += para.strip() + "\n\n"
        
        if len(raw_text) > len(short_text):
            short_text += "..."
        
        formatted = f"🌿 <b>{plant_name}</b>\n\n{short_text.strip()}"
    
    # Добавляем призыв к действию
    formatted += "\n\n💾 <i>Сохраните растение для напоминаний о поливе!</i>"
    
    return formatted

# Обработка изображений
async def optimize_image(image_data: bytes) -> bytes:
    """Оптимизация изображения"""
    try:
        image = Image.open(BytesIO(image_data))
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        # Уменьшаем если больше 1024px
        if max(image.size) > 1024:
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        
        output = BytesIO()
        image.save(output, format='JPEG', quality=85, optimize=True)
        return output.getvalue()
    except:
        return image_data

# Функция-заглушка для случаев, когда API недоступны
async def fallback_plant_analysis(user_question: str = None) -> dict:
    """Резервная функция анализа с общими советами"""
    
    fallback_text = """
РАСТЕНИЕ: Комнатное растение
СОСТОЯНИЕ: Для точной оценки рекомендуется визуальный осмотр листьев и корней
ПОЛИВ: Проверяйте влажность почвы пальцем - поливайте когда верхний слой подсох на 2-3 см
СВЕТ: Большинство растений предпочитают яркий рассеянный свет без прямых солнечных лучей
ТЕМПЕРАТУРА: 18-24°C - оптимальный диапазон для большинства комнатных растений
СОВЕТ: Наблюдайте за растением - листья подскажут его потребности (желтые листья - переувлажнение, коричневые кончики - сухость)
    """.strip()
    
    if user_question:
        fallback_text += f"\n\nПо вашему вопросу '{user_question}': Рекомендуем обратиться к справочнику по комнатным растениям или проконсультироваться в садовом центре."
    
    formatted_analysis = format_plant_analysis(fallback_text)
    
    return {
        "success": True,
        "analysis": formatted_analysis,
        "raw_analysis": fallback_text,
        "fallback": True
    }

# Анализ через Plant.id API
async def analyze_with_plantid(image_data: bytes) -> dict:
    """Анализ растения через Plant.id API"""
    try:
        import httpx
        
        if not PLANTID_API_KEY:
            return await fallback_plant_analysis()
        
        optimized_image = await optimize_image(image_data)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.plant.id/v2/identify",
                json={
                    "images": [f"data:image/jpeg;base64,{base64_image}"],
                    "modifiers": ["crops_fast", "similar_images", "health_assessment"],
                    "plant_language": "ru",
                    "plant_details": ["common_names", "care"]
                },
                headers={
                    "Content-Type": "application/json",
                    "Api-Key": PLANTID_API_KEY
                }
            )
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get("suggestions") and len(data["suggestions"]) > 0:
                suggestion = data["suggestions"][0]
                plant_name = suggestion.get("plant_name", "Неизвестное растение")
                probability = suggestion.get("probability", 0) * 100
                
                # Получаем информацию о здоровье если есть
                health_info = "Требуется визуальная оценка"
                if data.get("health_assessment"):
                    health = data["health_assessment"]
                    if health.get("is_healthy"):
                        if health["is_healthy"]["probability"] > 0.7:
                            health_info = "Выглядит здоровым"
                        else:
                            health_info = "Возможны проблемы со здоровьем"
                
                analysis_text = f"""
РАСТЕНИЕ: {plant_name} (достоверность: {probability:.0f}%)
СОСТОЯНИЕ: {health_info}
ПОЛИВ: Поливайте когда верхний слой почвы подсохнет на 2-3 см
СВЕТ: Яркий рассеянный свет, избегайте прямых солнечных лучей
ТЕМПЕРАТУРА: 18-24°C для большинства комнатных растений  
СОВЕТ: Изучите конкретные потребности данного вида растения для оптимального ухода
                """.strip()
                
                formatted_analysis = format_plant_analysis(analysis_text)
                
                return {
                    "success": True,
                    "analysis": formatted_analysis,
                    "raw_analysis": analysis_text,
                    "plant_name": plant_name,
                    "confidence": probability
                }
        
        return await fallback_plant_analysis()
        
    except Exception as e:
        print(f"Plant.id API error: {e}")
        return await fallback_plant_analysis()

# Анализ через Claude API
async def analyze_with_claude(image_data: bytes, user_question: str = None) -> dict:
    """Анализ растения через Claude API"""
    try:
        import anthropic
        
        if not CLAUDE_API_KEY:
            return await fallback_plant_analysis(user_question)
        
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        
        optimized_image = await optimize_image(image_data)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        prompt = """
        Проанализируйте это комнатное растение и дайте практические советы по уходу в формате:

        РАСТЕНИЕ: [тип/название растения]
        СОСТОЯНИЕ: [оценка здоровья по внешнему виду]
        ПОЛИВ: [рекомендации по частоте и способу полива]
        СВЕТ: [требования к освещению]
        ТЕМПЕРАТУРА: [оптимальный температурный режим]
        СОВЕТ: [главная рекомендация по улучшению ухода]

        Отвечайте кратко и практично на русском языке.
        """
        
        if user_question:
            prompt += f"\n\nТакже ответьте на вопрос: {user_question}"
        
        message = client.messages.create(
            model="claude-3-sonnet-20240229",
            max_tokens=600,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64_image
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ]
        )
        
        raw_analysis = message.content[0].text
        formatted_analysis = format_plant_analysis(raw_analysis)
        
        return {
            "success": True,
            "analysis": formatted_analysis,
            "raw_analysis": raw_analysis,
            "source": "claude"
        }
        
    except Exception as e:
        print(f"Claude API error: {e}")
        return await fallback_plant_analysis(user_question)

# Обновленный анализ растения с несколькими попытками
async def analyze_plant_image(image_data: bytes, user_question: str = None) -> dict:
    """Анализ изображения растения с несколькими вариантами API"""
    
    # Попытка 1: OpenAI с улучшенным промптом
    if openai_client:
        try:
            optimized_image = await optimize_image(image_data)
            base64_image = base64.b64encode(optimized_image).decode('utf-8')
            
            # Более нейтральный промпт, фокус на советах по уходу
            prompt = """
            Вы - эксперт по уходу за комнатными растениями. На изображении показано домашнее растение. 
            Предоставьте практические рекомендации по уходу в следующем формате:

            РАСТЕНИЕ: [тип растения на основе видимых характеристик листьев и формы]
            СОСТОЯНИЕ: [общая оценка внешнего вида и здоровья]  
            ПОЛИВ: [рекомендации по режиму полива]
            СВЕТ: [потребности в освещении]
            ТЕМПЕРАТУРА: [оптимальные условия содержания]
            СОВЕТ: [один ключевой совет по улучшению ухода]

            Сосредоточьтесь на практических советах по уходу за растением.
            Отвечайте кратко и четко на русском языке.
            """
            
            if user_question:
                prompt += f"\n\nДополнительно ответьте на вопрос: {user_question}"
            
            response = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}",
                                    "detail": "low"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=600,
                temperature=0.3
            )
            
            raw_analysis = response.choices[0].message.content
            
            # Проверяем, не отказался ли OpenAI
            if "sorry" in raw_analysis.lower() or "can't help" in raw_analysis.lower():
                raise Exception("OpenAI refused to analyze")
            
            formatted_analysis = format_plant_analysis(raw_analysis)
            
            return {
                "success": True,
                "analysis": formatted_analysis,
                "raw_analysis": raw_analysis,
                "source": "openai"
            }
            
        except Exception as e:
            print(f"OpenAI API error: {e}")
    
    # Попытка 2: Plant.id API
    if PLANTID_API_KEY:
        result = await analyze_with_plantid(image_data)
        if result["success"] and not result.get("fallback"):
            return result
    
    # Попытка 3: Claude API  
    if CLAUDE_API_KEY:
        result = await analyze_with_claude(image_data, user_question)
        if result["success"] and not result.get("fallback"):
            return result
    
    # Запасной вариант с общими советами
    return await fallback_plant_analysis(user_question)

# Обработчики команд
@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Команда /start"""
    user_id = message.from_user.id
    
    # Добавляем пользователя в БД
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
        "Я помогу тебе ухаживать за растениями:\n"
        "📸 Пришли фото растения для анализа\n"
        "❓ Задай вопрос о растениях\n"
        "🌿 Получай напоминания о поливе",
        reply_markup=main_menu()
    )

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Команда /help"""
    help_text = """
🌱 <b>Как пользоваться ботом:</b>

📸 <b>Анализ растения:</b>
• Пришли фото растения
• Получи полный анализ и рекомендации
• Сохрани растение в коллекцию

❓ <b>Вопросы о растениях:</b>
• Нажми "Задать вопрос"
• Опиши проблему или задай любой вопрос
• Получи экспертный совет

🌿 <b>Мои растения:</b>
• Просматривай сохраненные растения
• Отмечай полив и уход
• Получай напоминания

<b>Команды:</b>
/start - главное меню
/help - эта справка
    """
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_menu())

# Обработка фотографий
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработка фотографий растений"""
    try:
        processing_msg = await message.reply("🔍 Анализирую ваше растение...")
        
        # Получаем фото
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)
        
        # Анализируем
        user_question = message.caption if message.caption else None
        result = await analyze_plant_image(file_data.read(), user_question)
        
        await processing_msg.delete()
        
        if result["success"]:
            # Сохраняем временный анализ
            user_id = message.from_user.id
            temp_analyses[user_id] = {
                "analysis": result.get("raw_analysis", result["analysis"]),
                "formatted_analysis": result["analysis"],
                "photo_file_id": photo.file_id,
                "date": datetime.now(),
                "source": result.get("source", "fallback")
            }
            
            # Показываем источник анализа если не fallback
            source_text = ""
            if result.get("source") == "openai":
                source_text = "\n\n🤖 <i>Анализ выполнен с помощью ИИ</i>"
            elif result.get("source") == "plantid":
                source_text = "\n\n🌿 <i>Анализ выполнен Plant.id</i>"
            elif result.get("source") == "claude":
                source_text = "\n\n🧠 <i>Анализ выполнен Claude AI</i>"
            elif result.get("fallback"):
                source_text = "\n\n💡 <i>Показаны общие рекомендации по уходу</i>"
            
            # Отправляем красиво отформатированный результат
            await message.reply(
                f"🌱 <b>Анализ растения:</b>\n\n{result['analysis']}{source_text}",
                parse_mode="HTML",
                reply_markup=after_analysis()
            )
        else:
            await message.reply(f"❌ Ошибка анализа: {result.get('error', 'Неизвестная ошибка')}")
            
    except Exception as e:
        print(f"Ошибка обработки фото: {e}")
        await message.reply("❌ Произошла ошибка. Попробуйте позже.")

# Callback обработчики
@dp.callback_query(F.data == "analyze")
async def analyze_callback(callback: types.CallbackQuery):
    await callback.message.answer("📸 Пришлите фото растения для анализа")
    await callback.answer()

@dp.callback_query(F.data == "question")
async def question_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("❓ Задайте ваш вопрос о растениях:")
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()

@dp.message(StateFilter(PlantStates.waiting_question))
async def handle_question(message: types.Message, state: FSMContext):
    """Обработка текстовых вопросов"""
    try:
        processing_msg = await message.reply("🤔 Думаю над ответом...")
        
        # Пробуем разные API для ответа на вопрос
        answer = None
        
        # Попытка 1: OpenAI
        if openai_client:
            try:
                response = await openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "system",
                            "content": "Ты эксперт по растениям и цветам. Отвечай подробно и практично на русском языке. Используй эмодзи для наглядности."
                        },
                        {
                            "role": "user",
                            "content": message.text
                        }
                    ],
                    max_tokens=800,
                    temperature=0.4
                )
                answer = response.choices[0].message.content
            except Exception as e:
                print(f"OpenAI question error: {e}")
        
        # Попытка 2: Claude API
        if not answer and CLAUDE_API_KEY:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
                
                response = client.messages.create(
                    model="claude-3-sonnet-20240229",
                    max_tokens=800,
                    messages=[
                        {
                            "role": "user", 
                            "content": f"Ты эксперт по растениям. Ответь подробно и практично на русском языке на вопрос: {message.text}"
                        }
                    ]
                )
                answer = response.content[0].text
            except Exception as e:
                print(f"Claude question error: {e}")
        
        await processing_msg.delete()
        
        if answer:
            # Добавляем эмодзи если их нет
            if not any(char in answer for char in ["🌿", "💧", "☀️", "🌡️", "💡"]):
                answer = f"🌿 <b>Ответ эксперта:</b>\n\n{answer}"
            
            await message.reply(answer, parse_mode="HTML", reply_markup=main_menu())
        else:
            # Запасной ответ
            await message.reply(
                "🤔 К сожалению, не могу ответить на ваш вопрос прямо сейчас.\n\n"
                "💡 Рекомендуем:\n"
                "• Обратиться в садовый центр\n" 
                "• Поискать информацию в справочниках\n"
                "• Сфотографировать растение для анализа\n\n"
                "Попробуйте задать вопрос позже!",
                reply_markup=main_menu()
            )
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка ответа на вопрос: {e}")
        await message.reply("❌ Произошла ошибка. Попробуйте позже.", reply_markup=main_menu())
        await state.clear()

@dp.callback_query(F.data == "save_plant")
async def save_plant_callback(callback: types.CallbackQuery):
    """Сохранение растения"""
    user_id = callback.from_user.id
    
    if user_id in temp_analyses:
        try:
            analysis_data = temp_analyses[user_id]
            
            # Сохраняем в БД (используем полный анализ, не отформатированный)
            db = await get_db()
            plant_id = await db.save_plant(
                user_id=user_id,
                analysis=analysis_data["analysis"],
                photo_file_id=analysis_data["photo_file_id"]
            )
            
            # Удаляем временные данные
            del temp_analyses[user_id]
            
            await callback.message.answer(
                "✅ <b>Растение сохранено!</b>\n\n"
                "🌱 Теперь вы можете:\n"
                "• Отмечать полив\n"
                "• Просматривать историю ухода\n"
                "• Получать напоминания\n\n"
                "Перейдите в 'Мои растения' чтобы увидеть коллекцию!",
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
    """Просмотр сохраненных растений"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=5)
        
        if not plants:
            await callback.message.answer(
                "🌱 <b>У вас пока нет растений</b>\n\n"
                "📸 Пришлите фото растения для анализа и сохранения в коллекцию!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            await callback.answer()
            return
        
        text = f"🌿 <b>Ваша коллекция ({len(plants)} растений):</b>\n\n"
        
        for i, plant in enumerate(plants, 1):
            # Извлекаем название растения из анализа
            plant_name = f"Растение #{plant['id']}"
            if plant.get('plant_name'):
                plant_name = plant['plant_name']
            elif "РАСТЕНИЕ:" in str(plant['analysis']):
                lines = plant['analysis'].split('\n')
                for line in lines:
                    if line.startswith("РАСТЕНИЕ:"):
                        plant_name = line.replace("РАСТЕНИЕ:", "").strip()
                        break
            
            saved_date = plant["saved_date"].strftime("%d.%m.%Y")
            
            if plant["last_watered"]:
                watered = plant["last_watered"].strftime("%d.%m")
                water_icon = "💧"
            else:
                watered = "не поливали"
                water_icon = "🌵"
            
            text += f"{i}. 🌱 <b>{plant_name}</b>\n"
            text += f"   📅 Добавлено: {saved_date}\n"
            text += f"   {water_icon} Полив: {watered}\n\n"
        
        # Кнопки для действий с растениями
        keyboard = [
            [InlineKeyboardButton(text="💧 Отметить полив всех", callback_data="water_plants")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ]
        
        await callback.message.answer(
            text, 
            parse_mode="HTML", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        print(f"Ошибка загрузки растений: {e}")
        await callback.message.answer("❌ Ошибка загрузки растений.")
    
    await callback.answer()

@dp.callback_query(F.data == "water_plants")
async def water_plants_callback(callback: types.CallbackQuery):
    """Отметка полива"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        await db.update_watering(user_id)
        
        current_time = datetime.now().strftime("%d.%m.%Y в %H:%M")
        
        await callback.message.answer(
            f"💧 <b>Полив отмечен!</b>\n\n"
            f"🌱 Все ваши растения политы {current_time}\n\n"
            f"💡 <b>Следующий полив:</b>\n"
            f"• Обычные растения: через 3-7 дней\n"
            f"• Суккуленты: через 1-2 недели\n"
            f"• Орхидеи: через 5-10 дней\n\n"
            f"⏰ Не забудьте проверить влажность почвы!",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
    except Exception as e:
        print(f"Ошибка отметки полива: {e}")
        await callback.message.answer("❌ Ошибка отметки полива.")
    
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def stats_callback(callback: types.CallbackQuery):
    """Статистика пользователя"""
    user_id = callback.from_user.id
    
    try:
        db = await get_db()
        stats = await db.get_user_stats(user_id)
        
        text = f"📊 <b>Ваша статистика:</b>\n\n"
        text += f"🌱 <b>Растений в коллекции:</b> {stats['total_plants']}\n"
        
        if stats['total_plants'] > 0:
            watered_percent = int((stats['watered_plants'] / stats['total_plants']) * 100)
            text += f"💧 <b>Политых растений:</b> {stats['watered_plants']} ({watered_percent}%)\n"
            
            if stats['first_plant_date']:
                first_date = stats['first_plant_date'].strftime("%d.%m.%Y")
                text += f"📅 <b>Первое растение:</b> {first_date}\n"
            
            if stats['last_watered_date']:
                last_watered = stats['last_watered_date'].strftime("%d.%m.%Y")
                text += f"💧 <b>Последний полив:</b> {last_watered}\n"
        
        # Добавляем мотивационное сообщение
        if stats['total_plants'] == 0:
            text += f"\n🌟 <b>Начните свою коллекцию!</b>\n"
            text += f"Пришлите фото растения для анализа."
        elif stats['watered_plants'] == stats['total_plants']:
            text += f"\n🏆 <b>Отлично!</b> Все растения политы!"
        elif stats['watered_plants'] == 0:
            text += f"\n🌵 <b>Время полива!</b> Ваши растения ждут воды."
        else:
            text += f"\n💪 <b>Хороший уход!</b> Продолжайте в том же духе!"
        
        await callback.message.answer(text, parse_mode="HTML", reply_markup=main_menu())
        
    except Exception as e:
        print(f"Ошибка загрузки статистики: {e}")
        await callback.message.answer("❌ Ошибка загрузки статистики.")
    
    await callback.answer()

@dp.callback_query(F.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    await callback.message.answer("🌱 <b>Главное меню:</b>", parse_mode="HTML", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "ask_about")
async def ask_about_callback(callback: types.CallbackQuery, state: FSMContext):
    """Вопрос о проанализированном растении"""
    user_id = callback.from_user.id
    
    if user_id in temp_analyses:
        await callback.message.answer(
            "❓ <b>Задайте вопрос об этом растении:</b>\n\n"
            "💡 <b>Например:</b>\n"
            "• Почему желтеют листья?\n"
            "• Как часто поливать?\n"
            "• Нужна ли пересадка?\n"
            "• Почему не цветёт?",
            parse_mode="HTML"
        )
        await state.set_state(PlantStates.waiting_question)
    else:
        await callback.message.answer("❌ Сначала проанализируйте растение.")
    
    await callback.answer()

# Webhook setup для Railway
async def on_startup():
    """Инициализация при запуске"""
    # Инициализируем базу данных
    await init_database()
    
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        print(f"Webhook установлен: {WEBHOOK_URL}/webhook")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Webhook удален, используется polling")

async def on_shutdown():
    """Очистка при завершении"""
    # Закрываем соединения с БД
    try:
        db = await get_db()
        await db.close()
    except Exception as e:
        print(f"Ошибка закрытия БД: {e}")
    
    # Закрываем сессию бота
    try:
        await bot.session.close()
    except Exception as e:
        print(f"Ошибка закрытия сессии бота: {e}")

# Webhook handler
async def webhook_handler(request):
    """Обработчик webhook запросов"""
    try:
        url = str(request.url)
        index = url.rfind('/')
        token = url[index + 1:]
        
        if token == BOT_TOKEN.split(':')[1]:  # Простая проверка токена
            update = types.Update.model_validate(await request.json(), strict=False)
            await dp.feed_update(bot, update)
            return web.Response()
        else:
            return web.Response(status=403)
    except Exception as e:
        print(f"Ошибка webhook: {e}")
        return web.Response(status=500)

# Health check для Railway
async def health_check(request):
    """Проверка здоровья сервиса"""
    return web.json_response({"status": "healthy", "bot": "Bloom AI Plant Care Assistant"})

# Главная функция
async def main():
    """Основная функция запуска бота"""
    logging.basicConfig(level=logging.INFO)
    
    # Инициализация
    await on_startup()
    
    if WEBHOOK_URL:
        # Webhook режим для Railway
        app = web.Application()
        app.router.add_post('/webhook', webhook_handler)
        app.router.add_get('/health', health_check)
        app.router.add_get('/', health_check)  # Дополнительный health check
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        print(f"🚀 Webhook сервер запущен на порту {PORT}")
        print(f"🌱 Бот Bloom готов к работе!")
        
        # Держим сервер работающим
        try:
            await asyncio.Future()  # Ожидаем бесконечно
        except KeyboardInterrupt:
            print("🛑 Получен сигнал остановки")
        finally:
            await runner.cleanup()
            await on_shutdown()
    else:
        # Polling режим для разработки
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
