import logging
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from states.user_states import FeedbackStates
from keyboards.main_menu import main_menu
from database import get_db

logger = logging.getLogger(__name__)

router = Router()


async def show_feedback_menu(callback: types.CallbackQuery):
    """Показать меню обратной связи"""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [InlineKeyboardButton(text="🐛 Сообщить о баге", callback_data="feedback_bug")],
        [InlineKeyboardButton(text="❌ Неточный анализ", callback_data="feedback_analysis_error")],
        [InlineKeyboardButton(text="💡 Предложение", callback_data="feedback_suggestion")],
        [InlineKeyboardButton(text="⭐ Общий отзыв", callback_data="feedback_review")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    
    await callback.message.answer(
        "📝 <b>Обратная связь</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("feedback_"))
async def feedback_type_callback(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа обратной связи"""
    feedback_type = callback.data.replace("feedback_", "")
    
    type_messages = {
        "bug": "🐛 <b>Сообщить о баге</b>\n\nОпишите проблему:",
        "analysis_error": "❌ <b>Неточный анализ</b>\n\nРасскажите о неправильном определении:",
        "suggestion": "💡 <b>Предложение</b>\n\nПоделитесь идеей:",
        "review": "⭐ <b>Общий отзыв</b>\n\nПоделитесь впечатлениями:"
    }
    
    await state.update_data(feedback_type=feedback_type)
    await state.set_state(FeedbackStates.writing_message)
    
    await callback.message.answer(
        type_messages.get(feedback_type, "Напишите сообщение:"),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(StateFilter(FeedbackStates.writing_message))
async def handle_feedback_message(message: types.Message, state: FSMContext):
    """Обработка сообщения обратной связи"""
    try:
        feedback_text = message.text.strip() if message.text else ""
        feedback_photo = None
        if message.photo:
            feedback_photo = message.photo[-1].file_id
        
        if not feedback_text and not feedback_photo:
            await message.reply("📝 Напишите сообщение или приложите фото")
            return
        
        if feedback_text and len(feedback_text) < 5:
            await message.reply("📝 Слишком короткое (минимум 5 символов)")
            return
        
        data = await state.get_data()
        feedback_type = data.get('feedback_type', 'review')
        
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.first_name or f"user_{user_id}"
        
        db = await get_db()
        await db.save_feedback(
            user_id=user_id,
            username=username,
            feedback_type=feedback_type,
            message=feedback_text or "Фото без комментария",
            photo_file_id=feedback_photo
        )
        
        await message.answer(
            "✅ <b>Спасибо за отзыв!</b>\n\nВаше сообщение поможет улучшить бота.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка обратной связи: {e}")
        await message.reply("❌ Ошибка обработки")
        await state.clear()
