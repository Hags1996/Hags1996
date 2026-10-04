"""Проверки прав и ввода.

callback_data кнопки — это просто строка от клиента. Её можно отправить боту,
даже не получив сообщения с этой кнопкой (сторонний клиент, userbot). Поэтому
каждый обработчик кнопки сам проверяет, кто её нажал.
"""
import db
from conftest import ADMIN_ID, register, run

WORKER, OTHER_WORKER, MANAGER, FOREIGN_MANAGER = 222, 223, 333, 444


def setup_branch(tg):
    register(tg, WORKER, name="Иванов Иван", branch="Абая")
    register(tg, OTHER_WORKER, name="Сидоров Сидор", branch="Абая")
    register(tg, MANAGER, name="Петров Пётр", branch="Абая")
    register(tg, FOREIGN_MANAGER, name="Чужой Менеджер", branch="Гагарина")
    run(db.update_user_role(MANAGER, db.ROLE_MANAGER))
    run(db.update_user_role(FOREIGN_MANAGER, db.ROLE_MANAGER))


def make_request(tg):
    tg.send(WORKER, "✏️ Запросить изменение расписания")
    tg.send(WORKER, "25.10")
    tg.send(WORKER, "08:00 - 20:00")
    return run(db.get_pending_requests("Абая"))[0]["id"]


def test_worker_cannot_make_himself_admin(tg):
    setup_branch(tg)
    tg.press(WORKER, f"set_role:{WORKER}:{db.ROLE_SUPERADMIN}")
    assert run(db.get_user(WORKER))["role"] == db.ROLE_WORKER


def test_worker_cannot_approve_requests(tg):
    setup_branch(tg)
    req_id = make_request(tg)
    tg.press(OTHER_WORKER, f"app_req:{req_id}")
    assert run(db.get_user_schedule(WORKER)) == []


def test_manager_cannot_approve_other_branch(tg):
    setup_branch(tg)
    req_id = make_request(tg)
    tg.press(FOREIGN_MANAGER, f"app_req:{req_id}")
    assert run(db.get_user_schedule(WORKER)) == []


def test_worker_cannot_edit_someone_schedule(tg):
    setup_branch(tg)
    tg.press(OTHER_WORKER, f"edit_emp:{WORKER}")
    tg.send(OTHER_WORKER, "25.10")
    tg.send(OTHER_WORKER, "Выходной")
    assert run(db.get_user_schedule(WORKER)) == []


def test_manager_cannot_edit_other_branch_schedule(tg):
    setup_branch(tg)
    tg.press(FOREIGN_MANAGER, f"edit_emp:{WORKER}")
    tg.send(FOREIGN_MANAGER, "25.10")
    tg.send(FOREIGN_MANAGER, "Выходной")
    assert run(db.get_user_schedule(WORKER)) == []


def test_forged_branch_is_rejected(tg):
    tg.send(WORKER, "/start")
    tg.send(WORKER, "Иванов Иван")
    tg.press(WORKER, "select_branch:Несуществующий")
    tg.send(WORKER, "Бариста")
    assert run(db.get_user(WORKER)) is None


def test_last_admin_cannot_be_demoted(tg):
    tg.press(ADMIN_ID, f"set_role:{ADMIN_ID}:{db.ROLE_WORKER}")
    assert run(db.get_user(ADMIN_ID))["role"] == db.ROLE_SUPERADMIN


def test_html_in_name_is_escaped(tg):
    replies = []
    tg.send(WORKER, "/start")
    tg.send(WORKER, "<b>Хакер</b> <a href='http://evil'>жми</a>")
    tg.press(WORKER, "select_branch:Абая")
    replies = tg.send(WORKER, "Бариста")
    admin_note = tg.texts_to(replies, ADMIN_ID)[0]
    assert "<a href" not in admin_note
    assert "&lt;b&gt;Хакер" in admin_note


def test_unregistered_user_cannot_request_change(tg):
    replies = tg.send(WORKER, "✏️ Запросить изменение расписания")
    assert any("/start" in t for t in tg.texts_to(replies, WORKER))
    tg.send(WORKER, "25.10")
    tg.send(WORKER, "08:00 - 20:00")
    assert run(db.get_pending_requests("Абая")) == []
