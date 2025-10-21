import logging
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from states.user_states import PlantStates
from services.ai_service import answer_plant_question
from plant_memory import get_plant_context, save_interaction
from keyboards.main_menu import main_menu

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data.startswith("ask_about_plant_"))
async def ask_about_plant_callback(callback: types.CallbackQuery, state: FSMContext):
    """Задать вопрос о конкретном растении"""
    try:
        plant_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        
        from database import get_db
        db = await get_db()
        plant = await db.get_plant_with_state(plant_id, user_id)
        
        if not plant:
            await callback.answer("❌ Растение не найдено", show_alert=True)
            return
        
        await state.update_data(question_plant_id=plant_id)
        await state.set_state(PlantStates.waiting_question)
        
        plant_name = plant['display_name']
        
        await callback.message.answer(
            f"❓ <b>Вопрос о растении: {plant_name}</b>\n\n"
            f"🧠 Я буду учитывать всю историю этого растения:\n"
            f"• Все предыдущие анализы\n"
            f"• Ваши прошлые вопросы\n"
            f"• Историю проблем\n"
            f"• Паттерны ухода\n\n"
            f"✍️ Напишите ваш вопрос:",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback.answer()


@router.message(StateFilter(PlantStates.waiting_question))
async def handle_question(message: types.Message, state: FSMContext):
    """Обработка вопросов с полным контекстом растения"""
    try:
        logger.info(f"❓ Пользователь {message.from_user.id} задал вопрос")
        
        data = await state.get_data()
        plant_id = data.get('question_plant_id')
        user_id = message.from_user.id
        
        processing_msg = await message.reply(
            "🤔 <b>Анализирую с учетом истории растения...</b>", 
            parse_mode="HTML"
        )
        
        # Получаем контекст растения
        context_text = ""
        if plant_id:
            context_text = await get_plant_context(plant_id, user_id, focus="general")
            logger.info(f"📚 Загружен контекст растения {plant_id} ({len(context_text)} символов)")
        
        # Если нет контекста растения, проверяем временный анализ
        if not context_text:
            from services.plant_service import temp_analyses
            if user_id in temp_analyses:
                plant_info = temp_analyses[user_id]
                plant_name = plant_info.get("plant_name", "растение")
                context_text = f"Контекст: Недавно анализировал {plant_name}"
        
        # Получаем ответ от AI
        answer = await answer_plant_question(message.text, context_text)
        
        await processing_msg.delete()
        
        if answer and len(answer) > 50 and not answer.startswith("❌"):
            # Сохраняем взаимодействие
            if plant_id:
                await save_interaction(
                    plant_id, user_id, message.text, answer,
                    context_used={"context_length": len(context_text)}
                )
            
            await message.reply(answer, parse_mode="HTML" if "<" not in answer else None)
        else:
            await message.reply(
                "🤔 Не могу дать ответ. Попробуйте переформулировать.",
                reply_markup=main_menu()
            )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка ответа: {e}", exc_info=True)
        await message.reply("❌ Ошибка обработки", reply_markup=main_menu())
        await state.clear()
