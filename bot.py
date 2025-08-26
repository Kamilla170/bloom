import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import json

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import base64

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///plants.db")

# База данных
Base = declarative_base()
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, index=True)
    username = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    notifications_enabled = Column(Boolean, default=True)

class Plant(Base):
    __tablename__ = "plants"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    name = Column(String, index=True)
    plant_type = Column(String)
    description = Column(Text)
    watering_frequency = Column(Integer)  # дни
    fertilizing_frequency = Column(Integer)  # дни
    last_watered = Column(DateTime)
    last_fertilized = Column(DateTime)
    photo_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# FSM States
class PlantStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_plant_name = State()
    waiting_for_watering_schedule = State()
    waiting_for_fertilizing_schedule = State()

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

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
                            "text": """Проанализируй это изображение растения и предоставь следующую информацию в формате JSON:
                            {
                                "plant_name": "название растения",
                                "plant_type": "тип/семейство",
                                "description": "краткое описание",
                                "watering_frequency": число_дней_между_поливами,
                                "fertilizing_frequency": число_дней_между_подкормками,
                                "care_tips": "советы по уходу",
                                "confidence": процент_уверенности_в_определении
                            }
                            
                            Если не можешь определить растение, укажи confidence: 0 и дай общие советы."""
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
            "max_tokens": 1000
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/chat/completions", 
                                  headers=headers, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    content = result["choices"][0]["message"]["content"]
                    try:
                        # Извлекаем JSON из ответа
                        json_start = content.find('{')
                        json_end = content.rfind('}') + 1
                        json_content = content[json_start:json_end]
                        return json.loads(json_content)
                    except json.JSONDecodeError:
                        return {"error": "Не удалось обработать ответ GPT"}
                else:
                    return {"error": f"API Error: {response.status}"}
    
    async def get_plant_advice(self, question: str, plant_context: str = "") -> str:
        """Получение советов по уходу за растениями"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        system_prompt = f"""Ты эксперт по уходу за растениями и цветами. 
        Отвечай на русском языке, давай практические и полезные советы.
        {f'Контекст о растении пользователя: {plant_context}' if plant_context else ''}"""
        
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            "max_tokens": 1000,
            "temperature": 0.7
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/chat/completions", 
                                  headers=headers, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    return result["choices"][0]["message"]["content"]
                else:
                    return "Извини, не могу ответить на вопрос прямо сейчас. Попробуй позже."

# Сервисы
openai_service = OpenAIService(OPENAI_API_KEY)

def get_db() -> Session:
    db = SessionLocal()
    try:
        return db
    finally:
        db.close()

def get_main_keyboard() -> InlineKeyboardMarkup:
    """Главная клавиатура"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌱 Добавить растение", callback_data="add_plant")],
        [InlineKeyboardButton(text="🌿 Мои растения", callback_data="my_plants")],
        [InlineKeyboardButton(text="💧 Полить сейчас", callback_data="water_now")],
        [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="notifications")]
    ])
    return keyboard

@dp.message(Command("start"))
async def start_handler(message: Message):
    """Обработчик команды /start"""
    db = get_db()
    
    # Создаем или находим пользователя
    user = db.query(User).filter(User.telegram_id == message.from_user.id).first()
    if not user:
        user = User(
            telegram_id=message.from_user.id,
            username=message.from_user.username
        )
        db.add(user)
        db.commit()
    
    welcome_text = """🌱 Привет! Я бот-помощник по уходу за растениями!

Я могу:
• 📸 Определить растение по фото
• 💧 Напоминать о поливе
• 🌿 Давать советы по уходу
• 📅 Планировать подкормки
• ❓ Отвечать на вопросы о растениях

Выбери действие ниже или просто отправь фото своего растения!"""
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.message(F.photo)
async def photo_handler(message: Message, state: FSMContext):
    """Обработчик фотографий"""
    await message.answer("🔍 Анализирую фото растения...")
    
    try:
        # Получаем файл
        photo = message.photo[-1]  # Берем самое большое фото
        file = await bot.get_file(photo.file_id)
        
        # Скачиваем фото
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}") as resp:
                image_data = await resp.read()
        
        # Конвертируем в base64
        image_base64 = base64.b64encode(image_data).decode()
        
        # Анализируем через GPT
        analysis = await openai_service.analyze_plant_image(image_base64)
        
        if "error" in analysis:
            await message.answer(f"❌ Ошибка анализа: {analysis['error']}")
            return
        
        # Формируем ответ
        confidence = analysis.get('confidence', 0)
        if confidence < 50:
            response = "🤔 Не могу точно определить растение, но вот общие рекомендации:\n\n"
        else:
            response = f"✅ Определил растение с уверенностью {confidence}%\n\n"
        
        response += f"🌱 **{analysis.get('plant_name', 'Неизвестное растение')}**\n"
        response += f"📋 Тип: {analysis.get('plant_type', 'Не определен')}\n\n"
        response += f"📝 {analysis.get('description', '')}\n\n"
        response += f"💧 Полив: каждые {analysis.get('watering_frequency', 7)} дней\n"
        response += f"🌿 Подкормка: каждые {analysis.get('fertilizing_frequency', 30)} дней\n\n"
        response += f"💡 **Советы по уходу:**\n{analysis.get('care_tips', '')}"
        
        # Предлагаем добавить растение
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить в мои растения", 
                                callback_data=f"save_plant_{photo.file_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await message.answer(response, reply_markup=keyboard, parse_mode="Markdown")
        
        # Сохраняем данные анализа в состояние
        await state.update_data(analysis=analysis, photo_file_id=photo.file_id)
        
    except Exception as e:
        logger.error(f"Ошибка анализа фото: {e}")
        await message.answer("❌ Произошла ошибка при анализе фото. Попробуйте еще раз.")

@dp.callback_query(F.data.startswith("save_plant_"))
async def save_plant_handler(callback: CallbackQuery, state: FSMContext):
    """Сохранение растения"""
    data = await state.get_data()
    analysis = data.get('analysis')
    
    if not analysis:
        await callback.answer("❌ Данные анализа не найдены")
        return
    
    await callback.message.answer(
        f"🌱 Отлично! Давай добавим **{analysis.get('plant_name')}** в твою коллекцию.\n\n"
        f"Как назовем это растение? (например: 'Фикус в гостиной' или просто '{analysis.get('plant_name')}')",
        parse_mode="Markdown"
    )
    
    await state.set_state(PlantStates.waiting_for_plant_name)
    await callback.answer()

@dp.message(StateFilter(PlantStates.waiting_for_plant_name))
async def plant_name_handler(message: Message, state: FSMContext):
    """Обработка названия растения"""
    data = await state.get_data()
    analysis = data.get('analysis')
    
    db = get_db()
    
    # Создаем растение
    plant = Plant(
        user_id=message.from_user.id,
        name=message.text,
        plant_type=analysis.get('plant_type', ''),
        description=analysis.get('description', ''),
        watering_frequency=analysis.get('watering_frequency', 7),
        fertilizing_frequency=analysis.get('fertilizing_frequency', 30),
        last_watered=datetime.utcnow(),
        last_fertilized=datetime.utcnow(),
        photo_path=data.get('photo_file_id')
    )
    
    db.add(plant)
    db.commit()
    
    # Планируем уведомления
    schedule_plant_notifications(plant)
    
    await message.answer(
        f"✅ Растение **{message.text}** добавлено!\n\n"
        f"💧 Следующий полив: через {plant.watering_frequency} дней\n"
        f"🌿 Следующая подкормка: через {plant.fertilizing_frequency} дней\n\n"
        f"Я буду напоминать тебе о уходе за растением!",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )
    
    await state.clear()

@dp.callback_query(F.data == "my_plants")
async def my_plants_handler(callback: CallbackQuery):
    """Показ растений пользователя"""
    db = get_db()
    plants = db.query(Plant).filter(Plant.user_id == callback.from_user.id).all()
    
    if not plants:
        await callback.message.answer(
            "🌱 У тебя пока нет растений.\nОтправь фото растения, чтобы добавить его!",
            reply_markup=get_main_keyboard()
        )
        await callback.answer()
        return
    
    response = "🌿 **Твои растения:**\n\n"
    
    for plant in plants:
        days_since_watering = (datetime.utcnow() - plant.last_watered).days
        days_since_fertilizing = (datetime.utcnow() - plant.last_fertilized).days
        
        next_watering = plant.watering_frequency - days_since_watering
        next_fertilizing = plant.fertilizing_frequency - days_since_fertilizing
        
        status_water = "💧" if next_watering <= 0 else f"💧 через {next_watering}д"
        status_fert = "🌿" if next_fertilizing <= 0 else f"🌿 через {next_fertilizing}д"
        
        response += f"🌱 **{plant.name}**\n"
        response += f"   {status_water} | {status_fert}\n"
        response += f"   {plant.plant_type}\n\n"
    
    await callback.message.answer(response, reply_markup=get_main_keyboard(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "water_now")
async def water_now_handler(callback: CallbackQuery):
    """Полив растения сейчас"""
    db = get_db()
    plants = db.query(Plant).filter(Plant.user_id == callback.from_user.id).all()
    
    if not plants:
        await callback.answer("У тебя нет растений для полива")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💧 {plant.name}", callback_data=f"water_{plant.id}")]
        for plant in plants
    ] + [[InlineKeyboardButton(text="🏠 Назад", callback_data="main_menu")]])
    
    await callback.message.answer("💧 Какое растение поливаем?", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("water_"))
async def water_plant_handler(callback: CallbackQuery):
    """Полив конкретного растения"""
    plant_id = int(callback.data.split("_")[1])
    db = get_db()
    
    plant = db.query(Plant).filter(Plant.id == plant_id).first()
    if plant:
        plant.last_watered = datetime.utcnow()
        db.commit()
        
        # Перепланируем уведомления
        schedule_plant_notifications(plant)
        
        await callback.message.answer(
            f"💧 Отлично! **{plant.name}** полито.\n"
            f"Следующий полив через {plant.watering_frequency} дней.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown"
        )
    
    await callback.answer()

@dp.callback_query(F.data == "notifications")
async def notifications_handler(callback: CallbackQuery):
    """Настройки уведомлений"""
    db = get_db()
    user = db.query(User).filter(User.telegram_id == callback.from_user.id).first()
    
    status = "включены" if user.notifications_enabled else "выключены"
    action = "выключить" if user.notifications_enabled else "включить"
    callback_data = "toggle_notifications"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔔 {action.capitalize()} уведомления", callback_data=callback_data)],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="main_menu")]
    ])
    
    await callback.message.answer(
        f"🔔 Уведомления сейчас **{status}**\n\n"
        f"Я буду напоминать о:\n"
        f"• 💧 Поливе растений\n"
        f"• 🌿 Подкормке\n"
        f"• 🌱 Общем уходе",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "toggle_notifications")
async def toggle_notifications_handler(callback: CallbackQuery):
    """Переключение уведомлений"""
    db = get_db()
    user = db.query(User).filter(User.telegram_id == callback.from_user.id).first()
    
    user.notifications_enabled = not user.notifications_enabled
    db.commit()
    
    status = "включены" if user.notifications_enabled else "выключены"
    await callback.message.answer(f"✅ Уведомления {status}!", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery):
    """Возврат в главное меню"""
    await callback.message.answer("🏠 Главное меню", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.message(F.text)
async def text_handler(message: Message):
    """Обработчик текстовых вопросов"""
    if len(message.text.split()) < 3:  # Игнорируем короткие сообщения
        return
    
    # Получаем контекст растений пользователя
    db = get_db()
    plants = db.query(Plant).filter(Plant.user_id == message.from_user.id).all()
    
    plant_context = ""
    if plants:
        plant_context = "Растения пользователя: " + ", ".join([
            f"{plant.name} ({plant.plant_type})" for plant in plants
        ])
    
    await message.answer("🤔 Думаю над твоим вопросом...")
    
    try:
        answer = await openai_service.get_plant_advice(message.text, plant_context)
        await message.answer(answer, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Ошибка получения ответа от GPT: {e}")
        await message.answer(
            "❌ Не могу ответить на вопрос прямо сейчас. Попробуй позже.",
            reply_markup=get_main_keyboard()
        )

def schedule_plant_notifications(plant: Plant):
    """Планирование уведомлений для растения"""
    # Удаляем старые задачи
    try:
        scheduler.remove_job(f"water_{plant.id}")
        scheduler.remove_job(f"fertilize_{plant.id}")
    except:
        pass
    
    # Планируем полив
    water_date = plant.last_watered + timedelta(days=plant.watering_frequency)
    scheduler.add_job(
        send_watering_reminder,
        trigger='date',
        run_date=water_date,
        args=[plant.user_id, plant.id, plant.name],
        id=f"water_{plant.id}"
    )
    
    # Планируем подкормку
    fertilize_date = plant.last_fertilized + timedelta(days=plant.fertilizing_frequency)
    scheduler.add_job(
        send_fertilizing_reminder,
        trigger='date',
        run_date=fertilize_date,
        args=[plant.user_id, plant.id, plant.name],
        id=f"fertilize_{plant.id}"
    )

async def send_watering_reminder(user_id: int, plant_id: int, plant_name: str):
    """Отправка напоминания о поливе"""
    db = get_db()
    user = db.query(User).filter(User.telegram_id == user_id).first()
    
    if not user or not user.notifications_enabled:
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💧 Полито!", callback_data=f"water_{plant_id}")],
        [InlineKeyboardButton(text="⏰ Напомнить позже", callback_data=f"remind_later_{plant_id}")]
    ])
    
    try:
        await bot.send_message(
            user_id,
            f"💧 Время полить **{plant_name}**!\n\n"
            f"Не забудь проверить почву - она должна быть сухой на 2-3 см в глубину.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

async def send_fertilizing_reminder(user_id: int, plant_id: int, plant_name: str):
    """Отправка напоминания о подкормке"""
    db = get_db()
    user = db.query(User).filter(User.telegram_id == user_id).first()
    
    if not user or not user.notifications_enabled:
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌿 Подкормлено!", callback_data=f"fertilize_{plant_id}")],
        [InlineKeyboardButton(text="⏰ Напомнить позже", callback_data=f"remind_fert_later_{plant_id}")]
    ])
    
    try:
        await bot.send_message(
            user_id,
            f"🌿 Время подкормить **{plant_name}**!\n\n"
            f"Используй подходящее удобрение согласно инструкции на упаковке.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

async def main():
    """Запуск бота"""
    print("🌱 Бот запускается...")
    
    # Запускаем планировщик
    scheduler.start()
    
    # Восстанавливаем напоминания для существующих растений
    db = get_db()
    plants = db.query(Plant).all()
    for plant in plants:
        schedule_plant_notifications(plant)
    
    print(f"📅 Восстановлено {len(plants)} напоминаний")
    print("✅ Бот готов к работе!")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print("🛑 Бот остановлен")
    finally:
        await bot.session.close()
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
