"""
Plant Memory Manager - Система управления полным контекстом растений
Обеспечивает долгосрочную память AI по каждому растению
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from database import get_db

logger = logging.getLogger(__name__)

class PlantMemoryManager:
    """Менеджер памяти растений"""
    
    def __init__(self):
        self.context_cache = {}  # Кэш контекста в памяти
    
    async def build_full_context(self, plant_id: int, user_id: int, 
                                include_analyses: int = 5,
                                include_qa: int = 10,
                                include_problems: bool = True) -> Dict:
        """
        Построить полный контекст растения
        
        Args:
            plant_id: ID растения
            user_id: ID пользователя
            include_analyses: Количество последних анализов
            include_qa: Количество последних Q&A
            include_problems: Включить историю проблем
        
        Returns:
            Словарь с полным контекстом растения
        """
        try:
            db = await get_db()
            
            # Базовая информация о растении
            plant_info = await db.get_plant_with_state(plant_id, user_id)
            if not plant_info:
                return {}
            
            context = {
                "plant_id": plant_id,
                "plant_name": plant_info['display_name'],
                "species": plant_info.get('plant_name'),
                "added_date": plant_info['saved_date'],
                "current_state": plant_info.get('current_state', 'healthy'),
                "state_changed_date": plant_info.get('state_changed_date'),
                "growth_stage": plant_info.get('growth_stage', 'young'),
                "days_in_collection": (datetime.now() - plant_info['saved_date']).days,
                
                # История ухода
                "watering_info": {
                    "last_watered": plant_info.get('last_watered'),
                    "watering_count": plant_info.get('watering_count', 0),
                    "watering_interval": plant_info.get('watering_interval', 5),
                    "total_waterings": plant_info.get('watering_count', 0)
                },
                
                # История анализов
                "analyses_history": [],
                
                # История состояний
                "state_history": [],
                
                # Вопросы и ответы
                "qa_history": [],
                
                # Проблемы
                "problems": {
                    "current": [],
                    "resolved": [],
                    "recurring": []
                },
                
                # Паттерны пользователя
                "user_patterns": [],
                
                # Условия содержания
                "environment": {}
            }
            
            # Загружаем историю анализов
            if include_analyses > 0:
                analyses = await db.get_plant_analyses_history(plant_id, limit=include_analyses)
                context["analyses_history"] = self._format_analyses(analyses)
            
            # Загружаем историю состояний
            state_history = await db.get_plant_state_history(plant_id, limit=20)
            context["state_history"] = self._format_state_history(state_history)
            
            # Загружаем Q&A историю
            if include_qa > 0:
                qa_history = await db.get_plant_qa_history(plant_id, limit=include_qa)
                context["qa_history"] = self._format_qa_history(qa_history)
            
            # Загружаем проблемы
            if include_problems:
                all_problems = await db.get_plant_problems_history(plant_id, limit=20)
                unresolved = await db.get_unresolved_problems(plant_id)
                
                context["problems"]["current"] = [dict(p) for p in unresolved]
                context["problems"]["resolved"] = [
                    dict(p) for p in all_problems if p['resolved']
                ]
                context["problems"]["recurring"] = self._find_recurring_problems(all_problems)
            
            # Загружаем паттерны пользователя
            patterns = await db.get_user_patterns(plant_id)
            context["user_patterns"] = self._format_patterns(patterns)
            
            # Загружаем условия содержания
            environment = await db.get_plant_environment(plant_id)
            if environment:
                context["environment"] = dict(environment)
            
            # Кэшируем контекст
            self.context_cache[f"{user_id}_{plant_id}"] = {
                "context": context,
                "timestamp": datetime.now()
            }
            
            logger.info(f"✅ Построен полный контекст для растения {plant_id}")
            return context
            
        except Exception as e:
            logger.error(f"❌ Ошибка построения контекста: {e}")
            return {}
    
    def _format_analyses(self, analyses: List[Dict]) -> List[Dict]:
        """Форматировать историю анализов"""
        formatted = []
        for analysis in analyses:
            formatted.append({
                "date": analysis['analysis_date'],
                "confidence": analysis.get('confidence', 0),
                "detected_state": analysis.get('detected_state'),
                "problems": analysis.get('detected_problems'),
                "summary": self._summarize_analysis(analysis['full_analysis'])
            })
        return formatted
    
    def _format_state_history(self, history: List[Dict]) -> List[Dict]:
        """Форматировать историю состояний"""
        formatted = []
        for entry in history:
            formatted.append({
                "date": entry['change_date'],
                "from": entry.get('previous_state'),
                "to": entry['new_state'],
                "reason": entry.get('change_reason'),
                "adjustments": {
                    "watering": entry.get('watering_adjustment', 0),
                    "feeding": entry.get('feeding_adjustment')
                }
            })
        return formatted
    
    def _format_qa_history(self, qa_list: List[Dict]) -> List[Dict]:
        """Форматировать историю Q&A"""
        formatted = []
        for qa in qa_list:
            formatted.append({
                "date": qa['question_date'],
                "question": qa['question_text'],
                "answer_summary": self._summarize_text(qa['answer_text'], max_length=150),
                "feedback": qa.get('user_feedback'),
                "action_taken": qa.get('follow_up_action'),
                "resolved": qa.get('problem_resolved', False)
            })
        return formatted
    
    def _format_patterns(self, patterns: List[Dict]) -> List[Dict]:
        """Форматировать паттерны пользователя"""
        formatted = []
        for pattern in patterns:
            formatted.append({
                "type": pattern['pattern_type'],
                "data": pattern['pattern_data'],
                "confidence": pattern['confidence'],
                "occurrences": pattern['occurrences']
            })
        return formatted
    
    def _find_recurring_problems(self, problems: List[Dict]) -> List[Dict]:
        """Найти повторяющиеся проблемы"""
        problem_counts = {}
        for problem in problems:
            ptype = problem['problem_type']
            if ptype not in problem_counts:
                problem_counts[ptype] = []
            problem_counts[ptype].append(problem)
        
        recurring = []
        for ptype, occurrences in problem_counts.items():
            if len(occurrences) >= 2:
                recurring.append({
                    "problem_type": ptype,
                    "occurrences": len(occurrences),
                    "dates": [p['problem_date'] for p in occurrences],
                    "last_occurrence": occurrences[0]['problem_date']
                })
        
        return recurring
    
    def _summarize_analysis(self, full_text: str, max_length: int = 200) -> str:
        """Сократить анализ до резюме"""
        if not full_text or len(full_text) <= max_length:
            return full_text
        
        # Берем первые строки до ПОЛИВ_АНАЛИЗ
        lines = full_text.split('\n')
        summary = []
        for line in lines[:10]:
            if line.startswith(("РАСТЕНИЕ:", "СОСТОЯНИЕ:", "ТЕКУЩЕЕ_СОСТОЯНИЕ:")):
                summary.append(line)
        
        return '\n'.join(summary)
    
    def _summarize_text(self, text: str, max_length: int = 150) -> str:
        """Сократить текст"""
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."
    
    async def format_context_for_ai(self, plant_id: int, user_id: int,
                                   focus: str = "general") -> str:
        """
        Форматировать контекст для отправки AI
        
        Args:
            plant_id: ID растения
            user_id: ID пользователя
            focus: Фокус контекста ('general', 'problem', 'care', 'comparison')
        
        Returns:
            Отформатированный текст контекста для AI
        """
        context = await self.build_full_context(plant_id, user_id)
        
        if not context:
            return ""
        
        if focus == "general":
            return self._format_general_context(context)
        elif focus == "problem":
            return self._format_problem_context(context)
        elif focus == "care":
            return self._format_care_context(context)
        else:
            return self._format_general_context(context)
    
    def _format_general_context(self, context: Dict) -> str:
        """Форматировать общий контекст"""
        lines = []
        
        # Базовая информация
        lines.append(f"РАСТЕНИЕ: {context['plant_name']} ({context['species']})")
        lines.append(f"В КОЛЛЕКЦИИ: {context['days_in_collection']} дней")
        lines.append(f"ТЕКУЩЕЕ СОСТОЯНИЕ: {context['current_state']}")
        lines.append(f"ЭТАП РОСТА: {context['growth_stage']}")
        lines.append("")
        
        # Полив
        watering = context['watering_info']
        if watering['last_watered']:
            days_ago = (datetime.now() - watering['last_watered']).days
            lines.append(f"ПОЛИВ: последний {days_ago} дней назад, интервал {watering['watering_interval']} дней")
        lines.append("")
        
        # История состояний (последние 3)
        if context['state_history']:
            lines.append("ИСТОРИЯ СОСТОЯНИЙ:")
            for state in context['state_history'][:3]:
                date_str = state['date'].strftime('%d.%m')
                lines.append(f"  {date_str}: {state['from'] or 'начало'} → {state['to']}")
                if state['reason']:
                    lines.append(f"    Причина: {state['reason']}")
            lines.append("")
        
        # Текущие проблемы
        if context['problems']['current']:
            lines.append("ТЕКУЩИЕ ПРОБЛЕМЫ:")
            for problem in context['problems']['current'][:3]:
                lines.append(f"  - {problem['problem_type']}: {problem['problem_description']}")
                if problem['solution_tried']:
                    lines.append(f"    Попытка решения: {problem['solution_tried']}")
            lines.append("")
        
        # Повторяющиеся проблемы
        if context['problems']['recurring']:
            lines.append("ПОВТОРЯЮЩИЕСЯ ПРОБЛЕМЫ:")
            for recurring in context['problems']['recurring']:
                lines.append(f"  - {recurring['problem_type']} (повторялось {recurring['occurrences']} раз)")
            lines.append("")
        
        # Паттерны ухода
        if context['user_patterns']:
            lines.append("ПАТТЕРНЫ УХОДА ПОЛЬЗОВАТЕЛЯ:")
            for pattern in context['user_patterns'][:3]:
                if pattern['confidence'] > 0.5:
                    lines.append(f"  - {pattern['type']}: {pattern['data']}")
            lines.append("")
        
        # Условия содержания
        if context['environment']:
            env = context['environment']
            if env.get('location'):
                lines.append(f"РАСПОЛОЖЕНИЕ: {env['location']}")
            if env.get('lighting'):
                lines.append(f"ОСВЕЩЕНИЕ: {env['lighting']}")
            lines.append("")
        
        # Предыдущие вопросы (последние 3)
        if context['qa_history']:
            lines.append("ПРЕДЫДУЩИЕ ВОПРОСЫ:")
            for qa in context['qa_history'][:3]:
                date_str = qa['date'].strftime('%d.%m')
                lines.append(f"  {date_str}: {qa['question']}")
                if qa['action_taken']:
                    lines.append(f"    Действие: {qa['action_taken']}")
                if qa['resolved']:
                    lines.append(f"    ✓ Решено")
            lines.append("")
        
        return '\n'.join(lines)
    
    def _format_problem_context(self, context: Dict) -> str:
        """Форматировать контекст с фокусом на проблемы"""
        lines = []
        
        lines.append(f"РАСТЕНИЕ: {context['plant_name']}")
        lines.append(f"СОСТОЯНИЕ: {context['current_state']}")
        lines.append("")
        
        # Детальная история проблем
        if context['problems']['current']:
            lines.append("=== ТЕКУЩИЕ ПРОБЛЕМЫ ===")
            for problem in context['problems']['current']:
                lines.append(f"\nПроблема: {problem['problem_type']}")
                lines.append(f"Описание: {problem['problem_description']}")
                if problem['suspected_cause']:
                    lines.append(f"Предполагаемая причина: {problem['suspected_cause']}")
                if problem['solution_tried']:
                    lines.append(f"Что уже пробовали: {problem['solution_tried']}")
                    if problem['result']:
                        lines.append(f"Результат: {problem['result']}")
                days_ago = (datetime.now() - problem['problem_date']).days
                lines.append(f"Начало: {days_ago} дней назад")
            lines.append("")
        
        # Повторяющиеся проблемы с деталями
        if context['problems']['recurring']:
            lines.append("=== ПОВТОРЯЮЩИЕСЯ ПРОБЛЕМЫ (ВАЖНО!) ===")
            for recurring in context['problems']['recurring']:
                lines.append(f"\n{recurring['problem_type']}:")
                lines.append(f"  Повторений: {recurring['occurrences']}")
                lines.append(f"  Даты: {', '.join([d.strftime('%d.%m') for d in recurring['dates'][:5]])}")
            lines.append("")
        
        # Решенные проблемы (для контекста)
        if context['problems']['resolved']:
            lines.append("РАНЕЕ РЕШЕННЫЕ ПРОБЛЕМЫ:")
            for problem in context['problems']['resolved'][:3]:
                lines.append(f"  - {problem['problem_type']}: {problem['solution_tried']}")
            lines.append("")
        
        # Связанные Q&A
        problem_related_qa = [qa for qa in context['qa_history'] if not qa['resolved']]
        if problem_related_qa:
            lines.append("СВЯЗАННЫЕ ВОПРОСЫ:")
            for qa in problem_related_qa[:3]:
                lines.append(f"  {qa['date'].strftime('%d.%m')}: {qa['question']}")
        
        return '\n'.join(lines)
    
    def _format_care_context(self, context: Dict) -> str:
        """Форматировать контекст с фокусом на уход"""
        lines = []
        
        lines.append(f"РАСТЕНИЕ: {context['plant_name']}")
        lines.append(f"ВОЗРАСТ В КОЛЛЕКЦИИ: {context['days_in_collection']} дней")
        lines.append("")
        
        # Детальная информация о поливе
        watering = context['watering_info']
        lines.append("=== ИСТОРИЯ ПОЛИВА ===")
        lines.append(f"Всего поливов: {watering['total_waterings']}")
        lines.append(f"Интервал: {watering['watering_interval']} дней")
        if watering['last_watered']:
            days_ago = (datetime.now() - watering['last_watered']).days
            lines.append(f"Последний полив: {days_ago} дней назад")
            if days_ago > watering['watering_interval']:
                lines.append(f"⚠️ Просрочен на {days_ago - watering['watering_interval']} дней")
        lines.append("")
        
        # Паттерны ухода
        if context['user_patterns']:
            lines.append("=== ВАШИ ПАТТЕРНЫ УХОДА ===")
            for pattern in context['user_patterns']:
                if pattern['confidence'] > 0.4:
                    lines.append(f"{pattern['type']}:")
                    lines.append(f"  {pattern['data']}")
                    lines.append(f"  Уверенность: {int(pattern['confidence']*100)}% ({pattern['occurrences']} наблюдений)")
            lines.append("")
        
        # Условия содержания
        if context['environment']:
            lines.append("=== УСЛОВИЯ СОДЕРЖАНИЯ ===")
            env = context['environment']
            for key, value in env.items():
                if value and key not in ['id', 'plant_id', 'user_id', 'updated_date']:
                    lines.append(f"{key}: {value}")
            lines.append("")
        
        # История изменений состояния
        if context['state_history']:
            lines.append("=== ДИНАМИКА СОСТОЯНИЯ ===")
            for i, state in enumerate(context['state_history'][:5]):
                date_str = state['date'].strftime('%d.%m')
                change = f"{state['from'] or 'начало'} → {state['to']}"
                lines.append(f"{date_str}: {change}")
                if state['adjustments']['watering']:
                    adj = state['adjustments']['watering']
                    lines.append(f"  Корректировка полива: {'+' if adj > 0 else ''}{adj} дней")
        
        return '\n'.join(lines)
    
    async def analyze_patterns(self, plant_id: int, user_id: int) -> Dict:
        """
        Анализ паттернов ухода и предсказание проблем
        
        Returns:
            Словарь с обнаруженными паттернами и предсказаниями
        """
        context = await self.build_full_context(plant_id, user_id)
        
        patterns = {
            "watering_pattern": None,
            "problem_correlation": [],
            "care_consistency": 0.0,
            "predictions": []
        }
        
        # Анализ паттернов полива
        if context['watering_info']['total_waterings'] >= 5:
            patterns["watering_pattern"] = self._analyze_watering_pattern(context)
        
        # Корреляция проблем с действиями
        if context['problems']['recurring']:
            patterns["problem_correlation"] = self._analyze_problem_correlation(context)
        
        # Оценка постоянства ухода
        patterns["care_consistency"] = self._calculate_care_consistency(context)
        
        # Предсказания
        patterns["predictions"] = await self._generate_predictions(context)
        
        return patterns
    
    def _analyze_watering_pattern(self, context: Dict) -> Dict:
        """Анализ паттернов полива"""
        watering = context['watering_info']
        
        return {
            "average_interval": watering['watering_interval'],
            "total_waterings": watering['total_waterings'],
            "regularity": "регулярный" if watering['total_waterings'] >= 10 else "нерегулярный"
        }
    
    def _analyze_problem_correlation(self, context: Dict) -> List[Dict]:
        """Анализ корреляции проблем с событиями"""
        correlations = []
        
        # Ищем повторяющиеся проблемы
        for recurring in context['problems']['recurring']:
            # Проверяем связь с поливом
            correlations.append({
                "problem": recurring['problem_type'],
                "suspected_trigger": "Возможно связано с режимом полива",
                "recommendation": "Проверьте частоту полива"
            })
        
        return correlations
    
    def _calculate_care_consistency(self, context: Dict) -> float:
        """Рассчитать постоянство ухода (0-1)"""
        score = 0.5
        
        # Бонусы
        if context['watering_info']['total_waterings'] >= 10:
            score += 0.2
        
        if len(context['state_history']) >= 3:
            score += 0.1
        
        if context['user_patterns']:
            score += 0.2
        
        # Штрафы
        if len(context['problems']['current']) > 2:
            score -= 0.2
        
        return max(0.0, min(1.0, score))
    
    async def _generate_predictions(self, context: Dict) -> List[Dict]:
        """Генерация предсказаний"""
        predictions = []
        
        # Предсказание на основе повторяющихся проблем
        if context['problems']['recurring']:
            for recurring in context['problems']['recurring']:
                if recurring['occurrences'] >= 3:
                    predictions.append({
                        "type": "recurring_problem",
                        "problem": recurring['problem_type'],
                        "confidence": 0.7,
                        "message": f"Высокая вероятность повторения проблемы '{recurring['problem_type']}'"
                    })
        
        # Предсказание на основе просроченного полива
        watering = context['watering_info']
        if watering['last_watered']:
            days_since = (datetime.now() - watering['last_watered']).days
            if days_since > watering['watering_interval'] + 2:
                predictions.append({
                    "type": "watering_needed",
                    "confidence": 0.9,
                    "message": f"Полив просрочен на {days_since - watering['watering_interval']} дней"
                })
        
        return predictions
    
    def clear_cache(self, user_id: int = None, plant_id: int = None):
        """Очистить кэш контекста"""
        if user_id and plant_id:
            key = f"{user_id}_{plant_id}"
            if key in self.context_cache:
                del self.context_cache[key]
        elif user_id:
            # Очистить все растения пользователя
            keys_to_delete = [k for k in self.context_cache.keys() if k.startswith(f"{user_id}_")]
            for key in keys_to_delete:
                del self.context_cache[key]
        else:
            # Очистить весь кэш
            self.context_cache.clear()


# Глобальный экземпляр
memory_manager = PlantMemoryManager()

async def get_plant_context(plant_id: int, user_id: int, focus: str = "general") -> str:
    """Получить контекст растения для AI"""
    return await memory_manager.format_context_for_ai(plant_id, user_id, focus)

async def save_interaction(plant_id: int, user_id: int, question: str, answer: str, context_used: dict = None):
    """Сохранить взаимодействие с растением"""
    db = await get_db()
    await db.save_qa_interaction(plant_id, user_id, question, answer, context_used)
    memory_manager.clear_cache(user_id, plant_id)
