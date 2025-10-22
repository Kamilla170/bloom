import os
import asyncpg
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

class PlantDatabase:
    def __init__(self):
        self.database_url = os.getenv("DATABASE_URL")
        self.pool = None
        
    async def init_pool(self):
        """Инициализация пула соединений"""
        try:
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=3
            )
            await self.create_tables()
            logger.info("✅ База данных подключена")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            raise
            
    async def create_tables(self):
        """Создание таблиц"""
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    onboarding_completed BOOLEAN DEFAULT FALSE,
                    care_style_profile JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица настроек пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    reminder_time TEXT DEFAULT '09:00',
                    timezone TEXT DEFAULT 'Europe/Moscow',
                    reminder_enabled BOOLEAN DEFAULT TRUE,
                    monthly_photo_reminder BOOLEAN DEFAULT TRUE,
                    last_monthly_reminder TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # Таблица растений
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plants (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    analysis TEXT NOT NULL,
                    photo_file_id TEXT NOT NULL,
                    plant_name TEXT,
                    custom_name TEXT,
                    saved_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_watered TIMESTAMP,
                    watering_count INTEGER DEFAULT 0,
                    watering_interval INTEGER DEFAULT 5,
                    notes TEXT,
                    reminder_enabled BOOLEAN DEFAULT TRUE,
                    plant_type TEXT DEFAULT 'regular',
                    growing_id INTEGER,
                    current_state TEXT DEFAULT 'healthy',
                    state_changed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    state_changes_count INTEGER DEFAULT 0,
                    growth_stage TEXT DEFAULT 'young',
                    last_photo_analysis TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    environment_data JSONB,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # === НОВЫЕ ТАБЛИЦЫ ДЛЯ ПОЛНОГО КОНТЕКСТА ===
            
            # Полная история всех анализов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plant_analyses_full (
                    id SERIAL PRIMARY KEY,
                    plant_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    analysis_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    photo_file_id TEXT NOT NULL,
                    full_analysis TEXT NOT NULL,
                    ai_model TEXT DEFAULT 'gpt-4o',
                    confidence FLOAT,
                    identified_species TEXT,
                    detected_state TEXT,
                    detected_problems JSONB,
                    recommendations JSONB,
                    watering_advice TEXT,
                    lighting_advice TEXT,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # История вопросов и ответов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plant_qa_history (
                    id SERIAL PRIMARY KEY,
                    plant_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    question_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    question_text TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    ai_model TEXT DEFAULT 'gpt-4o',
                    context_used JSONB,
                    user_feedback TEXT,
                    follow_up_action TEXT,
                    problem_resolved BOOLEAN,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # История проблем и решений
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plant_problems_log (
                    id SERIAL PRIMARY KEY,
                    plant_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    problem_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    problem_type TEXT NOT NULL,
                    problem_description TEXT,
                    suspected_cause TEXT,
                    solution_tried TEXT,
                    solution_date TIMESTAMP,
                    result TEXT,
                    resolved BOOLEAN DEFAULT FALSE,
                    resolution_date TIMESTAMP,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # Паттерны ухода пользователя (обучение)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plant_user_patterns (
                    id SERIAL PRIMARY KEY,
                    plant_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    pattern_type TEXT NOT NULL,
                    pattern_data JSONB NOT NULL,
                    confidence FLOAT DEFAULT 0.5,
                    occurrences INTEGER DEFAULT 1,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # Условия содержания растения
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plant_environment (
                    id SERIAL PRIMARY KEY,
                    plant_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    location TEXT,
                    lighting TEXT,
                    humidity_level TEXT,
                    temperature_range TEXT,
                    air_circulation TEXT,
                    distance_from_window TEXT,
                    updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # Таблица истории состояний растений
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plant_state_history (
                    id SERIAL PRIMARY KEY,
                    plant_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    previous_state TEXT,
                    new_state TEXT NOT NULL,
                    change_reason TEXT,
                    change_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    photo_file_id TEXT,
                    ai_analysis TEXT,
                    watering_adjustment INTEGER DEFAULT 0,
                    feeding_adjustment INTEGER,
                    recommendations TEXT,
                    manual_event BOOLEAN DEFAULT FALSE,
                    event_type TEXT,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # Остальные таблицы
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS growing_plants (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    plant_name TEXT NOT NULL,
                    growth_method TEXT NOT NULL,
                    growing_plan TEXT NOT NULL,
                    task_calendar JSONB,
                    current_stage INTEGER DEFAULT 0,
                    total_stages INTEGER DEFAULT 4,
                    started_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    estimated_completion DATE,
                    status TEXT DEFAULT 'active',
                    notes TEXT,
                    photo_file_id TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS growth_stages (
                    id SERIAL PRIMARY KEY,
                    growing_plant_id INTEGER NOT NULL,
                    stage_number INTEGER NOT NULL,
                    stage_name TEXT NOT NULL,
                    stage_description TEXT NOT NULL,
                    estimated_duration_days INTEGER NOT NULL,
                    completed_date TIMESTAMP,
                    photo_file_id TEXT,
                    notes TEXT,
                    reminder_interval INTEGER DEFAULT 2,
                    FOREIGN KEY (growing_plant_id) REFERENCES growing_plants (id) ON DELETE CASCADE
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS growth_diary (
                    id SERIAL PRIMARY KEY,
                    growing_plant_id INTEGER NOT NULL,
                    entry_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    entry_type TEXT NOT NULL,
                    description TEXT,
                    photo_file_id TEXT,
                    stage_number INTEGER,
                    user_id BIGINT NOT NULL,
                    FOREIGN KEY (growing_plant_id) REFERENCES growing_plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS care_history (
                    id SERIAL PRIMARY KEY,
                    plant_id INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    action_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    plant_id INTEGER,
                    growing_plant_id INTEGER,
                    reminder_type TEXT NOT NULL,
                    next_date TIMESTAMP NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_sent TIMESTAMP,
                    send_count INTEGER DEFAULT 0,
                    stage_number INTEGER,
                    task_day INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (growing_plant_id) REFERENCES growing_plants (id) ON DELETE CASCADE
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    feedback_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    photo_file_id TEXT,
                    context_data TEXT,
                    status TEXT DEFAULT 'new',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # Добавляем новые колонки
            try:
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS current_state TEXT DEFAULT 'healthy'")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS state_changed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS state_changes_count INTEGER DEFAULT 0")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS growth_stage TEXT DEFAULT 'young'")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS last_photo_analysis TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS environment_data JSONB")
                await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS monthly_photo_reminder BOOLEAN DEFAULT TRUE")
                await conn.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS last_monthly_reminder TIMESTAMP")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS care_style_profile JSONB")
            except Exception as e:
                logger.info(f"Колонки уже существуют: {e}")
            
            # Индексы для оптимизации
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plants_user_id ON plants (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plants_state ON plants (current_state)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plant_state_history_plant_id ON plant_state_history (plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plant_analyses_full_plant_id ON plant_analyses_full (plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plant_qa_history_plant_id ON plant_qa_history (plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plant_problems_log_plant_id ON plant_problems_log (plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plant_user_patterns_plant_id ON plant_user_patterns (plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_growing_plants_user_id ON growing_plants (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON reminders (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_next_date ON reminders (next_date, is_active)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_care_history_plant_id ON care_history (plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_growth_stages_growing_plant_id ON growth_stages (growing_plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_growth_diary_growing_plant_id ON growth_diary (growing_plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback (user_id)")

    # В конце метода create_tables, после всех await conn.execute(...)

# === МИГРАЦИЯ ДЛЯ СИСТЕМЫ СТАТИСТИКИ ===
logger.info("📊 Применение миграции для системы статистики...")

await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_activity TIMESTAMP")
await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_last_activity ON users(last_activity DESC)")

await conn.execute("""
    CREATE TABLE IF NOT EXISTS daily_stats (
        id SERIAL PRIMARY KEY,
        stat_date DATE UNIQUE NOT NULL,
        total_users INTEGER NOT NULL DEFAULT 0,
        new_users INTEGER NOT NULL DEFAULT 0,
        active_users INTEGER NOT NULL DEFAULT 0,
        users_watered INTEGER NOT NULL DEFAULT 0,
        users_added_plants INTEGER NOT NULL DEFAULT 0,
        total_waterings INTEGER NOT NULL DEFAULT 0,
        total_plants_added INTEGER NOT NULL DEFAULT 0,
        analyses_count INTEGER NOT NULL DEFAULT 0,
        questions_count INTEGER NOT NULL DEFAULT 0,
        growing_started INTEGER NOT NULL DEFAULT 0,
        feedback_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(stat_date DESC)")
await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_created ON daily_stats(created_at DESC)")

logger.info("✅ Миграция для системы статистики применена")
    
    def extract_plant_name_from_analysis(self, analysis_text: str) -> str:
        """Извлекает название растения из текста анализа"""
        if not analysis_text:
            return None
        
        lines = analysis_text.split('\n')
        for line in lines:
            if line.startswith("РАСТЕНИЕ:"):
                plant_name = line.replace("РАСТЕНИЕ:", "").strip()
                
                if "(" in plant_name:
                    plant_name = plant_name.split("(")[0].strip()
                
                plant_name = plant_name.split("достоверность:")[0].strip()
                plant_name = plant_name.split("%")[0].strip()
                plant_name = plant_name.replace("🌿", "").strip()
                
                if 3 <= len(plant_name) <= 80 and not plant_name.lower().startswith(("неизвестн", "неопознан", "комнатное растение")):
                    return plant_name
        
        return None
    
    # === МЕТОДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ===
    
    async def add_user(self, user_id: int, username: str = None, first_name: str = None):
        """Добавить или обновить пользователя"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) 
                DO UPDATE SET 
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name
            """, user_id, username, first_name)
            
            await conn.execute("""
                INSERT INTO user_settings (user_id)
                VALUES ($1)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id)
    
    async def get_user_reminder_settings(self, user_id: int) -> Optional[Dict]:
        """Получить настройки напоминаний пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT reminder_time, timezone, reminder_enabled, monthly_photo_reminder
                FROM user_settings
                WHERE user_id = $1
            """, user_id)
            
            if row:
                return dict(row)
            return None
    
    # === МЕТОДЫ ДЛЯ РАСТЕНИЙ С СОСТОЯНИЯМИ ===
    
    async def save_plant(self, user_id: int, analysis: str, photo_file_id: str, plant_name: str = None) -> int:
        """Сохранить растение"""
        async with self.pool.acquire() as conn:
            if not plant_name:
                plant_name = self.extract_plant_name_from_analysis(analysis)
            
            plant_id = await conn.fetchval("""
                INSERT INTO plants (user_id, analysis, photo_file_id, plant_name, last_photo_analysis)
                VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                RETURNING id
            """, user_id, analysis, photo_file_id, plant_name)
            
            try:
                await conn.execute("""
                    INSERT INTO care_history (plant_id, action_type, notes)
                    VALUES ($1, 'added', 'Растение добавлено в коллекцию')
                """, plant_id)
            except Exception as e:
                logger.error(f"Ошибка добавления в историю: {e}")
            
            return plant_id
    
    async def get_plant_with_state(self, plant_id: int, user_id: int = None) -> Optional[Dict]:
        """Получить растение с информацией о состоянии"""
        async with self.pool.acquire() as conn:
            query = """
                SELECT p.*, 
                       COALESCE(p.custom_name, p.plant_name, 'Растение #' || p.id) as display_name
                FROM plants p
                WHERE p.id = $1
            """
            params = [plant_id]
            
            if user_id:
                query += " AND p.user_id = $2"
                params.append(user_id)
            
            row = await conn.fetchrow(query, *params)
            
            if row:
                return dict(row)
            return None
    
    async def update_plant_state(self, plant_id: int, user_id: int, new_state: str, 
                                change_reason: str = None, photo_file_id: str = None,
                                ai_analysis: str = None, watering_adjustment: int = 0,
                                feeding_adjustment: int = None, recommendations: str = None,
                                manual_event: bool = False, event_type: str = None):
        """Обновить состояние растения"""
        async with self.pool.acquire() as conn:
            current = await conn.fetchrow("""
                SELECT current_state FROM plants WHERE id = $1 AND user_id = $2
            """, plant_id, user_id)
            
            if not current:
                return False
            
            previous_state = current['current_state']
            
            await conn.execute("""
                UPDATE plants 
                SET current_state = $1,
                    state_changed_date = CURRENT_TIMESTAMP,
                    state_changes_count = COALESCE(state_changes_count, 0) + 1
                WHERE id = $2 AND user_id = $3
            """, new_state, plant_id, user_id)
            
            await conn.execute("""
                INSERT INTO plant_state_history 
                (plant_id, user_id, previous_state, new_state, change_reason, 
                 photo_file_id, ai_analysis, watering_adjustment, feeding_adjustment,
                 recommendations, manual_event, event_type)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """, plant_id, user_id, previous_state, new_state, change_reason,
                photo_file_id, ai_analysis, watering_adjustment, feeding_adjustment,
                recommendations, manual_event, event_type)
            
            if watering_adjustment != 0:
                await conn.execute("""
                    UPDATE plants 
                    SET watering_interval = GREATEST(2, LEAST(15, 
                        COALESCE(watering_interval, 5) + $1))
                    WHERE id = $2
                """, watering_adjustment, plant_id)
            
            return True
    
    async def get_plant_state_history(self, plant_id: int, limit: int = 10) -> List[Dict]:
        """Получить историю изменений состояний"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM plant_state_history
                WHERE plant_id = $1
                ORDER BY change_date DESC
                LIMIT $2
            """, plant_id, limit)
            
            return [dict(row) for row in rows]
    
    async def get_plants_for_monthly_reminder(self) -> List[Dict]:
        """Получить растения для месячного напоминания"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.*, 
                       COALESCE(p.custom_name, p.plant_name, 'Растение #' || p.id) as display_name
                FROM plants p
                JOIN user_settings us ON p.user_id = us.user_id
                WHERE p.plant_type = 'regular'
                  AND us.monthly_photo_reminder = TRUE
                  AND (
                    p.last_photo_analysis IS NULL 
                    OR p.last_photo_analysis < CURRENT_TIMESTAMP - INTERVAL '30 days'
                  )
                  AND (
                    us.last_monthly_reminder IS NULL
                    OR us.last_monthly_reminder < CURRENT_TIMESTAMP - INTERVAL '30 days'
                  )
            """)
            
            return [dict(row) for row in rows]
    
    async def mark_monthly_reminder_sent(self, user_id: int):
        """Отметить отправку месячного напоминания"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_settings
                SET last_monthly_reminder = CURRENT_TIMESTAMP
                WHERE user_id = $1
            """, user_id)
    
    async def update_plant_name(self, plant_id: int, user_id: int, new_name: str):
        """Обновить название растения"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE plants 
                SET custom_name = $1 
                WHERE id = $2 AND user_id = $3
            """, new_name, plant_id, user_id)
            
            try:
                await conn.execute("""
                    INSERT INTO care_history (plant_id, action_type, notes)
                    VALUES ($1, 'renamed', $2)
                """, plant_id, f'Переименовано в "{new_name}"')
            except Exception as e:
                logger.error(f"Ошибка добавления в историю: {e}")
    
    async def update_plant_watering_interval(self, plant_id: int, interval_days: int):
        """Обновить интервал полива"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE plants 
                SET watering_interval = $1 
                WHERE id = $2
            """, interval_days, plant_id)
    
    async def get_plant_by_id(self, plant_id: int, user_id: int = None) -> Optional[Dict]:
        """Получить растение по ID"""
        async with self.pool.acquire() as conn:
            query = """
                SELECT id, user_id, analysis, photo_file_id, plant_name, custom_name,
                       saved_date, last_watered, 
                       COALESCE(watering_count, 0) as watering_count,
                       COALESCE(watering_interval, 5) as watering_interval,
                       COALESCE(reminder_enabled, TRUE) as reminder_enabled,
                       notes, plant_type, growing_id,
                       current_state, state_changed_date, state_changes_count,
                       growth_stage, last_photo_analysis
                FROM plants 
                WHERE id = $1
            """
            params = [plant_id]
            
            if user_id:
                query += " AND user_id = $2"
                params.append(user_id)
            
            row = await conn.fetchrow(query, *params)
            
            if row:
                display_name = row['custom_name'] or row['plant_name']
                if not display_name:
                    extracted_name = self.extract_plant_name_from_analysis(row['analysis'])
                    display_name = extracted_name or f"Растение #{row['id']}"
                
                result = dict(row)
                result['display_name'] = display_name
                return result
            return None
    
    async def get_user_plants(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Получить все растения пользователя"""
        async with self.pool.acquire() as conn:
            regular_rows = await conn.fetch("""
                SELECT id, analysis, photo_file_id, plant_name, custom_name, 
                       saved_date, last_watered, 
                       COALESCE(watering_count, 0) as watering_count,
                       COALESCE(watering_interval, 5) as watering_interval,
                       COALESCE(reminder_enabled, TRUE) as reminder_enabled,
                       notes, plant_type, growing_id,
                       current_state, state_changed_date, state_changes_count
                FROM plants 
                WHERE user_id = $1 AND plant_type = 'regular'
                ORDER BY saved_date DESC
                LIMIT $2
            """, user_id, limit)
            
            growing_rows = await conn.fetch("""
                SELECT gp.id, gp.plant_name, gp.photo_file_id, gp.started_date,
                       gp.current_stage, gp.total_stages, gp.status,
                       gs.stage_name as current_stage_name
                FROM growing_plants gp
                LEFT JOIN growth_stages gs ON gp.id = gs.growing_plant_id AND gs.stage_number = gp.current_stage + 1
                WHERE gp.user_id = $1 AND gp.status = 'active'
                ORDER BY gp.started_date DESC
            """, user_id)
            
            plants = []
            
            for row in regular_rows:
                display_name = None
                
                if row['custom_name']:
                    display_name = row['custom_name']
                elif row['plant_name']:
                    display_name = row['plant_name']
                else:
                    extracted_name = self.extract_plant_name_from_analysis(row['analysis'])
                    if extracted_name:
                        display_name = extracted_name
                        try:
                            await conn.execute("""
                                UPDATE plants SET plant_name = $1 WHERE id = $2
                            """, extracted_name, row['id'])
                        except:
                            pass
                
                if not display_name or display_name.lower().startswith(("неизвестн", "неопознан")):
                    display_name = f"Растение #{row['id']}"
                
                plant_data = dict(row)
                plant_data['display_name'] = display_name
                plant_data['type'] = 'regular'
                plants.append(plant_data)
            
            for row in growing_rows:
                stage_info = f"Этап {row['current_stage']}/{row['total_stages']}"
                if row['current_stage_name']:
                    stage_info += f": {row['current_stage_name']}"
                
                plant_data = {
                    'id': f"growing_{row['id']}",
                    'display_name': f"{row['plant_name']} 🌱",
                    'saved_date': row['started_date'],
                    'photo_file_id': row['photo_file_id'] or 'default_growing',
                    'last_watered': None,
                    'watering_count': 0,
                    'type': 'growing',
                    'growing_id': row['id'],
                    'stage_info': stage_info,
                    'status': row['status']
                }
                plants.append(plant_data)
            
            plants.sort(key=lambda x: x['saved_date'], reverse=True)
            
            return plants[:limit]
    
    async def update_watering(self, user_id: int, plant_id: int = None):
        """Отметить полив"""
        async with self.pool.acquire() as conn:
            if plant_id:
                await conn.execute("""
                    UPDATE plants 
                    SET last_watered = CURRENT_TIMESTAMP,
                        watering_count = COALESCE(watering_count, 0) + 1
                    WHERE user_id = $1 AND id = $2
                """, user_id, plant_id)
                
                try:
                    await conn.execute("""
                        INSERT INTO care_history (plant_id, action_type, notes)
                        VALUES ($1, 'watered', 'Растение полито')
                    """, plant_id)
                except Exception as e:
                    logger.error(f"Ошибка добавления в историю: {e}")
            else:
                plant_ids = await conn.fetch("""
                    SELECT id FROM plants WHERE user_id = $1
                """, user_id)
                
                await conn.execute("""
                    UPDATE plants 
                    SET last_watered = CURRENT_TIMESTAMP,
                        watering_count = COALESCE(watering_count, 0) + 1
                    WHERE user_id = $1
                """, user_id)
                
                for plant_row in plant_ids:
                    try:
                        await conn.execute("""
                            INSERT INTO care_history (plant_id, action_type, notes)
                            VALUES ($1, 'watered', 'Растение полито (массовый полив)')
                        """, plant_row['id'])
                    except Exception as e:
                        logger.error(f"Ошибка добавления в историю: {e}")
    
    async def delete_plant(self, user_id: int, plant_id: int):
        """Удалить растение"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM plants 
                WHERE user_id = $1 AND id = $2
            """, user_id, plant_id)
    
    # === МЕТОДЫ ДЛЯ НАПОМИНАНИЙ ===
    
    async def create_reminder(self, user_id: int, plant_id: int, reminder_type: str, next_date: datetime):
        """Создать напоминание"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE reminders 
                SET is_active = FALSE 
                WHERE user_id = $1 AND plant_id = $2 AND reminder_type = $3 AND is_active = TRUE
            """, user_id, plant_id, reminder_type)
            
            await conn.execute("""
                INSERT INTO reminders (user_id, plant_id, reminder_type, next_date)
                VALUES ($1, $2, $3, $4)
            """, user_id, plant_id, reminder_type, next_date)
    
    # === МЕТОДЫ ДЛЯ ВЫРАЩИВАНИЯ ===
    
    async def create_growing_plant(self, user_id: int, plant_name: str, growth_method: str, 
                                 growing_plan: str, task_calendar: dict = None, 
                                 photo_file_id: str = None) -> int:
        """Создать выращиваемое растение"""
        async with self.pool.acquire() as conn:
            calendar_json = json.dumps(task_calendar) if task_calendar else None
            
            growing_id = await conn.fetchval("""
                INSERT INTO growing_plants 
                (user_id, plant_name, growth_method, growing_plan, task_calendar, photo_file_id, estimated_completion)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
            """, user_id, plant_name, growth_method, growing_plan, calendar_json, photo_file_id, 
                datetime.now().date() + timedelta(days=90))
            
            await self.create_growth_stages(growing_id, growing_plan)
            
            await conn.execute("""
                INSERT INTO growth_diary (growing_plant_id, user_id, entry_type, description)
                VALUES ($1, $2, 'started', $3)
            """, growing_id, user_id, f"Начато выращивание {plant_name}")
            
            return growing_id
    
    async def create_growth_stages(self, growing_plant_id: int, growing_plan: str):
        """Создать этапы выращивания"""
        stages = self.parse_growing_plan_to_stages(growing_plan)
        
        async with self.pool.acquire() as conn:
            for i, stage in enumerate(stages):
                await conn.execute("""
                    INSERT INTO growth_stages 
                    (growing_plant_id, stage_number, stage_name, stage_description, estimated_duration_days)
                    VALUES ($1, $2, $3, $4, $5)
                """, growing_plant_id, i + 1, stage['name'], stage['description'], stage['duration'])
    
    def parse_growing_plan_to_stages(self, growing_plan: str) -> List[Dict]:
        """Парсит план в этапы"""
        stages = []
        lines = growing_plan.split('\n')
        current_stage = None
        
        for line in lines:
            line = line.strip()
            if line.startswith('🌱 ЭТАП') or line.startswith('🌿 ЭТАП') or line.startswith('🌸 ЭТАП'):
                if current_stage:
                    stages.append(current_stage)
                
                stage_info = line.split(':', 1)
                if len(stage_info) > 1:
                    stage_name = stage_info[1].strip()
                    duration = 7
                    if '(' in stage_name and ')' in stage_name:
                        duration_text = stage_name[stage_name.find('(')+1:stage_name.find(')')]
                        import re
                        numbers = re.findall(r'\d+', duration_text)
                        if numbers:
                            duration = int(numbers[0])
                    
                    current_stage = {
                        'name': stage_name.split('(')[0].strip(),
                        'description': '',
                        'duration': duration
                    }
                    
            elif current_stage and line.startswith('•'):
                current_stage['description'] += line + '\n'
        
        if current_stage:
            stages.append(current_stage)
        
        if not stages:
            stages = [
                {'name': 'Подготовка и посадка', 'description': 'Подготовка и посадка', 'duration': 7},
                {'name': 'Прорастание', 'description': 'Появление всходов', 'duration': 14},
                {'name': 'Рост и развитие', 'description': 'Активный рост', 'duration': 30},
                {'name': 'Взрослое растение', 'description': 'Готово к пересадке', 'duration': 30}
            ]
        
        return stages
    
    async def get_growing_plant_by_id(self, growing_id: int, user_id: int = None) -> Optional[Dict]:
        """Получить выращиваемое растение"""
        async with self.pool.acquire() as conn:
            query = """
                SELECT gp.*, gs.stage_name as current_stage_name, gs.stage_description as current_stage_desc
                FROM growing_plants gp
                LEFT JOIN growth_stages gs ON gp.id = gs.growing_plant_id AND gs.stage_number = gp.current_stage + 1
                WHERE gp.id = $1
            """
            params = [growing_id]
            
            if user_id:
                query += " AND gp.user_id = $2"
                params.append(user_id)
            
            row = await conn.fetchrow(query, *params)
            
            if row:
                return dict(row)
            return None
    
    async def create_growing_reminder(self, growing_id: int, user_id: int, reminder_type: str, 
                                    next_date: datetime, stage_number: int = None, task_day: int = None):
        """Создать напоминание для выращивания"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE reminders 
                SET is_active = FALSE 
                WHERE growing_plant_id = $1 AND reminder_type = $2 AND is_active = TRUE
            """, growing_id, reminder_type)
            
            await conn.execute("""
                INSERT INTO reminders 
                (user_id, growing_plant_id, reminder_type, next_date, stage_number, task_day)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, user_id, growing_id, reminder_type, next_date, stage_number, task_day)
    
    # === МЕТОДЫ ДЛЯ ОБРАТНОЙ СВЯЗИ ===
    
    async def save_feedback(self, user_id: int, username: str, feedback_type: str, 
                          message: str, photo_file_id: str = None, context_data: str = None) -> int:
        """Сохранить обратную связь"""
        async with self.pool.acquire() as conn:
            feedback_id = await conn.fetchval("""
                INSERT INTO feedback (user_id, username, feedback_type, message, photo_file_id, context_data)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """, user_id, username, feedback_type, message, photo_file_id, context_data)
            
            return feedback_id
    
    # === МЕТОДЫ ДЛЯ СТАТИСТИКИ ===
    
    async def get_user_stats(self, user_id: int) -> Dict:
        """Статистика пользователя"""
        async with self.pool.acquire() as conn:
            regular_stats = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_plants,
                    COUNT(CASE WHEN last_watered IS NOT NULL THEN 1 END) as watered_plants,
                    COALESCE(SUM(watering_count), 0) as total_waterings,
                    COUNT(CASE WHEN reminder_enabled = TRUE THEN 1 END) as plants_with_reminders,
                    MIN(saved_date) as first_plant_date,
                    MAX(last_watered) as last_watered_date
                FROM plants 
                WHERE user_id = $1 AND plant_type = 'regular'
            """, user_id)
            
            growing_stats = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_growing,
                    COUNT(CASE WHEN status = 'active' THEN 1 END) as active_growing,
                    COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed_growing
                FROM growing_plants 
                WHERE user_id = $1
            """, user_id)
            
            feedback_stats = await conn.fetchrow("""
                SELECT COUNT(*) as total_feedback
                FROM feedback 
                WHERE user_id = $1
            """, user_id)
            
            return {
                'total_plants': regular_stats['total_plants'] or 0,
                'watered_plants': regular_stats['watered_plants'] or 0,
                'total_waterings': regular_stats['total_waterings'] or 0,
                'plants_with_reminders': regular_stats['plants_with_reminders'] or 0,
                'first_plant_date': regular_stats['first_plant_date'],
                'last_watered_date': regular_stats['last_watered_date'],
                'total_growing': growing_stats['total_growing'] or 0,
                'active_growing': growing_stats['active_growing'] or 0,
                'completed_growing': growing_stats['completed_growing'] or 0,
                'total_feedback': feedback_stats['total_feedback'] or 0
            }
    
    # === МЕТОДЫ ДЛЯ ПОЛНОГО КОНТЕКСТА РАСТЕНИЙ ===
    
    async def save_full_analysis(self, plant_id: int, user_id: int, photo_file_id: str,
                                full_analysis: str, confidence: float, identified_species: str,
                                detected_state: str, detected_problems: dict = None,
                                recommendations: dict = None, watering_advice: str = None,
                                lighting_advice: str = None) -> int:
        """Сохранить полный анализ растения"""
        async with self.pool.acquire() as conn:
            analysis_id = await conn.fetchval("""
                INSERT INTO plant_analyses_full 
                (plant_id, user_id, photo_file_id, full_analysis, confidence, 
                 identified_species, detected_state, detected_problems, recommendations,
                 watering_advice, lighting_advice)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
            """, plant_id, user_id, photo_file_id, full_analysis, confidence,
                identified_species, detected_state, 
                json.dumps(detected_problems) if detected_problems else None,
                json.dumps(recommendations) if recommendations else None,
                watering_advice, lighting_advice)
            
            return analysis_id
    
    async def get_plant_analyses_history(self, plant_id: int, limit: int = 10) -> List[Dict]:
        """Получить историю анализов растения"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM plant_analyses_full
                WHERE plant_id = $1
                ORDER BY analysis_date DESC
                LIMIT $2
            """, plant_id, limit)
            
            return [dict(row) for row in rows]
    
    async def save_qa_interaction(self, plant_id: int, user_id: int, question: str,
                                 answer: str, context_used: dict = None) -> int:
        """Сохранить вопрос-ответ"""
        async with self.pool.acquire() as conn:
            qa_id = await conn.fetchval("""
                INSERT INTO plant_qa_history 
                (plant_id, user_id, question_text, answer_text, context_used)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """, plant_id, user_id, question, answer,
                json.dumps(context_used) if context_used else None)
            
            return qa_id
    
    async def get_plant_qa_history(self, plant_id: int, limit: int = 10) -> List[Dict]:
        """Получить историю вопросов о растении"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM plant_qa_history
                WHERE plant_id = $1
                ORDER BY question_date DESC
                LIMIT $2
            """, plant_id, limit)
            
            return [dict(row) for row in rows]
    
    async def log_plant_problem(self, plant_id: int, user_id: int, problem_type: str,
                               description: str, suspected_cause: str = None) -> int:
        """Зафиксировать проблему растения"""
        async with self.pool.acquire() as conn:
            problem_id = await conn.fetchval("""
                INSERT INTO plant_problems_log 
                (plant_id, user_id, problem_type, problem_description, suspected_cause)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """, plant_id, user_id, problem_type, description, suspected_cause)
            
            return problem_id
    
    async def get_plant_problems_history(self, plant_id: int, limit: int = 20) -> List[Dict]:
        """Получить историю проблем растения"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM plant_problems_log
                WHERE plant_id = $1
                ORDER BY problem_date DESC
                LIMIT $2
            """, plant_id, limit)
            
            return [dict(row) for row in rows]
    
    async def get_unresolved_problems(self, plant_id: int) -> List[Dict]:
        """Получить нерешенные проблемы"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM plant_problems_log
                WHERE plant_id = $1 AND resolved = FALSE
                ORDER BY problem_date DESC
            """, plant_id)
            
            return [dict(row) for row in rows]
    
    async def save_user_pattern(self, plant_id: int, user_id: int, pattern_type: str,
                               pattern_data: dict, confidence: float = 0.5):
        """Сохранить паттерн ухода пользователя"""
        async with self.pool.acquire() as conn:
            existing = await conn.fetchrow("""
                SELECT id, occurrences, confidence FROM plant_user_patterns
                WHERE plant_id = $1 AND user_id = $2 AND pattern_type = $3
            """, plant_id, user_id, pattern_type)
            
            if existing:
                new_confidence = min(1.0, existing['confidence'] + 0.1)
                await conn.execute("""
                    UPDATE plant_user_patterns
                    SET pattern_data = $1,
                        confidence = $2,
                        occurrences = occurrences + 1,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE id = $3
                """, json.dumps(pattern_data), new_confidence, existing['id'])
            else:
                await conn.execute("""
                    INSERT INTO plant_user_patterns
                    (plant_id, user_id, pattern_type, pattern_data, confidence)
                    VALUES ($1, $2, $3, $4, $5)
                """, plant_id, user_id, pattern_type, json.dumps(pattern_data), confidence)
    
    async def get_user_patterns(self, plant_id: int, min_confidence: float = 0.3) -> List[Dict]:
        """Получить паттерны ухода пользователя"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM plant_user_patterns
                WHERE plant_id = $1 AND confidence >= $2
                ORDER BY confidence DESC, last_updated DESC
            """, plant_id, min_confidence)
            
            return [dict(row) for row in rows]
    
    async def get_plant_environment(self, plant_id: int) -> Optional[Dict]:
        """Получить условия содержания растения"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM plant_environment WHERE plant_id = $1
            """, plant_id)
            
            if row:
                return dict(row)
            return None
    
    async def close(self):
        """Закрыть соединения"""
        if self.pool:
            await self.pool.close()
            logger.info("✅ База данных закрыта")

# Глобальный экземпляр
db = None

async def init_database():
    """Инициализация базы данных"""
    global db
    db = PlantDatabase()
    await db.init_pool()
    return db

async def get_db():
    """Получить экземпляр базы данных"""
    global db
    if db is None:
        db = await init_database()
    return db
