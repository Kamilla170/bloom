"""
Pydantic схемы для REST API
"""

from datetime import datetime
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


# === PLANTS ===

class PlantSummary(BaseModel):
    id: int
    display_name: str
    plant_name: Optional[str] = None
    current_state: str = "healthy"
    state_emoji: str = "🌱"
    watering_interval: int = 7
    last_watered: Optional[datetime] = None
    water_status: str = ""
    photo_file_id: Optional[str] = None
    photo_url: Optional[str] = None
    saved_date: Optional[datetime] = None


class PlantDetail(BaseModel):
    id: int
    display_name: str
    plant_name: Optional[str] = None
    current_state: str = "healthy"
    state_emoji: str = "🌱"
    state_name: str = "Здоровое"
    watering_interval: int = 7
    last_watered: Optional[datetime] = None
    water_status: str = ""
    photo_file_id: Optional[str] = None
    photo_url: Optional[str] = None
    saved_date: Optional[datetime] = None
    state_changes_count: int = 0
    growth_stage: str = "young"
    analysis: Optional[str] = None


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
    watered_at: Optional[datetime] = None


class RenamePlantRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class StateHistoryEntry(BaseModel):
    date: Optional[datetime] = None
    from_state: Optional[str] = None
    to_state: str
    reason: Optional[str] = None
    emoji_from: str = ""
    emoji_to: str = "🌱"


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
    price: int
    days: int
    per_month: Optional[int] = None


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
