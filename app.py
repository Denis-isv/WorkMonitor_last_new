import os, json, secrets, re
from datetime import datetime, date, time, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, abort, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32),
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL', 'sqlite:///workmonitor.db'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    UPLOAD_FOLDER=os.path.join(app.root_path, 'uploads'),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SQLALCHEMY_ENGINE_OPTIONS={
        'pool_pre_ping': True,
        'pool_recycle': 5,
        'connect_args': {'check_same_thread': False, 'timeout': 30},
    },
)
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Войдите, чтобы продолжить.'
login_manager.login_message_category = 'error'

def now(): return datetime.utcnow()

def parse_datetime(value):
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is not None:
        parsed=parsed.astimezone().replace(tzinfo=None)
    return parsed

class User(UserMixin, db.Model):
    id=db.Column(db.Integer, primary_key=True)
    username=db.Column(db.String(80), unique=True, nullable=False)
    full_name=db.Column(db.String(160), nullable=False)
    password_hash=db.Column(db.String(255), nullable=False)
    role=db.Column(db.String(30), default='employee', nullable=False)
    telegram_id=db.Column(db.String(80), unique=True)
    telegram_link_code=db.Column(db.String(80), default=lambda: secrets.token_urlsafe(8))
    department_id=db.Column(db.Integer, db.ForeignKey('department.id'))
    schedule_id=db.Column(db.Integer, db.ForeignKey('work_schedule.id'))
    position=db.Column(db.String(160), default='')
    work_rate=db.Column(db.String(20), default='full', nullable=False)
    manager_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    active=db.Column(db.Boolean, default=True, nullable=False)
    created_at=db.Column(db.DateTime, default=now)
    events=db.relationship('CalendarEvent', backref='owner', lazy=True, cascade='all, delete-orphan')
    def set_password(self,p): self.password_hash=generate_password_hash(p)
    def check_password(self,p): return check_password_hash(self.password_hash,p)

class Department(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    name=db.Column(db.String(160), unique=True, nullable=False)
    description=db.Column(db.Text, default='')
    manager_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    users=db.relationship('User', foreign_keys='User.department_id', backref='department', lazy=True)

class WorkSchedule(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    name=db.Column(db.String(120), nullable=False)
    workdays=db.Column(db.String(40), default='1,2,3,4,5')
    start_time=db.Column(db.String(5), default='09:00')
    end_time=db.Column(db.String(5), default='18:00')
    tolerance_minutes=db.Column(db.Integer, default=10)
    break_minutes=db.Column(db.Integer, default=60)
    active=db.Column(db.Boolean, default=True)

class CalendarEvent(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    title=db.Column(db.String(180), nullable=False)
    description=db.Column(db.Text, default='')
    start_at=db.Column(db.DateTime, nullable=False, index=True)
    end_at=db.Column(db.DateTime, nullable=False)
    category=db.Column(db.String(30), default='work')
    status=db.Column(db.String(30), default='planned')
    course=db.Column(db.String(120), default='')
    user_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at=db.Column(db.DateTime, default=now)

class Project(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    name=db.Column(db.String(180), nullable=False)
    code=db.Column(db.String(80), unique=True)
    description=db.Column(db.Text, default='')
    manager_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    start_date=db.Column(db.Date)
    end_date=db.Column(db.Date)
    status=db.Column(db.String(30), default='active')
    funding_source=db.Column(db.String(160), default='')
    created_at=db.Column(db.DateTime, default=now)

class ProjectMember(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    project_id=db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    user_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role=db.Column(db.String(80), default='Участник')

class Task(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    project_id=db.Column(db.Integer, db.ForeignKey('project.id'))
    title=db.Column(db.String(240), nullable=False)
    description=db.Column(db.Text, default='')
    creator_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    assignee_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at=db.Column(db.DateTime, default=now)
    deadline=db.Column(db.Date)
    status=db.Column(db.String(30), default='new')
    priority=db.Column(db.String(20), default='normal')
    parent_task_id=db.Column(db.Integer, db.ForeignKey('task.id'))

class TaskAssignee(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    task_id=db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False, index=True)
    user_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    __table_args__=(db.UniqueConstraint('task_id','user_id',name='uq_task_assignee'),)

class TimeEntry(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    employee_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_id=db.Column(db.Integer, db.ForeignKey('project.id'))
    task_id=db.Column(db.Integer, db.ForeignKey('task.id'))
    work_date=db.Column(db.Date, nullable=False, default=date.today)
    duration_minutes=db.Column(db.Integer, nullable=False)
    duration_type=db.Column(db.String(20), default='exact')
    source=db.Column(db.String(30), default='manual')
    confidence=db.Column(db.Float, default=1.0)
    description=db.Column(db.Text, default='')
    source_reference=db.Column(db.String(240), default='')
    extraction_status=db.Column(db.String(30), default='confirmed')
    created_at=db.Column(db.DateTime, default=now)

class AttendanceEvent(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    employee_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    occurred_at=db.Column(db.DateTime, nullable=False)
    direction=db.Column(db.String(20), nullable=False)
    checkpoint=db.Column(db.String(160), default='')
    source=db.Column(db.String(80), default='manual')
    external_id=db.Column(db.String(160), unique=True)
    raw_payload=db.Column(db.Text, default='')
    created_at=db.Column(db.DateTime, default=now)

class AttendanceCorrection(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    employee_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    work_date=db.Column(db.Date, nullable=False)
    correction_type=db.Column(db.String(40), nullable=False)
    reason=db.Column(db.Text, nullable=False)
    status=db.Column(db.String(30), default='requires_confirmation')
    created_by=db.Column(db.Integer, db.ForeignKey('user.id'))
    approved_by=db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at=db.Column(db.DateTime, default=now)

class TelegramChat(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    chat_id=db.Column(db.String(100), unique=True, nullable=False)
    title=db.Column(db.String(240), default='')
    enabled=db.Column(db.Boolean, default=True)
    created_at=db.Column(db.DateTime, default=now)

class TelegramMessage(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    telegram_message_id=db.Column(db.String(100), nullable=False)
    chat_id=db.Column(db.String(100), nullable=False)
    sender_telegram_id=db.Column(db.String(100), default='')
    sender_name=db.Column(db.String(240), default='')
    user_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    message_date=db.Column(db.DateTime, nullable=False, default=now)
    text=db.Column(db.Text, default='')
    raw_json=db.Column(db.Text, default='')
    reply_to_message_id=db.Column(db.String(100), default='')
    media_type=db.Column(db.String(40), default='text')
    attachment_path=db.Column(db.String(500), default='')
    transcript=db.Column(db.Text, default='')
    transcript_language=db.Column(db.String(20), default='')
    transcript_confidence=db.Column(db.Float)
    processed=db.Column(db.Boolean, default=False)
    created_at=db.Column(db.DateTime, default=now)
    __table_args__=(db.UniqueConstraint('chat_id','telegram_message_id',name='uq_tg_message'),)

class DiscussionBlock(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    title=db.Column(db.String(240), default='')
    summary=db.Column(db.Text, default='')
    started_at=db.Column(db.DateTime, default=now)
    ended_at=db.Column(db.DateTime, default=now)
    participant_names=db.Column(db.Text, default='')
    project_id=db.Column(db.Integer, db.ForeignKey('project.id'))
    message_ids=db.Column(db.Text, default='[]')
    created_at=db.Column(db.DateTime, default=now)

class ExtractedEvent(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    event_type=db.Column(db.String(60), nullable=False)
    title=db.Column(db.String(240), nullable=False)
    description=db.Column(db.Text, default='')
    employee_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    project_id=db.Column(db.Integer, db.ForeignKey('project.id'))
    task_id=db.Column(db.Integer, db.ForeignKey('task.id'))
    event_date=db.Column(db.Date)
    duration_minutes=db.Column(db.Integer)
    confidence=db.Column(db.Float, default=0)
    extraction_status=db.Column(db.String(30), default='requires_confirmation')
    source_message_ids=db.Column(db.Text, default='[]')
    llm_model=db.Column(db.String(120), default='')
    created_at=db.Column(db.DateTime, default=now)

class ConfirmationRequest(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    user_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    extracted_event_id=db.Column(db.Integer, db.ForeignKey('extracted_event.id'))
    question=db.Column(db.Text, nullable=False)
    status=db.Column(db.String(30), default='pending')
    answer=db.Column(db.Text, default='')
    created_at=db.Column(db.DateTime, default=now)
    answered_at=db.Column(db.DateTime)

class Decision(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    project_id=db.Column(db.Integer, db.ForeignKey('project.id'))
    title=db.Column(db.String(240), nullable=False)
    description=db.Column(db.Text, default='')
    decision_date=db.Column(db.Date, default=date.today)
    participants=db.Column(db.Text, default='')
    source_message_ids=db.Column(db.Text, default='[]')
    confidence=db.Column(db.Float, default=1.0)

class Issue(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    project_id=db.Column(db.Integer, db.ForeignKey('project.id'))
    task_id=db.Column(db.Integer, db.ForeignKey('task.id'))
    author_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    description=db.Column(db.Text, nullable=False)
    status=db.Column(db.String(30), default='open')
    severity=db.Column(db.String(30), default='medium')
    responsible_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at=db.Column(db.DateTime, default=now)
    resolved_at=db.Column(db.DateTime)

class WorkResult(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    employee_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    project_id=db.Column(db.Integer, db.ForeignKey('project.id'))
    task_id=db.Column(db.Integer, db.ForeignKey('task.id'))
    discussion_block_id=db.Column(db.Integer, db.ForeignKey('discussion_block.id'))
    title=db.Column(db.String(240), nullable=False)
    description=db.Column(db.Text, default='')
    source_message_id=db.Column(db.Integer, db.ForeignKey('telegram_message.id'))
    result_date=db.Column(db.Date, default=date.today)
    confidence=db.Column(db.Float, default=1.0)

class LLMRun(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    model=db.Column(db.String(120), default='')
    model_version=db.Column(db.String(120), default='')
    prompt_version=db.Column(db.String(80), default='v1')
    input_text=db.Column(db.Text, default='')
    output_text=db.Column(db.Text, default='')
    processing_time_ms=db.Column(db.Integer)
    confidence=db.Column(db.Float)
    status=db.Column(db.String(30), default='ok')
    created_at=db.Column(db.DateTime, default=now)

class AuditLog(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    actor_id=db.Column(db.Integer, db.ForeignKey('user.id'))
    action=db.Column(db.String(120), nullable=False)
    entity_type=db.Column(db.String(80), default='')
    entity_id=db.Column(db.String(80), default='')
    old_value=db.Column(db.Text, default='')
    new_value=db.Column(db.Text, default='')
    reason=db.Column(db.Text, default='')
    ip_address=db.Column(db.String(80), default='')
    created_at=db.Column(db.DateTime, default=now)

class AbsenceRequest(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    user_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    absence_type=db.Column(db.String(20), nullable=False)
    date_start=db.Column(db.Date, nullable=False)
    date_end=db.Column(db.Date, nullable=True)
    reason=db.Column(db.Text, default='')
    status=db.Column(db.String(20), default='pending', nullable=False)
    created_at=db.Column(db.DateTime, default=now)
    reviewed_by=db.Column(db.Integer, db.ForeignKey('user.id'))
    reviewed_at=db.Column(db.DateTime)
    attachment_name=db.Column(db.String(255), default='')
    attachment_path=db.Column(db.String(500), default='')
    employee=db.relationship('User', foreign_keys=[user_id])

class DailySchedule(db.Model):
    id=db.Column(db.Integer, primary_key=True)
    user_id=db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    work_date=db.Column(db.Date, nullable=False, index=True)
    start_time=db.Column(db.String(5), default='09:00')
    end_time=db.Column(db.String(5), default='18:00')
    is_day_off=db.Column(db.Boolean, default=False, nullable=False)
    note=db.Column(db.String(500), default='')
    tasks_text=db.Column(db.Text, default='')
    fact_text=db.Column(db.Text, default='')
    confidence=db.Column(db.Float, default=1.0)
    source_message_id=db.Column(db.String(100), default='')
    actual_start=db.Column(db.String(5), default='')
    actual_end=db.Column(db.String(5), default='')
    efficiency=db.Column(db.Float, default=1.0)
    created_at=db.Column(db.DateTime, default=now)
    employee=db.relationship('User')
    __table_args__=(db.UniqueConstraint('user_id','work_date',name='uq_daily_schedule_user_date'),)

@login_manager.user_loader
def load_user(user_id):
    try: return db.session.get(User, int(user_id))
    except (ValueError, TypeError): return None

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*a, **kw):
        if current_user.role != 'admin': abort(403)
        return fn(*a, **kw)
    return wrapped

def audit(action, entity_type='', entity_id='', old='', new='', reason=''):
    try:
        db.session.add(AuditLog(actor_id=current_user.id if current_user.is_authenticated else None,
            action=action, entity_type=entity_type, entity_id=str(entity_id or ''),
            old_value=str(old or '')[:10000], new_value=str(new or '')[:10000],
            reason=str(reason or '')[:2000], ip_address=(request.remote_addr or '')[:80]))
    except Exception: pass

# =====================================================================
# Шаблонные фильтры (русский язык)
# =====================================================================
@app.template_filter('role_ru')
def role_ru_filter(value):
    return {
        'admin': 'Администратор', 'employee': 'Сотрудник',
        'manager': 'Руководитель', 'project_manager': 'Руководитель проекта',
        'hr': 'HR',
    }.get(value, value or '—')

@app.template_filter('work_rate_ru')
def work_rate_ru_filter(value):
    return {'full': 'Полная ставка', 'half': 'Полставки'}.get(value, value or '—')

@app.template_filter('status_ru')
def status_ru_filter(value):
    return {
        'active': 'Активен', 'paused': 'Приостановлен',
        'completed': 'Завершён', 'cancelled': 'Отменён',
        'new': 'Новая', 'planned': 'Запланирована',
        'in_progress': 'В работе', 'blocked': 'Заблокирована',
        'review': 'На проверке', 'open': 'Открыта',
        'resolved': 'Решена', 'closed': 'Закрыта',
        'pending': 'На согласовании', 'approved': 'Согласовано',
        'rejected': 'Отклонено',
        'confirmed': 'Подтверждено', 'automatic': 'Автоматически',
        'requires_confirmation': 'Требует подтверждения',
        'pending_deletion': 'Ожидает удаления',
    }.get(value, value or '—')

@app.template_filter('priority_ru')
def priority_ru_filter(value):
    return {'low': 'Низкий', 'normal': 'Обычный',
            'high': 'Высокий', 'urgent': 'Срочный'}.get(value, value or '—')

@app.template_filter('severity_ru')
def severity_ru_filter(value):
    return {'low': 'Низкая', 'medium': 'Средняя',
            'high': 'Высокая', 'critical': 'Критическая'}.get(value, value or '—')

@app.template_filter('source_ru')
def source_ru_filter(value):
    return {'manual': 'Вручную', 'telegram': 'Telegram',
            'task_system': 'Система задач',
            'external_system': 'Внешняя система'}.get(value, value or '—')

@app.template_filter('duration_type_ru')
def duration_type_ru_filter(value):
    return {'exact': 'Точное', 'approximate': 'Примерное'}.get(value, value or '—')

@app.template_filter('direction_ru')
def direction_ru_filter(value):
    return {'entry': 'Вход', 'exit': 'Выход'}.get(value, value or '—')

@app.template_filter('correction_type_ru')
def correction_type_ru_filter(value):
    return {
        'forgot_badge': 'Забыл пропуск', 'wrong_event': 'Неверная отметка СКУД',
        'remote_work': 'Удалённая работа', 'business_trip': 'Командировка',
        'service_trip': 'Служебный выезд', 'vacation': 'Отпуск',
        'sick_leave': 'Больничный', 'approved_absence': 'Согласованное отсутствие',
        'other': 'Другое'
    }.get(value, value or '—')

@app.cli.command('init-db')
def init_db():
    db.create_all()
    admin_name=os.getenv('ADMIN_USERNAME','admin')
    admin_password=os.getenv('ADMIN_PASSWORD','ChangeMe123!')
    if not User.query.filter_by(username=admin_name).first():
        u=User(username=admin_name,full_name=os.getenv('ADMIN_FULL_NAME','Администратор'),role='admin')
        u.set_password(admin_password); u.telegram_link_code=secrets.token_urlsafe(8); db.session.add(u); db.session.commit()
        print(f'Создан администратор {admin_name}.')
    if not WorkSchedule.query.first():
        db.session.add(WorkSchedule(name='Стандартная пятидневка',workdays='1,2,3,4,5',
            start_time='09:00',end_time='18:00',tolerance_minutes=10,break_minutes=60,active=True))
        db.session.commit()
    print('База данных готова.')

@app.route('/')
def index():
    return redirect(url_for('dashboard' if current_user.is_authenticated else 'login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method=='POST':
        user=User.query.filter_by(username=request.form.get('username','').strip()).first()
        if user and user.active and user.check_password(request.form.get('password','')):
            login_user(user, remember=True)
            audit('login','User',user.id)
            db.session.commit()
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Неверный логин или пароль', 'error')
    return render_template('login.html')

@app.route('/logout', methods=['GET','POST'])
@login_required
def logout():
    audit('logout','User',current_user.id); db.session.commit(); logout_user()
    return redirect(url_for('login'))

@app.route('/calendar')
@login_required
def calendar_page():
    users=User.query.filter_by(active=True).order_by(User.full_name).all() if current_user.role=='admin' else [current_user]
    return render_template('calendar.html',users=users)

@app.route('/dashboard')
@login_required
def dashboard():
    today=date.today()
    month_start=date(today.year,today.month,1)
    total_users=User.query.filter_by(active=True).count()
    projects=Project.query.filter(Project.status!='completed').count()
    open_tasks=Task.query.filter(Task.status.notin_(['completed','cancelled'])).count()
    pending=ExtractedEvent.query.filter_by(extraction_status='requires_confirmation').count()
    open_issues=Issue.query.filter_by(status='open').count()
    minutes=db.session.query(db.func.coalesce(db.func.sum(TimeEntry.duration_minutes),0)).filter(TimeEntry.work_date>=month_start).scalar()
    recent=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(8).all()
    return render_template('dashboard.html',total_users=total_users,projects=projects,open_tasks=open_tasks,
        pending=pending,open_issues=open_issues,minutes=minutes,recent=recent,today=today)

@app.route('/api/events', methods=['GET','POST'])
@login_required
def events_api():
    if request.method=='POST':
        data=request.get_json(silent=True) or request.form
        try:
            start=parse_datetime(data.get('start_at',''))
            end=parse_datetime(data.get('end_at',''))
            if end<=start: return jsonify(error='Время окончания должно быть позже начала'),400
            title=str(data.get('title','')).strip()
            category=str(data.get('category','work'))
            if category not in ('work','vacation','sick_leave','day_off'): return jsonify(error='Недопустимый тип события'),400
            if current_user.role!='admin' and category!='work': return jsonify(error='Отпуск, больничный и выходной оформляются через раздел «Отпуск и больничный»'),403
            if not title: return jsonify(error='Укажи название события'),400
            user_id=int(data.get('user_id') or current_user.id)
            if current_user.role!='admin': user_id=current_user.id
            if not db.session.get(User,user_id): return jsonify(error='Пользователь не найден'),404
            event=CalendarEvent(title=title,description=str(data.get('description',''))[:10000],start_at=start,end_at=end,
                category=category,status=str(data.get('status','planned')),
                course='',user_id=user_id)
            db.session.add(event); db.session.flush(); audit('create','CalendarEvent',event.id,new=event.title); db.session.commit()
            return jsonify(event_json(event)),201
        except (ValueError,TypeError): return jsonify(error='Проверь дату и время'),400
    query=CalendarEvent.query
    if current_user.role!='admin': query=query.filter_by(user_id=current_user.id)
    if request.args.get('start'):
        try: query=query.filter(CalendarEvent.end_at>=parse_datetime(request.args['start']))
        except ValueError: return jsonify(error='Неверная дата start'),400
    if request.args.get('end'):
        try: query=query.filter(CalendarEvent.start_at<parse_datetime(request.args['end']))
        except ValueError: return jsonify(error='Неверная дата end'),400
    if request.args.get('user_id') and current_user.role=='admin':
        try: query=query.filter_by(user_id=int(request.args['user_id']))
        except ValueError: return jsonify(error='Неверный user_id'),400
    result=[event_json(e) for e in query.order_by(CalendarEvent.start_at).all()]

    absq=AbsenceRequest.query.filter(AbsenceRequest.status=='approved')
    if current_user.role!='admin': absq=absq.filter_by(user_id=current_user.id)
    for a in absq.all():
        owner=db.session.get(User,a.user_id)
        if request.args.get('start') and a.date_end and a.date_end < parse_datetime(request.args['start']).date(): continue
        if request.args.get('end') and a.date_start >= parse_datetime(request.args['end']).date(): continue
        kind=a.absence_type
        title={'vacation':'Отпуск','sick_leave':'Больничный','day_off':'Выходной'}.get(kind,'Отсутствие')
        end_d = a.date_end or date(2099,12,31)
        result.append({'id':f'absence-{a.id}','title':title,'description':owner.full_name if owner else '',
            'start_at':datetime.combine(a.date_start,time(9,0)).isoformat(),
            'end_at':datetime.combine(end_d,time(18,0)).isoformat(),
            'category':kind,'status':'approved','course':'','user_id':a.user_id,
            'user_name':owner.full_name if owner else '', 'readonly':True})

    task_query=Task.query.filter(Task.deadline.isnot(None),Task.status.notin_(['completed','cancelled']))
    if current_user.role!='admin':
        assigned_task_ids=[x.task_id for x in TaskAssignee.query.filter_by(user_id=current_user.id).all()]
        task_query=task_query.filter(db.or_(Task.assignee_id==current_user.id, Task.id.in_(assigned_task_ids)))
    for t in task_query.all():
        if request.args.get('start') and t.deadline < parse_datetime(request.args['start']).date(): continue
        if request.args.get('end') and t.deadline >= parse_datetime(request.args['end']).date(): continue
        owners=[]
        if t.assignee_id: owners.append(db.session.get(User,t.assignee_id))
        for ta in TaskAssignee.query.filter_by(task_id=t.id).all():
            u=db.session.get(User,ta.user_id)
            if u and all(x.id!=u.id for x in owners): owners.append(u)
        if current_user.role!='admin' and not any(u and u.id==current_user.id for u in owners): continue
        owner=owners[0] if owners else current_user
        result.append({'id':f'task-{t.id}','title':'Задача: '+t.title,
            'description':(', '.join(u.full_name for u in owners if u) if owners else (owner.full_name if owner else ''))+' · срок',
            'start_at':datetime.combine(t.deadline,time(9,0)).isoformat(),
            'end_at':datetime.combine(t.deadline,time(10,0)).isoformat(),
            'category':'work','status':t.status,'course':'',
            'user_id':owner.id if owner else current_user.id,
            'user_name':owner.full_name if owner else '','readonly':True})

    try:
        start_d = parse_datetime(request.args['start']).date() if request.args.get('start') else None
        end_d = parse_datetime(request.args['end']).date() if request.args.get('end') else None
    except ValueError:
        start_d = end_d = None

    schedule_q = DailySchedule.query
    if current_user.role != 'admin':
        schedule_q = schedule_q.filter_by(user_id=current_user.id)
    if start_d:
        schedule_q = schedule_q.filter(DailySchedule.work_date >= start_d)
    if end_d:
        schedule_q = schedule_q.filter(DailySchedule.work_date < end_d)

    for s in schedule_q.all():
        owner = db.session.get(User, s.user_id)
        if s.is_day_off:
            result.append({'id': f'schedule-off-{s.id}','title': '🏖 Выходной',
                'description': owner.full_name if owner else '',
                'start_at': datetime.combine(s.work_date, time(0,0)).isoformat(),
                'end_at': datetime.combine(s.work_date, time(23,59)).isoformat(),
                'category': 'day_off','status': 'planned',
                'user_id': s.user_id,'user_name': owner.full_name if owner else '',
                'readonly': True,'source': 'schedule'})
        else:
            result.append({'id': f'schedule-{s.id}',
                'title': f'🕐 Работа {s.start_time}–{s.end_time}',
                'description': owner.full_name if owner else '',
                'start_at': datetime.combine(s.work_date, time.fromisoformat(s.start_time)).isoformat(),
                'end_at': datetime.combine(s.work_date, time.fromisoformat(s.end_time)).isoformat(),
                'category': 'work','status': 'planned',
                'user_id': s.user_id,'user_name': owner.full_name if owner else '',
                'readonly': True,'source': 'schedule'})
    return jsonify(result)

def event_json(e):
    return {'id':e.id,'title':e.title,'description':e.description,'start_at':e.start_at.isoformat(),'end_at':e.end_at.isoformat(),
        'category':e.category,'status':e.status,'course':e.course,'user_id':e.user_id,'user_name':e.owner.full_name}

@app.route('/api/events/<int:event_id>', methods=['PATCH','DELETE'])
@login_required
def event_detail(event_id):
    e=db.session.get(CalendarEvent,event_id)
    if not e: return jsonify(error='Событие не найдено'),404
    if current_user.role!='admin' and e.user_id!=current_user.id: return jsonify(error='Недостаточно прав'),403
    if request.method=='DELETE':
        audit('delete','CalendarEvent',e.id,old=e.title); db.session.delete(e); db.session.commit(); return jsonify(ok=True)
    data=request.get_json(silent=True) or {}
    old=e.title
    for key in ('title','description','status'):
        if key in data: setattr(e,key,str(data[key])[:10000])
    if 'category' in data:
        if data['category'] not in ('work','vacation','sick_leave','day_off'): return jsonify(error='Недопустимый тип события'),400
        if current_user.role!='admin' and data['category']!='work': return jsonify(error='Отпуск, больничный и выходной оформляются через раздел «Отпуск и больничный»'),403
        e.category=data['category']
    if current_user.role=='admin' and 'user_id' in data:
        try:
            target_id=int(data['user_id'])
            if not db.session.get(User,target_id): return jsonify(error='Сотрудник не найден'),404
            e.user_id=target_id
        except (ValueError,TypeError): return jsonify(error='Неверный сотрудник'),400
    for key in ('start_at','end_at'):
        if key in data:
            try: setattr(e,key,parse_datetime(data[key]))
            except ValueError: return jsonify(error='Неверный формат даты'),400
    if e.end_at<=e.start_at or not e.title.strip(): return jsonify(error='Проверь название и время события'),400
    audit('update','CalendarEvent',e.id,old=old,new=e.title); db.session.commit(); return jsonify(event_json(e))

@app.route('/api/me')
@login_required
def me(): return jsonify(id=current_user.id,username=current_user.username,full_name=current_user.full_name,role=current_user.role)

SECTIONS={
'projects': {'title':'Проекты','model':Project,'fields':[('name','Название','text',True),('code','Код','text',False),('description','Описание','textarea',False),('manager_id','Руководитель','user',False),('start_date','Начало','date',False),('end_date','Окончание','date',False),('status','Статус','choice:active,paused,completed,cancelled',True),('funding_source','Источник финансирования','text',False)],'cols':['name','code','status','funding_source','participants'],'search':['name','code','description']},
'tasks': {'title':'Задачи','model':Task,'fields':[('title','Название','text',True),('project_id','Проект','project',False),('description','Описание','textarea',False),('assignee_id','Ответственный','user',False),('deadline','Срок','date',False),('status','Статус','choice:new,planned,in_progress,blocked,review,completed,cancelled',True),('priority','Приоритет','choice:low,normal,high,urgent',True)],'cols':['title','project_id','assignee_id','deadline','status','priority'],'search':['title','description']},
'time': {'title':'Трудозатраты','model':TimeEntry,'fields':[('employee_id','Сотрудник','user',True),('project_id','Проект','project',False),('task_id','Задача','task',False),('work_date','Дата','date',True),('duration_minutes','Минуты','number',True),('duration_type','Точность','choice:exact,approximate',True),('source','Источник','choice:manual,telegram,task_system,external_system',True),('confidence','Уверенность 0–1','decimal',True),('description','Описание работы','textarea',False),('source_reference','Ссылка на источник','text',False),('extraction_status','Статус','choice:confirmed,automatic,requires_confirmation,rejected',True)],'cols':['employee_id','project_id','task_id','work_date','duration_minutes','source','confidence'],'search':['description','source_reference']},
'attendance': {'title':'Рабочее время','model':AttendanceEvent,'fields':[('employee_id','Сотрудник','user',True),('occurred_at','Дата и время','datetime',True),('direction','Направление','choice:entry,exit',True),('checkpoint','Точка прохода','text',False),('source','Источник','text',True),('external_id','ID события источника','text',False)],'cols':['employee_id','occurred_at','direction','checkpoint','source'],'search':['checkpoint','external_id']},
'corrections': {'title':'Корректировки времени','model':AttendanceCorrection,'fields':[('employee_id','Сотрудник','user',True),('work_date','Дата','date',True),('correction_type','Тип','choice:forgot_badge,wrong_event,remote_work,business_trip,service_trip,vacation,sick_leave,approved_absence,other',True),('reason','Основание','textarea',True),('status','Статус','choice:requires_confirmation,approved,rejected',True)],'cols':['employee_id','work_date','correction_type','reason','status'],'search':['reason']},
'decisions': {'title':'Журнал решений','model':Decision,'fields':[('project_id','Проект','project',False),('decision_date','Дата','date',True),('title','Решение','text',True),('description','Описание','textarea',False),('participants','Участники','text',False),('confidence','Уверенность 0–1','decimal',True)],'cols':['decision_date','title','project_id','confidence'],'search':['title','description','participants']},
'issues': {'title':'Проблемы','model':Issue,'fields':[('project_id','Проект','project',False),('task_id','Задача','task',False),('description','Проблема','textarea',True),('severity','Серьёзность','choice:low,medium,high,critical',True),('status','Статус','choice:open,in_progress,resolved,closed',True),('responsible_id','Ответственный','user',False)],'cols':['description','project_id','severity','status','responsible_id','created_at'],'search':['description']},
'results': {'title':'Результаты работы','model':WorkResult,'fields':[('employee_id','Сотрудник','user',False),('project_id','Проект','project',False),('task_id','Задача','task',False),('result_date','Дата','date',True),('title','Результат','text',True),('description','Описание','textarea',False),('confidence','Уверенность 0–1','decimal',True)],'cols':['result_date','title','employee_id','project_id','confidence'],'search':['title','description']},
'confirmations': {'title':'Подтверждения AI','model':ExtractedEvent,'fields':[('event_type','Тип события','text',True),('title','Событие','text',True),('description','Описание','textarea',False),('employee_id','Сотрудник','user',False),('project_id','Проект','project',False),('event_date','Дата','date',False),('duration_minutes','Длительность, мин','number',False),('confidence','Уверенность 0–1','decimal',True),('extraction_status','Статус','choice:confirmed,automatic,requires_confirmation,rejected',True)],'cols':['event_type','title','employee_id','event_date','duration_minutes','confidence','extraction_status'],'search':['title','description']},
'discussions': {'title':'Обсуждения','model':DiscussionBlock,'fields':[('title','Название блока','text',True),('summary','Краткое содержание','textarea',False),('started_at','Начало','datetime',True),('ended_at','Окончание','datetime',True),('participant_names','Участники','text',False),('project_id','Проект','project',False)],'cols':['title','summary','started_at','ended_at','participant_names','project_id'],'search':['title','summary','participant_names']},
'messages': {'title':'Telegram-сообщения','model':TelegramMessage,'fields':[('chat_id','ID чата','text',True),('telegram_message_id','ID сообщения','text',True),('sender_name','Автор','text',False),('message_date','Дата','datetime',True),('text','Исходный текст','textarea',False),('reply_to_message_id','Ответ на ID','text',False),('media_type','Тип вложения','text',True),('transcript','Транскрипт','textarea',False)],'cols':['message_date','sender_name','chat_id','telegram_message_id','text','media_type','processed'],'search':['text','transcript','sender_name','chat_id']},
'users': {'title':'Сотрудники','model':User,'fields':[('username','Логин','text',True),('full_name','ФИО','text',True),('position','Должность','text',False),('work_rate','Ставка','choice:full,half',True),('department_id','Подразделение','department',False),('schedule_id','Рабочий график','schedule',False),('role','Роль','choice:admin,employee',True),('active','Активен','choice:true,false',True)],'cols':['full_name','username','position','department_id','role','active','telegram_link_code'],'search':['full_name','username','position']},
'departments': {'title':'Подразделения','model':Department,'fields':[('name','Название','text',True),('description','Описание','textarea',False)],'cols':['name','description'],'search':['name','description']},
'schedules': {'title':'Рабочие графики','model':WorkSchedule,'fields':[('name','Название','text',True),('workdays','Дни недели (1=пн…7=вс)','text',True),('start_time','Начало','text',True),('end_time','Окончание','text',True),('tolerance_minutes','Допуск, мин','number',True),('break_minutes','Перерыв, мин','number',True),('active','Активен','choice:true,false',True)],'cols':['name','workdays','start_time','end_time','tolerance_minutes','break_minutes','active'],'search':['name']},
}
NAV=[('dashboard','Главная','⌂'),('calendar','Календарь','▦'),('today','Кто работает сегодня','☀'),('week','Расписание недели','▦'),('month','Итоги месяца','▤'),('leave','Отпуск и больничный','♡'),('schedule','Мой график','◷'),('users','Сотрудники','♙'),('projects','Проекты','▤'),('tasks','Задачи','✓'),('issues','Проблемы','!')]

def label_value(key, value):
    if value is None or value=='': return '—'
    if key in ('employee_id','user_id','assignee_id','manager_id','responsible_id','creator_id','approved_by','created_by'):
        u=db.session.get(User,value) if str(value).isdigit() else None
        return u.full_name if u else value
    if key in ('project_id',):
        o=db.session.get(Project,value) if str(value).isdigit() else None
        return o.name if o else value
    if key in ('task_id','parent_task_id'):
        o=db.session.get(Task,value) if str(value).isdigit() else None
        return o.title if o else value
    if key=='department_id':
        o=db.session.get(Department,value) if str(value).isdigit() else None
        return o.name if o else value
    if isinstance(value,datetime): return value.strftime('%d.%m.%Y %H:%M')
    if isinstance(value,date): return value.strftime('%d.%m.%Y')
    if isinstance(value,bool): return 'Да' if value else 'Нет'
    if key=='duration_minutes':
        try: return f'{int(value)//60} ч {int(value)%60:02d} мин'
        except Exception: pass
    return str(value)

@app.context_processor
def inject_nav():
    return {'main_nav':NAV,'current_endpoint':request.endpoint}

@app.route('/workspace/<section>')
@login_required
def workspace(section):
    if section not in SECTIONS or section in ('time','confirmations','decisions','messages','discussions','attendance','corrections'): abort(404)
    if section=='users' and current_user.role!='admin': abort(403)
    if section in ('departments','schedules') and current_user.role!='admin': abort(403)
    config=SECTIONS[section]; model=config['model']
    q=(request.args.get('q') or '').strip()
    status_filter=(request.args.get('status') or '').strip()
    query=model.query
    if section=='users' and current_user.role!='admin': query=query.filter_by(id=current_user.id)
    if q and config.get('search'):
        filters=[getattr(model,k).ilike(f'%{q}%') for k in config['search'] if hasattr(model,k)]
        if filters: query=query.filter(db.or_(*filters))
    if status_filter and hasattr(model,'status'):
        query=query.filter(model.status==status_filter)
    if current_user.role!='admin':
        if section in ('time','attendance','corrections','confirmations','results'):
            owner_field={'time':'employee_id','attendance':'employee_id','corrections':'employee_id','confirmations':'employee_id','results':'employee_id'}[section]
            query=query.filter(getattr(model,owner_field)==current_user.id)
        if section=='messages':
            query=query.filter_by(user_id=current_user.id)
    rows=query.order_by(model.id.desc()).limit(300).all()
    table=[]
    for obj in rows:
        cells=[]
        for key in config['cols']:
            val=getattr(obj,key,None)
            if section=='projects' and key=='participants':
                member_ids=[x.user_id for x in ProjectMember.query.filter_by(project_id=obj.id).all()]
                val=', '.join(u.full_name for u in User.query.filter(User.id.in_(member_ids)).order_by(User.full_name).all()) if member_ids else 'Не назначены'
            cells.append((key,label_value(key,val)))
        edit_data={}
        for key,label,kind,required in config['fields']:
            val=getattr(obj,key,None)
            edit_data[key]=val.isoformat() if hasattr(val,'isoformat') else ('true' if val is True else 'false' if val is False else ('' if val is None else str(val)))
        if section=='projects': edit_data['member_ids']=[x.user_id for x in ProjectMember.query.filter_by(project_id=obj.id).all()]
        if section=='tasks': edit_data['assignee_ids']=[x.user_id for x in TaskAssignee.query.filter_by(task_id=obj.id).all()]
        table.append({'id':obj.id,'cells':cells,'object':obj,'edit_data':edit_data})
    opts={'user':[{'id':u.id,'name':u.full_name} for u in User.query.filter_by(active=True).order_by(User.full_name).all()],
          'project':[{'id':p.id,'name':p.name} for p in Project.query.order_by(Project.name).all()],
          'task':[{'id':t.id,'name':t.title} for t in Task.query.order_by(Task.title).all()],
          'department':[{'id':d.id,'name':d.name} for d in Department.query.order_by(Department.name).all()],
          'schedule':[{'id':s.id,'name':s.name} for s in WorkSchedule.query.order_by(WorkSchedule.name).all()]}
    return render_template('workspace.html',section=section,config=config,rows=table,query_text=q,opts=opts)

def parse_field(kind, value):
    if value is None or value=='': return None
    if kind=='date': return date.fromisoformat(value)
    if kind=='datetime': return datetime.fromisoformat(value)
    if kind=='number': return int(value)
    if kind=='decimal': return float(value)
    if kind in ('user','project','task','department','schedule'): return int(value)
    if kind=='choice:true,false': return value=='true'
    if kind.startswith('choice:'): return value
    return value.strip() if isinstance(value,str) else value

@app.route('/workspace/<section>/save',methods=['POST'])
@login_required
def workspace_save(section):
    if section not in SECTIONS or section in ('time','confirmations','decisions','messages','discussions','attendance','corrections'): abort(404)
    cfg=SECTIONS[section]; model=cfg['model']
    item_id=request.form.get('id','').strip()
    obj=db.session.get(model,int(item_id)) if item_id.isdigit() else None
    if item_id and not obj: flash('Запись не найдена','error'); return redirect(url_for('workspace',section=section))
    if section in ('users','messages','departments','schedules') and current_user.role!='admin': abort(403)
    old=repr({c:getattr(obj,c,None) for c in cfg['cols']}) if obj else ''
    try:
        if not obj:
            obj=model()
            if section=='users':
                obj.set_password(secrets.token_urlsafe(12))
                obj.telegram_link_code=secrets.token_urlsafe(8)
            db.session.add(obj)
        for key,label,kind,required in cfg['fields']:
            raw=request.form.get(key,'')
            if required and raw=='':
                raise ValueError(f'Поле «{label}» обязательно')
            if not hasattr(obj,key): continue
            value=parse_field(kind,raw)
            setattr(obj,key,value if value is not None else ('' if kind in ('text','textarea') else None))
        if section=='tasks':
            db.session.flush()
            TaskAssignee.query.filter_by(task_id=obj.id).delete()
            for member_id in request.form.getlist('assignee_ids'):
                if member_id.isdigit() and db.session.get(User,int(member_id)):
                    db.session.add(TaskAssignee(task_id=obj.id,user_id=int(member_id)))
        if section=='projects':
            db.session.flush()
            ProjectMember.query.filter_by(project_id=obj.id).delete()
            for member_id in request.form.getlist('member_ids'):
                if member_id.isdigit() and db.session.get(User,int(member_id)):
                    db.session.add(ProjectMember(project_id=obj.id,user_id=int(member_id)))
        if section=='attendance' and not obj.external_id: obj.external_id=None
        if section=='projects' and not obj.code: obj.code=None
        if not item_id and section=='tasks': obj.creator_id=current_user.id
        if not item_id and section=='issues': obj.author_id=current_user.id
        if not item_id and section=='corrections': obj.created_by=current_user.id
        if hasattr(obj,'confidence') and obj.confidence is not None and not 0<=obj.confidence<=1:
            raise ValueError('Уверенность должна быть от 0 до 1')
        if section=='time' and (obj.duration_minutes is None or obj.duration_minutes<=0):
            raise ValueError('Длительность должна быть положительным числом минут')
        if section=='users':
            new_password=request.form.get('new_password','').strip()
            if new_password: obj.set_password(new_password)
        if section=='attendance' and obj.external_id and item_id:
            duplicate=AttendanceEvent.query.filter(AttendanceEvent.external_id==obj.external_id,AttendanceEvent.id!=obj.id).first()
            if duplicate: raise ValueError('Событие с таким внешним ID уже существует')
        if section=='messages' and item_id:
            duplicate=TelegramMessage.query.filter_by(chat_id=obj.chat_id,telegram_message_id=obj.telegram_message_id).filter(TelegramMessage.id!=obj.id).first()
            if duplicate: raise ValueError('Это сообщение уже сохранено')
        audit('create' if not item_id else 'update',model.__name__,obj.id,old=old,new=repr({c:getattr(obj,c,None) for c in cfg['cols']}))
        db.session.commit()
        flash('Запись сохранена','success')
    except (ValueError,TypeError) as exc:
        db.session.rollback(); flash(str(exc) or 'Проверьте значения полей','error')
    except Exception:
        db.session.rollback(); flash('Не удалось сохранить запись.','error')
    return redirect(url_for('workspace',section=section))

@app.route('/workspace/<section>/<int:item_id>/delete',methods=['POST'])
@login_required
def workspace_delete(section,item_id):
    if section not in SECTIONS: abort(404)
    if section in ('time','confirmations','decisions','messages','discussions','attendance','corrections'): abort(404)
    if section in ('users','messages','departments','schedules') and current_user.role!='admin': abort(403)
    cfg=SECTIONS[section]
    obj=db.session.get(cfg['model'],item_id)
    if not obj: flash('Запись не найдена','error')
    elif section=='users' and obj.id==current_user.id: flash('Нельзя удалить свою учётную запись','error')
    else:
        audit('delete',cfg['model'].__name__,obj.id,old=repr(obj))
        if section=='users':
            obj.active=False
        else:
            db.session.delete(obj)
        db.session.commit()
        flash('Запись удалена' if section!='users' else 'Учётная запись деактивирована','success')
    return redirect(url_for('workspace',section=section))

@app.route('/reports')
@login_required
def reports():
    return redirect(url_for('month_summary'))

@app.route('/api/reports/summary')
@login_required
def report_summary_api():
    start_s=request.args.get('start'); end_s=request.args.get('end')
    try:
        start=date.fromisoformat(start_s) if start_s else date(date.today().year,date.today().month,1)
        end=date.fromisoformat(end_s) if end_s else date.today()
    except ValueError: return jsonify(error='Неверный формат даты'),400
    q=TimeEntry.query.filter(TimeEntry.work_date>=start,TimeEntry.work_date<=end)
    if current_user.role!='admin': q=q.filter_by(employee_id=current_user.id)
    rows=q.all(); by_project={}
    for x in rows:
        p=db.session.get(Project,x.project_id) if x.project_id else None
        name=p.name if p else 'Без проекта'; by_project[name]=by_project.get(name,0)+x.duration_minutes
    return jsonify({'start':start.isoformat(),'end':end.isoformat(),'entries':len(rows),'minutes':sum(x.duration_minutes for x in rows),
      'projects':[{'name':k,'minutes':v} for k,v in sorted(by_project.items(),key=lambda kv:kv[1],reverse=True)]})

@app.route('/api/telegram/webhook',methods=['POST'])
def telegram_webhook():
    secret=os.getenv('TELEGRAM_WEBHOOK_SECRET','').strip()
    if not secret:
        return jsonify(error='Webhook отключён'),503
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token')!=secret: abort(403)
    payload=request.get_json(silent=True) or {}
    msg=payload.get('message') or payload.get('edited_message')
    if not msg: return jsonify(ok=True,ignored=True)
    chat=msg.get('chat') or {}; sender=msg.get('from') or {}
    chat_id=str(chat.get('id','')); mid=str(msg.get('message_id',''))
    if not chat_id or not mid: return jsonify(ok=True,ignored=True)
    if TelegramMessage.query.filter_by(chat_id=chat_id,telegram_message_id=mid).first(): return jsonify(ok=True,duplicate=True)
    sender_tid=str(sender.get('id',''))
    user=User.query.filter_by(telegram_id=sender_tid).first() if sender_tid else None
    text_value=msg.get('text') or msg.get('caption') or ''
    media='text'
    if msg.get('voice'): media='voice'
    elif msg.get('video_note'): media='video_note'
    elif msg.get('audio'): media='audio'
    elif msg.get('document'): media='document'
    elif msg.get('photo'): media='photo'
    m=TelegramMessage(telegram_message_id=mid,chat_id=chat_id,sender_telegram_id=sender_tid,
        sender_name=' '.join(x for x in [sender.get('first_name'),sender.get('last_name')] if x),
        user_id=user.id if user else None,
        message_date=datetime.fromtimestamp(msg.get('date',int(now().timestamp()))),
        text=text_value,raw_json=json.dumps(payload,ensure_ascii=False),reply_to_message_id=str((msg.get('reply_to_message') or {}).get('message_id','')),
        media_type=media)
    db.session.add(m); db.session.commit()
    return jsonify(ok=True,message_id=m.id)

@app.route('/api/attendance/summary')
@login_required
def attendance_summary():
    try:
        work_date=date.fromisoformat(request.args.get('date',date.today().isoformat()))
    except ValueError:
        return jsonify(error='Дата должна быть YYYY-MM-DD'),400
    users=User.query.filter_by(active=True).order_by(User.full_name).all()
    if current_user.role!='admin':
        users=[u for u in users if u.id==current_user.id]
    default_schedule=WorkSchedule.query.filter_by(active=True).order_by(WorkSchedule.id).first()
    output=[]
    for user in users:
        schedule=db.session.get(WorkSchedule,user.schedule_id) if user.schedule_id else default_schedule
        day_number=work_date.isoweekday()
        workdays=[x.strip() for x in (schedule.workdays if schedule else '1,2,3,4,5').split(',')]
        planned_start=datetime.combine(work_date,time.fromisoformat(schedule.start_time if schedule else '09:00'))
        planned_end=datetime.combine(work_date,time.fromisoformat(schedule.end_time if schedule else '18:00'))
        events=AttendanceEvent.query.filter_by(employee_id=user.id).filter(
            AttendanceEvent.occurred_at>=datetime.combine(work_date,time.min),
            AttendanceEvent.occurred_at<datetime.combine(work_date+timedelta(days=1),time.min)).order_by(AttendanceEvent.occurred_at).all()
        entries=[e.occurred_at for e in events if e.direction.lower() in ('entry','in','вход')]
        exits=[e.occurred_at for e in events if e.direction.lower() in ('exit','out','выход')]
        first=min(entries) if entries else None
        last=max(exits) if exits else None
        gross=max(0,int((last-first).total_seconds()//60)) if first and last else None
        break_minutes=schedule.break_minutes if schedule else 60
        net=max(0,gross-break_minutes) if gross is not None else None
        tolerance=schedule.tolerance_minutes if schedule else 10
        late=max(0,int((first-planned_start).total_seconds()//60)-tolerance) if first and str(day_number) in workdays else 0
        early=max(0,int((planned_end-last).total_seconds()//60)-tolerance) if last and str(day_number) in workdays else 0
        overtime=max(0,int((last-planned_end).total_seconds()//60)) if last and str(day_number) in workdays else 0
        output.append({'employee_id':user.id,'employee':user.full_name,'date':work_date.isoformat(),
            'is_workday':str(day_number) in workdays,'planned_start':planned_start.isoformat(),'planned_end':planned_end.isoformat(),
            'actual_first_entry':first.isoformat() if first else None,'actual_last_exit':last.isoformat() if last else None,
            'gross_presence_minutes':gross,'break_minutes':break_minutes if gross is not None else None,
            'net_presence_minutes':net,'late_minutes':late,'early_leave_minutes':early,
            'potential_overtime_minutes':overtime,'has_missing_entry':bool(last and not first),'has_missing_exit':bool(first and not last),
            'unexplained_absence':bool(str(day_number) in workdays and not first and not last and work_date<=date.today())})
    return jsonify(date=work_date.isoformat(),employees=output)

@app.route('/api/search')
@login_required
def search_api():
    q=(request.args.get('q') or '').strip()
    if len(q)<2: return jsonify(results=[])
    pattern=f'%{q}%'
    messages=TelegramMessage.query.filter(db.or_(TelegramMessage.text.ilike(pattern),TelegramMessage.transcript.ilike(pattern))).order_by(TelegramMessage.message_date.desc()).limit(20).all()
    tasks=Task.query.filter(db.or_(Task.title.ilike(pattern),Task.description.ilike(pattern))).limit(10).all()
    return jsonify(results=[{'type':'telegram','id':m.id,'date':m.message_date.isoformat(),'title':m.sender_name or 'Telegram','text':m.text or m.transcript} for m in messages]+[{'type':'task','id':t.id,'title':t.title,'text':t.description or ''} for t in tasks])

@app.route('/today')
@login_required
def who_works_today():
    day=date.today()
    users=User.query.filter_by(active=True).order_by(User.full_name).all()
    schedules={x.user_id:x for x in DailySchedule.query.filter_by(work_date=day).all()}
    absences=AbsenceRequest.query.filter(
        AbsenceRequest.status=='approved',
        AbsenceRequest.date_start<=day,
        db.or_(AbsenceRequest.date_end.is_(None), AbsenceRequest.date_end>=day)
    ).all()
    absent_by_user={x.user_id:x for x in absences}
    rows=[]
    for u in users:
        sch=schedules.get(u.id); ab=absent_by_user.get(u.id)
        if ab: status={'vacation':'В отпуске','sick_leave':'На больничном','day_off':'Выходной'}.get(ab.absence_type,'Отсутствует')
        elif sch and sch.is_day_off: status='Выходной'
        elif sch: status=f"Работает {sch.start_time}–{sch.end_time}"
        else: status='График не указан'
        rows.append({'user':u,'schedule':sch,'absence':ab,'status':status})
    return render_template('team_today.html',rows=rows,day=day)

@app.route('/week')
@login_required
def team_week():
    base=date.fromisoformat(request.args.get('start',date.today().isoformat()))
    monday=base-timedelta(days=base.weekday()); days=[monday+timedelta(days=i) for i in range(7)]
    users=User.query.filter_by(active=True).order_by(User.full_name).all()
    schedules=DailySchedule.query.filter(DailySchedule.work_date>=days[0],DailySchedule.work_date<=days[-1]).all()
    by={(x.user_id,x.work_date):x for x in schedules}
    absences=AbsenceRequest.query.filter(
        AbsenceRequest.status=='approved',
        AbsenceRequest.date_start<=days[-1],
        db.or_(AbsenceRequest.date_end.is_(None), AbsenceRequest.date_end>=days[0])
    ).all()
    def state(u,d):
        a=next((x for x in absences if x.user_id==u.id and x.date_start<=d and (x.date_end is None or x.date_end>=d)),None)
        if a:return {'vacation':'Отпуск','sick_leave':'Больничный','day_off':'Выходной'}.get(a.absence_type,'Отсутствует')
        x=by.get((u.id,d))
        if x:return 'Выходной' if x.is_day_off else f'{x.start_time}–{x.end_time}'
        return 'Не задано'
    return render_template('team_week.html',days=days,users=users,state=state,prev=days[0]-timedelta(days=7),nxt=days[0]+timedelta(days=7))

@app.route('/month')
@login_required
def month_summary():
    today=date.today(); year=int(request.args.get('year',today.year)); month=int(request.args.get('month',today.month))
    start=date(year,month,1); end=date(year+1,1,1) if month==12 else date(year,month+1,1)
    users=User.query.filter_by(active=True).order_by(User.full_name).all()
    rows=[]
    for u in users:
        q=DailySchedule.query.filter(DailySchedule.user_id==u.id,DailySchedule.work_date>=start,DailySchedule.work_date<end).all()
        workdays=sum(1 for x in q if not x.is_day_off)
        planned=0
        for x in q:
            if x.is_day_off: continue
            try:
                ps=x.start_time.split(':'); pe=x.end_time.split(':')
                planned += (int(pe[0])*60+int(pe[1])) - (int(ps[0])*60+int(ps[1]))
            except Exception:
                pass

        absq=AbsenceRequest.query.filter(
            AbsenceRequest.user_id==u.id,
            AbsenceRequest.status=='approved',
            AbsenceRequest.date_start<end,
            db.or_(AbsenceRequest.date_end.is_(None), AbsenceRequest.date_end>=start)
        ).all()

        def _abs_days(a, first, last):
            e = a.date_end or (last - timedelta(days=1))
            return max(0, (min(e, last - timedelta(days=1)) - max(a.date_start, first)).days + 1)

        rows.append({
            'user':u,'days':workdays,'hours':round(planned/60,1),
            'vacation':sum(_abs_days(a,start,end) for a in absq if a.absence_type=='vacation'),
            'sick':sum(_abs_days(a,start,end) for a in absq if a.absence_type=='sick_leave')
        })
    return render_template('team_month.html',rows=rows,year=year,month=month,
        month_name=['','Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'][month],
        prev=(date(year,month,1)-timedelta(days=1)),nxt=end)

@app.route('/leave',methods=['GET','POST'])
@login_required
def leave_requests():
    if request.method=='POST':
        try:
            kind=request.form.get('absence_type','')
            start=date.fromisoformat(request.form.get('date_start',''))
            end_str=request.form.get('date_end','').strip()
            end=date.fromisoformat(end_str) if end_str else None

            if kind not in ('vacation','sick_leave','day_off'):
                raise ValueError('Неверный тип отсутствия')
            if end and end < start:
                raise ValueError('Дата окончания раньше начала')
            if kind != 'sick_leave' and not end:
                flash('Укажи дату окончания. Только для больничного можно оставить пустой.','error')
                return redirect(url_for('leave_requests'))

            target=int(request.form.get('user_id') or current_user.id) if current_user.role=='admin' else current_user.id
            req=AbsenceRequest(
                user_id=target, absence_type=kind,
                date_start=start, date_end=end,
                reason=request.form.get('reason','').strip(),
                status='approved' if current_user.role=='admin' else 'pending',
                reviewed_by=current_user.id if current_user.role=='admin' else None,
                reviewed_at=now() if current_user.role=='admin' else None,
            )
            uploaded=request.files.get('attachment')
            if uploaded and uploaded.filename:
                safe=secure_filename(uploaded.filename)
                if safe:
                    folder=os.path.join(app.config['UPLOAD_FOLDER'],'absence')
                    os.makedirs(folder,exist_ok=True)
                    stored=f'{secrets.token_hex(12)}_{safe}'
                    uploaded.save(os.path.join(folder,stored))
                    req.attachment_name=safe
                    req.attachment_path=f'absence/{stored}'
            db.session.add(req)
            audit('create','AbsenceRequest',new=f'{kind}:{start}:{end}')
            db.session.commit()
            flash('Заявка сохранена.','success')
        except (ValueError,TypeError) as e:
            flash(str(e) or 'Проверь тип отсутствия и даты.','error')
        return redirect(url_for('leave_requests'))

    query=AbsenceRequest.query if current_user.role=='admin' else AbsenceRequest.query.filter_by(user_id=current_user.id)
    requests_list=query.order_by(AbsenceRequest.created_at.desc()).limit(300).all()
    users=User.query.filter_by(active=True).order_by(User.full_name).all() if current_user.role=='admin' else []
    return render_template('leave.html',requests=requests_list,users=users)

@app.route('/leave/<int:request_id>/attachment')
@login_required
def leave_attachment(request_id):
    item=db.session.get(AbsenceRequest,request_id)
    if not item: abort(404)
    if current_user.role!='admin' and item.user_id!=current_user.id: abort(403)
    if not item.attachment_path: abort(404)
    return send_from_directory(app.config['UPLOAD_FOLDER'], item.attachment_path, as_attachment=True, download_name=item.attachment_name or 'attachment')

@app.route('/leave/<int:req_id>/review',methods=['POST'])
@admin_required
def review_leave(req_id):
    item=AbsenceRequest.query.get_or_404(req_id); action=request.form.get('action')
    if action not in ('approved','rejected'): abort(400)
    item.status=action; item.reviewed_by=current_user.id; item.reviewed_at=now()
    audit(action,'AbsenceRequest',item.id); db.session.commit()
    flash('Решение сохранено.','success'); return redirect(url_for('leave_requests'))

@app.route('/schedule',methods=['GET','POST'])
@login_required
def personal_schedule():
    if request.method=='POST':
        try:
            d=date.fromisoformat(request.form.get('work_date',''))
            off=request.form.get('is_day_off')=='on'
            st=request.form.get('start_time','09:00')
            en=request.form.get('end_time','18:00')
            if not off and (len(st)!=5 or len(en)!=5 or en<=st):
                raise ValueError()

            item=DailySchedule.query.filter_by(user_id=current_user.id,work_date=d).first()
            if not item:
                item=DailySchedule(user_id=current_user.id,work_date=d)
                db.session.add(item)

            item.start_time=st
            item.end_time=en
            item.is_day_off=off
            item.note=request.form.get('note','').strip()[:500]
            item.actual_start = request.form.get('actual_start', '').strip()
            item.actual_end = request.form.get('actual_end', '').strip()
            try:
                item.efficiency = max(0.0, min(1.0, float(request.form.get('efficiency', '100')) / 100.0))
            except (ValueError, TypeError):
                item.efficiency = 1.0
            db.session.commit()
            flash('График сохранён.','success')
        except ValueError:
            flash('Проверь дату и время.', 'error')
        return redirect(url_for('personal_schedule'))

    start=date.today()-timedelta(days=date.today().weekday())
    days=[start+timedelta(days=i) for i in range(14)]
    entries=DailySchedule.query.filter_by(user_id=current_user.id)\
        .filter(DailySchedule.work_date>=days[0], DailySchedule.work_date<=days[-1])\
        .order_by(DailySchedule.work_date).all()
    by_date={x.work_date:x for x in entries}
    return render_template('personal_schedule.html', days=days, by_date=by_date, now_date=date.today())

@app.route('/api/employees/search')
@login_required
def employee_search():
    q=(request.args.get('q') or '').strip()
    if not q:return jsonify(results=[])
    rows=User.query.filter_by(active=True).filter(db.or_(User.full_name.ilike(f'%{q}%'),User.username.ilike(f'%{q}%'),User.position.ilike(f'%{q}%'))).order_by(User.full_name).limit(50).all()
    return jsonify(results=[{'id':u.id,'name':u.full_name,'position':u.position or '', 'department':u.department.name if u.department else ''} for u in rows])

@app.route('/employees')
@login_required
def employee_directory():
    q=(request.args.get('q') or '').strip()
    query=User.query.filter_by(active=True)
    if q: query=query.filter(db.or_(User.full_name.ilike(f'%{q}%'),User.username.ilike(f'%{q}%'),User.position.ilike(f'%{q}%')))
    users=query.order_by(User.full_name).all()
    return render_template('employee_directory.html',users=users,q=q)

@app.route('/health')
def health(): return jsonify(status='ok',service='WorkMonitor',database='ok')

with app.app_context():
    db.create_all()
    try:
        db.session.execute(db.text("UPDATE user SET role='employee' WHERE role NOT IN ('admin','employee')"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        cols = {row[1] for row in db.session.execute(db.text('PRAGMA table_info(user)')).fetchall()}
        if 'work_rate' not in cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN work_rate VARCHAR(20) NOT NULL DEFAULT 'full'"))
            db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        cols = {row[1] for row in db.session.execute(db.text('PRAGMA table_info(daily_schedule)')).fetchall()}
        for col, sql in [
            ('tasks_text', "TEXT DEFAULT ''"),
            ('fact_text', "TEXT DEFAULT ''"),
            ('confidence', "FLOAT DEFAULT 1.0"),
            ('source_message_id', "VARCHAR(100) DEFAULT ''"),
            ('actual_start', "VARCHAR(5) DEFAULT ''"),
            ('actual_end', "VARCHAR(5) DEFAULT ''"),
            ('efficiency', "FLOAT DEFAULT 1.0"),
        ]:
            if col not in cols:
                db.session.execute(db.text(f"ALTER TABLE daily_schedule ADD COLUMN {col} {sql}"))
                db.session.commit()
    except Exception:
        db.session.rollback()

if __name__=='__main__':
    app.run(host=os.getenv('HOST','0.0.0.0'),port=int(os.getenv('PORT','5000')),debug=os.getenv('FLASK_DEBUG','0')=='1')