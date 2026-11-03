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
