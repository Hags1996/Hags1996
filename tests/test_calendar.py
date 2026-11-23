"""Календарь (мини-приложение): ссылка со сменами, разбор ответа, запрос из календаря."""
import json
from datetime import date, timedelta

import bot as B
import db
from conftest import ADMIN_ID, FIXED_TODAY, register, run

TODAY = date(2026, 11, 17)
URL = "https://example.org/cal/"


def calendar_message(tg, user_id, data):
    """Пользователь нажал «Отправить» в календаре: Telegram присылает web_app_data."""
    uid = tg._next_id()
    return tg._run({"update_id": uid, "message": {
        "message_id": uid, "date": 1700000000,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "web_app_data": {"data": data, "button_text": B.BTN_CALENDAR}}})


# --- ссылка ---

def test_url_has_only_dates_and_codes():
    rows = [{"date_str": "2026-11-18", "shift_time": "09:00 - 18:00"},
            {"date_str": "2026-11-19", "shift_time": "Выходной"},
            {"date_str": "2026-11-20", "shift_time": "с утра"},
            {"date_str": "25.10", "shift_time": "08:00 - 20:00"}]
    assert B.build_calendar_url(URL, TODAY, rows) == \
        URL + "?t=20261117&s=20261118:09001800,20261119:X,20261120:U"


def test_url_keeps_existing_query():
    assert B.build_calendar_url(URL + "?v=2", TODAY, []).startswith(URL + "?v=2&t=20261117")


def test_url_length_is_capped():
    rows = [{"date_str": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "shift_time": "09:00 - 18:00"}
            for i in range(300)]
    url = B.build_calendar_url(URL, TODAY, rows)
    assert url.count(",") == B.CALENDAR_MAX_SHIFTS - 1


# --- разбор ответа календаря ---

def req(d, t, a="req", e=None):
    data = {"a": a, "d": d, "t": t}
    if e is not None:
        data["e"] = e                                     # конец периода включительно
    return json.dumps(data)


def test_parse_ok_and_normalized():
    assert B.parse_calendar_data(req("2026-11-18", "9:00-18:00"), TODAY) == (["2026-11-18"], "09:00 - 18:00")
    assert B.parse_calendar_data(req("2026-11-17", "выходной"), TODAY) == (["2026-11-17"], "Выходной")


def test_parse_rejects_bad_input():
    bad = [None, "", "не json", "[]", req("2026-11-18", "09:00 - 18:00", a="del"),
           req("2026-11-16", "09:00 - 18:00"),        # прошедший день
           req("2028-01-01", "09:00 - 18:00"),        # дальше года
           req("2026-02-30", "09:00 - 18:00"),        # нет такой даты
           req("2026-11-18", "25:00 - 18:00"),        # нет такого времени
           json.dumps({"a": "req", "d": 20261118, "t": "09:00 - 18:00"}),
           req("2026-11-18", "09:00 - 18:00") + " " * B.CALENDAR_DATA_MAX]
    for raw in bad:
        assert B.parse_calendar_data(raw, TODAY) is None, raw


# --- разбор периода «с — по» ---

def test_parse_period_ok():
    assert B.parse_calendar_data(req("2026-11-24", "9:00-18:00", e="2026-11-26"), TODAY) == \
        (["2026-11-24", "2026-11-25", "2026-11-26"], "09:00 - 18:00")
    # через границу месяца, ровно CALENDAR_RANGE_MAX_DAYS дней, начиная с сегодня
    days, shift = B.parse_calendar_data(req("2026-11-28", "Выходной", e="2026-12-04"), TODAY)
    assert (days[0], days[-1], len(days), shift) == ("2026-11-28", "2026-12-04", B.CALENDAR_RANGE_MAX_DAYS, "Выходной")
    assert B.parse_calendar_data(req("2026-11-17", "10:00 - 19:00", e="2026-11-18"), TODAY) == \
        (["2026-11-17", "2026-11-18"], "10:00 - 19:00")


def test_parse_period_of_one_day_and_without_end_are_same():
    one = (["2026-11-18"], "09:00 - 18:00")
    assert B.parse_calendar_data(req("2026-11-18", "09:00 - 18:00"), TODAY) == one
    assert B.parse_calendar_data(req("2026-11-18", "09:00 - 18:00", e="2026-11-18"), TODAY) == one


def test_parse_period_rejects_bad_input():
    far = TODAY + timedelta(days=B.CALENDAR_REQUEST_DAYS)
    bad = [req("2026-11-24", "09:00 - 18:00", e="2026-11-23"),      # конец раньше начала
           req("2026-11-24", "09:00 - 18:00", e="2026-12-01"),      # 8 дней — длиннее лимита
           req("2026-11-24", "09:00 - 18:00", e="2027-11-24"),      # год
           req("2026-11-16", "09:00 - 18:00", e="2026-11-18"),      # начало в прошлом
           req("2026-11-10", "09:00 - 18:00", e="2026-11-12"),      # весь период в прошлом
           req(far.isoformat(), "09:00 - 18:00", e=(far + timedelta(days=1)).isoformat()),  # конец дальше года
           req("2026-11-24", "09:00 - 18:00", e="завтра"),          # мусор
           req("2026-11-24", "09:00 - 18:00", e=""),
           req("2026-11-24", "09:00 - 18:00", e="2026-02-30"),      # нет такой даты
           req("2026-11-24", "09:00 - 18:00", e=20261126),          # не строка
           req("2026-11-24", "09:00 - 18:00", e=["2026-11-26"]),
           json.dumps({"a": "req", "d": "2026-11-24", "e": None, "t": "09:00 - 18:00"}),
           req("2026-11-24", "25:00 - 18:00", e="2026-11-26")]      # нет такого времени
    for raw in bad:
        assert B.parse_calendar_data(raw, TODAY) is None, raw


def test_period_answer_fits_data_limit():
    longest = json.dumps({"a": "req", "d": "2026-11-24", "e": "2026-11-30", "t": "09:00 - 18:00"})
    assert len(longest) < B.CALENDAR_DATA_MAX
    assert B.parse_calendar_data(longest, TODAY) is not None


# --- кнопка в меню ---

def keyboards_to(replies, chat_id):
    return [r["reply_markup"] or "" for r in replies if r["chat_id"] == str(chat_id)]


def test_no_calendar_button_without_url(tg, monkeypatch):
    monkeypatch.setattr(B, "WEBAPP_URL", "")
    replies = tg.send(ADMIN_ID, "/start")
    assert not any("web_app" in k for k in keyboards_to(replies, ADMIN_ID))


def test_start_and_my_schedule_give_fresh_calendar(tg, monkeypatch):
    monkeypatch.setattr(B, "WEBAPP_URL", URL)
    run(db.set_shift(ADMIN_ID, FIXED_TODAY.isoformat(), "09:00 - 18:00"))
    for text in ("/start", B.BTN_MY_SCHEDULE):
        replies = tg.send(ADMIN_ID, text)
        marks = [k for k in keyboards_to(replies, ADMIN_ID) if "web_app" in k]
        assert marks, text
        assert f"{FIXED_TODAY:%Y%m%d}:09001800" in marks[-1]


# --- запрос из календаря ---

def test_calendar_request_goes_to_management(tg):
    register(tg, 222)
    day = FIXED_TODAY.replace(day=FIXED_TODAY.day + 1).isoformat()
    replies = calendar_message(tg, 222, req(day, "10:00 - 19:00"))
    assert any("успешно отправлен" in t for t in tg.texts_to(replies, 222))
    assert any("Новый запрос на изменение расписания" in t and "10:00 - 19:00" in t
               for t in tg.texts_to(replies, ADMIN_ID))
    pending = run(db.get_pending_requests("Абая"))
    assert [(p["date_str"], p["desired_time"]) for p in pending] == [(day, "10:00 - 19:00")]


def test_calendar_bad_data_is_refused(tg):
    register(tg, 222)
    replies = calendar_message(tg, 222, req("2000-01-01", "10:00 - 19:00"))
    assert any("Не получилось принять запрос из календаря" in t for t in tg.texts_to(replies, 222))
    assert run(db.get_pending_requests("Абая")) == []


def test_calendar_needs_registration_and_access(tg):
    replies = calendar_message(tg, 333, req(FIXED_TODAY.isoformat(), "10:00 - 19:00"))
    assert any("не зарегистрированы" in t for t in tg.texts_to(replies, 333))

    register(tg, 222, approve=False)                      # ждёт подтверждения
    replies = calendar_message(tg, 222, req(FIXED_TODAY.isoformat(), "10:00 - 19:00"))
    assert any("на рассмотрении" in t for t in tg.texts_to(replies, 222))
    assert run(db.get_pending_requests("Абая")) == []


def test_calendar_period_makes_request_per_day_and_one_answer(tg, monkeypatch):
    monkeypatch.setattr(B, "BROADCAST_DELAY", 0)
    register(tg, 222)
    days = [(FIXED_TODAY + timedelta(days=i)).isoformat() for i in (1, 2, 3)]
    replies = calendar_message(tg, 222, req(days[0], "10:00 - 19:00", e=days[-1]))

    to_worker = tg.texts_to(replies, 222)                 # сотруднику — одно итоговое сообщение
    assert len(to_worker) == 1
    assert "на период" in to_worker[0] and "3 дня" in to_worker[0] and "10:00 - 19:00" in to_worker[0]
    assert "05.10.2026" in to_worker[0] and "07.10.2026" in to_worker[0]

    to_admin = [r for r in replies if r["chat_id"] == str(ADMIN_ID) and r["method"] == "sendMessage"]
    assert len(to_admin) == 3                             # руководству — обычная заявка на каждый день
    assert all("Новый запрос на изменение расписания" in r["text"] and "app_req:" in (r["reply_markup"] or "")
               for r in to_admin)
    pending = run(db.get_pending_requests("Абая"))
    assert sorted((p["date_str"], p["desired_time"]) for p in pending) == [(d, "10:00 - 19:00") for d in days]


def test_calendar_period_request_is_decided_per_day(tg, monkeypatch):
    monkeypatch.setattr(B, "BROADCAST_DELAY", 0)
    register(tg, 222)
    days = [(FIXED_TODAY + timedelta(days=i)).isoformat() for i in (1, 2)]
    calendar_message(tg, 222, req(days[0], "Выходной", e=days[1]))
    first = min(p["id"] for p in run(db.get_pending_requests("Абая")))
    tg.press(ADMIN_ID, f"app_req:{first}")            # обычная кнопка обычной заявки
    assert [p["date_str"] for p in run(db.get_pending_requests("Абая"))] == [days[1]]


def test_calendar_bad_period_creates_nothing(tg):
    register(tg, 222)
    start = (FIXED_TODAY + timedelta(days=1)).isoformat()
    for end in ((FIXED_TODAY + timedelta(days=1 + B.CALENDAR_RANGE_MAX_DAYS)).isoformat(),   # длиннее лимита
                FIXED_TODAY.isoformat(),                                                      # конец раньше начала
                "мусор"):
        replies = calendar_message(tg, 222, req(start, "10:00 - 19:00", e=end))
        assert tg.texts_to(replies, 222) == [B.BAD_CALENDAR_TEXT], end
        assert tg.texts_to(replies, ADMIN_ID) == []
    assert run(db.get_pending_requests("Абая")) == []


def test_calendar_period_without_management_gets_one_honest_answer(tg, monkeypatch):
    monkeypatch.setattr(B, "BROADCAST_DELAY", 0)
    days = [(FIXED_TODAY + timedelta(days=i)).isoformat() for i in (1, 2)]
    replies = calendar_message(tg, ADMIN_ID, req(days[0], "10:00 - 19:00", e=days[1]))   # свою заявку не получает
    to_self = tg.texts_to(replies, ADMIN_ID)
    assert len(to_self) == 1 and "доставить руководству сейчас не удалось 2 из 2" in to_self[0]
