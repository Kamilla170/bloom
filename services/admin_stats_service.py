"""
Admin Statistics Service
Сбор и отправка ежедневной статистики администраторам
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from aiogram import Bot

from database import get_db
from config import ADMIN_USER_IDS
from utils.time_utils import get_moscow_now

logger = logging.getLogger(__name__)


async def collect_daily_stats(target_date: datetime = None) -> Dict:
    """
    Собрать статистику за указанный день
    
    Args:
        target_date: дата для сбора статистики (по умолчанию вчера)
        
    Returns:
        Dict со всей статистикой
    """
    try:
        db = await get_db()
        
        if target_date is None:
            # По умолчанию вчера
            target_date = get_moscow_now() - timedelta(days=1)
        
        target_date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        target_date_end = target_date_start + timedelta(days=1)
        
        async with db.pool.acquire() as conn:
            # 1. ПОЛЬЗОВАТЕЛИ
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            
            new_users = await conn.fetchval("""
                SELECT COUNT(*) FROM users 
                WHERE created_at >= $1 AND created_at < $2
            """, target_date_start, target_date_end)
            
            # ИСПРАВЛЕНИЕ: Считаем активных по реальным действиям
            active_users = await conn.fetchval("""
                SELECT COUNT(DISTINCT user_id) FROM (
                    SELECT user_id FROM plants 
                    WHERE saved_date >= $1 AND saved_date < $2
                    UNION
                    SELECT user_id FROM plant_qa_history 
                    WHERE question_date >= $1 AND question_date < $2
                    UNION
                    SELECT user_id FROM plant_analyses_full 
                    WHERE analysis_date >= $1 AND analysis_date < $2
                    UNION
                    SELECT user_id FROM care_history 
                    WHERE action_date >= $1 AND action_date < $2
                    UNION
                    SELECT user_id FROM growing_plants 
                    WHERE started_date >= $1 AND started_date < $2
                    UNION
                    SELECT user_id FROM feedback 
                    WHERE created_at >= $1 AND created_at < $2
                ) active_users_union
            """, target_date_start, target_date_end)
            
            # 2. РАСТЕНИЯ
            users_added_plants = await conn.fetchval("""
                SELECT COUNT(DISTINCT user_id) FROM plants 
                WHERE saved_date >= $1 AND saved_date < $2
            """, target_date_start, target_date_end)
            
            total_plants_added = await conn.fetchval("""
                SELECT COUNT(*) FROM plants 
                WHERE saved_date >= $1 AND saved_date < $2
            """, target_date_start, target_date_end)
            
            # 3. ПОЛИВЫ
            users_watered = await conn.fetchval("""
                SELECT COUNT(DISTINCT user_id) FROM plants 
                WHERE last_watered >= $1 AND last_watered < $2
            """, target_date_start, target_date_end)
            
            total_waterings = await conn.fetchval("""
                SELECT COUNT(*) FROM care_history 
                WHERE action_type = 'watered' 
                AND action_date >= $1 AND action_date < $2
            """, target_date_start, target_date_end)
            
            # 4. АКТИВНОСТЬ
            analyses_count = await conn.fetchval("""
                SELECT COUNT(*) FROM plant_analyses_full 
                WHERE analysis_date >= $1 AND analysis_date < $2
            """, target_date_start, target_date_end)
            
            questions_count = await conn.fetchval("""
                SELECT COUNT(*) FROM plant_qa_history 
                WHERE question_date >= $1 AND question_date < $2
            """, target_date_start, target_date_end)
            
            photo_updates = await conn.fetchval("""
                SELECT COUNT(*) FROM plant_state_history 
                WHERE change_date >= $1 AND change_date < $2
                AND photo_file_id IS NOT NULL
            """, target_date_start, target_date_end)
            
            growing_started = await conn.fetchval("""
                SELECT COUNT(*) FROM growing_plants 
                WHERE started_date >= $1 AND started_date < $2
            """, target_date_start, target_date_end)
            
            feedback_count = await conn.fetchval("""
                SELECT COUNT(*) FROM feedback 
                WHERE created_at >= $1 AND created_at < $2
            """, target_date_start, target_date_end)
            
            # 5. ТОП-3 АКТИВНЫХ (по количеству действий)
            top_active = await conn.fetch("""
                WITH user_actions AS (
                    SELECT user_id, COUNT(*) as action_count
                    FROM (
                        SELECT user_id FROM plants WHERE saved_date >= $1 AND saved_date < $2
                        UNION ALL
                        SELECT user_id FROM plant_qa_history WHERE question_date >= $1 AND question_date < $2
                        UNION ALL
                        SELECT user_id FROM plant_analyses_full WHERE analysis_date >= $1 AND analysis_date < $2
                        UNION ALL
                        SELECT user_id FROM care_history WHERE action_date >= $1 AND action_date < $2
                        UNION ALL
                        SELECT user_id FROM growing_plants WHERE started_date >= $1 AND started_date < $2
                        UNION ALL
                        SELECT user_id FROM feedback WHERE created_at >= $1 AND created_at < $2
                    ) actions
                    GROUP BY user_id
                )
                SELECT u.user_id, u.username, u.first_name, ua.action_count
                FROM user_actions ua
                JOIN users u ON ua.user_id = u.user_id
                ORDER BY ua.action_count DESC
                LIMIT 3
            """, target_date_start, target_date_end)
            
            # 6. RETENTION (7-дневный)
            week_ago = target_date_start - timedelta(days=7)
            users_week_ago = await conn.fetchval("""
                SELECT COUNT(*) FROM users WHERE created_at < $1
            """, week_ago)
            
            # ИСПРАВЛЕНИЕ: Считаем retention по реальной активности
            active_from_week_ago = await conn.fetchval("""
                SELECT COUNT(DISTINCT user_id) FROM (
                    SELECT user_id FROM plants 
                    WHERE saved_date >= $1 AND saved_date < $2
                    AND user_id IN (SELECT user_id FROM users WHERE created_at < $3)
                    UNION
                    SELECT user_id FROM plant_qa_history 
                    WHERE question_date >= $1 AND question_date < $2
                    AND user_id IN (SELECT user_id FROM users WHERE created_at < $3)
                    UNION
                    SELECT user_id FROM plant_analyses_full 
                    WHERE analysis_date >= $1 AND analysis_date < $2
                    AND user_id IN (SELECT user_id FROM users WHERE created_at < $3)
                    UNION
                    SELECT user_id FROM care_history 
                    WHERE action_date >= $1 AND action_date < $2
                    AND user_id IN (SELECT user_id FROM users WHERE created_at < $3)
                ) retention_users
            """, target_date_start, target_date_end, week_ago)
            
            retention_7day = 0
            if users_week_ago > 0:
                retention_7day = (active_from_week_ago / users_week_ago) * 100
        
        stats = {
            'date': target_date_start.date(),
            'users': {
                'total': total_users or 0,
                'new': new_users or 0,
                'active': active_users or 0,
                'inactive': (total_users or 0) - (active_users or 0),
                'retention_7day': round(retention_7day, 1)
            },
            'plants': {
                'users_added': users_added_plants or 0,
                'total_added': total_plants_added or 0,
                'users_watered': users_watered or 0,
                'total_waterings': total_waterings or 0,
                'growing_started': growing_started or 0
            },
            'activity': {
                'analyses': analyses_count or 0,
                'questions': questions_count or 0,
                'photo_updates': photo_updates or 0,
                'feedback': feedback_count or 0
            },
            'top_active': [
                {
                    'user_id': row['user_id'],
                    'username': row['username'] or row['first_name'] or f"user_{row['user_id']}",
                    'actions': row['action_count']
                }
                for row in top_active
            ]
        }
        
        return stats
        
    except Exception as e:
        logger.error(f"❌ Ошибка сбора статистики: {e}", exc_info=True)
        return {}


async def save_daily_stats(stats: Dict) -> bool:
    """Сохранить статистику в базу данных"""
    try:
        db = await get_db()
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO daily_stats (
                    stat_date, total_users, new_users, active_users,
                    users_watered, users_added_plants, total_waterings,
                    total_plants_added, analyses_count, questions_count,
                    growing_started, feedback_count
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (stat_date) DO UPDATE SET
                    total_users = EXCLUDED.total_users,
                    new_users = EXCLUDED.new_users,
                    active_users = EXCLUDED.active_users,
                    users_watered = EXCLUDED.users_watered,
                    users_added_plants = EXCLUDED.users_added_plants,
                    total_waterings = EXCLUDED.total_waterings,
                    total_plants_added = EXCLUDED.total_plants_added,
                    analyses_count = EXCLUDED.analyses_count,
                    questions_count = EXCLUDED.questions_count,
                    growing_started = EXCLUDED.growing_started,
                    feedback_count = EXCLUDED.feedback_count
            """, 
                stats['date'],
                stats['users']['total'],
                stats['users']['new'],
                stats['users']['active'],
                stats['plants']['users_watered'],
                stats['plants']['users_added'],
                stats['plants']['total_waterings'],
                stats['plants']['total_added'],
                stats['activity']['analyses'],
                stats['activity']['questions'],
                stats['plants']['growing_started'],
                stats['activity']['feedback']
            )
        
        logger.info(f"✅ Статистика за {stats['date']} сохранена в БД")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения статистики: {e}")
        return False


async def get_comparison_stats(target_date: datetime) -> Dict:
    """
    Получить статистику для сравнения
    
    Returns:
        Dict с данными для сравнения:
        - yesterday (день назад)
        - week_ago (неделя назад, тот же день)
        - prev_week (предыдущая неделя, сумма)
        - prev_month (предыдущий месяц, сумма)
    """
    try:
        db = await get_db()
        target_date_obj = target_date.date()
        
        # День назад
        day_ago = target_date_obj - timedelta(days=1)
        
        # Неделя назад (тот же день недели)
        week_ago = target_date_obj - timedelta(days=7)
        
        # Предыдущая неделя (7 дней до target_date)
        prev_week_start = target_date_obj - timedelta(days=7)
        prev_week_end = target_date_obj
        
        # Предыдущий месяц (30 дней до target_date)
        prev_month_start = target_date_obj - timedelta(days=30)
        prev_month_end = target_date_obj
        
        async with db.pool.acquire() as conn:
            # День назад
            yesterday = await conn.fetchrow("""
                SELECT * FROM daily_stats WHERE stat_date = $1
            """, day_ago)
            
            # Неделя назад
            week_ago_stats = await conn.fetchrow("""
                SELECT * FROM daily_stats WHERE stat_date = $1
            """, week_ago)
            
            # Предыдущая неделя (сумма)
            prev_week = await conn.fetchrow("""
                SELECT 
                    SUM(new_users) as new_users,
                    SUM(active_users) as active_users,
                    SUM(users_added_plants) as users_added_plants,
                    SUM(users_watered) as users_watered,
                    SUM(total_waterings) as total_waterings,
                    SUM(total_plants_added) as total_plants_added,
                    SUM(analyses_count) as analyses_count,
                    SUM(questions_count) as questions_count
                FROM daily_stats 
                WHERE stat_date >= $1 AND stat_date < $2
            """, prev_week_start, prev_week_end)
            
            # Предыдущий месяц (сумма)
            prev_month = await conn.fetchrow("""
                SELECT 
                    SUM(new_users) as new_users,
                    SUM(active_users) as active_users,
                    SUM(users_added_plants) as users_added_plants,
                    SUM(users_watered) as users_watered,
                    SUM(total_waterings) as total_waterings,
                    SUM(total_plants_added) as total_plants_added,
                    SUM(analyses_count) as analyses_count,
                    SUM(questions_count) as questions_count
                FROM daily_stats 
                WHERE stat_date >= $1 AND stat_date < $2
            """, prev_month_start, prev_month_end)
        
        return {
            'yesterday': dict(yesterday) if yesterday else None,
            'week_ago': dict(week_ago_stats) if week_ago_stats else None,
            'prev_week': dict(prev_week) if prev_week else None,
            'prev_month': dict(prev_month) if prev_month else None
        }
        
    except Exception as e:
        logger.error(f"❌ Ошибка получения данных для сравнения: {e}")
        return {}


def calculate_trend(current: int, previous: Optional[int]) -> str:
    """
    Рассчитать тренд и вернуть форматированную строку
    
    Returns:
        Строка вида "+15% ⬆️" или "-5% ⬇️" или "—"
    """
    if previous is None or previous == 0:
        return "—"
    
    if current == previous:
        return "0% ➡️"
    
    diff_percent = ((current - previous) / previous) * 100
    
    if diff_percent > 0:
        return f"+{diff_percent:.1f}% ⬆️"
    else:
        return f"{diff_percent:.1f}% ⬇️"


def format_admin_report(stats: Dict, comparisons: Dict) -> str:
    """
    Форматировать красивый отчет для администраторов
    
    Args:
        stats: текущая статистика
        comparisons: данные для сравнения
        
    Returns:
        Форматированный текст отчета
    """
    date_str = stats['date'].strftime('%d.%m.%Y')
    
    # Проценты
    total = stats['users']['total']
    active_percent = (stats['users']['active'] / total * 100) if total > 0 else 0
    inactive_percent = 100 - active_percent
    added_plants_percent = (stats['plants']['users_added'] / total * 100) if total > 0 else 0
    watered_percent = (stats['plants']['users_watered'] / total * 100) if total > 0 else 0
    
    # Тренды
    yesterday = comparisons.get('yesterday')
    week_ago = comparisons.get('week_ago')
    
    new_users_trend_day = calculate_trend(stats['users']['new'], yesterday['new_users'] if yesterday else None)
    active_trend_day = calculate_trend(stats['users']['active'], yesterday['active_users'] if yesterday else None)
    watered_trend_day = calculate_trend(stats['plants']['users_watered'], yesterday['users_watered'] if yesterday else None)
    
    new_users_trend_week = calculate_trend(stats['users']['new'], week_ago['new_users'] if week_ago else None)
    active_trend_week = calculate_trend(stats['users']['active'], week_ago['active_users'] if week_ago else None)
    
    # Retention статус
    retention = stats['users']['retention_7day']
    if retention >= 40:
        retention_status = "отлично ✅"
    elif retention >= 25:
        retention_status = "хорошо 👍"
    elif retention >= 15:
        retention_status = "норма ➡️"
    else:
        retention_status = "низкий ⚠️"
    
    report = f"""
📊 <b>ЕЖЕДНЕВНЫЙ ОТЧЕТ - {date_str}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

👥 <b>ПОЛЬЗОВАТЕЛИ</b>
├─ Всего в базе: <b>{stats['users']['total']:,}</b>
├─ Новых за день: <b>{stats['users']['new']}</b> {new_users_trend_day}
│  └─ vs неделю назад: {new_users_trend_week}
├─ Активных вчера: <b>{stats['users']['active']}</b> ({active_percent:.1f}%) {active_trend_day}
│  └─ vs неделю назад: {active_trend_week}
│  └─ Неактивных: {stats['users']['inactive']} ({inactive_percent:.1f}%)
└─ 7-day retention: <b>{retention:.1f}%</b> ({retention_status})

🌱 <b>РАСТЕНИЯ</b>
├─ Добавили растение: <b>{stats['plants']['users_added']}</b> чел ({added_plants_percent:.1f}%)
│  └─ Всего растений: {stats['plants']['total_added']} шт {watered_trend_day}
├─ Полили: <b>{stats['plants']['users_watered']}</b> чел ({watered_percent:.1f}%)
│  └─ Всего поливов: {stats['plants']['total_waterings']} действий
└─ Выращивание с нуля: <b>{stats['plants']['growing_started']}</b> чел

🔍 <b>АКТИВНОСТЬ</b>
├─ Анализов сделано: <b>{stats['activity']['analyses']}</b>
├─ Вопросов задано: <b>{stats['activity']['questions']}</b>
├─ Обновлено фото: <b>{stats['activity']['photo_updates']}</b>
└─ Обратной связи: <b>{stats['activity']['feedback']}</b>
"""
    
    # ТОП-3 активных
    if stats['top_active']:
        report += "\n🏆 <b>ТОП-3 АКТИВНЫХ</b>\n"
        for i, user in enumerate(stats['top_active'], 1):
            username = user['username']
            if not username.startswith('@'):
                username = f"@{username}"
            report += f"{i}. {username} - {user['actions']} действий\n"
    
    report += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    return report


async def send_daily_report_to_admins(bot: Bot):
    """
    Собрать статистику за вчера и отправить всем администраторам
    """
    try:
        logger.info("📊 Начинаем сбор ежедневной статистики...")
        
        # Собираем статистику за вчера
        yesterday = get_moscow_now() - timedelta(days=1)
        stats = await collect_daily_stats(yesterday)
        
        if not stats:
            logger.error("❌ Не удалось собрать статистику")
            return
        
        # Сохраняем в БД для трендов
        await save_daily_stats(stats)
        
        # Получаем данные для сравнения
        comparisons = await get_comparison_stats(yesterday)
        
        # Форматируем отчет
        report = format_admin_report(stats, comparisons)
        
        # Отправляем всем администраторам
        for admin_id in ADMIN_USER_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=report,
                    parse_mode="HTML"
                )
                logger.info(f"✅ Отчет отправлен администратору {admin_id}")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки администратору {admin_id}: {e}")
        
        logger.info("✅ Ежедневная статистика успешно отправлена")
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка отправки статистики: {e}", exc_info=True)
