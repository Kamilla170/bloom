# === ПОСЛЕ КОМАНДЫ /help ДОБАВЬТЕ ЭТО ===

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработка фотографий растений"""
    try:
        processing_msg = await message.reply(
            "🔍 <b>Анализирую ваше растение...</b>\n"
            "⏳ Определяю вид и состояние растения\n"
            "🧠 Готовлю персональные рекомендации",
            parse_mode="HTML"
        )
        
        # Получаем фото в лучшем качестве
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)
        
        # Анализируем с вопросом пользователя если есть
        user_question = message.caption if message.caption else None
        result = await analyze_plant_image(file_data.read(), user_question)
        
        await processing_msg.delete()
        
        if result["success"]:
            # Сохраняем детальный анализ
            user_id = message.from_user.id
            temp_analyses[user_id] = {
                "analysis": result.get("raw_analysis", result["analysis"]),
                "formatted_analysis": result["analysis"],
                "photo_file_id": photo.file_id,
                "date": datetime.now(),
                "source": result.get("source", "unknown"),
                "plant_name": result.get("plant_name", "Неизвестное растение"),
                "confidence": result.get("confidence", 0),
                "needs_retry": result.get("needs_retry", False)
            }
            
            # Добавляем рекомендации по улучшению фото если нужно
            retry_text = ""
            if result.get("needs_retry"):
                retry_text = ("\n\n📸 <b>Для лучшего результата:</b>\n"
                            "• Сфотографируйте при ярком освещении\n"
                            "• Покажите листья крупным планом\n"
                            "• Уберите лишние предметы из кадра")
            
            response_text = f"🌱 <b>Результат анализа:</b>\n\n{result['analysis']}{retry_text}"
            
            # Выбираем клавиатуру в зависимости от качества анализа
            if result.get("needs_retry"):
                keyboard = [
                    [InlineKeyboardButton(text="🔄 Повторить фото", callback_data="reanalyze")],
                    [InlineKeyboardButton(text="💾 Сохранить как есть", callback_data="save_plant")],
                    [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="ask_about")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
                ]
                reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
            else:
                reply_markup = after_analysis()
            
            await message.reply(
                response_text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        else:
            error_msg = result.get('error', 'Неизвестная ошибка')
            await message.reply(
                f"❌ <b>Ошибка анализа:</b> {error_msg}\n\n"
                f"🔄 Попробуйте:\n"
                f"• Сделать фото при лучшем освещении\n"
                f"• Показать растение целиком\n"
                f"• Повторить попытку через минуту",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            
    except Exception as e:
        print(f"Ошибка обработки фото: {e}")
        await message.reply(
            "❌ Произошла техническая ошибка при анализе.\n"
            "🔄 Пожалуйста, попробуйте позже или обратитесь в поддержку.",
            reply_markup=main_menu()
        )

@dp.message(F.text, ~StateFilter(PlantStates.waiting_question, PlantStates.editing_plant_name))
async def handle_text_message(message: types.Message):
    """Обработка произвольных текстовых сообщений"""
    try:
        text = message.text.strip()
        
        # Пропускаем команды
        if text.startswith('/'):
            return
        
        # Проверяем, связан ли текст с растениями и безопасен ли он
        is_safe_plant_topic, reason = is_plant_related_and_safe(text)
        
        if reason == "illegal":
            await message.reply(
                "⚠️ Извините, я не могу предоставить информацию о таких растениях.\n\n"
                "🌱 Я помогаю только с комнатными, садовыми и декоративными растениями!\n"
                "📸 Пришлите фото своего домашнего растения для анализа.",
                reply_markup=main_menu()
            )
            return
        
        if not is_safe_plant_topic:
            await message.reply(
                "🌱 Я специализируюсь только на вопросах о растениях!\n\n"
                "💡 <b>Могу помочь с:</b>\n"
                "• Уходом за комнатными растениями\n"
                "• Проблемами с листьями и цветением\n"
                "• Поливом и подкормкой\n"
                "• Болезнями и вредителями\n"
                "• Пересадкой и размножением\n\n"
                "📸 Или пришлите фото растения для анализа!",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            return
        
        # Обрабатываем вопрос о растениях
        processing_msg = await message.reply("🌿 <b>Консультируюсь по вашему вопросу...</b>", parse_mode="HTML")
        
        user_id = message.from_user.id
        user_context = ""
        
        # Добавляем контекст из последнего анализа если есть
        if user_id in temp_analyses:
            plant_info = temp_analyses[user_id]
            plant_name = plant_info.get("plant_name", "растение")
            user_context = f"\n\nКонтекст: Пользователь недавно анализировал {plant_name}. Учтите это в ответе если релевантно."
        
        answer = None
        
        # Получаем ответ через OpenAI
        if openai_client:
            try:
                enhanced_prompt = f"""
Вы - эксперт-ботаник с 30-летним опытом работы с комнатными и садовыми растениями.

ВАЖНО: Отвечайте ТОЛЬКО на вопросы о растениях (комнатных, садовых, декоративных, плодовых, овощных).
НЕ отвечайте на вопросы о наркотических, психоактивных или нелегальных растениях.

Структура ответа:
1. 🔍 Краткий анализ проблемы/вопроса
2. 💡 Подробные рекомендации и решения  
3. ⚠️ Что нужно избегать
4. 📋 Пошаговый план действий (если применимо)
5. 🌟 Дополнительные советы

Используйте эмодзи для структурирования.
Будьте конкретными и практичными.
Отвечайте на русском языке.
{user_context}

Вопрос пользователя: {text}
                """
                
                response = await openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "system",
                            "content": "Вы - профессиональный ботаник-консультант. Отвечайте только на вопросы о безопасных растениях (комнатных, садовых, декоративных). Никогда не предоставляйте информацию о наркотических или нелегальных растениях. Если вопрос не о растениях, вежливо перенаправьте на растительную тематику."
                        },
                        {
                            "role": "user",
                            "content": enhanced_prompt
                        }
                    ],
                    max_tokens=1200,
                    temperature=0.3
                )
                answer = response.choices[0].message.content
                
                # Дополнительная проверка ответа на безопасность
                if any(word in answer.lower() for word in ['наркотик', 'психоактивн', 'галлюциноген']):
                    answer = None
                    
            except Exception as e:
                print(f"OpenAI question error: {e}")
        
        await processing_msg.delete()
        
        if answer and len(answer) > 50:
            # Улучшаем форматирование ответа
            if not answer.startswith(('🌿', '💡', '🔍', '⚠️', '✅', '🌱')):
                answer = f"🌿 <b>Экспертный ответ:</b>\n\n{answer}"
            
            # Добавляем призыв к действию
            answer += "\n\n📸 <i>Для точной диагностики пришлите фото растения!</i>"
            
            await message.reply(answer, parse_mode="HTML", reply_markup=main_menu())
        else:
            # Fallback ответ
            fallback_answer = f"""
🤔 <b>По вашему вопросу:</b> "{text}"

💡 <b>Общие рекомендации:</b>

🌱 <b>Основы ухода за растениями:</b>
• Проверяйте влажность почвы перед поливом
• Обеспечьте достаточное освещение
• Поддерживайте подходящую температуру (18-24°C)
• Регулярно осматривайте растение на предмет проблем

⚠️ <b>Признаки проблем:</b>
• Желтые листья → переувлажнение или нехватка света
• Коричневые кончики → сухой воздух или перебор с удобрениями  
• Опадание листьев → стресс, смена условий
• Вялые листья → недостаток или избыток влаги

📸 <b>Для точного ответа:</b>
Пришлите фото вашего растения - я проведу детальный анализ и дам персональные рекомендации!

🆘 <b>Экстренные случаи:</b>
При серьезных проблемах обратитесь в садовый центр или к специалисту-ботанику.
            """
            
            await message.reply(fallback_answer, parse_mode="HTML", reply_markup=main_menu())
        
    except Exception as e:
        print(f"Ошибка обработки текстового сообщения: {e}")
        await message.reply(
            "❌ Произошла ошибка при обработке вашего сообщения.\n"
            "🔄 Попробуйте переформулировать вопрос или пришлите фото растения.", 
            reply_markup=main_menu()
        )

# === ОБРАБОТЧИКИ СОСТОЯНИЙ ===
@dp.message(StateFilter(PlantStates.waiting_question))
async def handle_question(message: types.Message, state: FSMContext):
    """Обработка текстовых вопросов с улучшенным контекстом"""
    try:
        processing_msg = await message.reply("🤔 <b>Консультируюсь с экспертом...</b>", parse_mode="HTML")
        
        user_id = message.from_user.id
        user_context = ""
        
        # Добавляем контекст из последнего анализа если есть
        if user_id in temp_analyses:
            plant_info = temp_analyses[user_id]
            plant_name = plant_info.get("plant_name", "растение")
            user_context = f"\n\nКонтекст: Пользователь недавно анализировал {plant_name}. Учтите это в ответе."
        
        answer = None
        
        # Улучшенный промпт для OpenAI
        if openai_client:
            try:
                enhanced_prompt = f"""
Вы - ведущий эксперт по комнатным и садовым растениям с 30-летним опытом.
Ответьте подробно и практично на вопрос пользователя о растениях.

Структура ответа:
1. Краткий диагноз/ответ на вопрос
2. Подробные рекомендации по решению
3. Дополнительные советы по профилактике
4. При необходимости - когда обращаться к специалисту

Используйте эмодзи для наглядности.
Давайте конкретные, применимые советы.
{user_context}

Вопрос: {message.text}
                """
                
                response = await openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "system",
                            "content": "Вы - профессиональный ботаник и консультант по растениям. Отвечайте экспертно, но доступным языком на русском."
                        },
                        {
                            "role": "user",
                            "content": enhanced_prompt
                        }
                    ],
                    max_tokens=1000,
                    temperature=0.3
                )
                answer = response.choices[0].message.content
            except Exception as e:
                print(f"OpenAI question error: {e}")
        
        await processing_msg.delete()
        
        if answer and len(answer) > 50:
            # Улучшаем форматирование ответа
            if not answer.startswith(('🌿', '💡', '🔍', '⚠️', '✅')):
                answer = f"🌿 <b>Экспертный ответ:</b>\n\n{answer}"
            
            await message.reply(answer, parse_mode="HTML", reply_markup=main_menu())
        else:
            # Улучшенный fallback
            fallback_answer = f"""
🤔 <b>По вашему вопросу:</b> "{message.text}"

К сожалению, сейчас не могу дать полный экспертный ответ. 

💡 <b>Рекомендую:</b>
• Сфотографируйте растение для точной диагностики
• Опишите симптомы более подробно
• Обратитесь в ботанический сад или садовый центр
• Попробуйте переформулировать вопрос

🌱 <b>Общие советы:</b>
• Проверьте освещение и полив
• Осмотрите листья на предмет вредителей  
• Убедитесь в подходящей влажности воздуха

Попробуйте задать вопрос позже или пришлите фото для анализа!
            """
            
            await message.reply(fallback_answer, parse_mode="HTML", reply_markup=main_menu())
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка ответа на вопрос: {e}")
        await message.reply(
            "❌ Произошла ошибка при обработке вопроса.\n"
            "🔄 Попробуйте переформулировать или задать вопрос позже.", 
            reply_markup=main_menu()
        )
        await state.clear()

@dp.message(StateFilter(PlantStates.editing_plant_name))
async def handle_plant_name_edit(message: types.Message, state: FSMContext):
    """Обработка нового названия растения"""
    try:
        data = await state.get_data()
        plant_id = data.get('editing_plant_id')
        
        if not plant_id:
            await message.reply("❌ Ошибка: растение не найдено.")
            await state.clear()
            return
        
        new_name = message.text.strip()
        
        # Проверяем длину названия
        if len(new_name) < 2:
            await message.reply("❌ Название слишком короткое. Минимум 2 символа.")
            return
        
        if len(new_name) > 50:
            await message.reply("❌ Название слишком длинное. Максимум 50 символов.")
            return
        
        # Обновляем название в базе данных
        db = await get_db()
        await db.update_plant_name(plant_id, message.from_user.id, new_name)
        
        await message.reply(
            f"✅ <b>Название изменено!</b>\n\n"
            f"🌱 Новое название: <b>{new_name}</b>\n\n"
            f"Растение обновлено в вашей коллекции.",
            parse_mode="HTML",
            reply_markup=plant_management_keyboard(plant_id)
        )
        
        await state.clear()
        
    except Exception as e:
        print(f"Ошибка сохранения названия: {e}")
        await message.reply("❌ Ошибка сохранения названия.")
        await state.clear()

# === CALLBACK ОБРАБОТЧИКИ ===
# ... (ЗДЕСЬ ДОЛЖНЫ БЫТЬ ВСЕ ОСТАЛЬНЫЕ CALLBACK ОБРАБОТЧИКИ ИЗ ДОКУМЕНТА)

# === ДОПОЛНИТЕЛЬНЫЕ КОМАНДЫ ===
@dp.message(Command("analyze"))
async def analyze_command(message: types.Message):
    """Команда /analyze"""
    await message.answer(
        "📸 <b>Анализ растения</b>\n\n"
        "Отправьте фото растения для получения:\n"
        "🔍 Точного определения вида\n"
        "🩺 Оценки состояния здоровья\n"
        "💡 Персональных рекомендаций по уходу\n\n"
        "📋 <b>Советы для лучшего результата:</b>\n"
        "• Фотографируйте при дневном свете\n"
        "• Покажите листья и общий вид растения\n" 
        "• Избегайте размытых и тёмных снимков\n"
        "• Можете добавить вопрос в описании к фото",
        parse_mode="HTML"
    )

@dp.message(Command("question"))
async def question_command(message: types.Message, state: FSMContext):
    """Команда /question"""
    await message.answer(
        "❓ <b>Консультация по растениям</b>\n\n"
        "💡 <b>Я могу помочь с:</b>\n"
        "• Проблемами с листьями (желтеют, сохнут, опадают)\n"
        "• Режимом полива и подкормки\n" 
        "• Пересадкой и размножением\n"
        "• Болезнями и вредителями\n"
        "• Выбором места для растения\n"
        "• Любыми другими вопросами по уходу\n\n"
        "✍️ <b>Напишите ваш вопрос:</b>",
        parse_mode="HTML"
    )
    await state.set_state(PlantStates.waiting_question)

@dp.message(Command("plants"))
async def plants_command(message: types.Message):
    """Команда /plants"""
    await my_plants_callback(types.CallbackQuery(
        id="cmd_plants",
        from_user=message.from_user,
        chat_instance="cmd",
        message=message,
        data="my_plants"
    ))

@dp.message(Command("stats"))
async def stats_command(message: types.Message):
    """Команда /stats"""
    await stats_callback(types.CallbackQuery(
        id="cmd_stats",
        from_user=message.from_user,
        chat_instance="cmd",
        message=message,
        data="stats"
    ))

# === WEBHOOK ОБРАБОТЧИКИ ===
async def webhook_handler(request):
    """Обработчик webhook запросов"""
    try:
        url = str(request.url)
        index = url.rfind('/')
        token = url[index + 1:]
        
        if token == BOT_TOKEN.split(':')[1]:
            update = types.Update.model_validate(await request.json(), strict=False)
            await dp.feed_update(bot, update)
            return web.Response()
        else:
            return web.Response(status=403)
    except Exception as e:
        print(f"Ошибка webhook: {e}")
        return web.Response(status=500)

# Health check для Railway
async def health_check(request):
    """Проверка здоровья сервиса"""
    return web.json_response({
        "status": "healthy", 
        "bot": "Bloom AI Plant Care Assistant", 
        "version": "2.0",
        "features": ["plant_identification", "health_assessment", "care_recommendations"]
    })

# === ГЛАВНАЯ ФУНКЦИЯ ===
async def main():
    """Основная функция запуска бота"""
    logging.basicConfig(level=logging.INFO)
    
    await on_startup()
    
    if WEBHOOK_URL:
        app = web.Application()
        app.router.add_post('/webhook', webhook_handler)
        app.router.add_get('/health', health_check)
        app.router.add_get('/', health_check)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        print(f"🚀 Bloom AI Plant Bot запущен на порту {PORT}")
        print(f"🌱 Готов к точному распознаванию растений!")
        
        try:
            await asyncio.Future()
        except KeyboardInterrupt:
            print("🛑 Получен сигнал остановки")
        finally:
            await runner.cleanup()
            await on_shutdown()
    else:
        print("🤖 Бот запущен в режиме polling")
        try:
            await dp.start_polling(bot, drop_pending_updates=True)
        except KeyboardInterrupt:
            print("🛑 Получен сигнал остановки")
        finally:
            await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
    except KeyboardInterrupt:
        print("🛑 Принудительная остановка")
