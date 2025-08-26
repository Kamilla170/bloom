# 🌸 Bloom - AI Plant Care Assistant

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![aiogram](https://img.shields.io/badge/aiogram-3.2.0-blue.svg)](https://docs.aiogram.dev/)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o-green.svg)](https://openai.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> Умный Telegram-бот для ухода за растениями с поддержкой ИИ

## ✨ Возможности

### 🔍 **Анализ растений**
- 📸 Определение вида растения по фотографии
- 🌿 Рекомендации по уходу и поливу
- 🔬 Диагностика заболеваний и вредителей
- 💡 Персонализированные советы

### 📅 **Умные уведомления**
- 💧 Автоматические напоминания о поливе
- 🌱 Напоминания о подкормке
- 📋 Персонализированное расписание ухода
- ⏰ Настраиваемое время уведомлений

### 📊 **Аналитика и журнал**
- 📝 Журнал всех действий по уходу
- 📈 Статистика за месяц/год
- 🏆 Рейтинг ухода за растениями
- 📷 Фотоархив растений

### 🤖 **ИИ-консультант**
- ❓ Ответы на вопросы о растениях
- 🎯 Учет контекста ваших растений
- 🧠 Обучение на ваших данных
- 💬 Естественное общение

## 🚀 Быстрый старт

### Предварительные требования
- Python 3.11+
- Telegram Bot Token ([получить у @BotFather](https://t.me/BotFather))
- OpenAI API Key ([получить здесь](https://platform.openai.com/api-keys))

### Установка

1. **Клонируйте репозиторий:**
```bash
git clone https://github.com/Kamilla170/bloom.git
cd bloom
```

2. **Создайте виртуальное окружение:**
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate     # Windows
```

3. **Установите зависимости:**
```bash
pip install -r requirements.txt
```

4. **Настройте переменные окружения:**
```bash
cp .env.example .env
# Отредактируйте .env файл с вашими токенами
```

5. **Инициализируйте базу данных:**
```bash
python -m alembic upgrade head
```

6. **Запустите бота:**
```bash
python -m app.main
```

## 🐳 Запуск с Docker

1. **Соберите и запустите:**
```bash
docker-compose up -d
```

2. **Просмотр логов:**
```bash
docker-compose logs -f bot
```

## 🛠 Разработка

### Структура проекта
```
bloom/
├── app/                 # Основное приложение
│   ├── bot/            # Логика бота
│   ├── database/       # Модели и БД
│   ├── services/       # Внешние сервисы
│   └── utils/          # Утилиты
├── migrations/         # Миграции БД
├── tests/             # Тесты
└── docs/              # Документация
```

### Запуск тестов
```bash
pytest tests/
```

### Создание миграции
```bash
alembic revision --autogenerate -m "описание изменений"
alembic upgrade head
```

## 📚 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Запуск бота и главное меню |
| `/help` | Справка по командам |
| `/health` | Проверка здоровья растения по фото |
| `/schedule` | Генерация расписания ухода |
| `/stats` | Статистика ухода |
| `/log` | Журнал действий |
| `/settings` | Настройки бота |

## 🏗 Архитектура

### Технологии
- **Framework:** aiogram 3.x (async/await)
- **AI:** OpenAI GPT-4o Vision
- **Database:** PostgreSQL + SQLAlchemy
- **Cache:** Redis
- **Scheduler:** APScheduler
- **Images:** Pillow

### Основные компоненты
- **Bot Handlers** - обработка сообщений
- **AI Services** - интеграция с OpenAI
- **Database Layer** - работа с данными
- **Notification System** - планирование уведомлений
- **Image Processing** - обработка фотографий

## 🔧 Конфигурация

### Переменные окружения

| Переменная | Описание | Обязательная |
|------------|----------|--------------|
| `BOT_TOKEN` | Токен Telegram бота | ✅ |
| `OPENAI_API_KEY` | Ключ OpenAI API | ✅ |
| `DATABASE_URL` | URL базы данных | ✅ |
| `REDIS_URL` | URL Redis | ❌ |
| `WEBHOOK_URL` | URL для webhook | ❌ |
| `LOG_LEVEL` | Уровень логирования | ❌ |

### Production настройки
- Используйте PostgreSQL вместо SQLite
- Настройте Redis для кеширования
- Включите webhook режим
- Настройте мониторинг и алерты

## 📖 API Integration

### OpenAI GPT-4 Vision
Бот использует самую современную модель для:
- Распознавания растений по фотографиям
- Диагностики проблем и заболеваний
- Генерации персонализированных советов

### Возможные расширения
- Weather API для климатических советов
- IoT интеграция для датчиков
- Календарные системы
- E-commerce интеграция

## 🤝 Вклад в проект

1. Форкните проект
2. Создайте feature branch (`git checkout -b feature/AmazingFeature`)
3. Закоммитьте изменения (`git commit -m 'Add some AmazingFeature'`)
4. Запушьте в branch (`git push origin feature/AmazingFeature`)
5. Создайте Pull Request

## 📝 Roadmap

### v1.0 (Текущая версия)
- [x] Базовая функциональность бота
- [x] Распознавание растений
- [x] Система уведомлений
- [x] Журнал ухода

### v1.1 (Планируется)
- [ ] Веб-интерфейс для управления
- [ ] Социальные функции
- [ ] Интеграция с погодными API
- [ ] Мобильное приложение

### v2.0 (Будущее)
- [ ] IoT интеграция
- [ ] AR функции
- [ ] Marketplace растений
- [ ] Продвинутая аналитика

## 📄 Лицензия

Этот проект лицензирован под MIT License - см. файл [LICENSE](LICENSE) для деталей.

## 👥 Авторы

- **Kamilla170** - *Initial work* - [GitHub](https://github.com/Kamilla170)

## 🙏 Благодарности

- OpenAI за потрясающий GPT-4 Vision API
- Команде aiogram за отличный фреймворк
- Сообществу разработчиков за вдохновение

## 📞 Поддержка

Если у вас есть вопросы или предложения:
- Создайте [Issue](https://github.com/Kamilla170/bloom/issues)
- Напишите в [Discussions](https://github.com/Kamilla170/bloom/discussions)

---

<div align="center">
  <strong>🌸 Bloom - Пусть твои растения цветут! 🌸</strong>
</div>
