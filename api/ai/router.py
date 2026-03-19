"""
Эндпоинты для ИИ-вопросов о растениях
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from api.auth.dependencies import get_current_user
from api.schemas import QuestionRequest, QuestionResponse
from services.ai_service import answer_plant_question
from services.subscription_service import check_limit, increment_usage
from plant_memory import get_plant_context, save_interaction
from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/question", response_model=QuestionResponse)
async def ask_question(
    req: QuestionRequest,
    user_id: int = Depends(get_current_user),
):
    """Задать вопрос ИИ о растении"""
    # Проверяем лимит
    allowed, error_msg = await check_limit(user_id, "questions")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=error_msg)

    # Загружаем контекст растения если указано
    context_text = ""
    plant_name = None

    if req.plant_id:
        db = await get_db()
        plant = await db.get_plant_with_state(req.plant_id, user_id)
        if not plant:
            raise HTTPException(status_code=404, detail="Растение не найдено")

        plant_name = plant.get("display_name")
        context_text = await get_plant_context(req.plant_id, user_id, focus="general")

    # Получаем ответ от AI
    answer = await answer_plant_question(req.question, context_text)

    if isinstance(answer, dict):
        if "error" in answer:
            return QuestionResponse(success=False, error=answer["error"])

        answer_text = answer.get("answer", "")
        model_name = answer.get("model")
    else:
        answer_text = answer
        model_name = None

    if not answer_text or len(answer_text) < 20:
        return QuestionResponse(
            success=False,
            error="Не удалось сформировать ответ. Попробуйте переформулировать.",
        )

    # Увеличиваем счётчик
    await increment_usage(user_id, "questions")

    # Сохраняем взаимодействие
    if req.plant_id:
        await save_interaction(
            req.plant_id, user_id, req.question, answer_text,
            context_used={"context_length": len(context_text)},
        )

    return QuestionResponse(
        success=True,
        answer=answer_text,
        model=model_name,
        plant_name=plant_name,
    )
