import asyncio
import html
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # до import db: DB_FILE читается из окружения

import telebot
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage
from telebot.asyncio_handler_backends import State, StatesGroup
from telebot import asyncio_filters
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

import db
from shifts import parse_date, parse_shift, format_date
from db import ROLE_WORKER, ROLE_MANAGER, ROLE_BRANCH_ADMIN, ROLE_SUPERADMIN, MANAGEMENT_ROLES

# =====================================================================
# КОНФИГУРАЦИЯ БОТА
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_first_admin = os.getenv("FIRST_SUPERADMIN_ID", "").strip()
if not BOT_TOKEN or not _first_admin.isdigit():
    sys.exit("Заполните BOT_TOKEN и FIRST_SUPERADMIN_ID (число) в файле .env — образец в .env.example")
FIRST_SUPERADMIN_ID = int(_first_admin)

# Логи — в консоль и в файл
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Инициализация бота с хранилищем состояний FSM
storage = StateMemoryStorage()
bot = AsyncTeleBot(BOT_TOKEN, state_storage=storage)

# Названия ролей и филиалов
ROLE_NAMES = {
    ROLE_WORKER: "Работник",
    ROLE_MANAGER: "Менеджер",
    ROLE_BRANCH_ADMIN: "Управляющий",
    ROLE_SUPERADMIN: "Администратор"
}

BRANCHES = ["Гагарина", "Абая", "Гугл"]

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

# =====================================================================
# ПРОВЕРКА РОЛИ
# =====================================================================
async def get_user_with_role(user_id: int, roles):
    """Пользователь из БД, если у него одна из ролей roles, иначе None."""
    user = await db.get_user(user_id)
    if user and user['role'] in roles:
        return user
    return None


def can_manage_branch(manager, branch: str) -> bool:
    """Руководитель управляет своим филиалом, Администратор — любым."""
    return manager['role'] == ROLE_SUPERADMIN or manager['branch'] == branch


def esc(value) -> str:
    """Экранирование пользовательского текста перед вставкой в HTML-сообщение."""
    return html.escape(str(value))


BAD_DATE_TEXT = (
    "❗️ Не получилось распознать дату. Введите её в формате <b>ДД.ММ</b>, например <code>25.10</code>.\n\n"
    "<i>Для отмены введите /cancel</i>"
)
BAD_SHIFT_TEXT = (
    "❗️ Не получилось распознать время. Введите <code>08:00 - 20:00</code> или <code>Выходной</code>.\n\n"
    "<i>Для отмены введите /cancel</i>"
)

# =====================================================================
# КЛАВИАТУРЫ И ИНТЕРФЕЙС
# =====================================================================
def get_main_keyboard(role: str) -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("📅 Моё расписание"))
    markup.add(KeyboardButton("✏️ Запросить изменение расписания"))
    markup.add(KeyboardButton("👤 Мой профиль"))
    
    if role in MANAGEMENT_ROLES:
        markup.add(KeyboardButton("🏢 Расписание филиала"))
        markup.add(KeyboardButton("📥 Запросы на изменение"))
        markup.add(KeyboardButton("🛠 Изменить расписание сотрудника"))
        
    if role == ROLE_SUPERADMIN:
        markup.add(KeyboardButton("👤 Назначить роль"))
        markup.add(KeyboardButton("📢 Глобальная рассылка"))
        markup.add(KeyboardButton("⏸ Приостановить/Запустить бота"))
        
    return markup

def get_branches_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for b in BRANCHES:
        markup.add(InlineKeyboardButton(text=b, callback_data=f"select_branch:{b}"))
    markup.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action"))
    return markup

def get_roles_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="Работник", callback_data=f"set_role:{target_user_id}:{ROLE_WORKER}"))
    markup.add(InlineKeyboardButton(text="Менеджер", callback_data=f"set_role:{target_user_id}:{ROLE_MANAGER}"))
    markup.add(InlineKeyboardButton(text="Управляющий", callback_data=f"set_role:{target_user_id}:{ROLE_BRANCH_ADMIN}"))
    markup.add(InlineKeyboardButton(text="Второй Администратор", callback_data=f"set_role:{target_user_id}:{ROLE_SUPERADMIN}"))
    markup.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action"))
    return markup

# =====================================================================
# МИДДЛВЕЙР ПРОВЕРКИ ПАУЗЫ БОТА
# =====================================================================
async def check_pause(message_or_call) -> bool:
    user_id = message_or_call.from_user.id
    user = await db.get_user(user_id)
    paused = await db.is_bot_paused()
    # Log pause check details
    logger.info("check_pause: user_id=%s, paused=%s, role=%s", user_id, paused, user['role'] if user else None)
    # Суперадмин может взаимодействовать с ботом даже на паузе
    if paused and (not user or user['role'] != ROLE_SUPERADMIN):
        msg = "⛔️ Бот временно остановлен администратором."
        if isinstance(message_or_call, telebot.types.CallbackQuery):
            await bot.answer_callback_query(message_or_call.id, msg, show_alert=True)
        else:
            await bot.send_message(message_or_call.chat.id, msg)
        return True
    return False

# =====================================================================
# СИСТЕМНЫЕ ХЭНДЛЕРЫ: ОТМЕНА, СПРАВКА, ПРОФИЛЬ
# =====================================================================
@bot.message_handler(commands=['cancel'], state="*")
@bot.message_handler(func=lambda m: m.text in ["❌ Отмена", "Отмена", "/cancel"], state="*")
async def cmd_cancel(message: telebot.types.Message):
    """Сброс любого текущего состояния FSM и возврат в главное меню."""
    current_state = await bot.get_state(message.from_user.id, message.chat.id)
    user = await db.get_user(message.from_user.id)
    role = user['role'] if user else ROLE_WORKER
    
    if current_state is not None:
        await bot.delete_state(message.from_user.id, message.chat.id)
        await bot.send_message(
            message.chat.id,
            "❌ Действие отменено. Вы вернулись в главное меню.",
            reply_markup=get_main_keyboard(role)
        )
    else:
        await bot.send_message(
            message.chat.id,
            "ℹ️ Нет активных действий для отмены.",
            reply_markup=get_main_keyboard(role)
        )

@bot.callback_query_handler(func=lambda c: c.data == "cancel_action", state="*")
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
    except Exception:
        pass

@bot.message_handler(commands=['help'])
async def cmd_help(message: telebot.types.Message):
    """Справка по доступным возможностям бота с учетом роли пользователя."""
    if await check_pause(message):
        return
        
    user = await db.get_user(message.from_user.id)
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
        "• <b>📅 Моё расписание</b> — просмотр персонального графика смен\n"
        "• <b>✏️ Запросить изменение расписания</b> — подача заявки руководству\n"
        "• <b>👤 Мой профиль</b> — просмотр ФИО, филиала, должности и роли\n"
    )
    
    if role in MANAGEMENT_ROLES:
        text += (
            "\n<b>Возможности руководства:</b>\n"
            "• <b>🏢 Расписание филиала</b> — просмотр смен всех сотрудников филиала\n"
            "• <b>📥 Запросы на изменение</b> — одобрение или отклонение заявок работников\n"
            "• <b>🛠 Изменить расписание сотрудника</b> — прямое назначение/редактирование смены\n"
        )
        
    if role == ROLE_SUPERADMIN:
        text += (
            "\n<b>Панель Главного Администратора:</b>\n"
            "• <b>👤 Назначить роль</b> — смена прав пользователей (Работник, Менеджер, Управляющий, Администратор)\n"
            "• <b>📢 Глобальная рассылка</b> — отправка объявления всем зарегистрированным пользователям\n"
            "• <b>⏸ Приостановить/Запустить бота</b> — переключение режима обслуживания системы\n"
        )
        
    await bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=['profile'])
@bot.message_handler(func=lambda m: m.text == "👤 Мой профиль")
async def cmd_profile(message: telebot.types.Message):
    """Отображение карточки профиля пользователя."""
    if await check_pause(message):
        return
        
    user = await db.get_user(message.from_user.id)
    if not user:
        await bot.send_message(
            message.chat.id,
            "⚠️ Вы еще не зарегистрированы в системе. Нажмите /start для регистрации."
        )
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
    logger.info("cmd_start invoked for user_id=%s", message.from_user.id)
    if await check_pause(message):
        return
    user = await db.get_user(message.from_user.id)
    if user:
        logger.info("Existing user %s found, sending welcome back", user['telegram_id'])
        await bot.send_message(
            message.chat.id,
            f"С возвращением, {user['full_name']}!\n"
            f"Ваша роль: {ROLE_NAMES.get(user['role'], 'Работник')}\n"
            f"Филиал: {user['branch']}\n\n"
            f"Используйте кнопки меню ниже или команду /help для справки.",
            reply_markup=get_main_keyboard(user['role'])
        )
    else:
        logger.info("New user %s not found, starting registration", message.from_user.id)
        await bot.send_message(
            message.chat.id,
            "Добро пожаловать! Давайте пройдем регистрацию.\n\n"
            "Введите ваше ФИО (например: Иванов Иван):\n\n"
            "<i>Для отмены введите /cancel</i>",
            parse_mode="HTML"
        )
        await bot.set_state(message.from_user.id, RegistrationState.full_name, message.chat.id)

@bot.message_handler(state=RegistrationState.full_name)
async def process_full_name(message: telebot.types.Message):
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['full_name'] = message.text.strip()
        
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
    position = message.text.strip()
    user_id = message.from_user.id
    
    async with bot.retrieve_data(user_id, message.chat.id) as data:
        full_name = data['full_name']
        branch = data['branch']
        
    # По умолчанию — Работник (если не первый суперадмин)
    role = ROLE_SUPERADMIN if user_id == FIRST_SUPERADMIN_ID else ROLE_WORKER
    
    await db.add_user(user_id, full_name, branch, position, role)
    await bot.delete_state(user_id, message.chat.id)
    
    await bot.send_message(
        message.chat.id,
        "🎉 Регистрация успешно завершена! Доступные функции отображены в меню ниже.",
        reply_markup=get_main_keyboard(role)
    )
    
    # Уведомление Суперадминам о новой регистрации
    superadmins = await db.get_superadmins()
    for admin in superadmins:
        try:
            await bot.send_message(
                admin['telegram_id'],
                f"🔔 <b>Новый пользователь зарегистрирован!</b>\n\n"
                f"<b>ID:</b> <code>{user_id}</code>\n"
                f"<b>ФИО:</b> {esc(full_name)}\n"
                f"<b>Филиал:</b> {esc(branch)}\n"
                f"<b>Должность:</b> {esc(position)}",
                parse_mode="HTML",
                reply_markup=get_roles_keyboard(user_id)
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin['telegram_id']}: {e}")

# =====================================================================
# ХЭНДЛЕРЫ РАБОТНИКА
# =====================================================================
@bot.message_handler(func=lambda m: m.text == "📅 Моё расписание")
async def view_my_schedule(message: telebot.types.Message):
    if await check_pause(message):
        return
        
    schedules = await db.get_user_schedule(message.from_user.id)
    if not schedules:
        await bot.send_message(message.chat.id, "У вас пока нет назначенного расписания.")
        return
        
    text = "🗓 <b>Ваше расписание:</b>\n\n"
    for s in schedules:
        text += f"• <b>{esc(format_date(s['date_str']))}:</b> <code>{esc(s['shift_time'])}</code>\n"
        
    await bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "✏️ Запросить изменение расписания")
async def start_request_schedule(message: telebot.types.Message):
    if await check_pause(message):
        return
    if not await db.get_user(message.from_user.id):
        await bot.send_message(message.chat.id, "⚠️ Вы еще не зарегистрированы в системе. Нажмите /start для регистрации.")
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
    
    req_id = await db.create_request(user_id, date_str, desired_time)
    await bot.send_message(message.chat.id, "✅ Ваш запрос успешно отправлен руководству филиала!")
    
    # Уведомление Менеджеров и Управляющих филиала
    managers = await db.get_managers_and_admins(user['branch'])
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Принять", callback_data=f"app_req:{req_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_req:{req_id}")
    )
    
    for mgr in managers:
        try:
            await bot.send_message(
                mgr['telegram_id'],
                f"📩 <b>Новый запрос на изменение расписания!</b>\n\n"
                f"<b>Сотрудник:</b> {esc(user['full_name'])} ({esc(user['position'])})\n"
                f"<b>Филиал:</b> {esc(user['branch'])}\n"
                f"<b>Дата:</b> {esc(format_date(date_str))}\n"
                f"<b>Желаемое время:</b> {esc(desired_time)}",
                parse_mode="HTML",
                reply_markup=markup
            )
        except Exception as e:
            logger.error(f"Не удалось отправить запрос менеджеру {mgr['telegram_id']}: {e}")

# =====================================================================
# ХЭНДЛЕРЫ МЕНЕДЖЕРА И УПРАВЛЯЮЩЕГО
# =====================================================================
@bot.message_handler(func=lambda m: m.text == "🏢 Расписание филиала")
async def view_branch_schedule(message: telebot.types.Message):
    if await check_pause(message):
        return
        
    user = await get_user_with_role(message.from_user.id, MANAGEMENT_ROLES)
    if not user:
        return
        
    employees = await db.get_branch_schedule(user['branch'])
    if not employees:
        await bot.send_message(message.chat.id, "В вашем филиале нет сотрудников.")
        return
        
    text = f"🏢 <b>Расписание филиала «{esc(user['branch'])}»:</b>\n\n"
    for emp in employees:
        text += f"👤 <b>{esc(emp['full_name'])}</b> (<i>{esc(emp['position'])}</i>):\n"
        if emp['shifts']:
            for date_str, shift_time in emp['shifts']:
                text += f"   • {esc(format_date(date_str))}: <code>{esc(shift_time)}</code>\n"
        else:
            text += "   • <i>Расписание отсутствует</i>\n"
        text += "\n"
            
    await bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📥 Запросы на изменение")
async def view_pending_requests(message: telebot.types.Message):
    if await check_pause(message):
        return
        
    user = await get_user_with_role(message.from_user.id, MANAGEMENT_ROLES)
    if not user:
        return
        
    requests = await db.get_pending_requests(user['branch'])
    if not requests:
        await bot.send_message(message.chat.id, "Активных запросов нет.")
        return
        
    for r in requests:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("✅ Принять", callback_data=f"app_req:{r['id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_req:{r['id']}")
        )
        await bot.send_message(
            message.chat.id,
            f"📋 <b>Запрос №{r['id']}</b>\n"
            f"<b>Сотрудник:</b> {esc(r['full_name'])} ({esc(r['position'])})\n"
            f"<b>Дата:</b> {esc(format_date(r['date_str']))}\n"
            f"<b>Желаемое время:</b> {esc(r['desired_time'])}",
            parse_mode="HTML",
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda c: c.data.startswith(('app_req:', 'rej_req:')))
async def process_request_decision(call: telebot.types.CallbackQuery):
    if await check_pause(call):
        return
        
    action, req_id_str = call.data.split(':')
    req_id = int(req_id_str)

    manager = await get_user_with_role(call.from_user.id, MANAGEMENT_ROLES)
    req_branch = await db.get_request_branch(req_id)
    if not manager or req_branch is None or not can_manage_branch(manager, req_branch):
        await bot.answer_callback_query(call.id, "⛔️ Недостаточно прав.", show_alert=True)
        return
    
    approve = action == 'app_req'
    req = await db.decide_request(req_id, approve)
    if not req:
        await bot.answer_callback_query(call.id, "Запрос уже обработан.", show_alert=True)
        return
        
    await bot.answer_callback_query(call.id, "Решение сохранено!")
    await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    
    # Уведомление работника
    if approve:
        msg_text = f"🔔 Ваше расписание на [{format_date(req['date_str'])}] было изменено: новое время [{req['desired_time']}]."
    else:
        msg_text = f"❌ Ваш запрос на изменение расписания на [{format_date(req['date_str'])}] был отклонен."
        
    try:
        await bot.send_message(req['user_id'], msg_text)
    except Exception as e:
        logger.error(f"Ошибка при отправке ЛС пользователю {req['user_id']}: {e}")

@bot.message_handler(func=lambda m: m.text == "🛠 Изменить расписание сотрудника")
async def start_edit_schedule(message: telebot.types.Message):
    if await check_pause(message):
        return
        
    user = await get_user_with_role(message.from_user.id, MANAGEMENT_ROLES)
    if not user:
        return
        
    employees = await db.get_branch_users(user['branch'])
    if not employees:
        await bot.send_message(message.chat.id, "Нет сотрудников для редактирования.")
        return
        
    markup = InlineKeyboardMarkup()
    for emp in employees:
        markup.add(InlineKeyboardButton(text=f"{emp['full_name']} ({emp['position']})", callback_data=f"edit_emp:{emp['telegram_id']}"))
    markup.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action"))
        
    await bot.send_message(message.chat.id, "Выберите сотрудника для редактирования расписания:", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith('edit_emp:'))
async def select_emp_to_edit(call: telebot.types.CallbackQuery):
    target_id = int(call.data.split(':')[1])
    manager = await get_user_with_role(call.from_user.id, MANAGEMENT_ROLES)
    target = await db.get_user(target_id)
    if not manager or not target or not can_manage_branch(manager, target['branch']):
        await bot.answer_callback_query(call.id, "⛔️ Недостаточно прав.", show_alert=True)
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
    
    await db.set_shift(target_id, date_str, shift_time)
    await bot.send_message(message.chat.id, "✅ Расписание сотрудника успешно обновлено!")
    
    # Личное уведомление сотруднику
    try:
        await bot.send_message(
            target_id,
            f"🔔 Ваше расписание на [{format_date(date_str)}] было изменено: новое время [{shift_time}]."
        )
    except Exception as e:
        logger.error(f"Не удалось отправить личное сообщение {target_id}: {e}")

# =====================================================================
# ХЭНДЛЕРЫ СУПЕРАДМИНИСТРАТОРА
# =====================================================================
@bot.message_handler(func=lambda m: m.text == "👤 Назначить роль")
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
    if not message.text.isdigit():
        await bot.send_message(message.chat.id, "Пожалуйста, введите корректный числовой Telegram ID:")
        return
        
    target_id = int(message.text.strip())
    target_user = await db.get_user(target_id)
    
    if not target_user:
        await bot.send_message(message.chat.id, "⚠️ Пользователь с таким ID не найден в базе данных.")
        await bot.delete_state(message.from_user.id, message.chat.id)
        return
        
    await bot.delete_state(message.from_user.id, message.chat.id)
    await bot.send_message(
        message.chat.id,
        f"Выберите новую роль для <b>{esc(target_user['full_name'])}</b>:",
        parse_mode="HTML",
        reply_markup=get_roles_keyboard(target_id)
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith('set_role:'))
async def process_role_callback(call: telebot.types.CallbackQuery):
    if not await get_user_with_role(call.from_user.id, (ROLE_SUPERADMIN,)):
        await bot.answer_callback_query(call.id, "⛔️ Недостаточно прав.", show_alert=True)
        return

    _, target_id_str, new_role = call.data.split(':')
    target_id = int(target_id_str)
    target_user = await db.get_user(target_id)
    if not target_user or new_role not in ROLE_NAMES:
        await bot.answer_callback_query(call.id, "⚠️ Пользователь или роль не найдены.", show_alert=True)
        return

    superadmins = await db.count_superadmins()
    # Жесткое ограничение на количество Администраторов (максимум 2)
    if new_role == ROLE_SUPERADMIN and target_user['role'] != ROLE_SUPERADMIN and superadmins >= 2:
        await bot.answer_callback_query(call.id, "❌ Достигнут лимит: в системе может быть максимум 2 Администратора!", show_alert=True)
        return
    # Нельзя снять последнего Администратора — иначе управлять ботом будет некому
    if target_user['role'] == ROLE_SUPERADMIN and new_role != ROLE_SUPERADMIN and superadmins <= 1:
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
        logger.error(f"Не удалось уведомить пользователя {target_id}: {e}")

@bot.message_handler(func=lambda m: m.text == "📢 Глобальная рассылка")
async def start_broadcast(message: telebot.types.Message):
    if not await get_user_with_role(message.from_user.id, (ROLE_SUPERADMIN,)):
        return
        
    await bot.send_message(
        message.chat.id,
        "📢 Введите текст сообщения для глобальной рассылки:\n\n"
        "<i>Для отмены введите /cancel</i>",
        parse_mode="HTML"
    )
    await bot.set_state(message.from_user.id, AdminBroadcastState.message_text, message.chat.id)

@bot.message_handler(state=AdminBroadcastState.message_text)
async def process_broadcast_text(message: telebot.types.Message):
    text = message.text
    await bot.delete_state(message.from_user.id, message.chat.id)
    
    users = await db.get_all_users()
    count = 0
    for u in users:
        try:
            await bot.send_message(u['telegram_id'], f"📢 <b>Объявление от администрации:</b>\n\n{esc(text)}", parse_mode="HTML")
            count += 1
        except Exception:
            pass
            
    await bot.send_message(message.chat.id, f"✅ Рассылка завершена. Успешно отправлено: {count} пользователям.")

@bot.message_handler(func=lambda m: m.text == "⏸ Приостановить/Запустить бота")
async def toggle_bot_pause(message: telebot.types.Message):
    if not await get_user_with_role(message.from_user.id, (ROLE_SUPERADMIN,)):
        return
        
    current_state = await db.is_bot_paused()
    new_state = not current_state
    await db.set_bot_paused(new_state)
    
    users = await db.get_all_users()
    if new_state:
        status_msg = "⛔️ <b>Бот временно остановлен администратором.</b>"
    else:
        status_msg = "✅ <b>Работа бота возобновлена!</b>"
        
    for u in users:
        try:
            await bot.send_message(u['telegram_id'], status_msg, parse_mode="HTML")
        except Exception:
            pass
            
    state_desc = "приостановлена" if new_state else "возобновлена"
    await bot.send_message(message.chat.id, f"Работа бота была {state_desc}.")

# =====================================================================
# ФИЛЬТРЫ И ЗАПУСК
# =====================================================================
async def main():
    # Инициализация структуры БД
    await db.init_db(FIRST_SUPERADMIN_ID)
    
    # Добавление фильтров состояний
    bot.add_custom_filter(asyncio_filters.StateFilter(bot))
    bot.add_custom_filter(asyncio_filters.IsDigitFilter())
    
    logger.info("Бот успешно запущен и готов к работе (обновление v1.1.0)!")
    await bot.polling(non_stop=True, skip_pending=True)

if __name__ == "__main__":
    asyncio.run(main())
