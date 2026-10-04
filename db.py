"""Работа с базой данных SQLite (aiosqlite). Весь SQL бота живёт здесь."""
import os

import aiosqlite

DB_FILE = os.getenv("DB_FILE", "bot_data.db")

ROLE_WORKER = "worker"
ROLE_MANAGER = "manager"
ROLE_BRANCH_ADMIN = "branch_admin"
ROLE_SUPERADMIN = "superadmin"

MANAGEMENT_ROLES = (ROLE_MANAGER, ROLE_BRANCH_ADMIN, ROLE_SUPERADMIN)


def _connect():
    return aiosqlite.connect(DB_FILE)


async def init_db(first_superadmin_id: int):
    """Создание таблиц базы данных и регистрация первого суперадмина."""
    async with _connect() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                branch TEXT NOT NULL,
                position TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'worker'
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

        if first_superadmin_id:
            async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (first_superadmin_id,)) as cursor:
                user = await cursor.fetchone()
            if user:
                await db.execute("UPDATE users SET role = ? WHERE telegram_id = ?", (ROLE_SUPERADMIN, first_superadmin_id))
            else:
                await db.execute(
                    "INSERT INTO users (telegram_id, full_name, branch, position, role) VALUES (?, ?, ?, ?, ?)",
                    (first_superadmin_id, "Главный Администратор", "Гугл", "Суперадмин", ROLE_SUPERADMIN)
                )
        await db.commit()


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


# ---------------------------------------------------------------------
# Расписание
# ---------------------------------------------------------------------
async def get_user_schedule(user_id: int):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT date_str, shift_time FROM schedules WHERE user_id = ? ORDER BY date_str", (user_id,)
        ) as cursor:
            return await cursor.fetchall()


async def get_branch_schedule(branch: str):
    """Сотрудники филиала и их смены одним запросом: {user_row: [schedule_rows]}."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.telegram_id, u.full_name, u.position, s.date_str, s.shift_time
            FROM users u
            LEFT JOIN schedules s ON s.user_id = u.telegram_id
            WHERE u.branch = ?
            ORDER BY u.telegram_id, s.id
        """, (branch,)) as cursor:
            rows = await cursor.fetchall()

    employees = {}
    for r in rows:
        emp = employees.setdefault(r['telegram_id'], {"full_name": r['full_name'], "position": r['position'], "shifts": []})
        if r['date_str'] is not None:
            emp["shifts"].append((r['date_str'], r['shift_time']))
    return list(employees.values())


async def set_shift(user_id: int, date_str: str, shift_time: str):
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO schedules (user_id, date_str, shift_time) VALUES (?, ?, ?)",
            (user_id, date_str, shift_time)
        )
        await db.commit()


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
    """Принять/отклонить заявку. Возвращает заявку или None, если она уже обработана."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM schedule_requests WHERE id = ?", (req_id,)) as cursor:
            req = await cursor.fetchone()
        if not req or req['status'] != 'PENDING':
            return None

        new_status = 'APPROVED' if approve else 'REJECTED'
        await db.execute("UPDATE schedule_requests SET status = ? WHERE id = ?", (new_status, req_id))
        if approve:
            await db.execute(
                "INSERT OR REPLACE INTO schedules (user_id, date_str, shift_time) VALUES (?, ?, ?)",
                (req['user_id'], req['date_str'], req['desired_time'])
            )
        await db.commit()
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
