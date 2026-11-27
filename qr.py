"""QR-отметка прихода: меняющийся код филиала. Только стандартная библиотека.

Код живёт в окнах по QR_WINDOW_SECONDS секунд: код = HMAC-SHA256(ключ филиала, номер окна),
ключ филиала = HMAC-SHA256(секрет, код филиала). Секрет — QR_SECRET_KEY из .env,
без него коды не создаются и не проверяются.

Ссылка: https://t.me/<бот>?start=chk_<код филиала>_<код>. Telegram пропускает
в параметре start только A-Za-z0-9_- и не длиннее 64 символов.
"""
import hashlib
import hmac
import re

QR_WINDOW_SECONDS = 15     # длина окна: код меняется раз в 15 секунд
CODE_LEN = 10              # символов hex (40 бит)
START_PREFIX = "chk_"      # параметр /start для отметки прихода

_PAYLOAD_RE = re.compile(r"chk_([a-z0-9]{1,20})_([0-9a-f]{%d})" % CODE_LEN)


def window(ts: int) -> int:
    """Номер окна (по QR_WINDOW_SECONDS секунд) для unix-времени."""
    return int(ts) // QR_WINDOW_SECONDS


def make_code(secret: str, branch_code: str, win: int) -> str:
    branch_key = hmac.new(secret.encode(), branch_code.encode(), hashlib.sha256).digest()
    return hmac.new(branch_key, str(win).encode(), hashlib.sha256).hexdigest()[:CODE_LEN]


def make_payload(secret: str, branch_code: str, ts: int) -> str:
    """Параметр start для текущего окна: chk_<филиал>_<код>."""
    return f"{START_PREFIX}{branch_code}_{make_code(secret, branch_code, window(ts))}"


def make_link(bot_username: str, payload: str) -> str:
    return f"https://t.me/{bot_username}?start={payload}"


def parse_payload(payload: str) -> tuple[str, str] | None:
    """«chk_abaya_0123456789» → («abaya», «0123456789»); None — плохой формат."""
    m = _PAYLOAD_RE.fullmatch(payload or "")
    return m.groups() if m else None


def verify(secret: str, branch_code: str, code: str, ts: int) -> bool:
    """Код верен для окна t−1, t или t+1, где t — окно ts (времени сообщения Telegram)."""
    t = window(ts)
    ok = False
    for win in (t - 1, t, t + 1):
        ok |= hmac.compare_digest(make_code(secret, branch_code, win), code)
    return ok
