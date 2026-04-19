"""
Эндпоинты для работы с растениями
"""

import logging
import uuid
from datetime import datetime, timedelta
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status

from database import get_db
from api.auth.dependencies import get_current_user
from api.schemas import (
    PlantListResponse, PlantSummary, PlantDetail,
    AnalysisResponse, SavePlantRequest, WaterPlantResponse,
    RenamePlantRequest, StateHistoryEntry, SuccessResponse,
)
from services.ai_service import analyze_plant_image, extract_watering_info
from services.plant_service import (
    save_analyzed_plant, get_user_plants_list,
    water_plant, delete_plant, rename_plant,
    get_plant_details, get_plant_state_history,
)
from services.subscription_service import check_limit, increment_usage
from services.reminder_service import create_plant_reminder
from config import STATE_EMOJI, STATE_NAMES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plants", tags=["plants"])

# Временное хранилище анализов для app-пользователей (аналог temp_analyses в боте)
_app_temp_analyses: dict[str, dict] = {}


@router.get("", response_model=PlantListResponse)
async def list_plants(user_id: int = Depends(get_current_user)):
    """Список растений пользователя"""
    plants = await get_user_plants_list(user_id, limit=50)

    items = []
    for p in plants:
        if p.get("type") == "growing":
            continue  # пока пропускаем выращиваемые

        items.append(PlantSummary(
            id=p["id"],
            display_name=p["display_name"],
            plant_name=p.get("plant_name"),
            current_state=p.get("current_state", "healthy"),
            state_emoji=p.get("emoji", "🌱"),
            watering_interval=p.get("watering_interval", 7),
            last_watered=p.get("last_watered"),
            water_status=p.get("water_status", ""),
            photo_file_id=p.get("photo_file_id"),
            saved_date=p.get("saved_date"),
        ))

    return PlantListResponse(plants=items, total=len(items))


@router.get("/{plant_id}", response_model=PlantDetail)
async def get_plant(plant_id: int, user_id: int = Depends(get_current_user)):
    """Детали растения"""
    details = await get_plant_details(plant_id, user_id)
    if not details:
        raise HTTPException(status_code=404, detail="Растение не найдено")

    db = await get_db()
    plant = await db.get_plant_by_id(plant_id, user_id)

    return PlantDetail(
        id=plant_id,
        display_name=details["plant_name"],
        plant_name=plant.get("plant_name") if plant else None,
        current_state=details["current_state"],
        state_emoji=details["state_emoji"],
        state_name=details["state_name"],
        watering_interval=details["watering_interval"],
        last_watered=plant.get("last_watered") if plant else None,
        water_status=details["water_status"],
        photo_file_id=plant.get("photo_file_id") if plant else None,
        saved_date=plant.get("saved_date") if plant else None,
        state_changes_count=details["state_changes_count"],
        growth_stage=plant.get("growth_stage", "young") if plant else "young",
        analysis=plant.get("analysis") if plant else None,
    )


@router.post("/analyze", response_model=AnalysisResponse)
async def analyze_photo(
    photo: UploadFile = File(...),
    user_id: int = Depends(get_current_user),
):
    """Загрузить фото и получить анализ растения"""
    # Проверяем лимит
    allowed, error_msg = await check_limit(user_id, "analyses")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=error_msg)

    # Читаем файл
    image_bytes = await photo.read()
    if len(image_bytes) < 1000:
        raise HTTPException(status_code=400, detail="Файл слишком маленький")
    if len(image_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл слишком большой (макс. 20 МБ)")

    # Анализируем
    result = await analyze_plant_image(image_bytes)

    if not result["success"]:
        return AnalysisResponse(
            success=False,
            error=result.get("error", "Ошибка анализа"),
        )

    # Увеличиваем счётчик
    await increment_usage(user_id, "analyses")

    # Сохраняем во временное хранилище
    temp_id = str(uuid.uuid4())
    _app_temp_analyses[temp_id] = {
        "user_id": user_id,
        "analysis": result.get("raw_analysis", result["analysis"]),
        "formatted_analysis": result["analysis"],
        "photo_bytes": image_bytes,
        "photo_file_id": None,  # У app-пользователей нет Telegram file_id
        "plant_name": result.get("plant_name", "Неизвестное растение"),
        "confidence": result.get("confidence", 0),
        "state_info": result.get("state_info", {}),
        "watering_interval": result.get("watering_interval"),
        "created_at": datetime.now(),
    }

    state_info = result.get("state_info", {})

    return AnalysisResponse(
        success=True,
        analysis=result["analysis"],
        plant_name=result.get("plant_name"),
        confidence=result.get("confidence"),
        watering_interval=result.get("watering_interval"),
        state=state_info.get("current_state", "healthy"),
        temp_id=temp_id,
    )


@router.post("", response_model=PlantDetail)
async def save_plant(
    req: SavePlantRequest,
    user_id: int = Depends(get_current_user),
):
    """Сохранить проанализированное растение в коллекцию"""
    # Проверяем лимит растений
    allowed, error_msg = await check_limit(user_id, "plants")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=error_msg)

    analysis_data = _app_temp_analyses.get(req.temp_id)
    if not analysis_data or analysis_data["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Анализ не найден или истёк")

    # Определяем дату последнего полива
    last_watered = None
    if req.last_watered_days_ago is not None and req.last_watered_days_ago >= 0:
        last_watered = datetime.now() - timedelta(days=req.last_watered_days_ago)

    # Для app-пользователей photo_file_id будет NULL
    # В будущем можно загружать фото в S3/Supabase Storage
    analysis_data["photo_file_id"] = analysis_data.get("photo_file_id") or "app_photo_pending"

    result = await save_analyzed_plant(user_id, analysis_data, last_watered=last_watered)

    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Ошибка сохранения"))

    # Удаляем из temp
    _app_temp_analyses.pop(req.temp_id, None)

    return PlantDetail(
        id=result["plant_id"],
        display_name=result["plant_name"],
        current_state=result["state"],
        state_emoji=result["state_emoji"],
        state_name=result["state_name"],
        watering_interval=result["interval"],
    )


@router.post("/{plant_id}/water", response_model=WaterPlantResponse)
async def water_single_plant(plant_id: int, user_id: int = Depends(get_current_user)):
    """Отметить полив растения"""
    result = await water_plant(user_id, plant_id)

    if not result["success"]:
        raise HTTPException(status_code=404, detail=result.get("error", "Растение не найдено"))

    return WaterPlantResponse(
        success=True,
        plant_name=result["plant_name"],
        next_watering_days=result["next_watering_days"],
        watered_at=datetime.now(),
    )


@router.patch("/{plant_id}", response_model=SuccessResponse)
async def update_plant(
    plant_id: int,
    req: RenamePlantRequest,
    user_id: int = Depends(get_current_user),
):
    """Переименовать растение"""
    result = await rename_plant(user_id, plant_id, req.name)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Ошибка"))

    return SuccessResponse(message=f"Растение переименовано: {result['new_name']}")


@router.delete("/{plant_id}", response_model=SuccessResponse)
async def remove_plant(plant_id: int, user_id: int = Depends(get_current_user)):
    """Удалить растение"""
    result = await delete_plant(user_id, plant_id)

    if not result["success"]:
        raise HTTPException(status_code=404, detail="Растение не найдено")

    return SuccessResponse(message=f"{result['plant_name']} удалено")


@router.get("/{plant_id}/history", response_model=list[StateHistoryEntry])
async def plant_history(plant_id: int, user_id: int = Depends(get_current_user)):
    """История состояний растения"""
    # Проверяем что растение принадлежит пользователю
    details = await get_plant_details(plant_id, user_id)
    if not details:
        raise HTTPException(status_code=404, detail="Растение не найдено")

    history = await get_plant_state_history(plant_id, limit=20)

    return [
        StateHistoryEntry(
            date=entry.get("date"),
            from_state=entry.get("from_state"),
            to_state=entry.get("to_state", "healthy"),
            reason=entry.get("reason"),
            emoji_from=entry.get("emoji_from", ""),
            emoji_to=entry.get("emoji_to", "🌱"),
        )
        for entry in history
    ]


@router.post("/water-all", response_model=SuccessResponse)
async def water_all(user_id: int = Depends(get_current_user)):
    """Полить все растения"""
    from services.plant_service import water_all_plants

    result = await water_all_plants(user_id)

    if not result["success"]:
        raise HTTPException(status_code=500, detail="Ошибка")

    return SuccessResponse(message="Все растения политы")
