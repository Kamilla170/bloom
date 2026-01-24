from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

def main_menu():
    """Главное меню"""
    keyboard = [
        [
            InlineKeyboardButton(text="🌱 Добавить растение", callback_data="add_plant"),
            InlineKeyboardButton(text="🌿 Вырастить с нуля", callback_data="grow_from_scratch")
        ],
        [
            InlineKeyboardButton(text="📸 Анализ растения", callback_data="analyze"),
            InlineKeyboardButton(text="🤖 Спросить ИИ", callback_data="question")
        ],
        [
            InlineKeyboardButton(text="🌿 Мои растения", callback_data="my_plants"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats")
        ],
        [
            InlineKeyboardButton(text="📝 Обратная связь", callback_data="feedback"),
            InlineKeyboardButton(text="ℹ️ Справка", callback_data="help")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def simple_back_menu():
    """Простая кнопка назад"""
    keyboard = [
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
