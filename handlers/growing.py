import logging
from datetime import timedelta
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from states.user_states import PlantStates
from services.ai_service import generate_growing_plan
from keyboards.plant_menu import growing_plant_menu, delete_confirmation
from keyboards.main_menu import simple_back_menu
from database import get_db
from utils.time_utils import get_moscow_now

logger = logging.getLogger(__name__)

router = Router()


@router.message(StateFilter(PlantStates.choosing_plant_to_grow))
async def handle_plant_choice_for_growing(message: types.Message, state: FSMContext):
    """Обработка выбора растения для выращивания"""
    try:
        plant_name = message.text.strip()
        
        if len(plant_name) < 2:
            await message.reply("🤔 Слишком короткое название")
            return
        
        processing_msg = await message.reply(
            f"🧠 <b>Готовлю план выращивания...</b>\n\n"
            f"🌱 Растение: {plant_name}",
            parse_mode="HTML"
        )
        
        growing_plan, task_calendar = await generate_growing_plan(plant_name)
        
        await processing_msg.delete()
        
        if growing_plan and task_calendar:
            await state.update_data(
                plant_name=plant_name,
                growing_plan=growing_plan,
                task_calendar=task_calendar
            )
            
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            
            keyboard = [
                [InlineKeyboardButton(text="✅ Понятно, начинаем!", callback_data="confirm_growing_plan")],
                [InlineKeyboardButton(text="🔄 Другое растение", callback_data="grow_from_scratch")],
            ]
            
            await message.reply(
                f"🌱 <b>План готов!</b>\n\n{growing_plan}\n\n📋 Готовы начать?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        else:
            await message.reply(
                f"🤔 Не удалось составить план для '{plant_name}'",
                reply_markup=simple_back_menu()
            )
            await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка выбора растения: {e}")
        await message.reply("❌ Ошибка обработки", reply_markup=simple_back_menu())
        await state.clear()


@router.callback_query(F.data == "confirm_growing_plan")
async def confirm_growing_plan_callback(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение плана выращивания"""
    try:
        data = await state.get_data()
        plant_name = data.get('plant_name')
        growing_plan = data.get('growing_plan')
        task_calendar = data.get('task_calendar')
        
        if not all([plant_name, growing_plan, task_calendar]):
            await callback.message.answer("❌ Данные потеряны. Начните заново.")
            await state.clear()
            await callback.answer()
            return
        
        user_id = callback.from_user.id
        db = await get_db()
        
        # Создаем выращиваемое растение
        growing_id = await db.create_growing_plant(
            user_id=user_id,
            plant_name=plant_name,
            growth_method="from_seed",
            growing_plan=growing_plan,
            task_calendar=task_calendar
        )
        
        # Создаем первое напоминание
        first_task_date = get_moscow_now() + timedelta(days=1)
        first_task_date_naive = first_task_date.replace(tzinfo=None)
        
        await db.create_growing_reminder(
            growing_id=growing_id,
            user_id=user_id,
            reminder_type="task",
            next_date=first_task_date_naive,
            stage_number=1,
            task_day=1
        )
        
        from keyboards.main_menu import main_menu
        
        await callback.message.answer(
            f"✅ <b>Выращивание началось!</b>\n\n"
            f"🌱 <b>{plant_name}</b> добавлено в коллекцию\n\n"
            f"📅 Первое напоминание завтра\n"
            f"📸 Не забывайте фотографировать прогресс!",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка создания выращивания: {e}")
        await callback.message.answer("❌ Ошибка создания")
        await state.clear()
    
    await callback.answer()


@router.callback_query(F.data.startswith("edit_growing_"))
async def edit_growing_callback(callback: types.CallbackQuery):
    """Меню редактирования выращиваемого растения"""
    try:
        growing_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        growing_plant = await db.get_growing_plant_by_id(growing_id, user_id)
        
        if not growing_plant:
            await callback.answer("❌ Растение не найдено", show_alert=True)
            return
        
        plant_name = growing_plant['plant_name']
        current_stage = growing_plant['current_stage']
        total_stages = growing_plant['total_stages']
        status = growing_plant['status']
        started_date = growing_plant['started_date']
        
        days_growing = (get_moscow_now().date() - started_date.date()).days
        
        stage_name = growing_plant.get('current_stage_name', f'Этап {current_stage + 1}')
        
        await callback.message.answer(
            f"⚙️ <b>Управление выращиванием</b>\n\n"
            f"🌱 <b>{plant_name}</b>\n"
            f"📅 День {days_growing} выращивания\n"
            f"📊 Этап: {current_stage}/{total_stages}\n"
            f"🏷️ {stage_name}\n"
            f"⚡ Статус: {status}\n\n"
            f"Выберите действие:",
            parse_mode="HTML",
            reply_markup=growing_plant_menu(growing_id)
        )
        
    except Exception as e:
        logger.error(f"Ошибка меню выращивания: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.callback_query(F.data.startswith("delete_growing_"))
async def delete_growing_callback(callback: types.CallbackQuery):
    """Удаление выращиваемого растения"""
    try:
        growing_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        growing_plant = await db.get_growing_plant_by_id(growing_id, user_id)
        
        if not growing_plant:
            await callback.answer("❌ Растение не найдено", show_alert=True)
            return
        
        plant_name = growing_plant['plant_name']
        
        await callback.message.answer(
            f"🗑️ <b>Удаление выращивания</b>\n\n"
            f"🌱 {plant_name}\n\n"
            f"⚠️ Это действие нельзя отменить\n\n"
            f"❓ Вы уверены?",
            parse_mode="HTML",
            reply_markup=delete_confirmation(growing_id, is_growing=True)
        )
        
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_growing_"))
async def confirm_delete_growing_callback(callback: types.CallbackQuery):
    """Подтверждение удаления выращиваемого растения"""
    try:
        growing_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        growing_plant = await db.get_growing_plant_by_id(growing_id, user_id)
        
        if growing_plant:
            plant_name = growing_plant['plant_name']
            
            # Удаляем из базы
            async with db.pool.acquire() as conn:
                await conn.execute("""
                    DELETE FROM growing_plants
                    WHERE id = $1 AND user_id = $2
                """, growing_id, user_id)
            
            await callback.message.answer(
                f"🗑️ <b>Выращивание удалено</b>\n\n"
                f"❌ {plant_name} удалено из коллекции",
                parse_mode="HTML",
                reply_markup=simple_back_menu()
            )
        else:
            await callback.answer("❌ Растение не найдено", show_alert=True)
        
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()
