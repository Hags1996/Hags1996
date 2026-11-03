"""Исправления: рассылка, отмена, длинное расписание, филиалы Администратора,
доставка заявок, пауза, ввод на шагах FSM, поддельные кнопки, логи.

Все имена и ID выдуманные.
"""
import json
import logging
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from telebot import asyncio_helper

import bot as B
import db
from conftest import ADMIN_ID, register, run

WORKER, MANAGER, FOREIGN_WORKER = 222, 333, 555


# ---------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------
def answers(replies):
    """Тексты ответов на нажатия инлайн-кнопок."""
    return [r["text"] for r in replies if r["method"] == "answerCallbackQuery"]


def callbacks(replies):
    """Все callback_data из инлайн-клавиатур ответов."""
    result = []
    for r in replies:
        markup = json.loads(r["reply_markup"]) if r["reply_markup"] else {}
        for row in markup.get("inline_keyboard", []):
            result += [b["callback_data"] for b in row]
    return result


def fail_requests(tg, monkeypatch, api_method, chat_id=None):
    """Telegram отвечает ошибкой на вызов api_method (для chat_id или для всех)."""
    def fake(token, url, method="get", params=None, **kwargs):
        params = params or {}
        if url == api_method and (chat_id is None or params.get("chat_id") == str(chat_id)):
            tg.sent.append({"method": url + ":failed", "chat_id": params.get("chat_id"),
                            "text": params.get("text", ""), "reply_markup": None})
            raise RuntimeError("Forbidden: bot was blocked by the user")
        return tg.request(token, url, method, params, **kwargs)
    monkeypatch.setattr(asyncio_helper, "_process_request", fake)


@pytest.fixture
def sleeps(monkeypatch):
    """Подменяет asyncio.sleep в боте: записывает паузы и не ждёт."""
    calls = []

    async def fake_sleep(delay, *args, **kwargs):
        calls.append(delay)

    monkeypatch.setattr(B.asyncio, "sleep", fake_sleep)
    return calls


@pytest.fixture
def branch(tg):
    """Работник и менеджер в «Абая», работник в «Гагарина» (без менеджера)."""
    register(tg, WORKER, name="Иванов Иван", branch="Абая")
    register(tg, MANAGER, name="Петров Пётр", branch="Абая", position="Менеджер")
    register(tg, FOREIGN_WORKER, name="Сидоров Сидор", branch="Гагарина")
    run(db.update_user_role(MANAGER, db.ROLE_MANAGER))
    return tg


def day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


# ---------------------------------------------------------------------
# 1. Рассылка: предпросмотр, кнопки меню не принимаются, итог N / M
# ---------------------------------------------------------------------
def test_broadcast_does_not_swallow_menu_button(branch, sleeps):
    tg = branch
    tg.send(ADMIN_ID, B.BTN_BROADCAST)
    replies = tg.send(ADMIN_ID, B.BTN_PAUSE)
    assert run(db.is_bot_paused()) is False
    assert tg.texts_to(replies, WORKER) == []
    replies = tg.send(ADMIN_ID, "/help")
    assert tg.texts_to(replies, WORKER) == []

    replies = tg.send(ADMIN_ID, "Завтра собрание в 10:00")
    assert tg.texts_to(replies, WORKER) == []  # пока только предпросмотр
    preview = [r for r in replies if r["chat_id"] == str(ADMIN_ID) and r["reply_markup"]]
    assert preview and "broadcast_send" in callbacks(preview)
    assert "Завтра собрание" in preview[-1]["text"]

    replies = tg.press(ADMIN_ID, "broadcast_send")
    assert any("Завтра собрание" in t for t in tg.texts_to(replies, WORKER))
    assert any("отправлено 4 / не доставлено 0" in t for t in tg.texts_to(replies, ADMIN_ID))
    assert sleeps and all(s == B.BROADCAST_DELAY for s in sleeps)


def test_broadcast_cancel_from_preview(branch, sleeps):
    tg = branch
    tg.send(ADMIN_ID, B.BTN_BROADCAST)
    tg.send(ADMIN_ID, "Текст")
    replies = tg.press(ADMIN_ID, "cancel_action")
    assert answers(replies)
    replies = tg.press(ADMIN_ID, "broadcast_send")  # после отмены кнопка не работает
    assert tg.texts_to(replies, WORKER) == []


def test_broadcast_counts_failures_and_logs_without_text_or_id(branch, sleeps, monkeypatch, caplog):
    tg = branch
    tg.send(ADMIN_ID, B.BTN_BROADCAST)
    tg.send(ADMIN_ID, "Секретный текст объявления")
    fail_requests(tg, monkeypatch, "sendMessage", chat_id=WORKER)
    with caplog.at_level(logging.DEBUG):
        replies = tg.press(ADMIN_ID, "broadcast_send")
    assert any("отправлено 3 / не доставлено 1" in t for t in tg.texts_to(replies, ADMIN_ID))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "bot"]
    assert warnings
    assert "Секретный текст" not in caplog.text
    assert str(WORKER) not in " ".join(r.getMessage() for r in warnings)
    assert B.uid_tag(WORKER) in caplog.text


def test_pause_notification_uses_delay_and_reports(branch, sleeps):
    tg = branch
    replies = tg.send(ADMIN_ID, B.BTN_PAUSE)
    assert run(db.is_bot_paused()) is True
    assert any("отправлено 4 / не доставлено 0" in t for t in tg.texts_to(replies, ADMIN_ID))
    assert len(sleeps) == 3


# ---------------------------------------------------------------------
# 2. Отмена работает и без активного состояния
# ---------------------------------------------------------------------
def test_cancel_command_without_state_answers(tg):
    replies = tg.send(ADMIN_ID, "/cancel")
    assert any("Нет активных действий" in t for t in tg.texts_to(replies, ADMIN_ID))


def test_inline_cancel_without_state_answers(branch):
    tg = branch
    tg.send(MANAGER, B.BTN_EDIT_SCHEDULE)
    replies = tg.press(MANAGER, "cancel_action")
    assert answers(replies) == ["Действие отменено."]
    replies = tg.press(ADMIN_ID, "cancel_action")  # клавиатура ролей у Администратора
    assert answers(replies) == ["Действие отменено."]


def test_cancel_inside_state_still_works(branch):
    tg = branch
    tg.send(WORKER, B.BTN_REQUEST)
    replies = tg.send(WORKER, "/cancel")
    assert any("Действие отменено" in t for t in tg.texts_to(replies, WORKER))
    replies = tg.send(WORKER, "25.10")
    assert any("Не понял" in t for t in tg.texts_to(replies, WORKER))


# ---------------------------------------------------------------------
# 3. Расписание на 14 дней и разбиение длинных сообщений
# ---------------------------------------------------------------------
def test_my_schedule_shows_next_14_days(branch):
    tg = branch
    for offset, shift in ((-1, "07:00 - 19:00"), (0, "08:00 - 20:00"), (13, "09:00 - 21:00"), (14, "10:00 - 22:00")):
        run(db.set_shift(WORKER, day(offset), shift))
    text = "\n".join(tg.texts_to(tg.send(WORKER, B.BTN_MY_SCHEDULE), WORKER))
    assert "08:00 - 20:00" in text and "09:00 - 21:00" in text
    assert "07:00 - 19:00" not in text and "10:00 - 22:00" not in text


def test_branch_schedule_db_period_keeps_employees_without_shifts(branch):
    run(db.set_shift(WORKER, day(-5), "08:00 - 20:00"))
    run(db.set_shift(WORKER, day(1), "09:00 - 21:00"))
    employees = run(db.get_branch_schedule("Абая", day(0), day(13)))
    by_name = {e["full_name"]: e["shifts"] for e in employees}
    assert by_name["Иванов Иван"] == [(day(1), "09:00 - 21:00")]
    assert by_name["Петров Пётр"] == []
    assert len(run(db.get_branch_schedule("Абая"))[0]["shifts"]) + len(run(db.get_branch_schedule("Абая"))[1]["shifts"]) == 2


def test_long_branch_schedule_is_split(branch):
    tg = branch
    names = []
    for i in range(12):
        uid = 7000 + i
        name = f"Сотрудник номер {i:02d} " + "Д" * 40
        names.append(name)
        run(db.add_user(uid, name, "Абая", "Бариста"))
        for offset in range(14):
            run(db.set_shift(uid, day(offset), "08:00 - 20:00"))
    texts = tg.texts_to(tg.send(MANAGER, B.BTN_BRANCH_SCHEDULE), MANAGER)
    assert len(texts) > 1
    assert all(B.text_len(t) <= 4096 for t in texts)
    joined = "\n".join(texts)
    assert all(n in joined for n in names)


def test_split_message_on_line_boundaries():
    lines = [f"строка {i} " + "x" * 50 for i in range(200)]
    parts = B.split_message("\n".join(lines), limit=1000)
    assert all(B.text_len(p) <= 1000 for p in parts)
    assert "\n".join(parts).split("\n") == lines
    assert all(len(p) <= 10 for p in B.split_message("a" * 35, limit=10))


# ---------------------------------------------------------------------
# 4. Администратор сначала выбирает филиал
# ---------------------------------------------------------------------
def test_superadmin_picks_branch_for_schedule(branch):
    tg = branch
    run(db.set_shift(WORKER, day(0), "08:00 - 20:00"))
    replies = tg.send(ADMIN_ID, B.BTN_BRANCH_SCHEDULE)
    assert all(f"pick_branch:sched:{b}" in callbacks(replies) for b in B.BRANCHES)
    text = "\n".join(tg.texts_to(tg.press(ADMIN_ID, "pick_branch:sched:Абая"), ADMIN_ID))
    assert "Иванов Иван" in text and "08:00 - 20:00" in text


def test_superadmin_picks_branch_for_requests_and_edit(branch):
    tg = branch
    tg.send(WORKER, B.BTN_REQUEST)
    tg.send(WORKER, "25.10")
    tg.send(WORKER, "08:00 - 20:00")
    tg.send(ADMIN_ID, B.BTN_REQUESTS)
    replies = tg.press(ADMIN_ID, "pick_branch:reqs:Абая")
    assert any("Иванов Иван" in t for t in tg.texts_to(replies, ADMIN_ID))

    tg.send(ADMIN_ID, B.BTN_EDIT_SCHEDULE)
    replies = tg.press(ADMIN_ID, "pick_branch:edit:Абая")
    assert f"edit_emp:{WORKER}" in callbacks(replies)


def test_manager_cannot_pick_foreign_branch(branch):
    tg = branch
    replies = tg.send(MANAGER, B.BTN_BRANCH_SCHEDULE)  # менеджер — сразу свой филиал
    assert any("Абая" in t for t in tg.texts_to(replies, MANAGER))
    replies = tg.press(MANAGER, "pick_branch:sched:Гагарина")
    assert answers(replies) == [B.NO_RIGHTS_TEXT]
    assert not any("Сидоров" in t for t in tg.texts_to(replies, MANAGER))


# ---------------------------------------------------------------------
# 5. Заявка в филиале без руководителя уходит Администраторам
# ---------------------------------------------------------------------
def test_request_recipients_fallback_to_superadmins(branch):
    assert [u["telegram_id"] for u in run(db.get_request_recipients("Абая"))] == [MANAGER]
    assert [u["telegram_id"] for u in run(db.get_request_recipients("Гагарина"))] == [ADMIN_ID]


def test_request_without_manager_goes_to_admin(branch):
    tg = branch
    tg.send(FOREIGN_WORKER, B.BTN_REQUEST)
    tg.send(FOREIGN_WORKER, "25.10")
    replies = tg.send(FOREIGN_WORKER, "08:00 - 20:00")
    assert any("Новый запрос" in t for t in tg.texts_to(replies, ADMIN_ID))
    assert any("успешно отправлен" in t for t in tg.texts_to(replies, FOREIGN_WORKER))


def test_worker_is_told_when_request_not_delivered(branch, monkeypatch):
    tg = branch
    tg.send(FOREIGN_WORKER, B.BTN_REQUEST)
    tg.send(FOREIGN_WORKER, "25.10")
    fail_requests(tg, monkeypatch, "sendMessage", chat_id=ADMIN_ID)
    replies = tg.send(FOREIGN_WORKER, "08:00 - 20:00")
    texts = tg.texts_to(replies, FOREIGN_WORKER)
    assert not any("успешно отправлен" in t for t in texts)
    assert any("не удалось" in t for t in texts)
    assert len(run(db.get_pending_requests("Гагарина"))) == 1  # заявка сохранена


# ---------------------------------------------------------------------
# 6. Пауза действует и на шаги FSM; Администратор не блокируется
# ---------------------------------------------------------------------
def test_pause_blocks_fsm_steps(branch):
    tg = branch
    tg.send(WORKER, B.BTN_REQUEST)
    run(db.set_bot_paused(True))
    replies = tg.send(WORKER, "25.10")
    assert tg.texts_to(replies, WORKER) == [B.PAUSED_TEXT]
    tg.send(WORKER, "08:00 - 20:00")
    assert run(db.get_pending_requests("Абая")) == []

    replies = tg.press(MANAGER, f"edit_emp:{WORKER}")
    assert answers(replies) == [B.PAUSED_TEXT]


def test_superadmin_not_blocked_by_pause(branch):
    tg = branch
    run(db.set_bot_paused(True))
    replies = tg.send(ADMIN_ID, "/help")
    assert any("Справка" in t for t in tg.texts_to(replies, ADMIN_ID))
    tg.send(ADMIN_ID, B.BTN_SET_ROLE)
    replies = tg.send(ADMIN_ID, str(WORKER))
    assert any("Выберите новую роль" in t for t in tg.texts_to(replies, ADMIN_ID))


# ---------------------------------------------------------------------
# 7. Шаги ФИО/должность: без кнопок меню, команд и длинного текста
# ---------------------------------------------------------------------
def test_registration_rejects_menu_commands_and_long_text(tg):
    tg.send(WORKER, "/start")
    for bad in (B.BTN_MY_SCHEDULE, "/help", "Я" * 65):
        replies = tg.send(WORKER, bad)
        assert not any("Выберите ваш филиал" in t for t in tg.texts_to(replies, WORKER)), bad
    tg.send(WORKER, "Иванов Иван")
    tg.press(WORKER, "select_branch:Абая")
    for bad in (B.BTN_PROFILE, "/start", "Б" * 65):
        tg.send(WORKER, bad)
    assert run(db.get_user(WORKER)) is None
    tg.send(WORKER, "Бариста")
    user = run(db.get_user(WORKER))
    assert (user["full_name"], user["position"]) == ("Иванов Иван", "Бариста")


def test_cancel_registration_hides_worker_keyboard(tg):
    tg.send(WORKER, "/start")
    replies = tg.send(WORKER, "/cancel")
    reply = [r for r in replies if r["chat_id"] == str(WORKER)][-1]
    assert json.loads(reply["reply_markup"]) == {"remove_keyboard": True}
    assert "/start" in reply["text"]


def test_date_step_rejects_commands(branch):
    tg = branch
    tg.send(WORKER, B.BTN_REQUEST)
    tg.send(WORKER, "/start")
    tg.send(WORKER, B.BTN_MY_SCHEDULE)
    tg.send(WORKER, "25.10")
    tg.send(WORKER, "08:00 - 20:00")
    assert len(run(db.get_pending_requests("Абая"))) == 1


# ---------------------------------------------------------------------
# 8. Поддельный callback_data и «²» не роняют обработчики
# ---------------------------------------------------------------------
@pytest.mark.parametrize("data", [
    "app_req:abc", "rej_req:", "app_req:1:2", "app_req:²",
    "edit_emp:1:2", "edit_emp:x", "edit_emp:²",
])
def test_forged_callback_is_answered(branch, data):
    replies = branch.press(MANAGER, data)
    assert answers(replies) == [B.BAD_CALLBACK_TEXT]


def test_forged_set_role_callback(tg):
    replies = tg.press(ADMIN_ID, "set_role:²:worker")
    assert answers(replies) == [B.BAD_CALLBACK_TEXT]
    replies = tg.press(ADMIN_ID, "set_role:1")
    assert answers(replies) == [B.BAD_CALLBACK_TEXT]


def test_superscript_digit_in_role_id(tg):
    tg.send(ADMIN_ID, B.BTN_SET_ROLE)
    replies = tg.send(ADMIN_ID, "²")
    assert any("корректный числовой" in t for t in tg.texts_to(replies, ADMIN_ID))


# ---------------------------------------------------------------------
# 9. Решение по заявке
# ---------------------------------------------------------------------
def make_request(tg):
    tg.send(WORKER, B.BTN_REQUEST)
    tg.send(WORKER, "25.10")
    tg.send(WORKER, "08:00 - 20:00")
    return run(db.get_pending_requests("Абая"))[0]["id"]


def test_second_decision_says_already_processed(branch):
    req_id = make_request(branch)
    branch.press(MANAGER, f"app_req:{req_id}")
    replies = branch.press(MANAGER, f"rej_req:{req_id}")
    assert answers(replies) == ["Заявка уже обработана"]
    assert branch.texts_to(replies, WORKER) == []


def test_worker_notified_even_if_buttons_not_removed(branch, monkeypatch):
    req_id = make_request(branch)
    fail_requests(branch, monkeypatch, "editMessageReplyMarkup")
    replies = branch.press(MANAGER, f"app_req:{req_id}")
    assert any("было изменено" in t for t in branch.texts_to(replies, WORKER))
    assert answers(replies) == ["Решение сохранено!"]


# ---------------------------------------------------------------------
# 10. Права перепроверяются перед сохранением смены
# ---------------------------------------------------------------------
def test_rights_rechecked_before_saving_shift(branch):
    tg = branch
    tg.send(MANAGER, B.BTN_EDIT_SCHEDULE)
    tg.press(MANAGER, f"edit_emp:{WORKER}")
    tg.send(MANAGER, "25.10")
    run(db.update_user_role(MANAGER, db.ROLE_WORKER))  # роль сняли во время ввода
    replies = tg.send(MANAGER, "08:00 - 20:00")
    assert run(db.get_user_schedule(WORKER)) == []
    assert any("Недостаточно прав" in t for t in tg.texts_to(replies, MANAGER))
    assert tg.texts_to(replies, WORKER) == []


# ---------------------------------------------------------------------
# 11. Неизвестный текст, устаревшие кнопки, файловое хранилище состояний
# ---------------------------------------------------------------------
def test_unknown_text_gets_help_hint(branch):
    replies = branch.send(WORKER, "абракадабра")
    assert any("Не понял. Нажмите /help" in t for t in branch.texts_to(replies, WORKER))


def test_unknown_callback_is_answered(branch):
    replies = branch.press(WORKER, "broadcast_send")  # не Администратор и не в состоянии
    assert answers(replies)
    replies = branch.press(WORKER, "что-то_старое")
    assert answers(replies)


def test_file_state_storage_survives_restart(tmp_path):
    path = tmp_path / "states.pkl"

    async def scenario():
        s1 = B.FileStateStorage(path)
        await s1.set_state(chat_id=1, user_id=1, state="RequestScheduleState:date_str")
        await s1.set_data(chat_id=1, user_id=1, key="date_str", value="2026-10-25")
        s2 = B.FileStateStorage(path)  # «перезапуск»
        return await s2.get_state(chat_id=1, user_id=1), await s2.get_data(chat_id=1, user_id=1)

    assert run(scenario()) == ("RequestScheduleState:date_str", {"date_str": "2026-10-25"})


def test_state_file_next_to_bot_and_ignored_by_git():
    root = Path(B.__file__).resolve().parent
    assert B.STATE_FILE.parent == root
    assert B.STATE_FILE.name in (root / ".gitignore").read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# 12. Логи: настройка в main(), ротация, метки вместо ID
# ---------------------------------------------------------------------
def test_import_does_not_configure_file_logging():
    # pytest держит свой FileHandler, поэтому ищем именно bot.log
    files = [Path(h.baseFilename).name for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
    assert "bot.log" not in files


def test_setup_logging_uses_rotation(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        handler = B.setup_logging(tmp_path / "bot.log")
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 1_000_000 and handler.backupCount == 5
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_uid_tag_hides_id():
    tag = B.uid_tag(987654321)
    assert len(tag) == 8 and all(c in "0123456789abcdef" for c in tag)
    assert tag == B.uid_tag(987654321) != B.uid_tag(987654322)
    assert "987654321" not in tag


def test_logs_do_not_contain_telegram_id(tg, caplog):
    uid = 987654321
    run(db.set_bot_paused(True))
    with caplog.at_level(logging.DEBUG, logger="bot"):
        tg.send(uid, "/start")
        run(db.set_bot_paused(False))
        tg.send(uid, "/start")
    bot_logs = " ".join(r.getMessage() for r in caplog.records if r.name == "bot")
    assert bot_logs  # что-то залогировано
    assert str(uid) not in bot_logs
