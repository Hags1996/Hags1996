import asyncio
import hashlib
import hmac
import html
import logging
import os
import pickle
import re
import secrets
import sys
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

# .env рядом с bot.py (без поиска по родительским папкам); до import db: DB_FILE читается из окружения
load_dotenv(Path(__file__).resolve().parent / ".env")

import telebot
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage, StatePickleStorage
from telebot.asyncio_handler_backends import State, StatesGroup, BaseMiddleware, CancelUpdate
from telebot import asyncio_filters
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)

import db
import shifts
from shifts import parse_date, parse_shift, format_date
from db import ROLE_WORKER, ROLE_MANAGER, ROLE_BRANCH_ADMIN, ROLE_SUPERADMIN, MANAGEMENT_ROLES
from db import USER_PENDING, USER_ACTIVE, USER_INACTIVE

# =====================================================================
# КОНФИГУРАЦИЯ БОТА
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "bot.log"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_first_admin = os.getenv("FIRST_SUPERADMIN_ID", "").strip()
if not BOT_TOKEN or not re.fullmatch(r"\d+", _first_admin, re.ASCII):
    sys.exit("Заполните BOT_TOKEN и FIRST_SUPERADMIN_ID (число) в файле .env — образец в .env.example")
FIRST_SUPERADMIN_ID = int(_first_admin)

# Соль для меток пользователей в логах. Без неё метки меняются при каждом запуске.
LOG_SALT = os.getenv("LOG_SALT", "").strip()
_LOG_SALT_GENERATED = not LOG_SALT
if _LOG_SALT_GENERATED:
    LOG_SALT = secrets.token_hex(16)

logger = logging.getLogger(__name__)

# Состояния FSM — в файле рядом с базой, чтобы перезапуск не обрывал начатые сценарии
STATE_FILE = Path(db.DB_FILE).parent / "bot_states.pkl"


def uid_tag(user_id) -> str:
    """Метка пользователя для логов вместо Telegram ID: 8 символов HMAC-SHA256 с солью LOG_SALT."""
    return hmac.new(LOG_SALT.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:8]


def setup_logging(log_file=LOG_FILE):
    """Логи в консоль и в файл с ротацией (1 МБ × 5). Вызывается из main(), не при импорте."""
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    console_handler = logging.StreamHandler()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    if _LOG_SALT_GENERATED:
        logger.warning("LOG_SALT не задан в .env — метки пользователей в логах будут другими после перезапуска.")
    return file_handler


class FileStateStorage(StatePickleStorage):
    """StatePickleStorage без зависимости от aiofiles: файл читается и пишется в отдельном потоке,
    чтобы не блокировать цикл событий."""

    def __init__(self, file_path, prefix="telebot", separator=":"):
        self.file_path = str(file_path)
        self.prefix = prefix
        self.separator = separator
        self.lock = asyncio.Lock()
        self.create_dir()

    async def _read_from_file(self) -> dict:
        return await asyncio.to_thread(self._read_sync)

    async def _write_to_file(self, data: dict) -> None:
        await asyncio.to_thread(self._write_sync, data)

    def _read_sync(self) -> dict:
        with open(self.file_path, "rb") as f:
            return pickle.load(f)

    def _write_sync(self, data: dict) -> None:
        tmp = self.file_path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(data, f)
        os.replace(tmp, self.file_path)


# Хранилище в памяти до запуска; main() подключает файловое (тесты работают без файла)
bot = AsyncTeleBot(BOT_TOKEN, state_storage=StateMemoryStorage())

# Названия ролей и филиалов
ROLE_NAMES = {
    ROLE_WORKER: "Работник",
    ROLE_MANAGER: "Менеджер",
    ROLE_BRANCH_ADMIN: "Управляющий",
    ROLE_SUPERADMIN: "Администратор"
}

STATUS_NAMES = {
    USER_PENDING: "⏳ Ждёт подтверждения",
    USER_ACTIVE: "✅ Работает",
    USER_INACTIVE: "🚫 Неактивен",
}

BRANCHES = ["Гагарина", "Абая"]

SCHEDULE_DAYS = 14          # сколько дней расписания показывать
MAX_MESSAGE_LEN = 4096      # лимит Telegram на длину сообщения
MAX_NAME_LEN = 64           # ФИО и должность
BROADCAST_DELAY = 0.05      # пауза между сообщениями рассылки (лимит Telegram ~30 сообщений/с)

# Тексты кнопок главного меню: на шагах ввода их нельзя принять как данные
BTN_MY_SCHEDULE = "📅 Моё расписание"
BTN_REQUEST = "✏️ Запросить изменение расписания"
BTN_PROFILE = "👤 Мой профиль"
BTN_BRANCH_SCHEDULE = "🏢 Расписание филиала"
BTN_REQUESTS = "📥 Запросы на изменение"
BTN_EDIT_SCHEDULE = "🛠 Изменить расписание сотрудника"
BTN_STAFF = "👥 Сотрудники"
BTN_SET_ROLE = "👤 Назначить роль"
BTN_BROADCAST = "📢 Глобальная рассылка"
BTN_PAUSE = "⏸ Приостановить/Запустить бота"
MENU_TEXTS = {
    BTN_MY_SCHEDULE, BTN_REQUEST, BTN_PROFILE, BTN_BRANCH_SCHEDULE, BTN_REQUESTS,
    BTN_EDIT_SCHEDULE, BTN_STAFF, BTN_SET_ROLE, BTN_BROADCAST, BTN_PAUSE,
}

# =====================================================================
# FSM СОСТОЯНИЯ
# =====================================================================
class RegistrationState(StatesGroup):
    full_name = State()
    branch = State()
    position = State()

class RequestScheduleState(StatesGroup):
    date_str = State()
    desired_time = State()

class EditScheduleState(StatesGroup):
    target_user_id = State()
    date_str = State()
    shift_time = State()

class AdminSetRoleState(StatesGroup):
    target_user_id = State()

class AdminBroadcastState(StatesGroup):
    message_text = State()
    confirm = State()

ALL_STATES = [
    RegistrationState.full_name, RegistrationState.branch, RegistrationState.position,
    RequestScheduleState.date_str, RequestScheduleState.desired_time,
    EditScheduleState.target_user_id, EditScheduleState.date_str, EditScheduleState.shift_time,
    AdminSetRoleState.target_user_id,
    AdminBroadcastState.message_text, AdminBroadcastState.confirm,
]

# =====================================================================
# ПРОВЕРКА РОЛИ И ВВОДА
# =====================================================================
async def get_user_with_role(user_id: int, roles):
    """Активный пользователь из БД, если у него одна из ролей roles, иначе None."""
    user = await db.get_user(user_id)
    if user and user['role'] in roles and user['status'] == USER_ACTIVE:
        return user
    return None


def can_manage_branch(manager, branch: str) -> bool:
    """Руководитель управляет своим филиалом, Администратор — любым."""
    return manager['role'] == ROLE_SUPERADMIN or manager['branch'] == branch


def esc(value) -> str:
    """Экранирование пользовательского текста перед вставкой в HTML-сообщение."""
    return html.escape(str(value))


_ID_RE = r"\d{1,18}"  # только ASCII-цифры: «²».isdigit() == True, но int("²") падает


def parse_id(text) -> int | None:
    """Telegram ID или номер заявки из строки; None, если это не число."""
    text = (text or "").strip()
    if re.fullmatch(_ID_RE, text, re.ASCII):
        return int(text)
    return None


def parse_callback(data: str, pattern: str):
    """Разбор callback_data по шаблону. Кнопку может прислать поддельный клиент,
    поэтому всё, что не совпало целиком, — None."""
    m = re.fullmatch(pattern, data or "", re.ASCII)
    return m.groups() if m else None


def is_menu_or_command(message) -> bool:
    text = (message.text or "").strip()
    return text in MENU_TEXTS or text.startswith("/")


def text_len(text: str) -> int:
    """Длина так, как её считает Telegram (UTF-16): эмодзи — две единицы."""
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Режет длинный текст на части не длиннее limit по границам строк."""
    parts, current = [], ""
    for line in text.split("\n"):
        while text_len(line) > limit:  # одна строка длиннее лимита — режем по символам
            cut = limit
            while text_len(line[:cut]) > limit:
                cut -= 1
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:cut])
            line = line[cut:]
        candidate = f"{current}\n{line}" if current else line
        if text_len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        parts.append(current)
    return [p for p in parts if p.strip()]


async def send_long(chat_id, text: str, **kwargs):
    for part in split_message(text):
        await bot.send_message(chat_id, part, **kwargs)


def schedule_period():
    """Период показа расписания: от сегодня на SCHEDULE_DAYS дней (включительно)."""
    today = shifts._today()
    return today.isoformat(), (today + timedelta(days=SCHEDULE_DAYS - 1)).isoformat()


async def mass_send(users, text: str, what: str) -> tuple[int, int]:
    """Отправка всем users с паузой между сообщениями. Возвращает (доставлено, не доставлено).
    В лог — только метка пользователя и тип ошибки, без текста сообщения."""
    sent = failed = 0
    for i, u in enumerate(users):
        if i:
            await asyncio.sleep(BROADCAST_DELAY)
        try:
            await bot.send_message(u['telegram_id'], text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning("%s: не доставлено %s (%s)", what, uid_tag(u['telegram_id']), type(e).__name__)
    return sent, failed


BAD_DATE_TEXT = (
    "❗️ Не получилось распознать дату. Введите её в формате <b>ДД.ММ</b>, например <code>25.10</code>.\n\n"
    "<i>Для отмены введите /cancel</i>"
)
BAD_SHIFT_TEXT = (
    "❗️ Не получилось распознать время. Введите <code>08:00 - 20:00</code> или <code>Выходной</code>.\n\n"
    "<i>Для отмены введите /cancel</i>"
)
NOT_REGISTERED_TEXT = "⚠️ Вы еще не зарегистрированы в системе. Нажмите /start для регистрации."
BAD_CALLBACK_TEXT = "Некорректные данные"
NO_RIGHTS_TEXT = "⛔️ Недостаточно прав."
OWN_REQUEST_TEXT = "⛔️ Нельзя решать собственную заявку."
NOT_ACTIVE_TARGET_TEXT = "⚠️ Сотрудник неактивен."

# =====================================================================
# КЛАВИАТУРЫ И ИНТЕРФЕЙС
# =====================================================================
def get_main_keyboard(role: str) -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton(BTN_MY_SCHEDULE))
    markup.add(KeyboardButton(BTN_REQUEST))
    markup.add(KeyboardButton(BTN_PROFILE))

    if role in MANAGEMENT_ROLES:
        markup.add(KeyboardButton(BTN_BRANCH_SCHEDULE))
        markup.add(KeyboardButton(BTN_REQUESTS))
        markup.add(KeyboardButton(BTN_EDIT_SCHEDULE))
        markup.add(KeyboardButton(BTN_STAFF))

    if role == ROLE_SUPERADMIN:
        markup.add(KeyboardButton(BTN_SET_ROLE))
        markup.add(KeyboardButton(BTN_BROADCAST))
        markup.add(KeyboardButton(BTN_PAUSE))

    return markup

def get_branches_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for b in BRANCHES:
        markup.add(InlineKeyboardButton(text=f"🏢 {b}", callback_data=f"select_branch:{b}"))
    markup.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action"))
    return markup

def get_pick_branch_keyboard(action: str) -> InlineKeyboardMarkup:
    """Выбор филиала Администратором перед действием action (sched / reqs / edit / staff)."""
    markup = InlineKeyboardMarkup()
    for b in BRANCHES:
        markup.add(InlineKeyboardButton(text=f"🏢 {b}", callback_data=f"pick_branch:{action}:{b}"))
    if action == "staff":
        # Записи со старым или неизвестным филиалом (например, тестовым «Гугл») — перевести через карточку
        markup.add(InlineKeyboardButton(text="❓ Филиал не из списка", callback_data="staff_other"))
    markup.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action"))
    return markup

def get_roles_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="👷 Работник", callback_data=f"set_role:{target_user_id}:{ROLE_WORKER}"))
    markup.add(InlineKeyboardButton(text="🧑‍💼 Менеджер", callback_data=f"set_role:{target_user_id}:{ROLE_MANAGER}"))
    markup.add(InlineKeyboardButton(text="🏢 Управляющий", callback_data=f"set_role:{target_user_id}:{ROLE_BRANCH_ADMIN}"))
    markup.add(InlineKeyboardButton(text="👑 Второй Администратор", callback_data=f"set_role:{target_user_id}:{ROLE_SUPERADMIN}"))
    markup.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action"))
    return markup

def get_registration_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"reg_ok:{target_user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reg_no:{target_user_id}")
    )
    return markup

def get_request_keyboard(req_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Принять", callback_data=f"app_req:{req_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_req:{req_id}")
    )
    return markup

# =====================================================================
# МИДДЛВЕЙР: СТАТУС ПОЛЬЗОВАТЕЛЯ И ПАУЗА БОТА
# =====================================================================
PAUSED_TEXT = "⛔️ Бот временно остановлен администратором."
INACTIVE_TEXT = "🚫 Доступ закрыт, обратитесь к руководителю."
PENDING_TEXT = "⏳ Ваша заявка ещё на рассмотрении."
# Что доступно, пока заявка на доступ не подтверждена
PENDING_ALLOWED_COMMANDS = {"/start", "/help", "/cancel"}
CANCEL_TEXTS = {"❌ Отмена", "Отмена"}


def _allowed_while_pending(update) -> bool:
    if isinstance(update, telebot.types.CallbackQuery):
        return update.data == "cancel_action"
    text = (update.text or "").strip()
    if text in CANCEL_TEXTS:
        return True
    command = text.split()[0].split("@")[0].lower() if text else ""
    return command in PENDING_ALLOWED_COMMANDS


class AccessMiddleware(BaseMiddleware):
    """Общая проверка до любых обработчиков, в том числе шагов FSM:
    неактивный — доступ закрыт; ждущий подтверждения — только /start, /help, /cancel;
    пауза — для всех, кроме Администраторов."""

    def __init__(self):
        super().__init__()
        self.update_types = ['message', 'callback_query']

    @staticmethod
    async def _refuse(update, text):
        if isinstance(update, telebot.types.CallbackQuery):
            await bot.answer_callback_query(update.id, text, show_alert=True)
        else:
            await bot.send_message(update.chat.id, text)
        return CancelUpdate()

    async def pre_process(self, update, data):
        user = await db.get_user(update.from_user.id)
        status = user['status'] if user else None
        if status == USER_INACTIVE:
            logger.debug("access: user=%s неактивен", uid_tag(update.from_user.id))
            return await self._refuse(update, INACTIVE_TEXT)
        if status == USER_PENDING and not _allowed_while_pending(update):
            return await self._refuse(update, PENDING_TEXT)

        if not await db.is_bot_paused():
            return None
        role = user['role'] if user else None
        logger.debug("pause: user=%s, role=%s", uid_tag(update.from_user.id), role)
        if role == ROLE_SUPERADMIN:
            return None
        return await self._refuse(update, PAUSED_TEXT)

    async def post_process(self, update, data, exception):
        pass


bot.setup_middleware(AccessMiddleware())

# =====================================================================
# СИСТЕМНЫЕ ХЭНДЛЕРЫ: ОТМЕНА, СПРАВКА, ПРОФИЛЬ
# Отмена зарегистрирована первой и без фильтра state: работает и в любом
# состоянии FSM, и без него (state="*" в telebot 4.37 не срабатывает без состояния).
# =====================================================================
@bot.message_handler(commands=['cancel'])
@bot.message_handler(func=lambda m: m.text in CANCEL_TEXTS)
async def cmd_cancel(message: telebot.types.Message):
    """Сброс любого текущего состояния FSM и возврат в главное меню."""
    current_state = await bot.get_state(message.from_user.id, message.chat.id)
    user = await db.get_user(message.from_user.id)

    if current_state is not None:
        await bot.delete_state(message.from_user.id, message.chat.id)
        text = "❌ Действие отменено. Вы вернулись в главное меню."
    else:
        text = "ℹ️ Нет активных действий для отмены."

    if user and user['status'] == USER_ACTIVE:
        markup = get_main_keyboard(user['role'])
    elif user:
        # Заявка на доступ ещё не подтверждена — меню пока нет
        markup = ReplyKeyboardRemove()
        text += f"\n\n{PENDING_TEXT}"
    else:
        # Незарегистрированному не показываем меню работника — у него ещё ничего нет
        markup = ReplyKeyboardRemove()
        text += "\n\nДля регистрации нажмите /start."
    await bot.send_message(message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "cancel_action")
async def callback_cancel(call: telebot.types.CallbackQuery):
    """Обработчик нажатия инлайн кнопки 'Отмена'."""
    await bot.delete_state(call.from_user.id, call.message.chat.id)
    await bot.answer_callback_query(call.id, "Действие отменено.")
    try:
        await bot.edit_message_text(
            "❌ Действие отменено пользователем.",
            call.message.chat.id,
            call.message.message_id
        )
    except Exception as e:
        logger.debug("cancel: не удалось изменить сообщение (%s)", type(e).__name__)

@bot.message_handler(func=is_menu_or_command, state=ALL_STATES)
async def reject_menu_in_state(message: telebot.types.Message):
    """Кнопки меню и команды на шаге ввода не принимаются как данные:
    повторяем подсказку текущего шага."""
    state = await bot.get_state(message.from_user.id, message.chat.id)
    if state in (RequestScheduleState.date_str.name, EditScheduleState.date_str.name):
        text = BAD_DATE_TEXT
    elif state in (RequestScheduleState.desired_time.name, EditScheduleState.shift_time.name):
        text = BAD_SHIFT_TEXT
    else:
        text = (
            "✋ Сейчас бот ждёт ответ на предыдущий вопрос — кнопки меню и команды здесь не принимаются.\n"
            "Введите данные или нажмите /cancel, чтобы выйти в меню."
        )
    await bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=['help'])
async def cmd_help(message: telebot.types.Message):
    """Справка по доступным возможностям бота с учетом роли пользователя."""
    user = await db.get_user(message.from_user.id)
    if user and user['status'] == USER_PENDING:
        await bot.send_message(
            message.chat.id,
            f"📖 <b>Справка по боту</b>\n\n{PENDING_TEXT}\n"
            "Когда Администратор подтвердит доступ, бот пришлёт сообщение и покажет меню.\n\n"
            "• /start — проверить статус заявки\n"
            "• /help — эта справка\n"
            "• /cancel — прервать текущее действие",
            parse_mode="HTML"
        )
        return
    role = user['role'] if user else ROLE_WORKER
    role_title = ROLE_NAMES.get(role, "Пользователь")

    text = (
        f"📖 <b>Справка по боту (Ваша роль: {role_title})</b>\n\n"
        "<b>Основные команды:</b>\n"
        "• /start — Запуск/перезапуск бота и регистрация\n"
        "• /profile — Карточка вашего профиля\n"
        "• /help — Показать эту справку\n"
        "• /cancel — Прервать любое текущее действие (ввод данных)\n\n"
        "<b>Возможности сотрудника:</b>\n"
        f"• <b>{BTN_MY_SCHEDULE}</b> — ваши смены на {SCHEDULE_DAYS} дней вперёд\n"
        f"• <b>{BTN_REQUEST}</b> — подача заявки руководству\n"
        f"• <b>{BTN_PROFILE}</b> — просмотр ФИО, филиала, должности и роли\n"
    )

    if role in MANAGEMENT_ROLES:
        text += (
            "\n<b>Возможности руководства:</b>\n"
            f"• <b>{BTN_BRANCH_SCHEDULE}</b> — смены всех сотрудников филиала на {SCHEDULE_DAYS} дней\n"
            f"• <b>{BTN_REQUESTS}</b> — одобрение или отклонение заявок работников\n"
            f"• <b>{BTN_EDIT_SCHEDULE}</b> — прямое назначение/редактирование смены\n"
            f"• <b>{BTN_STAFF}</b> — карточки сотрудников: сделать неактивным, вернуть в работу\n"
        )

    if role == ROLE_SUPERADMIN:
        text += (
            "\n<b>Панель Главного Администратора:</b>\n"
            "• Для расписания, заявок, правки смен и сотрудников сначала выберите филиал\n"
            "• Заявки на доступ новых пользователей — кнопки «✅ Подтвердить / ❌ Отклонить»\n"
            "• В карточке сотрудника — перевод в другой филиал\n"
            f"• <b>{BTN_SET_ROLE}</b> — смена прав пользователей (Работник, Менеджер, Управляющий, Администратор)\n"
            f"• <b>{BTN_BROADCAST}</b> — объявление всем пользователям (с предпросмотром)\n"
            f"• <b>{BTN_PAUSE}</b> — переключение режима обслуживания системы\n"
        )

    await bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=['profile'])
@bot.message_handler(func=lambda m: m.text == BTN_PROFILE)
async def cmd_profile(message: telebot.types.Message):
    """Отображение карточки профиля пользователя."""
    user = await db.get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, NOT_REGISTERED_TEXT)
        return

    text = (
        "👤 <b>Ваш профиль в системе:</b>\n\n"
        f"🆔 <b>Telegram ID:</b> <code>{user['telegram_id']}</code>\n"
        f"🏷 <b>ФИО:</b> {esc(user['full_name'])}\n"
        f"🏢 <b>Филиал:</b> {esc(user['branch'])}\n"
        f"💼 <b>Должность:</b> {esc(user['position'])}\n"
        f"🔑 <b>Роль:</b> {ROLE_NAMES.get(user['role'], user['role'])}\n"
    )
    await bot.send_message(message.chat.id, text, parse_mode="HTML")

# =====================================================================
# ХЭНДЛЕРЫ: СТАРТ И РЕГИСТРАЦИЯ
# =====================================================================
@bot.message_handler(commands=['start'])
async def cmd_start(message: telebot.types.Message):
    user = await db.get_user(message.from_user.id)
    if user and user['status'] == USER_PENDING:
        await bot.send_message(
            message.chat.id,
            f"{PENDING_TEXT}\nКогда Администратор подтвердит доступ, бот пришлёт сообщение.",
            reply_markup=ReplyKeyboardRemove()
        )
    elif user:
        logger.info("start: пользователь %s вернулся", uid_tag(message.from_user.id))
        await bot.send_message(
            message.chat.id,
            f"С возвращением, {esc(user['full_name'])}!\n"
            f"Ваша роль: {ROLE_NAMES.get(user['role'], 'Работник')}\n"
            f"Филиал: {esc(user['branch'])}\n\n"
            f"Используйте кнопки меню ниже или команду /help для справки.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(user['role'])
        )
    else:
        logger.info("start: новый пользователь %s, регистрация", uid_tag(message.from_user.id))
        await bot.send_message(
            message.chat.id,
            "Добро пожаловать! Давайте пройдем регистрацию.\n\n"
            "Введите ваше ФИО (например: Иванов Иван):\n\n"
            "<i>Для отмены введите /cancel</i>",
            parse_mode="HTML"
        )
        await bot.set_state(message.from_user.id, RegistrationState.full_name, message.chat.id)


def clean_short_text(text) -> str | None:
    """ФИО или должность: непустая строка до MAX_NAME_LEN символов."""
    text = (text or "").strip()
    if not text or len(text) > MAX_NAME_LEN:
        return None
    return text


@bot.message_handler(state=RegistrationState.full_name)
async def process_full_name(message: telebot.types.Message):
    full_name = clean_short_text(message.text)
    if not full_name:
        await bot.send_message(
            message.chat.id,
            f"❗️ Введите ФИО текстом, не длиннее {MAX_NAME_LEN} символов.\n\n<i>Для отмены введите /cancel</i>",
            parse_mode="HTML"
        )
        return
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['full_name'] = full_name

    await bot.send_message(
        message.chat.id,
        "Выберите ваш филиал:",
        reply_markup=get_branches_keyboard()
    )
    await bot.set_state(message.from_user.id, RegistrationState.branch, message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('select_branch:'), state=RegistrationState.branch)
async def process_branch(call: telebot.types.CallbackQuery):
    branch = call.data.split(':', 1)[1]
    if branch not in BRANCHES:
        await bot.answer_callback_query(call.id, "Такого филиала нет.", show_alert=True)
        return
    async with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        data['branch'] = branch

    await bot.answer_callback_query(call.id)
    await bot.send_message(
        call.message.chat.id,
        "Введите вашу должность (например: Бариста, Кассир):\n\n<i>Для отмены введите /cancel</i>",
        parse_mode="HTML"
    )
    await bot.set_state(call.from_user.id, RegistrationState.position, call.message.chat.id)

@bot.message_handler(state=RegistrationState.position)
async def process_position(message: telebot.types.Message):
    position = clean_short_text(message.text)
    if not position:
        await bot.send_message(
            message.chat.id,
            f"❗️ Введите должность текстом, не длиннее {MAX_NAME_LEN} символов.\n\n<i>Для отмены введите /cancel</i>",
            parse_mode="HTML"
        )
        return
    user_id = message.from_user.id

    async with bot.retrieve_data(user_id, message.chat.id) as data:
        full_name = data['full_name']
        branch = data['branch']

    await bot.delete_state(user_id, message.chat.id)

    if user_id == FIRST_SUPERADMIN_ID:
        # Первый Администратор подтверждения не ждёт
        await db.add_user(user_id, full_name, branch, position, ROLE_SUPERADMIN, USER_ACTIVE)
        await bot.send_message(
            message.chat.id,
            "🎉 Регистрация успешно завершена! Доступные функции отображены в меню ниже.",
            reply_markup=get_main_keyboard(ROLE_SUPERADMIN)
        )
        return

    # Остальные ждут подтверждения Администратора
    await db.add_user(user_id, full_name, branch, position, ROLE_WORKER, USER_PENDING)
    logger.info("Регистрация: заявка на доступ от %s", uid_tag(user_id))
    await bot.send_message(
        message.chat.id,
        "📨 Заявка на доступ отправлена, ждите подтверждения.\n"
        "Когда Администратор её рассмотрит, бот пришлёт сообщение.",
        reply_markup=ReplyKeyboardRemove()
    )

    delivered = 0
    for admin in await db.get_superadmins():
        try:
            await bot.send_message(
                admin['telegram_id'],
                f"🔔 <b>Новая заявка на доступ</b>\n\n"
                f"<b>ID:</b> <code>{user_id}</code>\n"
                f"<b>ФИО:</b> {esc(full_name)}\n"
                f"<b>Филиал:</b> {esc(branch)}\n"
                f"<b>Должность:</b> {esc(position)}\n\n"
                "<i>После подтверждения можно назначить роль.</i>",
                parse_mode="HTML",
                reply_markup=get_registration_keyboard(user_id)
            )
            delivered += 1
        except Exception as e:
            logger.error("Не удалось уведомить админа %s о регистрации (%s)", uid_tag(admin['telegram_id']), type(e).__name__)
    if not delivered:
        logger.warning("Заявка на доступ от %s не доставлена ни одному Администратору", uid_tag(user_id))
        await bot.send_message(
            message.chat.id,
            "⚠️ Доставить заявку Администратору сейчас не удалось. Пожалуйста, сообщите руководителю лично."
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith(('reg_ok:', 'reg_no:')))
async def process_registration_decision(call: telebot.types.CallbackQuery):
    """Администратор подтверждает или отклоняет заявку на доступ."""
    if not await get_user_with_role(call.from_user.id, (ROLE_SUPERADMIN,)):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    parsed = parse_callback(call.data, rf"(reg_ok|reg_no):({_ID_RE})")
    if not parsed:
        await bot.answer_callback_query(call.id, BAD_CALLBACK_TEXT)
        return
    approve, target_id = parsed[0] == "reg_ok", int(parsed[1])

    target = await db.decide_registration(target_id, approve)
    if not target:
        await bot.answer_callback_query(call.id, "Заявка уже обработана", show_alert=True)
        await _remove_buttons(call)
        return

    logger.info("Регистрация %s: %s", uid_tag(target_id), "подтверждена" if approve else "отклонена")
    await bot.answer_callback_query(call.id, "Решение сохранено!")
    await _remove_buttons(call)

    if approve:
        await bot.send_message(
            call.message.chat.id,
            f"✅ Доступ для <b>{esc(target['full_name'])}</b> подтверждён. "
            f"Роль: <b>{ROLE_NAMES.get(target['role'], target['role'])}</b> — при необходимости выберите другую:",
            parse_mode="HTML",
            reply_markup=get_roles_keyboard(target_id)
        )
        user_text = "✅ Доступ подтверждён! Доступные функции отображены в меню ниже."
        user_markup = get_main_keyboard(target['role'])
    else:
        await bot.send_message(
            call.message.chat.id,
            f"❌ Заявка на доступ <b>{esc(target['full_name'])}</b> отклонена.",
            parse_mode="HTML"
        )
        user_text = "❌ В доступе отказано. Если это ошибка, обратитесь к руководителю."
        user_markup = ReplyKeyboardRemove()
    try:
        await bot.send_message(target_id, user_text, reply_markup=user_markup)
    except Exception as e:
        logger.error("Не удалось сообщить решение по доступу пользователю %s (%s)", uid_tag(target_id), type(e).__name__)

# =====================================================================
# ХЭНДЛЕРЫ РАБОТНИКА
# =====================================================================
@bot.message_handler(func=lambda m: m.text == BTN_MY_SCHEDULE)
async def view_my_schedule(message: telebot.types.Message):
    date_from, date_to = schedule_period()
    schedules = await db.get_user_schedule(message.from_user.id, date_from, date_to)
    period = f"{format_date(date_from)} — {format_date(date_to)}"
    if not schedules:
        await bot.send_message(message.chat.id, f"У вас нет смен на ближайшие {SCHEDULE_DAYS} дней ({period}).")
        return

    text = f"🗓 <b>Ваше расписание</b> ({period}):\n\n"
    for s in schedules:
        text += f"• <b>{esc(format_date(s['date_str']))}:</b> <code>{esc(s['shift_time'])}</code>\n"

    await send_long(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == BTN_REQUEST)
async def start_request_schedule(message: telebot.types.Message):
    if not await db.get_user(message.from_user.id):
        await bot.send_message(message.chat.id, NOT_REGISTERED_TEXT)
        return

    await bot.send_message(
        message.chat.id,
        "📅 Введите дату, на которую хотите изменить расписание (например: 25.10):\n\n"
        "<i>Для отмены введите /cancel</i>",
        parse_mode="HTML"
    )
    await bot.set_state(message.from_user.id, RequestScheduleState.date_str, message.chat.id)

@bot.message_handler(state=RequestScheduleState.date_str)
async def process_req_date(message: telebot.types.Message):
    date_str = parse_date(message.text or "")
    if not date_str:
        await bot.send_message(message.chat.id, BAD_DATE_TEXT, parse_mode="HTML")
        return
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['date_str'] = date_str

    await bot.send_message(
        message.chat.id,
        "⏰ Укажите желаемое время смены (например: 08:00 - 20:00 или Выходной):\n\n"
        "<i>Для отмены введите /cancel</i>",
        parse_mode="HTML"
    )
    await bot.set_state(message.from_user.id, RequestScheduleState.desired_time, message.chat.id)

@bot.message_handler(state=RequestScheduleState.desired_time)
async def process_req_time(message: telebot.types.Message):
    desired_time = parse_shift(message.text or "")
    if not desired_time:
        await bot.send_message(message.chat.id, BAD_SHIFT_TEXT, parse_mode="HTML")
        return
    user_id = message.from_user.id

    async with bot.retrieve_data(user_id, message.chat.id) as data:
        date_str = data['date_str']

    await bot.delete_state(user_id, message.chat.id)
    user = await db.get_user(user_id)
    if not user:
        await bot.send_message(message.chat.id, NOT_REGISTERED_TEXT)
        return

    req_id = await db.create_request(user_id, date_str, desired_time)

    # Руководство филиала, а если его нет — Администраторы
    recipients = await db.get_request_recipients(user['branch'], exclude_id=user_id)
    delivered = 0
    for mgr in recipients:
        try:
            await bot.send_message(
                mgr['telegram_id'],
                f"📩 <b>Новый запрос на изменение расписания!</b>\n\n"
                f"<b>Сотрудник:</b> {esc(user['full_name'])} ({esc(user['position'])})\n"
                f"<b>Филиал:</b> {esc(user['branch'])}\n"
                f"<b>Дата:</b> {esc(format_date(date_str))}\n"
                f"<b>Желаемое время:</b> {esc(desired_time)}",
                parse_mode="HTML",
                reply_markup=get_request_keyboard(req_id)
            )
            delivered += 1
        except Exception as e:
            logger.error("Не удалось отправить заявку руководителю %s (%s)", uid_tag(mgr['telegram_id']), type(e).__name__)

    if delivered:
        await bot.send_message(message.chat.id, "✅ Ваш запрос успешно отправлен руководству!")
    else:
        logger.warning("Заявка №%s сохранена, но не доставлена ни одному руководителю", req_id)
        await bot.send_message(
            message.chat.id,
            f"⚠️ Запрос №{req_id} сохранён, но доставить его руководству сейчас не удалось "
            "(в филиале нет руководителя или он недоступен). Пожалуйста, сообщите руководителю лично."
        )

# =====================================================================
# ХЭНДЛЕРЫ МЕНЕДЖЕРА И УПРАВЛЯЮЩЕГО
# Администратор видит любой филиал: сначала выбирает его кнопкой.
# =====================================================================
async def show_branch_schedule(chat_id, branch: str):
    date_from, date_to = schedule_period()
    employees = await db.get_branch_schedule(branch, date_from, date_to)
    if not employees:
        await bot.send_message(chat_id, f"В филиале «{branch}» нет сотрудников.")
        return

    text = (
        f"🏢 <b>Расписание филиала «{esc(branch)}»</b>\n"
        f"<i>{format_date(date_from)} — {format_date(date_to)}</i>\n\n"
    )
    for emp in employees:
        text += f"👤 <b>{esc(emp['full_name'])}</b> (<i>{esc(emp['position'])}</i>):\n"
        if emp['shifts']:
            for date_str, shift_time in emp['shifts']:
                text += f"   • {esc(format_date(date_str))}: <code>{esc(shift_time)}</code>\n"
        else:
            text += "   • <i>Расписание отсутствует</i>\n"
        text += "\n"

    await send_long(chat_id, text, parse_mode="HTML")


async def show_pending_requests(chat_id, branch: str):
    requests = await db.get_pending_requests(branch)
    if not requests:
        await bot.send_message(chat_id, f"Активных запросов в филиале «{branch}» нет.")
        return

    for r in requests:
        own = r['user_id'] == chat_id  # личный чат: chat_id — это ID руководителя
        text = (
            f"📋 <b>Запрос №{r['id']}</b>\n"
            f"<b>Сотрудник:</b> {esc(r['full_name'])} ({esc(r['position'])})\n"
            f"<b>Дата:</b> {esc(format_date(r['date_str']))}\n"
            f"<b>Желаемое время:</b> {esc(r['desired_time'])}"
        )
        if own:
            text += "\n\n<i>Это ваша заявка — её решает другой руководитель.</i>"
        await bot.send_message(
            chat_id, text, parse_mode="HTML",
            reply_markup=None if own else get_request_keyboard(r['id'])
        )


async def show_edit_employees(chat_id, branch: str):
    employees = await db.get_branch_users(branch)
    if not employees:
        await bot.send_message(chat_id, "Нет сотрудников для редактирования.")
        return

    markup = InlineKeyboardMarkup()
    for emp in employees:
        markup.add(InlineKeyboardButton(text=f"👤 {emp['full_name']} ({emp['position']})", callback_data=f"edit_emp:{emp['telegram_id']}"))
    markup.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action"))

    await bot.send_message(chat_id, f"Выберите сотрудника филиала «{branch}» для редактирования расписания:", reply_markup=markup)


def staff_list_keyboard(employees, branch: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки сотрудников → карточка; branch — показать кнопку «неактивные» этого филиала."""
    markup = InlineKeyboardMarkup()
    for emp in employees:
        mark = "🚫 " if emp['status'] == USER_INACTIVE else ""
        markup.add(InlineKeyboardButton(
            text=f"{mark}👤 {emp['full_name']} ({emp['position']})", callback_data=f"emp:{emp['telegram_id']}"
        ))
    if branch is not None:
        markup.add(InlineKeyboardButton(text="🗂 Показать неактивных", callback_data=f"staff_inactive:{branch}"))
    markup.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action"))
    return markup


async def show_staff(chat_id, branch: str):
    employees = await db.get_branch_users(branch)
    text = (
        f"👥 <b>Сотрудники филиала «{esc(branch)}»</b>\n"
        + ("Выберите сотрудника, чтобы открыть карточку:" if employees else "<i>Активных сотрудников нет.</i>")
    )
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=staff_list_keyboard(employees, branch))


BRANCH_ACTIONS = {
    "sched": (show_branch_schedule, "расписание"),
    "reqs": (show_pending_requests, "запросы"),
    "edit": (show_edit_employees, "правку смен"),
    "staff": (show_staff, "список сотрудников"),
}


async def run_branch_action(message: telebot.types.Message, action: str):
    """Руководитель сразу работает со своим филиалом, Администратор — сначала выбирает филиал."""
    user = await get_user_with_role(message.from_user.id, MANAGEMENT_ROLES)
    if not user:
        return
    show, title = BRANCH_ACTIONS[action]
    if user['role'] == ROLE_SUPERADMIN:
        await bot.send_message(
            message.chat.id,
            f"🏢 Выберите филиал, чтобы открыть {title}:",
            reply_markup=get_pick_branch_keyboard(action)
        )
        return
    await show(message.chat.id, user['branch'])


@bot.message_handler(func=lambda m: m.text == BTN_BRANCH_SCHEDULE)
async def view_branch_schedule(message: telebot.types.Message):
    await run_branch_action(message, "sched")

@bot.message_handler(func=lambda m: m.text == BTN_REQUESTS)
async def view_pending_requests(message: telebot.types.Message):
    await run_branch_action(message, "reqs")

@bot.message_handler(func=lambda m: m.text == BTN_EDIT_SCHEDULE)
async def start_edit_schedule(message: telebot.types.Message):
    await run_branch_action(message, "edit")

@bot.message_handler(func=lambda m: m.text == BTN_STAFF)
async def view_staff(message: telebot.types.Message):
    await run_branch_action(message, "staff")

@bot.callback_query_handler(func=lambda c: c.data.startswith('pick_branch:'))
async def process_pick_branch(call: telebot.types.CallbackQuery):
    parsed = parse_callback(call.data, r"pick_branch:(sched|reqs|edit|staff):(.+)")
    if not parsed or parsed[1] not in BRANCHES:
        await bot.answer_callback_query(call.id, BAD_CALLBACK_TEXT)
        return
    action, branch = parsed
    user = await get_user_with_role(call.from_user.id, MANAGEMENT_ROLES)
    if not user or not can_manage_branch(user, branch):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    await bot.answer_callback_query(call.id)
    show, _ = BRANCH_ACTIONS[action]
    await show(call.message.chat.id, branch)

@bot.callback_query_handler(func=lambda c: c.data.startswith(('app_req:', 'rej_req:')))
async def process_request_decision(call: telebot.types.CallbackQuery):
    parsed = parse_callback(call.data, rf"(app_req|rej_req):({_ID_RE})")
    if not parsed:
        await bot.answer_callback_query(call.id, BAD_CALLBACK_TEXT)
        return
    action, req_id = parsed[0], int(parsed[1])

    manager = await get_user_with_role(call.from_user.id, MANAGEMENT_ROLES)
    info = await db.get_request_info(req_id)
    if not manager or info is None or not can_manage_branch(manager, info['branch']):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    if info['user_id'] == manager['telegram_id']:
        await bot.answer_callback_query(call.id, OWN_REQUEST_TEXT, show_alert=True)
        return

    approve = action == 'app_req'
    req = await db.decide_request(req_id, approve)
    if not req:
        await bot.answer_callback_query(call.id, "Заявка уже обработана", show_alert=True)
        await _remove_buttons(call)
        return

    await bot.answer_callback_query(call.id, "Решение сохранено!")

    # Сначала уведомляем работника: это важнее, чем убрать кнопки
    if approve:
        msg_text = f"🔔 Ваше расписание на [{format_date(req['date_str'])}] было изменено: новое время [{req['desired_time']}]."
    else:
        msg_text = f"❌ Ваш запрос на изменение расписания на [{format_date(req['date_str'])}] был отклонен."
    try:
        await bot.send_message(req['user_id'], msg_text)
    except Exception as e:
        logger.error("Не удалось сообщить решение по заявке №%s пользователю %s (%s)",
                     req_id, uid_tag(req['user_id']), type(e).__name__)

    await _remove_buttons(call)


async def _remove_buttons(call):
    try:
        await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception as e:
        logger.debug("Не удалось убрать кнопки (%s)", type(e).__name__)

@bot.callback_query_handler(func=lambda c: c.data.startswith('edit_emp:'))
async def select_emp_to_edit(call: telebot.types.CallbackQuery):
    parsed = parse_callback(call.data, rf"edit_emp:({_ID_RE})")
    if not parsed:
        await bot.answer_callback_query(call.id, BAD_CALLBACK_TEXT)
        return
    target_id = int(parsed[0])
    manager = await get_user_with_role(call.from_user.id, MANAGEMENT_ROLES)
    target = await db.get_user(target_id)
    if not manager or not target or not can_manage_branch(manager, target['branch']):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    if target['status'] != USER_ACTIVE:
        await bot.answer_callback_query(call.id, NOT_ACTIVE_TARGET_TEXT, show_alert=True)
        return
    # Сначала состояние, потом данные: без состояния хранилище не сохранит data
    await bot.set_state(call.from_user.id, EditScheduleState.date_str, call.message.chat.id)
    async with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        data['target_user_id'] = target_id

    await bot.answer_callback_query(call.id)
    await bot.send_message(
        call.message.chat.id,
        "📅 Введите дату смены (например: 26.10):\n\n<i>Для отмены введите /cancel</i>",
        parse_mode="HTML"
    )

@bot.message_handler(state=EditScheduleState.date_str)
async def process_edit_date(message: telebot.types.Message):
    date_str = parse_date(message.text or "")
    if not date_str:
        await bot.send_message(message.chat.id, BAD_DATE_TEXT, parse_mode="HTML")
        return
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['date_str'] = date_str

    await bot.send_message(
        message.chat.id,
        "⏰ Введите новое время смены (например: 08:00 - 20:00 или Выходной):\n\n<i>Для отмены введите /cancel</i>",
        parse_mode="HTML"
    )
    await bot.set_state(message.from_user.id, EditScheduleState.shift_time, message.chat.id)

@bot.message_handler(state=EditScheduleState.shift_time)
async def process_edit_time(message: telebot.types.Message):
    shift_time = parse_shift(message.text or "")
    if not shift_time:
        await bot.send_message(message.chat.id, BAD_SHIFT_TEXT, parse_mode="HTML")
        return
    user_id = message.from_user.id

    async with bot.retrieve_data(user_id, message.chat.id) as data:
        target_id = data['target_user_id']
        date_str = data['date_str']

    await bot.delete_state(user_id, message.chat.id)

    # Права могли измениться, пока шёл ввод (роль сняли, сотрудника перевели)
    manager = await get_user_with_role(user_id, MANAGEMENT_ROLES)
    target = await db.get_user(target_id)
    if not manager or not target or not can_manage_branch(manager, target['branch']):
        await bot.send_message(message.chat.id, f"{NO_RIGHTS_TEXT} Смена не сохранена.")
        return
    if target['status'] != USER_ACTIVE:
        await bot.send_message(message.chat.id, f"{NOT_ACTIVE_TARGET_TEXT} Смена не сохранена.")
        return

    closed = await db.set_shift(target_id, date_str, shift_time)
    text = "✅ Расписание сотрудника успешно обновлено!"
    if closed:
        text += f"\nОжидающие заявки сотрудника на эту дату закрыты: {closed}."
    await bot.send_message(message.chat.id, text)

    # Личное уведомление сотруднику
    try:
        await bot.send_message(
            target_id,
            f"🔔 Ваше расписание на [{format_date(date_str)}] было изменено: новое время [{shift_time}]."
        )
    except Exception as e:
        logger.error("Не удалось уведомить сотрудника %s о смене (%s)", uid_tag(target_id), type(e).__name__)

# =====================================================================
# КАРТОЧКА СОТРУДНИКА: статус, правка смены, перевод в филиал
# Руководитель — сотрудники своего филиала, Администратор — любые.
# =====================================================================
def card_text(target) -> str:
    return (
        "👤 <b>Карточка сотрудника</b>\n\n"
        f"🏷 <b>ФИО:</b> {esc(target['full_name'])}\n"
        f"💼 <b>Должность:</b> {esc(target['position'])}\n"
        f"🏢 <b>Филиал:</b> {esc(target['branch'])}\n"
        f"🔑 <b>Роль:</b> {ROLE_NAMES.get(target['role'], esc(target['role']))}\n"
        f"📌 <b>Статус:</b> {STATUS_NAMES.get(target['status'], esc(target['status']))}"
    )


def can_change_status(viewer, target) -> bool:
    """Себя — нельзя; Администратора может отключить только Администратор."""
    if viewer['telegram_id'] == target['telegram_id']:
        return False
    return viewer['role'] == ROLE_SUPERADMIN or target['role'] != ROLE_SUPERADMIN


def card_keyboard(viewer, target) -> InlineKeyboardMarkup:
    tid = target['telegram_id']
    markup = InlineKeyboardMarkup()
    if target['status'] == USER_ACTIVE:
        markup.add(InlineKeyboardButton("🗓 Изменить смену", callback_data=f"edit_emp:{tid}"))
    if can_change_status(viewer, target):
        if target['status'] == USER_ACTIVE:
            markup.add(InlineKeyboardButton("🚫 Сделать неактивным", callback_data=f"emp_off:{tid}"))
        elif target['status'] == USER_INACTIVE:
            markup.add(InlineKeyboardButton("✅ Вернуть в работу", callback_data=f"emp_on:{tid}"))
    if viewer['role'] == ROLE_SUPERADMIN:
        markup.add(InlineKeyboardButton("🏢 Перевести в филиал", callback_data=f"emp_move:{tid}"))
    markup.add(InlineKeyboardButton("❌ Закрыть", callback_data="cancel_action"))
    return markup


async def _card_access(call, pattern: str, roles=MANAGEMENT_ROLES):
    """Разбор кнопки карточки и проверка прав. Возвращает (viewer, target, groups) или None
    (на нажатие уже ответили)."""
    parsed = parse_callback(call.data, pattern)
    if not parsed:
        await bot.answer_callback_query(call.id, BAD_CALLBACK_TEXT)
        return None
    viewer = await get_user_with_role(call.from_user.id, roles)
    target = await db.get_user(int(parsed[0]))
    if not viewer or not target or not can_manage_branch(viewer, target['branch']):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return None
    return viewer, target, parsed


async def _show_card(call, viewer, target_id: int, edit: bool):
    """Показать карточку: обновить сообщение с кнопкой (edit) или прислать новое."""
    target = await db.get_user(target_id)
    text, markup = card_text(target), card_keyboard(viewer, target)
    if edit:
        try:
            await bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                        parse_mode="HTML", reply_markup=markup)
            return
        except Exception as e:
            logger.debug("Карточка: не удалось обновить сообщение (%s)", type(e).__name__)
    await bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)


async def _notify(user_id: int, text: str, what: str, **kwargs):
    try:
        await bot.send_message(user_id, text, **kwargs)
    except Exception as e:
        logger.error("%s: не удалось уведомить %s (%s)", what, uid_tag(user_id), type(e).__name__)


@bot.callback_query_handler(func=lambda c: c.data.startswith('staff_inactive:'))
async def process_staff_inactive(call: telebot.types.CallbackQuery):
    parsed = parse_callback(call.data, r"staff_inactive:(.+)")
    if not parsed or parsed[0] not in BRANCHES:
        await bot.answer_callback_query(call.id, BAD_CALLBACK_TEXT)
        return
    branch = parsed[0]
    viewer = await get_user_with_role(call.from_user.id, MANAGEMENT_ROLES)
    if not viewer or not can_manage_branch(viewer, branch):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    await bot.answer_callback_query(call.id)
    employees = await db.get_branch_users(branch, USER_INACTIVE)
    text = (
        f"🗂 <b>Неактивные сотрудники филиала «{esc(branch)}»</b>\n"
        + ("Выберите сотрудника, чтобы открыть карточку:" if employees else "<i>Неактивных сотрудников нет.</i>")
    )
    await bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=staff_list_keyboard(employees))


@bot.callback_query_handler(func=lambda c: c.data == 'staff_other')
async def process_staff_other(call: telebot.types.CallbackQuery):
    """Администратор: пользователи с филиалом не из BRANCHES (старые записи, первый Администратор)."""
    if not await get_user_with_role(call.from_user.id, (ROLE_SUPERADMIN,)):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    await bot.answer_callback_query(call.id)
    employees = [u for u in await db.get_all_users() if u['branch'] not in BRANCHES and u['status'] != USER_PENDING]
    text = (
        "❓ <b>Филиал не из списка</b>\n"
        + ("Откройте карточку и переведите сотрудника в нужный филиал:" if employees else "<i>Таких сотрудников нет.</i>")
    )
    await bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=staff_list_keyboard(employees))


@bot.callback_query_handler(func=lambda c: c.data.startswith('emp:'))
async def open_employee_card(call: telebot.types.CallbackQuery):
    access = await _card_access(call, rf"emp:({_ID_RE})")
    if not access:
        return
    viewer, target, _ = access
    await bot.answer_callback_query(call.id)
    await _show_card(call, viewer, target['telegram_id'], edit=False)


@bot.callback_query_handler(func=lambda c: c.data.startswith('emp_off:'))
async def deactivate_employee(call: telebot.types.CallbackQuery):
    access = await _card_access(call, rf"emp_off:({_ID_RE})")
    if not access:
        return
    viewer, target, _ = access
    tid = target['telegram_id']
    if tid == viewer['telegram_id']:
        await bot.answer_callback_query(call.id, "❌ Нельзя сделать неактивным себя.", show_alert=True)
        return
    if not can_change_status(viewer, target):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    if target['role'] == ROLE_SUPERADMIN and await db.count_superadmins(active_only=True) <= 1:
        await bot.answer_callback_query(call.id, "❌ Это последний Администратор — сначала назначьте другого.", show_alert=True)
        return

    closed = await db.deactivate_user(tid)
    if closed is None:
        # Уже неактивен, ждёт подтверждения или второй Администратор успел раньше
        await bot.answer_callback_query(call.id, "Статус уже изменён", show_alert=True)
        await _show_card(call, viewer, tid, edit=True)
        return

    logger.info("Карточка: %s сделал неактивным %s, закрыто заявок %s",
                uid_tag(viewer['telegram_id']), uid_tag(tid), closed)
    await bot.delete_state(tid, tid)  # начатый ввод сотрудника больше не нужен
    await bot.answer_callback_query(call.id, "Сотрудник отключён")
    await _show_card(call, viewer, tid, edit=True)
    if closed:
        await bot.send_message(call.message.chat.id, f"Ожидающие заявки сотрудника закрыты: {closed}.")
    await _notify(tid, INACTIVE_TEXT, "Деактивация", reply_markup=ReplyKeyboardRemove())


@bot.callback_query_handler(func=lambda c: c.data.startswith('emp_on:'))
async def activate_employee(call: telebot.types.CallbackQuery):
    access = await _card_access(call, rf"emp_on:({_ID_RE})")
    if not access:
        return
    viewer, target, _ = access
    tid = target['telegram_id']
    if not can_change_status(viewer, target):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    if not await db.activate_user(tid):
        await bot.answer_callback_query(call.id, "Статус уже изменён", show_alert=True)
        await _show_card(call, viewer, tid, edit=True)
        return

    logger.info("Карточка: %s вернул в работу %s", uid_tag(viewer['telegram_id']), uid_tag(tid))
    await bot.answer_callback_query(call.id, "Сотрудник снова в работе")
    await _show_card(call, viewer, tid, edit=True)
    await _notify(tid, "✅ Доступ к боту снова открыт. Доступные функции — в меню ниже.", "Возврат в работу",
                  reply_markup=get_main_keyboard(target['role']))


@bot.callback_query_handler(func=lambda c: c.data.startswith('emp_move:'))
async def choose_employee_branch(call: telebot.types.CallbackQuery):
    access = await _card_access(call, rf"emp_move:({_ID_RE})", roles=(ROLE_SUPERADMIN,))
    if not access:
        return
    _, target, _ = access
    markup = InlineKeyboardMarkup()
    for b in BRANCHES:
        if b != target['branch']:
            markup.add(InlineKeyboardButton(f"🏢 {b}", callback_data=f"emp_to:{target['telegram_id']}:{b}"))
    markup.add(InlineKeyboardButton("❌ Отмена", callback_data="cancel_action"))
    await bot.answer_callback_query(call.id)
    await bot.send_message(
        call.message.chat.id,
        f"🏢 В какой филиал перевести <b>{esc(target['full_name'])}</b>?",
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith('emp_to:'))
async def move_employee(call: telebot.types.CallbackQuery):
    access = await _card_access(call, rf"emp_to:({_ID_RE}):(.+)", roles=(ROLE_SUPERADMIN,))
    if not access:
        return
    viewer, target, parsed = access
    branch = parsed[1]
    if branch not in BRANCHES:
        await bot.answer_callback_query(call.id, "Такого филиала нет.", show_alert=True)
        return
    tid = target['telegram_id']
    await db.set_user_branch(tid, branch)
    logger.info("Карточка: %s перевёл %s в другой филиал", uid_tag(viewer['telegram_id']), uid_tag(tid))
    await bot.answer_callback_query(call.id, "Сотрудник переведён")
    await _remove_buttons(call)
    await _show_card(call, viewer, tid, edit=False)
    if tid != viewer['telegram_id'] and target['status'] == USER_ACTIVE:
        await _notify(tid, f"🏢 Вас перевели в филиал «{esc(branch)}».", "Перевод", parse_mode="HTML")


# =====================================================================
# ХЭНДЛЕРЫ СУПЕРАДМИНИСТРАТОРА
# =====================================================================
@bot.message_handler(func=lambda m: m.text == BTN_SET_ROLE)
async def start_assign_role(message: telebot.types.Message):
    if not await get_user_with_role(message.from_user.id, (ROLE_SUPERADMIN,)):
        return

    await bot.send_message(
        message.chat.id,
        "👤 Введите Telegram ID пользователя, которому хотите изменить роль:\n\n"
        "<i>Для отмены введите /cancel</i>",
        parse_mode="HTML"
    )
    await bot.set_state(message.from_user.id, AdminSetRoleState.target_user_id, message.chat.id)

@bot.message_handler(state=AdminSetRoleState.target_user_id)
async def process_assign_role_id(message: telebot.types.Message):
    target_id = parse_id(message.text)
    if target_id is None:
        await bot.send_message(
            message.chat.id,
            "Пожалуйста, введите корректный числовой Telegram ID (только цифры 0–9).\n\n"
            "<i>Для отмены введите /cancel</i>",
            parse_mode="HTML"
        )
        return

    target_user = await db.get_user(target_id)
    await bot.delete_state(message.from_user.id, message.chat.id)

    if not target_user:
        await bot.send_message(message.chat.id, "⚠️ Пользователь с таким ID не найден в базе данных.")
        return

    await bot.send_message(
        message.chat.id,
        f"Выберите новую роль для <b>{esc(target_user['full_name'])}</b>:",
        parse_mode="HTML",
        reply_markup=get_roles_keyboard(target_id)
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith('set_role:'))
async def process_role_callback(call: telebot.types.CallbackQuery):
    if not await get_user_with_role(call.from_user.id, (ROLE_SUPERADMIN,)):
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return

    parsed = parse_callback(call.data, rf"set_role:({_ID_RE}):(\w+)")
    if not parsed:
        await bot.answer_callback_query(call.id, BAD_CALLBACK_TEXT)
        return
    target_id, new_role = int(parsed[0]), parsed[1]
    target_user = await db.get_user(target_id)
    if not target_user or new_role not in ROLE_NAMES:
        await bot.answer_callback_query(call.id, "⚠️ Пользователь или роль не найдены.", show_alert=True)
        return

    superadmins = await db.count_superadmins()
    # Жесткое ограничение на количество Администраторов (максимум 2)
    if new_role == ROLE_SUPERADMIN and target_user['role'] != ROLE_SUPERADMIN and superadmins >= 2:
        await bot.answer_callback_query(call.id, "❌ Достигнут лимит: в системе может быть максимум 2 Администратора!", show_alert=True)
        return
    # Нельзя снять последнего (активного) Администратора — иначе управлять ботом будет некому
    if (target_user['role'] == ROLE_SUPERADMIN and new_role != ROLE_SUPERADMIN
            and await db.count_superadmins(active_only=True) <= 1):
        await bot.answer_callback_query(call.id, "❌ Это последний Администратор — сначала назначьте другого.", show_alert=True)
        return

    await db.update_user_role(target_id, new_role)
    await bot.answer_callback_query(call.id, "Роль успешно изменена!")
    await bot.send_message(
        call.message.chat.id,
        f"✅ Пользователю <code>{target_id}</code> присвоена роль: <b>{ROLE_NAMES.get(new_role, new_role)}</b>",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            target_id,
            f"🎉 Ваша роль в системе была обновлена!\nНовая роль: <b>{ROLE_NAMES.get(new_role, new_role)}</b>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(new_role)
        )
    except Exception as e:
        logger.error("Не удалось уведомить пользователя %s о новой роли (%s)", uid_tag(target_id), type(e).__name__)

@bot.message_handler(func=lambda m: m.text == BTN_BROADCAST)
async def start_broadcast(message: telebot.types.Message):
    if not await get_user_with_role(message.from_user.id, (ROLE_SUPERADMIN,)):
        return

    await bot.send_message(
        message.chat.id,
        "📢 Введите текст сообщения для глобальной рассылки.\n"
        "Перед отправкой бот покажет предпросмотр.\n\n"
        "<i>Для отмены введите /cancel</i>",
        parse_mode="HTML"
    )
    await bot.set_state(message.from_user.id, AdminBroadcastState.message_text, message.chat.id)


def broadcast_text(text: str) -> str:
    return f"📢 <b>Объявление от администрации:</b>\n\n{esc(text)}"


@bot.message_handler(state=AdminBroadcastState.message_text)
async def process_broadcast_text(message: telebot.types.Message):
    # Кнопки меню и команды сюда не доходят — их отсекает reject_menu_in_state
    text = (message.text or "").strip()
    if not text:
        await bot.send_message(message.chat.id, "Введите текст объявления или /cancel.")
        return
    # Запас под заголовок объявления, чтобы сообщение влезло в лимит Telegram
    if text_len(broadcast_text(text)) > MAX_MESSAGE_LEN:
        await bot.send_message(message.chat.id, "❗️ Текст слишком длинный для одного сообщения. Сократите его или /cancel.")
        return

    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['broadcast_text'] = text
    await bot.set_state(message.from_user.id, AdminBroadcastState.confirm, message.chat.id)

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Отправить", callback_data="broadcast_send"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_action"),
    )
    await bot.send_message(message.chat.id, "👀 <b>Предпросмотр рассылки:</b>", parse_mode="HTML")
    await bot.send_message(message.chat.id, broadcast_text(text), parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "broadcast_send", state=AdminBroadcastState.confirm)
async def process_broadcast_send(call: telebot.types.CallbackQuery):
    if not await get_user_with_role(call.from_user.id, (ROLE_SUPERADMIN,)):
        await bot.delete_state(call.from_user.id, call.message.chat.id)
        await bot.answer_callback_query(call.id, NO_RIGHTS_TEXT, show_alert=True)
        return
    async with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        text = data.get('broadcast_text')
    await bot.delete_state(call.from_user.id, call.message.chat.id)
    if not text:
        await bot.answer_callback_query(call.id, BAD_CALLBACK_TEXT)
        return

    await bot.answer_callback_query(call.id, "Рассылка началась…")
    await _remove_buttons(call)

    users = await db.get_active_users()
    sent, failed = await mass_send(users, broadcast_text(text), "Рассылка")
    logger.info("Рассылка: отправлено %s, не доставлено %s", sent, failed)
    await bot.send_message(
        call.message.chat.id,
        f"✅ Рассылка завершена: отправлено {sent} / не доставлено {failed}."
    )

@bot.message_handler(func=lambda m: m.text == BTN_PAUSE)
async def toggle_bot_pause(message: telebot.types.Message):
    if not await get_user_with_role(message.from_user.id, (ROLE_SUPERADMIN,)):
        return

    new_state = not await db.is_bot_paused()
    await db.set_bot_paused(new_state)

    if new_state:
        status_msg = f"<b>{PAUSED_TEXT}</b>"
    else:
        status_msg = "✅ <b>Работа бота возобновлена!</b>"

    users = await db.get_active_users()
    sent, failed = await mass_send(users, status_msg, "Оповещение о паузе")
    logger.info("Пауза бота: %s; оповещено %s, не доставлено %s", new_state, sent, failed)

    state_desc = "приостановлена" if new_state else "возобновлена"
    await bot.send_message(
        message.chat.id,
        f"Работа бота была {state_desc}. Оповещение: отправлено {sent} / не доставлено {failed}."
    )

# =====================================================================
# ПОСЛЕДНИЕ ОБРАБОТЧИКИ: ВСЁ, ЧТО НЕ ПОДОШЛО ВЫШЕ
# =====================================================================
@bot.message_handler(func=lambda m: True)
async def unknown_text(message: telebot.types.Message):
    await bot.send_message(message.chat.id, "🤔 Не понял. Нажмите /help")

@bot.callback_query_handler(func=lambda c: True)
async def unknown_callback(call: telebot.types.CallbackQuery):
    """Устаревшая или поддельная кнопка: отвечаем, чтобы она не «грузилась» вечно."""
    await bot.answer_callback_query(call.id, "Кнопка устарела. Нажмите /help")

# =====================================================================
# ФИЛЬТРЫ И ЗАПУСК
# =====================================================================
async def main():
    setup_logging()

    # Инициализация структуры БД
    await db.init_db(FIRST_SUPERADMIN_ID)

    # Состояния FSM — в файл, чтобы перезапуск не обрывал начатые сценарии
    bot.current_states = FileStateStorage(STATE_FILE)

    # Добавление фильтров состояний
    bot.add_custom_filter(asyncio_filters.StateFilter(bot))
    bot.add_custom_filter(asyncio_filters.IsDigitFilter())

    logger.info("Бот успешно запущен и готов к работе!")
    await bot.polling(non_stop=True)

if __name__ == "__main__":
    asyncio.run(main())
