"""QR-отметка прихода (шаг 1): код в ссылке /start chk_…, таблица attendance, команда /qr."""
import logging
import re
import sqlite3
import time
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import bot as B
import db
import qr
from conftest import ADMIN_ID, FIXED_TODAY, register, run

SECRET = "test-qr-secret-not-real"
BOT_NAME = "smena_test_bot"
WORKER, WORKER2, WORKER3, MANAGER = 900000001, 900000002, 900000003, 900000004
TODAY = FIXED_TODAY.isoformat()


@pytest.fixture(autouse=True)
def qr_settings(monkeypatch):
    monkeypatch.setattr(B, "QR_SECRET_KEY", SECRET)
    monkeypatch.setattr(B, "_bot_username", None)

    async def fake_get_me():
        return SimpleNamespace(username=BOT_NAME)

    monkeypatch.setattr(B.bot, "get_me", fake_get_me)


def at(hour, minute, second=0, day=FIXED_TODAY) -> int:
    """Unix-время для местного времени «день, ЧЧ:ММ:СС»."""
    return int(datetime(day.year, day.month, day.day, hour, minute, second).timestamp())


def payload(branch_code="abaya", ts=None) -> str:
    """Что зашито в QR филиала в момент ts."""
    return qr.make_payload(SECRET, branch_code, at(9, 0) if ts is None else ts)


def scan(tg, user_id, start_param, ts):
    """Сотрудник открыл ссылку из QR: Telegram присылает «/start <параметр>» со временем ts."""
    uid = tg._next_id()
    return tg._run({"update_id": uid, "message": {
        "message_id": uid, "date": ts, "text": f"/start {start_param}",
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "entities": [{"type": "bot_command", "offset": 0, "length": len("/start")}]}})


def marks(user_id, date_str=TODAY):
    return run(db.get_attendance(user_id, date_str))


@pytest.fixture
def branch(tg):
    """Филиал «Абая»: два работника и менеджер (выдуманные)."""
    register(tg, WORKER, name="Тестов Тест", branch="Абая")
    register(tg, WORKER2, name="Пробная Анна", branch="Абая")
    register(tg, MANAGER, name="Руководов Рустам", branch="Абая", position="Менеджер")
    run(db.update_user_role(MANAGER, db.ROLE_MANAGER))
    return tg


# --- код ---

def test_code_accepted_only_in_neighbour_windows():
    assert qr.QR_WINDOW_SECONDS == 15
    w = qr.QR_WINDOW_SECONDS
    shown = at(9, 0, 7)
    code = qr.make_code(SECRET, "abaya", qr.window(shown))
    assert len(code) == qr.CODE_LEN
    assert qr.verify(SECRET, "abaya", code, shown - w)        # t−1
    assert qr.verify(SECRET, "abaya", code, shown)            # t
    assert qr.verify(SECRET, "abaya", code, shown + w)        # t+1
    assert not qr.verify(SECRET, "abaya", code, shown + 2 * w)  # t+2 — уже чужое окно
    assert not qr.verify(SECRET, "abaya", code, shown - 2 * w)
    assert not qr.verify(SECRET, "gagarina", code, shown)     # ключ у каждого филиала свой
    assert not qr.verify("другой секрет", "abaya", code, shown)


def test_start_param_is_telegram_safe():
    for code in B.BRANCH_CODES.values():
        p = payload(code)
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p)
        assert qr.parse_payload(p) == (code, p.rsplit("_", 1)[1])
    assert set(B.BRANCH_CODES) == set(B.BRANCHES)


# --- отметка ---

def test_valid_code_marks_arrival_and_notifies_manager(branch):
    run(db.set_shift(WORKER, TODAY, "09:00 - 18:00"))
    replies = scan(branch, WORKER, payload(ts=at(8, 55)), at(8, 55))

    assert branch.texts_to(replies, WORKER) == ["✅ Пришёл: 08:55, филиал «Абая»"]
    to_manager = branch.texts_to(replies, MANAGER)
    assert len(to_manager) == 1
    assert "Тестов Тест" in to_manager[0] and "08:55" in to_manager[0]
    rows = marks(WORKER)
    assert [(r["branch"], r["ts"], r["date_str"], r["note"]) for r in rows] == [("Абая", at(8, 55), TODAY, "")]


def test_code_older_than_one_minute_is_rejected(branch):
    shown = at(9, 0, 0)
    replies = scan(branch, WORKER, payload(ts=shown), shown + 60)
    assert branch.texts_to(replies, WORKER) == [B.QR_EXPIRED_TEXT]
    assert branch.texts_to(replies, MANAGER) == []
    assert marks(WORKER) == []

    replies = scan(branch, WORKER, payload(ts=shown), shown + 29)  # ещё соседнее окно (15-секундное)
    assert "✅ Пришёл: 09:00" in branch.texts_to(replies, WORKER)[0]


def test_foreign_branch_qr_is_refused(branch):
    replies = scan(branch, WORKER, payload("gagarina"), at(9, 0))
    text = branch.texts_to(replies, WORKER)[0]
    assert "«Гагарина»" in text and "Приход не отмечен" in text
    assert branch.texts_to(replies, MANAGER) == []
    assert marks(WORKER) == []


@pytest.mark.parametrize("bad", ["chk_mars_0123456789", "chk_abaya_XYZ", "chk_abaya", "chk_",
                                 "chk_abaya_0123456789abcdef", "chk_Абая_0123456789"])
def test_unknown_or_malformed_code_is_not_recognized(branch, bad):
    replies = scan(branch, WORKER, bad, at(9, 0))
    assert branch.texts_to(replies, WORKER) == [B.QR_BAD_CODE_TEXT]
    assert marks(WORKER) == []


def test_forged_code_is_not_recorded(branch):
    replies = scan(branch, WORKER, "chk_abaya_0000000000", at(9, 0))
    assert branch.texts_to(replies, WORKER) == [B.QR_EXPIRED_TEXT]
    assert marks(WORKER) == []


def test_two_workers_with_one_code(branch):
    shared = payload(ts=at(9, 0))
    first = scan(branch, WORKER, shared, at(9, 0, 5))
    second = scan(branch, WORKER2, shared, at(9, 0, 20))
    assert branch.texts_to(first, WORKER)[0].startswith("✅ Пришёл: 09:00")
    assert branch.texts_to(second, WORKER2)[0].startswith("✅ Пришёл: 09:00")
    assert len(marks(WORKER)) == len(marks(WORKER2)) == 1


def test_repeat_same_day_keeps_first_mark(branch):
    scan(branch, WORKER, payload(ts=at(9, 2)), at(9, 2))
    replies = scan(branch, WORKER, payload(ts=at(12, 30)), at(12, 30))
    assert branch.texts_to(replies, WORKER) == ["ℹ️ Вы уже отметились в 09:02."]
    assert branch.texts_to(replies, MANAGER) == []
    assert [r["ts"] for r in marks(WORKER)] == [at(9, 2)]


def test_next_day_is_a_new_mark(branch):
    scan(branch, WORKER, payload(ts=at(9, 0)), at(9, 0))
    tomorrow = date(2026, 10, 5)
    replies = scan(branch, WORKER, payload(ts=at(9, 0, day=tomorrow)), at(9, 0, day=tomorrow))
    assert branch.texts_to(replies, WORKER)[0].startswith("✅ Пришёл")
    assert len(marks(WORKER, "2026-10-05")) == 1


def test_time_comes_from_message_date_not_bot_clock(branch):
    # Сообщение из прошлого (бот «спал»): код того времени принят, хотя часы бота давно ушли вперёд
    old = at(9, 5)
    assert abs(time.time() - old) > 3600
    replies = scan(branch, WORKER, payload(ts=old), old)
    assert branch.texts_to(replies, WORKER)[0].startswith("✅ Пришёл: 09:05")
    assert marks(WORKER)[0]["ts"] == old

    # А свежий код при старом времени сообщения — устарел
    replies = scan(branch, WORKER2, payload(ts=int(time.time())), old)
    assert branch.texts_to(replies, WORKER2) == [B.QR_EXPIRED_TEXT]
    assert marks(WORKER2) == []


def test_late_and_off_schedule_notes(branch):
    register(branch, WORKER3, name="Безграфиков Борис", branch="Абая")
    run(db.set_shift(WORKER, TODAY, "09:00 - 18:00"))
    run(db.set_shift(WORKER2, TODAY, "Выходной"))
    code = payload(ts=at(9, 17))

    replies = scan(branch, WORKER, code, at(9, 17))
    assert branch.texts_to(replies, WORKER) == ["✅ Пришёл: 09:17, филиал «Абая»\n⚠️ опоздание 17 мин"]
    assert "опоздание 17 мин" in branch.texts_to(replies, MANAGER)[0]

    replies = scan(branch, WORKER2, code, at(9, 17))
    assert "вне графика" in branch.texts_to(replies, WORKER2)[0]
    assert "вне графика" in branch.texts_to(replies, MANAGER)[0]

    scan(branch, WORKER3, code, at(9, 17))  # смены нет совсем
    assert [marks(u)[0]["note"] for u in (WORKER, WORKER2, WORKER3)] == \
        ["опоздание 17 мин", "вне графика", "вне графика"]


@pytest.mark.parametrize("shift, hhmm, note", [
    ("09:00 - 18:00", (9, 0), ""),             # ровно к началу
    ("09:00 - 18:00", (8, 40), ""),            # раньше
    ("09:00 - 18:00", (10, 30), "опоздание 90 мин"),
    ("8.00 20.00", (8, 5), "опоздание 5 мин"),  # старая запись другим форматом
    ("выходной", (9, 0), "вне графика"),
    (None, (9, 0), "вне графика"),
    ("с утра", (11, 0), ""),                   # формат не распознан — без пометки
])
def test_checkin_note(shift, hhmm, note):
    assert B.checkin_note(shift, datetime(2026, 10, 4, *hhmm)) == note


def test_inactive_and_pending_are_not_marked(branch):
    run(db.deactivate_user(WORKER))
    replies = scan(branch, WORKER, payload(), at(9, 0))
    assert branch.texts_to(replies, WORKER) == [B.INACTIVE_TEXT]
    assert marks(WORKER) == []

    register(branch, WORKER3, name="Ждущий Жан", branch="Абая", approve=False)
    replies = scan(branch, WORKER3, payload(), at(9, 0))
    text = branch.texts_to(replies, WORKER3)[0]
    assert B.PENDING_TEXT in text and B.QR_SCAN_AGAIN_TEXT in text
    assert marks(WORKER3) == []


def test_unregistered_user_starts_registration(tg):
    replies = scan(tg, WORKER, payload(), at(9, 0))
    text = tg.texts_to(replies, WORKER)[0]
    assert "Введите ваше ФИО" in text and B.QR_SCAN_AGAIN_TEXT in text
    assert run(db.get_user(WORKER)) is None
    assert marks(WORKER) == []


def test_secret_not_set(branch, monkeypatch):
    monkeypatch.setattr(B, "QR_SECRET_KEY", "")
    replies = scan(branch, WORKER, payload(), at(9, 0))
    assert branch.texts_to(replies, WORKER) == [B.QR_NOT_SET_TEXT]
    assert marks(WORKER) == []
    assert "не настроена" in branch.texts_to(branch.send(MANAGER, "/qr"), MANAGER)[0]


def test_logs_have_no_names_or_ids(branch, caplog):
    with caplog.at_level(logging.DEBUG, logger="bot"):
        scan(branch, WORKER, payload(), at(9, 0))
        scan(branch, WORKER2, payload("gagarina"), at(9, 0))
    bot_logs = " ".join(r.getMessage() for r in caplog.records if r.name == "bot")
    assert B.uid_tag(WORKER) in bot_logs
    for secret_bit in (str(WORKER), str(WORKER2), "Тестов", "Пробная"):
        assert secret_bit not in bot_logs


# --- /qr ---

def test_qr_command_not_for_worker(branch):
    assert branch.texts_to(branch.send(WORKER, "/qr"), WORKER) == []


def test_manager_gets_own_branch_link_that_works(branch):
    text = branch.texts_to(branch.send(MANAGER, "/qr"), MANAGER)[0]
    assert "около 30 секунд" in text
    links = re.findall(rf"https://t\.me/{BOT_NAME}\?start=(chk_[a-z0-9]+_[0-9a-f]+)", text)
    assert len(links) == 1 and links[0].startswith("chk_abaya_")

    replies = scan(branch, WORKER, links[0], int(time.time()))
    assert branch.texts_to(replies, WORKER)[0].startswith("✅ Пришёл")


def test_admin_gets_links_of_all_branches(branch):
    text = branch.texts_to(branch.send(ADMIN_ID, "/qr"), ADMIN_ID)[0]
    for code in B.BRANCH_CODES.values():
        assert f"start=chk_{code}_" in text


def test_help_mentions_qr_for_management_only(branch):
    assert "/qr" in branch.texts_to(branch.send(MANAGER, "/help"), MANAGER)[0]
    assert "/qr" not in branch.texts_to(branch.send(WORKER, "/help"), WORKER)[0]


# --- база ---

def test_migration_3_adds_attendance_with_backup(tmp_path, monkeypatch):
    path = tmp_path / "v2.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, branch TEXT NOT NULL,
                            position TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'worker',
                            status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                                date_str TEXT NOT NULL, shift_time TEXT NOT NULL);
        CREATE TABLE schedule_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                                        date_str TEXT NOT NULL, desired_time TEXT NOT NULL,
                                        status TEXT NOT NULL DEFAULT 'PENDING');
        INSERT INTO users VALUES (5, 'Старый Работник', 'Абая', 'Бариста', 'worker', 'inactive');
        PRAGMA user_version = 2;
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(db, "DB_FILE", str(path))

    run(db.init_db(ADMIN_ID, today=date(2026, 11, 27)))

    backup = tmp_path / "v2.db.bak-20261127"
    assert backup.exists()
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 3
    columns = [r[1] for r in con.execute("PRAGMA table_info(attendance)").fetchall()]
    assert columns == ["id", "user_id", "branch", "ts", "date_str", "note"]
    indexes = {r[1] for r in con.execute("PRAGMA index_list(attendance)").fetchall()}
    assert "idx_attendance_user_date" in indexes
    assert con.execute("SELECT status FROM users WHERE telegram_id = 5").fetchone() == ("inactive",)
    con.close()

    run(db.init_db(ADMIN_ID, today=date(2026, 11, 28)))  # повторный запуск — без новой копии
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v2.db", "v2.db.bak-20261127"]


def test_add_attendance_once_per_day(tg):
    assert run(db.add_attendance(WORKER, "Абая", at(9, 0), TODAY, "")) is None
    assert run(db.add_attendance(WORKER, "Абая", at(10, 0), TODAY, "")) == at(9, 0)
    assert run(db.add_attendance(WORKER2, "Абая", at(9, 0), TODAY, "")) is None
    assert len(marks(WORKER)) == 1
