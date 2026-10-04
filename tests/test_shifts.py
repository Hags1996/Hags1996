"""Разбор дат/времени и миграция старой базы."""
import sqlite3
from datetime import date

import pytest

import db
from conftest import ADMIN_ID, register, run
from shifts import DAY_OFF, format_date, parse_date, parse_shift

TODAY = date(2026, 10, 4)


@pytest.mark.parametrize("text, expected", [
    ("25.10", "2026-10-25"),
    ("5.1", "2027-01-05"),          # январь из октября — следующий год
    ("20.09", "2026-09-20"),        # прошло меньше месяца — этот год
    ("25.10.2027", "2027-10-25"),
    (" 01.11 ", "2026-11-01"),
    ("31.02", None),
    ("Понедельник", None),
    ("📥 Запросы на изменение", None),
    ("", None),
])
def test_parse_date(text, expected):
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize("text, expected", [
    ("08:00 - 20:00", "08:00 - 20:00"),
    ("8:00-20:00", "08:00 - 20:00"),
    ("8.00 20.00", "08:00 - 20:00"),
    ("9:00–21:00", "09:00 - 21:00"),
    ("выходной", DAY_OFF),
    ("9:-00-21:00", None),
    ("25:00 - 26:00", None),
    ("📢 Глобальная рассылка", None),
])
def test_parse_shift(text, expected):
    assert parse_shift(text) == expected


def test_format_date():
    assert format_date("2026-10-25") == "25.10.2026"
    assert format_date("старое значение") == "старое значение"


def test_menu_button_is_not_saved_as_date(tg):
    """Пользователь застрял на вводе даты и жмёт кнопку меню — её текст не должен стать датой."""
    register(tg, 222)
    tg.send(222, "✏️ Запросить изменение расписания")
    replies = tg.send(222, "📥 Запросы на изменение")
    assert any("Не получилось распознать дату" in t for t in tg.texts_to(replies, 222))
    tg.send(222, "25.10")
    replies = tg.send(222, "📢 Глобальная рассылка")
    assert any("Не получилось распознать время" in t for t in tg.texts_to(replies, 222))
    tg.send(222, "8:00-20:00")
    req = run(db.get_pending_requests("Абая"))[0]
    assert req["desired_time"] == "08:00 - 20:00"
    assert parse_date("25.10") == req["date_str"]


def test_one_shift_per_day(tg):
    register(tg, 222)
    run(db.set_shift(222, "2026-10-25", "08:00 - 20:00"))
    run(db.set_shift(222, "2026-10-25", DAY_OFF))
    assert [tuple(r) for r in run(db.get_user_schedule(222))] == [("2026-10-25", DAY_OFF)]


def test_migration_of_old_database(tmp_path, monkeypatch):
    """Старая база: даты «ДД.ММ», дубли смен, мусор из кнопок меню."""
    path = tmp_path / "old.db"
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
        INSERT INTO schedules (user_id, date_str, shift_time) VALUES
            (5, '01.10', '8:00 20:00'),
            (5, '01.10', '09:00 - 21:00'),
            (5, '📥 Запросы на изменение', '📢 Глобальная рассылка');
        INSERT INTO schedule_requests (user_id, date_str, desired_time) VALUES (5, '01.10', '09:00 - 21:00');
    """)
    con.commit()
    con.close()

    monkeypatch.setattr(db, "DB_FILE", str(path))
    run(db.init_db(ADMIN_ID))
    run(db.init_db(ADMIN_ID))  # повторный запуск ничего не ломает

    year = date.today().year
    rows = [tuple(r) for r in run(db.get_user_schedule(5))]
    assert (f"{year}-10-01", "09:00 - 21:00") in rows       # из двух дублей осталась последняя
    assert ("📥 Запросы на изменение", "📢 Глобальная рассылка") in rows  # мусор не удалён молча
    assert len(rows) == 2
    con = sqlite3.connect(path)
    assert con.execute("SELECT date_str FROM schedule_requests").fetchone()[0] == f"{year}-10-01"
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
