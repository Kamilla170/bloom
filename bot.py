import os
import asyncio
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from openai import OpenAI

# Загружаем токены из .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# Простая база напоминаний в памяти
reminders = {}

# --- Команда /start ---
@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Привет 🌱! Я бот по уходу за растениями.\n"
        "Пришли мне текст — отвечу как GPT.\n"
        "Пришли фото растения — попробую определить, что это.\n"
        "Команда /remind для установки напоминания (полив, удобрение)."
    )

# --- Обработка фото ---
@dp.message(lambda m: m.photo)
async def handle_photo(message: Message):
    try:
        photo = message.photo[-1]  # Берём фото в хорошем качестве
        file = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты бот-эксперт по растениям."},
                {"role": "user", "content": f"Что это за растение? Дай совет по уходу. Фото: {file_url}"}
            ]
        )

        answer = response.choices[0].message.content
        await message.answer(answer)

    except Exception as e:
        await message.answer("Не удалось проанализировать фото 😔")
        print("Ошибка фото:", e)

# --- Обработка текста ---
@dp.message()
async def gpt_handler(message: Message):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты бот-эксперт по уходу за растениями и цветами."},
                {"role": "user", "content": message.text},
            ],
        )
        answer = response.choices[0].message.content
        await message.answer(answer)
    except Exception as e:
        await message.answer("Ошибка при запросе к GPT 😔")
        print("Ошибка GPT:", e)

# --- Установка напоминания ---
@dp.message(lambda m: m.text and m.text.startswith("/remind"))
async def remind_handler(message: Message):
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            return await message.answer("Использование: /remind <минуты> <задача>\nНапример: /remind 1 полить цветок")

        minutes = int(parts[1])
        task = parts[2]

        remind_time = datetime.now() + timedelta(minutes=minutes)
        reminders[remind_time] = (message.chat.id, task)

        await message.answer(f"⏰ Напоминание установлено: {task} через {minutes} минут")

    except Exception as e:
        await message.answer("Ошибка при установке напоминания")
        print("Ошибка напоминания:", e)

# --- Проверка и отправка напоминаний ---
async def reminder_loop():
    while True:
        now = datetime.now()
        due = [time for time in reminders if time <= now]
        for time in due:
            chat_id, task = reminders.pop(time)
            await bot.send_message(chat_id, f"🌿 Напоминание: {task}")
        await asyncio.sleep(30)

# --- Запуск ---
async def main():
    asyncio.create_task(reminder_loop())  # запуск проверки напоминаний
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
