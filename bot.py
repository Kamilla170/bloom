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

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Простое хранилище в памяти (в продакшене используйте базу данных)
users_data = {}

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

# Анализ растения с помощью OpenAI
async def analyze_plant_image(image_data: bytes, user_question: str = None) -> dict:
    """Анализ изображения растения"""
    try:
        # Оптимизируем изображение
        optimized_image = await optimize_image(image_data)
        base64_image = base64.b64encode(optimized_image).decode('utf-8')
        
        # Базовый промпт
        prompt = """
        Проанализируй это растение и дай подробную информацию:
        
        1. Название растения (обычное и научное)
        2. Семейство
        3. Состояние растения (здоровье)
        4. Проблемы если есть
        5. Рекомендации по уходу:
           - Полив (как часто)
           - Освещение
           - Температура
           - Влажность
           - Подкормка
        6. Советы по сезонному уходу
        
        Отвечай подробно на русском языке.
        """
        
        if user_question:
            prompt += f"\n\nДополнительно ответь на вопрос: {user_question}"
        
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
            max_tokens=1500,
            temperature=0.3
        )
        
        return {
            "success": True,
            "analysis": response.choices[0].message.content
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
    if user_id not in users_data:
        users_data[user_id] = {
            "plants": [],
            "reminders": []
        }
    
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
            # Сохраняем анализ для пользователя
            user_id = message.from_user.id
            if user_id not in users_data:
                users_data[user_id] = {"plants": [], "reminders": []}
            
            users_data[user_id]["last_analysis"] = {
                "analysis": result["analysis"],
                "photo_file_id": photo.file_id,
                "date": datetime.now()
            }
            
            # Отправляем результат
            analysis = result["analysis"]
            if len(analysis) > 4000:
                analysis = analysis[:4000] + "...\n\n💬 Полный анализ сохранен"
            
            await message.reply(
                f"🌱 <b>Анализ растения:</b>\n\n{analysis}",
                parse_mode="HTML",
                reply_markup=after_analysis()
            )
        else:
            await message.reply(f"❌ Ошибка анализа: {result['error']}")
            
    except Exception as e:
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
                    "content": "Ты эксперт по растениям и цветам. Отвечай подробно и практично на русском языке."
                },
                {
                    "role": "user",
                    "content": message.text
                }
            ],
            max_tokens=1000,
            temperature=0.4
        )
        
        await processing_msg.delete()
        
        answer = response.choices[0].message.content
        await message.reply(f"🌿 <b>Ответ эксперта:</b>\n\n{answer}", parse_mode="HTML", reply_markup=main_menu())
        await state.clear()
        
    except Exception as e:
        await message.reply("❌ Произошла ошибка. Попробуйте позже.")
        await state.clear()

@dp.callback_query(F.data == "save_plant")
async def save_plant_callback(callback: types.CallbackQuery):
    """Сохранение растения"""
    user_id = callback.from_user.id
    
    if user_id in users_data and "last_analysis" in users_data[user_id]:
        analysis_data = users_data[user_id]["last_analysis"]
        
        # Простое сохранение в память
        plant = {
            "id": len(users_data[user_id]["plants"]) + 1,
            "analysis": analysis_data["analysis"],
            "photo_file_id": analysis_data["photo_file_id"],
            "saved_date": datetime.now(),
            "last_watered": None
        }
        
        users_data[user_id]["plants"].append(plant)
        
        await callback.message.answer(
            "✅ Растение сохранено!\n"
            "Теперь вы можете отмечать полив и получать напоминания.",
            reply_markup=main_menu()
        )
    else:
        await callback.message.answer("❌ Нет данных для сохранения. Сначала проанализируйте растение.")
    
    await callback.answer()

@dp.callback_query(F.data == "my_plants")
async def my_plants_callback(callback: types.CallbackQuery):
    """Просмотр сохраненных растений"""
    user_id = callback.from_user.id
    
    if user_id not in users_data or not users_data[user_id]["plants"]:
        await callback.message.answer(
            "🌱 У вас пока нет сохраненных растений.\n"
            "Пришлите фото растения для анализа!",
            reply_markup=main_menu()
        )
        await callback.answer()
        return
    
    plants = users_data[user_id]["plants"]
    text = f"🌿 <b>Ваши растения ({len(plants)}):</b>\n\n"
    
    for i, plant in enumerate(plants[-5:], 1):  # Показываем последние 5
        saved_date = plant["saved_date"].strftime("%d.%m.%Y")
        watered = plant["last_watered"].strftime("%d.%m") if plant["last_watered"] else "никогда"
        
        text += f"{i}. Растение #{plant['id']}\n"
        text += f"   📅 Добавлено: {saved_date}\n"
        text += f"   💧 Полив: {watered}\n\n"
    
    # Кнопки для действий с растениями
    keyboard = [
        [InlineKeyboardButton(text="💧 Отметить полив", callback_data="water_plants")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    
    await callback.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data == "water_plants")
async def water_plants_callback(callback: types.CallbackQuery):
    """Отметка полива"""
    user_id = callback.from_user.id
    
    if user_id in users_data and users_data[user_id]["plants"]:
        # Отмечаем полив для всех растений
        for plant in users_data[user_id]["plants"]:
            plant["last_watered"] = datetime.now()
        
        await callback.message.answer(
            "✅ Полив отмечен для всех растений!\n"
            "Не забудьте полить их снова через несколько дней.",
            reply_markup=main_menu()
        )
    else:
        await callback.message.answer("❌ Нет растений для полива.")
    
    await callback.answer()

@dp.callback_query(F.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    await callback.message.answer("🌱 Главное меню:", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "ask_about")
async def ask_about_callback(callback: types.CallbackQuery, state: FSMContext):
    """Вопрос о проанализированном растении"""
    user_id = callback.from_user.id
    
    if user_id in users_data and "last_analysis" in users_data[user_id]:
        await callback.message.answer(
            "❓ Задайте вопрос об этом растении:\n"
            "Например: 'Почему желтеют листья?' или 'Как часто поливать?'"
        )
        await state.set_state(PlantStates.waiting_question)
    else:
        await callback.message.answer("❌ Сначала проанализируйте растение.")
    
    await callback.answer()

# Webhook setup для Railway
async def on_startup():
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        print(f"Webhook установлен: {WEBHOOK_URL}/webhook")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Webhook удален, используется polling")

async def on_shutdown():
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
    return web.json_response({"status": "healthy"})

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
