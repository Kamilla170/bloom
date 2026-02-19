import logging
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from database import get_db
from keyboards.main_menu import main_menu
from states.user_states import PlantStates
from config import ADMIN_USER_IDS

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
        "🤖 <b>Спросите ИИ о растениях</b>\n\n"
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
    
    logger.info(f"📊 Запрос статистики от user_id={user_id}")
    
    try:
        db = await get_db()
        
        # Дополнительная проверка - считаем растения напрямую
        async with db.pool.acquire() as conn:
            direct_count = await conn.fetchval(
                "SELECT COUNT(*) FROM plants WHERE user_id = $1", user_id
            )
            logger.info(f"📊 Прямой подсчёт растений для user_id={user_id}: {direct_count}")
        
        stats = await db.get_user_stats(user_id)
        
        logger.info(f"📊 Статистика для user_id={user_id}: plants={stats['total_plants']}, waterings={stats['total_waterings']}")
        
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
        logger.error(f"❌ Ошибка статистики для user_id={user_id}: {e}", exc_info=True)
        await message.answer("❌ Ошибка загрузки статистики", reply_markup=main_menu())


@router.message(Command("test_reminders"))
async def test_reminders_command(message: types.Message):
    """Тестовая команда для принудительной проверки напоминаний (только для админов)"""
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
        status_msg = await message.answer("🔄 <b>Запускаю проверку напоминаний...</b>", parse_mode="HTML")
        
        from services.reminder_service import check_and_send_reminders
        
        # Запускаем проверку напоминаний
        await check_and_send_reminders(message.bot)
        
        await status_msg.edit_text(
            "✅ <b>Проверка завершена!</b>\n\n"
            "📝 Проверьте логи сервера для деталей:\n"
            "• Сколько напоминаний найдено\n"
            "• Сколько отправлено\n"
            "• Были ли ошибки\n\n"
            "💡 Если напоминания не пришли - проверьте базу данных командой /check_reminders",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка тестовой проверки: {e}", exc_info=True)
        await message.answer(
            f"❌ <b>Ошибка при проверке:</b>\n\n<code>{str(e)}</code>\n\n"
            "📝 Подробности в логах сервера",
            parse_mode="HTML"
        )


@router.message(Command("check_reminders"))
async def check_reminders_status_command(message: types.Message):
    """Проверить статус напоминаний в базе данных (только для админов)"""
    user_id = message.from_user.id
    
    # Проверка прав администратора
    if user_id not in ADMIN_USER_IDS:
        await message.answer(
            f"❌ Эта команда доступна только администраторам\n\n"
            f"🔑 Ваш ID: <code>{user_id}</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        db = await get_db()
        from utils.time_utils import get_moscow_now
        moscow_now = get_moscow_now()
        moscow_date = moscow_now.date()
        
        async with db.pool.acquire() as conn:
            # Проверяем общую статистику напоминаний
            total_reminders = await conn.fetchval("""
                SELECT COUNT(*) FROM reminders 
                WHERE reminder_type = 'watering' AND is_active = TRUE
            """)
            
            # Проверяем напоминания на сегодня
            today_reminders = await conn.fetch("""
                SELECT p.id, p.user_id,
                       COALESCE(p.custom_name, p.plant_name, 'Растение #' || p.id) as display_name,
                       r.next_date, r.last_sent, r.is_active,
                       us.reminder_enabled as user_enabled,
                       p.reminder_enabled as plant_enabled
                FROM plants p
                JOIN reminders r ON r.plant_id = p.id AND r.reminder_type = 'watering'
                LEFT JOIN user_settings us ON p.user_id = us.user_id
                WHERE r.next_date::date <= $1::date
                ORDER BY r.next_date DESC
                LIMIT 10
            """, moscow_date)
            
            # Проверяем просроченные
            overdue = await conn.fetchval("""
                SELECT COUNT(*) FROM reminders r
                WHERE r.reminder_type = 'watering' 
                AND r.is_active = TRUE
                AND r.next_date::date < $1::date
                AND (r.last_sent IS NULL OR r.last_sent::date < $1::date)
            """, moscow_date)
        
        response = f"""
📊 <b>СТАТУС НАПОМИНАНИЙ</b>

🕐 <b>Текущее время (МСК):</b> {moscow_now.strftime('%d.%m.%Y %H:%M')}
📅 <b>Текущая дата:</b> {moscow_date}

📈 <b>Общая статистика:</b>
• Всего активных напоминаний: {total_reminders}
• Просроченных: {overdue}

📋 <b>Напоминания на сегодня и раньше (топ-10):</b>
"""
        
        if today_reminders:
            for i, rem in enumerate(today_reminders, 1):
                next_date = rem['next_date'].date() if rem['next_date'] else 'НЕ УСТАНОВЛЕНО'
                last_sent = rem['last_sent'].date() if rem['last_sent'] else 'НЕ ОТПРАВЛЯЛОСЬ'
                active = '✅' if rem['is_active'] else '❌'
                user_enabled = '✅' if rem['user_enabled'] else '❌'
                plant_enabled = '✅' if rem['plant_enabled'] else '❌'
                
                response += f"\n{i}. {rem['display_name']}\n"
                response += f"   User: {rem['user_id']}, Plant ID: {rem['id']}\n"
                response += f"   Next: {next_date}, Last: {last_sent}\n"
                response += f"   Active: {active}, UserEnabled: {user_enabled}, PlantEnabled: {plant_enabled}\n"
        else:
            response += "\n<i>Нет напоминаний на эту дату</i>\n"
        
        response += f"\n💡 Используйте /test_reminders для принудительной проверки"
        
        await message.answer(response, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"❌ Ошибка проверки статуса: {e}", exc_info=True)
        await message.answer(
            f"❌ <b>Ошибка:</b>\n\n<code>{str(e)}</code>",
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

🤖 <b>Спросите ИИ о растениях:</b>
- Помощь с любыми вопросами
- Диагностика проблем
- Персональные рекомендации

<b>Команды:</b>
/start - Главное меню
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
