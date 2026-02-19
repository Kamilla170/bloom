from aiogram.fsm.state import State, StatesGroup


class PlantStates(StatesGroup):
    """Состояния для работы с растениями"""
    waiting_question = State()
    editing_plant_name = State()
    choosing_plant_to_grow = State()
    planting_setup = State()
    waiting_growing_photo = State()
    adding_diary_entry = State()
    onboarding_welcome = State()
    onboarding_demo = State()
    onboarding_quick_start = State()
    waiting_state_update_photo = State()
    
    # Новые состояния для указания даты полива при добавлении растения
    waiting_last_watering = State()  # Ожидание выбора даты последнего полива
    
    # Новые состояния для указания даты последнего полива при сохранении
    waiting_last_watering = State()


class FeedbackStates(StatesGroup):
    """Состояния для обратной связи"""
    choosing_type = State()
    writing_message = State()


class AdminStates(StatesGroup):
    """Состояния для админ-переписки"""
    waiting_user_reply = State()
    waiting_admin_reply = State()
