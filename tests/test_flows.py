"""Основные сценарии бота: регистрация, заявки, расписание филиала."""
import db
from conftest import ADMIN_ID, register, run


def test_existing_user_start_gets_welcome_back(tg):
    replies = tg.send(ADMIN_ID, "/start")
    assert any("С возвращением" in t for t in tg.texts_to(replies, ADMIN_ID))


def test_new_user_full_registration(tg):
    replies = tg.send(222, "/start")
    assert any("Введите ваше ФИО" in t for t in tg.texts_to(replies, 222))

    replies = tg.send(222, "Иванов Иван")
    assert any("Выберите ваш филиал" in t for t in tg.texts_to(replies, 222))

    replies = tg.press(222, "select_branch:Абая")
    assert any("Введите вашу должность" in t for t in tg.texts_to(replies, 222))

    replies = tg.send(222, "Бариста")
    assert any("Регистрация успешно завершена" in t for t in tg.texts_to(replies, 222))
    assert any("Новый пользователь" in t for t in tg.texts_to(replies, ADMIN_ID))

    user = run(db.get_user(222))
    assert (user["full_name"], user["branch"], user["position"], user["role"]) == \
        ("Иванов Иван", "Абая", "Бариста", db.ROLE_WORKER)


def test_cancel_during_registration(tg):
    tg.send(222, "/start")
    replies = tg.send(222, "/cancel")
    assert any("Действие отменено" in t for t in tg.texts_to(replies, 222))
    assert run(db.get_user(222)) is None


def test_request_approved_by_manager_updates_schedule(tg):
    register(tg, 222, branch="Абая")
    register(tg, 333, name="Петров Пётр", branch="Абая", position="Менеджер")
    run(db.update_user_role(333, db.ROLE_MANAGER))

    tg.send(222, "✏️ Запросить изменение расписания")
    tg.send(222, "25.10")
    replies = tg.send(222, "08:00 - 20:00")
    assert any("Новый запрос" in t for t in tg.texts_to(replies, 333))

    req = run(db.get_pending_requests("Абая"))[0]
    replies = tg.press(333, f"app_req:{req['id']}")
    assert any("было изменено" in t for t in tg.texts_to(replies, 222))
    assert len(run(db.get_user_schedule(222))) == 1


def test_branch_schedule_lists_everyone(tg):
    register(tg, 222, name="Иванов Иван", branch="Абая")
    register(tg, 333, name="Петров Пётр", branch="Абая")
    run(db.update_user_role(333, db.ROLE_MANAGER))
    run(db.set_shift(222, "25.10", "08:00 - 20:00"))

    text = tg.texts_to(tg.send(333, "🏢 Расписание филиала"), 333)[0]
    assert "Иванов Иван" in text and "08:00 - 20:00" in text
    assert "Петров Пётр" in text and "Расписание отсутствует" in text


def test_manager_edits_employee_schedule(tg):
    register(tg, 222, branch="Абая")
    register(tg, 333, name="Петров Пётр", branch="Абая")
    run(db.update_user_role(333, db.ROLE_MANAGER))

    tg.send(333, "🛠 Изменить расписание сотрудника")
    tg.press(333, "edit_emp:222")
    tg.send(333, "25.10")
    replies = tg.send(333, "08:00 - 20:00")
    assert any("успешно обновлено" in t for t in tg.texts_to(replies, 333))
    assert len(run(db.get_user_schedule(222))) == 1
