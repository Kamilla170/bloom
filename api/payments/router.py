"""
Эндпоинты подписки и платежей
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from api.auth.dependencies import get_current_user
from api.schemas import (
    SubscriptionPlan,
    PlansResponse,
    DiscountInfo,
    CreatePaymentRequest,
    CreatePaymentResponse,
    SuccessResponse,
)
from config import SUBSCRIPTION_PLANS, DISCOUNT_PLANS, DISCOUNT_DURATION_DAYS
from database import get_db
from services.payment_service import create_payment, handle_payment_webhook, cancel_auto_payment
from services.subscription_service import is_pro

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


async def _get_discount_info(user_id: int) -> tuple[bool, Optional[datetime]]:
    """
    Возвращает (имеет_скидку, когда_закончится).
    Скидка действует DISCOUNT_DURATION_DAYS дней с момента регистрации.
    """
    try:
        db = await get_db()
        async with db.pool.acquire() as conn:
            created_at = await conn.fetchval(
                "SELECT created_at FROM users WHERE user_id = $1", user_id
            )
        if not created_at:
            return False, None
        if created_at.tzinfo:
            created_at = created_at.replace(tzinfo=None)
        now = datetime.utcnow()
        days_since = (now - created_at).total_seconds() / 86400
        if days_since > DISCOUNT_DURATION_DAYS:
            return False, None
        ends_at = created_at + timedelta(days=DISCOUNT_DURATION_DAYS)
        return True, ends_at
    except Exception as e:
        logger.error(f"❌ discount check failed for user_id={user_id}: {e}")
        return False, None


def _compute_discount_percent() -> int:
    """Процент скидки считается по 1month: (1 - promo/regular) * 100"""
    regular = SUBSCRIPTION_PLANS["1month"]["price"]
    promo = DISCOUNT_PLANS["1month"]["price"]
    if regular <= 0:
        return 0
    return round((1 - promo / regular) * 100)


@router.get("/plans", response_model=PlansResponse)
async def list_plans(user_id: int = Depends(get_current_user)):
    """Список тарифов с информацией о скидке"""
    has_discount, ends_at = await _get_discount_info(user_id)

    plans: list[SubscriptionPlan] = []
    for plan_id, regular in SUBSCRIPTION_PLANS.items():
        discounted = DISCOUNT_PLANS.get(plan_id) if has_discount else None
        active = discounted or regular
        plans.append(SubscriptionPlan(
            id=plan_id,
            label=regular["label"],
            days=regular["days"],
            price=active["price"],
            original_price=regular["price"],
            per_month=active.get("per_month"),
            is_popular=(plan_id == "3months"),
        ))

    discount_info: Optional[DiscountInfo] = None
    if has_discount and ends_at:
        discount_info = DiscountInfo(
            percent=_compute_discount_percent(),
            ends_at=ends_at,
            label="Скидка для новых пользователей",
        )

    return PlansResponse(plans=plans, discount=discount_info)


@router.post("/create", response_model=CreatePaymentResponse)
async def create_new_payment(
    req: CreatePaymentRequest,
    user_id: int = Depends(get_current_user),
):
    """Создать платёж для выбранного тарифа"""
    if await is_pro(user_id):
        raise HTTPException(status_code=400, detail="У вас уже есть подписка")

    has_discount, _ = await _get_discount_info(user_id)
    plan = (DISCOUNT_PLANS if has_discount else SUBSCRIPTION_PLANS).get(req.plan_id)

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
