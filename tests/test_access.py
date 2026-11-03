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
