import logging
from datetime import timedelta
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_db
from utils.time_utils import get_moscow_now

logger = logging.getLogger(__name__)


# === КОНФИГУРАЦИЯ ЦЕПОЧЕК ===

TRIGGER_CHAINS = {
    'onboarding_no_plant': {
        'description': 'Прошёл онбординг, но не добавил растение',
        'steps': [
            {
                'delay_hours': 3,
                'message': (
                    "🌱 Кстати, я всё ещё жду фото твоего растения!\n\n"
                    "Просто сфотографируй твое растение — и пришли мне. "
                    "Через пару секунд расскажу, что это за вид, "
                    "как за ним ухаживать и настрою полив."
                ),
                'button_text': '📸 Отправить фото',
                'button_callback': 'onboarding_analyze',
            },
            {
                'delay_hours': 24,
                'message': (
                    "🤔 Не знаешь, с чего начать? Вот что получишь, "
                    "когда добавишь растение:\n\n"
                    "🔍 Узнаешь точный вид и состояние\n"
                    "💧 Получишь персональный график полива\n"
                    "🔔 Я буду напоминать, когда пора поливать\n\n"
                    "Достаточно одного фото — попробуй!"
                ),
                'button_text': '📸 Добавить растение',
                'button_callback': 'onboarding_analyze',
            },
            {
                'delay_hours': 72,
                'message': (
                    "🌿 Я умею распознавать тысячи видов растений — "
                    "от обычных фиалок до редких тропических. "
                    "А ещё подбираю уход под конкретное состояние: "
                    "если растение болеет, получишь одни советы, "
                    "если цветёт — совсем другие.\n\n"
                    "Пришли фото, когда будет настроение — "
                    "посмотрим, что у тебя растёт!"
                ),
                'button_text': None,
                'button_callback': None,
            },
        ],
        'cancel_on': 'plant_added',
    },
}


# === СОЗДАНИЕ ЦЕПОЧКИ ===

async def start_chain(user_id: int, chain_type: str):
    """Запускает триггерную цепочку для пользователя"""
    chain_config = TRIGGER_CHAINS.get(chain_type)
    if not chain_config:
        logger.error(f"❌ Неизвестный тип цепочки: {chain_type}")
        return

    try:
        db = await get_db()
        moscow_now = get_moscow_now()

        async with db.pool.acquire() as conn:
            # Проверяем, нет ли уже активной цепочки этого типа
            existing = await conn.fetchval("""
                SELECT COUNT(*) FROM trigger_queue
                WHERE user_id = $1 AND chain_type = $2
                AND sent = FALSE AND cancelled = FALSE
            """, user_id, chain_type)

            if existing > 0:
                logger.info(f"⏭️ Цепочка '{chain_type}' уже активна для user_id={user_id}")
                return

            # Создаём все шаги цепочки
            for step_num, step_config in enumerate(chain_config['steps'], 1):
                send_at = moscow_now + timedelta(hours=step_config['delay_hours'])
                send_at_naive = send_at.replace(tzinfo=None)

                await conn.execute("""
                    INSERT INTO trigger_queue
                    (user_id, chain_type, step, send_at)
                    VALUES ($1, $2, $3, $4)
                """, user_id, chain_type, step_num, send_at_naive)

            logger.info(
                f"✅ Цепочка '{chain_type}' создана для user_id={user_id}: "
                f"{len(chain_config['steps'])} шагов"
            )

    except Exception as e:
        logger.error(f"❌ Ошибка создания цепочки '{chain_type}' для {user_id}: {e}", exc_info=True)


# === ОТМЕНА ЦЕПОЧКИ ===

async def cancel_chain(user_id: int, chain_type: str):
    """Отменяет все неотправленные сообщения цепочки"""
    try:
        db = await get_db()

        async with db.pool.acquire() as conn:
            result = await conn.fetch("""
                UPDATE trigger_queue
                SET cancelled = TRUE, cancelled_at = CURRENT_TIMESTAMP
                WHERE user_id = $1 AND chain_type = $2
                AND sent = FALSE AND cancelled = FALSE
                RETURNING id
            """, user_id, chain_type)

            cancelled_count = len(result)

            if cancelled_count > 0:
                logger.info(
                    f"🛑 Цепочка '{chain_type}' отменена для user_id={user_id}: "
                    f"{cancelled_count} сообщений"
                )

    except Exception as e:
        logger.error(f"❌ Ошибка отмены цепочки '{chain_type}' для {user_id}: {e}", exc_info=True)


async def cancel_chains_by_event(user_id: int, event: str):
    """Отменяет все цепочки, которые отменяются по данному событию"""
    for chain_type, config in TRIGGER_CHAINS.items():
        if config.get('cancel_on') == event:
            await cancel_chain(user_id, chain_type)


# === ПРОВЕРКА И ОТПРАВКА ===

async def check_and_send_triggers(bot):
    """Проверяет и отправляет готовые триггерные сообщения"""
    try:
        db = await get_db()
        moscow_now = get_moscow_now()
        moscow_now_naive = moscow_now.replace(tzinfo=None)

        async with db.pool.acquire() as conn:
            # Берём сообщения, которые пора отправить
            pending = await conn.fetch("""
                SELECT tq.id, tq.user_id, tq.chain_type, tq.step, tq.send_at
                FROM trigger_queue tq
                WHERE tq.sent = FALSE
                AND tq.cancelled = FALSE
                AND tq.send_at <= $1
                ORDER BY tq.send_at ASC
                LIMIT 50
            """, moscow_now_naive)

            if not pending:
                return

            logger.info(f"📨 Найдено {len(pending)} триггерных сообщений для отправки")

            sent_count = 0
            skip_count = 0
            error_count = 0

            for msg in pending:
                try:
                    # Проверяем стоп-условие перед отправкой
                    should_send = await check_stop_condition(
                        msg['user_id'], msg['chain_type']
                    )

                    if not should_send:
                        # Отменяем всю оставшуюся цепочку
                        await cancel_chain(msg['user_id'], msg['chain_type'])
                        skip_count += 1
                        continue

                    # Отправляем сообщение
                    await send_trigger_message(bot, msg)

                    # Помечаем как отправленное
                    await conn.execute("""
                        UPDATE trigger_queue
                        SET sent = TRUE, sent_at = $1
                        WHERE id = $2
                    """, moscow_now_naive, msg['id'])

                    sent_count += 1

                except Exception as e:
                    error_count += 1
                    logger.error(
                        f"❌ Ошибка отправки триггера id={msg['id']}, "
                        f"user={msg['user_id']}: {e}"
                    )

            if sent_count > 0 or skip_count > 0 or error_count > 0:
                logger.info(
                    f"📊 Триггеры: отправлено={sent_count}, "
                    f"пропущено={skip_count}, ошибок={error_count}"
                )

    except Exception as e:
        logger.error(f"❌ Ошибка проверки триггеров: {e}", exc_info=True)


async def check_stop_condition(user_id: int, chain_type: str) -> bool:
    """
    Проверяет, нужно ли ещё отправлять сообщения цепочки.
    Возвращает True если нужно отправлять, False если условие выполнено.
    """
    config = TRIGGER_CHAINS.get(chain_type)
    if not config:
        return False

    cancel_on = config.get('cancel_on')
    if not cancel_on:
        return True

    db = await get_db()

    async with db.pool.acquire() as conn:
        if cancel_on == 'plant_added':
            plants_count = await conn.fetchval("""
                SELECT COUNT(*) FROM plants
                WHERE user_id = $1 AND plant_type = 'regular'
            """, user_id)
            return plants_count == 0

        # Сюда добавлять другие стоп-условия:
        # elif cancel_on == 'payment_made':
        #     ...
        # elif cancel_on == 'watered_plant':
        #     ...

    return True


async def send_trigger_message(bot, msg_row):
    """Отправляет одно триггерное сообщение"""
    chain_type = msg_row['chain_type']
    step = msg_row['step']
    user_id = msg_row['user_id']

    config = TRIGGER_CHAINS.get(chain_type)
    if not config:
        logger.error(f"❌ Конфигурация не найдена: {chain_type}")
        return

    step_index = step - 1
    if step_index >= len(config['steps']):
        logger.error(f"❌ Шаг {step} не найден в цепочке '{chain_type}'")
        return

    step_config = config['steps'][step_index]
    message_text = step_config['message']

    # Собираем клавиатуру если есть кнопка
    reply_markup = None
    if step_config.get('button_text') and step_config.get('button_callback'):
        keyboard = [[
            InlineKeyboardButton(
                text=step_config['button_text'],
                callback_data=step_config['button_callback']
            )
        ]]
        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await bot.send_message(
        chat_id=user_id,
        text=message_text,
        reply_markup=reply_markup
    )

    logger.info(
        f"📤 Триггер отправлен: chain='{chain_type}', "
        f"step={step}, user_id={user_id}"
    )
