import os
import asyncpg
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging

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
            print("✅ База данных подключена")
        except Exception as e:
            print(f"❌ Ошибка подключения к БД: {e}")
            
    async def create_tables(self):
        """Создание таблиц включая новые для выращивания"""
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # Таблица растений с улучшенной структурой
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
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # НОВАЯ ТАБЛИЦА: Выращиваемые растения
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS growing_plants (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    plant_name TEXT NOT NULL,
                    growth_method TEXT NOT NULL,
                    growing_plan TEXT NOT NULL,
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
            
            # НОВАЯ ТАБЛИЦА: Этапы выращивания
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
            
            # НОВАЯ ТАБЛИЦА: Дневник роста
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
            
            # Таблица истории ухода
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
            
            # Обновленная таблица напоминаний
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
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE,
                    FOREIGN KEY (growing_plant_id) REFERENCES growing_plants (id) ON DELETE CASCADE
                )
            """)
            
            # Добавляем новые колонки если они не существуют
            try:
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS custom_name TEXT")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS watering_count INTEGER DEFAULT 0")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS notes TEXT")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS watering_interval INTEGER DEFAULT 5")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS reminder_enabled BOOLEAN DEFAULT TRUE")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS plant_type TEXT DEFAULT 'regular'")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS growing_id INTEGER")
                await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS last_sent TIMESTAMP")
                await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS send_count INTEGER DEFAULT 0")
                await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS growing_plant_id INTEGER")
                await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS stage_number INTEGER")
            except Exception as e:
                print(f"Колонки уже существуют или ошибка: {e}")
            
            # Индексы для оптимизации
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plants_user_id ON plants (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_growing_plants_user_id ON growing_plants (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON reminders (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_next_date ON reminders (next_date, is_active)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_care_history_plant_id ON care_history (plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_growth_stages_growing_plant_id ON growth_stages (growing_plant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_growth_diary_growing_plant_id ON growth_diary (growing_plant_id)")
            
    def extract_plant_name_from_analysis(self, analysis_text: str) -> str:
        """Извлекает название растения из текста анализа"""
        if not analysis_text:
            return None
        
        lines = analysis_text.split('\n')
        for line in lines:
            if line.startswith("РАСТЕНИЕ:"):
                plant_name = line.replace("РАСТЕНИЕ:", "").strip()
                
                # Убираем латинское название в скобках для отображения
                if "(" in plant_name:
                    plant_name = plant_name.split("(")[0].strip()
                
                # Убираем информацию о достоверности и проценты
                plant_name = plant_name.split("достоверность:")[0].strip()
                plant_name = plant_name.split("%")[0].strip()
                plant_name = plant_name.replace("🌿", "").strip()
                
                # Проверяем длину и разумность названия
                if 3 <= len(plant_name) <= 80 and not plant_name.lower().startswith(("неизвестн", "неопознан", "комнатное растение")):
                    return plant_name
        
        return None
            
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
            
            # Создаем настройки пользователя по умолчанию
            await conn.execute("""
                INSERT INTO user_settings (user_id)
                VALUES ($1)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id)
    
    # === МЕТОДЫ ДЛЯ ВЫРАЩИВАНИЯ РАСТЕНИЙ ===
    
    async def create_growing_plant(self, user_id: int, plant_name: str, growth_method: str, 
                                 growing_plan: str, photo_file_id: str = None) -> int:
        """Создать новое выращиваемое растение"""
        async with self.pool.acquire() as conn:
            # Создаем запись о выращивании
            growing_id = await conn.fetchval("""
                INSERT INTO growing_plants 
                (user_id, plant_name, growth_method, growing_plan, photo_file_id, estimated_completion)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """, user_id, plant_name, growth_method, growing_plan, photo_file_id, 
                datetime.now().date() + timedelta(days=90))  # 3 месяца по умолчанию
            
            # Создаем этапы выращивания из плана
            await self.create_growth_stages(growing_id, growing_plan)
            
            # Добавляем запись в дневник роста
            await conn.execute("""
                INSERT INTO growth_diary (growing_plant_id, user_id, entry_type, description)
                VALUES ($1, $2, 'started', $3)
            """, growing_id, user_id, f"Начато выращивание {plant_name}")
            
            return growing_id
    
    async def create_growth_stages(self, growing_plant_id: int, growing_plan: str):
        """Создать этапы выращивания из плана"""
        # Парсим план и создаем этапы
        stages = self.parse_growing_plan_to_stages(growing_plan)
        
        async with self.pool.acquire() as conn:
            for i, stage in enumerate(stages):
                await conn.execute("""
                    INSERT INTO growth_stages 
                    (growing_plant_id, stage_number, stage_name, stage_description, estimated_duration_days)
                    VALUES ($1, $2, $3, $4, $5)
                """, growing_plant_id, i + 1, stage['name'], stage['description'], stage['duration'])
    
    def parse_growing_plan_to_stages(self, growing_plan: str) -> List[Dict]:
        """Парсит план выращивания в этапы"""
        stages = []
        lines = growing_plan.split('\n')
        current_stage = None
        
        for line in lines:
            line = line.strip()
            if line.startswith('🌱 ЭТАП') or line.startswith('🌿 ЭТАП') or line.startswith('🌸 ЭТАП'):
                if current_stage:
                    stages.append(current_stage)
                
                # Извлекаем номер этапа и название
                stage_info = line.split(':', 1)
                if len(stage_info) > 1:
                    stage_name = stage_info[1].strip()
                    # Извлекаем продолжительность из скобок
                    duration = 7  # по умолчанию
                    if '(' in stage_name and ')' in stage_name:
                        duration_text = stage_name[stage_name.find('(')+1:stage_name.find(')')]
                        # Ищем числа в тексте продолжительности
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
        
        # Если не удалось распарсить, создаем стандартные этапы
        if not stages:
            stages = [
                {'name': 'Подготовка и посадка', 'description': 'Подготовка семян/черенка и посадка', 'duration': 7},
                {'name': 'Прорастание', 'description': 'Появление первых всходов', 'duration': 14},
                {'name': 'Рост и развитие', 'description': 'Активный рост растения', 'duration': 30},
                {'name': 'Взрослое растение', 'description': 'Растение готово к пересадке', 'duration': 30}
            ]
        
        return stages
    
    async def get_growing_plant_by_id(self, growing_id: int, user_id: int = None) -> Optional[Dict]:
        """Получить выращиваемое растение по ID"""
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
    
    async def get_user_growing_plants(self, user_id: int) -> List[Dict]:
        """Получить все выращиваемые растения пользователя"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT gp.*, gs.stage_name as current_stage_name
                FROM growing_plants gp
                LEFT JOIN growth_stages gs ON gp.id = gs.growing_plant_id AND gs.stage_number = gp.current_stage + 1
                WHERE gp.user_id = $1 AND gp.status = 'active'
                ORDER BY gp.started_date DESC
            """, user_id)
            
            return [dict(row) for row in rows]
    
    async def advance_growth_stage(self, growing_id: int, photo_file_id: str = None, notes: str = None):
        """Перевести растение на следующий этап"""
        async with self.pool.acquire() as conn:
            # Получаем текущую информацию
            growing_plant = await conn.fetchrow("""
                SELECT current_stage, total_stages FROM growing_plants WHERE id = $1
            """, growing_id)
            
            if not growing_plant:
                return False
            
            current_stage = growing_plant['current_stage']
            total_stages = growing_plant['total_stages']
            
            # Отмечаем текущий этап как завершенный
            if current_stage > 0:
                await conn.execute("""
                    UPDATE growth_stages 
                    SET completed_date = CURRENT_TIMESTAMP, photo_file_id = $1, notes = $2
                    WHERE growing_plant_id = $3 AND stage_number = $4
                """, photo_file_id, notes, growing_id, current_stage)
            
            # Переводим на следующий этап
            new_stage = current_stage + 1
            if new_stage <= total_stages:
                await conn.execute("""
                    UPDATE growing_plants 
                    SET current_stage = $1
                    WHERE id = $2
                """, new_stage, growing_id)
                
                # Добавляем запись в дневник
                await conn.execute("""
                    INSERT INTO growth_diary (growing_plant_id, user_id, entry_type, description, photo_file_id, stage_number)
                    SELECT $1, user_id, 'stage_completed', $2, $3, $4
                    FROM growing_plants WHERE id = $1
                """, growing_id, f"Завершен этап {current_stage}", photo_file_id, current_stage)
                
                return True
            else:
                # Растение выращено полностью
                await self.complete_growing_plant(growing_id)
                return "completed"
    
    async def complete_growing_plant(self, growing_id: int):
        """Завершить выращивание растения"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE growing_plants 
                SET status = 'completed'
                WHERE id = $1
            """, growing_id)
            
            # Добавляем финальную запись в дневник
            await conn.execute("""
                INSERT INTO growth_diary (growing_plant_id, user_id, entry_type, description)
                SELECT $1, user_id, 'completed', 'Выращивание успешно завершено!'
                FROM growing_plants WHERE id = $1
            """, growing_id)
    
    async def add_diary_entry(self, growing_id: int, user_id: int, entry_type: str, 
                            description: str, photo_file_id: str = None):
        """Добавить запись в дневник роста"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO growth_diary 
                (growing_plant_id, user_id, entry_type, description, photo_file_id, stage_number)
                SELECT $1, $2, $3, $4, $5, current_stage
                FROM growing_plants WHERE id = $1
            """, growing_id, user_id, entry_type, description, photo_file_id)
    
    async def get_growth_diary(self, growing_id: int, limit: int = 20) -> List[Dict]:
        """Получить дневник роста"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM growth_diary 
                WHERE growing_plant_id = $1
                ORDER BY entry_date DESC
                LIMIT $2
            """, growing_id, limit)
            
            return [dict(row) for row in rows]
    
    async def create_growing_reminder(self, growing_id: int, user_id: int, reminder_type: str, 
                                    next_date: datetime, stage_number: int = None):
        """Создать напоминание для выращивания"""
        async with self.pool.acquire() as conn:
            # Деактивируем старые напоминания этого типа
            await conn.execute("""
                UPDATE reminders 
                SET is_active = FALSE 
                WHERE growing_plant_id = $1 AND reminder_type = $2 AND is_active = TRUE
            """, growing_id, reminder_type)
            
            # Создаем новое напоминание
            await conn.execute("""
                INSERT INTO reminders 
                (user_id, growing_plant_id, reminder_type, next_date, stage_number)
                VALUES ($1, $2, $3, $4, $5)
            """, user_id, growing_id, reminder_type, next_date, stage_number)
    
    # === ОБЫЧНЫЕ МЕТОДЫ РАСТЕНИЙ (без изменений) ===
    
    async def save_plant(self, user_id: int, analysis: str, photo_file_id: str, plant_name: str = None) -> int:
        """Сохранить растение с автоматическим извлечением названия"""
        async with self.pool.acquire() as conn:
            # Пытаемся извлечь название из анализа если не передано
            if not plant_name:
                plant_name = self.extract_plant_name_from_analysis(analysis)
            
            plant_id = await conn.fetchval("""
                INSERT INTO plants (user_id, analysis, photo_file_id, plant_name)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """, user_id, analysis, photo_file_id, plant_name)
            
            # Добавляем запись в историю
            try:
                await conn.execute("""
                    INSERT INTO care_history (plant_id, action_type, notes)
                    VALUES ($1, 'added', 'Растение добавлено в коллекцию')
                """, plant_id)
            except Exception as e:
                print(f"Ошибка добавления в историю: {e}")
            
            return plant_id
    
    async def update_plant_name(self, plant_id: int, user_id: int, new_name: str):
        """Обновить пользовательское название растения"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE plants 
                SET custom_name = $1 
                WHERE id = $2 AND user_id = $3
            """, new_name, plant_id, user_id)
            
            # Добавляем запись в историю
            try:
                await conn.execute("""
                    INSERT INTO care_history (plant_id, action_type, notes)
                    VALUES ($1, 'renamed', $2)
                """, plant_id, f'Переименовано в "{new_name}"')
            except Exception as e:
                print(f"Ошибка добавления в историю: {e}")
    
    async def update_plant_watering_interval(self, plant_id: int, interval_days: int):
        """Обновить интервал полива растения"""
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
                       notes, plant_type, growing_id
                FROM plants 
                WHERE id = $1
            """
            params = [plant_id]
            
            if user_id:
                query += " AND user_id = $2"
                params.append(user_id)
            
            row = await conn.fetchrow(query, *params)
            
            if row:
                # Определяем отображаемое название
                display_name = row['custom_name'] or row['plant_name']
                if not display_name:
                    extracted_name = self.extract_plant_name_from_analysis(row['analysis'])
                    display_name = extracted_name or f"Растение #{row['id']}"
                
                result = dict(row)
                result['display_name'] = display_name
                return result
            return None
    
    async def get_user_plants(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Получить растения пользователя включая выращиваемые"""
        async with self.pool.acquire() as conn:
            # Обычные растения
            regular_rows = await conn.fetch("""
                SELECT id, analysis, photo_file_id, plant_name, custom_name, 
                       saved_date, last_watered, 
                       COALESCE(watering_count, 0) as watering_count,
                       COALESCE(watering_interval, 5) as watering_interval,
                       COALESCE(reminder_enabled, TRUE) as reminder_enabled,
                       notes, plant_type, growing_id
                FROM plants 
                WHERE user_id = $1 AND plant_type = 'regular'
                ORDER BY saved_date DESC
                LIMIT $2
            """, user_id, limit)
            
            # Выращиваемые растения
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
            
            # Добавляем обычные растения
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
            
            # Добавляем выращиваемые растения
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
            
            # Сортируем по дате добавления
            plants.sort(key=lambda x: x['saved_date'], reverse=True)
            
            return plants[:limit]
    
    async def update_watering(self, user_id: int, plant_id: int = None):
        """Отметить полив"""
        async with self.pool.acquire() as conn:
            if plant_id:
                # Полив конкретного растения
                await conn.execute("""
                    UPDATE plants 
                    SET last_watered = CURRENT_TIMESTAMP,
                        watering_count = COALESCE(watering_count, 0) + 1
                    WHERE user_id = $1 AND id = $2
                """, user_id, plant_id)
                
                # Добавляем запись в историю
                try:
                    await conn.execute("""
                        INSERT INTO care_history (plant_id, action_type, notes)
                        VALUES ($1, 'watered', 'Растение полито')
                    """, plant_id)
                except Exception as e:
                    print(f"Ошибка добавления в историю: {e}")
            else:
                # Полив всех растений пользователя
                plant_ids = await conn.fetch("""
                    SELECT id FROM plants WHERE user_id = $1
                """, user_id)
                
                await conn.execute("""
                    UPDATE plants 
                    SET last_watered = CURRENT_TIMESTAMP,
                        watering_count = COALESCE(watering_count, 0) + 1
                    WHERE user_id = $1
                """, user_id)
                
                # Добавляем записи в историю для всех растений
                for plant_row in plant_ids:
                    try:
                        await conn.execute("""
                            INSERT INTO care_history (plant_id, action_type, notes)
                            VALUES ($1, 'watered', 'Растение полито (массовый полив)')
                        """, plant_row['id'])
                    except Exception as e:
                        print(f"Ошибка добавления в историю: {e}")
    
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
            # Удаляем старые активные напоминания для этого растения
            await conn.execute("""
                UPDATE reminders 
                SET is_active = FALSE 
                WHERE user_id = $1 AND plant_id = $2 AND reminder_type = $3 AND is_active = TRUE
            """, user_id, plant_id, reminder_type)
            
            # Создаем новое напоминание
            await conn.execute("""
                INSERT INTO reminders (user_id, plant_id, reminder_type, next_date)
                VALUES ($1, $2, $3, $4)
            """, user_id, plant_id, reminder_type, next_date)
    
    async def get_user_reminder_settings(self, user_id: int) -> Optional[Dict]:
        """Получить настройки напоминаний пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT reminder_time, timezone, reminder_enabled
                FROM user_settings
                WHERE user_id = $1
            """, user_id)
            
            if row:
                return {
                    'reminder_time': row['reminder_time'],
                    'timezone': row['timezone'],
                    'reminder_enabled': row['reminder_enabled']
                }
            return None
    
    async def update_user_reminder_settings(self, user_id: int, reminder_time: str = None, 
                                          timezone: str = None, reminder_enabled: bool = None):
        """Обновить настройки напоминаний пользователя"""
        async with self.pool.acquire() as conn:
            updates = []
            params = []
            param_count = 1
            
            if reminder_time is not None:
                updates.append(f"reminder_time = ${param_count}")
                params.append(reminder_time)
                param_count += 1
            
            if timezone is not None:
                updates.append(f"timezone = ${param_count}")
                params.append(timezone)
                param_count += 1
            
            if reminder_enabled is not None:
                updates.append(f"reminder_enabled = ${param_count}")
                params.append(reminder_enabled)
                param_count += 1
            
            if updates:
                params.append(user_id)
                query = f"""
                    UPDATE user_settings 
                    SET {', '.join(updates)}
                    WHERE user_id = ${param_count}
                """
                await conn.execute(query, *params)
    
    async def get_plant_history(self, plant_id: int, limit: int = 20) -> List[Dict]:
        """Получить историю ухода за растением"""
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch("""
                    SELECT action_type, action_date, notes
                    FROM care_history
                    WHERE plant_id = $1
                    ORDER BY action_date DESC
                    LIMIT $2
                """, plant_id, limit)
                
                history = []
                for row in rows:
                    history.append({
                        'action_type': row['action_type'],
                        'action_date': row['action_date'],
                        'notes': row['notes']
                    })
                
                return history
            except Exception as e:
                print(f"Ошибка получения истории: {e}")
                return []
    
    async def get_user_stats(self, user_id: int) -> Dict:
        """Статистика пользователя включая выращивание"""
        async with self.pool.acquire() as conn:
            # Статистика обычных растений
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
            
            # Статистика выращивания
            growing_stats = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_growing,
                    COUNT(CASE WHEN status = 'active' THEN 1 END) as active_growing,
                    COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed_growing
                FROM growing_plants 
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
                'completed_growing': growing_stats['completed_growing'] or 0
            }
    
    async def close(self):
        """Закрыть соединения"""
        if self.pool:
            await self.pool.close()

# Глобальный экземпляр базы данных
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
