# WorkMonitor

Информационная система мониторинга рабочего времени, трудозатрат и рабочей активности сотрудников с интеграцией Telegram-бота и локальной LLM.

## Возможности

- 📅 Планирование расписания сотрудников
- ⏰ Учёт фактического времени и эффективности
- 🏖 Оформление отпусков, больничных и выходных
- 👥 Управление сотрудниками, подразделениями, проектами и задачами
- 📊 Отчёты и итоги месяца
- 🤖 Telegram-бот с ИИ-разбором свободных сообщений
- 🎤 Транскрипция голосовых (Whisper, опционально)
- 📈 Экспорт данных, аудит изменений

## Стек

- Python 3.11
- Flask + SQLAlchemy + Flask-Login
- SQLite
- python-telegram-bot 22.x
- OpenAI-совместимый API (OpenRouter / Ollama)
- Pillow (картинки расписания)
- Bootstrap 5, Jinja2

## Установка

```bash
# 1. Создать venv
python3.11 -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Настроить конфигурацию
cp .env.example .env
# открой .env и заполни TELEGRAM_BOT_TOKEN, AI_API_KEY, ADMIN_PASSWORD

# 4. Инициализировать БД
python -m flask --app app init-db

# 5. Запустить сайт
python app.py

# 6. В отдельном окне — запустить бота
python telegram_bot.py