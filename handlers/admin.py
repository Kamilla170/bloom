import logging
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from database import get_db
from states.user_states import AdminStates
from config import ADMIN_USER_IDS

logger = logging.getLogger(__name__)

router = Router()


def is_admin(user_id: int) -> bool:
    """Проверка прав администратора"""
    return user_id in ADMIN_USER_IDS


@router.message(Command("send"))
async def send_message_to_user_command(message: types.Message, state: FSMContext):
    """
    Команда /send для отправки сообщения пользователю
    Формат: /send {user_id} {текст сообщения}
    """
    if not is_admin(message.from_user.id):
        await message.reply("❌ У вас нет прав администратора")
        return
    
    try:
        # Парсим команду
        parts = message.text.split(maxsplit=2)
        
        if len(parts) < 3:
            await message.reply(
                "📝 <b>Формат команды:</b>\n"
                "/send {user_id} {текст сообщения}\n\n"
                "<b>Пример:</b>\n"
                "/send 123456789 Привет! Как дела с растениями?",
                parse_mode="HTML"
            )
            return
        
        target_user_id = int(parts[1])
        message_text = parts[2]
        
        # Проверяем существование пользователя
        db = await get_db()
        user_info = await db.get_user_info_by_id(target_user_id)
        
        if not user_info:
            await message.reply(f"❌ Пользователь с ID {target_user_id} не найден")
            return
        
        # Отправляем сообщение пользователю
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        
        keyboard = [
            [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"reply_to_admin_{message.from_user.id}")]
        ]
        
        await message.bot.send_message(
            chat_id=target_user_id,
            text=f"💌 <b>Сообщение от администратора:</b>\n\n{message_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        # Сохраняем в базу
        message_id = await db.send_admin_message(
            from_user_id=message.from_user.id,
            to_user_id=target_user_id,
            message_text=message_text,
            context={"type": "admin_to_user"}
        )
        
        # Подтверждение админу
        username = user_info.get('username') or user_info.get('first_name') or f"user_{target_user_id}"
        
        await message.reply(
            f"✅ <b>Сообщение отправлено!</b>\n\n"
            f"👤 Кому: {username} (ID: {target_user_id})\n"
            f"📝 Текст: {message_text[:100]}{'...' if len(message_text) > 100 else ''}\n"
            f"🆔 Message ID: {message_id}",
            parse_mode="HTML"
        )
        
    except ValueError:
        await message.reply("❌ Неверный формат user_id. Должно быть число.")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}", exc_info=True)
        await message.reply(f"❌ Ошибка: {str(e)}")


@router.callback_query(F.data.startswith("reply_to_admin_"))
async def reply_to_admin_button(callback: types.CallbackQuery, state: FSMContext):
    """Пользователь нажал кнопку 'Ответить' на сообщение от админа"""
    try:
        admin_id = int(callback.data.split("_")[-1])
        
        await state.update_data(replying_to_admin=admin_id)
        await state.set_state(AdminStates.waiting_user_reply)
        
        await callback.message.answer(
            "✍️ <b>Напишите ваш ответ:</b>\n\n"
            "Ваше сообщение будет отправлено администратору.",
            parse_mode="HTML"
        )
        
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка reply_to_admin: {e}")
        await callback.answer("❌ Ошибка")


@router.message(StateFilter(AdminStates.waiting_user_reply))
async def handle_user_reply_to_admin(message: types.Message, state: FSMContext):
    """Обработка ответа пользователя админу"""
    try:
        data = await state.get_data()
        admin_id = data.get('replying_to_admin')
        
        if not admin_id:
            await message.reply("❌ Ошибка: потеряна информация о получателе")
            await state.clear()
            return
        
        user_id = message.from_user.id
        reply_text = message.text.strip()
        
        # Сохраняем в базу
        db = await get_db()
        message_id = await db.send_admin_message(
            from_user_id=user_id,
            to_user_id=admin_id,
            message_text=reply_text,
            context={"type": "user_to_admin"}
        )
        
        # Получаем информацию о пользователе
        user_info = await db.get_user_info_by_id(user_id)
        username = user_info.get('username') or "не указан"
        first_name = user_info.get('first_name') or f"user_{user_id}"
        
        # Отправляем админу
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        
        keyboard = [
            [InlineKeyboardButton(text="✉️ Ответить пользователю", callback_data=f"quick_reply_{user_id}")]
        ]
        
        admin_message = (
            f"📨 <b>Ответ от пользователя:</b>\n\n"
            f"👤 <b>Имя:</b> {first_name}\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
            f"👤 <b>Username:</b> @{username if username != 'не указан' else username}\n\n"
            f"💬 <b>Сообщение:</b>\n{reply_text}"
        )
        
        await message.bot.send_message(
            chat_id=admin_id,
            text=admin_message,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        # Подтверждение пользователю
        await message.reply(
            "✅ <b>Ваш ответ отправлен администратору!</b>",
            parse_mode="HTML"
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка обработки ответа: {e}", exc_info=True)
        await message.reply("❌ Ошибка отправки")
        await state.clear()


@router.callback_query(F.data.startswith("quick_reply_"))
async def quick_reply_button(callback: types.CallbackQuery, state: FSMContext):
    """Админ нажал кнопку 'Ответить пользователю'"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав")
        return
    
    try:
        target_user_id = int(callback.data.split("_")[-1])
        
        # Получаем информацию о пользователе
        db = await get_db()
        user_info = await db.get_user_info_by_id(target_user_id)
        
        if not user_info:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return
        
        username = user_info.get('username') or user_info.get('first_name') or f"user_{target_user_id}"
        
        await state.update_data(quick_reply_to=target_user_id)
        await state.set_state(AdminStates.waiting_admin_reply)
        
        await callback.message.answer(
            f"✍️ <b>Ответ пользователю {username}</b>\n\n"
            f"Напишите текст сообщения:",
            parse_mode="HTML"
        )
        
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка quick_reply: {e}")
        await callback.answer("❌ Ошибка")


@router.message(StateFilter(AdminStates.waiting_admin_reply))
async def handle_admin_quick_reply(message: types.Message, state: FSMContext):
    """Обработка быстрого ответа админа"""
    if not is_admin(message.from_user.id):
        await message.reply("❌ Нет прав")
        await state.clear()
        return
    
    try:
        data = await state.get_data()
        target_user_id = data.get('quick_reply_to')
        
        if not target_user_id:
            await message.reply("❌ Ошибка: потеряна информация о получателе")
            await state.clear()
            return
        
        reply_text = message.text.strip()
        
        # Отправляем сообщение пользователю
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        
        keyboard = [
            [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"reply_to_admin_{message.from_user.id}")]
        ]
        
        await message.bot.send_message(
            chat_id=target_user_id,
            text=f"💌 <b>Сообщение от администратора:</b>\n\n{reply_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
        # Сохраняем в базу
        db = await get_db()
        message_id = await db.send_admin_message(
            from_user_id=message.from_user.id,
            to_user_id=target_user_id,
            message_text=reply_text,
            context={"type": "admin_reply"}
        )
        
        # Подтверждение админу
        await message.reply(
            f"✅ <b>Сообщение отправлено!</b>\n\n"
            f"👤 Кому: ID {target_user_id}\n"
            f"🆔 Message ID: {message_id}",
            parse_mode="HTML"
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка отправки ответа: {e}", exc_info=True)
        await message.reply("❌ Ошибка отправки")
        await state.clear()


@router.message(Command("reply"))
async def reply_to_user_command(message: types.Message, state: FSMContext):
    """
    Альтернативная команда /reply для ответа пользователю
    Формат: /reply {user_id} {текст}
    """
    if not is_admin(message.from_user.id):
        await message.reply("❌ У вас нет прав администратора")
        return
    
    # Используем ту же логику что и /send
    await send_message_to_user_command(message, state)


@router.message(Command("messages"))
async def view_messages_command(message: types.Message):
    """Просмотр истории сообщений (для админов)"""
    if not is_admin(message.from_user.id):
        await message.reply("❌ У вас нет прав администратора")
        return
    
    try:
        db = await get_db()
        messages = await db.get_user_messages(message.from_user.id, limit=20)
        
        if not messages:
            await message.reply("📭 <b>История сообщений пуста</b>", parse_mode="HTML")
            return
        
        text = "📬 <b>История сообщений (последние 20):</b>\n\n"
        
        for msg in messages:
            date = msg['sent_at'].strftime('%d.%m %H:%M')
            
            if msg['from_user_id'] == message.from_user.id:
                # Исходящее
                to_name = msg.get('to_username') or msg.get('to_first_name') or f"user_{msg['to_user_id']}"
                direction = "→"
                text += f"<b>{date}</b> {direction} {to_name}\n"
            else:
                # Входящее
                from_name = msg.get('from_username') or msg.get('from_first_name') or f"user_{msg['from_user_id']}"
                direction = "←"
                text += f"<b>{date}</b> {direction} {from_name}\n"
            
            preview = msg['message_text'][:50] + "..." if len(msg['message_text']) > 50 else msg['message_text']
            text += f"   {preview}\n\n"
        
        await message.reply(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка просмотра сообщений: {e}", exc_info=True)
        await message.reply("❌ Ошибка загрузки")


@router.message(Command("users"))
async def list_users_command(message: types.Message):
    """Список активных пользователей (для админов)"""
    if not is_admin(message.from_user.id):
        await message.reply("❌ У вас нет прав администратора")
        return
    
    try:
        db = await get_db()
        
        async with db.pool.acquire() as conn:
            # Последние 20 активных пользователей
            users = await conn.fetch("""
                SELECT user_id, username, first_name, last_activity, 
                       plants_count, total_waterings, questions_asked
                FROM users
                WHERE last_activity IS NOT NULL
                ORDER BY last_activity DESC
                LIMIT 20
            """)
        
        if not users:
            await message.reply("📭 <b>Пользователи не найдены</b>", parse_mode="HTML")
            return
        
        text = "👥 <b>Последние 20 активных пользователей:</b>\n\n"
        
        for user in users:
            username = user['username'] or user['first_name'] or f"user_{user['user_id']}"
            last_activity = user['last_activity'].strftime('%d.%m %H:%M') if user['last_activity'] else 'никогда'
            
            text += f"👤 <b>{username}</b>\n"
            text += f"   🆔 ID: <code>{user['user_id']}</code>\n"
            text += f"   📅 Активность: {last_activity}\n"
            text += f"   🌱 Растений: {user['plants_count']}, 💧 Поливов: {user['total_waterings']}\n\n"
        
        await message.reply(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка списка пользователей: {e}", exc_info=True)
        await message.reply("❌ Ошибка загрузки")
