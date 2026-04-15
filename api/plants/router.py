"""
Эндпоинты для работы с растениями (Этап 3)
"""

import logging
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status

from database import get_db
from api.auth.dependencies import get_current_user
from api.schemas import (
    PlantListResponse, PlantSummary, PlantDetail,
    AnalysisResponse, SavePlantRequest, WaterPlantResponse,
    UpdatePlantRequest, FertilizeResponse, PlantPhotoEntry, SuccessResponse,
)
from services.ai_service import analyze_plant_image
from services.plant_service import (
    save_analyzed_plant, get_user_plants_list,
    water_plant, delete_plant, rename_plant,
    get_plant_details, fertilize_plant_action,
    update_plant_state_from_photo,
)
from services.subscription_service import check_limit, increment_usage
from api.services.cloudinary_service import upload_plant_photo, get_photo_url
from config import STATE_EMOJI, STATE_NAMES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plants", tags=["plants"])

# Временное хранилище анализов
_app_temp_analyses: dict[str, dict] = {}


def _plant_photo_url(photo_file_id: str | None, width: int = 400) -> str | None:
    return get_photo_url(photo_file_id, width)


def _plant_to_summary(p: dict) -> PlantSummary:
    """Преобразовать запись из БД в PlantSummary"""
    return PlantSummary(
        id=p["id"],
        display_name=p.get("display_name") or p.get("plant_name") or f"Растение #{p['id']}",
        plant_name=p.get("plant_name"),
        current_state=p.get("current_state", "healthy"),
        state_emoji=STATE_EMOJI.get(p.get("current_state", "healthy"), "🌱"),
        watering_interval=p.get("watering_interval", 7),
        last_watered=p.get("last_watered"),
        next_watering_date=p.get("next_watering_date"),
        needs_watering=bool(p.get("needs_watering", False)),
        water_status=p.get("water_status", ""),
        photo_file_id=p.get("photo_file_id"),
        photo_url=_plant_photo_url(p.get("photo_file_id"), 400),
        saved_date=p.get("saved_date"),
        current_streak=p.get("current_streak", 0) or 0,
        max_streak=p.get("max_streak", 0) or 0,
        fertilizing_enabled=bool(p.get("fertilizing_enabled", False)),
        fertilizing_interval=p.get("fertilizing_interval"),
        last_fertilized=p.get("last_fertilized"),
        next_fertilizing_date=p.get("next_fertilizing_date"),
    )


@router.get("", response_model=PlantListResponse)
async def list_plants(user_id: int = Depends(get_current_user)):
    """Список растений пользователя"""
    plants = await get_user_plants_list(user_id, limit=50)

    items = []
    for p in plants:
        if p.get("type") == "growing":
            continue
        items.append(_plant_to_summary(p))

    return PlantListResponse(plants=items, total=len(items))


@router.get("/{plant_id}", response_model=PlantDetail)
async def get_plant(plant_id: int, user_id: int = Depends(get_current_user)):
    """Детали растения"""
    db = await get_db()
    plant = await db.get_plant_with_state(plant_id, user_id)

    if not plant:
        raise HTTPException(status_code=404, detail="Растение не найдено")

    current_state = plant.get("current_state", "healthy")
    photo_fid = plant.get("photo_file_id")

    return PlantDetail(
        id=plant_id,
        display_name=plant.get("display_name") or f"Растение #{plant_id}",
        plant_name=plant.get("plant_name"),
        current_state=current_state,
        state_emoji=STATE_EMOJI.get(current_state, "🌱"),
        state_name=STATE_NAMES.get(current_state, "Здоровое"),
        watering_interval=plant.get("watering_interval", 7),
        last_watered=plant.get("last_watered"),
        next_watering_date=plant.get("next_watering_date"),
        needs_watering=bool(plant.get("needs_watering", False)),
        water_status="",
        photo_file_id=photo_fid,
        photo_url=_plant_photo_url(photo_fid, 800),
        saved_date=plant.get("saved_date"),
        analysis=plant.get("analysis"),
        current_streak=plant.get("current_streak", 0) or 0,
        max_streak=plant.get("max_streak", 0) or 0,
        fertilizing_enabled=bool(plant.get("fertilizing_enabled", False)),
        fertilizing_interval=plant.get("fertilizing_interval"),
        last_fertilized=plant.get("last_fertilized"),
        next_fertilizing_date=plant.get("next_fertilizing_date"),
    )


@router.post("/analyze", response_model=AnalysisResponse)
async def analyze_photo(
    photo: UploadFile = File(...),
    user_id: int = Depends(get_current_user),
):
    """Загрузить фото и получить анализ растения"""
    allowed, error_msg = await check_limit(user_id, "analyses")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=error_msg)

    image_bytes = await photo.read()
    if len(image_bytes) < 1000:
        raise HTTPException(status_code=400, detail="Файл слишком маленький")
    if len(image_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл слишком большой (макс. 20 МБ)")

    result = await analyze_plant_image(image_bytes)

    if not result["success"]:
        return AnalysisResponse(success=False, error=result.get("error", "Ошибка анализа"))

    await increment_usage(user_id, "analyses")

    plant_name_safe = (result.get("plant_name") or "plant").replace(" ", "_")[:30]
    photo_url = await upload_plant_photo(image_bytes, user_id, plant_name_safe)

    state_info = result.get("state_info", {})

    temp_id = str(uuid.uuid4())
    _app_temp_analyses[temp_id] = {
        "user_id": user_id,
        "analysis": result.get("raw_analysis", result["analysis"]),
        "formatted_analysis": result["analysis"],
        "photo_bytes": image_bytes,
        "photo_file_id": photo_url or "app_photo_pending",
        "plant_name": result.get("plant_name", "Неизвестное растение"),
        "confidence": result.get("confidence", 0),
        "state_info": state_info,
        "watering_interval": result.get("watering_interval"),
        "created_at": datetime.now(),
    }

    return AnalysisResponse(
        success=True,
        analysis=result["analysis"],
        plant_name=result.get("plant_name"),
        confidence=result.get("confidence"),
        watering_interval=result.get("watering_interval"),
        state=state_info.get("current_state", "healthy"),
        fertilizing_enabled=state_info.get("fertilizing_enabled", False),
        fertilizing_interval=state_info.get("fertilizing_interval"),
        temp_id=temp_id,
        photo_url=photo_url,
    )


@router.post("", response_model=PlantDetail)
async def save_plant(
    req: SavePlantRequest,
    user_id: int = Depends(get_current_user),
):
    """Сохранить проанализированное растение в коллекцию"""
    allowed, error_msg = await check_limit(user_id, "plants")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=error_msg)

    analysis_data = _app_temp_analyses.get(req.temp_id)
    if not analysis_data or analysis_data["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Анализ не найден или истёк")

    last_watered = None
    if req.last_watered_days_ago is not None and req.last_watered_days_ago >= 0:
        last_watered = datetime.now() - timedelta(days=req.last_watered_days_ago)

    result = await save_analyzed_plant(user_id, analysis_data, last_watered=last_watered)

    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Ошибка сохранения"))

    _app_temp_analyses.pop(req.temp_id, None)

    # Возвращаем полный объект растения
    db = await get_db()
    plant = await db.get_plant_with_state(result["plant_id"], user_id)

    if not plant:
        raise HTTPException(status_code=500, detail="Растение сохранено, но не загружено")

    current_state = plant.get("current_state", "healthy")
    photo_fid = plant.get("photo_file_id")

    return PlantDetail(
        id=plant["id"],
        display_name=plant.get("display_name") or f"Растение #{plant['id']}",
        plant_name=plant.get("plant_name"),
        current_state=current_state,
        state_emoji=STATE_EMOJI.get(current_state, "🌱"),
        state_name=STATE_NAMES.get(current_state, "Здоровое"),
        watering_interval=plant.get("watering_interval", 7),
        last_watered=plant.get("last_watered"),
        next_watering_date=plant.get("next_watering_date"),
        needs_watering=bool(plant.get("needs_watering", False)),
        water_status="",
        photo_file_id=photo_fid,
        photo_url=_plant_photo_url(photo_fid, 800),
        saved_date=plant.get("saved_date"),
        analysis=plant.get("analysis"),
        current_streak=plant.get("current_streak", 0) or 0,
        max_streak=plant.get("max_streak", 0) or 0,
        fertilizing_enabled=bool(plant.get("fertilizing_enabled", False)),
        fertilizing_interval=plant.get("fertilizing_interval"),
        last_fertilized=plant.get("last_fertilized"),
        next_fertilizing_date=plant.get("next_fertilizing_date"),
    )


@router.post("/{plant_id}/water", response_model=WaterPlantResponse)
async def water_single_plant(plant_id: int, user_id: int = Depends(get_current_user)):
    """Отметить полив растения (с расчётом серии)"""
    result = await water_plant(user_id, plant_id)

    if not result["success"]:
        raise HTTPException(status_code=404, detail=result.get("error", "Растение не найдено"))

    return WaterPlantResponse(
        success=True,
        plant_name=result["plant_name"],
        next_watering_days=result["next_watering_days"],
        next_watering_date=result.get("next_watering_date"),
        current_streak=result.get("current_streak", 0),
        max_streak=result.get("max_streak", 0),
        watered_at=datetime.now(),
    )


@router.post("/{plant_id}/fertilize", response_model=FertilizeResponse)
async def fertilize_single_plant(plant_id: int, user_id: int = Depends(get_current_user)):
    """Отметить подкормку растения"""
    result = await fertilize_plant_action(user_id, plant_id)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Ошибка"))

    return FertilizeResponse(
        success=True,
        plant_name=result["plant_name"],
        next_fertilizing_date=result["next_fertilizing_date"],
        interval=result["interval"],
    )


@router.patch("/{plant_id}", response_model=SuccessResponse)
async def update_plant(
    plant_id: int,
    req: UpdatePlantRequest,
    user_id: int = Depends(get_current_user),
):
    """Обновить растение (имя и/или fertilizing_enabled)"""
    db = await get_db()
    plant = await db.get_plant_by_id(plant_id, user_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Растение не найдено")

    updated_fields = []

    if req.name is not None:
        result = await rename_plant(user_id, plant_id, req.name)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("error", "Ошибка"))
        updated_fields.append(f"имя: {result['new_name']}")

    if req.fertilizing_enabled is not None:
        await db.update_plant_fertilizing(plant_id, user_id, req.fertilizing_enabled)
        updated_fields.append(f"подкормка: {'вкл' if req.fertilizing_enabled else 'выкл'}")

    if not updated_fields:
        raise HTTPException(status_code=400, detail="Нет полей для обновления")

    return SuccessResponse(message=f"Обновлено ({', '.join(updated_fields)})")


@router.delete("/{plant_id}", response_model=SuccessResponse)
async def remove_plant(plant_id: int, user_id: int = Depends(get_current_user)):
    """Удалить растение"""
    result = await delete_plant(user_id, plant_id)

    if not result["success"]:
        raise HTTPException(status_code=404, detail="Растение не найдено")

    return SuccessResponse(message=f"{result['plant_name']} удалено")


@router.get("/{plant_id}/photos", response_model=list[PlantPhotoEntry])
async def get_photos(plant_id: int, user_id: int = Depends(get_current_user)):
    """История фото растения"""
    db = await get_db()
    plant = await db.get_plant_by_id(plant_id, user_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Растение не найдено")

    photos = await db.get_plant_photos(plant_id, limit=50)

    return [
        PlantPhotoEntry(
            id=p["id"],
            photo_url=p["photo_url"],
            created_at=p["created_at"],
        )
        for p in photos
    ]


@router.post("/{plant_id}/photo", response_model=PlantDetail)
async def update_plant_photo(
    plant_id: int,
    photo: UploadFile = File(...),
    user_id: int = Depends(get_current_user),
):
    """
    Обновить главное фото растения с переанализом.
    Старое фото уезжает в plant_photos history.
    """
    allowed, error_msg = await check_limit(user_id, "analyses")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=error_msg)

    db = await get_db()
    plant = await db.get_plant_by_id(plant_id, user_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Растение не найдено")

    image_bytes = await photo.read()
    if len(image_bytes) < 1000:
        raise HTTPException(status_code=400, detail="Файл слишком маленький")
    if len(image_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл слишком большой")

    previous_state = plant.get("current_state", "healthy")
    result = await analyze_plant_image(image_bytes, previous_state=previous_state)

    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Ошибка анализа"))

    await increment_usage(user_id, "analyses")

    plant_name_safe = (result.get("plant_name") or "plant").replace(" ", "_")[:30]
    photo_url = await upload_plant_photo(image_bytes, user_id, plant_name_safe)

    if not photo_url:
        raise HTTPException(status_code=500, detail="Ошибка загрузки фото")

    state_info = result.get("state_info", {})

    update_result = await update_plant_state_from_photo(
        plant_id=plant_id,
        user_id=user_id,
        photo_file_id=photo_url,
        state_info=state_info,
        raw_analysis=result.get("raw_analysis", ""),
    )

    if not update_result["success"]:
        raise HTTPException(status_code=500, detail=update_result.get("error", "Ошибка обновления"))

    # Возвращаем обновлённое растение
    plant = await db.get_plant_with_state(plant_id, user_id)
    current_state = plant.get("current_state", "healthy")
    photo_fid = plant.get("photo_file_id")

    return PlantDetail(
        id=plant["id"],
        display_name=plant.get("display_name") or f"Растение #{plant['id']}",
        plant_name=plant.get("plant_name"),
        current_state=current_state,
        state_emoji=STATE_EMOJI.get(current_state, "🌱"),
        state_name=STATE_NAMES.get(current_state, "Здоровое"),
        watering_interval=plant.get("watering_interval", 7),
        last_watered=plant.get("last_watered"),
        next_watering_date=plant.get("next_watering_date"),
        needs_watering=bool(plant.get("needs_watering", False)),
        water_status="",
        photo_file_id=photo_fid,
        photo_url=_plant_photo_url(photo_fid, 800),
        saved_date=plant.get("saved_date"),
        analysis=plant.get("analysis"),
        current_streak=plant.get("current_streak", 0) or 0,
        max_streak=plant.get("max_streak", 0) or 0,
        fertilizing_enabled=bool(plant.get("fertilizing_enabled", False)),
        fertilizing_interval=plant.get("fertilizing_interval"),
        last_fertilized=plant.get("last_fertilized"),
        next_fertilizing_date=plant.get("next_fertilizing_date"),
    )


@router.post("/water-all", response_model=SuccessResponse)
async def water_all(user_id: int = Depends(get_current_user)):
    """Полить все растения"""
    from services.plant_service import water_all_plants
    result = await water_all_plants(user_id)
    if not result["success"]:
        raise HTTPException(status_code=500, detail="Ошибка")
    return SuccessResponse(message="Все растения политы")
