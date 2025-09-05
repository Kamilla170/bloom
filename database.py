import os
import asyncpg
import json
from datetime import datetime
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
        """Создание таблиц"""
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
                    notes TEXT,
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
            
            # Таблица напоминаний
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    plant_id INTEGER,
                    reminder_type TEXT NOT NULL,
                    next_date TIMESTAMP NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
                    FOREIGN KEY (plant_id) REFERENCES plants (id) ON DELETE CASCADE
                )
            """)
            
            # Добавляем новые колонки если они не существуют (для обновления старых БД)
            try:
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS custom_name TEXT")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS watering_count INTEGER DEFAULT 0")
                await conn.execute("ALTER TABLE plants ADD COLUMN IF NOT EXISTS notes TEXT")
            except Exception as e:
                print(f"Колонки уже существуют или ошибка: {e}")
            
            # Индексы для оптимизации
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plants_user_id ON plants (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON reminders (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_care_history_plant_id ON care_history (plant_id)")
            
    def extract_plant_name_from_analysis(self, analysis_text: str) -> str:
        """Извлекает название растения из текста анализа"""
        if not analysis_text:
            return None
        
        lines = analysis_text.split('\n')
        for line in lines:
            if line.startswith("РАСТЕНИЕ:"):
                plant_name = line.replace("РАСТЕНИЕ:", "").strip()
                # Убираем лишнюю информацию в скобках и проценты
                if "(" in plant_name:
                    plant_name = plant_name.split("(")[0].strip()
                # Убираем информацию о достоверности
                plant_name = plant_name.split("достоверность:")[0].strip()
                plant_name = plant_name.split("%")[0].strip()
                
                # Проверяем длину и разумность названия
                if 3 <= len(plant_name) <= 50 and not plant_name.lower().startswith(("неизвестн", "комнатн", "растение")):
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
    
    async def get_user_plants(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Получить растения пользователя с улучшенными названиями"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, analysis, photo_file_id, plant_name, custom_name, 
                       saved_date, last_watered, 
                       COALESCE(watering_count, 0) as watering_count, 
                       notes
                FROM plants 
                WHERE user_id = $1 
                ORDER BY saved_date DESC
                LIMIT $2
            """, user_id, limit)
            
            plants = []
            for row in rows:
                # Определяем отображаемое название
                display_name = None
                
                # Приоритет: custom_name -> plant_name -> извлечение из анализа -> fallback
                if row['custom_name']:
                    display_name = row['custom_name']
                elif row['plant_name']:
                    display_name = row['plant_name']
                else:
                    # Пытаемся извлечь из анализа
                    extracted_name = self.extract_plant_name_from_analysis(row['analysis'])
                    if extracted_name:
                        display_name = extracted_name
                        # Сохраняем извлеченное название для будущего использования
                        try:
                            await conn.execute("""
                                UPDATE plants SET plant_name = $1 WHERE id = $2
                            """, extracted_name, row['id'])
                        except Exception as e:
                            print(f"Ошибка обновления названия: {e}")
                
                # Fallback если ничего не найдено
                if not display_name:
                    display_name = f"Растение #{row['id']}"
                
                plants.append({
                    'id': row['id'],
                    'analysis': row['analysis'],
                    'photo_file_id': row['photo_file_id'],
                    'plant_name': row['plant_name'],
                    'custom_name': row['custom_name'],
                    'display_name': display_name,
                    'saved_date': row['saved_date'],
                    'last_watered': row['last_watered'],
                    'watering_count': row['watering_count'],
                    'notes': row['notes']
                })
            
            return plants
    
    async def get_plant_by_id(self, plant_id: int, user_id: int = None) -> Optional[Dict]:
        """Получить растение по ID"""
        async with self.pool.acquire() as conn:
            query = """
                SELECT id, user_id, analysis, photo_file_id, plant_name, custom_name,
                       saved_date, last_watered, 
                       COALESCE(watering_count, 0) as watering_count, 
                       notes
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
                
                return {
                    'id': row['id'],
                    'user_id': row['user_id'],
                    'analysis': row['analysis'],
                    'photo_file_id': row['photo_file_id'],
                    'plant_name': row['plant_name'],
                    'custom_name': row['custom_name'],
                    'display_name': display_name,
                    'saved_date': row['saved_date'],
                    'last_watered': row['last_watered'],
                    'watering_count': row['watering_count'],
                    'notes': row['notes']
                }
            return None
    
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
        """Статистика пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_plants,
                    COUNT(CASE WHEN last_watered IS NOT NULL THEN 1 END) as watered_plants,
                    COALESCE(SUM(watering_count), 0) as total_waterings,
                    MIN(saved_date) as first_plant_date,
                    MAX(last_watered) as last_watered_date
                FROM plants 
                WHERE user_id = $1
            """, user_id)
            
            return {
                'total_plants': row['total_plants'] or 0,
                'watered_plants': row['watered_plants'] or 0,
                'total_waterings': row['total_waterings'] or 0,
                'first_plant_date': row['first_plant_date'],
                'last_watered_date': row['last_watered_date']
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
