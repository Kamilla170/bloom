import asyncio
import logging
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage

# Импорты конфигурации
from config import (
    BOT_TOKEN, WEBHOOK_URL, PORT, MOSCOW_TZ, 
    validate_config, logger
)

# Импорты инициализации
from database import init_database, get_db

# Импорты сервисов
from services.reminder_service import (
    check_and_send_reminders, 
    check_monthly_photo_reminders
)

# Импорты handlers
from handlers import (
    commands, photo, callbacks, plants, 
    questions, feedback, onboarding, growing
)

# Импорт middleware
from middleware import ActivityTrackingMiddleware

# Настройка логирования уже в config
logger.info("🚀 Запуск Bloom AI Bot...")

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Планировщик
scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)


async def fix_inactive_reminders():
    """🔧 ОДНОРАЗОВЫЙ ФИКС: активировать неактивные напоминания"""
    try:
        logger.info("")
        logger.info("=" * 70)
        logger.info("🔧 ПРОВЕРКА НЕАКТИВНЫХ НАПОМИНАНИЙ")
        logger.info("=" * 70)
        
        db = await get_db()
        async with db.pool.acquire() as conn:
            # Считаем сколько неактивных
            inactive_count = await conn.fetchval("""
                SELECT COUNT(*) 
                FROM reminders r
                JOIN plants p ON r.plant_id = p.id
                WHERE r.reminder_type = 'watering'
                  AND r.is_active = FALSE
                  AND p.plant_type = 'regular'
                  AND p.reminder_enabled = TRUE
            """)
            
            if inactive_count > 0:
                logger.warning(f"⚠️ НАЙДЕНО {inactive_count} НЕАКТИВНЫХ НАПОМИНАНИЙ!")
                logger.info("🔧 Активирую напоминания...")
                
                # Активируем ВСЕ неактивные напоминания о поливе
                result = await conn.execute("""
                    UPDATE reminders r
                    SET is_active = TRUE
                    FROM plants p
                    WHERE r.plant_id = p.id
                      AND r.reminder_type = 'watering'
                      AND r.is_active = FALSE
                      AND p.plant_type = 'regular'
                      AND p.reminder_enabled = TRUE
                """)
                
                # Проверяем результат
                active_count = await conn.fetchval("""
                    SELECT COUNT(*) 
                    FROM reminders 
                    WHERE reminder_type = 'watering' 
                      AND is_active = TRUE
                """)
                
                logger.info(f"✅ АКТИВИРОВАНО {inactive_count} НАПОМИНАНИЙ!")
                logger.info(f"📊 Всего активных напоминаний сейчас: {active_count}")
                logger.info("")
                logger.info("💡 Теперь /test_reminders должен отправить все напоминания!")
            else:
                logger.info("✅ Все напоминания уже активны - фикс не требуется")
        
        logger.info("=" * 70)
        logger.info("")
                
    except Exception as e:
        logger.error(f"❌ Ошибка фикса напоминаний: {e}", exc_info=True)
        # НЕ прерываем запуск бота - это не критично


async def on_startup():
    """Инициализация при запуске"""
    try:
        logger.info("=" * 70)
        logger.info("🌱 BLOOM AI BOT - ИНИЦИАЛИЗАЦИЯ")
        logger.info("=" * 70)
        
        # Валидация конфигурации
        validate_config()
        
        # Инициализация базы данных
        await init_database()
        logger.info("✅ База данных инициализирована")
        
        # 🔧 ОДНОРАЗОВЫЙ ФИКС НАПОМИНАНИЙ
        await fix_inactive_reminders()
        
        # Удаление старого webhook
        logger.info("🔧 Удаление старого webhook...")
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            logger.warning(f"⚠️ Найден активный webhook: {webhook_info.url}")
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Webhook удален")
        else:
            logger.info("ℹ️ Webhook не был установлен")
        
        # Регистрация middleware
        register_middleware()
        
        # Регистрация handlers
        register_handlers()
        
        # Настройка планировщика
        setup_scheduler()
        
        # Установка webhook или polling
        if WEBHOOK_URL:
            await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
            logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}/webhook")
        else:
            logger.info("✅ Polling mode активирован")
        
        logger.info("=" * 70)
        logger.info("✅ ИНИЦИАЛИЗАЦИЯ ЗАВЕРШЕНА")
        logger.info("=" * 70)
            
    except Exception as e:
        logger.error(f"❌ Ошибка запуска: {e}", exc_info=True)
        raise


async def on_shutdown():
    """Завершение работы"""
    logger.info("🛑 Остановка бота...")
    
    if scheduler.running:
        scheduler.shutdown()
        logger.info("⏰ Планировщик остановлен")
    
    try:
        db = await get_db()
        await db.close()
        logger.info("✅ База данных закрыта")
    except:
        pass
    
    try:
        await bot.session.close()
        logger.info("✅ Сессия бота закрыта")
    except:
        pass


def register_middleware():
    """Регистрация middleware"""
    # Регистрируем middleware для отслеживания активности
    dp.message.middleware(ActivityTrackingMiddleware())
    dp.callback_query.middleware(ActivityTrackingMiddleware())
    
    logger.info("✅ Middleware зарегистрированы (Activity Tracking)")


def register_handlers():
    """Регистрация всех handlers"""
    # Регистрация routers в правильном порядке
    dp.include_router(commands.router)
    dp.include_router(photo.router)
    dp.include_router(plants.router)
    dp.include_router(questions.router)
    dp.include_router(feedback.router)
    dp.include_router(onboarding.router)
    dp.include_router(growing.router)
    dp.include_router(callbacks.router)  # Callbacks последними как fallback
    
    logger.info("✅ Handlers зарегистрированы")


def setup_scheduler():
    """Настройка планировщика задач"""
    logger.info("")
    logger.info("=" * 70)
    logger.info("⏰ НАСТРОЙКА ПЛАНИРОВЩИКА ЗАДАЧ")
    logger.info("=" * 70)
    
    from utils.time_utils import get_moscow_now
    moscow_now = get_moscow_now()
    logger.info(f"🕐 Текущее время (МСК): {moscow_now.strftime('%d.%m.%Y %H:%M:%S')}")
    logger.info(f"🌍 Часовой пояс: {MOSCOW_TZ}")
    
    # Ежедневные напоминания о поливе в 9:00 МСК
    scheduler.add_job(
        check_and_send_reminders,
        'cron',
        hour=9,
        minute=0,
        args=[bot],
        id='reminder_check',
        replace_existing=True
    )
    logger.info(f"✅ Задача 'reminder_check' добавлена: ежедневно в 09:00 МСК")
    
    # Месячные напоминания об обновлении фото в 10:00 МСК
    scheduler.add_job(
        check_monthly_photo_reminders,
        'cron',
        hour=10,
        minute=0,
        args=[bot],
        id='monthly_reminder_check',
        replace_existing=True
    )
    logger.info(f"✅ Задача 'monthly_reminder_check' добавлена: ежедневно в 10:00 МСК")
    
    # КРИТИЧЕСКИ ВАЖНО: Запускаем планировщик
    scheduler.start()
    logger.info("")
    logger.info("🚀 ПЛАНИРОВЩИК ЗАПУЩЕН И АКТИВЕН")
    
    # Проверяем что планировщик действительно работает
    if scheduler.running:
        logger.info("✅ Статус планировщика: РАБОТАЕТ")
        logger.info(f"📊 Активных задач: {len(scheduler.get_jobs())}")
        
        # Выводим список всех задач С ВРЕМЕНЕМ после запуска
        logger.info("")
        logger.info("📋 СПИСОК АКТИВНЫХ ЗАДАЧ:")
        for job in scheduler.get_jobs():
            # Теперь next_run_time доступен после start()
            next_run = job.next_run_time.strftime('%d.%m.%Y %H:%M:%S') if job.next_run_time else 'не запланировано'
            logger.info(f"   • {job.id}: следующий запуск {next_run}")
    else:
        logger.error("❌ ПЛАНИРОВЩИК НЕ ЗАПУСТИЛСЯ!")
    
    logger.info("=" * 70)


async def webhook_handler(request):
    """Webhook обработчик"""
    try:
        url = str(request.url)
        index = url.rfind('/')
        token = url[index + 1:]
        
        if token == BOT_TOKEN.split(':')[1]:
            update = types.Update.model_validate(await request.json(), strict=False)
            await dp.feed_update(bot, update)
            return web.Response()
        else:
            logger.warning("⚠️ Неверный токен в webhook")
            return web.Response(status=403)
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return web.Response(status=500)


async def health_check(request):
    """Health check endpoint"""
    from utils.time_utils import get_moscow_now
    moscow_now = get_moscow_now()
    
    # Проверяем статус планировщика
    scheduler_status = "running" if scheduler.running else "stopped"
    jobs_count = len(scheduler.get_jobs()) if scheduler.running else 0
    
    next_jobs = []
    if scheduler.running:
        for job in scheduler.get_jobs():
            next_jobs.append({
                "id": job.id,
                "next_run": str(job.next_run_time)
            })
    
    return web.json_response({
        "status": "healthy", 
        "bot": "Bloom AI", 
        "version": "5.4 - Stats Removed + Reminders Fix",
        "time_msk": moscow_now.strftime('%Y-%m-%d %H:%M:%S'),
        "timezone": str(MOSCOW_TZ),
        "scheduler": {
            "status": scheduler_status,
            "jobs_count": jobs_count,
            "next_jobs": next_jobs
        }
    })


async def main():
    """Main функция"""
    try:
        logger.info("🚀 Запуск Bloom AI v5.4 (Stats Removed + Reminders Fix)...")
        
        await on_startup()
        
        if WEBHOOK_URL:
            # Webhook mode
            app = web.Application()
            app.router.add_post('/webhook', webhook_handler)
            app.router.add_get('/health', health_check)
            app.router.add_get('/', health_check)
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', PORT)
            await site.start()
            
            logger.info("")
            logger.info("=" * 70)
            logger.info(f"🚀 BLOOM AI v5.4 УСПЕШНО ЗАПУЩЕН")
            logger.info(f"🌐 Порт: {PORT}")
            logger.info(f"📡 Webhook: {WEBHOOK_URL}/webhook")
            logger.info(f"❤️ Health check: {WEBHOOK_URL}/health")
            logger.info("=" * 70)
            
            try:
                await asyncio.Future()
            except KeyboardInterrupt:
                logger.info("🛑 Остановка через KeyboardInterrupt")
            finally:
                await runner.cleanup()
                await on_shutdown()
        else:
            # Polling mode
            logger.info("")
            logger.info("=" * 70)
            logger.info("🤖 BLOOM AI v5.4 В РЕЖИМЕ POLLING")
            logger.info("⏳ Ожидание сообщений от пользователей...")
            logger.info("=" * 70)
            
            try:
                await dp.start_polling(bot, drop_pending_updates=True)
            except KeyboardInterrupt:
                logger.info("🛑 Остановка через KeyboardInterrupt")
            finally:
                await on_shutdown()
                
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Ошибка запуска: {e}", exc_info=True)
