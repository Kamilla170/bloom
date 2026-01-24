import logging
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext

from keyboards.main_menu import main_menu, simple_back_menu
from states.user_states import PlantStates
from database import get_db

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    """Главное меню"""
    await callback.message.answer("🌱 <b>Главное меню</b>", parse_mode="HTML", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "add_plant")
async def add_plant_callback(callback: types.CallbackQuery):
    """Добавить растение"""
    await callback.message.answer("📸 <b>Пришлите фото растения</b>", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "analyze")
async def analyze_callback(callback: types.CallbackQuery):
    """Анализ растения"""
    await callback.message.answer("📸 <b>Пришлите фото для анализа</b>", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "reanalyze")
async def reanalyze_callback(callback: types.CallbackQuery):
    """Повторный анализ"""
    await callback.message.answer("📸 <b>Пришлите новое фото</b>", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "question")
async def question_callback(callback: types.CallbackQuery, state: FSMContext):
    """Спросить ИИ"""
    await callback.message.answer("🤖 <b>Спросите ИИ о растениях</b>\n\n✍️ Напишите ваш вопрос:", parse_mode="HTML")
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()


@router.callback_query(F.data == "ask_about")
async def ask_about_callback(callback: types.CallbackQuery, state: FSMContext):
    """Вопрос о текущем растении"""
    await callback.message.answer("🤖 <b>Спросите ИИ о растении</b>\n\n✍️ Напишите вопрос:", parse_mode="HTML")
    await state.set_state(PlantStates.waiting_question)
    await callback.answer()


@router.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    """Справка"""
    from handlers.commands import help_command
    await help_command(callback.message)
    await callback.answer()


@router.callback_query(F.data == "stats")
async def stats_callback(callback: types.CallbackQuery):
    """Статистика"""
    from handlers.commands import stats_command
    await stats_command(callback.message)
    await callback.answer()


@router.callback_query(F.data == "my_plants")
async def my_plants_callback(callback: types.CallbackQuery):
    """Моя коллекция"""
    from handlers.plants import show_plants_collection
    await show_plants_collection(callback)


@router.callback_query(F.data == "grow_from_scratch")
async def grow_from_scratch_callback(callback: types.CallbackQuery, state: FSMContext):
    """Выращивание с нуля"""
    await state.clear()
    
    await callback.message.answer(
        "🌿 <b>Выращиваем растение с нуля!</b>\n\n"
        "🌱 <b>Напишите, что хотите вырастить:</b>\n\n"
        "💡 <b>Примеры:</b> Базилик, Герань, Тюльпаны, Фикус, Помидоры\n\n"
        "✍️ Просто напишите название!",
        parse_mode="HTML"
    )
    
    await state.set_state(PlantStates.choosing_plant_to_grow)
    await callback.answer()


@router.callback_query(F.data == "save_plant")
async def save_plant_callback(callback: types.CallbackQuery):
    """Сохранить проанализированное растение"""
    from handlers.plants import save_plant_handler
    await save_plant_handler(callback)


@router.callback_query(F.data == "toggle_reminders")
async def toggle_reminders_callback(callback: types.CallbackQuery):
    """Переключить напоминания"""
    try:
        user_id = callback.from_user.id
        db = await get_db()
        
        async with db.pool.acquire() as conn:
            current = await conn.fetchrow("""
                SELECT reminder_enabled FROM user_settings WHERE user_id = $1
            """, user_id)
            
            if current:
                new_status = not current['reminder_enabled']
            else:
                new_status = False
            
            await conn.execute("""
                UPDATE user_settings
                SET reminder_enabled = $1
                WHERE user_id = $2
            """, new_status, user_id)
        
        status_text = "✅ включены" if new_status else "❌ выключены"
        
        await callback.message.answer(
            f"🔔 <b>Напоминания {status_text}</b>\n\n"
            f"Используйте /notifications для управления настройками",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка переключения: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()


@router.callback_query(F.data == "disable_monthly_reminders")
async def disable_monthly_reminders_callback(callback: types.CallbackQuery):
    """Отключить месячные напоминания"""
    try:
        user_id = callback.from_user.id
        db = await get_db()
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_settings
                SET monthly_photo_reminder = FALSE
                WHERE user_id = $1
            """, user_id)
        
        await callback.message.answer(
            "🔕 <b>Месячные напоминания об обновлении фото отключены</b>\n\n"
            "Вы можете включить их обратно в настройках.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка отключения напоминаний: {e}")
    
    await callback.answer()


@router.callback_query(F.data == "snooze_monthly_reminder")
async def snooze_monthly_reminder_callback(callback: types.CallbackQuery):
    """Отложить месячное напоминание"""
    try:
        from datetime import datetime, timedelta
        
        user_id = callback.from_user.id
        db = await get_db()
        
        week_ago = datetime.now() - timedelta(days=23)
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_settings
                SET last_monthly_reminder = $1
                WHERE user_id = $2
            """, week_ago, user_id)
        
        await callback.message.answer(
            "⏰ <b>Напомню через неделю!</b>\n\n"
            "Тогда еще раз предложу обновить фото растений.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка отложения напоминания: {e}")
    
    await callback.answer()


@router.callback_query(F.data == "feedback")
async def feedback_callback(callback: types.CallbackQuery):
    """Меню обратной связи"""
    from handlers.feedback import show_feedback_menu
    await show_feedback_menu(callback)
