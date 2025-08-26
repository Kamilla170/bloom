import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Any
import json
import base64

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_KEY")

# Простое хранение в памяти (вместо БД)
user_plants = {}  # {user_id: [{"name": "Фикус", "type": "Фикус", ...}, ...]}
user_settings = {}  # {user_id: {"notifications": True}}

# FSM States
class PlantStates(StatesGroup):
    waiting_for_plant_name = State()

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class OpenAIService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.openai.com/v1"
    
    async def analyze_plant_image(self, image_base64: str) -> Dict[str, Any]:
        """Анализ изображения растения через GPT-4 Vision"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Проанализируй это изображение растения и предоставь информацию в формате JSON:
                            {
                                "plant_name": "название растения",
                                "plant_type": "тип/семейство", 
                                "description": "краткое описание",
                                "watering_frequency": число_дней_между_поливами,
                                "care_tips": "советы по уходу",
                                "confidence": процент_уверенности_в_определении
                            }
                            
                            Если не можешь определить растение, укажи confidence: 0."""
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 800
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/chat/completions", 
                                      headers=headers, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()
                        content = result["choices"][0]["message"]["content"]
                        
                        # Извлекаем JSON из ответа
                        json_start = content.find('{')
                        json_end = content.rfind('}') + 1
                        if json_start != -1 and json_end != -1:
                            json_content = content[json_start:json_end]
                            return json.loads(json_content)
                        else:
                            return {"error": "Не удалось обработать ответ GPT"}
                    else:
                        return {"error": f"API Error: {response.status}"}
        except Exception as e:
            return {"error": f"Ошибка: {str(e)}"}
    
    async def get_plant_advice(self, question: str) -> str:
        """Получение советов по уходу за растениями"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "system", 
                    "content": "Ты эксперт по уходу за растениями. Отвечай на русском языке, давай практические советы."
                },
                {"role": "user", "content": question}
            ],
            "max_tokens": 800
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/chat/completions", 
                                      headers=headers, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result["choices"][0]["message"]["content"]
                    else:
                        return "Извини, не могу ответить на вопрос прямо сейчас."
        except Exception:
            return "Произошла ошибка. Попробуй позже."

# Сервис
openai_service = OpenAIService(OPENAI_API_KEY)

def get_main_keyboard() -> InlineKeyboardMarkup:
    """Главная клавиатура"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌱 Мои растения", callback_data="my_plants")],
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="ask_question")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
    ])
    return keyboard

@dp.message(Command("start"))
async def start_handler(message: Message):
    """Обработчик команды /start"""
    # Инициализируем пользователя
    user_id = message.from_user.id
    if user_id not in user_plants:
        user_plants[user_id] = []
    if user_id not in user_settings:
        user_settings[user_id] = {"notifications": True}
    
    welcome_text = """🌱 Привет! Я помощник по уходу за растениями!

Я могу:
• 📸 Определить растение по фото
• 🌿 Дать советы по уходу  
• ❓ Ответить на вопросы о растениях

**Просто отправь фото своего растения!** 📷"""
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.message(F.photo)
async def photo_handler(message: Message, state: FSMContext):
    """Обработчик фотографий"""
    await message.answer("🔍 Анализирую фото растения...")
    
    try:
        # Получаем файл
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        
        # Скачиваем фото
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
            async with session.get(url) as resp:
                image_data = await resp.read()
        
        # Конвертируем в base64
        image_base64 = base64.b64encode(image_data).decode()
        
        # Анализируем через GPT
        analysis = await openai_service.analyze_plant_image(image_base64)
        
        if "error" in analysis:
            await message.answer(f"❌ Ошибка: {analysis['error']}")
            return
        
        # Формируем ответ
        confidence = analysis.get('confidence', 0)
        if confidence < 50:
            response = "🤔 Не могу точно определить растение, но вот что вижу:\n\n"
        else:
            response = f"✅ Определил растение с уверенностью {confidence}%!\n\n"
        
        response += f"🌱 **{analysis.get('plant_name', 'Неизвестное растение')}**\n"
        response += f"📋 Тип: {analysis.get('plant_type', 'Не определен')}\n\n"
        response += f"📝 {analysis.get('description', '')}\n\n"
        response += f"💧 Рекомендуемый полив: каждые {analysis.get('watering_frequency', 7)} дней\n\n"
        response += f"💡 **Советы:**\n{analysis.get('care_tips', 'Общий уход как для большинства комнатных растений')}"
        
        # Предлагаем сохранить растение
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💾 Сохранить в коллекцию", 
                                callback_data="save_plant")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await message.answer(response, reply_markup=keyboard, parse_mode="Markdown")
        
        # Сохраняем данные в состояние
        await state.update_data(analysis=analysis)
        
    except Exception as e:
        logger.error(f"Ошибка анализа фото: {e}")
        await message.answer("❌ Произошла ошибка при анализе фото. Попробуйте еще раз.")

@dp.callback_query(F.data == "save_plant")
async def save_plant_handler(callback: CallbackQuery, state: FSMContext):
    """Сохранение растения"""
    data = await state.get_data()
    analysis = data.get('analysis')
    
    if not analysis:
        await callback.answer("❌ Данные анализа не найдены")
        return
    
    await callback.message.answer(
        f"🌱 Отлично! Давай сохраним **{analysis.get('plant_name')}**.\n\n"
        f"Как назовем это растение? (например: 'Фикус в гостиной')",
        parse_mode="Markdown"
    )
    
    await state.set_state(PlantStates.waiting_for_plant_name)
    await callback.answer()

@dp.message(StateFilter(PlantStates.waiting_for_plant_name))
async def plant_name_handler(message: Message, state: FSMContext):
    """Обработка названия растения"""
    data = await state.get_data()
    analysis = data.get('analysis')
    user_id = message.from_user.id
    
    # Сохраняем растение в память
    plant = {
        "name": message.text,
        "plant_name": analysis.get('plant_name', ''),
        "plant_type": analysis.get('plant_type', ''),
        "description": analysis.get('description', ''),
        "watering_frequency": analysis.get('watering_frequency', 7),
        "care_tips": analysis.get('care_tips', ''),
        "added_date": datetime.now().strftime("%Y-%m-%d")
    }
    
    user_plants[user_id].append(plant)
    
    await message.answer(
        f"✅ Растение **{message.text}** сохранено в коллекции!\n\n"
        f"💧 Рекомендуемый полив: каждые {plant['watering_frequency']} дней\n\n"
        f"Теперь можешь посмотреть все свои растения в разделе 'Мои растения'",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )
    
    await state.clear()

@dp.callback_query(F.data == "my_plants")
async def my_plants_handler(callback: CallbackQuery):
    """Показ растений пользователя"""
    user_id = callback.from_user.id
    plants = user_plants.get(user_id, [])
    
    if not plants:
        await callback.message.answer(
            "🌱 У тебя пока нет сохраненных растений.\n"
            "Отправь фото растения, чтобы добавить его в коллекцию!",
            reply_markup=get_main_keyboard()
        )
        await callback.answer()
        return
    
    response = "🌿 **Твоя коллекция растений:**\n\n"
    
    for i, plant in enumerate(plants, 1):
        response += f"**{i}. {plant['name']}**\n"
        response += f"   🌱 {plant['plant_name']} ({plant['plant_type']})\n"
        response += f"   💧 Полив: каждые {plant['watering_frequency']} дней\n"
        response += f"   📅 Добавлено: {plant['added_date']}\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Очистить коллекцию", callback_data="clear_plants")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    await callback.message.answer(response, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "clear_plants")
async def clear_plants_handler(callback: CallbackQuery):
    """Очистка коллекции растений"""
    user_id = callback.from_user.id
    user_plants[user_id] = []
    
    await callback.message.answer(
        "🗑 Коллекция очищена!",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "ask_question")
async def ask_question_handler(callback: CallbackQuery):
    """Режим вопросов"""
    await callback.message.answer(
        "❓ **Задай любой вопрос о растениях!**\n\n"
        "Например:\n"
        "• Почему желтеют листья у фикуса?\n"
        "• Как часто поливать кактус?\n"
        "• Что делать, если на листьях пятна?\n\n"
        "Просто напиши свой вопрос 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_handler(callback: CallbackQuery):
    """Справка"""
    help_text = """ℹ️ **Как пользоваться ботом:**

🌱 **Анализ растений:**
• Отправь фото растения
• Получи название и советы по уходу
• Сохрани в свою коллекцию

❓ **Вопросы о растениях:**
• Нажми "Задать вопрос"
• Напиши любой вопрос о уходе
• Получи персональный совет

📱 **Команды:**
• /start - перезапустить бота
• Просто отправь фото растения для анализа"""
    
    await callback.message.answer(
        help_text,
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery):
    """Главное меню"""
    await callback.message.answer(
        "🏠 **Главное меню**\n\nВыбери действие или отправь фото растения:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(F.text)
async def text_handler(message: Message):
    """Обработчик текстовых вопросов"""
    if len(message.text.split()) < 3:  # Игнорируем короткие сообщения
        return
    
    await message.answer("🤔 Думаю над твоим вопросом...")
    
    try:
        answer = await openai_service.get_plant_advice(message.text)
        await message.answer(f"💡 {answer}", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Ошибка получения ответа от GPT: {e}")
        await message.answer(
            "❌ Не могу ответить на вопрос прямо сейчас. Попробуй позже.",
            reply_markup=get_main_keyboard()
        )

async def main():
    """Запуск бота"""
    print("🌱 Простой бот запускается...")
    print("✅ Бот готов к работе!")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print("🛑 Бот остановлен")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
