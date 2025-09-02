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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

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

# Анализ растения с красивым форматированием
async def analyze_plant_image(image_data: bytes, user_question: str = None) -> dict:
    """Анализ изображения растения с улучшенным форматированием"""
    try:
        # Оптимизируем изображение
        optimized_image = await optimize_image(image_data)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        # Структурированный промпт для краткого ответа
        prompt = """
        Проанализируй растение и дай КРАТКИЙ ответ в таком формате:

        РАСТЕНИЕ: [название растения]
        СОСТОЯНИЕ: [здоровье одним предложением]
        ПОЛИВ: [как часто поливать]
        СВЕТ: [требования к освещению]
        ТЕМПЕРАТУРА: [оптимальная температура]
        СОВЕТ: [один важный совет по уходу]

        Отвечай кратко и четко на русском языке.
        """
        
        if user_question:
            prompt += f"\n\nТакже ответь на вопрос: {user_question}"
        
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
                                "detail": "high"
                            }
                        }
                    ]
                }
            ],
            max_tokens=600,  # Ограничиваем для краткости
            temperature=0.3
        )
        
        raw_analysis = response.choices[0].message.content
        formatted_analysis = format_plant_analysis(raw_analysis)
        
        return {
            "success": True,
            "analysis": formatted_analysis,
            "raw_analysis": raw_analysis
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

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
                "date": datetime.now()
            }
            
            # Отправляем красиво отформатированный результат
            await message.reply(
                f"🌱 <b>Анализ растения:</b>\n\n{result['analysis']}",
                parse_mode="HTML",
                reply_markup=after_analysis()
            )
        else:
            await message.reply(f"❌ Ошибка анализа: {result['error']}")
            
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
        
        # Обращаемся к OpenAI
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
        
        await processing_msg.delete()
        
        answer = response.choices[0].message.content
        
        # Добавляем эмодзи если их нет
        if not any(char in answer for char in ["🌿", "💧", "☀️", "🌡️", "💡"]):
            answer = f"🌿 <b>Ответ эксперта:</b>\n\n{answer}"
        
        await message.reply(answer, parse_mode="HTML", reply_markup=main_menu())
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка ответа на вопрос: {e}")
        await message.reply("❌ Произошла ошибка. Попробуйте позже.")
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
    # Инициализируем базу данных
    await init_database()
    
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        print(f"Webhook установлен: {WEBHOOK_URL}/webhook")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Webhook удален, используется polling")

async def on_shutdown():
    # Закрываем соединения с БД
    db = await get_db()
    await db.close()
    await bot.session.close()

# Webhook handler
async def webhook_handler(request):
    url = str(request.url)
    index = url.rfind('/')
    token = url[index + 1:]
    
    if token == BOT_TOKEN.split(':')[1]:  # Простая проверка токена
        update = types.Update.model_validate(await request.json(), strict=False)
        await dp.feed_update(bot, update)
        return web.Response()
    else:
        return web.Response(status=403)

# Health check для Railway
async def health_check(request):
    return web.json_response({"status": "healthy", "bot": "Bloom AI Plant Care Assistant"})

# Главная функция
async def main():
    logging.basicConfig(level=logging.INFO)
    
    await on_startup()
    
    if WEBHOOK_URL:
        # Webhook режим для Railway
        app = web.Application()
        app.router.add_post('/webhook', webhook_handler)
        app.router.add_get('/health', health_check)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        print(f"🚀 Webhook сервер запущен на порту {PORT}")
        print(f"🌱 Бот Bloom готов к работе!")
        
        # Держим сервер работающим
        try:
            await asyncio.Future()
        except KeyboardInterrupt:
            pass
        finally:
            await runner.cleanup()
            await on_shutdown()
    else:
        # Polling режим для разработки
        print("🤖 Бот запущен в режиме polling")
        try:
            await dp.start_polling(bot)
        finally:
            await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
