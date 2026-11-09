"""Решения владельца от 03.11.2026: своя заявка, карточка сотрудника и деактивация,
регистрация с подтверждением. Все имена и ID выдуманные."""
import json

import pytest

import bot as B
import db
from conftest import ADMIN_ID, register, run

WORKER, MANAGER, MANAGER2, FOREIGN_WORKER = 222, 333, 334, 555


def answers(replies):
    return [r["text"] for r in replies if r["method"] == "answerCallbackQuery"]


def callbacks(replies):
    result = []
    for r in replies:
        markup = json.loads(r["reply_markup"]) if r["reply_markup"] else {}
        for row in markup.get("inline_keyboard", []):
            result += [b["callback_data"] for b in row]
    return result


def request_change(tg, user_id, date="25.10", shift="08:00 - 20:00"):
    tg.send(user_id, B.BTN_REQUEST)
    tg.send(user_id, date)
    return tg.send(user_id, shift)


def pending_ids(branch):
    return [r["id"] for r in run(db.get_pending_requests(branch))]


@pytest.fixture
def branch(tg):
    """Работник и два менеджера в «Абая», работник в «Гагарина»."""
    register(tg, WORKER, name="Иванов Иван", branch="Абая")
    register(tg, MANAGER, name="Петров Пётр", branch="Абая", position="Менеджер")
    register(tg, MANAGER2, name="Смирнов Семён", branch="Абая", position="Менеджер")
    register(tg, FOREIGN_WORKER, name="Сидоров Сидор", branch="Гагарина")
    run(db.update_user_role(MANAGER, db.ROLE_MANAGER))
    run(db.update_user_role(MANAGER2, db.ROLE_MANAGER))
    return tg


# ---------------------------------------------------------------------
# 1. Руководитель не решает свою заявку и не получает её
# ---------------------------------------------------------------------
def test_manager_does_not_receive_own_request(branch):
    replies = request_change(branch, MANAGER)
    assert any("Новый запрос" in t for t in branch.texts_to(replies, MANAGER2))
    assert not any("Новый запрос" in t for t in branch.texts_to(replies, MANAGER))
    assert [u["telegram_id"] for u in run(db.get_request_recipients("Абая", exclude_id=MANAGER))] == [MANAGER2]


def test_manager_cannot_decide_own_request(branch):
    request_change(branch, MANAGER)
    req_id = pending_ids("Абая")[0]
    for action in ("app_req", "rej_req"):
        replies = branch.press(MANAGER, f"{action}:{req_id}")
        assert answers(replies) == [B.OWN_REQUEST_TEXT]
    assert pending_ids("Абая") == [req_id]
    assert run(db.get_user_schedule(MANAGER)) == []
    replies = branch.press(MANAGER2, f"app_req:{req_id}")  # коллега — может
    assert answers(replies) == ["Решение сохранено!"]


def test_lone_manager_request_goes_to_superadmins(branch):
    run(db.update_user_role(MANAGER2, db.ROLE_WORKER))
    assert [u["telegram_id"] for u in run(db.get_request_recipients("Абая", exclude_id=MANAGER))] == [ADMIN_ID]
    replies = request_change(branch, MANAGER)
    assert any("Новый запрос" in t for t in branch.texts_to(replies, ADMIN_ID))
    assert any("успешно отправлен" in t for t in branch.texts_to(replies, MANAGER))


def test_superadmin_own_request_without_other_admins(branch):
    """Единственный Администратор в филиале без руководителей: получателей нет — честное сообщение."""
    replies = request_change(branch, ADMIN_ID)
    texts = branch.texts_to(replies, ADMIN_ID)
    assert not any("Новый запрос" in t for t in texts)
    assert any("не удалось" in t for t in texts)
    req_id = run(db.create_request(ADMIN_ID, "2026-10-26", "Выходной"))
    assert answers(branch.press(ADMIN_ID, f"app_req:{req_id}")) == [B.OWN_REQUEST_TEXT]


def test_own_request_listed_without_buttons(branch):
    request_change(branch, MANAGER)
    request_change(branch, WORKER, date="26.10")
    own_id, worker_id = pending_ids("Абая")
    replies = branch.send(MANAGER, B.BTN_REQUESTS)
    assert f"app_req:{worker_id}" in callbacks(replies)
    assert f"app_req:{own_id}" not in callbacks(replies)
    assert any("Это ваша заявка" in t for t in branch.texts_to(replies, MANAGER))


# ---------------------------------------------------------------------
# 4. Регистрация с подтверждением
# ---------------------------------------------------------------------
NEWBIE = 777


def test_registration_waits_for_approval(tg):
    replies = register(tg, NEWBIE, name="Новиков Ной", approve=False)
    assert any("Заявка на доступ отправлена, ждите подтверждения" in t for t in tg.texts_to(replies, NEWBIE))
    card = [r for r in replies if r["chat_id"] == str(ADMIN_ID) and r["reply_markup"]]
    assert callbacks(card)[:2] == [f"reg_ok:{NEWBIE}", f"reg_no:{NEWBIE}"]
    assert not any(c.startswith("set_role:") for c in callbacks(card))
    assert run(db.get_user(NEWBIE))["status"] == db.USER_PENDING


@pytest.mark.parametrize("text", [B.BTN_MY_SCHEDULE, B.BTN_REQUEST, B.BTN_PROFILE, "/profile", "привет"])
def test_pending_user_sees_nothing_but_basic_commands(tg, text):
    register(tg, NEWBIE, approve=False)
    replies = tg.send(NEWBIE, text)
    assert tg.texts_to(replies, NEWBIE) == [B.PENDING_TEXT]


def test_pending_user_can_start_help_cancel(tg):
    register(tg, NEWBIE, approve=False)
    for cmd in ("/start", "/help", "/cancel"):
        texts = tg.texts_to(tg.send(NEWBIE, cmd), NEWBIE)
        assert texts and "ещё на рассмотрении" in texts[0], cmd
        assert "Моё расписание" not in texts[0]
    replies = tg.press(NEWBIE, "app_req:1")
    assert answers(replies) == [B.PENDING_TEXT]


def test_pending_user_hidden_from_branch_and_broadcast(branch):
    register(branch, NEWBIE, name="Новиков Ной", approve=False)
    text = "\n".join(branch.texts_to(branch.send(MANAGER, B.BTN_BRANCH_SCHEDULE), MANAGER))
    assert "Новиков Ной" not in text
    assert NEWBIE not in [u["telegram_id"] for u in run(db.get_active_users())]
    replies = branch.send(MANAGER, B.BTN_EDIT_SCHEDULE)
    assert f"edit_emp:{NEWBIE}" not in callbacks(replies)


def test_approve_registration(tg):
    register(tg, NEWBIE, approve=False)
    replies = tg.press(ADMIN_ID, f"reg_ok:{NEWBIE}")
    assert answers(replies) == ["Решение сохранено!"]
    user_msg = [r for r in replies if r["chat_id"] == str(NEWBIE)][-1]
    assert "Доступ подтверждён" in user_msg["text"]
    assert "keyboard" in json.loads(user_msg["reply_markup"])  # главное меню
    # после подтверждения — назначение ролей, как раньше
    assert f"set_role:{NEWBIE}:{db.ROLE_MANAGER}" in callbacks(replies)
    assert run(db.get_user(NEWBIE))["status"] == db.USER_ACTIVE
    assert any("Ваше расписание" in t or "нет смен" in t for t in tg.texts_to(tg.send(NEWBIE, B.BTN_MY_SCHEDULE), NEWBIE))


def test_reject_registration(tg):
    register(tg, NEWBIE, approve=False)
    replies = tg.press(ADMIN_ID, f"reg_no:{NEWBIE}")
    assert any("отказано" in t for t in tg.texts_to(replies, NEWBIE))
    assert run(db.get_user(NEWBIE))["status"] == db.USER_INACTIVE
    assert tg.texts_to(tg.send(NEWBIE, "/start"), NEWBIE) == [B.INACTIVE_TEXT]


def test_registration_decided_once(tg):
    register(tg, NEWBIE, approve=False)
    tg.press(ADMIN_ID, f"reg_no:{NEWBIE}")
    replies = tg.press(ADMIN_ID, f"reg_ok:{NEWBIE}")
    assert answers(replies) == ["Заявка уже обработана"]
    assert run(db.get_user(NEWBIE))["status"] == db.USER_INACTIVE
    assert tg.texts_to(replies, NEWBIE) == []


def test_concurrent_registration_decisions(tg):
    import asyncio
    register(tg, NEWBIE, approve=False)

    async def both():
        return await asyncio.gather(db.decide_registration(NEWBIE, True), db.decide_registration(NEWBIE, False))

    results = run(both())
    assert sum(r is not None for r in results) == 1


def test_only_superadmin_decides_registration(branch):
    register(branch, NEWBIE, approve=False)
    for uid in (WORKER, MANAGER):
        assert answers(branch.press(uid, f"reg_ok:{NEWBIE}")) == [B.NO_RIGHTS_TEXT]
    assert run(db.get_user(NEWBIE))["status"] == db.USER_PENDING
    assert answers(branch.press(ADMIN_ID, "reg_ok:abc")) == [B.BAD_CALLBACK_TEXT]


def test_registration_not_delivered_is_reported(tg, monkeypatch):
    from test_fixes import fail_requests
    tg.send(NEWBIE, "/start")
    tg.send(NEWBIE, "Новиков Ной")
    tg.press(NEWBIE, "select_branch:Абая")
    fail_requests(tg, monkeypatch, "sendMessage", chat_id=ADMIN_ID)
    replies = tg.send(NEWBIE, "Бариста")
    assert any("сообщите руководителю лично" in t for t in tg.texts_to(replies, NEWBIE))


# ---------------------------------------------------------------------
# 3. Карточка сотрудника: неактивный, возврат в работу, перевод
# ---------------------------------------------------------------------
def request_status(req_id):
    import sqlite3
    con = sqlite3.connect(db.DB_FILE)
    try:
        return con.execute("SELECT status FROM schedule_requests WHERE id = ?", (req_id,)).fetchone()[0]
    finally:
        con.close()


def test_staff_list_and_card_for_manager(branch):
    replies = branch.send(MANAGER, B.BTN_STAFF)
    data = callbacks(replies)
    assert f"emp:{WORKER}" in data and f"emp:{MANAGER2}" in data and "staff_inactive:Абая" in data
    assert f"emp:{FOREIGN_WORKER}" not in data

    replies = branch.press(MANAGER, f"emp:{WORKER}")
    card = branch.texts_to(replies, MANAGER)[-1]
    for part in ("Иванов Иван", "Бариста", "Абая", "Работник", "Работает"):
        assert part in card
    data = callbacks(replies)
    assert f"edit_emp:{WORKER}" in data and f"emp_off:{WORKER}" in data
    assert f"emp_move:{WORKER}" not in data  # перевод — только Администратор

    own = callbacks(branch.press(MANAGER, f"emp:{MANAGER}"))
    assert f"emp_off:{MANAGER}" not in own


def test_change_shift_from_card(branch):
    branch.press(MANAGER, f"emp:{WORKER}")
    branch.press(MANAGER, f"edit_emp:{WORKER}")
    branch.send(MANAGER, "25.10")
    replies = branch.send(MANAGER, "08:00 - 20:00")
    assert any("успешно обновлено" in t for t in branch.texts_to(replies, MANAGER))


def test_deactivate_employee(branch, monkeypatch):
    monkeypatch.setattr(B, "BROADCAST_DELAY", 0)
    run(db.set_shift(WORKER, "2026-10-05", "08:00 - 20:00"))
    request_change(branch, WORKER)
    req_id = pending_ids("Абая")[0]

    replies = branch.press(MANAGER, f"emp_off:{WORKER}")
    assert answers(replies) == ["Сотрудник отключён"]
    assert B.INACTIVE_TEXT in branch.texts_to(replies, WORKER)
    assert run(db.get_user(WORKER))["status"] == db.USER_INACTIVE
    assert request_status(req_id) == db.REQUEST_REJECTED           # ожидающие заявки закрыты
    assert run(db.get_user_schedule(WORKER)) != []                  # смены остались

    # любое действие — «Доступ закрыт»
    for text in ("/start", B.BTN_MY_SCHEDULE, "/help"):
        assert branch.texts_to(branch.send(WORKER, text), WORKER) == [B.INACTIVE_TEXT]
    assert answers(branch.press(WORKER, "cancel_action")) == [B.INACTIVE_TEXT]

    # не виден в расписании и списках, не получает рассылку и оповещение о паузе
    text = "\n".join(branch.texts_to(branch.send(MANAGER, B.BTN_BRANCH_SCHEDULE), MANAGER))
    assert "Иванов Иван" not in text
    assert f"edit_emp:{WORKER}" not in callbacks(branch.send(MANAGER, B.BTN_EDIT_SCHEDULE))
    assert f"emp:{WORKER}" not in callbacks(branch.send(MANAGER, B.BTN_STAFF))
    assert branch.texts_to(branch.send(ADMIN_ID, B.BTN_PAUSE), WORKER) == []
    branch.send(ADMIN_ID, B.BTN_PAUSE)
    branch.send(ADMIN_ID, B.BTN_BROADCAST)
    branch.send(ADMIN_ID, "Объявление")
    assert branch.texts_to(branch.press(ADMIN_ID, "broadcast_send"), WORKER) == []


def test_inactive_manager_gets_no_requests(branch):
    branch.press(MANAGER, f"emp_off:{MANAGER2}")
    replies = request_change(branch, WORKER)
    assert branch.texts_to(replies, MANAGER2) == []
    assert any("Новый запрос" in t for t in branch.texts_to(replies, MANAGER))
    assert [u["telegram_id"] for u in run(db.get_request_recipients("Абая"))] == [MANAGER]


def test_deactivated_during_input_cannot_finish(branch):
    branch.send(WORKER, B.BTN_REQUEST)
    branch.press(MANAGER, f"emp_off:{WORKER}")
    assert branch.texts_to(branch.send(WORKER, "25.10"), WORKER) == [B.INACTIVE_TEXT]
    run(db.activate_user(WORKER))
    branch.send(WORKER, "08:00 - 20:00")  # состояние ввода сброшено при отключении
    assert pending_ids("Абая") == []


def test_reactivate_employee(branch):
    branch.press(MANAGER, f"emp_off:{WORKER}")
    replies = branch.press(MANAGER, "staff_inactive:Абая")
    assert f"emp:{WORKER}" in callbacks(replies)
    card = callbacks(branch.press(MANAGER, f"emp:{WORKER}"))
    assert f"emp_on:{WORKER}" in card and f"edit_emp:{WORKER}" not in card

    replies = branch.press(MANAGER, f"emp_on:{WORKER}")
    assert answers(replies) == ["Сотрудник снова в работе"]
    assert any("снова открыт" in t for t in branch.texts_to(replies, WORKER))
    assert run(db.get_user(WORKER))["status"] == db.USER_ACTIVE
    assert answers(branch.press(MANAGER, f"emp_on:{WORKER}")) == ["Статус уже изменён"]


def test_edit_shift_of_inactive_is_refused(branch):
    branch.press(MANAGER, f"emp_off:{WORKER}")
    assert answers(branch.press(MANAGER, f"edit_emp:{WORKER}")) == [B.NOT_ACTIVE_TARGET_TEXT]
    assert run(db.get_user_schedule(WORKER)) == []


def test_cannot_deactivate_self(branch):
    assert answers(branch.press(MANAGER, f"emp_off:{MANAGER}")) == ["❌ Нельзя сделать неактивным себя."]
    assert answers(branch.press(ADMIN_ID, f"emp_off:{ADMIN_ID}")) == ["❌ Нельзя сделать неактивным себя."]
    assert run(db.get_user(MANAGER))["status"] == db.USER_ACTIVE


def test_rights_for_card_actions(branch):
    for uid, data in ((WORKER, f"emp_off:{MANAGER}"), (WORKER, f"emp:{MANAGER}"),
                      (MANAGER, f"emp_off:{FOREIGN_WORKER}"), (MANAGER, f"emp:{FOREIGN_WORKER}"),
                      (MANAGER, f"emp_move:{WORKER}"), (MANAGER, f"emp_to:{WORKER}:Гагарина")):
        assert answers(branch.press(uid, data)) == [B.NO_RIGHTS_TEXT], (uid, data)
    assert run(db.get_user(MANAGER))["status"] == db.USER_ACTIVE
    assert run(db.get_user(FOREIGN_WORKER))["status"] == db.USER_ACTIVE
    assert run(db.get_user(WORKER))["branch"] == "Абая"


def test_manager_cannot_deactivate_superadmin(branch):
    run(db.update_user_role(MANAGER2, db.ROLE_SUPERADMIN))  # Администратор, приписанный к «Абая»
    assert answers(branch.press(MANAGER, f"emp_off:{MANAGER2}")) == [B.NO_RIGHTS_TEXT]
    assert run(db.get_user(MANAGER2))["status"] == db.USER_ACTIVE


def test_last_superadmin_cannot_be_deactivated(branch):
    run(db.update_user_role(MANAGER2, db.ROLE_SUPERADMIN))
    assert answers(branch.press(MANAGER2, f"emp_off:{ADMIN_ID}")) == ["Сотрудник отключён"]
    assert run(db.deactivate_user(MANAGER2)) is None  # остался один активный Администратор
    assert run(db.get_user(MANAGER2))["status"] == db.USER_ACTIVE
    # и роль последнего активного Администратора снять нельзя
    branch.press(MANAGER2, f"set_role:{MANAGER2}:{db.ROLE_WORKER}")
    assert run(db.get_user(MANAGER2))["role"] == db.ROLE_SUPERADMIN


def test_two_superadmins_cannot_deactivate_each_other_at_once(branch):
    import asyncio
    run(db.update_user_role(MANAGER2, db.ROLE_SUPERADMIN))

    async def both():
        return await asyncio.gather(db.deactivate_user(ADMIN_ID), db.deactivate_user(MANAGER2))

    assert sum(r is not None for r in run(both())) == 1
    assert run(db.count_superadmins(active_only=True)) == 1


def test_superadmin_moves_employee_to_branch(branch):
    replies = branch.send(ADMIN_ID, B.BTN_STAFF)
    assert "pick_branch:staff:Абая" in callbacks(replies) and "staff_other" in callbacks(replies)
    assert f"emp:{WORKER}" in callbacks(branch.press(ADMIN_ID, "pick_branch:staff:Абая"))
    assert f"emp_move:{WORKER}" in callbacks(branch.press(ADMIN_ID, f"emp:{WORKER}"))

    replies = branch.press(ADMIN_ID, f"emp_move:{WORKER}")
    assert callbacks(replies)[:1] == [f"emp_to:{WORKER}:Гагарина"]
    replies = branch.press(ADMIN_ID, f"emp_to:{WORKER}:Гагарина")
    assert answers(replies) == ["Сотрудник переведён"]
    assert any("Гагарина" in t for t in branch.texts_to(replies, WORKER))
    assert run(db.get_user(WORKER))["branch"] == "Гагарина"
    assert answers(branch.press(ADMIN_ID, f"emp_to:{WORKER}:Гугл")) == ["Такого филиала нет."]


def test_superadmin_moves_test_branch_record(branch):
    run(db.add_user(9001, "Старый Тестов", "Гугл", "Бариста"))
    replies = branch.press(ADMIN_ID, "staff_other")
    data = callbacks(replies)
    assert "emp:9001" in data and f"emp:{ADMIN_ID}" in data and f"emp:{WORKER}" not in data
    assert answers(branch.press(MANAGER, "staff_other")) == [B.NO_RIGHTS_TEXT]
    branch.press(ADMIN_ID, "emp_to:9001:Абая")
    assert run(db.get_user(9001))["branch"] == "Абая"


@pytest.mark.parametrize("data", ["emp:abc", "emp_off:²", "emp_on:", "emp_move:1:2", "emp_to:1",
                                  "staff_inactive:Гугл"])
def test_forged_card_callbacks(branch, data):
    assert answers(branch.press(ADMIN_ID, data)) == [B.BAD_CALLBACK_TEXT]
