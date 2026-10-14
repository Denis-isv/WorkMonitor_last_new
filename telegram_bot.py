"""Telegram-бот WorkMonitor с ИИ-разбором, Whisper и полной синхронизацией."""
import os
import re
import json
import time
import logging
import secrets
import hmac
from io import BytesIO
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from PIL import Image, ImageDraw, ImageFont

from app import (
    app, db, User, CalendarEvent, TelegramMessage, ExtractedEvent,
    LLMRun, ConfirmationRequest, DailySchedule, AbsenceRequest,
    TimeEntry, Project, Task, DayTask,
)
from ai_parser import get_ai_parser

load_dotenv()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
MEDIA_DIR = Path(app.instance_path) / 'telegram_media'
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

CANCEL_TEXT = '❌ Отмена'
DAYS_RU_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
DAYS_RU_FULL = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
MONTHS_RU = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
             'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

_AI_PARSER = None


# =====================================================================
# Утилиты
# =====================================================================
def esc(text):
    if text is None:
        return ''
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def parse_time_string(s):
    h, m = s.split(':')
    return dtime(int(h), int(m))


def time_to_minutes(t):
    return t.hour * 60 + t.minute


def user_by_tg(tid):
    return User.query.filter_by(telegram_id=str(tid), active=True).first()


def _task_lines(text):
    out = []
    if not text:
        return out
    for line in text.split('\n'):
        s = line.strip()
        if not s:
            continue
        if s.startswith('-'):
            s = s[1:].strip()
        if s:
            out.append(s)
    return out


def _norm_task_lines(text):
    out = []
    for line in text.split('\n'):
        s = line.strip()
        if not s:
            continue
        if not s.startswith('-'):
            s = '- ' + s
        out.append(s)
    return out


def _tasks_short(tasks, limit=250):
    if not tasks:
        return ''
    lines = []
    for line in tasks.split('\n'):
        s = line.strip()
        if not s:
            continue
        if s.startswith('-'):
            s = s[1:].strip()
        if s:
            lines.append(s)
    joined = ' • '.join(lines)
    if len(joined) > limit:
        joined = joined[:limit - 3] + '...'
    return joined


def get_fonts():
    paths = [
        '/System/Library/Fonts/Helvetica.ttc',
        '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/Library/Fonts/Arial.ttf',
        'C:\\Windows\\Fonts\\arial.ttf',
        'C:\\Windows\\Fonts\\segoeui.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for path in paths:
        try:
            return {
                'title': ImageFont.truetype(path, 34),
                'day': ImageFont.truetype(path, 24),
                'header': ImageFont.truetype(path, 20),
                'cell': ImageFont.truetype(path, 18),
                'small': ImageFont.truetype(path, 16),
            }
        except Exception:
            continue
    d = ImageFont.load_default()
    return {'title': d, 'day': d, 'header': d, 'cell': d, 'small': d}


def _fallback_worked_hours(start_str, end_str):
    """Резервный расчёт отработанных часов, если calculate_worked_hours недоступна."""
    try:
        sh, sm = map(int, start_str.split(':'))
        eh, em = map(int, end_str.split(':'))
        return timedelta(minutes=(eh * 60 + em) - (sh * 60 + sm))
    except Exception:
        return timedelta(0)


async def _reply_personal(bot, user, text, parse_mode='HTML'):
    """Отправляет сообщение в личку пользователю."""
    if not user or not user.telegram_id:
        return False
    try:
        await bot.send_message(chat_id=int(user.telegram_id), text=text, parse_mode=parse_mode)
        return True
    except Exception as e:
        logger.error(f"Personal reply {user.id}: {e}")
        return False


# =====================================================================
# Клавиатуры
# =====================================================================
def main_menu_keyboard():
    kb = [
        [KeyboardButton('📅 Моё расписание'), KeyboardButton('👤 Мой профиль')],
        [KeyboardButton('⏰ Фактическое время'), KeyboardButton('🏖 Заявка на отпуск')],
        [KeyboardButton('📂 Мои заявки'), KeyboardButton('👥 Кто работает')],
        [KeyboardButton('📊 Итоги месяца'), KeyboardButton('👥 Расписание всех')],
        [KeyboardButton('❓ Помощь'), KeyboardButton('🔓 Отвязать аккаунт')],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_TEXT)]], resize_keyboard=True)


def schedule_week_keyboard(week_start, user_id, offset):
    today = date.today()
    row_days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        sch = DailySchedule.query.filter_by(user_id=user_id, work_date=d).first()
        if sch and sch.is_day_off:
            icon = "🏖"
        elif sch and sch.actual_start and sch.actual_end:
            icon = "🟢"
        elif sch:
            icon = "📋"
        else:
            icon = "⚪"
        today_marker = " •" if d == today else ""
        label = f"{DAYS_RU_SHORT[i]} {d.day} {icon}{today_marker}"
        row_days.append(InlineKeyboardButton(label, callback_data=f"sched_day_{d.isoformat()}"))

    end_of_week = week_start + timedelta(days=6)
    nav_row = [
        InlineKeyboardButton("⬅️", callback_data=f"sched_week_{offset - 1}"),
        InlineKeyboardButton(f"{week_start.strftime('%d.%m')}–{end_of_week.strftime('%d.%m')}",
                             callback_data="sched_noop"),
        InlineKeyboardButton("➡️", callback_data=f"sched_week_{offset + 1}"),
    ]
    return InlineKeyboardMarkup([nav_row, row_days[:4], row_days[4:]])


def schedule_day_keyboard(d, sch):
    ds = d.isoformat()
    has_plan = bool(sch and sch.tasks_text)
    has_fact = bool(sch and (sch.fact_text or sch.actual_start))
    rows = [
        [InlineKeyboardButton("🕐 Плановое время", callback_data=f"sched_time_{ds}")],
        [InlineKeyboardButton("📝 Задачи на день" + (" · ✓" if has_plan else ""),
                              callback_data=f"sched_tasks_{ds}")],
        [InlineKeyboardButton("✅ Факт + эффективность" + (" · ✓" if has_fact else ""),
                              callback_data=f"sched_fact_{ds}")],
        [InlineKeyboardButton("🏖 Отметить выходным", callback_data=f"sched_dayoff_{ds}")],
    ]
    if sch:
        rows.append([InlineKeyboardButton("🗑 Очистить день", callback_data=f"sched_clear_{ds}")])
    rows.append([InlineKeyboardButton("⬅️ К неделе", callback_data="sched_week_0")])
    return InlineKeyboardMarkup(rows)


# =====================================================================
# ИИ — сохранение результата в БД
# =====================================================================
def _ai_parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _ai_parse_time(s):
    if not s:
        return None
    try:
        h, m = s.split(':')
        return parse_time_string(f"{h}:{m}")
    except Exception:
        return None


async def save_ai_result(user, parsed, context):
    action = parsed.get('action', 'unknown')
    response = parsed.get('response', '')

    if action == 'plan':
        d = _ai_parse_date(parsed.get('date')) or (date.today() + timedelta(days=1))
        start = _ai_parse_time(parsed.get('start'))
        end = _ai_parse_time(parsed.get('end'))
        tasks = parsed.get('tasks') or []
        plan_text = '\n'.join(f"- {t}" for t in tasks) if tasks else None

        if not start or not end:
            return "⚠️ Не указано время. Напиши, например: «завтра с 10 до 18»."

        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
        if not sch:
            sch = DailySchedule(user_id=user.id, work_date=d)
            db.session.add(sch)
        sch.start_time = start.strftime('%H:%M')
        sch.end_time = end.strftime('%H:%M')
        sch.is_day_off = False
        if plan_text:
            sch.tasks_text = plan_text
        db.session.commit()

        txt = f"✅ План на {d.strftime('%d.%m.%Y')}: {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        if tasks:
            txt += f"\n📝 Задачи: {len(tasks)}"
        return txt

    if action == 'fact':
        d = _ai_parse_date(parsed.get('date')) or date.today()
        start = _ai_parse_time(parsed.get('start'))
        end = _ai_parse_time(parsed.get('end'))
        eff = parsed.get('efficiency')
        tasks = parsed.get('tasks') or []

        if not start or not end:
            return "⚠️ Не указано время. Напиши: «сегодня работал с 10 до 18»."

        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
        if not sch:
            sch = DailySchedule(user_id=user.id, work_date=d)
            db.session.add(sch)
        sch.actual_start = start.strftime('%H:%M')
        sch.actual_end = end.strftime('%H:%M')

        if eff is None:
            context.user_data['pending_ai_eff'] = {
                'user_id': user.id,
                'date': d.isoformat(),
                'start': start.strftime('%H:%M'),
                'end': end.strftime('%H:%M'),
                'tasks': tasks,
            }
            db.session.commit()
            return (f"📊 Записал факт за {d.strftime('%d.%m.%Y')}: "
                    f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}\n\n"
                    f"Какая <b>эффективность</b>? Отправь число от 0 до 100.\n"
                    f"<i>Например: 85</i>")

        try:
            eff_val = max(0.0, min(1.0, float(eff) / 100.0))
        except (ValueError, TypeError):
            eff_val = 1.0
        sch.efficiency = eff_val
        if tasks:
            sch.fact_text = '\n'.join(f"- {t}" for t in tasks)
        db.session.commit()
        return (f"✅ Факт за {d.strftime('%d.%m.%Y')}: "
                f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}, e% {int(eff_val*100)}")

    if action == 'note':
        tasks = parsed.get('tasks') or []
        if not tasks:
            return "⚠️ Не понял, что именно ты сделал. Уточни."
        d = _ai_parse_date(parsed.get('date')) or date.today()
        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
        if not sch:
            sch = DailySchedule(user_id=user.id, work_date=d)
            db.session.add(sch)
        sch.fact_text = '\n'.join(f"- {t}" for t in tasks)
        db.session.commit()
        return f"✅ Записал {len(tasks)} задач в отчёт за {d.strftime('%d.%m.%Y')}"

    if action == 'absence':
        d_start = _ai_parse_date(parsed.get('date'))
        d_end = _ai_parse_date(parsed.get('date_end'))
        abs_type = parsed.get('absence_type', 'other')

        if not d_start:
            return "⚠️ Не понял даты. Напиши: «отпуск с 5 по 10 ноября»."

        if abs_type not in ('vacation', 'sick_leave', 'day_off', 'other'):
            abs_type = 'other'
        if abs_type != 'sick_leave' and not d_end:
            return "⚠️ Укажи дату окончания (для больничного можно без неё)."

        req = AbsenceRequest(
            user_id=user.id, absence_type=abs_type,
            date_start=d_start, date_end=d_end,
            reason='Создано через ИИ', status='pending')
        db.session.add(req)
        db.session.commit()
        tail = f" до {d_end.strftime('%d.%m.%Y')}" if d_end else " (до выздоровления)"
        return f"✅ Заявка с {d_start.strftime('%d.%m.%Y')}{tail}. Ждёт подтверждения."

    if action == 'query':
        qtype = parsed.get('query_type', 'summary')
        if qtype == 'summary':
            return summary_text(user.id, 0)
        if qtype == 'who':
            return who_text_full()
        return "📊 Уточни, что показать."

    if action == 'chat':
        return response or "😊 Чем помочь?"

    if action == 'error':
        return response

    return response or "🤔 Не понял. Напиши: «сегодня работал с 10 до 18»."


async def ai_message_handler(update, context):
    global _AI_PARSER
    if _AI_PARSER is None:
        _AI_PARSER = get_ai_parser()

    msg = update.message
    chat = update.effective_chat
    if not msg or not msg.text:
        return False

    text = msg.text.strip()
    if not text:
        return False

    # Ожидание ответа по эффективности
    pending = context.user_data.get('pending_ai_eff')
    if pending:
        m = re.match(r'^\s*(\d{1,3})\s*%?\s*$', text)
        if m:
            eff_val = int(m.group(1))
            if not (0 <= eff_val <= 100):
                await msg.reply_text("⚠️ Эффективность от 0 до 100.")
                return True
            with app.app_context():
                db.session.expire_all()
                user = User.query.get(pending['user_id'])
                if not user:
                    context.user_data.pop('pending_ai_eff', None)
                    return True
                d = datetime.strptime(pending['date'], '%Y-%m-%d').date()
                sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
                if sch:
                    sch.efficiency = eff_val / 100.0
                    db.session.commit()
            context.user_data.pop('pending_ai_eff', None)
            await msg.reply_text(
                f"✅ Сохранено: {pending['start']}–{pending['end']}, e% {eff_val}")
            return True
        if text.lower() in ('отмена', 'cancel', '/cancel'):
            context.user_data.pop('pending_ai_eff', None)
            await msg.reply_text("Отменено.")
            return True

    if context.user_data.get('state'):
        return False

    if chat.type in ('group', 'supergroup'):
        bot_username = context.bot.username
        triggered = False
        if bot_username:
            for t in (f"@{bot_username}", f"@{bot_username.lower()}"):
                if t in text:
                    text = text.replace(t, '').strip()
                    triggered = True
                    break
        if not triggered:
            for prefix in ('ии,', 'ии ', 'бот,', 'бот ', 'запиши ', 'работал ', 'планирую '):
                if text.lower().startswith(prefix):
                    triggered = True
                    break
        if not triggered:
            return False
    else:
        if text.startswith('/'):
            return False
        btn = ['📅 Моё расписание', '👤 Мой профиль', '⏰ Фактическое время',
               '🏖 Заявка на отпуск', '📂 Мои заявки', '👥 Кто работает',
               '📊 Итоги месяца', '👥 Расписание всех',
               '❓ Помощь', '🔓 Отвязать аккаунт', CANCEL_TEXT]
        if text in btn:
            return False

    if not _AI_PARSER:
        return False

    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(chat.id if chat.type == 'private' else update.effective_user.id)
        if not user:
            return False

        try:
            await msg.chat.send_action("typing")
        except Exception:
            pass

        t0 = time.perf_counter()
        parsed = await _AI_PARSER.analyze(text=text, user_full_name=user.full_name)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        try:
            db.session.add(LLMRun(
                model=_AI_PARSER.model,
                input_text=text[:5000],
                output_text=json.dumps(parsed, ensure_ascii=False)[:5000],
                processing_time_ms=elapsed_ms,
                status='ok' if parsed.get('action') != 'error' else 'error',
            ))
            db.session.commit()
        except Exception as e:
            logger.error(f"LLMRun save error: {e}")
            db.session.rollback()

        try:
            result_text = await save_ai_result(user, parsed, context)
        except Exception as e:
            logger.exception(f"AI save error: {e}")
            result_text = f"⚠️ Ошибка сохранения: {e}"

    # В группе отвечаем в личку сотруднику
    if chat.type in ('group', 'supergroup'):
        await _reply_personal(context.bot, user, result_text)
    else:
        try:
            await msg.reply_text(result_text, parse_mode='HTML')
        except Exception:
            await msg.reply_text(result_text)
    return True


# =====================================================================
# Whisper
# =====================================================================
async def transcribe_media(context, file_obj, chat_id, message_id):
    if os.getenv('WHISPER_ENABLED', 'false').lower() not in ('1', 'true', 'yes', 'on'):
        return None, '', '', 0.0
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        suffix = Path(tg_file.file_path or '').suffix or '.bin'
        safe_name = f"{str(chat_id).replace('-', '_')}_{message_id}{suffix}"
        target = MEDIA_DIR / safe_name
        await tg_file.download_to_drive(custom_path=str(target))

        from faster_whisper import WhisperModel  # type: ignore
        model_name = os.getenv('WHISPER_MODEL', 'small')
        model = context.application.bot_data.get('whisper_model')
        if model is None:
            model = WhisperModel(
                model_name,
                device=os.getenv('WHISPER_DEVICE', 'cpu'),
                compute_type=os.getenv('WHISPER_COMPUTE_TYPE', 'int8'))
            context.application.bot_data['whisper_model'] = model
        segments, info = model.transcribe(str(target))
        text = ''.join(seg.text for seg in segments).strip()
        return str(target), text, info.language or '', float(info.language_probability or 0)
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        return None, '', '', 0.0


# =====================================================================
# Сохранение сообщений
# =====================================================================
async def store_message(update, context):
    msg = update.message
    if not msg or not update.effective_chat:
        return
    chat = update.effective_chat
    chat_id = str(chat.id)
    mid = str(msg.message_id)

    if msg.text and msg.text.startswith('/'):
        return

    text_value = msg.text or msg.caption or ''
    media_type = 'text'
    file_obj = None
    if msg.voice:
        media_type = 'voice'; file_obj = msg.voice
    elif msg.video_note:
        media_type = 'video_note'; file_obj = msg.video_note
    elif msg.audio:
        media_type = 'audio'; file_obj = msg.audio
    elif msg.document:
        media_type = 'document'; file_obj = msg.document
    elif msg.photo:
        media_type = 'photo'; file_obj = msg.photo[-1]

    with app.app_context():
        db.session.expire_all()
        if TelegramMessage.query.filter_by(chat_id=chat_id, telegram_message_id=mid).first():
            return

        sender = update.effective_user
        tid = str(sender.id) if sender else ''
        user = User.query.filter_by(telegram_id=tid).first() if tid else None

        attachment_path = ''
        transcript = ''
        transcript_lang = ''
        transcript_conf = 0.0

        if file_obj and media_type in ('voice', 'video_note', 'audio', 'video'):
            path, t, lang, conf = await transcribe_media(context, file_obj, chat_id, mid)
            if path:
                attachment_path = path
                transcript = t
                transcript_lang = lang
                transcript_conf = conf
                if t:
                    text_value = t

        row = TelegramMessage(
            telegram_message_id=mid,
            chat_id=chat_id,
            sender_telegram_id=tid,
            sender_name=' '.join(x for x in [(sender.first_name if sender else ''),
                                             (sender.last_name if sender else '')] if x),
            user_id=user.id if user else None,
            message_date=msg.date.replace(tzinfo=None) if msg.date else datetime.utcnow(),
            text=text_value,
            raw_json=json.dumps(msg.to_dict(), ensure_ascii=False, default=str),
            reply_to_message_id=str(msg.reply_to_message.message_id) if msg.reply_to_message else '',
            media_type=media_type,
            attachment_path=attachment_path,
            transcript=transcript,
            transcript_language=transcript_lang,
            transcript_confidence=transcript_conf,
        )
        db.session.add(row)
        db.session.commit()


# =====================================================================
# Команды
# =====================================================================
async def start(update, context):
    context.user_data.clear()
    tid = update.effective_user.id
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(tid)
        if user:
            await update.effective_message.reply_text(
                f"👋 С возвращением, <b>{esc(user.full_name)}</b>!\n\nВыбери действие 👇",
                parse_mode='HTML', reply_markup=main_menu_keyboard())
            return
    await update.effective_message.reply_text(
        "👋 Привет! Я — бот <b>WorkMonitor</b>.\n\n"
        "Чтобы привязать Telegram, отправь:\n"
        "<code>/link ЛОГИН КОД</code>\n\n"
        "Код можно получить в веб-приложении у администратора.",
        parse_mode='HTML', reply_markup=cancel_keyboard())


async def link(update, context):
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "🔗 <b>Привязка аккаунта</b>\n\n"
            "Отправь: <code>/link ЛОГИН КОД</code>\n\n"
            "<b>Пример:</b> <code>/link admin FpNYBc7MZoU</code>",
            parse_mode='HTML')
        return

    if len(args) == 1:
        username, code = None, args[0].strip()
    else:
        username, code = args[0].strip(), args[1].strip()

    tid = str(update.effective_user.id)
    with app.app_context():
        db.session.expire_all()
        if username:
            user = User.query.filter_by(username=username, active=True).first()
            if not user:
                await update.effective_message.reply_text(
                    f"❌ Пользователь «{username}» не найден.")
                return
        else:
            user = User.query.filter_by(telegram_link_code=code, active=True).first()
            if not user:
                await update.effective_message.reply_text("❌ Код не найден.")
                return

        if not user.telegram_link_code or not hmac.compare_digest(user.telegram_link_code, code):
            await update.effective_message.reply_text("❌ Неверный код.")
            return

        existing = User.query.filter_by(telegram_id=tid).first()
        if existing and existing.id != user.id:
            await update.effective_message.reply_text(
                f"⚠️ Этот Telegram уже привязан к «{existing.username}».\nСначала /unlink.")
            return
        if user.telegram_id and user.telegram_id != tid:
            await update.effective_message.reply_text("⚠️ Эта учётка привязана к другому Telegram.")
            return

        user.telegram_id = tid
        user.telegram_link_code = None
        db.session.commit()
        name, uname = user.full_name, user.username

    await update.effective_message.reply_text(
        f"✅ <b>Аккаунт привязан!</b>\n\n👤 {esc(name)} (<code>{esc(uname)}</code>)\n\nОтправь /start.",
        parse_mode='HTML')


async def unlink_cmd(update, context):
    tid = str(update.effective_user.id)
    with app.app_context():
        db.session.expire_all()
        user = User.query.filter_by(telegram_id=tid).first()
        if not user:
            await update.effective_message.reply_text("ℹ️ Этот Telegram не привязан.")
            return
        username_old = user.username
        user.telegram_id = None
        user.telegram_link_code = secrets.token_urlsafe(8)
        db.session.commit()
        new_code = user.telegram_link_code

    await update.effective_message.reply_text(
        f"✅ <b>Аккаунт отвязан</b>\n\n"
        f"Был привязан к: <code>{esc(username_old)}</code>\n\n"
        f"🔑 Новый код: <code>{new_code}</code>\n\n"
        f"Для повторной привязки:\n"
        f"<code>/link {esc(username_old)} {new_code}</code>",
        parse_mode='HTML')


async def help_cmd(update, context):
    text = (
        "❓ <b>Помощь по боту WorkMonitor</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📅 <b>Моё расписание</b> — календарь недели\n"
        "👤 <b>Мой профиль</b> — информация о тебе\n"
        "⏰ <b>Фактическое время</b> — внести факт\n"
        "🏖 <b>Заявка на отпуск</b> — отпуск/больничный\n"
        "📊 <b>Итоги месяца</b> — статистика\n"
        "👥 <b>Расписание всех</b> — картинкой\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Команды:</b>\n"
        "/start — меню\n"
        "/link ЛОГИН КОД — привязать аккаунт\n"
        "/unlink — отвязать\n"
        "/help — справка\n\n"
        "<b>В чате можно писать:</b>\n"
        "• <code>#расписание</code> — план на неделю\n"
        "• <code>#дейли</code> — факт за день\n\n"
        "<b>Свободный текст:</b>\n"
        "• «сегодня работал с 10 до 18»\n"
        "• «завтра с 9 до 17, задачи: отчёт»\n"
        "• «хочу в отпуск с 5 по 10 ноября»"
    )
    await update.effective_message.reply_text(text, parse_mode='HTML',
                                              reply_markup=main_menu_keyboard())


async def cmd_profile(update, context):
    tid = update.effective_user.id
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(tid)
        if not user:
            await update.effective_message.reply_text("❌ /start и привяжи аккаунт.")
            return
        today = date.today()
        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=today).first()
        role_ru = {'admin': 'Администратор', 'employee': 'Сотрудник'}.get(user.role, user.role)
        text = f"👤 <b>{esc(user.full_name)}</b>\n"
        text += f"🔑 <code>{esc(user.username)}</code>\n"
        text += f"🎭 {esc(role_ru)}\n"
        if user.position:
            text += f"💼 {esc(user.position)}\n"
        text += f"\n<b>{DAYS_RU_FULL[today.weekday()]}, {today.strftime('%d.%m.%Y')}</b>\n\n"
        if sch and sch.is_day_off:
            text += "🏖 Выходной\n"
        elif sch:
            text += f"🕐 План: {sch.start_time}–{sch.end_time}\n"
            if sch.actual_start and sch.actual_end:
                eff = int((sch.efficiency or 1.0) * 100)
                text += f"✅ Факт: {sch.actual_start}–{sch.actual_end} · e% {eff}\n"
            if sch.tasks_text:
                text += "\n📝 <b>Задачи:</b>\n"
                for t in _task_lines(sch.tasks_text):
                    text += f"   • {esc(t)}\n"
            if sch.fact_text:
                text += "\n✅ <b>Сделано:</b>\n"
                for t in _task_lines(sch.fact_text):
                    text += f"   • {esc(t)}\n"
        else:
            text += "<i>График не задан</i>\n"
    await update.effective_message.reply_text(text, parse_mode='HTML',
                                              reply_markup=main_menu_keyboard())


def who_text_full():
    today = date.today()
    users = User.query.filter_by(active=True).order_by(User.full_name).all()
    schedules = {x.user_id: x for x in DailySchedule.query.filter_by(work_date=today).all()}
    absences = AbsenceRequest.query.filter(
        AbsenceRequest.status == 'approved',
        AbsenceRequest.date_start <= today).all()
    absent = {}
    for a in absences:
        if a.date_end is None or a.date_end >= today:
            absent[a.user_id] = a

    text = f"👥 <b>Кто работает сегодня</b>\n<i>{today.strftime('%d.%m.%Y')}</i>\n━━━━━━━━━━━━━━━━━━━━\n"
    found = False
    for u in users:
        if u.id in absent:
            a = absent[u.id]
            status = {'vacation': '🏖 Отпуск', 'sick_leave': '🤒 Больничный',
                      'day_off': '🏖 Выходной'}.get(a.absence_type, '❌ Отсутствует')
            text += f"\n<b>{esc(u.full_name)}</b>\n   {status}\n"
            found = True
        else:
            sch = schedules.get(u.id)
            if sch and not sch.is_day_off:
                text += f"\n<b>{esc(u.full_name)}</b>\n   🕐 {sch.start_time}–{sch.end_time}\n"
                found = True
    if not found:
        text += "\n<i>Сегодня никто не работает.</i>"
    return text


async def cmd_who(update, context):
    with app.app_context():
        db.session.expire_all()
        text = who_text_full()
    await update.effective_message.reply_text(text, parse_mode='HTML',
                                              reply_markup=main_menu_keyboard())


# =====================================================================
# Расписание — просмотр
# =====================================================================
def _schedule_week_view_data(user_id, week_start, offset):
    end_of_week = week_start + timedelta(days=6)
    today = date.today()
    text = "📅 <b>Моё расписание</b>\n"
    text += f"<i>{week_start.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}</i>\n"
    text += "━━━━━━━━━━━━━━━━━━━━"

    for i in range(7):
        d = week_start + timedelta(days=i)
        sch = DailySchedule.query.filter_by(user_id=user_id, work_date=d).first()
        is_today = (d == today)
        day_short = DAYS_RU_SHORT[i]
        if sch and sch.is_day_off:
            icon = "🏖"
        elif is_today:
            icon = "🔸"
        elif sch and sch.actual_start:
            icon = "🟢"
        elif sch:
            icon = "📌"
        else:
            icon = "▫️"

        text += "\n\n"
        header = f"{icon} <b>{day_short} {d.strftime('%d.%m')}</b>"
        if is_today:
            header += " · <i>сегодня</i>"
        text += header + "\n"

        if sch and sch.is_day_off:
            text += "🏖 Выходной\n"
            continue

        if sch:
            text += f"🕐 План: {sch.start_time}–{sch.end_time}\n"
            if sch.actual_start and sch.actual_end:
                eff = int((sch.efficiency or 1.0) * 100)
                text += f"✅ Факт: {sch.actual_start}–{sch.actual_end} · e% {eff}\n"
            if sch.tasks_text:
                text += "📝 <b>Задачи:</b>\n"
                for t in _task_lines(sch.tasks_text):
                    text += f"   • {esc(t)}\n"
            if sch.fact_text:
                text += "✅ <b>Сделано:</b>\n"
                for t in _task_lines(sch.fact_text):
                    text += f"   • {esc(t)}\n"
        else:
            text += "<i>Пусто</i>\n"

    text += "\n━━━━━━━━━━━━━━━━━━━━\n👇 Нажми на день ниже."

    kb = schedule_week_keyboard(week_start, user_id, offset)
    rows = list(kb.inline_keyboard)
    rows.append([InlineKeyboardButton("🖼 Картинкой", callback_data="mysch_image")])
    rows.append([InlineKeyboardButton("🗑 Удалить неделю",
                                      callback_data=f"mysch_clearweek_{week_start.isoformat()}")])
    return text, InlineKeyboardMarkup(rows)


def _schedule_day_view_data(user_id, d):
    sch = DailySchedule.query.filter_by(user_id=user_id, work_date=d).first()
    is_today = (d == date.today())
    day_name = DAYS_RU_FULL[d.weekday()]

    text = f"📅 <b>{day_name}, {d.strftime('%d.%m.%Y')}</b>"
    if is_today:
        text += " · 🔸 <i>сегодня</i>"
    text += "\n━━━━━━━━━━━━━━━━━━━━\n\n"

    if sch and sch.is_day_off:
        text += "🏖 <b>Выходной</b>\n"
    elif sch:
        text += f"🕐 План: <b>{sch.start_time}–{sch.end_time}</b>\n"
    else:
        text += "🕐 <i>План не задан</i>\n"

    text += "\n📝 <b>Задачи</b>\n"
    plan = _task_lines(sch.tasks_text) if sch and sch.tasks_text else []
    if plan:
        for t in plan:
            text += f"   • {esc(t)}\n"
    else:
        text += "   <i>не указаны</i>\n"

    text += "\n✅ <b>Факт</b>\n"
    if sch and sch.actual_start and sch.actual_end:
        eff = int((sch.efficiency or 1.0) * 100)
        text += f"🕐 {sch.actual_start}–{sch.actual_end} · e% <b>{eff}</b>\n"
        facts = _task_lines(sch.fact_text) if sch.fact_text else []
        if facts:
            text += "   <b>Сделано:</b>\n"
            for t in facts:
                text += f"   • {esc(t)}\n"
    else:
        text += "   <i>не заполнено</i>\n"

    return text, schedule_day_keyboard(d, sch)


async def cmd_my_schedule(update, context):
    tid = update.effective_user.id
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(tid)
        if not user:
            await update.effective_message.reply_text("❌ /start.")
            return
        offset = context.user_data.get('sched_week_offset', 0)
        today = date.today()
        week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        text, kb = _schedule_week_view_data(user.id, week_start, offset)
    await update.effective_message.reply_text(text, parse_mode='HTML', reply_markup=kb)


# =====================================================================
# Callbacks расписания
# =====================================================================
async def sched_week_callback(update, context):
    q = update.callback_query
    data = q.data
    if data == "sched_noop":
        await q.answer(); return
    try:
        offset = int(data.replace("sched_week_", ""))
    except ValueError:
        await q.answer(); return
    context.user_data['sched_week_offset'] = offset
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            await q.answer("Нет привязки", show_alert=True); return
        today = date.today()
        ws = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        text, kb = _schedule_week_view_data(user.id, ws, offset)
    await q.answer()
    try:
        await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await q.message.delete()
        except Exception:
            pass
        await q.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


async def sched_day_callback(update, context):
    q = update.callback_query
    ds = q.data.replace("sched_day_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            await q.answer("Нет привязки", show_alert=True); return
        text, kb = _schedule_day_view_data(user.id, d)
    await q.answer()
    try:
        await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await q.message.delete()
        except Exception:
            pass
        await q.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


async def sched_time_callback(update, context):
    q = update.callback_query
    ds = q.data.replace("sched_time_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    context.user_data['sched_edit_date'] = ds
    context.user_data['state'] = 'sched_edit_start'
    await q.answer()
    msg = (f"🕐 <b>Плановое время — {d.strftime('%d.%m.%Y')}</b>\n\n"
           f"Введи <b>время прихода</b> (<code>ЧЧ:ММ</code>)\nНапример: <code>10:00</code>")
    try:
        await q.edit_message_text(msg, parse_mode='HTML')
    except Exception:
        try:
            await q.message.delete()
        except Exception:
            pass
        await q.message.chat.send_message(msg, parse_mode='HTML')
    await q.message.chat.send_message("Жду время 👇", reply_markup=cancel_keyboard())


async def sched_tasks_callback(update, context):
    q = update.callback_query
    ds = q.data.replace("sched_tasks_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            await q.answer("Нет привязки", show_alert=True); return
        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
        current = _task_lines(sch.tasks_text) if sch and sch.tasks_text else []

    context.user_data['sched_edit_date'] = ds
    context.user_data['state'] = 'sched_edit_tasks'
    if current:
        text = f"📝 <b>Задачи на {d.strftime('%d.%m.%Y')}</b>\n\n"
        for i, t in enumerate(current, 1):
            text += f"{i}. {esc(t)}\n"
        text += "\n<b>Отправь новый список</b> или «-» для очистки."
    else:
        text = (f"📝 <b>Задачи на {d.strftime('%d.%m.%Y')}</b>\n\n"
                f"Отправь список — каждая строка с «-».\n\n"
                f"<b>Пример:</b>\n<code>- Созвон\n- Отчёт</code>")
    await q.answer()
    try:
        await q.edit_message_text(text, parse_mode='HTML')
    except Exception:
        pass
    await q.message.chat.send_message("Жду список 👇", reply_markup=cancel_keyboard())


async def sched_fact_callback(update, context):
    q = update.callback_query
    ds = q.data.replace("sched_fact_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            await q.answer("Нет привязки", show_alert=True); return
        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()

    context.user_data['sched_edit_date'] = ds
    context.user_data['state'] = 'sched_edit_fact_start'
    cur = ""
    if sch and sch.actual_start and sch.actual_end:
        eff = int((sch.efficiency or 1.0) * 100)
        cur = f"<b>Сейчас:</b> {sch.actual_start}–{sch.actual_end}, e% {eff}\n\n"
    text = (f"✅ <b>Факт — {d.strftime('%d.%m.%Y')}</b>\n\n{cur}"
            f"Введи <b>время прихода</b> (<code>ЧЧ:ММ</code>)")
    await q.answer()
    try:
        await q.edit_message_text(text, parse_mode='HTML')
    except Exception:
        pass
    await q.message.chat.send_message("Жду 👇", reply_markup=cancel_keyboard())


async def sched_dayoff_callback(update, context):
    q = update.callback_query
    ds = q.data.replace("sched_dayoff_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            await q.answer("Нет привязки", show_alert=True); return
        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
        if sch:
            sch.is_day_off = True
        else:
            sch = DailySchedule(user_id=user.id, work_date=d, is_day_off=True)
            db.session.add(sch)
        db.session.commit()
        text, kb = _schedule_day_view_data(user.id, d)
    await q.answer("🏖")
    try:
        await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        pass


async def sched_clear_callback(update, context):
    q = update.callback_query
    ds = q.data.replace("sched_clear_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да", callback_data=f"sched_clear_yes_{ds}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"sched_day_{ds}")],
    ])
    await q.answer()
    try:
        await q.edit_message_text(f"🗑 Очистить {d.strftime('%d.%m.%Y')}?",
                                  reply_markup=kb)
    except Exception:
        pass


async def sched_clear_yes_callback(update, context):
    q = update.callback_query
    ds = q.data.replace("sched_clear_yes_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            await q.answer("Нет привязки", show_alert=True); return
        DailySchedule.query.filter_by(user_id=user.id, work_date=d).delete()
        db.session.commit()
        text, kb = _schedule_day_view_data(user.id, d)
    await q.answer("🗑")
    try:
        await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        pass


async def mysch_clearweek_callback(update, context):
    q = update.callback_query
    raw = q.data.replace("mysch_clearweek_", "")
    try:
        ws = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    we = ws + timedelta(days=6)
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            await q.answer("Нет привязки", show_alert=True); return
        cnt = DailySchedule.query.filter(
            DailySchedule.user_id == user.id,
            DailySchedule.work_date >= ws,
            DailySchedule.work_date <= we).count()
    if cnt == 0:
        await q.answer("Пусто", show_alert=True); return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"mysch_clearweek_yes_{raw}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="sched_week_0")],
    ])
    await q.answer()
    try:
        await q.edit_message_text(
            f"🗑 Удалить всю неделю?\n\n📅 {ws.strftime('%d.%m.%Y')} – {we.strftime('%d.%m.%Y')}\n\nЗаписей: {cnt}",
            reply_markup=kb)
    except Exception:
        pass


async def mysch_clearweek_yes_callback(update, context):
    q = update.callback_query
    raw = q.data.replace("mysch_clearweek_yes_", "")
    try:
        ws = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        await q.answer(); return
    we = ws + timedelta(days=6)
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            await q.answer("Нет привязки", show_alert=True); return
        deleted = DailySchedule.query.filter(
            DailySchedule.user_id == user.id,
            DailySchedule.work_date >= ws,
            DailySchedule.work_date <= we).delete(synchronize_session=False)
        db.session.commit()
        offset = context.user_data.get('sched_week_offset', 0)
        today = date.today()
        cur_ws = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        text, kb = _schedule_week_view_data(user.id, cur_ws, offset)
    await q.answer(f"🗑 {deleted}")
    try:
        await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        pass


# =====================================================================
# Картинка моего расписания
# =====================================================================
def generate_my_week_image(user_id, offset=0):
    today = date.today()
    ws = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    we = ws + timedelta(days=6)
    user = User.query.get(user_id)

    img_width = 1120
    header_height = 165
    day_height = 175
    footer_height = 60
    img_height = header_height + day_height * 7 + footer_height

    img = Image.new('RGB', (img_width, img_height), '#f8fafc')
    draw = ImageDraw.Draw(img)
    f = get_fonts()

    draw.rectangle([0, 0, img_width, header_height], fill='#286c6c')
    draw.text((40, 25), 'МОЁ РАСПИСАНИЕ', fill='white', font=f['title'])
    draw.text((40, 85), f"{ws.strftime('%d.%m')} – {we.strftime('%d.%m.%Y')}", fill='#c7efef', font=f['header'])
    draw.text((40, 122), f"👤 {user.full_name if user else ''}", fill='#d7eeee', font=f['small'])

    for i in range(7):
        d = ws + timedelta(days=i)
        y = header_height + i * day_height
        sch = DailySchedule.query.filter_by(user_id=user_id, work_date=d).first()
        is_today = (d == today)
        bg = '#e3eeee' if is_today else ('#ffffff' if i % 2 == 0 else '#f9fafb')
        draw.rectangle([0, y, img_width, y + day_height], fill=bg)
        if is_today:
            draw.rectangle([0, y, img_width - 1, y + day_height - 1], outline='#286c6c', width=3)
        draw.line([(0, y), (img_width, y)], fill='#e5e7eb', width=1)

        draw.text((40, y + 22), DAYS_RU_FULL[d.weekday()], fill='#111827', font=f['day'])
        draw.text((40, y + 58), d.strftime('%d.%m.%Y'), fill='#6b7280', font=f['cell'])
        if is_today:
            draw.text((40, y + 90), 'СЕГОДНЯ', fill='#286c6c', font=f['small'])

        draw.line([(300, y + 20), (300, y + day_height - 20)], fill='#e5e7eb', width=2)
        info_x = 330
        line_y = y + 18

        if sch and sch.is_day_off:
            draw.text((info_x, line_y), "🏖 Выходной", fill='#6b7280', font=f['cell'])
            continue

        if sch:
            draw.text((info_x, line_y), f"🕐 План: {sch.start_time}–{sch.end_time}",
                      fill='#1e40af', font=f['cell'])
            line_y += 30
            if sch.actual_start and sch.actual_end:
                eff = int((sch.efficiency or 1.0) * 100)
                draw.text((info_x, line_y),
                          f"✅ Факт: {sch.actual_start}–{sch.actual_end} · e% {eff}",
                          fill='#047857', font=f['cell'])
                line_y += 30
            if sch.tasks_text:
                draw.text((info_x, line_y), f"📝 {_tasks_short(sch.tasks_text, 90)}",
                          fill='#286c6c', font=f['small'])
                line_y += 24
            if sch.fact_text:
                draw.text((info_x, line_y), f"✔ {_tasks_short(sch.fact_text, 90)}",
                          fill='#047857', font=f['small'])
        else:
            draw.text((info_x, line_y), "Пусто", fill='#9ca3af', font=f['cell'])

    draw.text((40, img_height - 32), f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
              fill='#9ca3af', font=f['small'])
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


# =====================================================================
# Картинка «Итоги всех сотрудников»
# =====================================================================
def generate_all_summary_image(offset_months=0):
    """Картинка с итогами всех сотрудников за месяц."""
    from app import Attendance  # локальный импорт, чтобы избежать циклов
    try:
        from app import calculate_worked_hours  # noqa
        _calc = calculate_worked_hours
    except ImportError:
        _calc = _fallback_worked_hours

    today = date.today()
    m = today.month + offset_months
    y = today.year
    while m < 1:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1
    first = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)

    users = User.query.filter_by(active=True).order_by(User.full_name).all()

    rows = []
    grand_worked = 0
    grand_eff_min = 0
    grand_late = 0

    for u in users:
        atts = Attendance.query.filter(
            Attendance.user_id == u.id,
            Attendance.date >= first,
            Attendance.date < end,
            Attendance.status != 'rejected'
        ).all()

        total_min = 0
        eff_min = 0
        eff_count = 0
        late_min = 0

        for a in atts:
            if a.actual_start and a.actual_end:
                w = _calc(a.actual_start, a.actual_end)
                wm = int(w.total_seconds() // 60)
                total_min += wm
                eff_min += int(wm * (a.efficiency or 1.0))
                eff_count += 1
            if getattr(a, 'early_start', None) and a.early_start > 0:
                late_min += a.early_start

        if total_min == 0 and late_min == 0:
            continue

        avg_eff = (eff_min / total_min * 100) if total_min > 0 else 0
        final_min = eff_min - late_min

        rows.append((u.full_name, total_min, avg_eff, late_min, final_min))
        grand_worked += total_min
        grand_eff_min += eff_min
        grand_late += late_min

    grand_avg = (grand_eff_min / grand_worked * 100) if grand_worked > 0 else 0
    grand_final = grand_eff_min - grand_late

    img_width = 1200
    header_height = 160
    table_header_height = 70
    row_height = 55
    footer_height = 110
    img_height = header_height + table_header_height + row_height * max(len(rows), 1) + footer_height

    img = Image.new('RGB', (img_width, img_height), '#f8fafc')
    draw = ImageDraw.Draw(img)
    f = get_fonts()

    # Заголовок
    draw.rectangle([0, 0, img_width, header_height], fill='#286c6c')
    draw.text((40, 30), 'ИТОГИ ВСЕХ СОТРУДНИКОВ', fill='white', font=f['title'])
    draw.text((40, 90), f"{MONTHS_RU[m-1]} {y}", fill='#c7efef', font=f['header'])

    y0 = header_height
    draw.rectangle([0, y0, img_width, y0 + table_header_height], fill='#e5e7eb')

    # Колонки
    col_x = [30, 500, 700, 900, 1050]
    headers = ['Сотрудник', 'Отработано', 'Ср. e%', 'Опоздания', 'Итого']
    for i, h in enumerate(headers):
        draw.text((col_x[i], y0 + 22), h, fill='#1f2937', font=f['header'])

    # Строки
    for i, (name, worked_min, avg, late_min, final_min) in enumerate(rows):
        y1 = y0 + table_header_height + i * row_height
        bg = '#ffffff' if i % 2 == 0 else '#f1f5f9'
        draw.rectangle([0, y1, img_width, y1 + row_height], fill=bg)

        name_short = name if len(name) <= 32 else name[:29] + '...'
        draw.text((col_x[0], y1 + 16), name_short, fill='#1f2937', font=f['cell'])

        wh = worked_min // 60
        wm = worked_min % 60
        draw.text((col_x[1], y1 + 16), f"{wh}ч {wm:02d}м", fill='#4f46e5', font=f['cell'])

        eff_color = '#10b981' if avg >= 85 else ('#e9a36b' if avg >= 60 else '#e88e8e')
        draw.text((col_x[2], y1 + 16), f"{avg:.0f}%", fill=eff_color, font=f['cell'])

        lh = late_min // 60
        lm = late_min % 60
        late_color = '#e88e8e' if late_min > 0 else '#9ca3af'
        draw.text((col_x[3], y1 + 16), f"{lh}ч {lm:02d}м", fill=late_color, font=f['cell'])

        fh = final_min // 60
        fm = final_min % 60
        draw.text((col_x[4], y1 + 16), f"{fh}ч {fm:02d}м", fill='#047857', font=f['cell'])

    if not rows:
        draw.text((30, y0 + table_header_height + 30),
                  'За этот месяц нет данных',
                  fill='#6b7280', font=f['header'])

    # Итоговая полоса
    y_footer = y0 + table_header_height + row_height * max(len(rows), 1)
    draw.rectangle([0, y_footer, img_width, y_footer + row_height], fill='#286c6c')

    gw_h = grand_worked // 60; gw_m = grand_worked % 60
    gl_h = grand_late // 60; gl_m = grand_late % 60
    gf_h = grand_final // 60; gf_m = grand_final % 60

    draw.text((col_x[0], y_footer + 18), 'ИТОГО:', fill='white', font=f['header'])
    draw.text((col_x[1], y_footer + 18), f"{gw_h}ч {gw_m:02d}м", fill='#c7efef', font=f['cell'])
    draw.text((col_x[2], y_footer + 18), f"{grand_avg:.0f}%", fill='#c7efef', font=f['cell'])
    draw.text((col_x[3], y_footer + 18), f"{gl_h}ч {gl_m:02d}м", fill='#fca5a5', font=f['cell'])
    draw.text((col_x[4], y_footer + 18), f"{gf_h}ч {gf_m:02d}м", fill='#86efac', font=f['cell'])

    draw.text((30, img_height - 40),
              f'Сформировано: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
              fill='#6b7280', font=f['small'])

    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


async def mysch_view_callback(update, context):
    q = update.callback_query
    await q.answer()
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            return
        offset = context.user_data.get('sched_week_offset', 0)
        today = date.today()
        ws = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        we = ws + timedelta(days=6)
        if q.data == "mysch_image":
            try:
                buf = generate_my_week_image(user.id, offset)
            except Exception as e:
                logger.exception(f"Image error: {e}"); return
            try:
                await q.message.delete()
            except Exception:
                pass
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️", callback_data="mysch_img_prev"),
                 InlineKeyboardButton("Текущая", callback_data="mysch_img_now"),
                 InlineKeyboardButton("➡️", callback_data="mysch_img_next")],
                [InlineKeyboardButton("📄 Текстом", callback_data="mysch_text")],
            ])
            await q.message.chat.send_photo(
                photo=buf,
                caption=f"📅 <b>Моё расписание</b>\n{ws.strftime('%d.%m')} – {we.strftime('%d.%m.%Y')}",
                parse_mode='HTML', reply_markup=kb)
        elif q.data == "mysch_text":
            text, kb = _schedule_week_view_data(user.id, ws, offset)
            try:
                await q.message.delete()
            except Exception:
                pass
            await q.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


async def mysch_img_nav_callback(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    offset = context.user_data.get('sched_week_offset', 0)
    if data.endswith('prev'):
        offset -= 1
    elif data.endswith('next'):
        offset += 1
    else:
        offset = 0
    context.user_data['sched_week_offset'] = offset
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(q.from_user.id)
        if not user:
            return
        today = date.today()
        ws = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        we = ws + timedelta(days=6)
        try:
            buf = generate_my_week_image(user.id, offset)
        except Exception:
            return
    try:
        await q.message.delete()
    except Exception:
        pass
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️", callback_data="mysch_img_prev"),
         InlineKeyboardButton("Текущая", callback_data="mysch_img_now"),
         InlineKeyboardButton("➡️", callback_data="mysch_img_next")],
        [InlineKeyboardButton("📄 Текстом", callback_data="mysch_text")],
    ])
    await q.message.chat.send_photo(
        photo=buf,
        caption=f"📅 <b>Моё расписание</b>\n{ws.strftime('%d.%m')} – {we.strftime('%d.%m.%Y')}",
        parse_mode='HTML', reply_markup=kb)


# =====================================================================
# Расписание всех сотрудников картинкой
# =====================================================================
def generate_all_week_image(offset=0):
    today = date.today()
    ws = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    we = ws + timedelta(days=6)

    users = User.query.filter_by(active=True).order_by(User.full_name).all()
    rows = []
    for emp in users:
        emp_row = []
        has_any = False
        total_minutes = 0
        for i in range(7):
            day = ws + timedelta(days=i)
            sch = DailySchedule.query.filter_by(user_id=emp.id, work_date=day).first()
            if sch and sch.is_day_off:
                emp_row.append('Вых')
                has_any = True
            elif sch:
                emp_row.append(f"{sch.start_time[:5]}–{sch.end_time[:5]}")
                has_any = True
                try:
                    ps = sch.start_time.split(':'); pe = sch.end_time.split(':')
                    total_minutes += (int(pe[0])*60+int(pe[1])) - (int(ps[0])*60+int(ps[1]))
                except Exception:
                    pass
            else:
                emp_row.append('—')
        if has_any:
            rows.append((emp.full_name, emp_row, total_minutes))

    img_width = 1500
    header_height = 150
    table_header_height = 90
    row_height = 55
    footer_height = 80
    img_height = header_height + table_header_height + row_height * max(len(rows), 1) + footer_height

    img = Image.new('RGB', (img_width, img_height), '#f8fafc')
    draw = ImageDraw.Draw(img)
    f = get_fonts()

    draw.rectangle([0, 0, img_width, header_height], fill='#286c6c')
    draw.text((30, 30), 'РАСПИСАНИЕ НА НЕДЕЛЮ', fill='white', font=f['title'])
    draw.text((30, 85), f"{ws.strftime('%d.%m')} – {we.strftime('%d.%m.%Y')}", fill='#c7efef', font=f['header'])

    y0 = header_height
    draw.rectangle([0, y0, img_width, y0 + table_header_height], fill='#e5e7eb')
    name_col_width = 230
    total_col_width = 130
    day_col_width = (img_width - name_col_width - total_col_width - 40) / 7

    draw.text((30, y0 + 30), 'Сотрудник', fill='#1f2937', font=f['header'])

    for i in range(7):
        day_x = name_col_width + 30 + int(i * day_col_width)
        d = ws + timedelta(days=i)
        draw.text((day_x + 20, y0 + 15), DAYS_RU_SHORT[i], fill='#1f2937', font=f['header'])
        draw.text((day_x + 10, y0 + 50), d.strftime('%d.%m'), fill='#6b7280', font=f['small'])

    draw.text((img_width - total_col_width + 15, y0 + 30), 'ИТОГО', fill='#1f2937', font=f['header'])

    for i, (name, emp_row, total_min) in enumerate(rows):
        y1 = y0 + table_header_height + i * row_height
        bg = '#ffffff' if i % 2 == 0 else '#f1f5f9'
        draw.rectangle([0, y1, img_width, y1 + row_height], fill=bg)
        name_short = name if len(name) <= 22 else name[:20] + '...'
        draw.text((30, y1 + 16), name_short, fill='#1f2937', font=f['cell'])
        for j, cell in enumerate(emp_row):
            cell_x = name_col_width + 30 + int(j * day_col_width)
            if cell == 'Вых':
                draw.text((cell_x + 10, y1 + 16), 'Вых', fill='#6b7280', font=f['cell'])
            elif cell == '—':
                draw.text((cell_x + 10, y1 + 16), '—', fill='#9ca3af', font=f['cell'])
            else:
                draw.text((cell_x + 5, y1 + 16), cell, fill='#286c6c', font=f['cell'])

        total_h = total_min // 60
        total_m = total_min % 60
        total_x = img_width - total_col_width + 15
        draw.text((total_x, y1 + 16), f"{total_h}ч {total_m:02d}м", fill='#10b981', font=f['cell'])

    if not rows:
        draw.text((30, y0 + table_header_height + 30),
                  'Пока никто не составил расписание.',
                  fill='#6b7280', font=f['header'])

    draw.text((30, img_height - 40),
              f'Сформировано: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
              fill='#6b7280', font=f['small'])

    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


async def cmd_all_week(update, context):
    try:
        offset = context.user_data.get('all_week_offset', 0)
        with app.app_context():
            db.session.expire_all()
            buf = generate_all_week_image(offset)
        today = date.today()
        ws = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️", callback_data="allweek_prev"),
             InlineKeyboardButton("Текущая", callback_data="allweek_now"),
             InlineKeyboardButton("➡️", callback_data="allweek_next")]
        ])
        await update.effective_message.reply_photo(
            photo=buf,
            caption=f"👥 <b>Расписание всех сотрудников</b>\nНеделя с {ws.strftime('%d.%m.%Y')}",
            parse_mode='HTML', reply_markup=kb)
    except Exception as e:
        logger.exception(f"cmd_all_week: {e}")
        await update.effective_message.reply_text("⚠️ Не удалось сформировать картинку.",
                                                  reply_markup=main_menu_keyboard())


async def all_week_callback(update, context):
    q = update.callback_query
    await q.answer()
    try:
        if q.data == "allweek_prev":
            context.user_data['all_week_offset'] = context.user_data.get('all_week_offset', 0) - 1
        elif q.data == "allweek_next":
            context.user_data['all_week_offset'] = context.user_data.get('all_week_offset', 0) + 1
        else:
            context.user_data['all_week_offset'] = 0
        offset = context.user_data.get('all_week_offset', 0)
        with app.app_context():
            db.session.expire_all()
            buf = generate_all_week_image(offset)
        today = date.today()
        ws = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️", callback_data="allweek_prev"),
             InlineKeyboardButton("Текущая", callback_data="allweek_now"),
             InlineKeyboardButton("➡️", callback_data="allweek_next")]
        ])
        await q.message.reply_photo(
            photo=buf,
            caption=f"👥 <b>Расписание всех</b>\nНеделя с {ws.strftime('%d.%m.%Y')}",
            parse_mode='HTML', reply_markup=kb)
    except Exception as e:
        logger.exception(f"all_week: {e}")


# =====================================================================
# Итоги
# =====================================================================
def summary_text(user_id, offset_months=0):
    user = User.query.get(user_id)
    today = date.today()
    m = today.month + offset_months
    y = today.year
    while m < 1: m += 12; y -= 1
    while m > 12: m -= 12; y += 1
    first = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)

    schedules = DailySchedule.query.filter(
        DailySchedule.user_id == user_id,
        DailySchedule.work_date >= first,
        DailySchedule.work_date < end).all()

    planned_min = 0
    actual_min = 0
    for s in schedules:
        if s.is_day_off:
            continue
        try:
            ps = s.start_time.split(':'); pe = s.end_time.split(':')
            planned_min += (int(pe[0]) * 60 + int(pe[1])) - (int(ps[0]) * 60 + int(ps[1]))
        except Exception:
            pass
        if s.actual_start and s.actual_end:
            try:
                a_s = s.actual_start.split(':'); a_e = s.actual_end.split(':')
                actual_min += (int(a_e[0]) * 60 + int(a_e[1])) - (int(a_s[0]) * 60 + int(a_s[1]))
            except Exception:
                pass

    return (f"📊 <b>Мои итоги за {MONTHS_RU[m-1]} {y}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 {esc(user.full_name)}\n\n"
            f"🕐 Часов по плану: <b>{planned_min / 60:.2f}</b>\n"
            f"✅ Часов по факту: <b>{actual_min / 60:.2f}</b>\n"
            f"📅 Рабочих дней: <b>{sum(1 for s in schedules if not s.is_day_off)}</b>")


async def cmd_summary(update, context):
    chat_id = update.effective_chat.id
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(chat_id)
        if not user:
            await update.effective_message.reply_text("❌ /start.")
            return
        text = summary_text(user.id, 0)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
         InlineKeyboardButton("Текущий", callback_data="sum_now"),
         InlineKeyboardButton("Следующий ➡️", callback_data="sum_next")],
        [InlineKeyboardButton("👥 Итоги всех (картинкой)", callback_data="sum_all")],
    ])
    await update.effective_message.reply_text(text, parse_mode='HTML', reply_markup=kb)


async def summary_callback(update, context):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    data = q.data

    if data in ("sum_prev", "allsum_prev"):
        context.user_data['summary_offset'] = context.user_data.get('summary_offset', 0) - 1
    elif data in ("sum_next", "allsum_next"):
        context.user_data['summary_offset'] = context.user_data.get('summary_offset', 0) + 1
    elif data in ("sum_now", "allsum_now"):
        context.user_data['summary_offset'] = 0

    offset = context.user_data.get('summary_offset', 0)

    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(chat_id)
        if not user:
            return

        text = summary_text(user.id, offset)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
         InlineKeyboardButton("Текущий", callback_data="sum_now"),
         InlineKeyboardButton("Следующий ➡️", callback_data="sum_next")],
        [InlineKeyboardButton("👥 Итоги всех (картинкой)", callback_data="sum_all")],
    ])
    try:
        await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await q.message.reply_text(text, parse_mode='HTML', reply_markup=kb)
        except Exception:
            pass


async def summary_choice_callback(update, context):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    context.user_data['summary_offset'] = 0

    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(chat_id)
        if not user:
            await q.edit_message_text("❌ Аккаунт не привязан.")
            return

        if q.data == "sum_my":
            text = summary_text(user.id, 0)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
                 InlineKeyboardButton("Текущий", callback_data="sum_now"),
                 InlineKeyboardButton("Следующий ➡️", callback_data="sum_next")],
                [InlineKeyboardButton("👥 Итоги всех (картинкой)", callback_data="sum_all")],
            ])
            await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        else:
            try:
                buf = generate_all_summary_image(0)
            except Exception as e:
                logger.exception(f"summary image error: {e}")
                await q.edit_message_text("⚠️ Не удалось сформировать картинку.")
                return

            today = date.today()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
                 InlineKeyboardButton("Текущий", callback_data="sum_now"),
                 InlineKeyboardButton("Следующий ➡️", callback_data="sum_next")],
                [InlineKeyboardButton("👤 Мои итоги", callback_data="sum_my")],
            ])
            await q.message.reply_photo(
                photo=buf,
                caption=f"📊 <b>Итоги всех за {MONTHS_RU[today.month-1]} {today.year}</b>",
                parse_mode='HTML',
                reply_markup=kb
            )


async def summary_all_callback(update, context):
    """Показ картинки со всеми итогами (используется кнопкой sum_all)."""
    q = update.callback_query
    await q.answer()
    offset = context.user_data.get('summary_offset', 0)
    with app.app_context():
        db.session.expire_all()
        try:
            buf = generate_all_summary_image(offset)
        except Exception as e:
            logger.exception(f"summary image error: {e}")
            await q.message.reply_text("⚠️ Не удалось сформировать картинку.")
            return

    today = date.today()
    m = today.month + offset
    y = today.year
    while m < 1:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
         InlineKeyboardButton("Текущий", callback_data="sum_now"),
         InlineKeyboardButton("Следующий ➡️", callback_data="sum_next")],
        [InlineKeyboardButton("👤 Мои итоги", callback_data="sum_my")],
    ])
    await q.message.reply_photo(
        photo=buf,
        caption=f"📊 <b>Итоги всех за {MONTHS_RU[m-1]} {y}</b>",
        parse_mode='HTML',
        reply_markup=kb
    )


# =====================================================================
# Мои заявки (все активные)
# =====================================================================
async def cmd_my_absences(update, context):
    tid = update.effective_user.id
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(tid)
        if not user:
            await update.effective_message.reply_text("❌ /start.")
            return
        absences = AbsenceRequest.query.filter(
            AbsenceRequest.user_id == user.id,
        ).order_by(AbsenceRequest.date_start.desc()).limit(20).all()

    if not absences:
        await update.effective_message.reply_text("📂 У тебя пока нет заявок.",
                                                  reply_markup=main_menu_keyboard())
        return

    text = "📂 <b>Мои заявки</b>\n━━━━━━━━━━━━━━━━━━━━"
    for a in absences:
        type_ru = {'vacation': '🏖 Отпуск', 'sick_leave': '🤒 Больничный',
                   'day_off': '📅 Выходной'}.get(a.absence_type, a.absence_type)
        status = {'pending': '⏳ На согласовании',
                  'approved': '✅ Согласовано',
                  'rejected': '❌ Отклонено'}.get(a.status, a.status)
        end = a.date_end.strftime('%d.%m.%Y') if a.date_end else 'до выздоровления'
        text += f"\n\n{type_ru}\n📅 {a.date_start.strftime('%d.%m.%Y')} → {end}\n{status}"

    await update.effective_message.reply_text(text, parse_mode='HTML',
                                              reply_markup=main_menu_keyboard())


# =====================================================================
# Обработка состояний (ввод)
# =====================================================================
async def sched_edit_start_handler(update, context):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        context.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard()); return
    try:
        h, m = text.split(':')
        if len(h) == 1: h = '0' + h
        st = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как <code>10:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard()); return
    context.user_data['sched_edit_time_start'] = st.strftime('%H:%M')
    context.user_data['state'] = 'sched_edit_end'
    await update.message.reply_text(
        f"🕐 Приход: <b>{st.strftime('%H:%M')}</b>\n\nВведи <b>время ухода</b>:",
        parse_mode='HTML', reply_markup=cancel_keyboard())


async def sched_edit_end_handler(update, context):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        context.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard()); return
    try:
        h, m = text.split(':')
        if len(h) == 1: h = '0' + h
        et = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как <code>18:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard()); return
    st = parse_time_string(context.user_data.get('sched_edit_time_start'))
    if st >= et:
        await update.message.reply_text("❌ Время ухода позже прихода.",
                                        reply_markup=cancel_keyboard()); return
    ds = context.user_data.get('sched_edit_date')
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(update.effective_user.id)
        if not user:
            return
        d = datetime.strptime(ds, '%Y-%m-%d').date()
        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
        if sch:
            sch.start_time = st.strftime('%H:%M')
            sch.end_time = et.strftime('%H:%M')
            sch.is_day_off = False
        else:
            sch = DailySchedule(user_id=user.id, work_date=d,
                                start_time=st.strftime('%H:%M'),
                                end_time=et.strftime('%H:%M'),
                                is_day_off=False)
            db.session.add(sch)
        db.session.commit()
        text_view, kb = _schedule_day_view_data(user.id, d)
    context.user_data.clear()
    await update.message.reply_text(f"✅ Сохранено: <b>{st.strftime('%H:%M')}–{et.strftime('%H:%M')}</b>",
                                    parse_mode='HTML')
    await update.message.reply_text(text_view, parse_mode='HTML', reply_markup=kb)


async def sched_edit_tasks_handler(update, context):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        context.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard()); return
    ds = context.user_data.get('sched_edit_date')
    if ds is None:
        context.user_data.clear()
        await update.message.reply_text("❌ Сессия устарела.", reply_markup=main_menu_keyboard()); return
    if text == '-':
        new_lines = []
    else:
        new_lines = _norm_task_lines(text)
        if not new_lines:
            await update.message.reply_text("❌ Пусто.", reply_markup=cancel_keyboard()); return
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(update.effective_user.id)
        if not user:
            return
        d = datetime.strptime(ds, '%Y-%m-%d').date()
        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
        if not sch:
            sch = DailySchedule(user_id=user.id, work_date=d)
            db.session.add(sch)
        sch.tasks_text = '\n'.join(new_lines) if new_lines else ''
        db.session.commit()
        text_view, kb = _schedule_day_view_data(user.id, d)
    context.user_data.clear()
    await update.message.reply_text("✅ Сохранено." if new_lines else "✅ Очищено.")
    await update.message.reply_text(text_view, parse_mode='HTML', reply_markup=kb)


async def sched_edit_fact_start_handler(update, context):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        context.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard()); return
    try:
        h, m = text.split(':')
        if len(h) == 1: h = '0' + h
        st = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ <code>10:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard()); return
    context.user_data['sched_edit_fact_start'] = st.strftime('%H:%M')
    context.user_data['state'] = 'sched_edit_fact_end'
    await update.message.reply_text(
        f"✅ Приход: <b>{st.strftime('%H:%M')}</b>\n\nВведи <b>время ухода</b>:",
        parse_mode='HTML', reply_markup=cancel_keyboard())


async def sched_edit_fact_end_handler(update, context):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        context.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard()); return
    try:
        h, m = text.split(':')
        if len(h) == 1: h = '0' + h
        et = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ <code>18:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard()); return
    st = parse_time_string(context.user_data.get('sched_edit_fact_start'))
    if st >= et:
        await update.message.reply_text("❌ Время ухода позже прихода.",
                                        reply_markup=cancel_keyboard()); return
    context.user_data['sched_edit_fact_end'] = et.strftime('%H:%M')
    context.user_data['state'] = 'sched_edit_eff'
    await update.message.reply_text(
        f"🕕 Уход: <b>{et.strftime('%H:%M')}</b>\n\n"
        f"📈 Введи <b>эффективность</b> 0–100\n<i>Например: 85</i>",
        parse_mode='HTML', reply_markup=cancel_keyboard())


async def sched_edit_eff_handler(update, context):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        context.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard()); return
    try:
        eff = int(text)
        if not (0 <= eff <= 100): raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Число 0–100.", reply_markup=cancel_keyboard()); return
    ds = context.user_data.get('sched_edit_date')
    st = context.user_data.get('sched_edit_fact_start')
    et = context.user_data.get('sched_edit_fact_end')
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(update.effective_user.id)
        if not user:
            return
        d = datetime.strptime(ds, '%Y-%m-%d').date()
        sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
        if not sch:
            sch = DailySchedule(user_id=user.id, work_date=d)
            db.session.add(sch)
        sch.actual_start = st
        sch.actual_end = et
        sch.efficiency = eff / 100.0
        db.session.commit()
        text_view, kb = _schedule_day_view_data(user.id, d)
    context.user_data.clear()
    await update.message.reply_text(f"✅ Сохранено: <b>{st}–{et}</b>, e% <b>{eff}</b>",
                                    parse_mode='HTML')
    await update.message.reply_text(text_view, parse_mode='HTML', reply_markup=kb)


# =====================================================================
# Хэштеги в группе — отвечаем в личку
# =====================================================================
async def handle_hashtag_message(update, context):
    msg = update.message
    chat = update.effective_chat
    if not msg or not msg.text:
        return
    if chat.type not in ('group', 'supergroup'):
        return
    text = msg.text
    if '#расписание' not in text and '#дейли' not in text:
        return

    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(update.effective_user.id)
        if not user:
            return

        if '#расписание' in text:
            week_start = date.today() - timedelta(days=date.today().weekday())
            day_map = {'ПН': 0, 'ВТ': 1, 'СР': 2, 'ЧТ': 3, 'ПТ': 4, 'СБ': 5, 'ВС': 6}

            # Парсим блоки по дням: время + список задач
            day_data = {}          # day_code -> {'start','end','tasks':[],'day_off'}
            current_day = None

            for raw_line in text.split('\n'):
                s = raw_line.strip()
                if not s:
                    continue

                # Заголовок дня с временем: "ПН: 11:00 - 18:00"
                m = re.match(
                    r'^(ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s*:\s*(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\s*(.*)$',
                    s, re.IGNORECASE)
                if m:
                    day_code = m.group(1).upper()
                    if day_code not in day_map:
                        continue
                    st = m.group(2)
                    en = m.group(3)
                    if len(st.split(':')[0]) == 1:
                        st = '0' + st
                    if len(en.split(':')[0]) == 1:
                        en = '0' + en
                    day_data[day_code] = {'start': st, 'end': en, 'tasks': [], 'day_off': False}
                    current_day = day_code
                    tail = m.group(4).strip().lstrip('-').strip()
                    if tail:
                        day_data[day_code]['tasks'].append(tail)
                    continue

                # День без времени (выходной): "СР: ——————"
                m2 = re.match(r'^(ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s*:\s*[-—–_]*\s*$', s, re.IGNORECASE)
                if m2:
                    day_code = m2.group(1).upper()
                    if day_code in day_map:
                        day_data[day_code] = {'start': '09:00', 'end': '18:00',
                                              'tasks': [], 'day_off': True}
                        current_day = day_code
                    continue

                # Строка задачи: "-Что-то"
                if current_day and current_day in day_data:
                    task_text = s.lstrip('-').strip()
                    if task_text:
                        day_data[current_day]['tasks'].append(task_text)
                    continue

            saved_days = 0
            saved_tasks = 0

            for day_code, info in day_data.items():
                target = week_start + timedelta(days=day_map[day_code])
                sch = DailySchedule.query.filter_by(user_id=user.id, work_date=target).first()
                if not sch:
                    sch = DailySchedule(user_id=user.id, work_date=target)
                    db.session.add(sch)

                sch.start_time = info['start']
                sch.end_time = info['end']
                sch.is_day_off = info['day_off']

                if info['tasks']:
                    sch.tasks_text = '\n'.join(f"- {t}" for t in info['tasks'])
                    saved_tasks += len(info['tasks'])

                db.session.flush()

                # Раскладываем задачи в чек-лист DayTask (если ещё нет)
                if info['tasks']:
                    existing = {t.text.strip().lower()
                                for t in DayTask.query.filter_by(
                                    user_id=user.id, work_date=target).all()}
                    pos = DayTask.query.filter_by(
                        user_id=user.id, work_date=target).count()
                    for t_text in info['tasks']:
                        if t_text.strip().lower() in existing:
                            continue
                        pos += 1
                        db.session.add(DayTask(
                            user_id=user.id,
                            work_date=target,
                            text=t_text[:500],
                            done=False,
                            position=pos,
                        ))

                saved_days += 1

            db.session.commit()

            if saved_days == 0 and _AI_PARSER:
                # Fallback через ИИ, если не удалось распарсить структуру
                try:
                    clean_text = text.replace('#расписание', '').strip()
                    parsed = await _AI_PARSER.analyze(text=clean_text, user_full_name=user.full_name)
                    if parsed.get('action') in ('plan', 'fact'):
                        result = await save_ai_result(user, parsed, context)
                        await _reply_personal(context.bot, user,
                                              f"✅ Расписание разобрано:\n{result}")
                        return
                except Exception as e:
                    logger.error(f"AI plan in group: {e}")
                await _reply_personal(context.bot, user,
                                      "⚠️ Не удалось разобрать расписание. Напиши в личку боту.")
                return

            if saved_days == 0:
                await _reply_personal(context.bot, user,
                                      "⚠️ Не удалось разобрать расписание. Проверь формат.")
                return

            tail_msg = f", задач: {saved_tasks}" if saved_tasks else ""
            await _reply_personal(
                context.bot, user,
                f"✅ Сохранено дней: {saved_days}{tail_msg}\n\n"
                f"Открой «📅 Моё расписание» для проверки.")
            return

        if '#дейли' in text:
            dm = re.search(r'(\d{2}\.\d{2}\.\d{4})', text)
            tm = re.search(r'(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})', text)
            em = re.search(r'[Ээ]ффективность\s*:?\s*(\d+)', text)
            if not dm or not tm:
                await _reply_personal(context.bot, user,
                                      "⚠️ Не удалось разобрать #дейли. Проверь формат.")
                return
            try:
                d = datetime.strptime(dm.group(1), '%d.%m.%Y').date()
            except ValueError:
                return
            st, en = tm.group(1), tm.group(2)
            eff = int(em.group(1)) if em else None
            tasks = [l.strip() for l in text.split('\n') if l.strip().startswith('-')]

            sch = DailySchedule.query.filter_by(user_id=user.id, work_date=d).first()
            if not sch:
                sch = DailySchedule(user_id=user.id, work_date=d)
                db.session.add(sch)
            sch.actual_start = st
            sch.actual_end = en
            if eff is not None:
                sch.efficiency = max(0.0, min(1.0, eff / 100.0))
            if tasks:
                sch.fact_text = '\n'.join(tasks)
            db.session.commit()

            msg_txt = (f"✅ Дейли за {d.strftime('%d.%m.%Y')} сохранён:\n"
                       f"🕐 {st}–{en}" + (f" · e% {eff}" if eff is not None else ""))
            if tasks:
                msg_txt += f"\n📝 Задач: {len(tasks)}"
            await _reply_personal(context.bot, user, msg_txt)
            return

# =====================================================================
# Reply-меню
# =====================================================================
async def text_router(update, context):
    text = update.message.text or ''
    chat = update.effective_chat
    tid = update.effective_user.id

    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(tid)

    if not user:
        if chat.type in ('group', 'supergroup'):
            return
        await update.effective_message.reply_text(
            "❌ Сначала привяжи аккаунт: <code>/link ЛОГИН КОД</code>",
            parse_mode='HTML')
        return

    state = context.user_data.get('state')
    if state in ('sched_edit_start', 'sched_edit_end',
                 'sched_edit_tasks',
                 'sched_edit_fact_start', 'sched_edit_fact_end', 'sched_edit_eff'):
        if text == CANCEL_TEXT:
            context.user_data.clear()
            await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard()); return
        handlers = {
            'sched_edit_start': sched_edit_start_handler,
            'sched_edit_end': sched_edit_end_handler,
            'sched_edit_tasks': sched_edit_tasks_handler,
            'sched_edit_fact_start': sched_edit_fact_start_handler,
            'sched_edit_fact_end': sched_edit_fact_end_handler,
            'sched_edit_eff': sched_edit_eff_handler,
        }
        await handlers[state](update, context)
        return

    if text == CANCEL_TEXT:
        context.user_data.clear()
        await update.message.reply_text("Главное меню 👇", reply_markup=main_menu_keyboard())
        return

    # Reply-меню
    if 'Моё расписание' in text:
        await cmd_my_schedule(update, context); return
    if 'Мой профиль' in text:
        await cmd_profile(update, context); return
    if 'Кто работает' in text:
        await cmd_who(update, context); return
    if 'Итоги месяца' in text:
        await cmd_summary(update, context); return
    if 'Расписание всех' in text:
        await cmd_all_week(update, context); return
    if 'Мои заявки' in text:
        await cmd_my_absences(update, context); return
    if 'Помощь' in text:
        await help_cmd(update, context); return
    if 'Заявка на отпуск' in text:
        await update.message.reply_text(
            "🏖 Отправь одним сообщением:\n"
            "<code>отпуск 2026-11-02 2026-11-06</code>\n"
            "или\n"
            "<code>больничный 2026-11-02</code> (без даты окончания)",
            parse_mode='HTML', reply_markup=main_menu_keyboard())
        return
    if 'Фактическое время' in text:
        await update.message.reply_text("📅 Открой «📅 Моё расписание» → выбери день.",
                                        reply_markup=main_menu_keyboard())
        return
    if 'Отвязать аккаунт' in text:
        await unlink_cmd(update, context); return

    # ИИ-разбор
    handled = await ai_message_handler(update, context)
    if handled:
        return

    await update.message.reply_text(
        "🤔 Не понимаю. Используй кнопки 👇\n"
        "Или напиши свободно: «сегодня работал с 10 до 18».",
        reply_markup=main_menu_keyboard())


# =====================================================================
# Обработка leave-заявок текстом
# =====================================================================
async def handle_leave_request(update, context):
    raw = (update.effective_message.text or '').strip()
    m = re.match(r'(отпуск|больничный|выходной)\s+(\d{4}-\d{2}-\d{2})(?:\s+(\d{4}-\d{2}-\d{2}))?',
                 raw, re.I)
    if not m:
        return False
    with app.app_context():
        db.session.expire_all()
        user = user_by_tg(update.effective_user.id)
        if not user:
            if update.effective_chat.type == 'private':
                await update.effective_message.reply_text('Сначала /link ЛОГИН КОД')
            return True
        kind = {'отпуск': 'vacation', 'больничный': 'sick_leave', 'выходной': 'day_off'}[m.group(1).lower()]
        try:
            start = date.fromisoformat(m.group(2))
            end = date.fromisoformat(m.group(3)) if m.group(3) else None
        except ValueError:
            await update.effective_message.reply_text('Формат: ГГГГ-ММ-ДД.')
            return True
        if end and end < start:
            await update.effective_message.reply_text('Дата окончания раньше начала.')
            return True
        if kind != 'sick_leave' and not end:
            await update.effective_message.reply_text('Для отпуска/выходного укажи дату окончания.')
            return True
        db.session.add(AbsenceRequest(
            user_id=user.id, absence_type=kind,
            date_start=start, date_end=end,
            reason='Создано через Telegram', status='pending'))
        db.session.commit()
    tail = f" до {end.strftime('%d.%m.%Y')}" if end else " (до выздоровления)"
    msg = f"✅ Заявка на {m.group(1)} с {start.strftime('%d.%m.%Y')}{tail} отправлена."
    if update.effective_chat.type in ('group', 'supergroup'):
        await _reply_personal(context.bot, user, msg)
    else:
        await update.effective_message.reply_text(msg)
    return True


# =====================================================================
# Напоминания
# =====================================================================
async def morning_reminder(context):
    with app.app_context():
        db.session.expire_all()
        today = date.today()
        if today.weekday() >= 5:
            return
        users = User.query.filter_by(active=True).all()
        schedules = {x.user_id: x for x in DailySchedule.query.filter_by(work_date=today).all()}
        absences = AbsenceRequest.query.filter(
            AbsenceRequest.status == 'approved',
            AbsenceRequest.date_start <= today).all()
        absent = {a.user_id: a for a in absences if a.date_end is None or a.date_end >= today}

        working = []
        for u in users:
            if u.id in absent:
                continue
            sch = schedules.get(u.id)
            if sch and not sch.is_day_off:
                working.append(f"• {u.full_name}: {sch.start_time}–{sch.end_time}")

        text_who = "☀️ <b>Доброе утро!</b>\n\n"
        if working:
            text_who += "👥 <b>Сегодня работают:</b>\n\n" + "\n".join(working)
        else:
            text_who += "Сегодня никто не работает."

        sent = 0
        for u in users:
            if not u.telegram_id:
                continue
            sch = schedules.get(u.id)
            rem = ""
            if not sch and u.id not in absent:
                rem = "\n\n📅 Не забудь заполнить график: «📅 Моё расписание»."
            try:
                await context.bot.send_message(chat_id=int(u.telegram_id),
                                               text=text_who + rem,
                                               parse_mode='HTML')
                sent += 1
            except Exception as e:
                logger.error(f"morning {u.id}: {e}")
        logger.info(f"Утренняя рассылка: отправлено {sent}")


async def evening_reminder(context):
    with app.app_context():
        db.session.expire_all()
        today = date.today()
        if today.weekday() >= 5:
            return
        users = User.query.filter_by(active=True).all()
        sent = 0
        for u in users:
            if not u.telegram_id:
                continue
            sch = DailySchedule.query.filter_by(user_id=u.id, work_date=today).first()
            if not sch or sch.is_day_off:
                continue
            if sch.actual_start and sch.actual_end:
                continue
            try:
                await context.bot.send_message(
                    chat_id=int(u.telegram_id),
                    text=(f"🌆 Конец дня.\n\nЗаполни факт: "
                          f"«📅 Моё расписание» → сегодня → «✅ Факт + эффективность»."),
                    parse_mode='HTML')
                sent += 1
            except Exception as e:
                logger.error(f"evening {u.id}: {e}")
        logger.info(f"Вечерняя рассылка: отправлено {sent}")


async def weekly_reminder(context):
    with app.app_context():
        db.session.expire_all()
        today = date.today()
        next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        users = User.query.filter_by(active=True).all()
        sent = 0
        for u in users:
            if not u.telegram_id:
                continue
            has_plan = DailySchedule.query.filter(
                DailySchedule.user_id == u.id,
                DailySchedule.work_date >= next_monday).first() is not None
            if has_plan:
                continue
            try:
                await context.bot.send_message(
                    chat_id=int(u.telegram_id),
                    text=(f"📅 <b>Напоминание о расписании</b>\n\n"
                          f"{esc(u.full_name)}, пришли расписание "
                          f"на неделю с <b>{next_monday.strftime('%d.%m.%Y')}</b>.\n\n"
                          f"Открой «📅 Моё расписание»."),
                    parse_mode='HTML')
                sent += 1
            except Exception as e:
                logger.error(f"weekly {u.id}: {e}")
        logger.info(f"Пятничная рассылка: отправлено {sent}")


# =====================================================================
# Main
# =====================================================================
def main():
    if not TOKEN:
        raise SystemExit('Не задан TELEGRAM_BOT_TOKEN в .env')

    from telegram.request import HTTPXRequest
    proxy = os.getenv('TELEGRAM_PROXY', '').strip()
    if proxy:
        print(f"🔌 Прокси: {proxy}")
        req = HTTPXRequest(proxy=proxy,
                           connect_timeout=30.0, read_timeout=30.0,
                           write_timeout=30.0, pool_timeout=30.0,
                           httpx_kwargs={'trust_env': False})
        application = Application.builder().token(TOKEN)\
            .request(req).get_updates_request(req).build()
    else:
        application = Application.builder().token(TOKEN).build()

    global _AI_PARSER
    _AI_PARSER = get_ai_parser()
    if _AI_PARSER:
        print(f"✅ ИИ-парсер: {_AI_PARSER.model} @ {_AI_PARSER.base_url}")
    else:
        print("ℹ️  ИИ выключен (AI_ENABLED=false или нет ключа)")

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('link', link))
    application.add_handler(CommandHandler('unlink', unlink_cmd))
    application.add_handler(CommandHandler('help', help_cmd))

    # Группа: хэштеги (приоритет)
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        handle_hashtag_message), group=-2)

    # Личка: текстовые кнопки + свободный текст
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        text_router), group=1)

    # Сохранение всех сообщений
    application.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        store_message), group=2)

    # Обработка leave-заявок текстом
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_leave_request), group=3)

    # Callbacks
    application.add_handler(CallbackQueryHandler(sched_week_callback, pattern=r'^sched_(week_|noop)'))
    application.add_handler(CallbackQueryHandler(sched_day_callback, pattern=r'^sched_day_'))
    application.add_handler(CallbackQueryHandler(sched_time_callback, pattern=r'^sched_time_'))
    application.add_handler(CallbackQueryHandler(sched_tasks_callback, pattern=r'^sched_tasks_'))
    application.add_handler(CallbackQueryHandler(sched_fact_callback, pattern=r'^sched_fact_'))
    application.add_handler(CallbackQueryHandler(sched_dayoff_callback, pattern=r'^sched_dayoff_'))
    application.add_handler(CallbackQueryHandler(sched_clear_yes_callback, pattern=r'^sched_clear_yes_'))
    application.add_handler(CallbackQueryHandler(sched_clear_callback, pattern=r'^sched_clear_\d'))
    application.add_handler(CallbackQueryHandler(mysch_clearweek_yes_callback, pattern=r'^mysch_clearweek_yes_'))
    application.add_handler(CallbackQueryHandler(mysch_clearweek_callback, pattern=r'^mysch_clearweek_'))
    application.add_handler(CallbackQueryHandler(mysch_view_callback, pattern=r'^mysch_(text|image)$'))
    application.add_handler(CallbackQueryHandler(mysch_img_nav_callback, pattern=r'^mysch_img_(prev|now|next)$'))
    application.add_handler(CallbackQueryHandler(all_week_callback, pattern=r'^allweek_'))
    application.add_handler(CallbackQueryHandler(summary_choice_callback, pattern=r'^sum_(my|all)$'))
    application.add_handler(CallbackQueryHandler(summary_callback, pattern=r'^sum_(prev|next|now)$'))

    # Job queue
    if application.job_queue is not None:
        application.job_queue.run_daily(morning_reminder,
                                        time=dtime(hour=8, minute=0),
                                        days=(0, 1, 2, 3, 4))
        application.job_queue.run_daily(evening_reminder,
                                        time=dtime(hour=18, minute=0),
                                        days=(0, 1, 2, 3, 4))
        application.job_queue.run_daily(weekly_reminder,
                                        time=dtime(hour=17, minute=0),
                                        days=(4,))
        print('✅ Напоминания: утро 08:00, вечер 18:00, пятница 17:00')

        async def refresh_session(ctx):
            with app.app_context():
                db.session.expire_all()
        application.job_queue.run_repeating(refresh_session, interval=10, first=5)
    else:
        print('⚠️  JobQueue не установлен')

    print('🤖 Telegram-бот WorkMonitor запущен. Ctrl+C для остановки.')
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()