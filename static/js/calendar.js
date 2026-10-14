/**
 * WorkMonitor — Календарь.
 * Отображение месяца/недели/дня, работа с событиями,
 * мини-календарь, панель дня, редактирование дня и события.
 */
(() => {
    'use strict';

    // =====================================================================
    // Константы
    // =====================================================================
    const MONTHS_RU = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                       'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];

    const DAYS_RU = ['Воскресенье', 'Понедельник', 'Вторник', 'Среда',
                     'Четверг', 'Пятница', 'Суббота'];

    const CATEGORY_ICONS = {
        work: '▣',
        vacation: '♡',
        sick_leave: '✚',
        day_off: '☼',
        fact: '✓',
        meeting: '◈',
        study: '✦',
    };

    const STATUS_LABELS = {
        planned: 'Запланировано',
        in_progress: 'В процессе',
        completed: 'Выполнено',
        cancelled: 'Отменено',
        approved: 'Согласовано',
        confirmed: 'Подтверждено',
        new: 'Новая',
        review: 'На проверке',
        blocked: 'Заблокирована',
    };

    // =====================================================================
    // Состояние
    // =====================================================================
    const state = {
        cursor: new Date(),
        selected: new Date(),
        view: 'month',
        events: [],
        dayTasks: [],
    };
    state.cursor.setDate(1);

    // =====================================================================
    // Утилиты
    // =====================================================================
    const pad = n => String(n).padStart(2, '0');

    const dateKey = d =>
        `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

    const toLocalInput = d =>
        `${dateKey(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;

    const parseDate = s =>
        new Date(s.length === 10 ? `${s}T12:00:00` : s);

    const sameDay = (a, b) => dateKey(a) === dateKey(b);

    const startOfWeek = d => {
        const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        x.setDate(x.getDate() - (x.getDay() + 6) % 7);
        return x;
    };

    const formatTime = s =>
        new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' })
            .format(parseDate(s));

    // =====================================================================
    // DOM
    // =====================================================================
    const $ = sel => document.querySelector(sel);
    const $$ = sel => document.querySelectorAll(sel);

    // =====================================================================
    // Toast
    // =====================================================================
    function toast(message) {
        const el = $('#toast');
        if (!el) return;
        el.textContent = message;
        el.classList.add('show');
        clearTimeout(toast.timer);
        toast.timer = setTimeout(() => el.classList.remove('show'), 3000);
    }

    // =====================================================================
    // Загрузка событий
    // =====================================================================
    async function loadEvents() {
        const first = new Date(state.cursor.getFullYear(), state.cursor.getMonth(), 1);
        const nextMonth = new Date(state.cursor.getFullYear(), state.cursor.getMonth() + 1, 1);

        const from = new Date(first);
        from.setDate(from.getDate() - 8);
        const to = new Date(nextMonth);
        to.setDate(to.getDate() + 8);

        try {
            const url = `/api/events?start=${encodeURIComponent(from.toISOString())}&end=${encodeURIComponent(to.toISOString())}`;
            const response = await fetch(url);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            state.events = await response.json();
        } catch (err) {
            console.error('loadEvents error:', err);
            toast('Не удалось загрузить события. Обнови страницу.');
            state.events = state.events || [];
        } finally {
            render();
        }
    }

    async function loadDayTasks() {
        const key = dateKey(state.selected);
        try {
            const res = await fetch(`/api/day/${key}/tasks`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            state.dayTasks = await res.json();
        } catch (err) {
            console.error('loadDayTasks error:', err);
            state.dayTasks = [];
        }
        renderDayTasks();
    }

    function filteredEvents() {
        const filter = $('#categoryFilter');
        if (!filter) return state.events;
        if (filter.value === 'all') return state.events;
        return state.events.filter(e => e.category === filter.value);
    }

    // =====================================================================
    // Рендер — общий
    // =====================================================================
    function render() {
        const monthTitle = $('#monthTitle');
        const miniTitle = $('#miniMonthTitle');
        const title = `${MONTHS_RU[state.cursor.getMonth()]} ${state.cursor.getFullYear()}`;
        if (monthTitle) monthTitle.textContent = title;
        if (miniTitle) miniTitle.textContent = title;

        renderMain();
        renderMini();
        renderDetails();
    }

    function renderDayTasks() {
        const list = document.getElementById('dayTasks');
        const countEl = document.getElementById('tasksCount');
        if (!list) return;

        list.innerHTML = '';

        const total = state.dayTasks.length;
        const doneCount = state.dayTasks.filter(t => t.done).length;
        if (countEl) {
            countEl.textContent = total ? `${doneCount} / ${total}` : '';
        }

        state.dayTasks.forEach(t => {
            const li = document.createElement('li');
            li.className = 'day-task' + (t.done ? ' done' : '');

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = !!t.done;
            cb.addEventListener('change', async () => {
                try {
                    await fetch(`/api/tasks/${t.id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ done: cb.checked }),
                    });
                    t.done = cb.checked;
                    li.classList.toggle('done', cb.checked);
                    renderDayTasks();
                } catch (err) {
                    toast('Не удалось обновить задачу');
                    cb.checked = !cb.checked;
                }
            });

            const span = document.createElement('span');
            span.className = 'task-text';
            span.textContent = t.text;

            const del = document.createElement('button');
            del.className = 'task-delete';
            del.type = 'button';
            del.title = 'Удалить';
            del.textContent = '✕';
            del.addEventListener('click', async () => {
                if (!confirm('Удалить задачу?')) return;
                try {
                    await fetch(`/api/tasks/${t.id}`, { method: 'DELETE' });
                    state.dayTasks = state.dayTasks.filter(x => x.id !== t.id);
                    renderDayTasks();
                } catch (err) {
                    toast('Не удалось удалить');
                }
            });

            li.append(cb, span, del);
            list.append(li);
        });
    }

    // =====================================================================
    // Основной календарь
    // =====================================================================
    function renderMain() {
        const grid = $('#calendarGrid');
        if (!grid) return;
        grid.innerHTML = '';

        let first, last;
        if (state.view === 'month') {
            first = startOfWeek(new Date(state.cursor.getFullYear(), state.cursor.getMonth(), 1));
            last = new Date(first);
            last.setDate(first.getDate() + 41);
        } else if (state.view === 'week') {
            first = startOfWeek(state.selected);
            last = new Date(first);
            last.setDate(first.getDate() + 6);
        } else {
            first = new Date(state.selected);
            last = new Date(state.selected);
        }

        const days = [];
        for (let d = new Date(first); d <= last; d.setDate(d.getDate() + 1)) {
            days.push(new Date(d));
        }

        grid.style.gridTemplateColumns = `repeat(${state.view === 'day' ? 1 : 7},minmax(0,1fr))`;

        const weekdayRow = $('.weekday-row');
        if (weekdayRow) weekdayRow.style.display = state.view === 'day' ? 'none' : 'grid';

        days.forEach(d => {
            const cell = document.createElement('div');
            cell.className = 'calendar-cell';
            if (state.view === 'month' && d.getMonth() !== state.cursor.getMonth()) {
                cell.classList.add('outside');
            }
            if (sameDay(d, state.selected)) cell.classList.add('selected');
            if (sameDay(d, new Date())) cell.classList.add('today');
            cell.dataset.date = dateKey(d);

            const num = document.createElement('span');
            num.className = 'date-number';
            num.textContent = d.getDate();
            cell.append(num);

            const dayEvents = filteredEvents().filter(e => sameDay(parseDate(e.start_at), d));
            const maxVisible = state.view === 'week' ? 30 : 3;

            dayEvents.slice(0, maxVisible).forEach(e => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = `event-chip ${e.category} ${e.status}`;
                chip.title = `${formatTime(e.start_at)} · ${e.title}`;
                const icon = CATEGORY_ICONS[e.category] || '▣';
                chip.textContent = `${icon} ${e.title}`;
                // ВАЖНО: чип больше не перехватывает клик —
                // событие всплывёт до ячейки и откроет панель дня.
                cell.append(chip);
            });

            if (dayEvents.length > maxVisible) {
                const more = document.createElement('div');
                more.className = 'event-more';
                more.textContent = `+ ещё ${dayEvents.length - maxVisible}`;
                cell.append(more);
            }

            // Клик по любой точке ячейки (номер, «План», «Факт», пустое место)
            // открывает информацию об этом дне.
            const selectThisDay = () => {
                state.selected = new Date(d);
                if (state.view === 'month' && d.getMonth() !== state.cursor.getMonth()) {
                    state.cursor = new Date(d.getFullYear(), d.getMonth(), 1);
                }
                render();
            };

            cell.addEventListener('click', selectThisDay);
            num.addEventListener('click', selectThisDay);
            cell.style.cursor = 'pointer';

            grid.append(cell);
        });
    }

    // =====================================================================
    // Мини-календарь
    // =====================================================================
    function renderMini() {
        const mini = $('#miniGrid');
        if (!mini) return;
        mini.innerHTML = '';

        const first = startOfWeek(new Date(state.cursor.getFullYear(), state.cursor.getMonth(), 1));
        const events = filteredEvents();

        for (let i = 0; i < 42; i++) {
            const d = new Date(first);
            d.setDate(first.getDate() + i);

            const b = document.createElement('button');
            b.type = 'button';
            b.textContent = d.getDate();
            if (d.getMonth() !== state.cursor.getMonth()) b.classList.add('outside');
            if (sameDay(d, state.selected)) b.classList.add('selected');
            if (events.some(e => sameDay(parseDate(e.start_at), d))) b.classList.add('has-events');

            b.addEventListener('click', () => {
                state.selected = new Date(d);
                if (d.getMonth() !== state.cursor.getMonth()) {
                    state.cursor = new Date(d.getFullYear(), d.getMonth(), 1);
                }
                render();
            });

            mini.append(b);
        }
    }

    // =====================================================================
    // Панель выбранного дня
    // =====================================================================
    function renderDetails() {
        const title = $('#selectedDateTitle');
        const weekday = $('#selectedWeekday');
        const countEl = $('#eventCount');
        const list = $('#dayEvents');
        const empty = $('#emptyState');
        if (!title || !list) return;

        const date = state.selected;

        title.textContent = `${date.getDate()} ${MONTHS_RU[date.getMonth()].toLowerCase()}`;
        if (weekday) weekday.textContent = DAYS_RU[date.getDay()];

        const events = filteredEvents()
            .filter(e => sameDay(parseDate(e.start_at), date))
            .sort((a, b) => a.start_at.localeCompare(b.start_at));

        const count = events.length;
        let word = 'событий';
        if (count === 1) word = 'событие';
        else if (count > 1 && count < 5) word = 'события';
        if (countEl) countEl.textContent = `${count} ${word}`;

        list.innerHTML = '';
        if (empty) empty.hidden = count > 0;

        events.forEach(e => {
            const card = document.createElement('article');
            card.className = `day-event cat-${e.category}`;

            const icon = document.createElement('div');
            icon.className = 'event-icon';
            icon.textContent = CATEGORY_ICONS[e.category] || '▣';

            const body = document.createElement('div');

            const meta = document.createElement('div');
            meta.className = 'event-meta';

            const time = document.createElement('span');
            time.textContent = `${formatTime(e.start_at)} – ${formatTime(e.end_at)}`;

            const status = document.createElement('span');
            status.textContent = STATUS_LABELS[e.status] || e.status;
            meta.append(time, status);

            const name = document.createElement('div');
            name.className = 'event-name';
            name.textContent = e.title;

            const desc = document.createElement('div');
            desc.className = 'event-description';
            desc.textContent = e.description || e.course || (e.user_name ?? '');

            body.append(meta, name, desc);
            card.append(icon, body);

            card.title = e.readonly
                ? 'Задача из списка — редактируй в разделе «Мой график»'
                : 'Нажми, чтобы изменить событие';
            card.style.cursor = e.readonly ? 'default' : 'pointer';

            if (!e.readonly) {
                card.addEventListener('click', () => openEdit(e));
            }

            list.append(card);
        });

        const editBtn = $('#editDayBtn');
        if (editBtn) {
            editBtn.dataset.selectedDate = dateKey(date);
        }

        // Подгружаем чек-лист задач для выбранного дня
        loadDayTasks();
    }

    // =====================================================================
    // Навигация
    // =====================================================================
    function move(delta) {
        if (state.view === 'day') {
            state.selected.setDate(state.selected.getDate() + delta);
            state.cursor = new Date(state.selected.getFullYear(), state.selected.getMonth(), 1);
        } else if (state.view === 'week') {
            state.selected.setDate(state.selected.getDate() + delta * 7);
            state.cursor = new Date(state.selected.getFullYear(), state.selected.getMonth(), 1);
        } else {
            state.cursor = new Date(state.cursor.getFullYear(), state.cursor.getMonth() + delta, 1);
        }
        render();
    }

    // =====================================================================
    // Диалог события
    // =====================================================================
    function openNew() {
        const dialog = $('#eventDialog');
        if (!dialog) return;

        const start = new Date(state.selected);
        start.setHours(18, 0, 0, 0);
        const end = new Date(start);
        end.setHours(start.getHours() + 1);

        $('#eventId').value = '';
        $('#dialogTitle').textContent = 'Новое событие';
        $('#eventTitle').value = '';
        $('#eventStart').value = toLocalInput(start);
        $('#eventEnd').value = toLocalInput(end);
        $('#eventCategory').value = 'work';
        $('#eventStatus').value = 'planned';
        $('#eventDescription').value = '';
        $('#deleteEventBtn').hidden = true;

        const userSelect = $('#eventUser');
        if (userSelect) {
            const app = $('#app');
            if (app) userSelect.value = String(app.dataset.userId);
        }

        dialog.showModal();
    }

    function openEdit(e) {
        if (e.readonly) {
            toast('Это задача из раздела «Мой график». Измени её там.');
            return;
        }
        const dialog = $('#eventDialog');
        if (!dialog) return;

        $('#eventId').value = e.id;
        $('#dialogTitle').textContent = 'Редактировать событие';
        $('#eventTitle').value = e.title;
        $('#eventStart').value = toLocalInput(parseDate(e.start_at));
        $('#eventEnd').value = toLocalInput(parseDate(e.end_at));
        $('#eventCategory').value = e.category;
        $('#eventStatus').value = e.status;
        $('#eventDescription').value = e.description || '';

        const userSelect = $('#eventUser');
        if (userSelect) userSelect.value = String(e.user_id);

        $('#deleteEventBtn').hidden = false;
        dialog.showModal();
    }

    async function saveEvent(ev) {
        ev.preventDefault();
        const id = $('#eventId').value;

        const payload = {
            title: $('#eventTitle').value.trim(),
            start_at: $('#eventStart').value,
            end_at: $('#eventEnd').value,
            category: $('#eventCategory').value,
            status: $('#eventStatus').value,
            description: $('#eventDescription').value,
        };

        const userSelect = $('#eventUser');
        if (userSelect) payload.user_id = userSelect.value;

        try {
            const res = await fetch(id ? `/api/events/${id}` : '/api/events', {
                method: id ? 'PATCH' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Не удалось сохранить');

            $('#eventDialog').close();
            state.selected = new Date(payload.start_at);
            state.cursor = new Date(state.selected.getFullYear(), state.selected.getMonth(), 1);
            await loadEvents();
            toast(id ? 'Событие обновлено' : 'Событие создано');
        } catch (err) {
            toast(err.message);
        }
    }

    async function deleteEvent() {
        const id = $('#eventId').value;
        if (!id || !confirm('Удалить это событие?')) return;

        try {
            const res = await fetch(`/api/events/${id}`, { method: 'DELETE' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Ошибка удаления');
            $('#eventDialog').close();
            await loadEvents();
            toast('Событие удалено');
        } catch (err) {
            toast(err.message);
        }
    }

    // =====================================================================
    // Диалог редактирования ДНЯ
    // =====================================================================
    async function openDayEditor() {
        const dayDialog = document.getElementById('dayDialog');
        const editBtn = document.getElementById('editDayBtn');
        if (!dayDialog) return;

        const dateStr = dayDialog.dataset.selectedDate
            || (editBtn && editBtn.dataset.selectedDate)
            || dateKey(state.selected);

        try {
            const res = await fetch(`/api/day/${dateStr}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();

            document.getElementById('dayDate').value = dateStr;
            document.getElementById('dayDialogTitle').textContent =
                new Intl.DateTimeFormat('ru-RU', {
                    day: 'numeric', month: 'long', year: 'numeric'
                }).format(parseDate(dateStr));

            document.getElementById('dayIsDayOff').checked = !!data.is_day_off;
            document.getElementById('dayStartTime').value = data.start_time || '09:00';
            document.getElementById('dayEndTime').value = data.end_time || '18:00';
            document.getElementById('dayActualStart').value = data.actual_start || '';
            document.getElementById('dayActualEnd').value = data.actual_end || '';
            document.getElementById('dayEfficiency').value = Math.round(data.efficiency || 100);
            document.getElementById('dayTasksText').value = data.tasks_text || '';
            document.getElementById('dayFactText').value = data.fact_text || '';
            document.getElementById('dayNote').value = data.note || '';

            dayDialog.showModal();
        } catch (err) {
            console.error('openDayEditor error:', err);
            toast('Не удалось загрузить данные дня.');
        }
    }

    async function saveDay(ev) {
        ev.preventDefault();
        const dateStr = document.getElementById('dayDate').value;

        const payload = {
            is_day_off: document.getElementById('dayIsDayOff').checked,
            start_time: document.getElementById('dayStartTime').value,
            end_time: document.getElementById('dayEndTime').value,
            actual_start: document.getElementById('dayActualStart').value,
            actual_end: document.getElementById('dayActualEnd').value,
            efficiency: document.getElementById('dayEfficiency').value,
            tasks_text: document.getElementById('dayTasksText').value,
            fact_text: document.getElementById('dayFactText').value,
            note: document.getElementById('dayNote').value,
        };

        try {
            const res = await fetch(`/api/day/${dateStr}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Не удалось сохранить');
            document.getElementById('dayDialog').close();
            await loadEvents();
            toast('✅ День сохранён');
        } catch (err) {
            toast(err.message);
        }
    }

    // =====================================================================
    // Инициализация
    // =====================================================================
    function init() {
        // ⛔ Если нет календаря на этой странице — выходим
        if (!document.getElementById('calendarGrid')) {
            return;
        }

        const prevBtn = $('#prevMonth');
        const nextBtn = $('#nextMonth');
        const todayBtn = $('#todayBtn');
        const miniPrev = $('#miniPrev');
        const miniNext = $('#miniNext');

        if (prevBtn) prevBtn.addEventListener('click', () => move(-1));
        if (nextBtn) nextBtn.addEventListener('click', () => move(1));
        if (miniPrev) miniPrev.addEventListener('click', () => move(-1));
        if (miniNext) miniNext.addEventListener('click', () => move(1));
        if (todayBtn) {
            todayBtn.addEventListener('click', () => {
                state.selected = new Date();
                state.cursor = new Date(state.selected.getFullYear(), state.selected.getMonth(), 1);
                render();
            });
        }

        $$('[data-view]').forEach(btn => {
            btn.addEventListener('click', () => {
                state.view = btn.dataset.view;
                $$('[data-view]').forEach(x => x.classList.toggle('selected', x === btn));
                render();
            });
        });

        const catFilter = $('#categoryFilter');
        if (catFilter) catFilter.addEventListener('change', render);

        const addBtn = $('#addEventBtn');
        const emptyAddBtn = $('#emptyAddBtn');
        if (addBtn) addBtn.addEventListener('click', openNew);
        if (emptyAddBtn) emptyAddBtn.addEventListener('click', openNew);

        const closeBtn = $('#closeDialog');
        const cancelBtn = $('#cancelDialog');
        const deleteBtn = $('#deleteEventBtn');
        const form = $('#eventForm');

        if (closeBtn) closeBtn.addEventListener('click', () => $('#eventDialog').close());
        if (cancelBtn) cancelBtn.addEventListener('click', () => $('#eventDialog').close());
        if (deleteBtn) deleteBtn.addEventListener('click', deleteEvent);
        if (form) form.addEventListener('submit', saveEvent);

        // Диалог ДНЯ
        const editDayBtn = $('#editDayBtn');
        if (editDayBtn) {
            editDayBtn.addEventListener('click', e => {
                e.preventDefault();
                openDayEditor();
            });
        }
        const closeDayBtn = document.getElementById('closeDayDialog');
        const cancelDayBtn = document.getElementById('cancelDayDialog');
        const dayForm = document.getElementById('dayForm');
        if (closeDayBtn) closeDayBtn.addEventListener('click', () => document.getElementById('dayDialog').close());
        if (cancelDayBtn) cancelDayBtn.addEventListener('click', () => document.getElementById('dayDialog').close());
        if (dayForm) dayForm.addEventListener('submit', saveDay);

        // Чек-лист задач: добавление новой задачи
        const addTaskForm = document.getElementById('addTaskForm');
        if (addTaskForm) {
            addTaskForm.addEventListener('submit', async ev => {
                ev.preventDefault();
                const input = document.getElementById('newTaskInput');
                if (!input) return;
                const text = input.value.trim();
                if (!text) return;

                const key = dateKey(state.selected);
                try {
                    const res = await fetch(`/api/day/${key}/tasks`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ text }),
                    });
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    const t = await res.json();
                    state.dayTasks.push(t);
                    input.value = '';
                    renderDayTasks();
                } catch (err) {
                    toast('Не удалось добавить задачу');
                }
            });
        }

        // Мобильное меню
        const mobileMenu = $('#mobileMenu');
        if (mobileMenu) {
            mobileMenu.addEventListener('click', () => {
                const sidebar = document.querySelector('.sidebar');
                if (sidebar) sidebar.classList.toggle('open');
            });
        }

        // Заглушки
        $$('[data-soon]').forEach(a => {
            a.addEventListener('click', e => {
                e.preventDefault();
                toast(a.dataset.soon);
            });
        });

        loadEvents();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();