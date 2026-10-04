"""Тестовый стенд: настоящие хэндлеры бота, поддельный Telegram API, временная БД.

Запросы к Telegram не уходят: asyncio_helper._process_request подменён и
складывает вызовы в список `tg.sent`, чтобы тест мог проверить, что бот ответил.
"""
import asyncio
import json
import os
import sys

import pytest

os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["FIRST_SUPERADMIN_ID"] = "111"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telebot import asyncio_filters, asyncio_helper, types  # noqa: E402
from telebot.asyncio_storage import StateMemoryStorage  # noqa: E402

import bot as B  # noqa: E402
import db  # noqa: E402

B.bot.add_custom_filter(asyncio_filters.StateFilter(B.bot))
B.bot.add_custom_filter(asyncio_filters.IsDigitFilter())

ADMIN_ID = 111


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self._update_id = 0

    async def request(self, token, url, method="get", params=None, files=None, **kwargs):
        params = params or {}
        self.sent.append({"method": url, "chat_id": params.get("chat_id"), "text": params.get("text", ""),
                          "reply_markup": params.get("reply_markup")})
        if url.startswith("send") or url.startswith("edit"):
            return {"message_id": 1, "date": 1700000000, "chat": {"id": 1, "type": "private"}}
        return True

    def _next_id(self):
        self._update_id += 1
        return self._update_id

    def _run(self, update_dict):
        before = len(self.sent)
        update = types.Update.de_json(json.dumps(update_dict))
        asyncio.run(B.bot.process_new_updates([update]))
        return self.sent[before:]

    def send(self, user_id, text):
        """Пользователь пишет боту текст. Возвращает ответы бота."""
        uid = self._next_id()
        message = {"message_id": uid, "date": 1700000000, "text": text,
                   "chat": {"id": user_id, "type": "private"},
                   "from": {"id": user_id, "is_bot": False, "first_name": "Test"}}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return self._run({"update_id": uid, "message": message})

    def press(self, user_id, data):
        """Пользователь жмёт инлайн-кнопку с callback_data=data."""
        uid = self._next_id()
        return self._run({"update_id": uid, "callback_query": {
            "id": str(uid), "chat_instance": "x", "data": data,
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {"message_id": 1, "date": 1700000000, "text": "...",
                        "chat": {"id": user_id, "type": "private"},
                        "from": {"id": 1, "is_bot": True, "first_name": "bot"}}}})

    def texts_to(self, replies, chat_id):
        return [r["text"] for r in replies if r["chat_id"] == str(chat_id) and r["method"] == "sendMessage"]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tg(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "test.db"))
    B.bot.current_states = StateMemoryStorage()
    fake = FakeTelegram()
    monkeypatch.setattr(asyncio_helper, "_process_request", fake.request)
    run(db.init_db(ADMIN_ID))
    return fake


def register(tg, user_id, name="Иванов Иван", branch="Абая", position="Бариста"):
    tg.send(user_id, "/start")
    tg.send(user_id, name)
    tg.press(user_id, f"select_branch:{branch}")
    tg.send(user_id, position)
