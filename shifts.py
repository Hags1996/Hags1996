"""Разбор и форматирование дат и времени смен.

В БД дата хранится как ГГГГ-ММ-ДД: такая строка правильно сортируется
(«2026-10-25» < «2026-11-01»), а «25.10» и «01.11» — нет.
"""
import re
from datetime import date, timedelta

DAY_OFF = "Выходной"

_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$")
_SHIFT_RE = re.compile(r"^(\d{1,2})[:.](\d{2})\s*[-–—]?\s*(\d{1,2})[:.](\d{2})$")


def _today() -> date:
    """Текущая дата. Отдельной функцией, чтобы тесты могли её подменить."""
    return date.today()


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(text: str, today: date | None = None) -> str | None:
    """«25.10» или «25.10.2026» → «2026-10-25». None, если это не дата.

    Без года берётся ближайшая такая дата: если она прошла больше месяца
    назад, значит имеется в виду следующий год (в декабре «05.01» — январь).
    «29.02» в невисокосный год — 29 февраля следующего года, если оно есть.
    """
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    today = today or _today()
    day, month = int(m.group(1)), int(m.group(2))
    if m.group(3):
        result = _make_date(int(m.group(3)), month, day)
        return result.isoformat() if result else None

    result = _make_date(today.year, month, day)
    if result is None or result < today - timedelta(days=30):
        result = _make_date(today.year + 1, month, day)
    return result.isoformat() if result else None


def parse_date_nearest(text: str, today: date) -> str | None:
    """Для миграции старых записей «ДД.ММ»: год, при котором дата ближе всего к today.

    В старой базе лежит и история, и будущие смены, поэтому правило
    parse_date («прошло больше месяца → следующий год») здесь не подходит:
    оно перенесло бы прошлые смены на год вперёд. Ближайший год — это
    окно ±6 месяцев вокруг дня миграции. Дата с годом разбирается как есть.
    """
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    if m.group(3):
        return parse_date(text, today)
    day, month = int(m.group(1)), int(m.group(2))
    candidates = [d for d in (_make_date(today.year + k, month, day) for k in (-1, 0, 1)) if d]
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - today).days)).isoformat()


def parse_shift(text: str) -> str | None:
    """«8:00-20:00», «08:00 - 20:00», «8.00 20.00» → «08:00 - 20:00»; «выходной» → «Выходной»."""
    text = text.strip()
    if text.lower() == DAY_OFF.lower():
        return DAY_OFF
    m = _SHIFT_RE.match(text)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60):
        return None
    return f"{h1:02d}:{m1:02d} - {h2:02d}:{m2:02d}"


def format_date(value: str) -> str:
    """«2026-10-25» → «25.10.2026». Старые записи в другом формате показываются как есть."""
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value
