import logging
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from database import get_db
from keyboards.main_menu import main_menu
from states.user_states import PlantStates

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("start"))
async def start_command(message: types.Message):
    """Команда /start с онбордингом"""
    user_id = message.from_user.id
    
    logger.info(f"📩 Получена команда /start от пользователя {user_id}")
    
    try:
        db = await get_db()
        
        async with db.pool.acquire() as conn:
            existing_user = await conn.fetchrow(
                "SELECT user_id FROM users WHERE user_id = $1", user_id
            )
            
            if not existing_user:
                await db.add_user(
                    user_id=user_id,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name
                )
                
                logger.info(f"✅ Новый пользователь {user_id} добавлен")
                
                # Импортируем здесь чтобы избежать циклических импортов
                from handlers.onboarding import start_onboarding
                await start_onboarding(message)
                return
            else:
                logger.info(f"✅ Возвращающийся пользователь {user_id}")
                await show_returning_user_welcome(message)
                return
                
    except Exception as e:
        logger.error(f"❌ Ошибка /start: {e}", exc_info=True)
        await show_returning_user_welcome(message)


async def show_returning_user_welcome(message: types.Message):
    """Приветствие для возвращающихся"""
    first_name = message.from_user.first_name or "друг"
    
    await message.answer(
        f"🌱 С возвращением, {first_name}!\n\n"
        "Что будем делать с растениями сегодня?",
        reply_markup=main_menu()
    )


@router.message(Command("add"))
async def add_command(message: types.Message):
    """Команда /add"""
    await message.answer(
        "📸 <b>Добавление растения</b>\n\n"
        "Пришлите фото вашего растения, и я:\n"
        "• Определю вид\n"
        "• Проанализирую состояние\n"
        "• Дам рекомендации по уходу\n\n"
        "📷 Жду ваше фото!",
        parse_mode="HTML"
    )


@router.message(Command("grow"))
async def grow_command(message: types.Message, state: FSMContext):
    """Команда /grow"""
    await message.answer(
        "🌿 <b>Выращиваем с нуля!</b>\n\n"
        "🌱 Напишите, что хотите вырастить:",
        parse_mode="HTML"
    )
    await state.set_state(PlantStates.choosing_plant_to_grow)


@router.message(Command("analyze"))
async def analyze_command(message: types.Message):
    """Команда /analyze"""
    await message.answer(
        "🔍 <b>Анализ растения</b>\n\n"
        "Пришлите фото растения для детального анализа:\n"
        "• Определение вида\n"
        "• Оценка состояния\n"
        "• Проблемы и решения\n"
        "• Рекомендации по уходу\n\n"
        "📸 Пришлите фото сейчас:",
        parse_mode="HTML"
    )


@router.message(Command("question"))
async def question_command(message: types.Message, state: FSMContext):
    """Команда /question"""
    await message.answer(
        "❓ <b>Задайте вопрос о растениях</b>\n\n"
        "💡 Я помогу с:\n"
        "• Проблемами листьев\n"
        "• Режимом полива\n"
        "• Пересадкой\n"
        "• Болезнями\n"
        "• Удобрениями\n\n"
        "✍️ Напишите ваш вопрос:",
        parse_mode="HTML"
    )
    await state.set_state(PlantStates.waiting_question)


@router.message(Command("plants"))
async def plants_command(message: types.Message):
    """Команда /plants"""
    from handlers.plants import show_plants_list
    await show_plants_list(message)


@router.message(Command("notifications"))
async def notifications_command(message: types.Message):
    """Команда /notifications"""
    user_id = message.from_user.id
    
    try:
        db = await get_db()
        settings = await db.get_user_reminder_settings(user_id)
        
        if not settings:
            settings = {
                'reminder_enabled': True,
                'reminder_time': '09:00',
                'timezone': 'Europe/Moscow'
            }
        
        status = "✅ Включены" if settings['reminder_enabled'] else "❌ Выключены"
        
        text = f"""
🔔 <b>Настройки уведомлений</b>

📊 <b>Статус:</b> {status}
⏰ <b>Время:</b> {settings['reminder_time']} МСК
🌍 <b>Часовой пояс:</b> {settings['timezone']}

<b>Типы напоминаний:</b>
💧 Полив растений - ежедневно в 9:00
📸 Обновление фото - раз в месяц в 10:00
🌱 Задачи выращивания - по календарю

💡 <b>Управление:</b>
Напоминания адаптируются под состояние растений!
"""
        
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        
        keyboard = [
            [
                InlineKeyboardButton(
                    text="✅ Включить" if not settings['reminder_enabled'] else "❌ Выключить",
                    callback_data="toggle_reminders"
                )
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ]
        
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        
    except Exception as e:
        logger.error(f"Ошибка настроек: {e}")
        await message.answer("❌ Ошибка загрузки настроек")


@router.message(Command("stats"))
async def stats_command(message: types.Message):
    """Команда /stats"""
    user_id = message.from_user.id
    
    try:
        db = await get_db()
        stats = await db.get_user_stats(user_id)
        
        stats_text = f"📊 <b>Ваша статистика</b>\n\n"
        stats_text += f"🌱 <b>Растений:</b> {stats['total_plants']}\n"
        stats_text += f"💧 <b>Поливов:</b> {stats['total_waterings']}\n"
        
        if stats['total_growing'] > 0:
            stats_text += f"\n🌿 <b>Выращивание:</b>\n"
            stats_text += f"• Активных: {stats['active_growing']}\n"
            stats_text += f"• Завершенных: {stats['completed_growing']}\n"
        
        if stats['first_plant_date']:
            from datetime import datetime
            days_using = (datetime.now().date() - stats['first_plant_date'].date()).days
            stats_text += f"\n📅 <b>Используете бота:</b> {days_using} дней\n"
        
        stats_text += f"\n🎯 <b>Продолжайте ухаживать за растениями!</b>"
        
        await message.answer(
            stats_text,
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        
    except Exception as e:
        logger.error(f"Ошибка статистики: {e}")
        await message.answer("❌ Ошибка загрузки статистики", reply_markup=main_menu())


@router.message(Command("test_stats"))
async def test_stats_command(message: types.Message):
    """Тестовая команда для проверки системы статистики (только для админов)"""
    from config import ADMIN_USER_IDS
    
    user_id = message.from_user.id
    
    # Проверка прав администратора
    if user_id not in ADMIN_USER_IDS:
        await message.answer(
            f"❌ Эта команда доступна только администраторам\n\n"
            f"🔑 Ваш ID: <code>{user_id}</code>\n"
            f"👥 Список админов: {ADMIN_USER_IDS}",
            parse_mode="HTML"
        )
        return
    
    try:
        status_msg = await message.answer("📊 <b>Шаг 1/6:</b> Начинаю генерацию статистики...", parse_mode="HTML")
        
        # Импортируем необходимые модули
        from services.admin_stats_service import collect_daily_stats, get_comparison_stats, format_admin_report, save_daily_stats
        from utils.time_utils import get_moscow_now
        from datetime import timedelta
        
        # Обновляем статус
        await status_msg.edit_text("📊 <b>Шаг 2/6:</b> Собираю данные за вчера...", parse_mode="HTML")
        
        # Собираем статистику за вчера
        yesterday = get_moscow_now() - timedelta(days=1)
        logger.info(f"🗓️ Собираю статистику за: {yesterday.date()}")
        
        stats = await collect_daily_stats(yesterday)
        
        if not stats:
            await status_msg.edit_text("❌ <b>Не удалось собрать статистику</b>\n\nПроверьте логи", parse_mode="HTML")
            return
        
        # Показываем краткую сводку
        await status_msg.edit_text(
            f"📊 <b>Шаг 3/6:</b> Данные собраны!\n\n"
            f"👥 Всего пользователей: {stats['users']['total']}\n"
            f"✅ Активных: {stats['users']['active']}\n"
            f"🌱 Добавлено растений: {stats['plants']['total_added']}\n"
            f"❓ Вопросов: {stats['activity']['questions']}\n"
            f"💧 Поливов: {stats['plants']['total_waterings']}",
            parse_mode="HTML"
        )
        
        # Сохраняем статистику
        await status_msg.edit_text("📊 <b>Шаг 4/6:</b> Сохраняю статистику в БД...", parse_mode="HTML")
        save_result = await save_daily_stats(stats)
        
        if not save_result:
            await message.answer("⚠️ Не удалось сохранить статистику в БД", parse_mode="HTML")
        
        # Получаем данные для сравнения
        await status_msg.edit_text("📊 <b>Шаг 5/6:</b> Получаю данные для сравнения...", parse_mode="HTML")
        comparisons = await get_comparison_stats(yesterday)
        
        # Форматируем отчет
        await status_msg.edit_text("📊 <b>Шаг 6/6:</b> Форматирую и отправляю отчет...", parse_mode="HTML")
        report = format_admin_report(stats, comparisons)
        
        # Отправляем отчет текущему пользователю
        await message.answer(report, parse_mode="HTML")
        
        # Отправляем всем администраторам
        success_count = 0
        for admin_id in ADMIN_USER_IDS:
            if admin_id == user_id:
                # Уже отправили выше
                success_count += 1
                continue
                
            try:
                await message.bot.send_message(
                    chat_id=admin_id,
                    text=f"📊 <b>Тестовый отчет от @{message.from_user.username or 'админа'}</b>\n\n" + report,
                    parse_mode="HTML"
                )
                success_count += 1
                logger.info(f"✅ Отчет отправлен администратору {admin_id}")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки администратору {admin_id}: {e}")
        
        await status_msg.edit_text(
            f"✅ <b>Готово!</b>\n\n"
            f"📬 Отчет отправлен {success_count}/{len(ADMIN_USER_IDS)} администраторам\n"
            f"📊 Статистика за {yesterday.date().strftime('%d.%m.%Y')}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка генерации тестовой статистики: {e}", exc_info=True)
        await message.answer(
            f"❌ <b>Критическая ошибка:</b>\n\n<code>{str(e)}</code>\n\n"
            "📝 Подробности в логах сервера",
            parse_mode="HTML"
        )


@router.message(Command("help"))
async def help_command(message: types.Message):
    """Команда /help"""
    help_text = """
🌱 <b>Как пользоваться ботом:</b>

🌱 <b>Добавление растения:</b>
- Пришли фото
- Получи анализ состояния
- Отслеживай изменения

📊 <b>Система состояний:</b>
- 💐 Цветение - особый уход
- 🌿 Активный рост - больше питания
- 😴 Период покоя - меньше полива
- ⚠️ Стресс - срочные действия

📸 <b>Месячные напоминания:</b>
- Обновляйте фото раз в месяц
- Отслеживайте изменения
- Адаптивные рекомендации

⏰ <b>Умные напоминания:</b>
- Адаптированы под состояние
- Учитывают этап роста
- Персональный график

<b>Команды:</b>
/start - Главное меню
/grow - Вырастить с нуля
/help - Справка
    """
    
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [InlineKeyboardButton(text="📝 Обратная связь", callback_data="feedback")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ]
    
    await message.answer(
        help_text, 
        parse_mode="HTML", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


@router.message(Command("feedback"))
async def feedback_command(message: types.Message):
    """Команда /feedback"""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [InlineKeyboardButton(text="🐛 Сообщить о баге", callback_data="feedback_bug")],
        [InlineKeyboardButton(text="❌ Неточный анализ", callback_data="feedback_analysis_error")],
        [InlineKeyboardButton(text="💡 Предложение", callback_data="feedback_suggestion")],
        [InlineKeyboardButton(text="⭐ Отзыв", callback_data="feedback_review")],
    ]
    
    await message.answer(
        "📝 <b>Обратная связь</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
