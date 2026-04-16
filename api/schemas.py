"""
Pydantic схемы для REST API (Этап 3)
"""

from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field


# === AUTH ===

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    first_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


# === USERS ===

class UserProfile(BaseModel):
    user_id: int
    email: Optional[str] = None
    first_name: Optional[str] = None
    created_at: Optional[datetime] = None
    plants_count: int = 0
    total_waterings: int = 0
    questions_asked: int = 0


class UserSettings(BaseModel):
    reminder_enabled: bool = True
    reminder_time: str = "09:00"
    monthly_photo_reminder: bool = True


class UpdateSettingsRequest(BaseModel):
    reminder_enabled: Optional[bool] = None
    reminder_time: Optional[str] = None
    monthly_photo_reminder: Optional[bool] = None


# === PLANTS (Этап 3) ===

class PlantSummary(BaseModel):
    id: int
    display_name: str
    plant_name: Optional[str] = None
    current_state: str = "healthy"
    state_emoji: str = "🌱"
    watering_interval: int = 7
    last_watered: Optional[datetime] = None
    next_watering_date: Optional[date] = None
    needs_watering: bool = False
    water_status: str = ""
    photo_file_id: Optional[str] = None
    photo_url: Optional[str] = None
    saved_date: Optional[datetime] = None
    current_streak: int = 0
    max_streak: int = 0
    fertilizing_enabled: bool = False
    fertilizing_interval: Optional[int] = None
    last_fertilized: Optional[datetime] = None
    next_fertilizing_date: Optional[date] = None


class PlantDetail(BaseModel):
    id: int
    display_name: str
    plant_name: Optional[str] = None
    current_state: str = "healthy"
    state_emoji: str = "🌱"
    state_name: str = "Здоровое"
    watering_interval: int = 7
    last_watered: Optional[datetime] = None
    next_watering_date: Optional[date] = None
    needs_watering: bool = False
    water_status: str = ""
    photo_file_id: Optional[str] = None
    photo_url: Optional[str] = None
    saved_date: Optional[datetime] = None
    analysis: Optional[str] = None
    current_streak: int = 0
    max_streak: int = 0
    fertilizing_enabled: bool = False
    fertilizing_interval: Optional[int] = None
    last_fertilized: Optional[datetime] = None
    next_fertilizing_date: Optional[date] = None


class PlantListResponse(BaseModel):
    plants: List[PlantSummary]
    total: int


class AnalysisResponse(BaseModel):
    success: bool
    analysis: Optional[str] = None
    plant_name: Optional[str] = None
    confidence: Optional[float] = None
    watering_interval: Optional[int] = None
    state: Optional[str] = None
    fertilizing_enabled: Optional[bool] = None
    fertilizing_interval: Optional[int] = None
    error: Optional[str] = None
    temp_id: Optional[str] = None
    photo_url: Optional[str] = None


class SavePlantRequest(BaseModel):
    temp_id: str
    last_watered_days_ago: Optional[int] = None


class WaterPlantResponse(BaseModel):
    success: bool
    plant_name: str = ""
    next_watering_days: int = 7
    next_watering_date: Optional[date] = None
    current_streak: int = 0
    max_streak: int = 0
    watered_at: Optional[datetime] = None


class FertilizeResponse(BaseModel):
    success: bool
    plant_name: str = ""
    next_fertilizing_date: Optional[date] = None
    interval: int = 30


class UpdatePlantRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    fertilizing_enabled: Optional[bool] = None


class RenamePlantRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class PlantPhotoEntry(BaseModel):
    id: int
    photo_url: str
    created_at: datetime


# === AI ===

class QuestionRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    plant_id: Optional[int] = None


class QuestionResponse(BaseModel):
    success: bool
    answer: Optional[str] = None
    model: Optional[str] = None
    plant_name: Optional[str] = None
    error: Optional[str] = None


# === SUBSCRIPTION ===

class PlanInfo(BaseModel):
    plan: str
    expires_at: Optional[datetime] = None
    days_left: Optional[int] = None
    auto_pay: bool = False
    is_grace_period: bool = False


class UsageStats(BaseModel):
    plan: str
    plants_count: int = 0
    plants_limit: str = "1"
    analyses_used: int = 0
    analyses_limit: str = "1"
    questions_used: int = 0
    questions_limit: str = "1"


class SubscriptionPlan(BaseModel):
    id: str
    label: str
    price: int              # текущая цена (со скидкой, если активна)
    original_price: int     # цена без скидки; равна price, если скидки нет
    days: int
    per_month: Optional[int] = None
    is_popular: bool = False


class DiscountInfo(BaseModel):
    percent: int
    ends_at: datetime
    label: str


class PlansResponse(BaseModel):
    plans: List[SubscriptionPlan]
    discount: Optional[DiscountInfo] = None


class CreatePaymentRequest(BaseModel):
    plan_id: str


class CreatePaymentResponse(BaseModel):
    success: bool
    payment_id: Optional[str] = None
    confirmation_url: Optional[str] = None
    error: Optional[str] = None


class RegisterDeviceRequest(BaseModel):
    fcm_token: str
    platform: str = "android"


class SuccessResponse(BaseModel):
    success: bool = True
    message: str = ""


class ErrorResponse(BaseModel):
    detail: str
