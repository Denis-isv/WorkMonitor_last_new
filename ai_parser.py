"""AI-парсер для WorkMonitor.

Поддерживает любой OpenAI-совместимый endpoint:
- OpenRouter (бесплатные модели)
- OpenAI
- Ollama (локально)
"""
import os
import re
import json
import logging
from datetime import date, datetime, timedelta

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class AIParser:
    def __init__(self, api_key, base_url, models, proxy=None, timeout=60.0):
        if isinstance(models, str):
            models = [m.strip() for m in models.split(',') if m.strip()]
        self.models = models if models else ['gpt-4o-mini']
        self.current_idx = 0
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.proxy = proxy or None
        self.timeout = timeout

    @property
    def model(self):
        return self.models[self.current_idx]

    def _next_model(self):
        self.current_idx = (self.current_idx + 1) % len(self.models)
        logger.warning(f"🔄 Переключаюсь на модель: {self.model}")

    async def analyze(self, text, user_full_name, today=None):
        if today is None:
            today = date.today()

        system_prompt = self._build_system_prompt(today)
        user_prompt = f"Сотрудник: {user_full_name}\nСообщение: {text}"

        last_error = None
        attempts = len(self.models) + 1

        for _ in range(attempts):
            try:
                async with httpx.AsyncClient(proxy=self.proxy, timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://workmonitor.local",
                            "X-Title": "WorkMonitor",
                        },
                        json={
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "temperature": 0.1,
                        },
                    )

                    if response.status_code == 429:
                        logger.warning(f"429 на {self.model}, переключаюсь")
                        last_error = "все модели перегружены"
                        self._next_model()
                        continue

                    if response.status_code in (400, 404):
                        logger.warning(f"{response.status_code} на {self.model}, переключаюсь")
                        last_error = f"модель недоступна ({response.status_code})"
                        self._next_model()
                        continue

                    response.raise_for_status()
                    data = response.json()
                    content = data['choices'][0]['message']['content']
                    return self._parse_response(content)

            except httpx.HTTPStatusError as e:
                last_error = f"HTTP {e.response.status_code}"
                logger.error(f"AI HTTP error: {e.response.status_code} {e.response.text[:200]}")
                if e.response.status_code in (400, 403, 404, 429):
                    self._next_model()
                    continue
                break
            except Exception as e:
                last_error = str(e)
                logger.error(f"AI analyze error: {e}")
                break

        return {
            'action': 'error',
            'response': f'⚠️ ИИ недоступен ({last_error}). Попробуй через минуту.',
        }

    def _build_system_prompt(self, today):
        days_ru = ['понедельник', 'вторник', 'среда', 'четверг',
                   'пятница', 'суббота', 'воскресенье']
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)

        return f"""Ты — ассистент системы WorkMonitor для учёта рабочего времени.
Разбирай сообщения сотрудников и возвращай СТРОГО JSON (без markdown, без пояснений).

Контекст:
- Сегодня: {today.isoformat()} ({days_ru[today.weekday()]})
- Вчера: {yesterday.isoformat()}
- Завтра: {tomorrow.isoformat()}

Формат ответа:
{{
  "action": "plan" | "fact" | "note" | "absence" | "query" | "chat" | "unknown",
  "date": "YYYY-MM-DD" | null,
  "start": "HH:MM" | null,
  "end": "HH:MM" | null,
  "efficiency": 0-100 | null,
  "tasks": ["..."] | null,
  "absence_type": "vacation" | "sick_leave" | "day_off" | "other" | null,
  "date_end": "YYYY-MM-DD" | null,
  "query_type": "summary" | "who" | "schedule" | null,
  "response": "краткий ответ пользователю на русском"
}}

Что означает action:
- "plan" — план на будущий день
- "fact" — факт работы (сколько часов работал)
- "note" — перечень того, что уже сделал (без времени)
- "absence" — заявка на отпуск/больничный/выходной
- "query" — вопрос про статистику/расписание
- "chat" — обычная беседа
- "unknown" — не удалось понять

Правила:
- Даты в формате YYYY-MM-DD
- Время в формате HH:MM (24ч)
- Время ВСЕГДА бери ИЗ ТЕКСТА — НИКОГДА не подставляй 09:00–18:00 по умолчанию
- Если пользователь пишет "с 7:40 до 20:00" → start="07:40", end="20:00"
- Эффективность — только если явно указана
- Для больничного date_end может быть null
- Только JSON, без markdown и лишних слов

Примеры:
- "сегодня был с 7:40 до 20:00" → {{"action":"fact","date":"{today.isoformat()}","start":"07:40","end":"20:00","response":"Записал"}}
- "сегодня с 10 до 18" → {{"action":"fact","date":"{today.isoformat()}","start":"10:00","end":"18:00","response":"Записал"}}
- "завтра с 9 до 17, задачи: отчёт" → {{"action":"plan","date":"{tomorrow.isoformat()}","start":"09:00","end":"17:00","tasks":["отчёт"],"response":"Записал"}}
- "сделал отчёт и провёл встречу" → {{"action":"note","tasks":["отчёт","встреча"],"response":"Записал"}}
- "хочу в отпуск с 5 по 10 ноября" → {{"action":"absence","absence_type":"vacation","date":"{today.year}-11-05","date_end":"{today.year}-11-10","response":"Заявка"}}
- "больничный с сегодня" → {{"action":"absence","absence_type":"sick_leave","date":"{today.isoformat()}","date_end":null,"response":"Больничный"}}
- "сколько наработал" → {{"action":"query","query_type":"summary","response":"Смотрю"}}
- "привет" → {{"action":"chat","response":"Привет! Чем помочь?"}}"""

    def _parse_response(self, content):
        content = content.strip()
        if content.startswith('```'):
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        return {'action': 'unknown', 'response': '⚠️ ИИ вернул нечитаемый ответ.'}


# =====================================================================
# Singleton
# =====================================================================
_AI_PARSER_INSTANCE = None


def get_ai_parser():
    global _AI_PARSER_INSTANCE
    if _AI_PARSER_INSTANCE is not None:
        return _AI_PARSER_INSTANCE

    enabled = os.getenv('AI_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
    api_key = os.getenv('AI_API_KEY', '').strip()
    base_url = os.getenv('AI_BASE_URL', 'https://openrouter.ai/api/v1').strip()
    models_str = os.getenv('AI_MODELS', os.getenv('AI_MODEL', 'gpt-4o-mini')).strip()
    proxy = os.getenv('AI_PROXY', '').strip() or None

    if not enabled or not api_key:
        return None

    models = [m.strip() for m in models_str.split(',') if m.strip()]
    _AI_PARSER_INSTANCE = AIParser(
        api_key=api_key,
        base_url=base_url,
        models=models,
        proxy=proxy,
    )
    return _AI_PARSER_INSTANCE


# =====================================================================
# Синхронная обёртка для совместимости со старым кодом
# =====================================================================
def parse_work_message(text: str) -> dict:
    import asyncio

    if not text or not text.strip():
        return {'ok': False, 'error': 'empty_text'}

    parser = get_ai_parser()
    if not parser:
        return {'ok': False, 'error': 'ai_disabled'}

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, parser.analyze(text, 'Сотрудник')).result(timeout=90)
        else:
            result = loop.run_until_complete(parser.analyze(text, 'Сотрудник'))
    except Exception:
        try:
            result = asyncio.run(parser.analyze(text, 'Сотрудник'))
        except Exception as e:
            return {'ok': False, 'error': 'ai_parse_failed', 'message': str(e)}

    if result.get('action') == 'error':
        return {'ok': False, 'error': 'ai_error', 'message': result.get('response', '')}

    events = []
    action = result.get('action')
    if action in ('fact', 'plan', 'note', 'absence'):
        events.append({
            'type': action,
            'title': result.get('response', action),
            'description': '',
            'duration_minutes': None,
            'confidence': 0.9,
        })
    return {'ok': True, 'action': action, 'events': events, 'raw': result}