import logging
from datetime import datetime, timedelta
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from states.user_states import PlantStates
from services.plant_service import (
    temp_analyses, save_analyzed_plant, get_user_plants_list, 
    water_plant, water_all_plants, delete_plant, rename_plant,
    get_plant_details, get_plant_state_history
)
from services.subscription_service import check_limit
from keyboards.main_menu import main_menu, simple_back_menu
from keyboards.plant_menu import plant_control_menu, delete_confirmation
from config import STATE_EMOJI, STATE_NAMES
from database import get_db
from utils.date_parser import parse_user_date, format_date_ago, get_days_offset

logger = logging.getLogger(__name__)

router = Router()


def last_watering_keyboard():
    """Клавиатура для выбора даты последнего полива"""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [
            InlineKeyboardButton(text="💧 Сегодня", callback_data="last_water_today"),
            InlineKeyboardButton(text="💧 Вчера", callback_data="last_water_yesterday")
        ],
        [
            InlineKeyboardButton(text="💧 2-3 дня назад", callback_data="last_water_2_3_days"),
            InlineKeyboardButton(text="💧 Неделю назад", callback_data="last_water_week")
        ],
        [
            InlineKeyboardButton(text="🤷 Не помню / Пропустить", callback_data="last_water_skip")
        ]
    ]
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_plants_list(message: types.Message):
    """Показать список растений (для команды)"""
    user_id = message.from_user.id
    
    try:
        plants = await get_user_plants_list(user_id, limit=15)
        
        if not plants:
            await message.answer(
                "🌱 <b>Коллекция пуста</b>\n\n"
                "Добавьте первое растение:\n"
                "📸 Пришлите фото или используйте /add",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            return
        
        await send_plants_list(message, plants, user_id)
        
    except Exception as e:
        logger.error(f"Ошибка коллекции: {e}")
        await message.answer("❌ Ошибка загрузки")


async def show_plants_collection(callback: types.CallbackQuery):
    """Показать коллекцию (для callback)"""
    user_id = callback.from_user.id
    
    try:
        plants = await get_user_plants_list(user_id, limit=15)
        
        if not plants:
            await callback.message.answer(
                "🌱 <b>Коллекция пуста</b>\n\nДобавьте первое растение!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            await callback.answer()
            return
        
        await send_plants_list(callback.message, plants, user_id)
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка коллекции: {e}")
        await callback.message.answer("❌ Ошибка загрузки")
        await callback.answer()


async def send_plants_list(message: types.Message, plants: list, user_id: int):
    """Отправить список растений"""
    text = f"🌿 <b>Ваша коллекция ({len(plants)} растений):</b>\n\n"
    
    keyboard_buttons = []
    
    for i, plant in enumerate(plants, 1):
        plant_name = plant['display_name']
        emoji = plant['emoji']
        
        if plant.get('type') == 'growing':
            stage_info = plant.get('stage_info', 'В процессе')
            text += f"{i}. {emoji} <b>{plant_name}</b>\n   {stage_info}\n\n"
            callback_data = f"edit_growing_{plant['growing_id']}"
        else:
            water_status = plant.get('water_status', '')
            text += f"{i}. {emoji} <b>{plant_name}</b>\n   💧 {water_status}\n\n"
            callback_data = f"edit_plant_{plant['id']}"
        
        short_name = plant_name[:15] + "..." if len(plant_name) > 15 else plant_name
        
        from aiogram.types import InlineKeyboardButton
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"⚙️ {short_name}", callback_data=callback_data)
        ])
    
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard_buttons.extend([
        [InlineKeyboardButton(text="💧 Полить все", callback_data="water_plants")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ])
    
    await message.answer(
        text, 
        parse_mode="HTML", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    )


@router.callback_query(F.data.startswith("edit_plant_"))
async def edit_plant_callback(callback: types.CallbackQuery):
    """Меню редактирования обычного растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        details = await get_plant_details(plant_id, user_id)
        
        if not details:
            await callback.answer("❌ Растение не найдено", show_alert=True)
            return
        
        text = f"""
⚙️ <b>Управление растением</b>

🌱 <b>{details['plant_name']}</b>
{details['state_emoji']} <b>Состояние:</b> {details['state_name']}
💧 {details['water_status']}
⏰ Интервал: {details['watering_interval']} дней
🔄 Изменений: {details['state_changes_count']}

Выберите действие:
"""
        
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=plant_control_menu(plant_id)
        )
        
    except Exception as e:
        logger.error(f"Ошибка меню: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.callback_query(F.data.startswith("water_plant_"))
async def water_single_plant_callback(callback: types.CallbackQuery):
    """Полив одного растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        result = await water_plant(user_id, plant_id)
        
        if result["success"]:
            await callback.message.answer(
                f"💧 <b>Полив отмечен!</b>\n\n"
                f"🌱 <b>{result['plant_name']}</b> полито {result['time']}\n"
                f"⏰ Следующее напоминание через {result['next_watering_days']} дней",
                parse_mode="HTML"
            )
            
            # === КОНТЕКСТНАЯ ПОДСКАЗКА: после первого полива ===
            from handlers.onboarding import send_tip_if_needed, TIP_AFTER_WATERING
            
            async def _send_watering_tip():
                await callback.message.answer(TIP_AFTER_WATERING)
            
            await send_tip_if_needed(user_id, 'watering', _send_watering_tip)
            
        else:
            await callback.answer(f"❌ {result['error']}", show_alert=True)
        
    except Exception as e:
        logger.error(f"Ошибка полива: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.callback_query(F.data == "water_plants")
async def water_plants_callback(callback: types.CallbackQuery):
    """Полив всех растений"""
    user_id = callback.from_user.id
    
    try:
        result = await water_all_plants(user_id)
        
        if result["success"]:
            await callback.message.answer(
                "💧 <b>Полив отмечен!</b>\n\nВсе растения политы",
                parse_mode="HTML",
                reply_markup=simple_back_menu()
            )
            
            # === КОНТЕКСТНАЯ ПОДСКАЗКА: после первого полива ===
            from handlers.onboarding import send_tip_if_needed, TIP_AFTER_WATERING
            
            async def _send_watering_tip():
                await callback.message.answer(TIP_AFTER_WATERING)
            
            await send_tip_if_needed(user_id, 'watering', _send_watering_tip)
            
        else:
            await callback.message.answer("❌ Ошибка")
        
    except Exception as e:
        logger.error(f"Ошибка полива: {e}")
        await callback.message.answer("❌ Ошибка")
    
    await callback.answer()


@router.callback_query(F.data.startswith("update_state_"))
async def update_state_callback(callback: types.CallbackQuery, state: FSMContext):
    """Обновить состояние растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        await state.update_data(
            updating_plant_state=True,
            state_plant_id=plant_id
        )
        
        await callback.message.answer(
            "📸 <b>Обновление состояния растения</b>\n\n"
            "Пришлите новое фото растения, и я:\n"
            "• Сравню с предыдущим состоянием\n"
            "• Определю изменения\n"
            "• Дам актуальные рекомендации\n\n"
            "📷 Пришлите фото сейчас:",
            parse_mode="HTML"
        )
        
        await state.set_state(PlantStates.waiting_state_update_photo)
        
    except Exception as e:
        logger.error(f"Ошибка обновления состояния: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.callback_query(F.data.startswith("view_state_history_"))
async def view_state_history_callback(callback: types.CallbackQuery):
    """Просмотр истории состояний"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        details = await get_plant_details(plant_id, user_id)
        if not details:
            await callback.answer("❌ Растение не найдено", show_alert=True)
            return
        
        history = await get_plant_state_history(plant_id, limit=10)
        
        text = f"📊 <b>История состояний: {details['plant_name']}</b>\n\n"
        text += f"{details['state_emoji']} <b>Текущее:</b> {details['state_name']}\n"
        text += f"🔄 <b>Всего изменений:</b> {details['state_changes_count']}\n\n"
        
        if history:
            text += f"📖 <b>История изменений:</b>\n\n"
            for entry in history[:5]:
                date_str = entry['date'].strftime('%d.%m %H:%M')
                
                text += f"📅 <b>{date_str}</b>\n"
                if entry['from_state']:
                    text += f"   {entry['emoji_from']} → {entry['emoji_to']}\n"
                else:
                    text += f"   {entry['emoji_to']} Добавлено\n"
                
                if entry['reason']:
                    reason = entry['reason'][:50] + "..." if len(entry['reason']) > 50 else entry['reason']
                    text += f"   💬 {reason}\n"
                
                text += "\n"
        else:
            text += "📝 История пока пуста\n\n"
        
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [
            [InlineKeyboardButton(text="📸 Обновить состояние", callback_data=f"update_state_{plant_id}")],
            [InlineKeyboardButton(text="🌿 К растению", callback_data=f"edit_plant_{plant_id}")],
        ]
        
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        logger.error(f"Ошибка просмотра истории: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.callback_query(F.data.startswith("rename_plant_"))
async def rename_plant_callback(callback: types.CallbackQuery, state: FSMContext):
    """Переименование растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        details = await get_plant_details(plant_id, user_id)
        
        if not details:
            await callback.answer("❌ Растение не найдено", show_alert=True)
            return
        
        current_name = details['plant_name']
        
        await state.update_data(editing_plant_id=plant_id)
        await state.set_state(PlantStates.editing_plant_name)
        
        await callback.message.answer(
            f"✏️ <b>Изменение названия</b>\n\n"
            f"🌱 Текущее: {current_name}\n\n"
            f"✍️ Напишите новое название:",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка переименования: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.message(StateFilter(PlantStates.editing_plant_name))
async def handle_plant_rename(message: types.Message, state: FSMContext):
    """Обработка нового названия"""
    try:
        new_name = message.text.strip()
        
        data = await state.get_data()
        plant_id = data.get('editing_plant_id')
        
        if not plant_id:
            await message.reply("❌ Ошибка данных")
            await state.clear()
            return
        
        user_id = message.from_user.id
        
        result = await rename_plant(user_id, plant_id, new_name)
        
        if result["success"]:
            await message.reply(
                f"✅ <b>Название изменено!</b>\n\n"
                f"🌱 Новое название: <b>{result['new_name']}</b>",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
        else:
            await message.reply(f"❌ {result['error']}")
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка переименования: {e}")
        await message.reply("❌ Ошибка сохранения")
        await state.clear()


@router.callback_query(F.data.startswith("delete_plant_"))
async def delete_plant_callback(callback: types.CallbackQuery):
    """Удаление растения"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        details = await get_plant_details(plant_id, user_id)
        
        if not details:
            await callback.answer("❌ Растение не найдено", show_alert=True)
            return
        
        plant_name = details['plant_name']
        
        await callback.message.answer(
            f"🗑️ <b>Удаление растения</b>\n\n"
            f"🌱 {plant_name}\n\n"
            f"⚠️ Это действие нельзя отменить\n\n"
            f"❓ Вы уверены?",
            parse_mode="HTML",
            reply_markup=delete_confirmation(plant_id, is_growing=False)
        )
        
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_plant_"))
async def confirm_delete_callback(callback: types.CallbackQuery):
    """Подтверждение удаления"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        result = await delete_plant(user_id, plant_id)
        
        if result["success"]:
            await callback.message.answer(
                f"🗑️ <b>Растение удалено</b>\n\n"
                f"❌ {result['plant_name']} удалено из коллекции",
                parse_mode="HTML",
                reply_markup=simple_back_menu()
            )
        else:
            await callback.answer("❌ Растение не найдено", show_alert=True)
        
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.callback_query(F.data.startswith("snooze_"))
async def snooze_reminder_callback(callback: types.CallbackQuery):
    """Отложить напоминание"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        from services.reminder_service import create_plant_reminder
        
        details = await get_plant_details(plant_id, user_id)
        
        if details:
            plant_name = details['plant_name']
            await create_plant_reminder(plant_id, user_id, 1)
            
            await callback.message.answer(
                f"⏰ <b>Напоминание отложено</b>\n\n"
                f"🌱 {plant_name}\n"
                f"📅 Завтра напомню полить",
                parse_mode="HTML"
            )
        
    except Exception as e:
        logger.error(f"Ошибка отложения: {e}")
        await callback.answer("❌ Ошибка")
    
    await callback.answer()


# === СОХРАНЕНИЕ РАСТЕНИЯ С ВЫБОРОМ ДАТЫ ПОЛИВА ===

async def save_plant_handler(callback: types.CallbackQuery, state: FSMContext):
    """Начало сохранения - показываем выбор даты последнего полива"""
    user_id = callback.from_user.id
    
    logger.info(f"💾 save_plant_handler вызван для user_id={user_id}")
    
    if user_id not in temp_analyses:
        await callback.message.answer("❌ Нет данных. Сначала проанализируйте растение")
        await callback.answer()
        return
    
    # Проверка лимита растений
    allowed, error_msg = await check_limit(user_id, 'plants')
    if not allowed:
        from handlers.subscription import send_limit_message
        await send_limit_message(callback, error_msg)
        return
    
    analysis_data = temp_analyses[user_id]
    plant_name = analysis_data.get("plant_name", "растение")
    
    # Устанавливаем состояние ожидания даты полива
    await state.update_data(saving_plant=True)
    await state.set_state(PlantStates.waiting_last_watering)
    
    logger.info(f"✅ Состояние установлено: waiting_last_watering для user_id={user_id}")
    
    await callback.message.answer(
        f"💧 <b>Когда последний раз поливали {plant_name}?</b>\n\n"
        f"Это поможет точнее рассчитать следующий полив.\n\n"
        f"💡 <i>Можете нажать кнопку или написать дату в чат</i>\n"
        f"<i>Примеры: «вчера», «3 дня назад», «25.01»</i>",
        parse_mode="HTML",
        reply_markup=last_watering_keyboard()
    )
    
    await callback.answer()


@router.callback_query(F.data.startswith("last_water_"))
async def handle_last_water_choice(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора даты полива кнопкой"""
    user_id = callback.from_user.id
    choice = callback.data.replace("last_water_", "")
    
    if user_id not in temp_analyses:
        await callback.message.answer("❌ Данные потеряны. Проанализируйте растение заново.")
        await state.clear()
        await callback.answer()
        return
    
    # Определяем дату последнего полива
    now = datetime.now()
    last_watered = None
    
    if choice == "today":
        last_watered = now
    elif choice == "yesterday":
        last_watered = now - timedelta(days=1)
    elif choice == "2_3_days":
        last_watered = now - timedelta(days=2)
    elif choice == "week":
        last_watered = now - timedelta(days=7)
    elif choice == "skip":
        last_watered = None  # Не устанавливаем
    
    # Сохраняем растение
    await finish_save_plant(callback.message, user_id, last_watered, state)
    await callback.answer()


@router.message(StateFilter(PlantStates.waiting_last_watering))
async def handle_last_water_text(message: types.Message, state: FSMContext):
    """Обработка текстового ввода даты полива"""
    user_id = message.from_user.id
    
    logger.info(f"📅 handle_last_water_text вызван для user_id={user_id}, текст='{message.text}'")
    
    if user_id not in temp_analyses:
        await message.reply("❌ Данные потеряны. Проанализируйте растение заново.")
        await state.clear()
        return
    
    # Пробуем распарсить дату
    parsed_date = parse_user_date(message.text)
    
    logger.info(f"📅 Результат парсинга: {parsed_date}")
    
    if parsed_date:
        # Успешно распарсили
        await finish_save_plant(message, user_id, parsed_date, state)
    else:
        # Не удалось распарсить - просим уточнить
        await message.reply(
            "🤔 <b>Не могу понять дату</b>\n\n"
            "Попробуйте написать иначе:\n"
            "• <i>вчера</i>\n"
            "• <i>3 дня назад</i>\n"
            "• <i>25.01</i> или <i>25 января</i>\n\n"
            "Или нажмите одну из кнопок выше ☝️",
            parse_mode="HTML"
        )


async def finish_save_plant(message_or_callback, user_id: int, last_watered: datetime, state: FSMContext):
    """Завершение сохранения растения"""
    try:
        analysis_data = temp_analyses[user_id]
        
        # Передаём дату последнего полива в save_analyzed_plant
        result = await save_analyzed_plant(user_id, analysis_data, last_watered=last_watered)
        
        if result["success"]:
            del temp_analyses[user_id]
            
            # Формируем сообщение об успехе
            success_text = f"✅ <b>Растение добавлено!</b>\n\n"
            success_text += f"🌱 <b>{result['plant_name']}</b> в вашей коллекции\n"
            success_text += f"{result['state_emoji']} <b>Состояние:</b> {result['state_name']}\n"
            
            if last_watered:
                water_ago = format_date_ago(last_watered)
                success_text += f"💧 <b>Последний полив:</b> {water_ago}\n"
            
            success_text += f"⏰ <b>Следующий полив:</b> через {result['next_watering_days']} дней\n\n"
            success_text += f"🧠 <b>Система памяти активирована!</b>\n"
            success_text += f"Теперь я буду помнить всю историю этого растения"
            
            # Отправляем сообщение
            if isinstance(message_or_callback, types.Message):
                await message_or_callback.answer(success_text, parse_mode="HTML", reply_markup=main_menu())
            else:
                await message_or_callback.answer(success_text, parse_mode="HTML", reply_markup=main_menu())
            
            # === КОНТЕКСТНАЯ ПОДСКАЗКА: после первого сохранения ===
            from handlers.onboarding import send_tip_if_needed, TIP_AFTER_SAVE
            
            async def _send_save_tip():
                if isinstance(message_or_callback, types.Message):
                    await message_or_callback.answer(TIP_AFTER_SAVE)
                else:
                    await message_or_callback.answer(TIP_AFTER_SAVE)
            
            await send_tip_if_needed(user_id, 'save', _send_save_tip)
            
        else:
            error_msg = f"❌ {result['error']}"
            if isinstance(message_or_callback, types.Message):
                await message_or_callback.answer(error_msg)
            else:
                await message_or_callback.answer(error_msg)
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}", exc_info=True)
        error_msg = "❌ Ошибка сохранения"
        if isinstance(message_or_callback, types.Message):
            await message_or_callback.answer(error_msg)
        else:
            await message_or_callback.answer(error_msg)
        await state.clear()
