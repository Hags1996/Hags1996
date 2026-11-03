"""db.py: пути к базе, резервная копия перед миграцией, гонки при решении заявок."""
import asyncio
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import db
from conftest import ADMIN_ID, run

WORKER, OTHER = 222, 223
MIGRATION_DAY = date(2026, 11, 3)


@pytest.fixture
def fresh_db():
    run(db.init_db(ADMIN_ID))
    run(db.add_user(WORKER, "Тестов Тест", "Абая", "Бариста"))
    run(db.add_user(OTHER, "Пробный Пётр", "Абая", "Бариста"))


def schedule(user_id):
    return [tuple(r) for r in run(db.get_user_schedule(user_id))]


def request_status(req_id):
    con = sqlite3.connect(db.DB_FILE)
    try:
        return con.execute("SELECT status FROM schedule_requests WHERE id = ?", (req_id,)).fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------
def test_default_db_file_is_next_to_module():
    base = Path(db.__file__).resolve().parent
    assert db._resolve_db_file(None) == str(base / "bot_data.db")
    assert db._resolve_db_file("") == str(base / "bot_data.db")
    assert db._resolve_db_file("other.db") == str(base / "other.db")
    assert db._resolve_db_file("/srv/data/bot.db") == "/srv/data/bot.db"


def test_tests_never_use_project_database(tmp_path):
    assert Path(db.DB_FILE).parent == tmp_path
    assert Path(db.DB_FILE) != Path(db.__file__).resolve().parent / "bot_data.db"


# ---------------------------------------------------------------------
# Резервная копия перед миграцией
# ---------------------------------------------------------------------
def make_old_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, branch TEXT NOT NULL,
                            position TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'worker');
        CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                                date_str TEXT NOT NULL, shift_time TEXT NOT NULL);
        CREATE TABLE schedule_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                                        date_str TEXT NOT NULL, desired_time TEXT NOT NULL,
                                        status TEXT NOT NULL DEFAULT 'PENDING');
        INSERT INTO users VALUES (5, 'Старый', 'Абая', 'Бариста', 'worker');
        INSERT INTO schedules (user_id, date_str, shift_time) VALUES (5, '01.10', '08:00 - 20:00');
    """)
    con.commit()
    con.close()


def test_backup_is_made_before_migration(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    make_old_db(path)
    monkeypatch.setattr(db, "DB_FILE", str(path))

    run(db.init_db(ADMIN_ID, today=MIGRATION_DAY))

    backup = tmp_path / "old.db.bak-20261103"
    assert backup.exists()
    con = sqlite3.connect(backup)
    assert con.execute("SELECT date_str FROM schedules").fetchall() == [("01.10",)]  # данные до миграции
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    con.close()

    run(db.init_db(ADMIN_ID, today=date(2026, 11, 4)))  # база уже обновлена — новых копий нет
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old.db", "old.db.bak-20261103"]


def test_existing_backup_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    make_old_db(path)
    (tmp_path / "old.db.bak-20261103").write_bytes(b"earlier copy")
    monkeypatch.setattr(db, "DB_FILE", str(path))

    run(db.init_db(ADMIN_ID, today=MIGRATION_DAY))

    assert (tmp_path / "old.db.bak-20261103").read_bytes() == b"earlier copy"
    assert (tmp_path / "old.db.bak-20261103-1").exists()


def test_new_database_gets_no_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "new.db"))
    run(db.init_db(ADMIN_ID, today=MIGRATION_DAY))
    assert [p.name for p in tmp_path.iterdir()] == ["new.db"]


# ---------------------------------------------------------------------
# Решение заявок
# ---------------------------------------------------------------------
async def _decide_concurrently(req_id, approvals):
    return await asyncio.gather(*(db.decide_request(req_id, a) for a in approvals))


def test_double_approve_is_processed_once(fresh_db):
    req_id = run(db.create_request(WORKER, "2026-10-25", "08:00 - 20:00"))
    results = run(_decide_concurrently(req_id, [True] * 5))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert (winners[0]["user_id"], winners[0]["date_str"], winners[0]["desired_time"]) == \
        (WORKER, "2026-10-25", "08:00 - 20:00")
    assert request_status(req_id) == "APPROVED"
    assert schedule(WORKER) == [("2026-10-25", "08:00 - 20:00")]


def test_approve_and_reject_at_once(fresh_db):
    req_id = run(db.create_request(WORKER, "2026-10-25", "08:00 - 20:00"))
    approved, rejected = run(_decide_concurrently(req_id, [True, False]))
    assert (approved is None) != (rejected is None)
    if approved is not None:
        assert request_status(req_id) == "APPROVED"
        assert schedule(WORKER) == [("2026-10-25", "08:00 - 20:00")]
    else:
        assert request_status(req_id) == "REJECTED"
        assert schedule(WORKER) == []


def test_decided_request_cannot_be_decided_again(fresh_db):
    req_id = run(db.create_request(WORKER, "2026-10-25", "08:00 - 20:00"))
    assert run(db.decide_request(req_id, False)) is not None
    assert run(db.decide_request(req_id, True)) is None
    assert schedule(WORKER) == []
    assert run(db.decide_request(9999, True)) is None


# ---------------------------------------------------------------------
# Смена от менеджера закрывает заявки на ту же дату
# ---------------------------------------------------------------------
def test_manager_shift_supersedes_pending_request(fresh_db):
    same_day = run(db.create_request(WORKER, "2026-10-25", "08:00 - 20:00"))
    other_day = run(db.create_request(WORKER, "2026-10-26", "08:00 - 20:00"))
    colleague = run(db.create_request(OTHER, "2026-10-25", "08:00 - 20:00"))

    closed = run(db.set_shift(WORKER, "2026-10-25", "Выходной"))

    assert closed == 1
    assert request_status(same_day) == "SUPERSEDED"
    assert request_status(other_day) == "PENDING"
    assert request_status(colleague) == "PENDING"
    assert {r["id"] for r in run(db.get_pending_requests("Абая"))} == {other_day, colleague}
    # старая кнопка «Принять» больше не перетирает смену, назначенную менеджером
    assert run(db.decide_request(same_day, True)) is None
    assert schedule(WORKER) == [("2026-10-25", "Выходной")]
