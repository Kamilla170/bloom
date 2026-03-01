import logging
from datetime import datetime, timedelta
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    ADMIN_USER_IDS, SUBSCRIPTION_PLANS, FREE_LIMITS,
    DISCOUNT_PLANS, DISCOUNT_DURATION_DAYS
)
from database import get_db
from services.subscription_service import (
    get_user_plan, get_usage_stats, activate_pro, revoke_pro, is_pro
)
from services.payment_service import create_payment, cancel_auto_payment

logger = logging.getLogger(__name__)

router = Router()


def plans_keyboard():
    """Клавиатура с выбором тарифа (обычные цены)"""
    buttons = []
    for plan_id, plan in SUBSCRIPTION_PLANS.items():
        if plan['days'] > 30:
            text = f"⭐ {plan['label']} — {plan['price']}₽ ({plan['per_month']}₽/мес)"
        else:
            text = f"⭐ {plan['label']} — {plan['price']}₽/мес"
        buttons.append([InlineKeyboardButton(
            text=text,
            callback_data=f"buy_{plan_id}"
        )])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def discount_plans_keyboard():
    """Клавиатура с выбором тарифа (скидочные цены)"""
    buttons = []
    for plan_id, plan in DISCOUNT_PLANS.items():
        original = plan['original_price']
        discounted = plan['price']
        label = plan['label']
        if plan['days'] > 30:
            text = f"🔥 {label} — {discounted}₽ (вместо {original}₽)"
        else:
            text = f"🔥 {label} — {discounted}₽/мес (вместо {original}₽)"
        buttons.append([InlineKeyboardButton(
            text=text,
            callback_data=f"buy_discount_{plan_id}"
        )])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def is_discount_eligible(user_id: int) -> bool:
    """Проверяет, имеет ли пользователь право на скидку (≤ 3 дней с регистрации)"""
    try:
        db = await get_db()
        async with db.pool.acquire() as conn:
            created_at = await conn.fetchval("""
                SELECT created_at FROM users WHERE user_id = $1
            """, user_id)
            
            if not created_at:
                return False
            
            # Убираем timezone info если есть, для корректного сравнения
            now = datetime.utcnow()
            if created_at.tzinfo:
                created_at = created_at.replace(tzinfo=None)
            
            days_since = (now - created_at).total_seconds() / 86400
            return days_since <= DISCOUNT_DURATION_DAYS
    except Exception as e:
        logger.error(f"Ошибка проверки скидки для {user_id}: {e}")
        return False


def subscription_manage_keyboard(plan_info: dict):
    """Клавиатура управления подпиской"""
    buttons = []
    
    if plan_info['plan'] == 'pro':
        if plan_info.get('auto_pay'):
            buttons.append([InlineKeyboardButton(
                text="🔕 Отключить автопродление", 
                callback_data="cancel_auto_pay"
            )])
        buttons.append([InlineKeyboardButton(
            text="💳 Отвязать карту", 
            callback_data="unlink_card"
        )])
        buttons.append([InlineKeyboardButton(
            text="📊 Моя статистика", callback_data="stats"
        )])
    else:
        buttons.append([InlineKeyboardButton(
            text="⭐ Оформить подписку", 
            callback_data="subscribe_pro"
        )])
    
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_limit_message(message_or_callback, error_text: str):
    """Отправить сообщение о достижении лимита"""
    keyboard = plans_keyboard()
    
    if isinstance(message_or_callback, types.CallbackQuery):
        await message_or_callback.message.answer(
            error_text, parse_mode="HTML", reply_markup=keyboard
        )
        await message_or_callback.answer()
    else:
        await message_or_callback.answer(
            error_text, parse_mode="HTML", reply_markup=keyboard
        )


# === КОМАНДЫ ===

@router.message(Command("pro"))
async def pro_command(message: types.Message):
    """Команда /pro — информация о подписке и оформление"""
    user_id = message.from_user.id
    plan_info = await get_user_plan(user_id)
    
    if plan_info['plan'] == 'pro':
        expires_str = plan_info['expires_at'].strftime('%d.%m.%Y') if plan_info['expires_at'] else '—'
        auto_text = "✅ Автопродление включено" if plan_info['auto_pay'] else "❌ Автопродление выключено"
        grace_text = "\n⚠️ <b>Grace period — продлите подписку!</b>" if plan_info['is_grace_period'] else ""
        
        await message.answer(
            f"⭐ <b>Ваш план: Подписка</b>\n\n"
            f"📅 Активна до: <b>{expires_str}</b>\n"
            f"📆 Осталось дней: <b>{plan_info['days_left']}</b>\n"
            f"{auto_text}"
            f"{grace_text}\n\n"
            f"🌱 Без ограничений на растения, анализы и вопросы",
            parse_mode="HTML",
            reply_markup=subscription_manage_keyboard(plan_info)
        )
    else:
        stats = await get_usage_stats(user_id)
        
        # Проверяем право на скидку
        has_discount = await is_discount_eligible(user_id)
        
        if has_discount:
            await message.answer(
                f"🌱 <b>Ваш план: Бесплатный</b>\n\n"
                f"<b>Использование функций:</b>\n"
                f"🌱 Растений: {stats['plants_count']}/{stats['plants_limit']}\n"
                f"📸 Анализов: {stats['analyses_used']}/{stats['analyses_limit']}\n"
                f"🤖 Вопросов: {stats['questions_used']}/{stats['questions_limit']}\n\n"
                f"🔥 <b>У вас есть скидка 33% для новых пользователей!</b>\n\n"
                f"⭐ Подписка снимает все ограничения:\n"
                f"• Неограниченное добавление растений\n"
                f"• Безлимитное количество анализов растений\n"
                f"• Поддержка 24/7 по всем вопросам о растениях\n",
                parse_mode="HTML",
                reply_markup=discount_plans_keyboard()
            )
        else:
            await message.answer(
                f"🌱 <b>Ваш план: Бесплатный</b>\n\n"
                f"<b>Использование функций:</b>\n"
                f"🌱 Растений: {stats['plants_count']}/{stats['plants_limit']}\n"
                f"📸 Анализов: {stats['analyses_used']}/{stats['analyses_limit']}\n"
                f"🤖 Вопросов: {stats['questions_used']}/{stats['questions_limit']}\n\n"
                f"<b>⭐ Выберите тариф:</b>\n"
                f"• Неограниченное добавление растений\n"
                f"• Безлимитное количество анализов растений\n"
                f"• Поддержка 24/7 по всем вопросам о растениях\n",
                parse_mode="HTML",
                reply_markup=plans_keyboard()
            )


@router.message(Command("subscription"))
async def subscription_command(message: types.Message):
    """Команда /subscription — то же что /pro"""
    await pro_command(message)


# === CALLBACK-и ===

@router.callback_query(F.data == "subscribe_pro")
async def subscribe_pro_callback(callback: types.CallbackQuery):
    """Показать выбор тарифа"""
    user_id = callback.from_user.id
    
    if await is_pro(user_id):
        await callback.answer("У вас уже есть подписка! ⭐", show_alert=True)
        return
    
    # Проверяем право на скидку
    has_discount = await is_discount_eligible(user_id)
    
    if has_discount:
        await callback.message.answer(
            "🔥 <b>Скидка 33% для новых пользователей!</b>\n\n"
            "⭐ Подписка снимает все ограничения:\n"
            "• Неограниченное добавление растений\n"
            "• Безлимитное количество анализов растений\n"
            "• Поддержка 24/7 по всем вопросам о растениях\n",
            parse_mode="HTML",
            reply_markup=discount_plans_keyboard()
        )
    else:
        await callback.message.answer(
            "⭐ <b>Выберите тариф подписки:</b>\n\n"
            "• Неограниченное добавление растений\n"
            "• Безлимитное количество анализов растений\n"
            "• Поддержка 24/7 по всем вопросам о растениях\n",
            parse_mode="HTML",
            reply_markup=plans_keyboard()
        )
    
    await callback.answer()


@router.callback_query(F.data == "show_discount_plans")
async def show_discount_plans_callback(callback: types.CallbackQuery):
    """Показать скидочные тарифы (из триггерных сообщений)"""
    user_id = callback.from_user.id
    
    if await is_pro(user_id):
        await callback.answer("У вас уже есть подписка! ⭐", show_alert=True)
        return
    
    # Проверяем, ещё ли действует скидка
    has_discount = await is_discount_eligible(user_id)
    
    if has_discount:
        await callback.message.answer(
            "🔥 <b>Ваша персональная скидка 33%</b>\n\n"
            "Выберите тариф:\n\n"
            "• 1 мес — <s>249₽</s> <b>169₽</b>\n"
            "• 3 мес — <s>599₽</s> <b>399₽</b>\n"
            "• 6 мес — <s>1099₽</s> <b>739₽</b>\n"
            "• 12 мес — <s>2099₽</s> <b>1369₽</b>\n\n"
            "Подписка снимает все ограничения.",
            parse_mode="HTML",
            reply_markup=discount_plans_keyboard()
        )
    else:
        await callback.message.answer(
            "⏰ К сожалению, скидка уже истекла.\n\n"
            "Но вы можете оформить подписку по обычной цене:",
            parse_mode="HTML",
            reply_markup=plans_keyboard()
        )
    
    await callback.answer()


@router.callback_query(F.data.startswith("buy_discount_"))
async def buy_discount_plan_callback(callback: types.CallbackQuery):
    """Оформление подписки со скидкой"""
    user_id = callback.from_user.id
    plan_id = callback.data.replace("buy_discount_", "")
    
    # Проверяем существование плана
    discount_plan = DISCOUNT_PLANS.get(plan_id)
    regular_plan = SUBSCRIPTION_PLANS.get(plan_id)
    if not discount_plan or not regular_plan:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    if await is_pro(user_id):
        await callback.answer("У вас уже есть подписка! ⭐", show_alert=True)
        return
    
    # Проверяем право на скидку
    has_discount = await is_discount_eligible(user_id)
    
    if not has_discount:
        await callback.message.answer(
            "⏰ К сожалению, скидка уже истекла.\n\n"
            "Вы можете оформить подписку по обычной цене:",
            parse_mode="HTML",
            reply_markup=plans_keyboard()
        )
        await callback.answer()
        return
    
    processing_msg = await callback.message.answer(
        "💳 <b>Создаю ссылку на оплату...</b>",
        parse_mode="HTML"
    )
    
    # Автопродление только для месячного тарифа
    save_method = (plan_id == '1month')
    
    # Создаём платёж со скидочной ценой
    result = await create_payment(
        user_id=user_id,
        amount=discount_plan['price'],
        days=discount_plan['days'],
        plan_label=f"{discount_plan['label']} (скидка 33%)",
        save_method=save_method
    )
    
    await processing_msg.delete()
    
    if result:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=result['confirmation_url'])],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ])
        
        auto_text = "\n🔄 Автопродление: включено (по обычной цене)" if save_method else ""
        
        await callback.message.answer(
            f"💳 <b>Оплата подписки со скидкой</b>\n\n"
            f"⭐ Тариф: <b>{discount_plan['label']}</b>\n"
            f"💰 Сумма: <s>{discount_plan['original_price']}₽</s> <b>{discount_plan['price']}₽</b>\n"
            f"📅 Период: <b>{discount_plan['days']} дней</b>"
            f"{auto_text}\n\n"
            f"Нажмите кнопку ниже для перехода к оплате.\n"
            f"После оплаты подписка активируется автоматически.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        await callback.message.answer(
            "❌ <b>Не удалось создать платёж</b>\n\n"
            "Платёжная система временно недоступна. Попробуйте позже.",
            parse_mode="HTML"
        )
    
    await callback.answer()


@router.callback_query(F.data.startswith("buy_"))
async def buy_plan_callback(callback: types.CallbackQuery):
    """Оформление подписки — создание платежа для выбранного тарифа (обычная цена)"""
    user_id = callback.from_user.id
    plan_id = callback.data.replace("buy_", "")
    
    # Пропускаем если это discount_ (обрабатывается выше)
    if plan_id.startswith("discount_"):
        return
    
    plan = SUBSCRIPTION_PLANS.get(plan_id)
    if not plan:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    if await is_pro(user_id):
        await callback.answer("У вас уже есть подписка! ⭐", show_alert=True)
        return
    
    processing_msg = await callback.message.answer(
        "💳 <b>Создаю ссылку на оплату...</b>",
        parse_mode="HTML"
    )
    
    # Автопродление только для месячного тарифа
    save_method = (plan_id == '1month')
    
    result = await create_payment(
        user_id=user_id,
        amount=plan['price'],
        days=plan['days'],
        plan_label=plan['label'],
        save_method=save_method
    )
    
    await processing_msg.delete()
    
    if result:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=result['confirmation_url'])],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
        ])
        
        auto_text = "\n🔄 Автопродление: включено" if save_method else ""
        
        await callback.message.answer(
            f"💳 <b>Оплата подписки</b>\n\n"
            f"⭐ Тариф: <b>{plan['label']}</b>\n"
            f"💰 Сумма: <b>{plan['price']}₽</b>\n"
            f"📅 Период: <b>{plan['days']} дней</b>"
            f"{auto_text}\n\n"
            f"Нажмите кнопку ниже для перехода к оплате.\n"
            f"После оплаты подписка активируется автоматически.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        await callback.message.answer(
            "❌ <b>Не удалось создать платёж</b>\n\n"
            "Платёжная система временно недоступна. Попробуйте позже.",
            parse_mode="HTML"
        )
    
    await callback.answer()


@router.callback_query(F.data == "cancel_auto_pay")
async def cancel_auto_pay_callback(callback: types.CallbackQuery):
    """Отключение автопродления"""
    user_id = callback.from_user.id
    
    await cancel_auto_payment(user_id)
    
    plan_info = await get_user_plan(user_id)
    expires_str = plan_info['expires_at'].strftime('%d.%m.%Y') if plan_info['expires_at'] else '—'
    
    await callback.message.answer(
        f"🔕 <b>Автопродление отключено</b>\n\n"
        f"Ваша подписка действует до <b>{expires_str}</b>.\n"
        f"После этой даты аккаунт перейдёт на бесплатный план.\n\n"
        f"Вы можете снова подписаться в любой момент через /pro",
        parse_mode="HTML"
    )
    
    await callback.answer()


@router.callback_query(F.data == "unlink_card")
async def unlink_card_callback(callback: types.CallbackQuery):
    """Отвязка карты — удаляет сохранённый метод оплаты"""
    user_id = callback.from_user.id
    
    await cancel_auto_payment(user_id)
    
    await callback.message.answer(
        "💳 <b>Карта отвязана</b>\n\n"
        "Сохранённый способ оплаты удалён из системы.\n"
        "Автопродление отключено.\n\n"
        "Для следующей оплаты нужно будет ввести данные карты заново.",
        parse_mode="HTML"
    )
    
    await callback.answer()


@router.callback_query(F.data == "show_subscription")
async def show_subscription_callback(callback: types.CallbackQuery):
    """Показать информацию о подписке"""
    user_id = callback.from_user.id
    
    plan_info = await get_user_plan(user_id)
    
    if plan_info['plan'] == 'pro':
        expires_str = plan_info['expires_at'].strftime('%d.%m.%Y') if plan_info['expires_at'] else '—'
        auto_text = "✅ Автопродление включено" if plan_info['auto_pay'] else "❌ Автопродление выключено"
        grace_text = "\n⚠️ <b>Grace period — продлите подписку!</b>" if plan_info['is_grace_period'] else ""
        
        await callback.message.answer(
            f"⭐ <b>Ваш план: Подписка</b>\n\n"
            f"📅 Активна до: <b>{expires_str}</b>\n"
            f"📆 Осталось дней: <b>{plan_info['days_left']}</b>\n"
            f"{auto_text}"
            f"{grace_text}\n\n"
            f"🌱 Без ограничений на растения, анализы и вопросы",
            parse_mode="HTML",
            reply_markup=subscription_manage_keyboard(plan_info)
        )
    else:
        stats = await get_usage_stats(user_id)
        
        # Проверяем право на скидку
        has_discount = await is_discount_eligible(user_id)
        
        if has_discount:
            await callback.message.answer(
                f"🌱 <b>Ваш план: Бесплатный</b>\n\n"
                f"<b>Использование функций:</b>\n"
                f"🌱 Растений: {stats['plants_count']}/{stats['plants_limit']}\n"
                f"📸 Анализов: {stats['analyses_used']}/{stats['analyses_limit']}\n"
                f"🤖 Вопросов: {stats['questions_used']}/{stats['questions_limit']}\n\n"
                f"🔥 <b>У вас есть скидка 33% для новых пользователей!</b>\n\n"
                f"⭐ Подписка снимает все ограничения:\n"
                f"• Неограниченное добавление растений\n"
                f"• Безлимитное количество анализов растений\n"
                f"• Поддержка 24/7 по всем вопросам о растениях\n",
                parse_mode="HTML",
                reply_markup=discount_plans_keyboard()
            )
        else:
            await callback.message.answer(
                f"🌱 <b>Ваш план: Бесплатный</b>\n\n"
                f"<b>Использование функций:</b>\n"
                f"🌱 Растений: {stats['plants_count']}/{stats['plants_limit']}\n"
                f"📸 Анализов: {stats['analyses_used']}/{stats['analyses_limit']}\n"
                f"🤖 Вопросов: {stats['questions_used']}/{stats['questions_limit']}\n\n"
                f"<b>⭐ Выберите тариф:</b>\n"
                f"• Неограниченное добавление растений\n"
                f"• Безлимитное количество анализов растений\n"
                f"• Поддержка 24/7 по всем вопросам о растениях\n",
                parse_mode="HTML",
                reply_markup=plans_keyboard()
            )
    
    await callback.answer()


# === АДМИН-КОМАНДЫ ===

@router.message(Command("grant_pro"))
async def grant_pro_command(message: types.Message):
    """
    /grant_pro {user_id} {days}
    Выдать подписку пользователю на N дней
    """
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.reply("❌ Нет прав администратора")
        return
    
    try:
        parts = message.text.split()
        
        if len(parts) < 3:
            await message.reply(
                "📝 <b>Формат:</b> /grant_pro {user_id} {days}\n\n"
                "<b>Пример:</b> /grant_pro 123456789 30",
                parse_mode="HTML"
            )
            return
        
        target_user_id = int(parts[1])
        days = int(parts[2])
        
        if days < 1 or days > 365:
            await message.reply("❌ Количество дней должно быть от 1 до 365")
            return
        
        db = await get_db()
        user_info = await db.get_user_info_by_id(target_user_id)
        
        if not user_info:
            await message.reply(f"❌ Пользователь с ID {target_user_id} не найден")
            return
        
        expires_at = await activate_pro(
            target_user_id, 
            days=days, 
            granted_by=message.from_user.id
        )
        
        username = user_info.get('username') or user_info.get('first_name') or f"user_{target_user_id}"
        expires_str = expires_at.strftime('%d.%m.%Y %H:%M')
        
        await message.reply(
            f"✅ <b>Подписка выдана!</b>\n\n"
            f"👤 Кому: {username} (ID: {target_user_id})\n"
            f"📅 На: {days} дней\n"
            f"⏰ До: {expires_str}",
            parse_mode="HTML"
        )
        
        # Уведомляем пользователя
        try:
            await message.bot.send_message(
                chat_id=target_user_id,
                text=(
                    f"🎁 <b>Вам подарена подписка!</b>\n\n"
                    f"📅 Активна до: <b>{expires_str}</b>\n\n"
                    f"🌱 Неограниченный доступ к функциям бота"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        
    except ValueError:
        await message.reply("❌ Неверный формат. Используйте: /grant_pro {user_id} {days}")
    except Exception as e:
        logger.error(f"Ошибка grant_pro: {e}", exc_info=True)
        await message.reply(f"❌ Ошибка: {str(e)}")


@router.message(Command("revoke_pro"))
async def revoke_pro_command(message: types.Message):
    """
    /revoke_pro {user_id}
    Отозвать подписку
    """
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.reply("❌ Нет прав администратора")
        return
    
    try:
        parts = message.text.split()
        
        if len(parts) < 2:
            await message.reply(
                "📝 <b>Формат:</b> /revoke_pro {user_id}\n\n"
                "<b>Пример:</b> /revoke_pro 123456789",
                parse_mode="HTML"
            )
            return
        
        target_user_id = int(parts[1])
        
        await revoke_pro(target_user_id)
        
        await message.reply(
            f"✅ Подписка отозвана у пользователя {target_user_id}",
            parse_mode="HTML"
        )
        
    except ValueError:
        await message.reply("❌ Неверный формат user_id")
    except Exception as e:
        logger.error(f"Ошибка revoke_pro: {e}", exc_info=True)
        await message.reply(f"❌ Ошибка: {str(e)}")
