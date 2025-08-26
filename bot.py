import os
import asyncio
import base64
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from openai import OpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Хранилище в памяти ---
user_plants = {}      # {user_id: [ {"name": ..., "type": ..., "watering_days": ..., "fertilizing_days": ...} ]}
care_logs = {}        # {user_id: [ {"plant": ..., "action": ..., "time": ..., "notes": ...} ]}
reminders = {}        # {datetime: (chat_id, task)}

# --- START ---
@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer(
        "🌱 Привет! Я бот по уходу за растениями.\n\n"
        "Функции:\n"
        "📸 Пришли фото → скажу что за растение.\n"
        "💬 Задай вопрос → отвечу GPT.\n"
        "🔬 /health → проверка здоровья.\n"
        "📅 /schedule → расписание ухода.\n"
        "📝 /log → журнал.\n"
        "📊 /stats → статистика.\n"
        "⏰ /remind → напоминания."
    )

# --- GPT ответы на текст ---
@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def gpt_answer(message: types.Message):
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ты эксперт по уходу за растениями."},
            {"role": "user", "content": message.text}
        ]
    )
    await message.answer(resp.choices[0].message.content)

# --- Анализ фото ---
@dp.message(lambda m: m.photo)
async def analyze_photo(message: types.Message):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Определи растение и дай советы по уходу."},
            {"role": "user", "content": f"Фото: {file_url}"}
        ]
    )
    await message.answer(resp.choices[0].message.content)

# --- Напоминания ---
@dp.message(Command("remind"))
async def remind(message: types.Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return await message.answer("Использование: /remind <минуты> <задача>")
    minutes = int(parts[1])
    task = parts[2]
    remind_time = datetime.now() + timedelta(minutes=minutes)
    reminders[remind_time] = (message.chat.id, task)
    await message.answer(f"⏰ Напоминание: {task} через {minutes} минут")

async def reminder_loop():
    while True:
        now = datetime.now()
        for t in list(reminders.keys()):
            if t <= now:
                chat_id, task = reminders.pop(t)
                await bot.send_message(chat_id, f"🌿 Напоминание: {task}")
        await asyncio.sleep(30)

# --- Проверка здоровья ---
@dp.message(Command("health"))
async def health(message: types.Message):
    await message.answer("📸 Пришли фото растения для проверки здоровья.")

# --- Генерация расписания ---
@dp.message(Command("schedule"))
async def schedule(message: types.Message):
    plants = user_plants.get(message.from_user.id, [])
    if not plants:
        return await message.answer("🌱 Сначала добавь растения в память (упрощённо).")
    info = "\n".join([f"{p['name']} ({p['type']})" for p in plants])

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Составь недельное расписание ухода."},
            {"role": "user", "content": f"Создай расписание ухода для: {info}"}
        ]
    )
    await message.answer(resp.choices[0].message.content)

# --- Журнал ухода ---
@dp.message(Command("log"))
async def log(message: types.Message):
    logs = care_logs.get(message.from_user.id, [])
    if not logs:
        return await message.answer("📝 Журнал пуст.")
    text = "📝 Последние действия:\n\n"
    for log in logs[-10:]:
        text += f"{log['time']} — {log['plant']} — {log['action']} ({log['notes']})\n"
    await message.answer(text)

# --- Статистика ---
@dp.message(Command("stats"))
async def stats(message: types.Message):
    logs = care_logs.get(message.from_user.id, [])
    if not logs:
        return await message.answer("📊 Нет статистики.")
    total = len(logs)
    watering = sum(1 for l in logs if l['action'] == "полив")
    fertilizing = sum(1 for l in logs if l['action'] == "подкормка")
    await message.answer(
        f"📊 Статистика:\nВсего действий: {total}\n💧 Поливов: {watering}\n🌿 Подкормок: {fertilizing}"
    )

# --- MAIN ---
async def main():
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
