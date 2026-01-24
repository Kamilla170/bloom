import logging
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext

from states.user_states import PlantStates
from database import get_db

logger = logging.getLogger(__name__)

router = Router()


async def start_onboarding(message: types.Message):
    """Онбординг для новых пользователей"""
    first_name = message.from_user.first_name or "друг"
    
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [InlineKeyboardButton(text="✨ Покажи пример", callback_data="onboarding_demo")],
        [InlineKeyboardButton(text="🚀 Хочу попробовать сразу", callback_data="onboarding_quick_start")],
    ]
    
    await message.answer(
        f"🌱 Отлично, {first_name}! Готов стать вашим садовым помощником!\n\n"
        "Давайте я покажу, как это работает на примере, а потом вы попробуете сами?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


@router.callback_query(F.data == "onboarding_demo")
async def onboarding_demo_callback(callback: types.CallbackQuery):
    """Демо анализа"""
    demo_text = (
        "🔍 <b>Смотрите! Вот как я анализирую растения:</b>\n\n"
        "🌿 <b>Фикус Бенджамина</b> (Ficus benjamina)\n"
        "🎯 <b>Уверенность:</b> 95%\n"
        "🌱 <b>Состояние:</b> Здоровое\n\n"
        "🔍 <b>Что видно:</b>\n"
        "✅ Листья: здоровые, зеленые\n"
        "❌ Почва: не видна в кадре\n\n"
        "💡 <b>Я отслеживаю изменения:</b>\n"
        "• Цветение → меняю режим полива\n"
        "• Стресс → даю срочные рекомендации\n"
        "• Активный рост → увеличиваю подкормку\n\n"
        "📸 <b>Месячные напоминания</b> обновить фото!"
    )
    
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [InlineKeyboardButton(text="📸 Проанализировать мое растение", callback_data="onboarding_try_analyze")],
        [InlineKeyboardButton(text="🌿 Вырастить что-то новое", callback_data="onboarding_try_grow")],
        [InlineKeyboardButton(text="🤖 Спросить ИИ о растениях", callback_data="onboarding_try_question")],
    ]
    
    await callback.message.answer(
        demo_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@router.callback_query(F.data == "onboarding_quick_start")
async def onboarding_quick_start_callback(callback: types.CallbackQuery):
    """Быстрый старт"""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [InlineKeyboardButton(text="📸 Проанализировать растение", callback_data="onboarding_try_analyze")],
        [InlineKeyboardButton(text="🌿 Вырастить с нуля", callback_data="onboarding_try_grow")],
        [InlineKeyboardButton(text="🤖 Спросить ИИ", callback_data="onboarding_try_question")],
        [InlineKeyboardButton(text="💡 Сначала покажи пример", callback_data="onboarding_demo")],
    ]
    
    await callback.message.answer(
        "🎯 <b>Отлично! С чего начнем?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@router.callback_query(F.data == "onboarding_try_analyze")
async def onboarding_try_analyze_callback(callback: types.CallbackQuery):
    """Попробовать анализ"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "📸 <b>Отлично! Пришлите фото вашего растения</b>\n\n"
        "💡 <b>Советы:</b>\n"
        "• Дневной свет\n"
        "• Покажите листья и общий вид\n"
        "• Включите почву если возможно\n\n"
        "📱 Жду ваше фото!",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "onboarding_try_grow")
async def onboarding_try_grow_callback(callback: types.CallbackQuery, state: FSMContext):
    """Попробовать выращивание"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "🌿 <b>Отлично! Выращиваем с нуля!</b>\n\n"
        "🌱 <b>Напишите, что хотите вырастить:</b>\n\n"
        "💡 Примеры: Базилик, Герань, Тюльпаны, Фикус\n\n"
        "✍️ Напишите название:",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.choosing_plant_to_grow)
    await callback.answer()


@router.callback_query(F.data == "onboarding_try_question")
async def onboarding_try_question_callback(callback: types.CallbackQuery, state: FSMContext):
    """Попробовать вопрос"""
    await mark_onboarding_completed(callback.from_user.id)
    
    await callback.message.answer(
        "🤖 <b>Спросите ИИ о растениях</b>\n\n"
        "💡 Помогу с:\n"
        "• Проблемами листьев\n"
        "• Режимом полива\n"
        "• Пересадкой\n"
        "• Болезнями\n\n"
        "✍️ Напишите вопрос:",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()


async def mark_onboarding_completed(user_id: int):
    """Отметить онбординг завершенным"""
    try:
        db = await get_db()
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET onboarding_completed = TRUE WHERE user_id = $1",
                user_id
            )
        logger.info(f"✅ Онбординг завершен для пользователя {user_id}")
    except Exception as e:
        logger.error(f"Ошибка онбординга: {e}")
