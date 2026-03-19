"""
Эндпоинты подписки и платежей
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.auth.dependencies import get_current_user
from api.schemas import (
    SubscriptionPlan, CreatePaymentRequest, CreatePaymentResponse, SuccessResponse,
)
from config import SUBSCRIPTION_PLANS, DISCOUNT_PLANS, DISCOUNT_DURATION_DAYS
from database import get_db
from services.payment_service import create_payment, handle_payment_webhook, cancel_auto_payment
from services.subscription_service import is_pro

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


async def _is_discount_eligible(user_id: int) -> bool:
    """Проверяет право на скидку"""
    try:
        db = await get_db()
        async with db.pool.acquire() as conn:
            created_at = await conn.fetchval(
                "SELECT created_at FROM users WHERE user_id = $1", user_id
            )
            if not created_at:
                return False
            now = datetime.utcnow()
            if created_at.tzinfo:
                created_at = created_at.replace(tzinfo=None)
            days_since = (now - created_at).total_seconds() / 86400
            return days_since <= DISCOUNT_DURATION_DAYS
    except Exception:
        return False


@router.get("/plans", response_model=list[SubscriptionPlan])
async def list_plans(user_id: int = Depends(get_current_user)):
    """Список тарифных планов (с учётом скидки если доступна)"""
    has_discount = await _is_discount_eligible(user_id)

    if has_discount:
        plans_source = DISCOUNT_PLANS
    else:
        plans_source = SUBSCRIPTION_PLANS

    plans = []
    for plan_id, plan in plans_source.items():
        plans.append(SubscriptionPlan(
            id=plan_id,
            label=plan["label"],
            price=plan["price"],
            days=plan["days"],
            per_month=plan.get("per_month"),
        ))

    return plans


@router.post("/create", response_model=CreatePaymentResponse)
async def create_new_payment(
    req: CreatePaymentRequest,
    user_id: int = Depends(get_current_user),
):
    """Создать платёж для выбранного тарифа"""
    if await is_pro(user_id):
        raise HTTPException(status_code=400, detail="У вас уже есть подписка")

    # Определяем цену: скидочная или обычная
    has_discount = await _is_discount_eligible(user_id)

    if has_discount:
        plan = DISCOUNT_PLANS.get(req.plan_id)
    else:
        plan = SUBSCRIPTION_PLANS.get(req.plan_id)

    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    save_method = (req.plan_id == "1month")

    result = await create_payment(
        user_id=user_id,
        amount=plan["price"],
        days=plan["days"],
        plan_label=plan["label"],
        save_method=save_method,
    )

    if not result:
        return CreatePaymentResponse(
            success=False,
            error="Платёжная система недоступна",
        )

    return CreatePaymentResponse(
        success=True,
        payment_id=result["payment_id"],
        confirmation_url=result["confirmation_url"],
    )


@router.post("/webhook")
async def payment_webhook(request: Request):
    """Webhook от YooKassa (без авторизации)"""
    try:
        payload = await request.json()
        success = await handle_payment_webhook(payload)
        if success:
            return {"status": "ok"}
        return {"status": "error"}, 400
    except Exception as e:
        logger.error(f"❌ Payment webhook error: {e}", exc_info=True)
        return {"status": "error"}, 500


@router.post("/cancel-auto", response_model=SuccessResponse)
async def cancel_auto(user_id: int = Depends(get_current_user)):
    """Отключить автопродление"""
    await cancel_auto_payment(user_id)
    return SuccessResponse(message="Автопродление отключено")
