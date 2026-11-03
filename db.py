"""Работа с базой данных SQLite (aiosqlite). Весь SQL бота живёт здесь."""
import asyncio
import logging
import os
import sqlite3
from datetime import date
from pathlib import Path

import aiosqlite

import shifts
from shifts import parse_date_nearest

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def _resolve_db_file(value: str | None) -> str:
    """Путь к базе. Относительный путь (и путь по умолчанию) — от папки с db.py,
    а не от текущей папки: иначе бот, запущенный из другого места, создал бы
    новую пустую базу рядом с собой."""
    path = Path(value) if value else Path("bot_data.db")
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path)


DB_FILE = _resolve_db_file(os.getenv("DB_FILE"))

ROLE_WORKER = "worker"
ROLE_MANAGER = "manager"
ROLE_BRANCH_ADMIN = "branch_admin"
ROLE_SUPERADMIN = "superadmin"

MANAGEMENT_ROLES = (ROLE_MANAGER, ROLE_BRANCH_ADMIN, ROLE_SUPERADMIN)

# Значения schedule_requests.status
REQUEST_PENDING = "PENDING"
REQUEST_APPROVED = "APPROVED"
REQUEST_REJECTED = "REJECTED"
REQUEST_SUPERSEDED = "SUPERSEDED"  # менеджер сам назначил смену на эту дату

# Значения users.status
USER_PENDING = "pending"    # зарегистрировался, ждёт подтверждения Администратора
USER_ACTIVE = "active"      # работает
USER_INACTIVE = "inactive"  # уволен / отклонён: доступ закрыт, история остаётся

# Филиал первого Администратора: ему филиал не важен, он выбирает его кнопками
NO_BRANCH = "—"

SCHEMA_VERSION = 2

_UPSERT_SHIFT_SQL = (
    "INSERT INTO schedules (user_id, date_str, shift_time) VALUES (?, ?, ?) "
    "ON CONFLICT(user_id, date_str) DO UPDATE SET shift_time = excluded.shift_time"
)


def _connect():
    return aiosqlite.connect(DB_FILE)


async def init_db(first_superadmin_id: int, today: date | None = None):
    """Создание таблиц базы данных и регистрация первого суперадмина.

    today — день миграции (по умолчанию сегодня); параметр нужен тестам.
    """
    today = today or shifts._today()
    await _backup_before_migration(today)
    async with _connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                branch TEXT NOT NULL,
                position TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'worker',
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date_str TEXT NOT NULL,
                shift_time TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date_str TEXT NOT NULL,
                desired_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES ('is_paused', '0')"
        )
        await _migrate(db, today)

        if first_superadmin_id:
            async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (first_superadmin_id,)) as cursor:
                user = await cursor.fetchone()
            if user:
                await db.execute(
                    "UPDATE users SET role = ?, status = ? WHERE telegram_id = ?",
                    (ROLE_SUPERADMIN, USER_ACTIVE, first_superadmin_id)
                )
            else:
                await db.execute(
                    "INSERT INTO users (telegram_id, full_name, branch, position, role, status) VALUES (?, ?, ?, ?, ?, ?)",
                    (first_superadmin_id, "Главный Администратор", NO_BRANCH, "Суперадмин", ROLE_SUPERADMIN, USER_ACTIVE)
                )
        await db.commit()


def _backup_path(today: date) -> Path:
    """bot_data.db.bak-ГГГГММДД рядом с базой; существующую копию не перезаписываем."""
    base = f"{DB_FILE}.bak-{today:%Y%m%d}"
    path, n = Path(base), 0
    while path.exists():
        n += 1
        path = Path(f"{base}-{n}")
    return path


def _sqlite_backup(src: str, dst: str):
    """Копия базы средствами SQLite (корректна и при открытой базе). Вызывать в потоке."""
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


async def _backup_before_migration(today: date) -> Path | None:
    """Если база старая (user_version < SCHEMA_VERSION и в ней есть таблицы) — копия до миграции."""
    if not Path(DB_FILE).exists():
        return None
    async with _connect() as db:
        async with db.execute("PRAGMA user_version") as cursor:
            version = (await cursor.fetchone())[0]
        async with db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1") as cursor:
            has_tables = await cursor.fetchone() is not None
    if version >= SCHEMA_VERSION or not has_tables:
        return None
    target = _backup_path(today)
    await asyncio.to_thread(_sqlite_backup, DB_FILE, str(target))
    logger.info("Перед обновлением базы сделана копия: %s", target)
    return target


async def _migrate(db, today: date):
    """Обновление схемы старой базы. Версия хранится в PRAGMA user_version."""
    async with db.execute("PRAGMA user_version") as cursor:
        version = (await cursor.fetchone())[0]

    if version < 1:
        # 1. Даты «ДД.ММ» → «ГГГГ-ММ-ДД», год — ближайший ко дню миграции.
        #    Нераспознанные значения не трогаем.
        for select_sql, update_sql in (
            ("SELECT id, date_str FROM schedules", "UPDATE schedules SET date_str = ? WHERE id = ?"),
            ("SELECT id, date_str FROM schedule_requests", "UPDATE schedule_requests SET date_str = ? WHERE id = ?"),
        ):
            async with db.execute(select_sql) as cursor:
                rows = await cursor.fetchall()
            for row_id, date_str in rows:
                iso = parse_date_nearest(date_str, today)
                if iso and iso != date_str:
                    await db.execute(update_sql, (iso, row_id))
        # 2. Одна смена на сотрудника в день: оставляем последнюю запись
        await db.execute("""
            DELETE FROM schedules WHERE id NOT IN (
                SELECT MAX(id) FROM schedules GROUP BY user_id, date_str
            )
        """)
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_user_date ON schedules(user_id, date_str)"
        )
        await db.execute("PRAGMA user_version = 1")

    if version < 2:
        # Статус пользователя. Все, кто уже есть в базе, — работают (active).
        async with db.execute("PRAGMA table_info(users)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "status" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        await db.execute("UPDATE users SET status = ?", (USER_ACTIVE,))
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_branch_status ON users(branch, status)")
        await db.execute("PRAGMA user_version = 2")


# ---------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------
async def get_user(telegram_id: int):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            return await cursor.fetchone()


async def add_user(telegram_id: int, full_name: str, branch: str, position: str, role: str = ROLE_WORKER):
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (telegram_id, full_name, branch, position, role) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, full_name, branch, position, role)
        )
        await db.commit()


async def update_user_role(telegram_id: int, role: str):
    async with _connect() as db:
        await db.execute("UPDATE users SET role = ? WHERE telegram_id = ?", (role, telegram_id))
        await db.commit()


async def count_superadmins() -> int:
    async with _connect() as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE role = ?", (ROLE_SUPERADMIN,)) as cursor:
            res = await cursor.fetchone()
            return res[0] if res else 0


async def get_all_users():
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cursor:
            return await cursor.fetchall()


async def get_branch_users(branch: str):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE branch = ?", (branch,)) as cursor:
            return await cursor.fetchall()


async def get_managers_and_admins(branch: str):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE branch = ? AND role IN (?, ?)",
            (branch, ROLE_MANAGER, ROLE_BRANCH_ADMIN)
        ) as cursor:
            return await cursor.fetchall()


async def get_superadmins():
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE role = ?", (ROLE_SUPERADMIN,)) as cursor:
            return await cursor.fetchall()


async def get_request_recipients(branch: str):
    """Кому доставить заявку сотрудника: Менеджеры и Управляющие филиала,
    а если их в филиале нет — Администраторы (чтобы заявка не терялась)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE branch = ? AND role IN (?, ?)",
            (branch, ROLE_MANAGER, ROLE_BRANCH_ADMIN)
        ) as cursor:
            rows = await cursor.fetchall()
        if rows:
            return rows
        async with db.execute("SELECT * FROM users WHERE role = ?", (ROLE_SUPERADMIN,)) as cursor:
            return await cursor.fetchall()


# ---------------------------------------------------------------------
# Расписание
# ---------------------------------------------------------------------
async def get_user_schedule(user_id: int, date_from: str | None = None, date_to: str | None = None):
    """Смены сотрудника по дате. date_from/date_to — «ГГГГ-ММ-ДД» включительно, None — без границы."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT date_str, shift_time FROM schedules WHERE user_id = ? "
            "AND (? IS NULL OR date_str >= ?) AND (? IS NULL OR date_str <= ?) "
            "ORDER BY date_str",
            (user_id, date_from, date_from, date_to, date_to)
        ) as cursor:
            return await cursor.fetchall()


async def get_branch_schedule(branch: str, date_from: str | None = None, date_to: str | None = None):
    """Сотрудники филиала и их смены одним запросом: [{full_name, position, shifts}].

    Фильтр по датам стоит в условии JOIN, поэтому сотрудник без смен
    в этом периоде всё равно попадает в список (с пустым shifts).
    """
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.telegram_id, u.full_name, u.position, s.date_str, s.shift_time
            FROM users u
            LEFT JOIN schedules s ON s.user_id = u.telegram_id
                AND (? IS NULL OR s.date_str >= ?)
                AND (? IS NULL OR s.date_str <= ?)
            WHERE u.branch = ?
            ORDER BY u.telegram_id, s.date_str
        """, (date_from, date_from, date_to, date_to, branch)) as cursor:
            rows = await cursor.fetchall()

    employees = {}
    for r in rows:
        emp = employees.setdefault(r['telegram_id'], {"full_name": r['full_name'], "position": r['position'], "shifts": []})
        if r['date_str'] is not None:
            emp["shifts"].append((r['date_str'], r['shift_time']))
    return list(employees.values())


async def set_shift(user_id: int, date_str: str, shift_time: str) -> int:
    """Назначить смену. Ожидающие заявки сотрудника на эту дату закрываются
    (SUPERSEDED), чтобы их «Принять» не перетёрло смену от менеджера.
    Возвращает число закрытых заявок."""
    async with _connect() as db:
        await db.execute(_UPSERT_SHIFT_SQL, (user_id, date_str, shift_time))
        cursor = await db.execute(
            "UPDATE schedule_requests SET status = ? WHERE user_id = ? AND date_str = ? AND status = ?",
            (REQUEST_SUPERSEDED, user_id, date_str, REQUEST_PENDING)
        )
        await db.commit()
        return cursor.rowcount


# ---------------------------------------------------------------------
# Заявки на изменение расписания
# ---------------------------------------------------------------------
async def create_request(user_id: int, date_str: str, desired_time: str) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO schedule_requests (user_id, date_str, desired_time, status) VALUES (?, ?, ?, 'PENDING')",
            (user_id, date_str, desired_time)
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_requests(branch: str):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT sr.id, sr.user_id, sr.date_str, sr.desired_time, u.full_name, u.position
            FROM schedule_requests sr
            JOIN users u ON sr.user_id = u.telegram_id
            WHERE u.branch = ? AND sr.status = 'PENDING'
        """, (branch,)) as cursor:
            return await cursor.fetchall()


async def get_request_branch(req_id: int):
    """Филиал сотрудника, подавшего заявку (None, если заявки нет)."""
    async with _connect() as db:
        async with db.execute("""
            SELECT u.branch FROM schedule_requests sr
            JOIN users u ON sr.user_id = u.telegram_id
            WHERE sr.id = ?
        """, (req_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def decide_request(req_id: int, approve: bool):
    """Принять/отклонить заявку. Возвращает заявку (Row) или None, если её нет
    или она уже обработана.

    Два менеджера могут нажать кнопки одновременно: статус меняется условным
    UPDATE ... AND status = 'PENDING' внутри BEGIN IMMEDIATE, и смену пишет
    только тот, чей UPDATE изменил строку.
    """
    new_status = REQUEST_APPROVED if approve else REQUEST_REJECTED
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "UPDATE schedule_requests SET status = ? WHERE id = ? AND status = ?",
                (new_status, req_id, REQUEST_PENDING)
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            async with db.execute("SELECT * FROM schedule_requests WHERE id = ?", (req_id,)) as cursor:
                req = await cursor.fetchone()
            if approve:
                await db.execute(_UPSERT_SHIFT_SQL, (req['user_id'], req['date_str'], req['desired_time']))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return req


# ---------------------------------------------------------------------
# Пауза бота
# ---------------------------------------------------------------------
async def is_bot_paused() -> bool:
    async with _connect() as db:
        async with db.execute("SELECT value FROM system_settings WHERE key = 'is_paused'") as cursor:
            res = await cursor.fetchone()
            return res[0] == '1' if res else False


async def set_bot_paused(paused: bool):
    val = '1' if paused else '0'
    async with _connect() as db:
        await db.execute("UPDATE system_settings SET value = ? WHERE key = 'is_paused'", (val,))
        await db.commit()
