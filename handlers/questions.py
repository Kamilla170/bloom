import logging
import re
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from states.user_states import PlantStates
from services.ai_service import answer_plant_question
from services.subscription_service import check_limit, increment_usage
from plant_memory import get_plant_context, save_interaction
from keyboards.main_menu import main_menu
from database import get_db

logger = logging.getLogger(__name__)

router = Router()

# Слова для выхода из режима вопросов
EXIT_WORDS = {'выход', 'выйти', 'меню', 'хватит', 'стоп', 'exit', 'quit', 'menu', 'назад', 'отмена'}


def question_continue_keyboard():
    """Клавиатура после ответа на вопрос"""
    keyboard = [
        [InlineKeyboardButton(text="🛑 Завершить диалог", callback_data="exit_question_mode")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def find_plant_in_question(user_id: int, question: str) -> dict | None:
    """
    Ищет упоминание растения пользователя в вопросе.
    
    Возвращает:
        dict с данными растения или None
    """
    try:
        db = await get_db()
        plants = await db.get_user_plants(user_id, limit=20)
        
        if not plants:
            return None
        
        question_lower = question.lower()
        
        # Ищем прямое упоминание названия растения
        for plant in plants:
            plant_name = plant.get('display_name', '').lower()
            custom_name = (plant.get('custom_name') or '').lower()
            original_name = (plant.get('plant_name') or '').lower()
            
            # Проверяем все варианты названий
            names_to_check = [plant_name, custom_name, original_name]
            names_to_check = [n for n in names_to_check if n and len(n) > 2]
            
            for name in names_to_check:
                # Ищем название или его часть (для "монстера" найдёт и "монстеры", "монстеру")
                # Берём корень слова (первые 70% букв, минимум 3)
                root_len = max(3, int(len(name) * 0.7))
                name_root = name[:root_len]
                
                if name_root in question_lower or name in question_lower:
                    logger.info(f"🔍 Найдено растение '{plant.get('display_name')}' в вопросе")
                    return plant
        
        # Ищем упоминания типа "первое растение", "второе"
        ordinals = {
            'первое': 0, 'первого': 0, 'первому': 0, '1': 0,
            'второе': 1, 'второго': 1, 'второму': 1, '2': 1,
            'третье': 2, 'третьего': 2, 'третьему': 2, '3': 2,
        }
        
        for word, index in ordinals.items():
            if word in question_lower and index < len(plants):
                logger.info(f"🔍 Найдено растение по порядку: #{index + 1}")
                return plants[index]
        
        return None
        
    except Exception as e:
        logger.error(f"Ошибка поиска растения в вопросе: {e}")
        return None


@router.callback_query(F.data.startswith("ask_about_plant_"))
async def ask_about_plant_callback(callback: types.CallbackQuery, state: FSMContext):
    """Задать вопрос о конкретном растении (из карточки растения)"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        db = await get_db()
        plant = await db.get_plant_with_state(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено", show_alert=True)
            return
        
        # Сохраняем контекст растения в состояние
        await state.update_data(
            question_plant_id=plant_id,
            question_plant_name=plant['display_name']
        )
        await state.set_state(PlantStates.waiting_question)
        
        plant_name = plant['display_name']
        
        await callback.message.answer(
            f"🤖 <b>Режим вопросов: {plant_name}</b>\n\n"
            f"🧠 Я учитываю всю историю этого растения.\n\n"
            f"✍️ Напишите ваш вопрос:",
            parse_mode="HTML",
            reply_markup=question_continue_keyboard()
        )
        
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "question")
async def start_question_mode_callback(callback: types.CallbackQuery, state: FSMContext):
    """Начать режим вопросов (из главного меню)"""
    await state.set_state(PlantStates.waiting_question)
    
    # Очищаем предыдущий контекст растения
    await state.update_data(question_plant_id=None, question_plant_name=None)
    
    await callback.message.answer(
        "🤖 <b>Режим вопросов об уходе за растениями</b>\n\n"
        "Спрашивайте что угодно! Я могу:\n"
        "• Ответить на общие вопросы о растениях\n"
        "• Дать совет по конкретному растению из вашей коллекции\n\n"
        "💡 <i>Примеры:</i>\n"
        "• «Почему желтеют листья у монстеры?»\n"
        "• «Как часто поливать фикус зимой?»\n"
        "• «Что делать если залил растение?»\n\n"
        "✍️ Напишите ваш вопрос:",
        parse_mode="HTML",
        reply_markup=question_continue_keyboard()
    )
    
    await callback.answer()


@router.callback_query(F.data == "exit_question_mode")
async def exit_question_mode_callback(callback: types.CallbackQuery, state: FSMContext):
    """Выход из режима вопросов"""
    await state.clear()
    
    await callback.message.answer(
        "👋 <b>Диалог завершён</b>\n\n"
        "Возвращайтесь, когда будут вопросы!",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    
    await callback.answer()


@router.message(StateFilter(PlantStates.waiting_question))
async def handle_question(message: types.Message, state: FSMContext):
    """Обработка вопросов с умным поиском контекста и продолжением диалога"""
    try:
        user_id = message.from_user.id
        question_text = message.text.strip()
        
        # Проверяем на команды выхода
        if question_text.lower() in EXIT_WORDS:
            await state.clear()
            await message.answer(
                "👋 <b>Диалог завершён</b>\n\n"
                "Возвращайтесь, когда будут вопросы!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            return
        
        # Проверяем на команды (начинаются с /)
        if question_text.startswith('/'):
            # Пропускаем - пусть обработает другой handler
            await state.clear()
            return
        
        # Проверка лимита вопросов
        allowed, error_msg = await check_limit(user_id, 'questions')
        if not allowed:
            from handlers.subscription import send_limit_message
            await send_limit_message(message, error_msg)
            await state.clear()
            return
        
        logger.info(f"❓ Вопрос от user_id={user_id}: {question_text[:50]}...")
        
        # Получаем данные из состояния
        data = await state.get_data()
        plant_id = data.get('question_plant_id')
        plant_name = data.get('question_plant_name')
        
        # Если контекст растения не установлен - ищем в вопросе
        found_plant = None
        if not plant_id:
            found_plant = await find_plant_in_question(user_id, question_text)
            if found_plant:
                plant_id = found_plant.get('id')
                plant_name = found_plant.get('display_name')
                # Сохраняем найденный контекст для следующих вопросов
                await state.update_data(
                    question_plant_id=plant_id,
                    question_plant_name=plant_name
                )
                logger.info(f"🔍 Автоматически определён контекст: {plant_name} (id={plant_id})")
        
        processing_msg = await message.reply(
            "🤔 <b>Думаю над ответом...</b>",
            parse_mode="HTML"
        )
        
        # Получаем контекст растения если есть
        context_text = ""
        if plant_id:
            context_text = await get_plant_context(plant_id, user_id, focus="general")
            logger.info(f"📚 Загружен контекст растения {plant_id} ({len(context_text)} символов)")
        
        # Если нет контекста растения - проверяем временный анализ
        if not context_text:
            from services.plant_service import temp_analyses
            if user_id in temp_analyses:
                plant_info = temp_analyses[user_id]
                temp_plant_name = plant_info.get("plant_name", "растение")
                context_text = f"Контекст: Недавно анализировал {temp_plant_name}"
        
        # Получаем ответ от AI
        answer = await answer_plant_question(question_text, context_text)
        
        await processing_msg.delete()
        
        # Обрабатываем ответ
        if isinstance(answer, dict):
            if "error" in answer:
                answer_text = answer["error"]
                model_name = None
            else:
                answer_text = answer.get("answer", "")
                model_name = answer.get("model", "unknown")
        else:
            answer_text = answer
            model_name = None
        
        if model_name:
            logger.info(f"✅ Ответ от модели: {model_name}")
        
        if answer_text and len(answer_text) > 50 and not answer_text.startswith("❌"):
            # Увеличиваем счётчик использования
            await increment_usage(user_id, 'questions')
            
            # Сохраняем взаимодействие
            if plant_id:
                await save_interaction(
                    plant_id, user_id, question_text, answer_text,
                    context_used={"context_length": len(context_text)}
                )
            
            # Формируем ответ с указанием контекста
            response_text = ""
            if plant_name and found_plant:
                # Показываем что нашли растение в вопросе
                response_text = f"🌱 <i>О растении: {plant_name}</i>\n\n"
            elif plant_name:
                # Контекст был установлен ранее
                response_text = f"🌱 <i>Контекст: {plant_name}</i>\n\n"
            
            response_text += answer_text
            response_text += "\n\n💬 <i>Можете задать ещё вопрос или завершить диалог</i>"
            
            # Пробуем отправить с HTML, если ошибка - без форматирования
            try:
                await message.reply(
                    response_text,
                    parse_mode="HTML",
                    reply_markup=question_continue_keyboard()
                )
            except Exception as parse_error:
                # Ошибка парсинга HTML - отправляем без форматирования
                logger.warning(f"⚠️ Ошибка HTML разметки, отправляю без форматирования: {parse_error}")
                # Убираем HTML теги для безопасной отправки
                import re
                clean_text = re.sub(r'<[^>]+>', '', response_text)
                await message.reply(
                    clean_text,
                    reply_markup=question_continue_keyboard()
                )
        else:
            await message.reply(
                "🤔 Не удалось сформировать ответ. Попробуйте переформулировать вопрос.",
                reply_markup=question_continue_keyboard()
            )
        
        # НЕ сбрасываем состояние - продолжаем диалог!
        
    except Exception as e:
        logger.error(f"Ошибка ответа: {e}", exc_info=True)
        await message.reply(
            "❌ Произошла ошибка. Попробуйте ещё раз или завершите диалог.",
            reply_markup=question_continue_keyboard()
        )
