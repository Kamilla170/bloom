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
            
            # Таблица растений
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plants (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    analysis TEXT NOT NULL,
                    photo_file_id TEXT NOT NULL,
                    plant_name TEXT,
                    saved_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_watered TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
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
            
            # Индексы для оптимизации
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_plants_user_id ON plants (user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON reminders (user_id)")
            
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
        """Сохранить растение"""
        async with self.pool.acquire() as conn:
            plant_id = await conn.fetchval("""
                INSERT INTO plants (user_id, analysis, photo_file_id, plant_name)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """, user_id, analysis, photo_file_id, plant_name)
            return plant_id
    
    async def get_user_plants(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Получить растения пользователя"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, analysis, photo_file_id, plant_name, saved_date, last_watered
                FROM plants 
                WHERE user_id = $1 
                ORDER BY saved_date DESC
                LIMIT $2
            """, user_id, limit)
            
            plants = []
            for row in rows:
                plants.append({
                    'id': row['id'],
                    'analysis': row['analysis'],
                    'photo_file_id': row['photo_file_id'],
                    'plant_name': row['plant_name'],
                    'saved_date': row['saved_date'],
                    'last_watered': row['last_watered']
                })
            
            return plants
    
    async def update_watering(self, user_id: int, plant_id: int = None):
        """Отметить полив"""
        async with self.pool.acquire() as conn:
            if plant_id:
                # Полив конкретного растения
                await conn.execute("""
                    UPDATE plants 
                    SET last_watered = CURRENT_TIMESTAMP 
                    WHERE user_id = $1 AND id = $2
                """, user_id, plant_id)
            else:
                # Полив всех растений пользователя
                await conn.execute("""
                    UPDATE plants 
                    SET last_watered = CURRENT_TIMESTAMP 
                    WHERE user_id = $1
                """, user_id)
    
    async def get_plant_by_id(self, plant_id: int) -> Optional[Dict]:
        """Получить растение по ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, user_id, analysis, photo_file_id, plant_name, saved_date, last_watered
                FROM plants 
                WHERE id = $1
            """, plant_id)
            
            if row:
                return {
                    'id': row['id'],
                    'user_id': row['user_id'],
                    'analysis': row['analysis'],
                    'photo_file_id': row['photo_file_id'],
                    'plant_name': row['plant_name'],
                    'saved_date': row['saved_date'],
                    'last_watered': row['last_watered']
                }
            return None
    
    async def delete_plant(self, user_id: int, plant_id: int):
        """Удалить растение"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM plants 
                WHERE user_id = $1 AND id = $2
            """, user_id, plant_id)
    
    async def get_user_stats(self, user_id: int) -> Dict:
        """Статистика пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_plants,
                    COUNT(CASE WHEN last_watered IS NOT NULL THEN 1 END) as watered_plants,
                    MIN(saved_date) as first_plant_date,
                    MAX(last_watered) as last_watered_date
                FROM plants 
                WHERE user_id = $1
            """, user_id)
            
            return {
                'total_plants': row['total_plants'] or 0,
                'watered_plants': row['watered_plants'] or 0,
                'first_plant_date': row['first_plant_date'],
                'last_watered_date': row['last_watered_date']
            }
    
    async def save_last_analysis(self, user_id: int, analysis: str, photo_file_id: str):
        """Сохранить данные последнего анализа (для временного хранения)"""
        # Используем простое хранилище в памяти для последнего анализа
        # В продакшене можно создать отдельную таблицу
        pass
    
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
