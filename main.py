"""KRANIN SHOP — production-ready Telegram-магазин цифровых товаров.

Версия 3.0: безопасные денежные операции, устойчивые FSM-сессии,
пагинация длинных списков, расширенная диагностика и обновлённый UI.
"""

import asyncio
import csv
import html
import io
import logging
import math
import os
import random
import re
import secrets
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    delete,
    exists,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates

# ==========================================
# ЛОГИРОВАНИЕ И КОНФИГУРАЦИЯ БД
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

APP_VERSION = "3.0.0"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "KRANIN_SUPPORT").strip().lstrip("@")
REVIEWS_CHANNEL_URL = "https://t.me/KRANIN_REVIEWS"
WELCOME_PHOTO_FILE_ID = os.getenv("WELCOME_PHOTO_FILE_ID", "").strip() or None
REDIS_URL = os.getenv("REDIS_URL", "").strip()


def env_int(name: str, default: int, minimum: int = 0, maximum: int = 1_000_000) -> int:
    """Читает целую настройку без падения всего процесса из-за опечатки."""
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        logger.warning("Некорректное %s; используется значение %s", name, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning("%s вне диапазона %s…%s; используется %s", name, minimum, maximum, default)
        return default
    return value


def env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 1_000_000.0) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        logger.warning("Некорректное %s; используется значение %s", name, default)
        return default
    if not math.isfinite(value) or not minimum <= value <= maximum:
        logger.warning("%s вне допустимого диапазона; используется %s", name, default)
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "y"}


raw_admin_ids = os.getenv("ADMIN_IDS", "8479717148")
ADMIN_IDS = sorted({int(x.strip()) for x in raw_admin_ids.split(",") if x.strip().isdigit()})
# НОВОЕ: диагностика — os.getenv возвращает дефолт ТОЛЬКО если переменная
# окружения полностью не задана. Если ADMIN_IDS на Render задан, но пустой
# или содержит нечисловой мусор (пробелы, лишние запятые и т.п.), список
# молча становится пустым — и все уведомления админам (отзывы, депозиты,
# низкий остаток и т.д.) просто не с кем отправлять: цикл `for admin_id in
# ADMIN_IDS` ни разу не выполнится, поэтому в логах не будет даже ошибки.
logger.info(f"ADMIN_IDS: raw='{raw_admin_ids}' -> parsed={ADMIN_IDS}")
if not ADMIN_IDS:
    logger.warning(
        "ADMIN_IDS пуст! Все уведомления администраторам (отзывы, депозиты, "
        "низкий остаток ключей и т.д.) отправляться не будут. Проверьте "
        "переменную окружения ADMIN_IDS на Render — она должна содержать "
        "ваш Telegram ID (число), например: 123456789"
    )

# Разумные пределы для финансовых сумм, чтобы отсечь inf/nan/переполнения
MAX_AMOUNT = 1_000_000.0
MIN_WITHDRAWAL = env_float("MIN_WITHDRAWAL", 500.0, 1.0, MAX_AMOUNT)
MIN_DEPOSIT = env_float("MIN_DEPOSIT", 20.0, 1.0, MAX_AMOUNT)
REFERRAL_PERCENT = env_float("REFERRAL_PERCENT", 5.0, 0.0, 100.0)
DROP_PENDING_UPDATES = env_bool("DROP_PENDING_UPDATES", False)

# Сколько заявок на пополнение в статусе "pending" может одновременно
# висеть у одного пользователя, прежде чем он сможет создать новую.
MAX_PENDING_DEPOSITS = env_int("MAX_PENDING_DEPOSITS", 3, 1, 20)
LOW_STOCK_THRESHOLD = env_int("LOW_STOCK_THRESHOLD", 3, 0, 100_000)
CATALOG_PAGE_SIZE = env_int("CATALOG_PAGE_SIZE", 8, 4, 12)
HISTORY_PAGE_SIZE = env_int("HISTORY_PAGE_SIZE", 5, 3, 8)

# НОВОЕ: колесо фортуны после покупки — вероятности и призы
WHEEL_KEY_CHANCE = 0.05       # 5% — ключ на 1 день любого софта
WHEEL_BALANCE_CHANCE = 0.05   # 5% — 50 грн на баланс
# оставшиеся 90% — "повезёт в следующий раз"
WHEEL_BALANCE_PRIZE = 50.0
WHEEL_KEY_DURATION = "1 день"

# Максимальные длины пользовательского текста, чтобы не словить необработанный
# DataError от БД (Postgres обрывает insert, если строка длиннее колонки)
MAX_REVIEW_LENGTH = 500
MAX_PROMO_CODE_LENGTH = 50
MAX_SOFTWARE_NAME_LENGTH = 100
MAX_DURATION_LENGTH = 50

# Порт для HTTP-заглушки: Render Free Web Service требует, чтобы процесс
# слушал $PORT, иначе деплой считается "упавшим" и уходит в бесконечный рестарт
PORT = env_int("PORT", 10000, 1, 65535)

if not DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
    )
else:
    DATABASE_URL = (
        DATABASE_URL
        .replace("postgres://", "postgresql+asyncpg://")
        .replace("postgresql://", "postgresql+asyncpg://")
    )
    if "?" in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.split("?")[0]

    connect_args = {}
    if any(domain in DATABASE_URL for domain in ["oregon-postgres", "frankfurt-postgres", "render.com"]):
        connect_args = {"ssl": "require"}

    # PostgreSQL connections can be closed by the provider while the bot is idle.
    # pre_ping checks a pooled connection before reusing it, and recycle prevents
    # very old connections from living in the pool indefinitely.
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=1200,
        pool_size=5,
        max_overflow=10,
    )

# Реквизиты можно переопределить переменной окружения PAYMENT_CARDS
# (несколько значений через « ; »), чтобы менять карты без правки кода.
_DEFAULT_CARDS = [
    "4441 1111 5555 7352",
    "4323 3473 8685 7285",
    "5232 4410 4407 1160",
]
CARDS = [c.strip() for c in os.getenv("PAYMENT_CARDS", "").split(";") if c.strip()] or _DEFAULT_CARDS

PERMANENT_DURATION = "Навсегда"
DURATIONS = ["1 день", "7 дней", "30 дней", PERMANENT_DURATION]
DURATION_SORT_ORDER = {duration: index for index, duration in enumerate(DURATIONS)}
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_MAX_BUTTONS = 100

# Служебная категория для игр, которым не нужен выбор ОС покупателем.
NO_OS_PLATFORM_NAME = "__NO_OS__"

async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
router = Router()


# ==========================================
# ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК
# ==========================================
# ФИКС ГЛАВНОГО БАГА "БОТ ЗАВИСАЕТ": если внутри любого хэндлера возникало
# необработанное исключение (например, Telegram отклонял edit_text из-за
# сломанной Markdown-разметки в ключе), aiogram просто логировал ошибку и
# останавливал обработку апдейта. callback.answer() при этом ни разу не
# вызывался, поэтому у пользователя нажатая кнопка вечно "крутилась"
# (Telegram ждёт answerCallbackQuery до ~30-60 сек, и это выглядит как
# зависание бота). Теперь любая ошибка перехватывается централизованно:
# мы логируем её и гарантированно отвечаем пользователю, чтобы кнопка
# разблокировалась, а чат не зависал в ожидании ответа.
_recent_errors: dict[str, float] = {}
ERROR_ALERT_COOLDOWN = 60.0


@router.errors()
async def global_error_handler(event: ErrorEvent, bot: Bot) -> bool:
    logger.exception(
        f"Необработанная ошибка при обработке апдейта: {event.exception}",
        exc_info=event.exception,
    )

    update = event.update
    try:
        if update.callback_query:
            try:
                await update.callback_query.answer(
                    "⚠️ Произошла ошибка при обработке запроса. Попробуйте ещё раз "
                    "или обратитесь в поддержку.",
                    show_alert=True,
                )
            except Exception:
                pass
        elif update.message:
            try:
                await update.message.answer(
                    "⚠️ Произошла ошибка при обработке запроса. Попробуйте ещё раз "
                    "или обратитесь в поддержку."
                )
            except Exception:
                pass
    except Exception:
        logger.exception("Ошибка внутри самого глобального обработчика ошибок")

    # НОВОЕ: уведомляем админов о непредвиденных ошибках, чтобы их не пришлось
    # искать вручную в логах Render. Одинаковые ошибки не чаще раза в минуту —
    # иначе при поломке в горячем хендлере админам прилетает лавина копий.
    err_key = f"{type(event.exception).__name__}:{str(event.exception)[:200]}"
    now = time.monotonic()
    if now - _recent_errors.get(err_key, -1e9) >= ERROR_ALERT_COOLDOWN:
        _recent_errors[err_key] = now
        if len(_recent_errors) > 200:
            for k in [k for k, ts in _recent_errors.items() if now - ts > ERROR_ALERT_COOLDOWN]:
                _recent_errors.pop(k, None)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🐞 Ошибка в боте: `{escape_md(str(event.exception))[:500]}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    return True


def escape_md(value) -> str:
    if value is None:
        return ""
    value = str(value)
    for ch in ("\\", "_", "*", "`", "["):
        value = value.replace(ch, "\\" + ch)
    return value


def md_url(url: str) -> str:
    # Скобки в URL ломают Markdown-ссылку [текст](url) и роняют отправку сообщения
    return (url or "").strip().replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def luhn_ok(digits: str) -> bool:
    # Контрольная сумма номера банковской карты (алгоритм Луна)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


async def lock_row(session: AsyncSession, model, row_id):
    # Блокирует строку (SELECT ... FOR UPDATE) и перечитывает её актуальное
    # состояние. Нужна, чтобы двойное нажатие кнопки админом / игроком не
    # обработало одну и ту же заявку или приз дважды.
    res = await session.execute(
        select(model).where(model.id == row_id).with_for_update().execution_options(populate_existing=True)
    )
    return res.scalar_one_or_none()


async def append_status(callback: CallbackQuery, suffix: str) -> None:
    # Дописывает статус к сообщению с заявкой (текст или подпись к фото).
    # parse_mode не используется: исходный текст уже без разметки, и юзернейм
    # с «_» раньше ломал Markdown и обрывал обработку заявки после commit.
    msg = callback.message
    try:
        if msg.caption is not None:
            await msg.edit_caption(caption=(msg.caption or "") + suffix, reply_markup=None)
        else:
            await msg.edit_text(text=(msg.text or "") + suffix, reply_markup=None)
    except TelegramBadRequest as e:
        logger.warning(f"Не удалось обновить сообщение заявки: {e}")


async def notify_admins(bot: Bot, text_msg: str, reply_markup=None, parse_mode: str | None = "Markdown") -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text_msg, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")


_background_tasks: set = set()


def spawn_background(coro) -> None:
    """Запускает фоновую работу с удержанием ссылки и логированием результата."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def on_done(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            return
        if error:
            logger.error(
                "Фоновая задача завершилась с ошибкой",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(on_done)


async def safe_edit_text(
    callback: CallbackQuery,
    bot: Bot,
    text_msg: str,
    keyboard: InlineKeyboardMarkup | None,
    parse_mode: str = "Markdown",
) -> None:
    """Безопасно меняет текстовый и медиа-экран, соблюдая лимиты Telegram."""
    text_msg = clip_text(text_msg)
    try:
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        with suppress(Exception):
            await callback.message.delete()
        try:
            await bot.send_message(callback.message.chat.id, text_msg, reply_markup=keyboard, parse_mode=parse_mode)
        except TelegramBadRequest:
            logger.exception("Не удалось отправить экран с Markdown; повторяю без разметки")
            plain_text = re.sub(r"\\([\\_*`\[])", r"\1", text_msg)
            plain_text = plain_text.replace("**", "").replace("`", "")
            await bot.send_message(callback.message.chat.id, plain_text, reply_markup=keyboard, parse_mode=None)


def user_display(message_or_callback) -> str:
    u = message_or_callback.from_user
    username = u.username
    if username:
        return f"@{escape_md(username)}"
    return "без юзернейма"


def parse_positive_amount(raw_text: str, max_amount: float = MAX_AMOUNT) -> float | None:
    """Парсит сумму через Decimal: без NaN/inf и с корректным округлением копеек."""
    if raw_text is None:
        return None
    normalized = raw_text.strip().replace(" ", "").replace(",", ".")
    if not normalized or len(normalized) > 40:
        return None
    try:
        value = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0 or value > Decimal(str(max_amount)):
        return None
    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value) if value > 0 else None


def round_money(value) -> float:
    """Единое округление денег во всех финансовых операциях."""
    try:
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            return 0.0
        return float(decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


def clip_text(value: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Не даёт тексту выйти за лимит Telegram и не обрезает его без предупреждения."""
    value = value or ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 32)].rstrip() + "\n\n… Список сокращён."


def normalize_web_url(value: str, max_length: int = 500) -> str | None:
    """Разрешает только обычные HTTP(S)-ссылки, безопасные для inline-кнопок."""
    candidate = (value or "").strip()
    if not candidate or len(candidate) > max_length:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return candidate


def format_dt(value: datetime | None, with_time: bool = True) -> str:
    if not value:
        return "—"
    return value.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def page_bounds(total: int, page: int, per_page: int) -> tuple[int, int, int]:
    total_pages = max(1, math.ceil(max(0, total) / per_page))
    page = max(0, min(page, total_pages - 1))
    return page, total_pages, page * per_page


def pagination_row(callback_prefix: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    """Единая навигация; prefix получает номер страницы последним аргументом."""
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="‹ Назад", callback_data=f"{callback_prefix}{page - 1}"))
    if total_pages > 1:
        row.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton(text="Вперёд ›", callback_data=f"{callback_prefix}{page + 1}"))
    return row


async def clear_state_keep_promo(state: FSMContext) -> None:
    """Сбрасывает незавершённый сценарий, но сохраняет активную скидку."""
    data = await state.get_data()
    promo_id = data.get("active_discount_promo_id")
    await state.clear()
    if promo_id:
        await state.update_data(active_discount_promo_id=promo_id)


def normalize_promo_code(value: str) -> str | None:
    """Нормализует промокод перед сохранением и активацией."""
    if value is None:
        return None
    value = value.strip().upper()
    if not value:
        return None
    if len(value) > MAX_PROMO_CODE_LENGTH:
        return None
    # Разрешаем только удобные для Telegram промокоды без пробелов:
    # латиница, цифры, дефис и подчёркивание.
    if not re.fullmatch(r"[A-Z0-9_-]+", value):
        return None
    return value


def normalize_duration(value: str) -> str:
    """Нормализует срок тарифа, сохраняя старые произвольные значения."""
    value = (value or "").strip()
    aliases = {
        "навсегда": PERMANENT_DURATION,
        "навечно": PERMANENT_DURATION,
        "бессрочно": PERMANENT_DURATION,
        "forever": PERMANENT_DURATION,
        "permanent": PERMANENT_DURATION,
    }
    return aliases.get(value.casefold(), value)


def log_balance_change(
    session: AsyncSession,
    user_id: int,
    amount: float,
    reason: str,
    balance_after: float,
    related_id: int | None = None,
) -> None:
    # Добавляет запись в лог операций с балансом (таблица BalanceLog).
    # Не делает commit самостоятельно — запись уходит в базу вместе
    # с остальными изменениями текущей транзакции.
    # amount: дельта изменения баланса (положительная — начисление, отрицательная — списание)
    # balance_after: баланс пользователя ПОСЛЕ применения этой операции
    session.add(
        BalanceLog(
            user_id=user_id,
            amount=round_money(amount),
            balance_after=round_money(balance_after),
            reason=reason,
            related_id=related_id,
        )
    )


def escape_like(value: str) -> str:
    # Экранирует служебные символы SQL LIKE/ILIKE (% и _ — вайлдкарды),
    # иначе username вида "john_doe" будет матчить и "johnXdoe".
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def find_user_by_identifier(session: AsyncSession, raw_identifier: str) -> "User | None":
    # Ищет пользователя по Telegram ID (число) либо по username (с @ или без).
    identifier = raw_identifier.strip()
    if identifier.startswith("@"):
        identifier = identifier[1:]

    if not identifier:
        return None

    if identifier.isdigit():
        user = await session.get(User, int(identifier))
        if user:
            return user
        # число может быть и частью username, поэтому продолжаем поиск ниже,
        # только если по ID никого не нашли

    # ФИКС: экранируем % и _ в пользовательском вводе, иначе ILIKE трактует
    # их как вайлдкарды SQL и может найти не того пользователя
    res = await session.execute(
        select(User).where(User.username.ilike(escape_like(identifier), escape="\\"))
    )
    return res.scalars().first()


# ==========================================
# ЛОКАЛИЗАЦИЯ И ЯЗЫКИ
# ==========================================
LANGUAGES = {
    "uk": {
        "welcome": (
            "💎 **KRANIN SHOP**\n"
            "Цифрові ліцензії та софт — в одному місці\n\n"
            "⚡️ Миттєва видача після оплати\n"
            "🔑 Усі ключі зберігаються в профілі\n"
            "🆘 Підтримка на зв’язку\n\n"
            "💰 **Ваш баланс:** `{balance:.2f} грн`\n\n"
            "Оберіть потрібний розділ:"
        ),
        "banned": "🚫 Ваш акаунт заблоковано в боті.",
        "lang_changed": "✅ Мову змінено на українську.",
        "lang_btn": "🌐 Мова",
        "catalog": "🛒 Каталог",
        "profile": "👤 Профіль",
        "deposit": "💳 Поповнити",
        "promo": "🎟 Промокод",
        "reviews": "⭐ Відгуки",
        "referral": "🤝 Партнёрська програма",
        "support": "🆘 Підтримка",
        "help": "ℹ️ Допомога",
        "admin": "🛠 Адмін-панель",
    },
    "ru": {
        "welcome": (
            "💎 **KRANIN SHOP**\n"
            "Цифровые лицензии и софт — в одном месте\n\n"
            "⚡️ Мгновенная выдача после оплаты\n"
            "🔑 Все ключи сохраняются в профиле\n"
            "🆘 Поддержка на связи\n\n"
            "💰 **Ваш баланс:** `{balance:.2f} грн`\n\n"
            "Выберите нужный раздел:"
        ),
        "banned": "🚫 Ваш аккаунт заблокирован в боте.",
        "lang_changed": "✅ Язык изменён на русский.",
        "lang_btn": "🌐 Язык",
        "catalog": "🛒 Каталог",
        "profile": "👤 Профиль",
        "deposit": "💳 Пополнить",
        "promo": "🎟 Промокод",
        "reviews": "⭐ Отзывы",
        "referral": "🤝 Партнёрская программа",
        "support": "🆘 Поддержка",
        "help": "ℹ️ Помощь",
        "admin": "🛠 Админ-панель",
    },
}

def get_language_kb(is_reg: bool = False):
    prefix = "reg_lang_" if is_reg else "lang_"
    keyboard = [
        [InlineKeyboardButton(text="🇺🇦 Українська", callback_data=f"{prefix}uk"),
         InlineKeyboardButton(text="🇷🇺 Русский", callback_data=f"{prefix}ru")]
    ]
    if not is_reg:
        keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ==========================================
# МОДЕЛИ БАЗЫ ДАННЫХ
# ==========================================
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(10), default="uk")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    @validates("balance")
    def _round_balance(self, key, value):
        # Баланс — Float, поэтому после серии += / -= накапливается «грязь»
        # вида 49.99999999. Округляем до копеек при каждой записи.
        return round_money(value) if value is not None else value

class Game(Base):
    """Игра — отдельная категория каталога, внутри которой лежат ОС и софт."""
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    softwares = relationship("Software", back_populates="game", cascade="all, delete-orphan")

class Platform(Base):
    __tablename__ = "platforms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    softwares = relationship("Software", back_populates="platform", cascade="all, delete-orphan")

class Software(Base):
    __tablename__ = "softwares"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"), nullable=True, index=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"))
    name: Mapped[str] = mapped_column(String(100))
    game = relationship("Game", back_populates="softwares")
    # НОВОЕ: file_id фото Telegram, которое показывается над текстом при выборе софта
    photo_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # НОВОЕ: описание софта (например, текст про аккаунт), показывается над списком тарифов
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # НОВОЕ: ссылка на файлы (например, инструкция/архив), выдаётся покупателю вместе с ключом
    files_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    platform = relationship("Platform", back_populates="softwares")
    products = relationship("Product", back_populates="software", cascade="all, delete-orphan")

class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    software_id: Mapped[int] = mapped_column(ForeignKey("softwares.id"))
    duration: Mapped[str] = mapped_column(String(50))
    price: Mapped[float] = mapped_column(Float)
    software = relationship("Software", back_populates="products")
    keys = relationship("LicenseKey", back_populates="product", cascade="all, delete-orphan")
    subscriptions = relationship("RestockSubscription", back_populates="product", cascade="all, delete-orphan")

    @validates("price")
    def _round_price(self, key, value):
        return round_money(value) if value is not None else value

class LicenseKey(Base):
    __tablename__ = "license_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    key_string: Mapped[str] = mapped_column(String(255), unique=True)
    is_sold: Mapped[bool] = mapped_column(Boolean, default=False)
    product = relationship("Product", back_populates="keys")

class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    # Точный тариф и цена на момент покупки — нужны для истории, статистики и отзывов.
    product_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("products.id"), nullable=True)
    product_name: Mapped[str] = mapped_column(String(100))
    key_issued: Mapped[str] = mapped_column(String(255))
    duration: Mapped[str] = mapped_column(String(50), default="")
    price_paid: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

# НОВОЕ: колесо фортуны после покупки. Отдельная таблица (а не колонка в
# Purchase) специально для того, чтобы не трогать уже существующую в
# продакшене таблицу purchases — create_all создаёт только отсутствующие
# таблицы и не делает ALTER TABLE, поэтому новая таблица безопасно
# появится сама при следующем запуске без ручной миграции.
class WheelSpin(Base):
    __tablename__ = "wheel_spins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ИЗМЕНЕНО: purchase_id стал nullable — спин теперь может быть привязан
    # либо к покупке, либо к отзыву (review_id). Ровно одно из двух заполнено.
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id"), unique=True, nullable=True)
    review_id: Mapped[int | None] = mapped_column(ForeignKey("reviews.id"), unique=True, nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    result: Mapped[str] = mapped_column(String(20))  # key / balance / lose
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class DepositRequest(Base):
    __tablename__ = "deposit_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    payment_card: Mapped[str | None] = mapped_column(String(100), nullable=True)
    receipt_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    processed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    amount: Mapped[float] = mapped_column(Float)
    card: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    processed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class BalanceLog(Base):
    # Журнал всех операций, меняющих баланс пользователя.
    __tablename__ = "balance_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    amount: Mapped[float] = mapped_column(Float)  # дельта: + начисление, - списание
    balance_after: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(50))  # purchase / deposit_approved / withdrawal_request / ...
    # ФИКС: raньше был Integer (int32) — падало с DataError "value out of int32
    # range", когда сюда записывали Telegram user_id (BigInteger), например
    # в реферальных бонусах и ручной корректировке баланса админом.
    related_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # id связанной сущности (заявки, покупки, user_id и т.д.)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PromoCode(Base):
    __tablename__ = "promo_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    # Старое поле сохраняем для совместимости: в режиме balance это сумма бонуса
    # на баланс. Для discount можно оставить 0.
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    uses_left: Mapped[int] = mapped_column(Integer, default=1)
    # balance — старый тип промокода (начисление на баланс),
    # discount — скидка на покупку.
    promo_type: Mapped[str] = mapped_column(String(20), default="balance", server_default="balance")
    discount_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    # NULL = действует на все товары, иначе только на конкретный тариф.
    product_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("products.id"), nullable=True)


class PromoCodeUsage(Base):
    # Фиксирует, что конкретный пользователь уже использовал конкретный промокод.
    __tablename__ = "promo_code_usages"
    __table_args__ = (UniqueConstraint("promo_id", "user_id", name="uq_promo_user_once"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    # Для скидочных промокодов сохраняем фактическую скидку и итоговую цену.
    discount_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    # Отзыв привязан к конкретному тарифу/покупке. Старые записи могут иметь NULL.
    product_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("products.id"), nullable=True)
    purchase_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("purchases.id"), nullable=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")

class RestockSubscription(Base):
    __tablename__ = "restock_subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_restock_user_product"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product = relationship("Product", back_populates="subscriptions")

# ==========================================
# MIDDLEWARE & STATES
# ==========================================
class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_pool):
        super().__init__()
        self.session_pool = session_pool

    async def __call__(self, handler, event: Update, data):
        async with self.session_pool() as session:
            data["session"] = session
            # ФИКС: middleware зарегистрирован через dp.update.middleware(...),
            # поэтому `event` здесь — это верхнеуровневый Update, а у него
            # НЕТ атрибута from_user (он есть только у вложенных
            # Update.message / Update.callback_query). getattr(event,
            # "from_user", None) молча возвращал None всегда, и проверка
            # бана никогда не выполнялась. Достаём реальное вложенное
            # событие (message/callback_query/...) и берём from_user у него.
            actual_event = (
                event.message
                or event.callback_query
                or event.edited_message
                or event.channel_post
                or event.my_chat_member
            )
            user_obj = getattr(actual_event, "from_user", None) if actual_event else None
            if user_obj:
                user = await session.get(User, user_obj.id)
                if user and user.is_banned:
                    lang = user.language if user.language in LANGUAGES else "uk"
                    msg_text = LANGUAGES[lang]["banned"]
                    if isinstance(actual_event, Message):
                        await actual_event.answer(msg_text)
                    elif isinstance(actual_event, CallbackQuery):
                        await actual_event.answer(msg_text, show_alert=True)
                    return

                # Старые inline-кнопки и сообщения без /start больше не приводят
                # к None-ошибкам: профиль создаётся автоматически. Для /start
                # регистрацию по-прежнему выполняет cmd_start с выбором языка.
                is_start = isinstance(actual_event, Message) and bool(
                    actual_event.text and actual_event.text.startswith("/start")
                )
                if not user and not is_start:
                    language = "ru" if (user_obj.language_code or "").casefold().startswith("ru") else "uk"
                    session.add(User(id=user_obj.id, username=user_obj.username, language=language))
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
            return await handler(event, data)

class ThrottleMiddleware(BaseMiddleware):
    # Защита от «дребезга» и флуда кнопками: повторное нажатие от того же
    # пользователя быстрее interval секунд молча гасится (callback.answer()
    # всё равно вызывается, чтобы кнопка не «висела»). Работает вместе с
    # блокировками строк в БД — это первый, дешёвый рубеж.
    def __init__(self, interval: float = 0.4):
        super().__init__()
        self.interval = interval
        self._last: dict[int, float] = {}

    async def __call__(self, handler, event: CallbackQuery, data):
        user = getattr(event, "from_user", None)
        if user is not None:
            now = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self.interval:
                try:
                    await event.answer()
                except Exception:
                    pass
                return
            self._last[user.id] = now
            if len(self._last) > 5000:
                cutoff = now - 60
                self._last = {uid: ts for uid, ts in self._last.items() if ts > cutoff}
        return await handler(event, data)


class AdminStates(StatesGroup):
    waiting_for_game_name = State()
    waiting_for_software_platform = State()
    waiting_for_software_name = State()
    waiting_for_duration = State()
    waiting_for_price = State()
    waiting_for_keys = State()
    waiting_for_software_media = State()
    waiting_for_software_files_link = State()
    waiting_for_promo_type = State()
    waiting_for_promo_code = State()
    waiting_for_promo_amount = State()
    waiting_for_promo_percent = State()
    waiting_for_promo_product = State()
    waiting_for_promo_uses = State()
    waiting_for_broadcast_audience = State()
    waiting_for_broadcast_text = State()
    waiting_for_user_id = State()
    waiting_for_user_balance_change = State()
    waiting_for_new_price = State()

class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_receipt = State()

class WithdrawalStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_card = State()

class UserStates(StatesGroup):
    waiting_for_promo_input = State()
    waiting_for_review_product = State()
    waiting_for_review_rating = State()
    waiting_for_review_text = State()

# ==========================================
# ГЛАВНОЕ МЕНЮ И СТАРТ
# ==========================================
def get_main_menu_kb(user_id: int, lang: str = "uk"):
    texts = LANGUAGES.get(lang, LANGUAGES["uk"])
    keyboard = [
        [InlineKeyboardButton(text=texts["catalog"], callback_data="shop")],
        [InlineKeyboardButton(text=texts["profile"], callback_data="profile"),
         InlineKeyboardButton(text=texts["deposit"], callback_data="deposit")],
        [InlineKeyboardButton(text=texts["promo"], callback_data="enter_promo"),
         InlineKeyboardButton(text=texts["reviews"], url=REVIEWS_CHANNEL_URL)],
        [InlineKeyboardButton(text=texts["referral"], callback_data="ref_program")],
        [InlineKeyboardButton(text=texts["help"], callback_data="help"),
         InlineKeyboardButton(text=texts["lang_btn"], callback_data="change_lang")],
        [InlineKeyboardButton(text=texts["support"], url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton(text=texts["admin"], callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_main_menu_text(balance: float, lang: str = "uk") -> str:
    texts = LANGUAGES.get(lang, LANGUAGES["uk"])
    return texts["welcome"].format(balance=balance)


async def send_main_menu(message: Message, user: "User") -> None:
    """Отправляет главное меню; при необходимости использует фирменную обложку."""
    lang = user.language if user.language in LANGUAGES else "uk"
    text_msg = get_main_menu_text(user.balance, lang)
    keyboard = get_main_menu_kb(user.id, lang)
    if WELCOME_PHOTO_FILE_ID:
        try:
            await message.answer_photo(
                WELCOME_PHOTO_FILE_ID,
                caption=clip_text(text_msg, TELEGRAM_CAPTION_LIMIT),
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return
        except TelegramBadRequest:
            logger.warning("WELCOME_PHOTO_FILE_ID недействителен; отправлено текстовое меню")
    await message.answer(text_msg, reply_markup=keyboard, parse_mode="Markdown")


def cancel_kb(target: str = "main_menu", label: str = "❌ Отмена") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=target)]])


def admin_back_kb(target: str = "admin_panel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=target)]])


async def get_product_label_map(session: AsyncSession) -> dict[int, str]:
    result = await session.execute(
        select(Product.id, Software.name, Product.duration)
        .join(Software, Product.software_id == Software.id)
        .order_by(Software.name.asc(), Product.id.asc())
    )
    return {pid: f"{sw_name} • {duration}" for pid, sw_name, duration in result.all()}


async def get_software_back_callback(session: AsyncSession, software: "Software | None") -> str:
    """Возвращает покупателя к списку софта именно выбранной игры и ОС."""
    if not software or not software.game_id:
        return "shop"
    platform = await session.get(Platform, software.platform_id)
    platform_name = (platform.name if platform else "").strip().casefold()
    if "android" in platform_name:
        return f"game_os_{software.game_id}_android"
    if "ios" in platform_name:
        return f"game_os_{software.game_id}_ios"
    # Для игры только с позициями без ОС открываем список софта напрямую;
    # если в той же игре есть Android/iOS, возвращаем в ветку «без выбора ОС».
    platform_rows = await session.execute(
        select(Platform.name)
        .join(Software, Software.platform_id == Platform.id)
        .where(Software.game_id == software.game_id)
    )
    has_mobile = any(
        "android" in (name or "").casefold() or "ios" in (name or "").casefold()
        for name in platform_rows.scalars().all()
    )
    return f"game_noos_{software.game_id}" if has_mobile else f"game_{software.game_id}"


async def resolve_user_purchased_product(session: AsyncSession, user_id: int, product_id: int):
    product = await session.get(Product, product_id)
    if not product:
        return None
    result = await session.execute(
        select(Purchase)
        .where(Purchase.user_id == user_id, Purchase.product_id == product_id)
        .order_by(Purchase.purchased_at.desc())
        .limit(1)
    )
    purchase = result.scalars().first()
    if purchase:
        return purchase

    software = await session.get(Software, product.software_id)
    if not software:
        return None
    legacy_name = f"{software.name} ({product.duration})"
    result = await session.execute(
        select(Purchase)
        .where(
            Purchase.user_id == user_id,
            Purchase.product_id.is_(None),
            Purchase.duration == product.duration,
            Purchase.product_name == legacy_name,
        )
        .order_by(Purchase.purchased_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_broadcast_user_ids(session: AsyncSession, audience: str) -> list[int]:
    if audience == "buyers":
        result = await session.execute(select(Purchase.user_id).distinct())
        return list(result.scalars().all())
    if audience == "nonbuyers":
        result = await session.execute(select(User.id).where(~exists().where(Purchase.user_id == User.id)))
        return list(result.scalars().all())
    if audience == "balance":
        result = await session.execute(select(User.id).where(User.balance > 0))
        return list(result.scalars().all())
    result = await session.execute(select(User.id))
    return list(result.scalars().all())


BROADCAST_AUDIENCES = {
    "all": "👥 Все пользователи",
    "buyers": "🛒 Только покупатели",
    "nonbuyers": "🌱 Без покупок",
    "balance": "💰 Пользователи с балансом",
}


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, session: AsyncSession, state: FSMContext):
    try:
        await state.clear()
        user = await session.get(User, message.from_user.id)
        if not user:
            referrer_id = None
            if command.args and command.args.isdigit():
                possible_ref = int(command.args)
                if possible_ref != message.from_user.id:
                    # ФИКС: проверяем, что реферер реально существует в базе,
                    # иначе FK-констрейнт уронит commit() и регистрация не пройдёт.
                    referrer_exists = await session.get(User, possible_ref)
                    if referrer_exists:
                        referrer_id = possible_ref

            user = User(
                id=message.from_user.id,
                username=message.from_user.username,
                referred_by=referrer_id,
                language="uk"
            )
            session.add(user)
            await session.commit()

            await message.answer(
                "🌐 **Будь ласка, виберіть мову / Пожалуйста, выберите язык:**",
                reply_markup=get_language_kb(is_reg=True),
                parse_mode="Markdown"
            )
            return

        else:
            if user.username != message.from_user.username:
                user.username = message.from_user.username
                await session.commit()

        await send_main_menu(message, user)
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка в cmd_start: {e}")
        await message.answer("⚠️ Произошла ошибка. Попробуйте снова.")

@router.message(Command("menu"))
async def cmd_menu(message: Message, session: AsyncSession, state: FSMContext):
    await clear_state_keep_promo(state)
    user = await session.get(User, message.from_user.id)
    if user:
        await send_main_menu(message, user)


def get_help_text(lang: str) -> str:
    if lang == "uk":
        return (
            "ℹ️ **Допомога**\n\n"
            "**Як купити товар**\n"
            "1. Відкрийте «Каталог».\n"
            "2. Оберіть гру, платформу, софт і тариф.\n"
            "3. Перевірте замовлення та підтвердьте покупку.\n\n"
            "Ключ з’явиться одразу та назавжди залишиться в розділі «Профіль» → «Мої ключі».\n\n"
            "**Оплата та промокоди**\n"
            "Поповніть баланс через головне меню. Промокод на знижку застосовується автоматично до підходящого товару.\n\n"
            f"Потрібна людина? Напишіть: @{SUPPORT_USERNAME}"
        )
    return (
        "ℹ️ **Помощь**\n\n"
        "**Как купить товар**\n"
        "1. Откройте «Каталог».\n"
        "2. Выберите игру, платформу, софт и тариф.\n"
        "3. Проверьте заказ и подтвердите покупку.\n\n"
        "Ключ появится сразу и навсегда останется в разделе «Профиль» → «Мои ключи».\n\n"
        "**Оплата и промокоды**\n"
        "Пополните баланс через главное меню. Промокод на скидку применится автоматически к подходящему товару.\n\n"
        f"Нужен живой человек? Напишите: @{SUPPORT_USERNAME}"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession):
    user = await session.get(User, message.from_user.id)
    lang = user.language if user and user.language in LANGUAGES else "ru"
    await message.answer(
        get_help_text(lang),
        reply_markup=get_main_menu_kb(message.from_user.id, lang),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "help")
async def help_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    user = await session.get(User, callback.from_user.id)
    lang = user.language if user and user.language in LANGUAGES else "ru"
    await safe_edit_text(
        callback,
        bot,
        get_help_text(lang),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"reg_lang_uk", "reg_lang_ru"}))
async def process_reg_language(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    await state.clear()
    lang_code = "uk" if callback.data == "reg_lang_uk" else "ru"
    user = await session.get(User, callback.from_user.id)
    if user:
        user.language = lang_code
        await session.commit()

    balance = user.balance if user else 0.0
    texts = LANGUAGES[lang_code]

    await callback.message.edit_text(
        get_main_menu_text(balance, lang_code),
        reply_markup=get_main_menu_kb(callback.from_user.id, lang_code),
        parse_mode="Markdown"
    )
    await callback.answer(texts["lang_changed"])

@router.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    # Не теряем уже применённую скидку при переходе через главное меню.
    await clear_state_keep_promo(state)
    user = await session.get(User, callback.from_user.id)
    balance = user.balance if user else 0.0
    lang = user.language if user and user.language in LANGUAGES else "uk"

    await safe_edit_text(
        callback, bot,
        get_main_menu_text(balance, lang),
        get_main_menu_kb(callback.from_user.id, lang),
    )
    await callback.answer()

# ==========================================
# СМЕНА ЯЗЫКА
# ==========================================
@router.callback_query(F.data == "change_lang")
async def change_lang_handler(callback: CallbackQuery, bot: Bot):
    await safe_edit_text(
        callback,
        bot,
        "🌐 **Виберіть мову / Выберите язык:**",
        get_language_kb(is_reg=False),
    )
    await callback.answer()

@router.callback_query(F.data.in_({"lang_uk", "lang_ru"}))
async def set_language(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    await clear_state_keep_promo(state)
    lang_code = "uk" if callback.data == "lang_uk" else "ru"
    user = await session.get(User, callback.from_user.id)
    if user:
        user.language = lang_code
        await session.commit()

    texts = LANGUAGES[lang_code]
    balance = user.balance if user else 0.0

    await callback.message.edit_text(
        get_main_menu_text(balance, lang_code),
        reply_markup=get_main_menu_kb(callback.from_user.id, lang_code),
        parse_mode="Markdown"
    )
    await callback.answer(texts["lang_changed"])

# ==========================================
# ПРОМОКОДЫ (ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ)
# ==========================================
@router.callback_query(F.data == "enter_promo")
async def enter_promo_handler(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(UserStates.waiting_for_promo_input)
    await safe_edit_text(
        callback,
        bot,
        "🎟 **Активация промокода**\n\nВведите ваш промокод:",
        cancel_kb("main_menu"),
    )
    await callback.answer()

@router.message(UserStates.waiting_for_promo_input)
async def process_promo_activation(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Отправьте промокод текстом.", reply_markup=cancel_kb("main_menu"))
        return

    code_text = normalize_promo_code(message.text)
    if not code_text:
        await message.answer(
            f"❌ Некорректный промокод. Используйте латинские буквы, цифры, `-` и `_`, максимум {MAX_PROMO_CODE_LENGTH} символов.",
            reply_markup=cancel_kb("main_menu"),
            parse_mode="Markdown"
        )
        return

    promo = await session.scalar(
        select(PromoCode)
        .where(func.upper(PromoCode.code) == code_text)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if not promo or promo.uses_left <= 0:
        await message.answer("❌ Промокод не найден или его лимит активаций исчерпан!", reply_markup=cancel_kb("main_menu"))
        await state.clear()
        return

    # Один пользователь может применить конкретный промокод только один раз.
    already_used = await session.scalar(
        select(exists().where(
            PromoCodeUsage.promo_id == promo.id,
            PromoCodeUsage.user_id == message.from_user.id
        ))
    )
    if already_used:
        await message.answer("❌ Вы уже использовали этот промокод ранее!", reply_markup=cancel_kb("main_menu"))
        await state.clear()
        return

    if promo.promo_type == "discount":
        percent = float(promo.discount_percent or 0)
        if percent <= 0 or percent > 100:
            await message.answer("❌ У этого промокода некорректная скидка. Обратитесь к администратору.")
            await state.clear()
            return

        await state.update_data(active_discount_promo_id=promo.id)

        if promo.product_id is None:
            target_text = "на любой товар"
        else:
            product = await session.get(Product, promo.product_id)
            if not product:
                await message.answer("❌ Товар, к которому привязан промокод, больше не существует.", reply_markup=cancel_kb("main_menu"))
                await state.update_data(active_discount_promo_id=None)
                await state.clear()
                return
            software = await session.get(Software, product.software_id)
            target_text = f"на {software.name if software else 'товар'} • {product.duration}"

        await state.clear()
        await state.update_data(active_discount_promo_id=promo.id)
        await message.answer(
            f"✅ Промокод `{escape_md(promo.code)}` применён!\n\n"
            f"💸 Скидка: **{percent:.0f}%**\n"
            f"📦 Действует: **{escape_md(target_text)}**\n\n"
            "🛒 Перейдите в каталог и выберите подходящий товар. Скидка будет показана перед оплатой.",
            reply_markup=get_main_menu_kb(
                message.from_user.id,
                (await session.get(User, message.from_user.id)).language,
            ),
            parse_mode="Markdown"
        )
        return

    # Старые промокоды-бонусы продолжают работать как раньше.
    user_res = await session.execute(
        select(User).where(User.id == message.from_user.id).with_for_update().execution_options(populate_existing=True)
    )
    user = user_res.scalar_one_or_none()
    if not user:
        await state.clear()
        return

    user.balance = round_money(user.balance + float(promo.amount))
    promo.uses_left -= 1
    session.add(PromoCodeUsage(
        promo_id=promo.id,
        user_id=user.id,
        discount_amount=0.0,
        final_price=0.0,
    ))
    log_balance_change(session, user.id, float(promo.amount), "promo_code", user.balance, related_id=promo.id)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await message.answer("❌ Вы уже использовали этот промокод ранее!", reply_markup=cancel_kb("main_menu"))
        await state.clear()
        return

    lang = user.language if user.language in LANGUAGES else "ru"
    await state.clear()
    await message.answer(
        f"🎉 **Промокод успешно активирован!**\nВам начислено `{float(promo.amount):.2f} грн` на баланс.",
        reply_markup=get_main_menu_kb(message.from_user.id, lang),
        parse_mode="Markdown"
    )

# ==========================================
# ⭐ НОРМАЛЬНАЯ СИСТЕМА ОТЗЫВОВ
# ==========================================
# Отзыв можно оставить только по реально купленному тарифу.
# Один пользователь может оставить один отзыв на один тариф.


def review_stars(rating: int) -> str:
    rating = max(1, min(5, int(rating)))
    return "⭐" * rating + "☆" * (5 - rating)


@router.callback_query(F.data == "show_reviews")
@router.callback_query(F.data.startswith("reviews_page_"))
async def show_reviews_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    """Публичный раздел отзывов ведёт в официальный Telegram-канал."""
    text_msg = (
        "⭐ **KRANIN SHOP — отзывы покупателей**\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Здесь публикуются реальные отзывы клиентов о покупках.\n"
        "Нажмите кнопку ниже, чтобы открыть канал с отзывами."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Открыть канал отзывов", url=REVIEWS_CHANNEL_URL)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await safe_edit_text(callback, bot, text_msg, keyboard)
    await callback.answer()


@router.callback_query(F.data == "leave_review")
async def leave_review_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    product_map = await get_product_label_map(session)
    result = await session.execute(
        select(Purchase).where(Purchase.user_id == callback.from_user.id).order_by(Purchase.purchased_at.desc())
    )
    purchases = result.scalars().all()
    reviewed_product_ids = set((await session.execute(
        select(Review.product_id).where(Review.user_id == callback.from_user.id)
    )).scalars().all())
    seen = set()
    available = []
    for purchase in purchases:
        product_id = purchase.product_id
        if not product_id:
            for pid, label in product_map.items():
                sw_name, duration = label.rsplit(" • ", 1)
                if purchase.product_name == f"{sw_name} ({duration})" and purchase.duration == duration:
                    product_id = pid
                    break
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        if product_id not in reviewed_product_ids:
            available.append((product_id, product_map.get(product_id, purchase.product_name)))

    if not available:
        await callback.answer("✅ По всем вашим покупкам отзыв уже оставлен.", show_alert=True)
        return
    await state.set_state(UserStates.waiting_for_review_product)
    buttons = [[InlineKeyboardButton(text=f"📦 {label}", callback_data=f"review_product_{pid}")] for pid, label in available[:20]]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="show_reviews")])
    await callback.message.edit_text(
        "✍️ **Новый отзыв**\n\nВыберите товар, который вы покупали:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("review_product_"))
async def process_review_product(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    try:
        product_id = int(callback.data.split("_")[2])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректный товар.", show_alert=True)
        return
    purchase = await resolve_user_purchased_product(session, callback.from_user.id, product_id)
    if not purchase:
        await callback.answer("❌ Этот товар не найден среди ваших покупок.", show_alert=True)
        return
    already = await session.scalar(
        select(func.count(Review.id)).where(Review.user_id == callback.from_user.id, Review.product_id == product_id)
    ) or 0
    if already:
        await callback.answer("✅ Отзыв по этому товару уже существует.", show_alert=True)
        await state.clear()
        return
    label = (await get_product_label_map(session)).get(product_id, "Товар")
    await state.update_data(product_id=product_id, purchase_id=purchase.id)
    await state.set_state(UserStates.waiting_for_review_rating)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐⭐⭐⭐⭐ 5/5", callback_data="review_stars_5")],
        [InlineKeyboardButton(text="⭐⭐⭐⭐ 4/5", callback_data="review_stars_4")],
        [InlineKeyboardButton(text="⭐⭐⭐ 3/5", callback_data="review_stars_3")],
        [InlineKeyboardButton(text="⭐⭐ 2/5", callback_data="review_stars_2")],
        [InlineKeyboardButton(text="⭐ 1/5", callback_data="review_stars_1")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="show_reviews")],
    ])
    await callback.message.edit_text(
        f"✍️ **Отзыв о товаре**\n\n📦 `{escape_md(label)}`\n\nВыберите оценку:",
        reply_markup=keyboard, parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("review_stars_"))
async def process_review_rating(callback: CallbackQuery, state: FSMContext):
    try:
        rating = int(callback.data.split("_")[2])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректная оценка.", show_alert=True)
        return
    if not 1 <= rating <= 5:
        await callback.answer("❌ Оценка должна быть от 1 до 5.", show_alert=True)
        return
    data = await state.get_data()
    if not data.get("product_id"):
        await state.clear()
        await callback.answer("❌ Сессия отзыва устарела. Начните заново.", show_alert=True)
        return
    await state.update_data(rating=rating)
    await state.set_state(UserStates.waiting_for_review_text)
    await callback.message.edit_text(
        f"📝 **Ваша оценка: {review_stars(rating)}**\n\nНапишите отзыв одним сообщением.\nМинимум 5, максимум {MAX_REVIEW_LENGTH} символов.",
        reply_markup=cancel_kb("show_reviews"), parse_mode="Markdown"
    )
    await callback.answer()


@router.message(UserStates.waiting_for_review_text)
async def save_review_handler(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not message.text or not message.text.strip():
        await message.answer("❌ Отправьте текст отзыва.", reply_markup=cancel_kb("show_reviews"))
        return
    text_val = message.text.strip()
    if len(text_val) < 5:
        await message.answer("❌ Отзыв слишком короткий — минимум 5 символов.")
        return
    if len(text_val) > MAX_REVIEW_LENGTH:
        await message.answer(f"❌ Максимум {MAX_REVIEW_LENGTH} символов.")
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await state.clear()
        await message.answer("⚠️ Сессия устарела. Начните отзыв заново.")
        return
    purchase = await resolve_user_purchased_product(session, message.from_user.id, int(product_id))
    if not purchase:
        await state.clear()
        await message.answer("❌ Не удалось подтвердить покупку этого товара.")
        return
    existing = await session.scalar(
        select(func.count(Review.id)).where(Review.user_id == message.from_user.id, Review.product_id == int(product_id))
    ) or 0
    if existing:
        await state.clear()
        await message.answer("✅ Отзыв по этому товару уже существует.")
        return
    review = Review(
        user_id=message.from_user.id,
        product_id=int(product_id),
        purchase_id=purchase.id,
        rating=int(data.get("rating", 5)),
        text=text_val,
        status="pending",
    )
    session.add(review)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await state.clear()
        await message.answer("✅ Такой отзыв уже был создан.")
        return
    await state.clear()

    product_label = (await get_product_label_map(session)).get(int(product_id), "Товар")
    author = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else (message.from_user.full_name or "Покупатель")
    )
    # Первое сообщение — готовый публичный пост без личных данных:
    # администратор может переслать его в канал целиком, сохранив форматирование.
    admin_msg = (
        "<b>⭐ ОТЗЫВ ПОКУПАТЕЛЯ • KRANIN SHOP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Товар:</b> {html.escape(product_label)}\n"
        f"⭐ <b>Оценка:</b> {review_stars(review.rating)} ({review.rating}/5)\n"
        "✅ <b>Покупка подтверждена</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>💬 ОТЗЫВ</b>\n"
        f"<i>{html.escape(text_val)}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>💎 KRANIN SHOP</b>"
    )
    # Служебные сведения идут отдельным сообщением и не попадут в канал,
    # когда администратор пересылает готовый пост.
    admin_details_msg = (
        "🔒 СЛУЖЕБНАЯ ИНФОРМАЦИЯ — НЕ ПЕРЕСЫЛАТЬ\n"
        f"👤 Покупатель: {author}\n"
        f"🆔 Telegram ID: {message.from_user.id}\n"
        f"🧾 Заказ: #{purchase.id} · Отзыв: #{review.id}\n"
        f"📅 Дата: {format_dt(review.created_at)}\n"
        f"↗️ Готовый пост выше можно переслать в {REVIEWS_CHANNEL_URL}"
    )
    delivered_to_admin = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_msg, parse_mode="HTML")
            delivered_to_admin += 1
            try:
                await bot.send_message(admin_id, admin_details_msg, parse_mode=None)
            except Exception as details_error:
                logger.error(f"Не удалось отправить служебные данные об отзыве админу {admin_id}: {details_error}")
        except Exception as e:
            logger.error(f"Не удалось уведомить админа о новом отзыве: {e}")

    if delivered_to_admin:
        confirmation = (
            "✅ **Спасибо за отзыв!**\n\n"
            "Ваш отзыв сохранён и отправлен администрации KRANIN SHOP.\n"
            "После публикации он появится в нашем Telegram-канале."
        )
    else:
        confirmation = (
            "⚠️ **Отзыв сохранён, но уведомление администрации не доставлено.**\n\n"
            "Пожалуйста, напишите в поддержку, чтобы отзыв не потерялся."
        )
    await message.answer(
        confirmation,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎰 Крутить колесо фортуны", callback_data=f"wheelreview_{review.id}")],
            [InlineKeyboardButton(text="📢 Канал отзывов", url=REVIEWS_CHANNEL_URL)],
            [InlineKeyboardButton(text="📋 Мои отзывы", callback_data="my_reviews")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ]),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "my_reviews")
@router.callback_query(F.data.regexp(r"^my_reviews_page_[0-9]+$"))
async def my_reviews_handler(callback: CallbackQuery, session: AsyncSession):
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data.startswith("my_reviews_page_") else 0
    total = await session.scalar(
        select(func.count(Review.id)).where(Review.user_id == callback.from_user.id)
    ) or 0
    page, total_pages, offset = page_bounds(total, page, HISTORY_PAGE_SIZE)
    result = await session.execute(
        select(Review, Software.name, Product.duration)
        .join(Product, Review.product_id == Product.id, isouter=True)
        .join(Software, Product.software_id == Software.id, isouter=True)
        .where(Review.user_id == callback.from_user.id)
        .order_by(Review.created_at.desc())
        .offset(offset)
        .limit(HISTORY_PAGE_SIZE)
    )
    rows = result.all()
    if not rows:
        text_msg = "📋 **Мои отзывы**\n\nВы ещё не оставляли отзывов."
    else:
        labels = {"pending": "🕒 На модерации", "approved": "✅ Проверен администрацией", "hidden": "🙈 Скрыт"}
        blocks = []
        for review, sw_name, duration in rows:
            product_label = f"{sw_name} • {duration}" if sw_name else "Товар"
            blocks.append(
                f"{review_stars(review.rating)} · `{escape_md(product_label)}`\n"
                f"_{escape_md(review.text)}_\n"
                f"{labels.get(review.status, review.status)} · {format_dt(review.created_at, False)}"
            )
        text_msg = "📋 **Мои отзывы**\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n──────────────\n\n".join(blocks)
    buttons = []
    nav = pagination_row("my_reviews_page_", page, total_pages)
    if nav:
        buttons.append(nav)
    buttons.extend([
        [InlineKeyboardButton(text="✍️ Новый отзыв", callback_data="leave_review")],
        [InlineKeyboardButton(text="⭐ Все отзывы", url=REVIEWS_CHANNEL_URL)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await callback.message.edit_text(clip_text(text_msg), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


# ==========================================
# 👤 ПРОФИЛЬ И ЛИЧНЫЙ КАБИНЕТ
# ==========================================
@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    await clear_state_keep_promo(state)
    user_id = callback.from_user.id
    user = await session.get(User, user_id)
    if not user:
        await callback.answer("❌ Пользователь не найден.", show_alert=True)
        return
    total_purchases = await session.scalar(select(func.count(Purchase.id)).where(Purchase.user_id == user_id)) or 0
    total_spent = await session.scalar(select(func.coalesce(func.sum(Purchase.price_paid), 0)).where(Purchase.user_id == user_id)) or 0
    referrals = await session.scalar(select(func.count(User.id)).where(User.referred_by == user_id)) or 0
    referral_bonus = await session.scalar(select(func.coalesce(func.sum(BalanceLog.amount), 0)).where(
        BalanceLog.user_id == user_id, BalanceLog.reason == "referral_bonus"
    )) or 0
    review_count = await session.scalar(select(func.count(Review.id)).where(Review.user_id == user_id)) or 0
    username = f"@{escape_md(user.username)}" if user.username else "не указан"
    lang_name = "🇺🇦 Українська" if user.language == "uk" else "🇷🇺 Русский"
    text_msg = (
        "👤 **МОЙ ПРОФИЛЬ**\n"
        f"{username} · `{user_id}`\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"💰 **Баланс:** `{user.balance:.2f} грн`\n"
        f"🛒 **Покупки:** `{total_purchases}` · на `{float(total_spent):.2f} грн`\n"
        f"🤝 **Рефералы:** `{referrals}` · бонусы `{float(referral_bonus):.2f} грн`\n"
        f"⭐ **Отзывы:** `{review_count}`\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 {lang_name}\n"
        f"📅 С нами с `{format_dt(user.created_at, False)}`"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="profile_keys")],
        [InlineKeyboardButton(text="🧾 Покупки", callback_data="profile_purchase_history"),
         InlineKeyboardButton(text="💳 Операции", callback_data="profile_balance_history")],
        [InlineKeyboardButton(text="⭐ Мои отзывы", callback_data="my_reviews"),
         InlineKeyboardButton(text="🤝 Рефералы", callback_data="ref_program")],
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="deposit")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await safe_edit_text(callback, bot, text_msg, keyboard)
    await callback.answer()


@router.callback_query(F.data == "profile_keys")
async def profile_keys_menu(callback: CallbackQuery, session: AsyncSession):
    user_id = callback.from_user.id
    result = await session.execute(
        select(Purchase.duration, func.count(Purchase.id))
        .where(Purchase.user_id == user_id)
        .group_by(Purchase.duration)
    )
    duration_counts = {duration: count for duration, count in result.all() if duration}
    durations = list(duration_counts)
    ordered = [d for d in DURATIONS if d in durations]
    ordered += [d for d in durations if d not in ordered]
    if not ordered:
        text_msg = "🔑 **Мои ключи**\n\nПокупок пока нет."
        buttons = [[InlineKeyboardButton(text="🛒 Каталог", callback_data="shop")]]
    else:
        text_msg = "🔑 **Мои ключи**\n━━━━━━━━━━━━━━━━━━━\nВыберите срок:"
        buttons = []
        for duration in ordered:
            count = duration_counts.get(duration, 0)
            buttons.append([InlineKeyboardButton(
                text=f"📂 {duration} · {count} шт.", callback_data=f"profile_durv_{duration.replace(' ', '_')}"
            )])
    buttons += [
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ]
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("profile_durv_"))
async def show_profile_keys(callback: CallbackQuery, session: AsyncSession):
    payload = callback.data[len("profile_durv_"):]
    duration_payload, separator, raw_page = payload.rpartition("_page_")
    if separator and raw_page.isdigit():
        page = int(raw_page)
    else:
        duration_payload = payload
        page = 0
    duration = duration_payload.replace("_", " ")
    total = await session.scalar(
        select(func.count(Purchase.id)).where(
            Purchase.user_id == callback.from_user.id,
            Purchase.duration == duration,
        )
    ) or 0
    page, total_pages, offset = page_bounds(total, page, HISTORY_PAGE_SIZE)
    result = await session.execute(
        select(Purchase)
        .where(Purchase.user_id == callback.from_user.id, Purchase.duration == duration)
        .order_by(Purchase.purchased_at.desc())
        .offset(offset)
        .limit(HISTORY_PAGE_SIZE)
    )
    purchases = result.scalars().all()
    if not purchases:
        text_msg = "🔑 Записей не найдено."
    else:
        blocks = [
            f"📦 **{escape_md(p.product_name)}**\n🔐 `{escape_md(p.key_issued)}`\n🕒 {format_dt(p.purchased_at)}"
            for p in purchases
        ]
        text_msg = f"🔑 **Мои ключи · {escape_md(duration)}**\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n──────────────\n\n".join(blocks)
    buttons = []
    nav = pagination_row(f"profile_durv_{duration_payload}_page_", page, total_pages)
    if nav:
        buttons.append(nav)
    buttons.extend([
        [InlineKeyboardButton(text="🔙 К категориям", callback_data="profile_keys")],
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")],
    ])
    await callback.message.edit_text(clip_text(text_msg), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "profile_purchase_history")
@router.callback_query(F.data.regexp(r"^profile_purchases_page_[0-9]+$"))
async def profile_purchase_history(callback: CallbackQuery, session: AsyncSession):
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data.startswith("profile_purchases_page_") else 0
    total = await session.scalar(
        select(func.count(Purchase.id)).where(Purchase.user_id == callback.from_user.id)
    ) or 0
    page, total_pages, offset = page_bounds(total, page, HISTORY_PAGE_SIZE)
    result = await session.execute(
        select(Purchase)
        .where(Purchase.user_id == callback.from_user.id)
        .order_by(Purchase.purchased_at.desc())
        .offset(offset)
        .limit(HISTORY_PAGE_SIZE)
    )
    purchases = result.scalars().all()
    if not purchases:
        text_msg = "🧾 **История покупок**\n\nПокупок пока нет."
    else:
        blocks = [
            f"🛒 **{escape_md(p.product_name)}**\n💰 `{p.price_paid:.2f} грн`\n🔑 `{escape_md(p.key_issued)}`\n🕒 {format_dt(p.purchased_at)}"
            for p in purchases
        ]
        text_msg = "🧾 **История покупок**\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n──────────────\n\n".join(blocks)
    buttons = []
    nav = pagination_row("profile_purchases_page_", page, total_pages)
    if nav:
        buttons.append(nav)
    buttons.extend([
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await callback.message.edit_text(clip_text(text_msg), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "profile_balance_history")
@router.callback_query(F.data.regexp(r"^profile_balance_page_[0-9]+$"))
async def profile_balance_history(callback: CallbackQuery, session: AsyncSession):
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data.startswith("profile_balance_page_") else 0
    total = await session.scalar(
        select(func.count(BalanceLog.id)).where(BalanceLog.user_id == callback.from_user.id)
    ) or 0
    page, total_pages, offset = page_bounds(total, page, HISTORY_PAGE_SIZE)
    result = await session.execute(
        select(BalanceLog)
        .where(BalanceLog.user_id == callback.from_user.id)
        .order_by(BalanceLog.created_at.desc())
        .offset(offset)
        .limit(HISTORY_PAGE_SIZE)
    )
    logs = result.scalars().all()
    labels = {
        "purchase": "🛒 Покупка", "deposit_approved": "📥 Пополнение", "withdrawal_request": "📤 Вывод",
        "withdrawal_rejected_refund": "↩️ Возврат", "referral_bonus": "🤝 Реферальный бонус",
        "promo_code": "🎟 Промокод", "wheel_bonus": "🎰 Приз", "admin_adjust": "🛠 Корректировка",
    }
    if not logs:
        text_msg = "💳 **История баланса**\n\nОпераций пока нет."
    else:
        blocks = []
        for log in logs:
            sign = "+" if log.amount >= 0 else ""
            blocks.append(
                f"{labels.get(log.reason, log.reason)}\n"
                f"💵 `{sign}{log.amount:.2f} грн` → `{log.balance_after:.2f} грн`\n"
                f"🕒 {format_dt(log.created_at)}"
            )
        text_msg = "💳 **История баланса**\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n──────────────\n\n".join(blocks)
    buttons = []
    nav = pagination_row("profile_balance_page_", page, total_pages)
    if nav:
        buttons.append(nav)
    buttons.extend([
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await callback.message.edit_text(clip_text(text_msg), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


# ==========================================
# КАТАЛОГ И ПОКУПКИ С УВЕДОМЛЕНИЯМИ (RESTOCK)
# ==========================================
async def _edit_catalog_screen(callback: CallbackQuery, bot: Bot, text_msg: str, keyboard: InlineKeyboardMarkup) -> None:
    text_msg = clip_text(text_msg)
    try:
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        try:
            await callback.message.delete()
        except Exception:
            pass
        await bot.send_message(
            callback.message.chat.id,
            text_msg,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )


@router.callback_query(F.data == "shop")
@router.callback_query(F.data.regexp(r"^shop_page_[0-9]+$"))
async def show_platforms(callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    """Шаг 1: paginated каталог игр; активная скидка сохраняется."""
    await clear_state_keep_promo(state)
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data.startswith("shop_page_") else 0
    result = await session.execute(
        select(Game, func.count(Software.id))
        .join(Software, Software.game_id == Game.id)
        .group_by(Game.id)
        .order_by(Game.name.asc(), Game.id.asc())
    )
    rows = result.all()
    page, total_pages, offset = page_bounds(len(rows), page, CATALOG_PAGE_SIZE)
    page_rows = rows[offset:offset + CATALOG_PAGE_SIZE]
    text_msg = (
        "🛒 **КАТАЛОГ**\n"
        "Выберите игру или категорию\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "**Шаг 1 из 4 · Игра**"
    )
    buttons = [
        [InlineKeyboardButton(text=f"🎮 {game.name[:46]} · {count}", callback_data=f"game_{game.id}")]
        for game, count in page_rows
    ]
    if not buttons:
        text_msg += "\n\nКаталог пока пуст. Загляните позже."
    nav = pagination_row("shop_page_", page, total_pages)
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    await _edit_catalog_screen(callback, bot, text_msg, InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^game_[0-9]+$"))
async def show_game_platforms(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    """Шаг 2: показать ОС выбранной игры; если ОС не нужна — сразу показать софт."""
    try:
        game_id = int(callback.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректная игра.", show_alert=True)
        return

    game = await session.get(Game, game_id)
    if not game:
        await callback.answer("❌ Игра не найдена.", show_alert=True)
        return

    result = await session.execute(
        select(Platform.name)
        .join(Software, Software.platform_id == Platform.id)
        .where(Software.game_id == game_id)
    )
    platform_names = [name or "" for name in result.scalars().all()]
    if not platform_names:
        await callback.answer("В этой игре пока нет добавленного софта.", show_alert=True)
        return

    kinds = set()
    for name in platform_names:
        normalized = name.strip().casefold()
        if "android" in normalized:
            kinds.add("android")
        elif "ios" in normalized:
            kinds.add("ios")
        else:
            kinds.add("noos")

    # Если админ везде поставил «Пропустить», выбор ОС реально пропускается.
    if "android" not in kinds and "ios" not in kinds:
        await _show_game_software_list(callback, session, bot, game_id, "noos", back_callback="shop")
        await callback.answer()
        return

    buttons = []
    if "android" in kinds:
        buttons.append([InlineKeyboardButton(text="🤖 Android", callback_data=f"game_os_{game_id}_android")])
    if "ios" in kinds:
        buttons.append([InlineKeyboardButton(text="🍏 iOS", callback_data=f"game_os_{game_id}_ios")])
    # При смешанных настройках даём отдельный вход в позиции, для которых ОС пропущена.
    if "noos" in kinds:
        buttons.append([InlineKeyboardButton(text="⏭ Софт без выбора ОС", callback_data=f"game_noos_{game_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 К списку игр", callback_data="shop")])
    await _edit_catalog_screen(
        callback,
        bot,
        f"🎮 **{escape_md(game.name)}**\n━━━━━━━━━━━━━━━━━━━\n📱 **Шаг 2/4 — выберите ОС:**",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


async def _show_game_software_list(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    game_id: int,
    os_kind: str,
    back_callback: str,
    page: int = 0,
) -> None:
    game = await session.get(Game, game_id)
    if not game:
        await callback.answer("❌ Игра не найдена.", show_alert=True)
        return

    query = (
        select(Software, Platform)
        .join(Platform, Software.platform_id == Platform.id)
        .where(Software.game_id == game_id)
    )
    platform_lower = func.lower(Platform.name)
    if os_kind == "android":
        query = query.where(platform_lower.contains("android"))
        os_title = "🤖 Android"
    elif os_kind == "ios":
        query = query.where(platform_lower.contains("ios"))
        os_title = "🍏 iOS"
    else:
        query = query.where(~platform_lower.contains("android"), ~platform_lower.contains("ios"))
        os_title = "⏭ Без выбора ОС"

    query = query.order_by(Software.name.asc(), Software.id.asc())
    result = await session.execute(query)
    rows = result.all()
    page, total_pages, offset = page_bounds(len(rows), page, CATALOG_PAGE_SIZE)
    page_rows = rows[offset:offset + CATALOG_PAGE_SIZE]
    root_flag = 1 if back_callback == "shop" else 0
    buttons = [
        [InlineKeyboardButton(text=f"🛡 {software.name[:56]}", callback_data=f"software_{software.id}")]
        for software, _platform in page_rows
    ]
    if not rows:
        text_msg = f"🎮 **{escape_md(game.name)}**\n\nДля выбранного раздела софт пока не добавлен."
    else:
        text_msg = (
            f"🎮 **{escape_md(game.name)}**\n"
            f"📱 {os_title}\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "**Шаг 3 из 4 · Софт**"
        )
    nav = pagination_row(f"gsw_{game_id}_{os_kind}_{root_flag}_", page, total_pages)
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback)])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    await _edit_catalog_screen(callback, bot, text_msg, InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.regexp(r"^game_os_[0-9]+_(android|ios)$"))
async def show_game_software_for_os(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    parts = callback.data.split("_")
    game_id = int(parts[2])
    os_kind = parts[3]
    await _show_game_software_list(
        callback, session, bot, game_id, os_kind, back_callback=f"game_{game_id}"
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^game_noos_[0-9]+$"))
async def show_game_software_without_os(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    game_id = int(callback.data.rsplit("_", 1)[1])
    # Это действие появляется только в смешанном каталоге; «Назад» вернёт к выбору ОС.
    await _show_game_software_list(
        callback, session, bot, game_id, "noos", back_callback=f"game_{game_id}"
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^gsw_[0-9]+_(android|ios|noos)_[01]_[0-9]+$"))
async def show_game_software_page(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    _prefix, game_id, os_kind, root_flag, page = callback.data.split("_")
    root_flag = int(root_flag)
    back_callback = "shop" if root_flag else f"game_{game_id}"
    await _show_game_software_list(
        callback,
        session,
        bot,
        int(game_id),
        os_kind,
        back_callback,
        int(page),
    )
    await callback.answer()


# Старый callback платформ сохранён для уже открытых сообщений и старых кнопок.
@router.callback_query(F.data.startswith("platform_"))
async def show_softwares(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    try:
        platform_id = int(callback.data.split("_")[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректная платформа.", show_alert=True)
        return
    platform = await session.get(Platform, platform_id)
    result = await session.execute(select(Software).filter_by(platform_id=platform_id).order_by(Software.name.asc()))
    softwares = result.scalars().all()
    text_msg = f"🛍 **{escape_md(platform.name) if platform else 'Платформа'}**\n━━━━━━━━━━━━━━━━━━━\n⚡️ Выберите нужный софт:"
    if not softwares:
        text_msg += "\n\n❌ На данную платформу софт пока не добавлен."
    buttons = [[InlineKeyboardButton(text=f"🛡 {sw.name[:58]}", callback_data=f"software_{sw.id}")] for sw in softwares]
    buttons.append([InlineKeyboardButton(text="🔙 К каталогу", callback_data="shop")])
    await _edit_catalog_screen(callback, bot, text_msg, InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


async def _render_software_screen(
    callback: CallbackQuery, bot: Bot, software: "Software", text_msg: str, keyboard: InlineKeyboardMarkup
) -> None:
    """Рендерит карточку товара и не роняет сценарий при нерабочем file_id."""
    chat_id = callback.message.chat.id
    if software.photo_file_id:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=software.photo_file_id,
                caption=clip_text(text_msg, TELEGRAM_CAPTION_LIMIT - 16),
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            with suppress(Exception):
                await callback.message.delete()
            return
        except TelegramBadRequest as exc:
            logger.warning("Не удалось показать фото софта %s: %s", software.id, exc)

    text_msg = clip_text(text_msg)
    try:
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        with suppress(Exception):
            await callback.message.delete()
        await bot.send_message(chat_id, text_msg, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data.startswith("software_"))
async def show_products(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    try:
        software_id = int(callback.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректный товар.", show_alert=True)
        return
    await _show_products_for_software_id(callback, session, bot, software_id)


async def _show_products_for_software_id(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, software_id: int
):
    software = await session.get(Software, software_id)
    if not software:
        await callback.answer("❌ Софт не найден!", show_alert=True)
        return

    back_callback = await get_software_back_callback(session, software)

    result = await session.execute(
        select(Product).filter_by(software_id=software_id).order_by(Product.id.asc())
    )
    products = sorted(
        result.scalars().all(),
        key=lambda item: (DURATION_SORT_ORDER.get(item.duration, len(DURATIONS)), item.price, item.id),
    )

    if software.description:
        # У фото Telegram лимит подписи 1024 символа — оставляем место для тарифов.
        description_limit = 560 if software.photo_file_id else 1500
        description = software.description
        if len(description) > description_limit:
            description = description[:description_limit - 1].rstrip() + "…"
        description_block = f"{escape_md(description)}\n\n"
    else:
        description_block = ""

    if not products:
        text_msg = f"🛒 **{escape_md(software.name)}**\n━━━━━━━━━━━━━━━━━━━\n{description_block}❌ Тарифы для этого софта пока отсутствуют."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К списку софта", callback_data=back_callback)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ])
        await _render_software_screen(callback, bot, software, text_msg, keyboard)
        await callback.answer()
        return

    text_msg = f"🛡 **{escape_md(software.name)}**\n━━━━━━━━━━━━━━━━━━━\n{description_block}**Шаг 4 из 4 · Тариф**\nВыберите срок лицензии:"
    # Остатки всех тарифов одним запросом вместо N+1
    stock_res = await session.execute(
        select(LicenseKey.product_id, func.count(LicenseKey.id))
        .where(LicenseKey.product_id.in_([p.id for p in products]), LicenseKey.is_sold == False)
        .group_by(LicenseKey.product_id)
    )
    stock_map = {pid: cnt for pid, cnt in stock_res.all()}

    buttons = []
    for p in products:
        available_keys = stock_map.get(p.id, 0)

        if available_keys > 0:
            buttons.append([InlineKeyboardButton(
                text=f"🟢 {p.duration} · {p.price:.2f} грн · {available_keys} шт.",
                callback_data=f"confirm_buy_{p.id}"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text=f"🔔 {p.duration} · нет в наличии",
                callback_data=f"notify_restock_{p.id}"
            )])

    buttons.append([InlineKeyboardButton(text="🔙 К списку софта", callback_data=back_callback)])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await _render_software_screen(callback, bot, software, text_msg, keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("notify_restock_"))
async def notify_restock_handler(callback: CallbackQuery, session: AsyncSession):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    product = await session.get(Product, product_id)
    if not product:
        await callback.answer("❌ Товар больше не существует.", show_alert=True)
        return

    existing = await session.scalar(
        select(RestockSubscription).filter_by(user_id=user_id, product_id=product_id)
    )
    if existing:
        await callback.answer("🔔 Уведомление уже включено.", show_alert=True)
        return

    session.add(RestockSubscription(user_id=user_id, product_id=product_id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
    await callback.answer("✅ Сообщим, как только товар появится.", show_alert=True)

# --- НОВОЕ: ШАГ ПОДТВЕРЖДЕНИЯ ПЕРЕД ПОКУПКОЙ ---
@router.callback_query(F.data.startswith("confirm_buy_"))
async def confirm_purchase_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    product = await session.get(Product, product_id)
    if not product:
        await callback.answer("❌ Товар не найден!", show_alert=True)
        return

    software = await session.get(Software, product.software_id)
    sw_name = software.name if software else "Товар"

    user = await session.get(User, user_id)
    balance = user.balance if user else 0.0

    avail_res = await session.execute(
        select(func.count(LicenseKey.id)).filter_by(product_id=product.id, is_sold=False)
    )
    available = avail_res.scalar() or 0
    if available <= 0:
        await callback.answer("❌ К сожалению, ключи только что закончились!", show_alert=True)
        return

    final_price = round_money(product.price)
    discount_amount = 0.0
    promo = None
    promo_data = await state.get_data()
    active_promo_id = promo_data.get("active_discount_promo_id")

    if active_promo_id:
        promo = await session.get(PromoCode, active_promo_id)
        if not promo or promo.uses_left <= 0 or promo.promo_type != "discount":
            await state.update_data(active_discount_promo_id=None)
            promo = None
        elif promo.product_id is not None and promo.product_id != product.id:
            target = await session.get(Product, promo.product_id)
            target_name = "другой товар"
            if target:
                target_sw = await session.get(Software, target.software_id)
                target_name = f"{target_sw.name if target_sw else 'товар'} • {target.duration}"
            await callback.answer(f"❌ Эта скидка действует только на: {target_name}", show_alert=True)
            return
        else:
            percent = max(0.0, min(100.0, float(promo.discount_percent or 0.0)))
            discount_amount = round_money(float(product.price) * percent / 100.0)
            final_price = max(0.0, round_money(float(product.price) - discount_amount))

    if balance < final_price:
        await callback.answer(
            f"❌ Недостаточно средств! Требуется {final_price:.2f} грн, у вас {balance:.2f} грн",
            show_alert=True
        )
        return

    price_block = f"💰 **Цена:** `{product.price:.2f} грн`"
    if promo and discount_amount > 0:
        price_block += (
            f"\n🎟 **Промокод:** `{escape_md(promo.code)}`"
            f"\n💸 **Скидка:** `-{discount_amount:.2f} грн` ({float(promo.discount_percent):.0f}%)"
            f"\n✅ **К оплате:** `{final_price:.2f} грн`"
        )
    else:
        price_block += f"\n✅ **К оплате:** `{final_price:.2f} грн`"

    text_msg = (
        f"🧾 **ВАШ ЗАКАЗ**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **{escape_md(sw_name)}**\n"
        f"⏳ Тариф: `{escape_md(product.duration)}`\n"
        f"{price_block}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"💳 Сейчас на балансе: `{balance:.2f} грн`\n"
        f"✨ После покупки: `{(balance - final_price):.2f} грн`\n\n"
        f"После подтверждения ключ будет выдан сразу."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить покупку", callback_data=f"buy_product_{product.id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"software_{product.software_id}")],
    ])
    await safe_edit_text(callback, bot, text_msg, keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("buy_product_"))
async def process_purchase(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    product = await lock_row(session, Product, product_id)
    if not product:
        await callback.answer("❌ Товар не найден!", show_alert=True)
        return

    software = await session.get(Software, product.software_id)

    # Блокировки пользователя, товара и ключа исключают двойное списание,
    # продажу одного ключа дважды и изменение цены во время оформления.
    user_res = await session.execute(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    user = user_res.scalar_one_or_none()
    if not user:
        await callback.answer("❌ Профиль не найден. Отправьте /start и повторите.", show_alert=True)
        return

    # Пересчитываем скидку непосредственно в момент списания, а не доверяем
    # цене из предыдущего экрана подтверждения.
    original_price = round_money(product.price)
    final_price = original_price
    discount_amount = 0.0
    promo = None

    state_data = await state.get_data()
    active_promo_id = state_data.get("active_discount_promo_id")
    if active_promo_id:
        promo_res = await session.execute(
            select(PromoCode).where(PromoCode.id == active_promo_id).with_for_update().execution_options(populate_existing=True)
        )
        promo = promo_res.scalar_one_or_none()

        if promo:
            used_before = await session.scalar(
                select(exists().where(
                    PromoCodeUsage.promo_id == promo.id,
                    PromoCodeUsage.user_id == user.id
                ))
            )
            if promo.promo_type != "discount" or promo.uses_left <= 0:
                promo = None
                await state.update_data(active_discount_promo_id=None)
            elif used_before:
                await state.update_data(active_discount_promo_id=None)
                await callback.answer("❌ Этот промокод уже был использован вами.", show_alert=True)
                return
            elif promo.product_id is not None and promo.product_id != product.id:
                await callback.answer("❌ Этот промокод не действует на выбранный товар.", show_alert=True)
                return
            else:
                percent = max(0.0, min(100.0, float(promo.discount_percent or 0.0)))
                discount_amount = round_money(original_price * percent / 100.0)
                final_price = max(0.0, round_money(original_price - discount_amount))

    if user.balance < final_price:
        await callback.answer(
            f"❌ Недостаточно средств! Требуется {final_price:.2f} грн", show_alert=True
        )
        return

    key_res = await session.execute(
        select(LicenseKey)
        .filter_by(product_id=product.id, is_sold=False)
        .limit(1)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)
    )
    license_key = key_res.scalars().first()

    if not license_key:
        await callback.answer("❌ Ключи этого номинала закончились!", show_alert=True)
        return

    user.balance = round_money(user.balance - final_price)
    license_key.is_sold = True
    log_balance_change(session, user.id, -final_price, "purchase", user.balance, related_id=product.id)

    if promo:
        promo.uses_left -= 1

    # НОВОЕ: остаток ключей ПОСЛЕ этой продажи (autoflush применит is_sold=True
    # до выполнения этого SELECT, поэтому только что проданный ключ уже не
    # попадёт в подсчёт "в наличии")
    remaining_res = await session.execute(
        select(func.count(LicenseKey.id)).filter_by(product_id=product.id, is_sold=False)
    )
    remaining_after_sale = remaining_res.scalar() or 0

    # ФИКС: уведомление рефереру раньше уходило ДО commit — при откате покупки
    # он получал сообщение о бонусе, которого на самом деле не было.
    referral_notice = None
    if user.referred_by:
        referrer_res = await session.execute(
            select(User).where(User.id == user.referred_by).with_for_update().execution_options(populate_existing=True)
        )
        referrer = referrer_res.scalar_one_or_none()
        if referrer:
            ref_bonus = round_money(final_price * REFERRAL_PERCENT / 100.0)
            referrer.balance = round_money(referrer.balance + ref_bonus)
            log_balance_change(session, referrer.id, ref_bonus, "referral_bonus", referrer.balance, related_id=user.id)
            referral_notice = (referrer.id, ref_bonus)

    sw_name = software.name if software else "Товар"
    purchase = Purchase(
        user_id=user.id,
        product_id=product.id,
        product_name=f"{sw_name} ({product.duration})",
        key_issued=license_key.key_string,
        duration=product.duration,
        price_paid=final_price,
    )
    session.add(purchase)

    if promo:
        session.add(PromoCodeUsage(
            promo_id=promo.id,
            user_id=user.id,
            discount_amount=discount_amount,
            final_price=final_price,
        ))
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Не удалось завершить покупку product_id=%s user_id=%s", product_id, user_id)
        await callback.answer(
            "⚠️ Покупка не проведена, средства не списаны. Попробуйте ещё раз.",
            show_alert=True,
        )
        return
    await state.update_data(active_discount_promo_id=None)

    success_text = (
        f"✅ **ЗАКАЗ #{purchase.id} ОПЛАЧЕН**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **{escape_md(sw_name)}**\n"
        f"⏳ Тариф: `{escape_md(product.duration)}`\n"
        f"💳 Списано: **{final_price:.2f} грн**\n"
        + (f"🎟 Скидка: `-{discount_amount:.2f} грн` ({float(promo.discount_percent):.0f}%)\n" if promo else "")
        + f"💰 Баланс: `{user.balance:.2f} грн`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 **Ваш ключ — нажмите, чтобы скопировать:**\n`{escape_md(license_key.key_string)}`\n\n"
        + (f"📁 [Файлы и инструкция]({md_url(software.files_link)})\n\n" if software and software.files_link else "")
        + "💾 Ключ сохранён в «Профиль» → «Мои ключи».\n\n"
        "⭐ Оставьте отзыв и получите вращение колеса фортуны."
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Оставить отзыв и крутить колесо 🎰", callback_data="leave_review")],
        [InlineKeyboardButton(text="📢 Канал отзывов", url=REVIEWS_CHANNEL_URL)],
        [InlineKeyboardButton(text="👤 Посмотреть мои ключи", callback_data="profile")],
        [InlineKeyboardButton(text="🛒 Купить еще софт", callback_data=await get_software_back_callback(session, software))],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    # ФИКС: покупатель получает ключ сразу, а не после рассылки уведомлений
    # всем админам; если edit_text не проходит (фото-сообщение, битая разметка) —
    # safe_edit_text отправит новое сообщение, чтобы ключ не потерялся.
    await safe_edit_text(callback, callback.bot, success_text, keyboard)
    await callback.answer()

    if referral_notice:
        try:
            await callback.bot.send_message(
                referral_notice[0],
                f"🎉 **Реферальный бонус!**\nВаш реферал совершил покупку. Вам начислено `{referral_notice[1]:.2f} грн` на баланс!",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление рефереру {referral_notice[0]}: {e}")

    # НОВОЕ: уведомление админам о каждой покупке ключа — с данными о
    # покупателе, товаре, сумме и оставшемся на складе количестве.
    purchase_notify_msg = (
        f"💸 **Новая покупка!**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Покупатель: `{user.id}` ({user_display(callback)})\n"
        f"📦 Товар: `{escape_md(sw_name)}`\n"
        f"⏳ Тариф: `{escape_md(product.duration)}`\n"
        f"💰 Сумма: `{final_price:.2f} грн`"
        + (f" (скидка {float(promo.discount_percent):.0f}%, было {original_price:.2f} грн)" if promo else "")
        + "\n"
        f"📊 Остаток на складе: `{remaining_after_sale}` шт."
    )
    for admin_id in ADMIN_IDS:
        try:
            await callback.bot.send_message(admin_id, purchase_notify_msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление о покупке админу {admin_id}: {e}")

    # НОВОЕ: уведомление админам о заканчивающемся остатке ключей.
    # Срабатывает один раз ровно в момент пересечения порога (а не на
    # каждой продаже ниже него) плюс отдельно — когда остаток обнулился.
    crossed_low_stock = (
        remaining_after_sale <= LOW_STOCK_THRESHOLD
        and remaining_after_sale + 1 > LOW_STOCK_THRESHOLD
    )
    if crossed_low_stock or remaining_after_sale == 0:
        if remaining_after_sale == 0:
            stock_text = "🔴 **Ключи закончились полностью!**"
        else:
            stock_text = f"🟡 **Остаток ключей на исходе:** `{remaining_after_sale}` шт."
        low_stock_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Загрузить ключи", callback_data=f"add_keys_prod_{product.id}")],
        ])
        for admin_id in ADMIN_IDS:
            try:
                await callback.bot.send_message(
                    admin_id,
                    f"{stock_text}\n\n"
                    f"📦 Товар: `{escape_md(sw_name)}`\n"
                    f"⏳ Тариф: `{escape_md(product.duration)}`",
                    reply_markup=low_stock_kb,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление о низком остатке админу {admin_id}: {e}")

# ==========================================
# 🎰 КОЛЕСО ФОРТУНЫ ПОСЛЕ ПОКУПКИ
# ==========================================
# Один спин на одну покупку (обеспечено уникальным purchase_id в WheelSpin +
# перехватом IntegrityError на случай двойного нажатия/гонки).
# Шансы: 5% — ключ на 1 день ЛЮБОГО СОФТА НА ВЫБОР пользователя,
# 5% — 50 грн на баланс, 90% — мимо.
# ==========================================
# 🎰 КОЛЕСО ФОРТУНЫ ЗА ОТЗЫВ
# ==========================================
# Тот же механизм, что и после покупки, но привязан к id отзыва
# (WheelSpin.review_id, уникален) — один спин на один отзыв.
@router.callback_query(F.data.startswith("wheelreview_"))
async def spin_wheel_review_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    review_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    review = await session.get(Review, review_id)
    if not review or review.user_id != user_id:
        await callback.answer("❌ Этот отзыв вам не принадлежит.", show_alert=True)
        return

    already_res = await session.execute(select(WheelSpin).filter_by(review_id=review_id))
    if already_res.scalars().first():
        await callback.answer("🎰 Вы уже крутили колесо за этот отзыв!", show_alert=True)
        return

    frames = ["🎰 🍒 ▪️ ▪️", "🎰 ▪️ 🔑 ▪️", "🎰 ▪️ ▪️ 💰", "🎰 ✨ ✨ ✨"]
    anim_msg = await bot.send_message(
        callback.message.chat.id,
        f"{frames[0]}\n\n_Крутим колесо фортуны..._",
        parse_mode="Markdown"
    )
    for frame in frames[1:]:
        try:
            await anim_msg.edit_text(
                f"{frame}\n\n_Крутим колесо фортуны..._",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await asyncio.sleep(0.4)

    # SystemRandom не позволяет предсказать результат по состоянию PRNG.
    roll = secrets.randbelow(1_000_000) / 1_000_000
    prize_kind = "lose"
    if roll < WHEEL_KEY_CHANCE:
        prize_kind = "key"
    elif roll < WHEEL_KEY_CHANCE + WHEEL_BALANCE_CHANCE:
        prize_kind = "balance"

    if prize_kind == "key":
        avail_res = await session.execute(
            select(Software.id, Software.name, func.count(LicenseKey.id))
            .join(Product, Product.software_id == Software.id)
            .join(LicenseKey, LicenseKey.product_id == Product.id)
            .where(Product.duration == WHEEL_KEY_DURATION, LicenseKey.is_sold == False)
            .group_by(Software.id, Software.name)
        )
        available_softwares = avail_res.all()

        if not available_softwares:
            prize_kind = "balance"
        else:
            spin = WheelSpin(review_id=review_id, user_id=user_id, result="key_pending")
            session.add(spin)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                await anim_msg.edit_text(
                    "🎰 Вы уже крутили колесо за этот отзыв!",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
                    ])
                )
                await callback.answer()
                return

            buttons = [
                [InlineKeyboardButton(
                    text=f"📦 {sw_name} ({cnt} шт.)",
                    callback_data=f"wheel_pick_{spin.id}_{sw_id}"
                )]
                for sw_id, sw_name, cnt in available_softwares
            ]
            await anim_msg.edit_text(
                f"🎉 **ДЖЕКПОТ!** 🎉\n\n"
                f"Вы выиграли ключ на **{WHEEL_KEY_DURATION}**!\n"
                f"👇 Выберите, какой софт хотите получить:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="Markdown"
            )
            await callback.answer()
            return

    user_res = await session.execute(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    user = user_res.scalar_one_or_none()

    result_text = ""
    final_kind = "lose"

    if prize_kind == "balance":
        if user:
            user.balance = round_money(user.balance + WHEEL_BALANCE_PRIZE)
            log_balance_change(session, user.id, WHEEL_BALANCE_PRIZE, "wheel_bonus", user.balance, related_id=review_id)
        final_kind = "balance"
        result_text = (
            f"🎉 **Поздравляем!** 🎉\n\n"
            f"Вы выиграли **{WHEEL_BALANCE_PRIZE:.0f} грн** на баланс!\n"
            f"💰 Новый баланс: `{(user.balance if user else 0):.2f} грн`"
        )
    else:
        result_text = (
            "🎲 **Почти!**\n\n"
            "В этот раз не повезло — спасибо, что оставили отзыв! 🍀"
        )

    spin = WheelSpin(review_id=review_id, user_id=user_id, result=final_kind)
    session.add(spin)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await anim_msg.edit_text(
            "🎰 Вы уже крутили колесо за этот отзыв!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
            ])
        )
        await callback.answer()
        return

    await anim_msg.edit_text(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
            [InlineKeyboardButton(text="🛒 Каталог софта", callback_data="shop")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ]),
        parse_mode="Markdown"
    )
    await callback.answer()

    if final_kind == "balance":
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎰 Пользователь `{user_id}` выиграл приз в колесе фортуны (за отзыв): {WHEEL_BALANCE_PRIZE:.0f} грн",
                    parse_mode="Markdown"
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("wheel_pick_"))
async def wheel_pick_software_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    parts = callback.data.split("_")
    spin_id = int(parts[2])
    software_id = int(parts[3])
    user_id = callback.from_user.id

    # ФИКС: блокируем спин — двойной тап по призу выдавал два ключа (или два
    # начисления) за один выигрыш, т.к. обе задачи видели result == "key_pending".
    spin = await lock_row(session, WheelSpin, spin_id)
    if not spin or spin.user_id != user_id:
        await callback.answer("❌ Этот приз вам не принадлежит.", show_alert=True)
        return

    if spin.result != "key_pending":
        await callback.answer("🎰 Этот приз уже был получен ранее!", show_alert=True)
        return

    software = await session.get(Software, software_id)
    if not software:
        await callback.answer("❌ Софт не найден!", show_alert=True)
        return

    # Берём случайный доступный ключ на 1 день среди тарифов выбранного софта
    key_res = await session.execute(
        select(LicenseKey)
        .join(Product, LicenseKey.product_id == Product.id)
        .where(
            Product.software_id == software_id,
            Product.duration == WHEEL_KEY_DURATION,
            LicenseKey.is_sold == False,
        )
        .limit(1)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)
    )
    license_key = key_res.scalars().first()

    if not license_key:
        # Пока пользователь выбирал — ключи именно этого софта закончились.
        # Приз не сгорает: предлагаем выбрать другой софт.
        avail_res = await session.execute(
            select(Software.id, Software.name, func.count(LicenseKey.id))
            .join(Product, Product.software_id == Software.id)
            .join(LicenseKey, LicenseKey.product_id == Product.id)
            .where(Product.duration == WHEEL_KEY_DURATION, LicenseKey.is_sold == False)
            .group_by(Software.id, Software.name)
        )
        available_softwares = avail_res.all()

        if not available_softwares:
            # Совсем ничего не осталось — заменяем приз на баланс
            user_res = await session.execute(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
            user = user_res.scalar_one_or_none()
            if user:
                user.balance = round_money(user.balance + WHEEL_BALANCE_PRIZE)
                log_balance_change(session, user.id, WHEEL_BALANCE_PRIZE, "wheel_bonus", user.balance, related_id=(spin.purchase_id or spin.review_id))
            spin.result = "balance"
            await session.commit()
            await callback.message.edit_text(
                f"😔 К сожалению, ключи этого софта только что закончились.\n\n"
                f"🎉 Вместо ключа начисляем **{WHEEL_BALANCE_PRIZE:.0f} грн** на баланс!\n"
                f"💰 Новый баланс: `{(user.balance if user else 0):.2f} грн`",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
                ]),
                parse_mode="Markdown"
            )
            await callback.answer()
            return

        buttons = [
            [InlineKeyboardButton(text=f"📦 {sw_name} ({cnt} шт.)", callback_data=f"wheel_pick_{spin.id}_{sw_id}")]
            for sw_id, sw_name, cnt in available_softwares
        ]
        await callback.answer("😔 Именно этот софт только что закончился, выберите другой!", show_alert=True)
        await callback.message.edit_text(
            f"🎉 **ДЖЕКПОТ!** 🎉\n\nВы выиграли ключ на **{WHEEL_KEY_DURATION}**!\n👇 Выберите софт:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )
        return

    license_key.is_sold = True
    bonus_purchase = Purchase(
        user_id=user_id,
        product_id=license_key.product_id,
        product_name=f"🎁 Приз колеса фортуны: {software.name}",
        key_issued=license_key.key_string,
        duration=WHEEL_KEY_DURATION,
        price_paid=0.0,
    )
    session.add(bonus_purchase)
    spin.result = "key"
    await session.commit()

    await callback.message.edit_text(
        f"🎉 **Готово!** 🎉\n\n"
        f"📦 `{escape_md(software.name)}` — **{WHEEL_KEY_DURATION}**\n"
        f"🔑 `{escape_md(license_key.key_string)}`\n\n"
        + (f"📁 **Файлы:** [Открыть]({md_url(software.files_link)})\n\n" if software.files_link else "")
        + "💾 Ключ уже сохранён в разделе «👤 Мой профиль».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ]),
        parse_mode="Markdown"
    )
    await callback.answer()

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🎰 Пользователь `{user_id}` выиграл и забрал ключ в колесе фортуны: `{escape_md(software.name)}` ({WHEEL_KEY_DURATION})",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ==========================================
# РЕФЕРАЛЬНАЯ СИСТЕМА И ВЫВОД СРЕДСТВ
# ==========================================
@router.callback_query(F.data == "ref_program")
async def show_ref_program(callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext):
    await clear_state_keep_promo(state)
    user_id = callback.from_user.id
    user = await session.get(User, user_id)
    balance = user.balance if user else 0.0

    ref_count_res = await session.execute(select(func.count(User.id)).where(User.referred_by == user_id))
    total_refs = ref_count_res.scalar() or 0

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"

    text_msg = (
        f"🤝 **Реферальная программа**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Приглашайте друзей по вашей ссылке и получайте **{REFERRAL_PERCENT:.2f}%** от суммы каждой их покупки прямо на баланс!\n\n"
        f"👥 **Приглашено друзей:** `{total_refs}`\n"
        f"💰 **Доступный баланс:** `{balance:.2f} грн`\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔗 **Ваша реферальная ссылка:**\n`{ref_link}`"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📤 Вывести средства (от {MIN_WITHDRAWAL:.0f} грн)", callback_data="start_withdrawal")],
            [InlineKeyboardButton(text="🛒 Каталог софта", callback_data="shop")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ]
    )
    await safe_edit_text(callback, bot, text_msg, keyboard)
    await callback.answer()

@router.callback_query(F.data == "start_withdrawal")
async def start_withdrawal_handler(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    user = await session.get(User, callback.from_user.id)
    balance = user.balance if user else 0.0
    if balance < MIN_WITHDRAWAL:
        await callback.answer(
            f"❌ Минимальная сумма вывода {MIN_WITHDRAWAL:.0f} грн. На вашем балансе: {balance:.2f} грн",
            show_alert=True
        )
        return

    await state.set_state(WithdrawalStates.waiting_for_amount)
    await callback.message.edit_text(
        f"📤 **Вывод средств**\n\n"
        f"💳 Ваш доступный баланс: `{balance:.2f} грн`\n\n"
        f"Введите сумму для вывода (минимум {MIN_WITHDRAWAL:.0f} грн):",
        reply_markup=cancel_kb("ref_program"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(WithdrawalStates.waiting_for_amount)
async def process_withdrawal_amount(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Отправьте сумму текстом (числом), например: 500", reply_markup=cancel_kb("ref_program"))
        return

    # ФИКС: используем безопасный парсер, отсекающий inf/nan/отрицательные/слишком большие суммы
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer("❌ Введите корректную сумму числом!", reply_markup=cancel_kb("ref_program"))
        return

    user = await session.get(User, message.from_user.id)
    balance = user.balance if user else 0.0

    if amount < MIN_WITHDRAWAL:
        await message.answer(f"❌ Минимальная сумма вывода — {MIN_WITHDRAWAL:.0f} грн. Введите сумму повторно:", reply_markup=cancel_kb("ref_program"))
        return
    if amount > balance:
        await message.answer(f"❌ У вас недостаточно средств! Доступно: `{balance:.2f} грн`. Введите сумму:", reply_markup=cancel_kb("ref_program"), parse_mode="Markdown")
        return

    await state.update_data(amount=amount)
    await state.set_state(WithdrawalStates.waiting_for_card)
    await message.answer("💳 Введите номер банковской карты для получения выплаты:", reply_markup=cancel_kb("ref_program"))

@router.message(WithdrawalStates.waiting_for_card)
async def process_withdrawal_card(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not message.text:
        await message.answer("❌ Отправьте номер карты текстом.", reply_markup=cancel_kb("ref_program"))
        return

    # Валидация номера карты: 13-19 цифр (пробелы/дефисы допускаются) + контрольная
    # сумма Луна — отсекает опечатки, из-за которых выплата ушла бы «в никуда».
    digits_only = re.sub(r"[\s-]", "", message.text)
    if not digits_only.isdigit() or not (13 <= len(digits_only) <= 19) or not luhn_ok(digits_only):
        await message.answer(
            "❌ Некорректный номер карты. Проверьте номер и введите его ещё раз (13-19 цифр):",
            reply_markup=cancel_kb("ref_program")
        )
        return
    card = " ".join(digits_only[i:i + 4] for i in range(0, len(digits_only), 4))

    data = await state.get_data()
    amount = data.get("amount")
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or not MIN_WITHDRAWAL <= amount <= MAX_AMOUNT:
        await message.answer("⚠️ Сессия вывода устарела, начните заново через «Реферальная система».")
        await state.clear()
        return

    user_id = message.from_user.id

    # ФИКС: блокируем строку пользователя, чтобы исключить одновременное создание
    # нескольких заявок на вывод, суммарно превышающих реальный баланс.
    user_res = await session.execute(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    user = user_res.scalar_one_or_none()
    if not user or user.balance < amount:
        await message.answer("❌ Недостаточно средств на балансе.")
        await state.clear()
        return

    user.balance = round_money(user.balance - amount)
    req = WithdrawalRequest(user_id=user_id, amount=amount, card=card)
    session.add(req)
    await session.flush()  # получаем req.id до commit, чтобы привязать к нему запись лога
    log_balance_change(session, user_id, -amount, "withdrawal_request", user.balance, related_id=req.id)
    await session.commit()
    await state.clear()

    lang = user.language if user and user.language in LANGUAGES else "uk"
    await message.answer(
        f"✅ **Заявка на вывод создана!**\n\n"
        f"💰 Сумма: `{amount:.2f} грн`\n"
        f"💳 Карта: `{escape_md(card)}`\n\n"
        f"⏳ Ожидайте обработки администратором.",
        reply_markup=get_main_menu_kb(user_id, lang),
        parse_mode="Markdown"
    )

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить выплату", callback_data=f"approve_wdr_{req.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_wdr_{req.id}")
        ]]
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📤 **Заявка на вывод средств #{req.id}**\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 Пользователь: `{user_id}` ({user_display(message)})\n"
                    f"💰 Сумма: `{amount:.2f} грн`\n"
                    f"💳 Карта: `{escape_md(card)}`"
                ),
                reply_markup=admin_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.exception(f"Не удалось отправить уведомление о выводе админу {admin_id}: {e}")

@router.callback_query(F.data.startswith("approve_wdr_"), F.from_user.id.in_(ADMIN_IDS))
async def approve_withdrawal(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    req_id = int(callback.data.split("_")[2])
    # ФИКС: блокируем заявку на время обработки — иначе двойной тап по кнопке
    # (или два админа сразу) проводили одну заявку дважды.
    req = await lock_row(session, WithdrawalRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    req.status = "approved"
    req.processed_by = callback.from_user.id
    req.processed_at = datetime.now(timezone.utc)
    await session.commit()

    try:
        await bot.send_message(
            req.user_id,
            f"🎉 **Ваша заявка на вывод `{req.amount:.2f} грн` успешно обработана!**\nСредства отправлены на карту `{escape_md(req.card)}`.",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await append_status(callback, "\n\n✅ ВЫПЛАЧЕНО")
    await callback.answer()

@router.callback_query(F.data.startswith("reject_wdr_"), F.from_user.id.in_(ADMIN_IDS))
async def reject_withdrawal(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    req_id = int(callback.data.split("_")[2])
    # ФИКС: без блокировки заявки двойной тап «Отклонить» возвращал деньги дважды
    req = await lock_row(session, WithdrawalRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    user_res = await session.execute(select(User).where(User.id == req.user_id).with_for_update().execution_options(populate_existing=True))
    user = user_res.scalar_one_or_none()
    if not user:
        await callback.answer("❌ Пользователь не найден. Возврат не выполнен.", show_alert=True)
        return

    req.status = "rejected"
    req.processed_by = callback.from_user.id
    req.processed_at = datetime.now(timezone.utc)
    user.balance = round_money(user.balance + req.amount)
    log_balance_change(session, user.id, req.amount, "withdrawal_rejected_refund", user.balance, related_id=req.id)
    await session.commit()

    try:
        await bot.send_message(
            req.user_id,
            f"❌ Ваша заявка на вывод `{req.amount:.2f} грн` была отклонена. Средства возвращены на ваш баланс.",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await append_status(callback, "\n\n❌ ОТКЛОНЕНО (средства возвращены)")
    await callback.answer()

# ==========================================
# ПОПОЛНЕНИЕ БАЛАНСА И ПРИЕМ ЧЕКОВ
# ==========================================
@router.callback_query(F.data == "deposit")
async def start_deposit(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot):
    # НОВОЕ: лимит заявок на пополнение в статусе "pending" на одного пользователя —
    # не даём плодить бесконечные заявки, пока предыдущие ещё не рассмотрены админом.
    pending_count = await session.scalar(
        select(func.count(DepositRequest.id)).where(
            DepositRequest.user_id == callback.from_user.id,
            DepositRequest.status == "pending",
        )
    )
    if pending_count and pending_count >= MAX_PENDING_DEPOSITS:
        await callback.answer(
            f"❌ У вас уже есть {pending_count} заявок на пополнение, ожидающих проверки. "
            f"Дождитесь их обработки администратором, прежде чем создавать новую.",
            show_alert=True
        )
        return

    await state.set_state(DepositStates.waiting_for_amount)
    await safe_edit_text(
        callback,
        bot,
        f"💳 **Пополнение баланса**\n\nВведите сумму пополнения в гривнах числом (минимум {MIN_DEPOSIT:.0f} грн):",
        cancel_kb("main_menu"),
    )
    await callback.answer()

@router.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Отправьте сумму текстом (числом), например: 300", reply_markup=cancel_kb("main_menu"))
        return

    # ФИКС: безопасный парсер сумм (отсекает inf/nan и т.д.) — критично для депозитов,
    # так как раньше float("inf") проходил проверку "amount <= 0" и мог довести
    # до бесконечного зачисления при невнимательном подтверждении админом.
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer("❌ Введите корректную сумму числом!", reply_markup=cancel_kb("main_menu"))
        return

    # НОВОЕ: минимальная сумма пополнения
    if amount < MIN_DEPOSIT:
        await message.answer(
            f"❌ Минимальная сумма пополнения — {MIN_DEPOSIT:.0f} грн. Введите сумму повторно:",
            reply_markup=cancel_kb("main_menu")
        )
        return

    selected_card = random.choice(CARDS)
    await state.update_data(amount=amount, card=selected_card)
    await state.set_state(DepositStates.waiting_for_receipt)

    text_msg = (
        f"💳 **Оплата по реквизитам**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 **Сумма к оплате:** `{amount:.2f} грн`\n\n"
        f"📌 **Реквизиты для перевода:**\n`{selected_card}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"📸 Переведите точную сумму и **отправьте сюда фото или файл чека**."
    )
    await message.answer(text_msg, reply_markup=cancel_kb("main_menu"), parse_mode="Markdown")

@router.message(DepositStates.waiting_for_receipt, F.photo | F.document)
async def process_receipt(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    amount = data.get("amount")
    user_id = message.from_user.id

    # ФИКС: если состояние потерялось (перезапуск процесса), раньше создавалась
    # заявка на 0.00 грн, которую админ мог случайно «подтвердить».
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or not MIN_DEPOSIT <= amount <= MAX_AMOUNT:
        await state.clear()
        await message.answer("⚠️ Сессия пополнения устарела. Начните заново через «💳 Пополнить баланс».")
        return

    # Повторная проверка лимита на случай, если пользователь успел открыть
    # несколько параллельных заявок до отправки чека (TOCTOU-защита)
    pending_count = await session.scalar(
        select(func.count(DepositRequest.id)).where(
            DepositRequest.user_id == user_id,
            DepositRequest.status == "pending",
        )
    )
    if pending_count and pending_count >= MAX_PENDING_DEPOSITS:
        await state.clear()
        await message.answer(
            f"❌ У вас уже есть {pending_count} заявок на пополнение в обработке. "
            f"Дождитесь их подтверждения администратором.",
        )
        return

    receipt_file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    receipt_type = "photo" if message.photo else "document"
    req = DepositRequest(
        user_id=user_id,
        amount=amount,
        payment_card=(data.get("card") or "")[:100] or None,
        receipt_file_id=receipt_file_id,
        receipt_type=receipt_type,
    )
    session.add(req)
    await session.commit()

    await state.clear()
    user = await session.get(User, user_id)
    lang = user.language if user and user.language in LANGUAGES else "uk"

    await message.answer(
        "⏳ **Чек принят!** Заявка отправлена администратору на проверку.",
        reply_markup=get_main_menu_kb(user_id, lang),
        parse_mode="Markdown"
    )

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_dep_{req.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_dep_{req.id}"),
        ]]
    )

    caption_text = (
        f"📥 **Заявка на пополнение #{req.id}**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Пользователь: `{user_id}` ({user_display(message)})\n"
        f"💰 Сумма: `{amount:.2f} грн`"
    )

    for admin_id in ADMIN_IDS:
        try:
            if message.photo:
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=message.photo[-1].file_id,
                    caption=caption_text,
                    reply_markup=admin_kb,
                    parse_mode="Markdown"
                )
            elif message.document:
                await bot.send_document(
                    chat_id=admin_id,
                    document=message.document.file_id,
                    caption=caption_text,
                    reply_markup=admin_kb,
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.exception(f"Не удалось отправить чек администратору {admin_id}: {e}")

@router.message(DepositStates.waiting_for_receipt)
async def process_receipt_wrong_type(message: Message):
    await message.answer(
        "📌 Пожалуйста, отправьте **фото или файл** чека об оплате.",
        reply_markup=cancel_kb("main_menu"),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("approve_dep_"), F.from_user.id.in_(ADMIN_IDS))
async def approve_deposit(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    req_id = int(callback.data.split("_")[2])
    # ФИКС: двойной тап «Подтвердить» зачислял пополнение дважды —
    # теперь заявка блокируется, и второй обработчик увидит статус «approved».
    req = await lock_row(session, DepositRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    user_res = await session.execute(select(User).where(User.id == req.user_id).with_for_update().execution_options(populate_existing=True))
    user = user_res.scalar_one_or_none()
    if not user:
        await callback.answer("❌ Пользователь не найден. Заявка не проведена.", show_alert=True)
        return

    req.status = "approved"
    req.processed_by = callback.from_user.id
    req.processed_at = datetime.now(timezone.utc)
    user.balance = round_money(user.balance + req.amount)
    log_balance_change(session, user.id, req.amount, "deposit_approved", user.balance, related_id=req.id)
    await session.commit()

    try:
        await bot.send_message(req.user_id, f"🎉 **Баланс успешно пополнен на {req.amount:.2f} грн!**", parse_mode="Markdown")
    except Exception:
        pass
    await append_status(callback, "\n\n✅ ПОДТВЕРЖДЕНО")
    await callback.answer()

@router.callback_query(F.data.startswith("reject_dep_"), F.from_user.id.in_(ADMIN_IDS))
async def reject_deposit(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    req_id = int(callback.data.split("_")[2])
    req = await lock_row(session, DepositRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    req.status = "rejected"
    req.processed_by = callback.from_user.id
    req.processed_at = datetime.now(timezone.utc)
    await session.commit()

    try:
        await bot.send_message(req.user_id, "❌ Ваша заявка на пополнение была отклонена.")
    except Exception:
        pass
    await append_status(callback, "\n\n❌ ОТКЛОНЕНО")
    await callback.answer()

# ==========================================
# ПАНЕЛЬ АДМИНИСТРАТОРА И РАСШИРЕННЫЕ ФУНКЦИИ
# ==========================================
async def _admin_dashboard_counts(session: AsyncSession):
    users = await session.scalar(select(func.count(User.id))) or 0
    purchases = await session.scalar(select(func.count(Purchase.id))) or 0
    revenue = await session.scalar(select(func.coalesce(func.sum(Purchase.price_paid), 0))) or 0
    stock = await session.scalar(select(func.count(LicenseKey.id)).where(LicenseKey.is_sold == False)) or 0
    pending_dep = await session.scalar(select(func.count(DepositRequest.id)).where(DepositRequest.status == "pending")) or 0
    pending_wdr = await session.scalar(select(func.count(WithdrawalRequest.id)).where(WithdrawalRequest.status == "pending")) or 0
    pending_reviews = await session.scalar(select(func.count(Review.id)).where(Review.status == "pending")) or 0
    return users, purchases, float(revenue), stock, pending_dep, pending_wdr, pending_reviews


@router.callback_query(F.data == "admin_panel", F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data == "admin_dashboard_refresh", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu(callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    await state.clear()
    users, purchases, revenue, stock, pending_dep, pending_wdr, pending_reviews = await _admin_dashboard_counts(session)
    text_msg = (
        "🛠 **KRANIN ADMIN • DASHBOARD**\n━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Пользователей: `{users}`\n"
        f"🛒 Покупок: `{purchases}`\n"
        f"💰 Оборот: `{revenue:.2f} грн`\n"
        f"📦 Ключей на складе: `{stock}`\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 Пополнения: `{pending_dep}` · 📤 Выводы: `{pending_wdr}`\n"
        f"⭐ Модерация отзывов: `{pending_reviews}`"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Товары и склад", callback_data="admin_catalog")],
        [InlineKeyboardButton(text="💳 Продажи и заявки", callback_data="admin_sales")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="admin_reviews_manage")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_start_broadcast")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin_promo_list")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_dashboard_refresh")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await safe_edit_text(callback, bot, text_msg, keyboard)
    await callback.answer()


@router.callback_query(F.data == "admin_catalog", F.from_user.id.in_(ADMIN_IDS))
async def admin_catalog_menu(callback: CallbackQuery, session: AsyncSession):
    stock = await session.scalar(select(func.count(LicenseKey.id)).where(LicenseKey.is_sold == False)) or 0
    softwares = await session.scalar(select(func.count(Software.id))) or 0
    products = await session.scalar(select(func.count(Product.id))) or 0
    text_msg = (
        "📦 **ТОВАРЫ И СКЛАД**\n━━━━━━━━━━━━━━━━━━━\n"
        f"🛡 Софт: `{softwares}` · 🧾 Тарифов: `{products}`\n"
        f"🔑 В наличии: `{stock}`"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить игру", callback_data="admin_add_game"), InlineKeyboardButton(text="➕ Добавить софт", callback_data="admin_add_sw")],
        [InlineKeyboardButton(text="➕ Создать тариф", callback_data="admin_add_prod"), InlineKeyboardButton(text="✏️ Изменить цену", callback_data="admin_edit_price_select")],
        [InlineKeyboardButton(text="🗑 Удалить софт", callback_data="admin_del_sw_select"), InlineKeyboardButton(text="🗑 Удалить тариф", callback_data="admin_del_prod_select")],
        [InlineKeyboardButton(text="📥 Загрузить ключи", callback_data="admin_add_keys_select"), InlineKeyboardButton(text="🗑 Очистить ключи", callback_data="admin_del_keys_select")],
        [InlineKeyboardButton(text="📊 Склад по тарифам", callback_data="admin_stock")],
        [InlineKeyboardButton(text="🖼 Фото/описание", callback_data="admin_sw_media_select")],
        [InlineKeyboardButton(text="🔗 Ссылки на файлы", callback_data="admin_sw_link_select")],
        [InlineKeyboardButton(text="🔋 Пополнение аккаунтов", callback_data="admin_topup_accounts")],
        [InlineKeyboardButton(text="🔙 В dashboard", callback_data="admin_panel")],
    ])
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "admin_stock", F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data.regexp(r"^admin_stock_page_[0-9]+$"), F.from_user.id.in_(ADMIN_IDS))
async def admin_stock_handler(callback: CallbackQuery, session: AsyncSession):
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data.startswith("admin_stock_page_") else 0
    result = await session.execute(
        select(Product, Software)
        .join(Software, Product.software_id == Software.id)
        .order_by(Software.name.asc(), Product.id.asc())
    )
    rows = result.all()
    page, total_pages, offset = page_bounds(len(rows), page, CATALOG_PAGE_SIZE)
    page_rows = rows[offset:offset + CATALOG_PAGE_SIZE]
    product_ids = [product.id for product, _software in page_rows]
    stock_map = {}
    if product_ids:
        stock_res = await session.execute(
            select(LicenseKey.product_id, LicenseKey.is_sold, func.count(LicenseKey.id))
            .where(LicenseKey.product_id.in_(product_ids))
            .group_by(LicenseKey.product_id, LicenseKey.is_sold)
        )
        stock_map = {(pid, sold): count for pid, sold, count in stock_res.all()}
    lines = ["📊 **СКЛАД ПО ТАРИФАМ**", "━━━━━━━━━━━━━━━━━━━"]
    buttons = []
    if not page_rows:
        lines.append("Тарифов пока нет.")
    for product, software in page_rows:
        available = stock_map.get((product.id, False), 0)
        sold = stock_map.get((product.id, True), 0)
        icon = "🟢" if available > LOW_STOCK_THRESHOLD else "🟡" if available else "🔴"
        lines.append(
            f"{icon} **{escape_md(software.name)}** · `{escape_md(product.duration)}`\n"
            f"   В наличии `{available}` · продано `{sold}` · `{product.price:.2f} грн`"
        )
        buttons.append([InlineKeyboardButton(text=f"📥 {software.name[:30]} · {product.duration}", callback_data=f"add_keys_prod_{product.id}")])
    nav = pagination_row("admin_stock_page_", page, total_pages)
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Товары и склад", callback_data="admin_catalog")])
    await callback.message.edit_text(clip_text("\n".join(lines)), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "admin_sales", F.from_user.id.in_(ADMIN_IDS))
async def admin_sales_menu(callback: CallbackQuery, session: AsyncSession):
    now = datetime.now(timezone.utc)
    today = now.date()
    total_revenue = await session.scalar(select(func.coalesce(func.sum(Purchase.price_paid), 0))) or 0
    today_revenue = await session.scalar(select(func.coalesce(func.sum(Purchase.price_paid), 0)).where(func.date(Purchase.purchased_at) == today)) or 0
    week_revenue = await session.scalar(select(func.coalesce(func.sum(Purchase.price_paid), 0)).where(Purchase.purchased_at >= now - timedelta(days=7))) or 0
    month_revenue = await session.scalar(select(func.coalesce(func.sum(Purchase.price_paid), 0)).where(Purchase.purchased_at >= now - timedelta(days=30))) or 0
    avg_check = await session.scalar(select(func.coalesce(func.avg(Purchase.price_paid), 0))) or 0
    purchases = await session.scalar(select(func.count(Purchase.id))) or 0
    text_msg = (
        "💳 **ПРОДАЖИ И ФИНАНСЫ**\n━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Сегодня: **{float(today_revenue):.2f} грн**\n"
        f"📆 7 дней: `{float(week_revenue):.2f} грн`\n"
        f"🗓 30 дней: `{float(month_revenue):.2f} грн`\n"
        f"💰 За всё время: `{float(total_revenue):.2f} грн`\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 Покупок: `{purchases}`\n"
        f"🧾 Средний чек: `{float(avg_check):.2f} грн`"
    )
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Неподтверждённые заявки", callback_data="admin_pending_orders")],
        [InlineKeyboardButton(text="📤 Экспорт продаж CSV", callback_data="admin_export_sales")],
        [InlineKeyboardButton(text="🔙 В dashboard", callback_data="admin_panel")],
    ]), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "admin_export_sales", F.from_user.id.in_(ADMIN_IDS))
async def admin_export_sales_handler(callback: CallbackQuery, session: AsyncSession):
    result = await session.execute(
        select(Purchase).order_by(Purchase.purchased_at.desc()).limit(10_000)
    )
    purchases = result.scalars().all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["purchase_id", "purchased_at_utc", "user_id", "product", "duration", "price_paid_uah"])
    for purchase in purchases:
        writer.writerow([
            purchase.id,
            purchase.purchased_at.isoformat() if purchase.purchased_at else "",
            purchase.user_id,
            purchase.product_name,
            purchase.duration,
            f"{purchase.price_paid:.2f}",
        ])
    document = BufferedInputFile(
        output.getvalue().encode("utf-8-sig"),
        filename=f"kranin_sales_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv",
    )
    await callback.message.answer_document(
        document,
        caption=f"📤 Продажи: {len(purchases)} записей (максимум 10 000 последних).",
    )
    await callback.answer("✅ CSV сформирован.")


@router.callback_query(F.data == "admin_users", F.from_user.id.in_(ADMIN_IDS))
async def admin_users_menu(callback: CallbackQuery, session: AsyncSession):
    users = await session.scalar(select(func.count(User.id))) or 0
    banned = await session.scalar(select(func.count(User.id)).where(User.is_banned == True)) or 0
    buyers = await session.scalar(select(func.count(func.distinct(Purchase.user_id)))) or 0
    text_msg = (
        "👥 **ПОЛЬЗОВАТЕЛИ**\n━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Всего: `{users}`\n"
        f"🛒 Покупали: `{buyers}`\n"
        f"🚫 Заблокированы: `{banned}`"
    )
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Найти / управлять", callback_data="admin_user_manage")],
        [InlineKeyboardButton(text="🔙 В dashboard", callback_data="admin_panel")],
    ]), parse_mode="Markdown")
    await callback.answer()


# --- НОВОЕ: НЕПОДТВЕРЖДЕННЫЕ ЗАЯВКИ (ПОПОЛНЕНИЯ + ВЫВОДЫ) ---
# Раньше заявки на пополнение/вывод уходили админу только один раз,
# при создании — если сообщение потерялось (например, у другого админа
# в другом чате, или было случайно закрыто), найти "зависшую" заявку
# можно было только вручную через базу данных. Теперь все заявки со
# статусом "pending" собраны в одном разделе панели администратора.
@router.callback_query(F.data == "admin_pending_orders", F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data.startswith("admin_pending_page_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_pending_orders(callback: CallbackQuery, session: AsyncSession):
    page = 0
    if callback.data.startswith("admin_pending_page_"):
        page = int(callback.data.split("_")[3])

    dep_res = await session.execute(
        select(DepositRequest).where(DepositRequest.status == "pending").order_by(DepositRequest.created_at.asc())
    )
    deposits = dep_res.scalars().all()

    wdr_res = await session.execute(
        select(WithdrawalRequest).where(WithdrawalRequest.status == "pending").order_by(WithdrawalRequest.created_at.asc())
    )
    withdrawals = wdr_res.scalars().all()

    # Объединяем оба типа заявок в единый список, отсортированный по времени создания
    combined = [("deposit", d) for d in deposits] + [("withdrawal", w) for w in withdrawals]
    combined.sort(key=lambda item: item[1].created_at)

    total = len(combined)
    per_page = 5
    total_pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, total_pages - 1))
    offset = page * per_page
    page_items = combined[offset:offset + per_page]

    header = (
        f"🧾 **Неподтвержденные заявки**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📥 Пополнений в ожидании: `{len(deposits)}`\n"
        f"📤 Выводов в ожидании: `{len(withdrawals)}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    if not page_items:
        text_msg = header + "\n✅ Все заявки обработаны, ожидающих нет."
    else:
        lines = [header]
        for kind, req in page_items:
            date_str = req.created_at.strftime("%d.%m.%Y %H:%M")
            if kind == "deposit":
                lines.append(
                    f"📥 **Пополнение #{req.id}**\n"
                    f"👤 Пользователь: `{req.user_id}`\n"
                    f"💰 Сумма: `{req.amount:.2f} грн`\n"
                    f"🕒 _{date_str}_"
                )
            else:
                lines.append(
                    f"📤 **Вывод #{req.id}**\n"
                    f"👤 Пользователь: `{req.user_id}`\n"
                    f"💰 Сумма: `{req.amount:.2f} грн`\n"
                    f"💳 Карта: `{escape_md(req.card)}`\n"
                    f"🕒 _{date_str}_"
                )
        text_msg = "\n\n".join(lines)

    buttons = []
    for kind, req in page_items:
        prefix = "dep" if kind == "deposit" else "wdr"
        icon = "📥" if kind == "deposit" else "📤"
        if kind == "deposit":
            buttons.append([InlineKeyboardButton(text=f"🔎 Открыть пополнение #{req.id}", callback_data=f"admin_deposit_view_{req.id}")])
        buttons.append([
            InlineKeyboardButton(text=f"✅ {icon}#{req.id}", callback_data=f"approve_{prefix}_{req.id}"),
            InlineKeyboardButton(text=f"❌ {icon}#{req.id}", callback_data=f"reject_{prefix}_{req.id}"),
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_pending_page_{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_pending_page_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin_pending_page_{page}")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        text_msg,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_deposit_view_[0-9]+$"), F.from_user.id.in_(ADMIN_IDS))
async def admin_deposit_view_handler(callback: CallbackQuery, session: AsyncSession):
    req_id = int(callback.data.rsplit("_", 1)[1])
    req = await session.get(DepositRequest, req_id)
    if not req:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return
    user = await session.get(User, req.user_id)
    username = f"@{escape_md(user.username)}" if user and user.username else "без username"
    status_label = {"pending": "🕒 Ожидает", "approved": "✅ Подтверждена", "rejected": "❌ Отклонена"}.get(req.status, req.status)
    text_msg = (
        f"📥 **Пополнение #{req.id}**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👤 `{req.user_id}` · {username}\n"
        f"💰 Сумма: **{req.amount:.2f} грн**\n"
        f"💳 Карта получателя: `{escape_md(req.payment_card or 'не сохранена')}`\n"
        f"📌 Статус: {status_label}\n"
        f"🕒 Создана: `{format_dt(req.created_at)}`"
    )
    if req.processed_at:
        text_msg += f"\n🛠 Обработана: `{format_dt(req.processed_at)}` · admin `{req.processed_by or '—'}`"
    buttons = []
    if req.receipt_file_id:
        buttons.append([InlineKeyboardButton(text="📎 Открыть чек", callback_data=f"admin_deposit_receipt_{req.id}")])
    if req.status == "pending":
        buttons.append([
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_dep_{req.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_dep_{req.id}"),
        ])
    buttons.append([InlineKeyboardButton(text="🔙 К заявкам", callback_data="admin_pending_orders")])
    await callback.message.edit_text(clip_text(text_msg), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_deposit_receipt_[0-9]+$"), F.from_user.id.in_(ADMIN_IDS))
async def admin_deposit_receipt_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    req_id = int(callback.data.rsplit("_", 1)[1])
    req = await session.get(DepositRequest, req_id)
    if not req or not req.receipt_file_id:
        await callback.answer("❌ Чек не сохранён для этой старой заявки.", show_alert=True)
        return
    caption = f"Чек к пополнению #{req.id} · {req.amount:.2f} грн"
    try:
        if req.receipt_type == "document":
            await bot.send_document(callback.message.chat.id, req.receipt_file_id, caption=caption)
        else:
            await bot.send_photo(callback.message.chat.id, req.receipt_file_id, caption=caption)
        await callback.answer()
    except TelegramBadRequest:
        await callback.answer("❌ Telegram не смог открыть сохранённый файл.", show_alert=True)


@router.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    # Кнопка-индикатор страницы — ничего не делает, просто гасит "часики" на кнопке
    await callback.answer()


# --- ⭐ ПРОФЕССИОНАЛЬНАЯ МОДЕРАЦИЯ ОТЗЫВОВ ---
REVIEW_FILTERS = {
    "pending": ("🕒 На модерации", "pending"),
    "approved": ("✅ Проверенные", "approved"),
    "hidden": ("🙈 Скрытые", "hidden"),
    "all": ("📋 Все", None),
}


@router.callback_query(F.data == "admin_reviews_manage", F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data.startswith("admin_rev_filter_"), F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data.startswith("admin_rev_page_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_reviews_list(callback: CallbackQuery, session: AsyncSession):
    status = "pending"
    page = 0
    if callback.data.startswith("admin_rev_filter_"):
        status = callback.data.split("_")[3]
    elif callback.data.startswith("admin_rev_page_"):
        parts = callback.data.split("_")
        if len(parts) > 3 and parts[3].isdigit():
            status = "pending"
            page = int(parts[3])
        else:
            status = parts[3] if len(parts) > 3 else "pending"
            page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    if status not in REVIEW_FILTERS:
        status = "pending"
    _, status_value = REVIEW_FILTERS[status]
    where = [] if status_value is None else [Review.status == status_value]
    total = await session.scalar(select(func.count(Review.id)).where(*where)) or 0
    pending = await session.scalar(select(func.count(Review.id)).where(Review.status == "pending")) or 0
    approved = await session.scalar(select(func.count(Review.id)).where(Review.status == "approved")) or 0
    hidden = await session.scalar(select(func.count(Review.id)).where(Review.status == "hidden")) or 0
    per_page = 5
    total_pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, total_pages - 1))
    result = await session.execute(
        select(Review, User.username, Software.name, Product.duration)
        .join(User, Review.user_id == User.id, isouter=True)
        .join(Product, Review.product_id == Product.id, isouter=True)
        .join(Software, Product.software_id == Software.id, isouter=True)
        .where(*where).order_by(Review.created_at.desc()).offset(page * per_page).limit(per_page)
    )
    rows = result.all()
    text_msg = (
        "⭐ **ОТЗЫВЫ / МОДЕРАЦИЯ**\n━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 `{pending}` · ✅ `{approved}` · 🙈 `{hidden}`\n"
        f"Раздел: **{REVIEW_FILTERS[status][0]}** · `{page + 1}/{total_pages}`\n━━━━━━━━━━━━━━━━━━━\n"
    )
    if not rows:
        text_msg += "Здесь пока нет отзывов."
    buttons = []
    for review, username, sw_name, duration in rows:
        author = f"@{username}" if username else f"ID {review.user_id}"
        label = f"{sw_name} • {duration}" if sw_name else "Старый отзыв"
        status_icon = {"pending": "🕒", "approved": "✅", "hidden": "🙈"}.get(review.status, "❔")
        snippet = review.text[:45] + "…" if len(review.text) > 45 else review.text
        text_msg += (
            f"{status_icon} **#{review.id}** · {escape_md(author)} · {review_stars(review.rating)}\n"
            f"📦 `{escape_md(label)}`\n_{escape_md(snippet)}_\n──────────────\n"
        )
        buttons.append([InlineKeyboardButton(text=f"🔎 Открыть #{review.id}", callback_data=f"admin_review_view_{review.id}")])
    buttons.insert(0, [
        InlineKeyboardButton(text="🕒", callback_data="admin_rev_filter_pending"),
        InlineKeyboardButton(text="✅", callback_data="admin_rev_filter_approved"),
        InlineKeyboardButton(text="🙈", callback_data="admin_rev_filter_hidden"),
        InlineKeyboardButton(text="📋", callback_data="admin_rev_filter_all"),
    ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_rev_page_{status}_{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_rev_page_{status}_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons += [
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin_rev_filter_{status}")],
        [InlineKeyboardButton(text="🔙 В dashboard", callback_data="admin_panel")],
    ]
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_review_view_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_review_view(callback: CallbackQuery, session: AsyncSession):
    try:
        review_id = int(callback.data.split("_")[3])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректный ID отзыва.", show_alert=True)
        return
    review = await session.get(Review, review_id)
    if not review:
        await callback.answer("❌ Отзыв не найден.", show_alert=True)
        return
    user = await session.get(User, review.user_id)
    product = await session.get(Product, review.product_id) if review.product_id else None
    software = await session.get(Software, product.software_id) if product else None
    author = f"@{user.username}" if user and user.username else f"ID {review.user_id}"
    label = f"{software.name} • {product.duration}" if software and product else "Старый отзыв без товара"
    status_label = {"pending": "🕒 На модерации", "approved": "✅ Проверен администрацией", "hidden": "🙈 Скрыт"}.get(review.status, review.status)
    text_msg = (
        f"⭐ **Отзыв #{review.id}**\n━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Автор: `{escape_md(author)}`\n"
        f"🆔 Telegram ID: `{review.user_id}`\n"
        f"📦 `{escape_md(label)}`\n"
        f"⭐ {review_stars(review.rating)}\n"
        f"📌 Статус: **{status_label}**\n"
        f"🕒 `{review.created_at.strftime('%d.%m.%Y %H:%M')}`\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"💬 **Текст:**\n_{escape_md(review.text)}_"
    )
    buttons = []
    if review.status != "approved":
        buttons.append([InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"admin_approve_review_{review.id}")])
    if review.status != "hidden":
        buttons.append([InlineKeyboardButton(text="🙈 Скрыть", callback_data=f"admin_hide_review_{review.id}")])
    buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_del_review_confirm_{review.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 К отзывам", callback_data="admin_reviews_manage")])
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_approve_review_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_approve_review(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    review_id = int(callback.data.split("_")[3])
    review = await session.get(Review, review_id)
    if not review:
        await callback.answer("❌ Отзыв не найден.", show_alert=True)
        return
    if review.status == "approved":
        await callback.answer("✅ Уже опубликован.", show_alert=True)
        return
    review.status = "approved"
    await session.commit()
    try:
        await bot.send_message(review.user_id, "✅ Ваш отзыв проверен администрацией KRANIN SHOP. Спасибо за обратную связь!")
    except Exception:
        pass
    await callback.answer("✅ Отзыв опубликован.", show_alert=True)
    await admin_review_view(callback, session)


@router.callback_query(F.data.startswith("admin_hide_review_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_hide_review(callback: CallbackQuery, session: AsyncSession):
    review_id = int(callback.data.split("_")[3])
    review = await session.get(Review, review_id)
    if not review:
        await callback.answer("❌ Отзыв не найден.", show_alert=True)
        return
    review.status = "hidden"
    await session.commit()
    await callback.answer("🙈 Отзыв скрыт.", show_alert=True)
    await admin_review_view(callback, session)


@router.callback_query(F.data.startswith("admin_del_review_confirm_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_delete_review_confirm(callback: CallbackQuery, session: AsyncSession):
    review_id = int(callback.data.split("_")[4])
    review = await session.get(Review, review_id)
    if not review:
        await callback.answer("❌ Отзыв уже удалён.", show_alert=True)
        return
    await callback.message.edit_text(
        f"⚠️ **Удаление отзыва #{review.id}**\n\nБез возможности восстановления.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"admin_del_review_{review.id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_review_view_{review.id}")],
        ]), parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_del_review_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_delete_review(callback: CallbackQuery, session: AsyncSession):
    review_id = int(callback.data.split("_")[3])
    review = await session.get(Review, review_id)
    if not review:
        await callback.answer("❌ Отзыв уже удалён.", show_alert=True)
        return
    await session.execute(delete(WheelSpin).where(WheelSpin.review_id == review.id))
    await session.delete(review)
    await session.commit()
    await callback.answer("🗑 Отзыв удалён.", show_alert=True)
    await admin_reviews_list(callback, session)


# --- СОЗДАНИЕ ПРОМОКОДА АДМИНОМ ---
@router.callback_query(F.data == "admin_create_promo", F.from_user.id.in_(ADMIN_IDS))
async def admin_create_promo_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_promo_type)
    await callback.message.edit_text(
        "🎟 **Создание промокода**\n\nВыберите тип промокода:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Скидка в %", callback_data="promo_type_discount")],
            [InlineKeyboardButton(text="💰 Бонус на баланс", callback_data="promo_type_balance")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
        ]),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "promo_type_discount", F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_type_discount(callback: CallbackQuery, state: FSMContext):
    await state.update_data(promo_type="discount")
    await state.set_state(AdminStates.waiting_for_promo_code)
    await callback.message.edit_text(
        "💸 **Скидочный промокод**\n\n"
        "Введите код. Разрешены латинские буквы, цифры, `-` и `_`.",
        reply_markup=cancel_kb("admin_panel"),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "promo_type_balance", F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_type_balance(callback: CallbackQuery, state: FSMContext):
    await state.update_data(promo_type="balance")
    await state.set_state(AdminStates.waiting_for_promo_code)
    await callback.message.edit_text(
        "💰 **Промокод на баланс**\n\n"
        "Введите код. Разрешены латинские буквы, цифры, `-` и `_`.",
        reply_markup=cancel_kb("admin_panel"),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_promo_code, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_code_entered(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Введите текст промокода.")
        return

    code = normalize_promo_code(message.text)
    if not code:
        await message.answer(
            f"❌ Некорректный код. Используйте латинские буквы, цифры, `-` и `_`, максимум {MAX_PROMO_CODE_LENGTH} символов.",
            reply_markup=cancel_kb("admin_panel"),
            parse_mode="Markdown"
        )
        return

    existing = await session.scalar(select(PromoCode).where(func.upper(PromoCode.code) == code))
    if existing:
        await message.answer(
            f"❌ Промокод `{escape_md(code)}` уже существует. Введите другой:",
            reply_markup=cancel_kb("admin_panel"),
            parse_mode="Markdown"
        )
        return

    data = await state.get_data()
    promo_type = data.get("promo_type", "balance")
    await state.update_data(code=code, promo_type=promo_type)

    if promo_type == "discount":
        await state.set_state(AdminStates.waiting_for_promo_percent)
        await message.answer(
            "💸 **Шаг 2/4**\n\n"
            "Введите размер скидки от **1 до 100%**.\n"
            "Например: `10`, `15`, `25`.",
            reply_markup=cancel_kb("admin_panel"),
            parse_mode="Markdown"
        )
    else:
        await state.set_state(AdminStates.waiting_for_promo_amount)
        await message.answer(
            "💰 **Шаг 2/3**\n\nВведите сумму бонуса в гривнах. Например: `100` или `25.50`.",
            reply_markup=cancel_kb("admin_panel"),
            parse_mode="Markdown"
        )


@router.message(AdminStates.waiting_for_promo_percent, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_percent_entered(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Введите процент числом.")
        return
    raw = message.text.strip().replace(",", ".")
    try:
        percent = float(raw)
    except ValueError:
        percent = -1
    if not math.isfinite(percent) or percent < 1 or percent > 100:
        await message.answer("❌ Процент должен быть от 1 до 100.", reply_markup=cancel_kb("admin_panel"))
        return

    await state.update_data(discount_percent=round(percent, 2))
    await state.set_state(AdminStates.waiting_for_promo_product)

    result = await session.execute(
        select(Product, Software).join(Software, Product.software_id == Software.id).order_by(Software.name.asc(), Product.id.asc())
    )
    rows = result.all()
    buttons = [[InlineKeyboardButton(text="🌐 Все товары", callback_data="promo_product_all")]]
    for product, software in rows[:80]:
        buttons.append([InlineKeyboardButton(
            text=f"📦 {software.name} • {product.duration} — {product.price:.2f} грн",
            callback_data=f"promo_product_{product.id}"
        )])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")])

    await message.answer(
        "📦 **Шаг 3/4 — выберите товар**\n\n"
        "Скидка может действовать на весь каталог или только на один конкретный тариф.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("promo_product_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_product_selected(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if callback.data == "promo_product_all":
        product_id = None
        target_text = "все товары"
    else:
        try:
            product_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.answer("❌ Неверный товар.", show_alert=True)
            return
        product = await session.get(Product, product_id)
        if not product:
            await callback.answer("❌ Товар не найден.", show_alert=True)
            return
        software = await session.get(Software, product.software_id)
        target_text = f"{software.name if software else 'Товар'} • {product.duration}"

    await state.update_data(product_id=product_id, promo_product_text=target_text)
    await state.set_state(AdminStates.waiting_for_promo_uses)
    await callback.message.edit_text(
        f"🎟 **Шаг 4/4**\n\n"
        f"💸 Скидка: `{float((await state.get_data()).get('discount_percent', 0)):.0f}%`\n"
        f"📦 Действует: **{escape_md(target_text)}**\n\n"
        "Введите максимальное количество активаций, например `100`.",
        reply_markup=cancel_kb("admin_panel"),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_promo_amount, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_amount_entered(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Введите сумму числом.")
        return

    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer(
            f"❌ Некорректная сумма. Введите положительное число не больше {MAX_AMOUNT:.0f} грн.",
            reply_markup=cancel_kb("admin_panel")
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(AdminStates.waiting_for_promo_uses)
    await message.answer(
        "🎟 **Создание промокода (3/3)**\n\n"
        "Введите максимальное количество активаций, например `100`.",
        reply_markup=cancel_kb("admin_panel")
    )


@router.message(AdminStates.waiting_for_promo_uses, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_uses_entered(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите целое положительное число активаций, например `100`.", reply_markup=cancel_kb("admin_panel"))
        return

    uses = int(message.text.strip())
    if uses <= 0 or uses > 1_000_000:
        await message.answer("❌ Количество активаций должно быть от 1 до 1 000 000.", reply_markup=cancel_kb("admin_panel"))
        return

    data = await state.get_data()
    code = normalize_promo_code(data.get("code"))
    promo_type = data.get("promo_type", "balance")
    amount = data.get("amount", 0.0)
    discount_percent = data.get("discount_percent")
    product_id = data.get("product_id")
    product_text = data.get("promo_product_text", "все товары")

    if not code:
        await state.clear()
        await message.answer("⚠️ Сессия создания промокода устарела.", reply_markup=cancel_kb("admin_panel"))
        return

    try:
        existing = await session.scalar(select(PromoCode).where(func.upper(PromoCode.code) == code))
        if existing:
            await message.answer(f"❌ Промокод `{escape_md(code)}` уже существует.", reply_markup=cancel_kb("admin_panel"), parse_mode="Markdown")
            await state.clear()
            return

        if promo_type == "discount":
            if discount_percent is None or not (1 <= float(discount_percent) <= 100):
                await message.answer("❌ Некорректный процент скидки.", reply_markup=cancel_kb("admin_panel"))
                await state.clear()
                return
            promo = PromoCode(
                code=code,
                amount=0.0,
                uses_left=uses,
                promo_type="discount",
                discount_percent=float(discount_percent),
                product_id=product_id,
            )
        else:
            promo = PromoCode(
                code=code,
                amount=float(amount),
                uses_left=uses,
                promo_type="balance",
            )

        session.add(promo)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await message.answer("❌ Промокод с таким кодом уже существует.", reply_markup=cancel_kb("admin_panel"))
        await state.clear()
        return
    except SQLAlchemyError as e:
        await session.rollback()
        logger.exception("Ошибка создания промокода", exc_info=e)
        await message.answer("⚠️ Не удалось сохранить промокод в БД.", reply_markup=cancel_kb("admin_panel"))
        await state.clear()
        return

    await state.clear()
    if promo_type == "discount":
        result_text = (
            f"✅ **Скидочный промокод создан!**\n\n"
            f"🎟 Код: `{escape_md(code)}`\n"
            f"💸 Скидка: **{float(discount_percent):.0f}%**\n"
            f"📦 Товар: **{escape_md(product_text)}**\n"
            f"🔢 Активаций: `{uses}`"
        )
    else:
        result_text = (
            f"✅ **Промокод на баланс создан!**\n\n"
            f"🎟 Код: `{escape_md(code)}`\n"
            f"💰 Бонус: `{float(amount):.2f} грн`\n"
            f"🔢 Активаций: `{uses}`"
        )

    await message.answer(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎟 Все промокоды", callback_data="admin_promo_list")],
            [InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")],
        ]),
        parse_mode="Markdown"
    )


# --- СПИСОК И УДАЛЕНИЕ ПРОМОКОДОВ ---
@router.callback_query(F.data == "admin_promo_list", F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data.startswith("admin_promo_page_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_list_handler(callback: CallbackQuery, session: AsyncSession):
    page = 0
    if callback.data.startswith("admin_promo_page_"):
        page = int(callback.data.split("_")[3])

    per_page = 8
    offset = page * per_page

    total_promos = await session.scalar(select(func.count(PromoCode.id))) or 0

    if total_promos == 0:
        await callback.message.edit_text(
            "📋 **Промокоды**\n\nПромокодов пока не создано.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin_create_promo")],
                [InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")],
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
        return

    usage_count = (
        select(func.count(PromoCodeUsage.id))
        .where(PromoCodeUsage.promo_id == PromoCode.id)
        .correlate(PromoCode)
        .scalar_subquery()
    )
    result = await session.execute(
        select(PromoCode, usage_count.label("used_count"), Software.name, Product.duration)
        .outerjoin(Product, Product.id == PromoCode.product_id)
        .outerjoin(Software, Software.id == Product.software_id)
        .order_by(PromoCode.id.desc())
        .offset(offset)
        .limit(per_page)
    )
    rows = result.all()

    text_msg = f"📋 **Промокоды** — страница {page + 1}\nВсего: `{total_promos}`\n━━━━━━━━━━━━━━━━━━━\n"
    buttons = []

    for p, used_count, sw_name, duration in rows:
        used_count = int(used_count or 0)
        status_icon = "🟢" if p.uses_left > 0 else "🔴"
        if p.promo_type == "discount":
            target = "все товары" if p.product_id is None else f"{sw_name or 'Товар'} • {duration or ''}"
            text_msg += (
                f"{status_icon} **{escape_md(p.code)}** — 💸 **{float(p.discount_percent or 0):.0f}%**\n"
                f"📦 `{escape_md(target)}` · использовано: `{used_count}` · осталось: `{p.uses_left}`\n"
            )
        else:
            distributed = used_count * float(p.amount)
            text_msg += (
                f"{status_icon} **{escape_md(p.code)}** — 💰 `{float(p.amount):.2f} грн`\n"
                f"👥 использовано: `{used_count}` · осталось: `{p.uses_left}` · выдано: `{distributed:.2f} грн`\n"
            )
        buttons.append([
            InlineKeyboardButton(text=f"📊 {p.code}", callback_data=f"admin_promo_stats_{p.id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"confirm_del_promo_{p.id}"),
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_promo_page_{page - 1}"))
    if offset + per_page < total_promos:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_promo_page_{page + 1}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin_create_promo")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_promo_stats_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_stats_handler(callback: CallbackQuery, session: AsyncSession):
    try:
        promo_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректный промокод.", show_alert=True)
        return

    result = await session.execute(
        select(PromoCode, Software.name, Product.duration)
        .outerjoin(Product, Product.id == PromoCode.product_id)
        .outerjoin(Software, Software.id == Product.software_id)
        .where(PromoCode.id == promo_id)
    )
    row = result.first()
    if not row:
        await callback.answer("❌ Промокод не найден.", show_alert=True)
        return
    promo, sw_name, duration = row

    total_used = int(await session.scalar(select(func.count(PromoCodeUsage.id)).where(PromoCodeUsage.promo_id == promo.id)) or 0)
    total_discount = float(await session.scalar(
        select(func.coalesce(func.sum(PromoCodeUsage.discount_amount), 0)).where(PromoCodeUsage.promo_id == promo.id)
    ) or 0)

    target_text = "все товары" if promo.product_id is None else f"{sw_name or 'Товар'} • {duration or ''}"

    if promo.promo_type == "discount":
        lines = [
            "📊 **Статистика промокода**",
            f"🎟 Код: `{escape_md(promo.code)}`",
            f"💸 Скидка: **{float(promo.discount_percent or 0):.0f}%**",
            f"📦 Действует: **{escape_md(target_text)}**",
            f"👥 Использовано: `{total_used}`",
            f"🟢 Осталось: `{promo.uses_left}`",
            f"💸 Всего скидок выдано: `{total_discount:.2f} грн`",
            "━━━━━━━━━━━━━━━━━━━",
        ]
    else:
        total_bonus = total_used * float(promo.amount)
        lines = [
            "📊 **Статистика промокода**",
            f"🎟 Код: `{escape_md(promo.code)}`",
            f"💰 Бонус: `{float(promo.amount):.2f} грн`",
            f"👥 Использовано: `{total_used}`",
            f"🟢 Осталось: `{promo.uses_left}`",
            f"📤 Выдано бонусов: `{total_bonus:.2f} грн`",
            "━━━━━━━━━━━━━━━━━━━",
        ]

    recent_res = await session.execute(
        select(PromoCodeUsage, User.username)
        .outerjoin(User, User.id == PromoCodeUsage.user_id)
        .where(PromoCodeUsage.promo_id == promo.id)
        .order_by(PromoCodeUsage.used_at.desc())
        .limit(10)
    )
    recent = recent_res.all()
    if recent:
        lines.append("👥 **Последние активации:**")
        for usage, username in recent:
            user_label = f"@{escape_md(username)}" if username else f"ID `{usage.user_id}`"
            date_label = usage.used_at.strftime("%d.%m.%Y %H:%M") if usage.used_at else "—"
            extra = f" · скидка `-{usage.discount_amount:.2f} грн`" if promo.promo_type == "discount" and usage.discount_amount is not None else ""
            lines.append(f"• {user_label} · `{date_label}`{extra}")
    else:
        lines.append("👥 Использований пока нет.")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить промокод", callback_data=f"confirm_del_promo_{promo.id}")],
            [InlineKeyboardButton(text="🔙 К списку промокодов", callback_data="admin_promo_list")],
        ]),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_del_promo_"), F.from_user.id.in_(ADMIN_IDS))
async def confirm_delete_promo(callback: CallbackQuery, session: AsyncSession):
    promo_id = int(callback.data.split("_")[3])
    promo = await session.get(PromoCode, promo_id)
    if not promo:
        await callback.answer("❌ Промокод не найден или уже был удален!", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить промокод", callback_data=f"do_del_promo_{promo_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_promo_list")]
    ])
    if promo.promo_type == "discount":
        promo_desc = f"скидка {float(promo.discount_percent or 0):.0f}%"
    else:
        promo_desc = f"бонус {float(promo.amount):.2f} грн"

    await callback.message.edit_text(
        f"⚠️ **Подтверждение удаления**\n\n"
        f"Удалить промокод `{escape_md(promo.code)}` ({promo_desc}, осталось активаций: `{promo.uses_left}`)?\n\n"
        f"*История совершённых активаций сохранится в журнале покупок/баланса.*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("do_del_promo_"), F.from_user.id.in_(ADMIN_IDS))
async def do_delete_promo(callback: CallbackQuery, session: AsyncSession):
    promo_id = int(callback.data.split("_")[3])
    promo = await session.get(PromoCode, promo_id)
    if not promo:
        await callback.answer("❌ Промокод не найден или уже был удален!", show_alert=True)
        return

    # Сначала удаляем записи об активациях (нет каскада на уровне ORM-связи),
    # затем сам промокод, чтобы не упереться в FK-констрейнт.
    await session.execute(delete(PromoCodeUsage).where(PromoCodeUsage.promo_id == promo_id))
    await session.delete(promo)
    await session.commit()

    await callback.answer("✅ Промокод удален!", show_alert=True)
    await admin_promo_list_handler(callback, session)

# --- 📢 ПРОДВИНУТАЯ РАССЫЛКА ---
@router.callback_query(F.data == "admin_start_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_broadcast_audience)
    await callback.message.edit_text(
        "📢 **Новая рассылка**\n\nВыберите аудиторию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Все", callback_data="broadcast_audience_all")],
            [InlineKeyboardButton(text="🛒 Только покупатели", callback_data="broadcast_audience_buyers")],
            [InlineKeyboardButton(text="🌱 Без покупок", callback_data="broadcast_audience_nonbuyers")],
            [InlineKeyboardButton(text="💰 С балансом", callback_data="broadcast_audience_balance")],
            [InlineKeyboardButton(text="🔙 В dashboard", callback_data="admin_panel")],
        ]), parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("broadcast_audience_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_choose_audience(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    audience = callback.data.replace("broadcast_audience_", "", 1)
    if audience not in BROADCAST_AUDIENCES:
        await callback.answer("❌ Неизвестная аудитория.", show_alert=True)
        return
    count = len(await get_broadcast_user_ids(session, audience))
    await state.update_data(broadcast_audience=audience)
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    await callback.message.edit_text(
        f"📢 **Аудитория:** {BROADCAST_AUDIENCES[audience]}\n"
        f"👥 Получателей: `{count}`\n\n"
        "Отправьте любое сообщение: текст, фото, видео или документ.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_broadcast_text, F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_prepare(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    audience = data.get("broadcast_audience", "all")
    if audience not in BROADCAST_AUDIENCES:
        audience = "all"
    count = len(await get_broadcast_user_ids(session, audience))
    if count == 0:
        await state.clear()
        await message.answer("⚠️ В этой аудитории нет пользователей.", reply_markup=admin_back_kb())
        return
    await state.update_data(broadcast_chat_id=message.chat.id, broadcast_message_id=message.message_id, broadcast_count=count)
    try:
        await message.send_copy(chat_id=message.chat.id)
    except Exception:
        pass
    await message.answer(
        "📢 **Предпросмотр выше**\n\n"
        f"🎯 {BROADCAST_AUDIENCES[audience]}\n"
        f"👥 Получателей: `{count}`\n\n"
        "Запустить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Запустить", callback_data="broadcast_confirm")],
            [InlineKeyboardButton(text="🔄 Выбрать аудиторию", callback_data="admin_start_broadcast")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
        ]), parse_mode="Markdown"
    )


async def _run_broadcast(bot: Bot, status_msg: Message, chat_id: int, message_id: int, audience: str, user_ids: list[int]) -> None:
    # Рассылка идёт в фоне: раньше хендлер висел всё время доставки и держал
    # открытую транзакцию БД (соединение из пула) на минуты.
    total = len(user_ids)
    audience_label = BROADCAST_AUDIENCES.get(audience, audience)
    success = 0
    for index, uid in enumerate(user_ids, start=1):
        for attempt in range(3):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=message_id)
                success += 1
                break
            except TelegramRetryAfter as e:
                if attempt >= 2:
                    break
                await asyncio.sleep(min(float(e.retry_after), 30.0))
            except TelegramForbiddenError:
                break
            except Exception as e:
                logger.warning(f"Broadcast failed uid={uid}: {e}")
                break
        await asyncio.sleep(0.06)
        if index % 100 == 0:
            try:
                await status_msg.edit_text(
                    f"📢 **Рассылка выполняется**\n\n📨 Обработано: `{index}/{total}`\n"
                    f"✅ Доставлено: `{success}`\n❌ Не доставлено: `{index - success}`", parse_mode="Markdown"
                )
            except Exception:
                pass
    try:
        await status_msg.edit_text(
            "✅ **РАССЫЛКА ЗАВЕРШЕНА**\n━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 {audience_label}\n"
            f"👥 Всего: `{total}`\n"
            f"✅ Доставлено: `{success}`\n"
            f"❌ Не доставлено: `{total - success}`",
            reply_markup=admin_back_kb(), parse_mode="Markdown"
        )
    except Exception:
        pass


@router.callback_query(F.data == "broadcast_confirm", F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_execute(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    chat_id = data.get("broadcast_chat_id")
    message_id = data.get("broadcast_message_id")
    audience = data.get("broadcast_audience", "all")
    # ФИКС: состояние сбрасываем СРАЗУ — двойной тап «Запустить» раньше мог
    # запустить две одинаковые рассылки подряд (второй клик успевал прочитать черновик).
    await state.clear()
    if not chat_id or not message_id:
        await callback.answer("❌ Черновик устарел. Создайте рассылку заново.", show_alert=True)
        return
    user_ids = await get_broadcast_user_ids(session, audience)
    await callback.message.edit_text(
        f"⏳ **Рассылка запущена**\n\n🎯 {BROADCAST_AUDIENCES.get(audience, audience)}\n"
        f"👥 Получателей: `{len(user_ids)}`\n\nДоставка выполняется…", parse_mode="Markdown"
    )
    await callback.answer()
    spawn_background(_run_broadcast(bot, callback.message, chat_id, message_id, audience, user_ids))


# --- УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЕМ ---
@router.callback_query(F.data == "admin_user_manage", F.from_user.id.in_(ADMIN_IDS))
async def admin_user_manage_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_user_id)
    await callback.message.edit_text(
        "👤 **Управление пользователем**\n\nВведите Telegram ID или username (например `@ivan123`) пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_user_id, F.from_user.id.in_(ADMIN_IDS))
async def admin_user_info_show(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text or not message.text.strip():
        await message.answer("❌ Введите ID (числом) или username пользователя.")
        return

    # НОВОЕ: поиск не только по Telegram ID, но и по username
    user = await find_user_by_identifier(session, message.text)
    if not user:
        await message.answer(
            "❌ Пользователь не найден в базе данных (проверьте ID или username).",
            reply_markup=cancel_kb("admin_panel")
        )
        return

    target_id = user.id
    await state.update_data(target_user_id=target_id)
    purchases_cnt = await session.scalar(select(func.count(Purchase.id)).where(Purchase.user_id == target_id))

    username_display = f"@{escape_md(user.username)}" if user.username else "отсутствует"
    info_text = (
        f"👤 **Информация о пользователе**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: `{user.id}`\n"
        f"👤 Username: {username_display}\n"
        f"💰 Баланс: `{user.balance:.2f} грн`\n"
        f"🛍 Покупок: `{purchases_cnt}`\n"
        f"🚫 Заблокирован: `{'Да' if user.is_banned else 'Нет'}`\n"
        f"🌐 Язык: `{user.language}`\n"
        f"📅 Регистрация: `{user.created_at.strftime('%d.%m.%Y %H:%M')}`"
    )

    ban_btn_text = "🔓 Разблокировать" if user.is_banned else "🔒 Заблокировать"
    ban_action = f"admin_unban_{target_id}" if user.is_banned else f"admin_ban_{target_id}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Изменить баланс", callback_data=f"admin_ch_bal_{target_id}")],
        [InlineKeyboardButton(text="📜 История баланса", callback_data=f"admin_bal_history_{target_id}_0")],
        [InlineKeyboardButton(text=ban_btn_text, callback_data=ban_action)],
        [InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]
    ])
    await state.clear()
    await message.answer(info_text, reply_markup=keyboard, parse_mode="Markdown")

# --- НОВОЕ: ИСТОРИЯ ОПЕРАЦИЙ С БАЛАНСОМ КОНКРЕТНОГО ПОЛЬЗОВАТЕЛЯ ---
BALANCE_LOG_REASON_LABELS = {
    "purchase": "🛒 Покупка",
    "deposit_approved": "📥 Пополнение (одобрено)",
    "withdrawal_request": "📤 Заявка на вывод",
    "withdrawal_rejected_refund": "↩️ Возврат (вывод отклонён)",
    "referral_bonus": "🤝 Реферальный бонус",
    "promo_code": "🎟 Промокод",
    "wheel_bonus": "🎰 Приз колеса фортуны",
    "admin_adjust": "🛠 Ручная корректировка (админ)",
}

@router.callback_query(F.data.startswith("admin_bal_history_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_balance_history(callback: CallbackQuery, session: AsyncSession):
    parts = callback.data.split("_")
    target_id = int(parts[3])
    page = int(parts[4])

    user = await session.get(User, target_id)
    if not user:
        await callback.answer("❌ Пользователь не найден.", show_alert=True)
        return

    per_page = 8
    total = await session.scalar(select(func.count(BalanceLog.id)).where(BalanceLog.user_id == target_id))
    total = total or 0
    total_pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, total_pages - 1))
    offset = page * per_page

    res = await session.execute(
        select(BalanceLog)
        .where(BalanceLog.user_id == target_id)
        .order_by(BalanceLog.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    logs = res.scalars().all()

    username_display = f"@{escape_md(user.username)}" if user.username else "без юзернейма"
    header = (
        f"📜 **История баланса**\n"
        f"👤 `{target_id}` ({username_display})\n"
        f"💰 Текущий баланс: `{user.balance:.2f} грн`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    if not logs:
        body = "Операций пока нет."
    else:
        lines = []
        for log in logs:
            sign = "+" if log.amount >= 0 else ""
            reason_label = BALANCE_LOG_REASON_LABELS.get(log.reason, f"❔ {log.reason}")
            date_str = log.created_at.strftime("%d.%m.%Y %H:%M")
            related = f" (ID: `{log.related_id}`)" if log.related_id is not None else ""
            lines.append(
                f"{reason_label}{related}\n"
                f"{sign}`{log.amount:.2f} грн` → баланс `{log.balance_after:.2f} грн`\n"
                f"🕒 _{date_str}_"
            )
        body = "\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n\n".join(lines)

    text_msg = header + body

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_bal_history_{target_id}_{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_bal_history_{target_id}_{page + 1}"))

    buttons = []
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        text_msg,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_ban_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_ban_user(callback: CallbackQuery, session: AsyncSession):
    uid = int(callback.data.split("_")[2])
    user = await session.get(User, uid)
    if user:
        user.is_banned = True
        await session.commit()
        await callback.answer("✅ Пользователь заблокирован!", show_alert=True)
    else:
        await callback.answer("❌ Пользователь не найден.", show_alert=True)

@router.callback_query(F.data.startswith("admin_unban_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_unban_user(callback: CallbackQuery, session: AsyncSession):
    uid = int(callback.data.split("_")[2])
    user = await session.get(User, uid)
    if user:
        user.is_banned = False
        await session.commit()
        await callback.answer("✅ Пользователь разблокирован!", show_alert=True)
    else:
        await callback.answer("❌ Пользователь не найден.", show_alert=True)

@router.callback_query(F.data.startswith("admin_ch_bal_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_change_balance_start(callback: CallbackQuery, state: FSMContext):
    uid = int(callback.data.split("_")[3])
    await state.update_data(target_user_id=uid)
    await state.set_state(AdminStates.waiting_for_user_balance_change)
    await callback.message.edit_text(
        "💰 **Изменение баланса**\n\n"
        "Введите новое значение баланса (например `500`) "
        "или относительное изменение со знаком (например `+100` / `-50`):",
        reply_markup=cancel_kb("admin_panel"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_user_balance_change, F.from_user.id.in_(ADMIN_IDS))
async def admin_change_balance_finish(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    uid = data.get("target_user_id")
    if not message.text:
        return

    raw = message.text.strip().replace(",", ".")
    is_relative = raw.startswith("+") or raw.startswith("-")

    try:
        val = float(raw)
    except ValueError:
        await message.answer("❌ Введите корректное число.")
        return

    if not math.isfinite(val) or abs(val) > MAX_AMOUNT:
        await message.answer("❌ Введите корректное конечное число в разумных пределах.")
        return

    # ФИКС: реализована заявленная в тексте логика +/- для относительного изменения баланса.
    # Раньше в любом случае происходила полная перезапись (user.balance = val),
    # что не соответствовало подсказке "+100 / -50".
    user_res = await session.execute(select(User).where(User.id == uid).with_for_update().execution_options(populate_existing=True))
    user = user_res.scalar_one_or_none()
    if not user:
        await message.answer("❌ Пользователь не найден.")
        await state.clear()
        return

    old_balance = user.balance

    if is_relative:
        new_balance = round_money(user.balance + val)
        if new_balance < 0:
            await message.answer(f"❌ Итоговый баланс не может быть отрицательным (получилось бы {new_balance:.2f} грн).")
            return
        user.balance = new_balance
    else:
        if val < 0:
            await message.answer("❌ Баланс не может быть отрицательным.")
            return
        user.balance = round_money(val)

    # НОВОЕ: логируем ручную корректировку баланса администратором
    delta = round_money(user.balance - old_balance)
    log_balance_change(session, user.id, delta, "admin_adjust", user.balance, related_id=message.from_user.id)

    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Баланс пользователя `{uid}` успешно изменен. Новый баланс: `{user.balance:.2f} грн`.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# --- ИЗМЕНЕНИЕ ЦЕНЫ БЕЗ РУЧНЫХ ЗАПРОСОВ К БД ---
@router.callback_query(F.data == "admin_edit_price_select", F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data.regexp(r"^admin_price_page_[0-9]+$"), F.from_user.id.in_(ADMIN_IDS))
async def admin_edit_price_select_handler(callback: CallbackQuery, session: AsyncSession):
    page = int(callback.data.rsplit("_", 1)[1]) if callback.data.startswith("admin_price_page_") else 0
    result = await session.execute(
        select(Product, Software)
        .join(Software, Product.software_id == Software.id)
        .order_by(Software.name.asc(), Product.id.asc())
    )
    rows = result.all()
    page, total_pages, offset = page_bounds(len(rows), page, CATALOG_PAGE_SIZE)
    buttons = [
        [InlineKeyboardButton(
            text=f"✏️ {software.name[:32]} · {product.duration} · {product.price:.2f}",
            callback_data=f"admin_price_prod_{product.id}",
        )]
        for product, software in rows[offset:offset + CATALOG_PAGE_SIZE]
    ]
    nav = pagination_row("admin_price_page_", page, total_pages)
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Товары и склад", callback_data="admin_catalog")])
    await callback.message.edit_text(
        "✏️ **Изменение цены**\n\nВыберите тариф:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_price_prod_[0-9]+$"), F.from_user.id.in_(ADMIN_IDS))
async def admin_edit_price_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    product_id = int(callback.data.rsplit("_", 1)[1])
    product = await session.get(Product, product_id)
    if not product:
        await callback.answer("❌ Тариф не найден.", show_alert=True)
        return
    software = await session.get(Software, product.software_id)
    await state.update_data(price_product_id=product_id)
    await state.set_state(AdminStates.waiting_for_new_price)
    await callback.message.edit_text(
        f"✏️ **Новая цена**\n\n📦 {escape_md(software.name if software else 'Товар')} · `{escape_md(product.duration)}`\n"
        f"Текущая цена: `{product.price:.2f} грн`\n\nВведите новую цену числом:",
        reply_markup=cancel_kb("admin_catalog"),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_new_price, F.from_user.id.in_(ADMIN_IDS))
async def admin_edit_price_finish(message: Message, state: FSMContext, session: AsyncSession):
    price = parse_positive_amount(message.text)
    if price is None:
        await message.answer("❌ Введите корректную положительную цену.", reply_markup=cancel_kb("admin_catalog"))
        return
    data = await state.get_data()
    product_id = data.get("price_product_id")
    product = await lock_row(session, Product, product_id)
    if not product:
        await state.clear()
        await message.answer("❌ Тариф больше не существует.", reply_markup=admin_back_kb("admin_catalog"))
        return
    old_price = product.price
    product.price = price
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ **Цена обновлена**\n\nБыло: `{old_price:.2f} грн`\nСтало: **{price:.2f} грн**\n\nУже оформленные заказы и история покупок не изменены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Склад", callback_data="admin_stock")],
            [InlineKeyboardButton(text="🔙 Товары и склад", callback_data="admin_catalog")],
        ]),
        parse_mode="Markdown",
    )


# --- СОЗДАНИЕ ТАРИФОВ И УПРАВЛЕНИЕ СОФТОМ ---
@router.callback_query(F.data == "admin_add_game", F.from_user.id.in_(ADMIN_IDS))
async def admin_add_game_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.waiting_for_game_name)
    await callback.message.edit_text(
        "➕ **Добавление игры**\n\nВведите название игры (например: `PUBG MOBILE`).\n"
        "После сохранения сможете добавить к ней Android/iOS софт или софт без выбора ОС.",
        reply_markup=cancel_kb("admin_catalog"),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_game_name, F.from_user.id.in_(ADMIN_IDS))
async def admin_add_game_save(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text or not message.text.strip():
        await message.answer("❌ Введите название игры текстом.")
        return
    name = message.text.strip()
    if len(name) > MAX_SOFTWARE_NAME_LENGTH:
        await message.answer(f"❌ Название слишком длинное. Максимум {MAX_SOFTWARE_NAME_LENGTH} символов.")
        return

    existing = await session.scalar(
        select(Game).where(func.lower(func.trim(Game.name)) == name.casefold())
    )
    if existing:
        await state.clear()
        await message.answer(
            f"ℹ️ Игра **{escape_md(existing.name)}** уже существует. Можно сразу добавить к ней софт.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить софт к этой игре", callback_data=f"admin_add_sw_game_{existing.id}")],
                [InlineKeyboardButton(text="🔙 В товары и склад", callback_data="admin_catalog")],
            ]),
            parse_mode="Markdown",
        )
        return

    game = Game(name=name)
    session.add(game)
    try:
        await session.commit()
        game_id = game.id
    except IntegrityError:
        await session.rollback()
        await message.answer("❌ Такая игра уже есть. Откройте список игр и добавьте софт в существующую.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        f"✅ Игра **{escape_md(name)}** добавлена.\n\nТеперь добавьте к ней конкретный софт — его название вводится отдельно.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить софт к игре", callback_data=f"admin_add_sw_game_{game_id}")],
            [InlineKeyboardButton(text="🎮 Добавить ещё игру", callback_data="admin_add_game")],
            [InlineKeyboardButton(text="🔙 В товары и склад", callback_data="admin_catalog")],
        ]),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "admin_add_sw", F.from_user.id.in_(ADMIN_IDS))
async def add_sw_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.clear()
    result = await session.execute(select(Game).order_by(Game.name.asc(), Game.id.asc()))
    games = result.scalars().all()
    if not games:
        await callback.answer("Сначала нажмите «Добавить игру» и введите её название.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"🎮 {game.name[:58]}", callback_data=f"sw_game_sel_{game.id}")] for game in games]
    buttons.append([InlineKeyboardButton(text="🔙 В товары и склад", callback_data="admin_catalog")])
    await callback.message.edit_text(
        "➕ **Добавление софта — шаг 1/3**\n\nСначала выберите игру, к которой относится софт:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_add_sw_game_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_add_software_for_specific_game(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    try:
        game_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("❌ Некорректная игра.", show_alert=True)
        return
    await _start_software_os_choice(callback, state, session, game_id)


@router.callback_query(F.data.startswith("sw_game_sel_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_software_game_selected(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    try:
        game_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("❌ Некорректная игра.", show_alert=True)
        return
    await _start_software_os_choice(callback, state, session, game_id)


async def _start_software_os_choice(callback: CallbackQuery, state: FSMContext, session: AsyncSession, game_id: int):
    game = await session.get(Game, game_id)
    if not game:
        await callback.answer("❌ Игра не найдена.", show_alert=True)
        return
    await state.clear()
    await state.update_data(game_id=game.id, game_name=game.name)
    await state.set_state(AdminStates.waiting_for_software_platform)
    buttons = [
        [InlineKeyboardButton(text="🤖 Android", callback_data="sw_os_android")],
        [InlineKeyboardButton(text="🍏 iOS", callback_data="sw_os_ios")],
        [InlineKeyboardButton(text="⏭ Пропустить (не спрашивать ОС у покупателя)", callback_data="sw_os_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_catalog")],
    ]
    await callback.message.edit_text(
        f"🎮 Игра: **{escape_md(game.name)}**\n\n"
        "➕ **Добавление софта — шаг 2/3**\nВыберите платформу. Если нажать «Пропустить», покупатель не увидит экран выбора ОС.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sw_os_"), F.from_user.id.in_(ADMIN_IDS))
async def add_sw_platform_chosen(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    game_id = data.get("game_id")
    current_state = await state.get_state()
    if not game_id or current_state != AdminStates.waiting_for_software_platform.state:
        await callback.answer("❌ Сценарий устарел. Начните добавление софта заново.", show_alert=True)
        await state.clear()
        return

    choice = callback.data.removeprefix("sw_os_")
    platform_name_by_choice = {"android": "Android", "ios": "iOS", "skip": NO_OS_PLATFORM_NAME}
    platform_name = platform_name_by_choice.get(choice)
    if not platform_name:
        await callback.answer("❌ Неизвестная платформа.", show_alert=True)
        return

    platform = await session.scalar(select(Platform).where(Platform.name == platform_name))
    if platform is None and choice == "skip":
        platform = Platform(name=NO_OS_PLATFORM_NAME)
        session.add(platform)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            platform = await session.scalar(select(Platform).where(Platform.name == NO_OS_PLATFORM_NAME))
    if not platform:
        await callback.answer("❌ Раздел ОС не найден. Перезапустите бота и попробуйте снова.", show_alert=True)
        return

    await state.update_data(platform_id=platform.id, platform_name=platform.name, os_choice=choice)
    await state.set_state(AdminStates.waiting_for_software_name)
    await callback.message.edit_text(
        f"🎮 Игра: **{escape_md(data.get('game_name', ''))}**\n"
        f"📱 Платформа: **{escape_md('Без выбора ОС' if choice == 'skip' else platform.name)}**\n\n"
        "➕ **Добавление софта — шаг 3/3**\n"
        "Теперь введите **название самого софта** (например: `Aim`, `Wallhack`, `Flexnote`).\n"
        "Это отдельное название, не название игры.",
        reply_markup=cancel_kb("admin_catalog"),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_software_name, F.from_user.id.in_(ADMIN_IDS))
async def save_software_name(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text or not message.text.strip():
        await message.answer("❌ Введите название софта текстом.")
        return
    name = message.text.strip()
    if len(name) > MAX_SOFTWARE_NAME_LENGTH:
        await message.answer(f"❌ Название слишком длинное. Максимум {MAX_SOFTWARE_NAME_LENGTH} символов. Введите другое:")
        return

    data = await state.get_data()
    game_id = data.get("game_id")
    platform_id = data.get("platform_id")
    if not game_id or not platform_id:
        await state.clear()
        await message.answer("❌ Не хватает данных игры/ОС. Начните добавление софта заново.")
        return

    duplicate = await session.scalar(
        select(Software).where(
            Software.game_id == game_id,
            Software.platform_id == platform_id,
            func.lower(func.trim(Software.name)) == name.casefold(),
        )
    )
    if duplicate:
        await state.clear()
        await message.answer(
            f"ℹ️ Софт **{escape_md(name)}** уже есть в этой игре и платформе. Можно сразу создать ему тариф.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать тариф", callback_data=f"prod_sw_sel_{duplicate.id}")],
                [InlineKeyboardButton(text="🔙 В товары и склад", callback_data="admin_catalog")],
            ]),
            parse_mode="Markdown",
        )
        return

    software = Software(game_id=game_id, platform_id=platform_id, name=name)
    session.add(software)
    try:
        await session.commit()
        software_id = software.id
    except IntegrityError:
        await session.rollback()
        await message.answer("❌ Не удалось сохранить софт из-за конфликта данных. Попробуйте ещё раз.")
        await state.clear()
        return

    game_name = data.get("game_name", "Игра")
    platform_name = data.get("platform_name", "")
    os_choice = data.get("os_choice")
    await state.clear()
    platform_label = "ОС пропускается" if os_choice == "skip" else platform_name
    await message.answer(
        f"✅ Софт **{escape_md(name)}** добавлен!\n"
        f"🎮 Игра: **{escape_md(game_name)}**\n"
        f"📱 Платформа: **{escape_md(platform_label)}**\n\n"
        "Теперь можно создать тариф: выбрать срок ключа и указать цену. После этого загрузите ключи для тарифа.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Добавить срок и цену", callback_data=f"prod_sw_sel_{software_id}")],
            [InlineKeyboardButton(text="➕ Добавить ещё софт", callback_data=f"admin_add_sw_game_{game_id}")],
            [InlineKeyboardButton(text="🔙 В товары и склад", callback_data="admin_catalog")],
        ]),
        parse_mode="Markdown",
    )


# --- ФОТО / ОПИСАНИЕ СОФТА ---
@router.callback_query(F.data == "admin_sw_media_select", F.from_user.id.in_(ADMIN_IDS))
async def admin_sw_media_select(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(
        select(Software, Platform.name)
        .join(Platform, Software.platform_id == Platform.id)
        .order_by(Software.name.asc(), Software.id.asc())
    )
    software_rows = res.all()
    if not software_rows:
        await callback.answer("❌ Сначала добавьте хотя бы один софт!", show_alert=True)
        return

    buttons = []
    for sw, platform_name in software_rows:
        mark = "🖼" if sw.photo_file_id else "🛡"
        platform_label = "без ОС" if platform_name == NO_OS_PLATFORM_NAME else platform_name
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {sw.name} · {platform_label}",
            callback_data=f"sw_media_sel_{sw.id}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        "🖼 **Фото/описание софта**\n\nВыберите софт:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("sw_media_sel_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_sw_media_start(callback: CallbackQuery, state: FSMContext):
    sw_id = int(callback.data.split("_")[3])
    await state.update_data(software_id=sw_id)
    await state.set_state(AdminStates.waiting_for_software_media)

    text_msg = (
        "🖼 **Фото/описание софта**\n\n"
        "Отправьте фото (можно с подписью — подпись станет текстом-описанием, "
        "который показывается над кнопками при выборе этого софта). "
        "Фото будет отображаться НАД текстом, как вы просили.\n\n"
        "Либо просто отправьте текст — тогда обновится только описание, без фото."
    )
    await callback.message.edit_text(text_msg, reply_markup=cancel_kb("admin_panel"), parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_software_media, F.from_user.id.in_(ADMIN_IDS), F.photo)
async def admin_sw_media_save_photo(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    sw_id = data.get("software_id")
    sw = await session.get(Software, sw_id)
    if not sw:
        await message.answer("❌ Софт не найден.")
        await state.clear()
        return

    sw.photo_file_id = message.photo[-1].file_id
    if message.caption:
        sw.description = message.caption.strip()[:1024]
    await session.commit()
    await state.clear()

    await message.answer(
        f"✅ Фото для `{escape_md(sw.name)}` сохранено" + (" вместе с описанием." if message.caption else "."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

@router.message(AdminStates.waiting_for_software_media, F.from_user.id.in_(ADMIN_IDS), F.text)
async def admin_sw_media_save_text(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    sw_id = data.get("software_id")
    sw = await session.get(Software, sw_id)
    if not sw:
        await message.answer("❌ Софт не найден.")
        await state.clear()
        return

    sw.description = message.text.strip()[:1024]
    await session.commit()
    await state.clear()

    await message.answer(
        f"✅ Описание для `{escape_md(sw.name)}` обновлено.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# --- ССЫЛКА НА ФАЙЛЫ (выдаётся покупателю вместе с ключом) ---
@router.callback_query(F.data == "admin_sw_link_select", F.from_user.id.in_(ADMIN_IDS))
async def admin_sw_link_select(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(select(Software))
    softwares = res.scalars().all()
    if not softwares:
        await callback.answer("❌ Сначала добавьте хотя бы один софт!", show_alert=True)
        return

    buttons = []
    for sw in softwares:
        mark = "🔗" if sw.files_link else "🛡"
        buttons.append([InlineKeyboardButton(text=f"{mark} {sw.name}", callback_data=f"sw_link_sel_{sw.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        "🔗 **Ссылка на файлы для софта**\n\nВыберите софт:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("sw_link_sel_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_sw_link_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    sw_id = int(callback.data.split("_")[3])
    sw = await session.get(Software, sw_id)
    if not sw:
        await callback.answer("❌ Софт не найден!", show_alert=True)
        return

    await state.update_data(software_id=sw_id)
    await state.set_state(AdminStates.waiting_for_software_files_link)

    current = f"\n\nТекущая ссылка: `{escape_md(sw.files_link)}`" if sw.files_link else ""
    text_msg = (
        f"🔗 **Ссылка на файлы для «{escape_md(sw.name)}»**{current}\n\n"
        "Пришлите ссылку на файлы (например, на облако/архив/инструкцию) — "
        "она будет автоматически добавляться в сообщение покупателю вместе "
        "с выданным ключом.\n\n"
        "Чтобы удалить ссылку — отправьте `-`."
    )
    await callback.message.edit_text(text_msg, reply_markup=cancel_kb("admin_panel"), parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_software_files_link, F.from_user.id.in_(ADMIN_IDS), F.text)
async def admin_sw_link_save(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    sw_id = data.get("software_id")
    sw = await session.get(Software, sw_id)
    if not sw:
        await message.answer("❌ Софт не найден.")
        await state.clear()
        return

    raw = message.text.strip()
    if raw == "-":
        sw.files_link = None
        result_text = f"✅ Ссылка для `{escape_md(sw.name)}` удалена."
    else:
        normalized_url = normalize_web_url(raw)
        if not normalized_url:
            await message.answer(
                "❌ Нужна корректная ссылка, начинающаяся с https:// или http:// (максимум 500 символов, без логина и пароля в URL)."
            )
            return
        sw.files_link = normalized_url
        result_text = f"✅ Ссылка для `{escape_md(sw.name)}` сохранена и теперь будет выдаваться вместе с ключом."

    await session.commit()
    await state.clear()

    await message.answer(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
# --- ПОПОЛНЕНИЕ АККАУНТОВ (быстрый доступ к загрузке ключей для раздела "Аккаунты") ---
@router.callback_query(F.data == "admin_topup_accounts", F.from_user.id.in_(ADMIN_IDS))
async def admin_topup_accounts_select(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(
        select(Product, Software)
        .join(Software, Product.software_id == Software.id)
        .join(Platform, Software.platform_id == Platform.id)
        .where(Platform.name.ilike("%аккаунт%"))
    )
    rows = res.all()
    if not rows:
        await callback.answer("❌ Пока нет ни одного раздела «Аккаунты» с тарифами.", show_alert=True)
        return

    buttons = []
    for p, sw in rows:
        keys_res = await session.execute(select(func.count(LicenseKey.id)).filter_by(product_id=p.id, is_sold=False))
        available = keys_res.scalar() or 0
        buttons.append([InlineKeyboardButton(
            text=f"🔋 {sw.name} — {p.duration} ({p.price:.2f} грн) · в наличии: {available}",
            callback_data=f"add_keys_prod_{p.id}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        "🔋 **Пополнение аккаунтов**\n\nВыберите позицию — затем пришлите аккаунты списком "
        "(каждый аккаунт с новой строки, например `login:password`):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "admin_add_prod", F.from_user.id.in_(ADMIN_IDS))
async def admin_add_prod_select_sw(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(
        select(Software, Platform.name, Game.name)
        .join(Platform, Software.platform_id == Platform.id)
        .outerjoin(Game, Software.game_id == Game.id)
        .order_by(Game.name.asc(), Software.name.asc(), Software.id.asc())
    )
    software_rows = res.all()
    if not software_rows:
        await callback.answer("❌ Сначала добавьте хотя бы один софт!", show_alert=True)
        return

    buttons = []
    for sw, platform_name, game_name in software_rows:
        platform_label = "без ОС" if platform_name == NO_OS_PLATFORM_NAME else platform_name
        # Telegram ограничивает длину текста inline-кнопки; оставляем идентификаторы читаемыми.
        game_label = (game_name or "Без игры")[:18]
        software_label = (sw.name or "Софт")[:22]
        platform_label = platform_label[:10]
        buttons.append([InlineKeyboardButton(
            text=f"🎮 {game_label} · 🛡 {software_label} · {platform_label}",
            callback_data=f"prod_sw_sel_{sw.id}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        "➕ **Создание тарифа (1/3)**\n\nВыберите софт, затем срок ключа и цену:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("prod_sw_sel_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_add_prod_duration(callback: CallbackQuery, state: FSMContext):
    sw_id = int(callback.data.split("_")[3])
    await state.update_data(software_id=sw_id)
    await state.set_state(AdminStates.waiting_for_duration)

    buttons = []
    for d in DURATIONS:
        buttons.append([InlineKeyboardButton(text=d, callback_data=f"prod_dur_sel_{d}")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")])

    await callback.message.edit_text(
        "➕ **Создание тарифа (2/3)**\n\nВыберите или введите длительность тарифа (например: `1 день`, `7 дней`, `30 дней` или `Навсегда`):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("prod_dur_sel_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_add_prod_duration_callback(callback: CallbackQuery, state: FSMContext):
    duration = normalize_duration(callback.data.replace("prod_dur_sel_", ""))
    await state.update_data(duration=duration)
    await state.set_state(AdminStates.waiting_for_price)

    await callback.message.edit_text(
        f"➕ **Создание тарифа (3/3)**\nВыбран срок ключа: `{duration}`\n\nВведите цену в гривнах (числом):" ,
        reply_markup=cancel_kb("admin_panel"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_duration, F.from_user.id.in_(ADMIN_IDS))
async def admin_add_prod_duration_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Введите текстовое значение для срока.")
        return
    duration = normalize_duration(message.text)
    if len(duration) > MAX_DURATION_LENGTH:
        await message.answer(
            f"❌ Слишком длинный текст ({len(duration)} символов). Максимум — {MAX_DURATION_LENGTH}. Введите другой:"
        )
        return
    await state.update_data(duration=duration)
    await state.set_state(AdminStates.waiting_for_price)
    await message.answer("➕ **Создание тарифа (3/3)**\n\nВведите цену в гривнах (числом):", reply_markup=cancel_kb("admin_panel"))

@router.message(AdminStates.waiting_for_price, F.from_user.id.in_(ADMIN_IDS))
async def admin_add_prod_finish(message: Message, state: FSMContext, session: AsyncSession):
    price = parse_positive_amount(message.text)
    if price is None:
        await message.answer("❌ Введите корректное положительное число для цены.")
        return

    data = await state.get_data()
    sw_id = data.get("software_id")
    duration = normalize_duration(data.get("duration", ""))

    existing = await session.execute(
        select(Product).where(
            Product.software_id == sw_id,
            Product.duration == duration,
        )
    )
    existing_product = existing.scalars().first()
    if existing_product:
        await state.clear()
        await message.answer(
            f"❌ У этого софта уже существует тариф `{escape_md(duration)}` "
            f"с ценой `{existing_product.price:.2f} грн`.\n\n"
            "Чтобы не создать два одинаковых тарифа, изменение существующей "
            "цены нужно делать в базе/отдельной функции редактирования.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]
            ),
            parse_mode="Markdown",
        )
        return

    product = Product(software_id=sw_id, duration=duration, price=price)
    session.add(product)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await state.clear()
        await message.answer(
            "⚠️ Не удалось создать тариф: конфликт данных. Попробуйте ещё раз.",
            reply_markup=cancel_kb("admin_panel"),
        )
        return
    await state.clear()

    sw = await session.get(Software, sw_id)
    sw_name = sw.name if sw else "Софт"

    await message.answer(
        f"✅ Тариф `{escape_md(sw_name)}` — `{escape_md(duration)}` по цене `{price:.2f} грн` успешно создан!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# --- УДАЛЕНИЕ СОФТА И ТАРИФОВ ---
async def detach_product_references(session: AsyncSession, product_ids: list[int]) -> None:
    # ФИКС: purchases.product_id и reviews.product_id — внешние ключи на products.id.
    # Удаление тарифа, по которому уже были покупки/отзывы, падало с
    # ForeignKeyViolationError. История не должна пропадать (в Purchase уже лежат
    # product_name / duration / price_paid на момент покупки), поэтому просто
    # обнуляем ссылку на удаляемый тариф — обе колонки nullable.
    if not product_ids:
        return
    await session.execute(
        update(Purchase).where(Purchase.product_id.in_(product_ids)).values(product_id=None)
    )
    await session.execute(
        update(Review).where(Review.product_id.in_(product_ids)).values(product_id=None)
    )
    # Скидочный промокод, привязанный к удаляемому тарифу, нельзя превращать
    # в глобальный. Отключаем его (uses_left=0) и только потом отвязываем.
    await session.execute(
        update(PromoCode)
        .where(PromoCode.product_id.in_(product_ids))
        .values(product_id=None, uses_left=0)
    )

@router.callback_query(F.data == "admin_del_sw_select", F.from_user.id.in_(ADMIN_IDS))
async def admin_del_sw_select(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(select(Software))
    softwares = res.scalars().all()
    if not softwares:
        await callback.answer("❌ Нет доступного софта для удаления!", show_alert=True)
        return

    buttons = []
    for sw in softwares:
        buttons.append([InlineKeyboardButton(text=f"❌ {sw.name} [ID: {sw.id}]", callback_data=f"confirm_del_sw_{sw.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])
    await callback.message.edit_text(
        "🗑 **Удаление софта**\n\nВыберите софт для удаления (вместе с ним удалятся тарифы и ключи):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_del_sw_"), F.from_user.id.in_(ADMIN_IDS))
async def confirm_delete_software(callback: CallbackQuery, session: AsyncSession):
    sw_id = int(callback.data.split("_")[3])
    sw = await session.get(Software, sw_id)
    if not sw:
        await callback.answer("❌ Софт не найден!", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить окончательно", callback_data=f"do_del_sw_{sw_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_del_sw_select")]
    ])
    await callback.message.edit_text(
        f"⚠️ **Подтверждение удаления**\n\nВы действительно хотите удалить софт `{escape_md(sw.name)}`?\n*Все связанные тарифы и ключи будут стерты безвозвратно!*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("do_del_sw_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_delete_software(callback: CallbackQuery, session: AsyncSession):
    sw_id = int(callback.data.split("_")[3])
    sw = await session.get(Software, sw_id)
    if not sw:
        await callback.answer("❌ Софт не найден!", show_alert=True)
        return
    
    sw_name = sw.name
    # ФИКС: тарифы софта удаляются каскадом — сначала отвязываем от них покупки/отзывы
    prod_ids = (await session.execute(select(Product.id).where(Product.software_id == sw_id))).scalars().all()
    await detach_product_references(session, list(prod_ids))
    await session.delete(sw)
    await session.commit()

    await callback.message.edit_text(
        f"✅ Софт `{escape_md(sw_name)}` успешно удален.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "admin_del_prod_select", F.from_user.id.in_(ADMIN_IDS))
async def admin_del_prod_select(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(select(Product))
    products = res.scalars().all()
    if not products:
        await callback.answer("❌ Нет доступных тарифов для удаления!", show_alert=True)
        return

    buttons = []
    for p in products:
        sw = await session.get(Software, p.software_id)
        sw_name = sw.name if sw else "Софт"
        buttons.append([InlineKeyboardButton(text=f"❌ {sw_name} — {p.duration} ({p.price:.2f} грн)", callback_data=f"confirm_del_prod_{p.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])
    await callback.message.edit_text(
        "🗑 **Удаление тарифа**\n\nВыберите тариф для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_del_prod_"), F.from_user.id.in_(ADMIN_IDS))
async def confirm_delete_product(callback: CallbackQuery, session: AsyncSession):
    prod_id = int(callback.data.split("_")[3])
    prod = await session.get(Product, prod_id)
    if not prod:
        await callback.answer("❌ Тариф не найден!", show_alert=True)
        return

    sw = await session.get(Software, prod.software_id)
    sw_name = sw.name if sw else "Софт"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить тариф", callback_data=f"do_del_prod_{prod_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_del_prod_select")]
    ])
    await callback.message.edit_text(
        f"⚠️ **Подтверждение удаления**\n\nУдалить тариф `{escape_md(sw_name)}` — `{escape_md(prod.duration)}` (`{prod.price:.2f} грн`)?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("do_del_prod_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_delete_product(callback: CallbackQuery, session: AsyncSession):
    prod_id = int(callback.data.split("_")[3])
    prod = await session.get(Product, prod_id)
    if not prod:
        await callback.answer("❌ Тариф не найден!", show_alert=True)
        return

    await detach_product_references(session, [prod_id])
    await session.delete(prod)
    await session.commit()

    await callback.message.edit_text(
        "✅ Тариф и все связанные с ним ключи успешно удалены. История покупок и отзывы сохранены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "admin_del_keys_select", F.from_user.id.in_(ADMIN_IDS))
async def admin_del_keys_select(callback: CallbackQuery, session: AsyncSession):
    products_res = await session.execute(select(Product))
    products = products_res.scalars().all()

    if not products:
        await callback.answer("❌ Нет доступных тарифов!", show_alert=True)
        return

    text_msg = "🗑 **Очистка ключей**\n\nВыберите тариф для управления ключами:"
    buttons = []
    for p in products:
        sw = await session.get(Software, p.software_id)
        sw_name = sw.name if sw else "Софт"
        buttons.append([InlineKeyboardButton(text=f"🧹 {sw_name} ({p.duration})", callback_data=f"clear_keys_prod_{p.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("clear_keys_prod_"), F.from_user.id.in_(ADMIN_IDS))
async def clear_keys_menu(callback: CallbackQuery, session: AsyncSession):
    prod_id = int(callback.data.split("_")[3])
    product = await session.get(Product, prod_id)
    if not product:
        await callback.answer("❌ Тариф не найден!", show_alert=True)
        return

    software = await session.get(Software, product.software_id)
    sw_name = software.name if software else "Софт"

    unsold_res = await session.execute(select(func.count(LicenseKey.id)).where(LicenseKey.product_id == prod_id, LicenseKey.is_sold == False))
    unsold_count = unsold_res.scalar() or 0

    sold_res = await session.execute(select(func.count(LicenseKey.id)).where(LicenseKey.product_id == prod_id, LicenseKey.is_sold == True))
    sold_count = sold_res.scalar() or 0

    text_msg = (
        f"🗑 **Управление ключами тарифа**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Софт: `{escape_md(sw_name)}`\n"
        f"⏳ Срок: `{escape_md(product.duration)}`\n\n"
        f"🟢 Непроданных ключей: `{unsold_count}`\n"
        f"🔴 Проданных ключей: `{sold_count}`\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 Удалить непроданные ({unsold_count})", callback_data=f"confirm_del_unsold_{prod_id}")],
        [InlineKeyboardButton(text="🔥 Удалить абсолютно ВСЕ ключи", callback_data=f"confirm_del_all_keys_{prod_id}")],
        [InlineKeyboardButton(text="🔙 К списку тарифов", callback_data="admin_del_keys_select")],
        [InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]
    ])
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_del_unsold_"), F.from_user.id.in_(ADMIN_IDS))
async def confirm_del_unsold(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[3])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить непроданные", callback_data=f"do_del_unsold_{prod_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"clear_keys_prod_{prod_id}")]
    ])
    await callback.message.edit_text("⚠️ Вы уверены, что хотите удалить ВСЕ непроданные ключи этого тарифа?", reply_markup=keyboard)

@router.callback_query(F.data.startswith("do_del_unsold_"), F.from_user.id.in_(ADMIN_IDS))
async def do_del_unsold(callback: CallbackQuery, session: AsyncSession):
    prod_id = int(callback.data.split("_")[3])
    await session.execute(delete(LicenseKey).where(LicenseKey.product_id == prod_id, LicenseKey.is_sold == False))
    await session.commit()
    await callback.message.edit_text(
        "✅ Непроданные ключи успешно удалены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]])
    )

@router.callback_query(F.data.startswith("confirm_del_all_keys_"), F.from_user.id.in_(ADMIN_IDS))
async def confirm_del_all_keys(callback: CallbackQuery):
    prod_id = int(callback.data.split("_")[4])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Да, удалить абсолютно ВСЕ ключи", callback_data=f"do_del_all_keys_{prod_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"clear_keys_prod_{prod_id}")]
    ])
    await callback.message.edit_text("⚠️ Вы уверены? Это удалит и проданные, и непроданные ключи этого тарифа!", reply_markup=keyboard)

@router.callback_query(F.data.startswith("do_del_all_keys_"), F.from_user.id.in_(ADMIN_IDS))
async def do_del_all_keys(callback: CallbackQuery, session: AsyncSession):
    prod_id = int(callback.data.split("_")[4])
    await session.execute(delete(LicenseKey).where(LicenseKey.product_id == prod_id))
    await session.commit()
    await callback.message.edit_text(
        "🔥 Все ключи тарифа успешно удалены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]])
    )

# --- ЗАГРУЗКА КЛЮЧЕЙ ---
async def _run_restock_notifications(bot: Bot, user_ids: list[int], software_name: str, duration: str) -> None:
    """Отправляет уведомления вне транзакции и не блокирует ответ администратору."""
    for user_id in user_ids:
        for attempt in range(2):
            try:
                await bot.send_message(
                    user_id,
                    f"🔔 **Товар снова в наличии**\n\n📦 **{escape_md(software_name)}**\n⏳ `{escape_md(duration)}`",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🛒 Открыть каталог", callback_data="shop")],
                    ]),
                    parse_mode="Markdown",
                )
                break
            except TelegramRetryAfter as exc:
                if attempt == 0:
                    await asyncio.sleep(min(float(exc.retry_after), 15.0))
            except TelegramForbiddenError:
                break
            except Exception as exc:
                logger.info("Не удалось уведомить %s о пополнении: %s", user_id, exc)
                break
        await asyncio.sleep(0.05)


@router.callback_query(F.data == "admin_add_keys_select", F.from_user.id.in_(ADMIN_IDS))
async def admin_add_keys_select(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(select(Product))
    products = res.scalars().all()
    if not products:
        await callback.answer("❌ Сначала создайте хотя бы один тариф!", show_alert=True)
        return

    buttons = []
    for p in products:
        sw = await session.get(Software, p.software_id)
        sw_name = sw.name if sw else "Софт"
        buttons.append([InlineKeyboardButton(text=f"🔑 {sw_name} ({p.duration})", callback_data=f"add_keys_prod_{p.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text("📥 **Загрузка ключей**\n\nВыберите тариф:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("add_keys_prod_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_add_keys_start(callback: CallbackQuery, state: FSMContext):
    prod_id = int(callback.data.split("_")[3])
    await state.update_data(product_id=prod_id)
    await state.set_state(AdminStates.waiting_for_keys)
    await callback.message.edit_text("📥 **Загрузка ключей**\n\nОтправьте ключи списком (каждый ключ с новой строки):", reply_markup=cancel_kb("admin_panel"), parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_keys, F.from_user.id.in_(ADMIN_IDS))
async def process_keys_upload(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not message.text:
        await message.answer("❌ Отправьте ключи обычным текстом, каждый с новой строки.")
        return

    data = await state.get_data()
    prod_id = data.get("product_id")

    if not isinstance(prod_id, int):
        await state.clear()
        await message.answer(
            "❌ Сессия загрузки ключей потеряна. Откройте админ-панель и начните загрузку заново.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🛠 Панель администратора", callback_data="admin_panel")]]
            ),
        )
        return

    prod = await session.get(Product, prod_id)
    if not prod:
        await state.clear()
        await message.answer(
            "❌ Выбранный тариф больше не существует.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🛠 Панель администратора", callback_data="admin_panel")]]
            ),
        )
        return

    # Telegram ограничивает размер текстового сообщения, поэтому разумно
    # ограничиваем число ключей в одной отправке и не позволяем случайно
    # засунуть в БД мусор на десятки тысяч строк.
    MAX_KEYS_PER_UPLOAD = 500

    raw_keys = []
    for line in message.text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        key = line.strip().lstrip("\ufeff")
        if key:
            raw_keys.append(key)

    if not raw_keys:
        await message.answer("❌ Не найдено ни одного непустого ключа. Пришлите ключи построчно.")
        return

    if len(raw_keys) > MAX_KEYS_PER_UPLOAD:
        await message.answer(
            f"❌ За одну загрузку можно добавить максимум {MAX_KEYS_PER_UPLOAD} ключей.\n"
            f"Сейчас получено: {len(raw_keys)}.\n\n"
            "Разделите список на несколько сообщений."
        )
        return

    # Убираем дубликаты внутри самого сообщения, сохраняя порядок.
    seen = set()
    keys = []
    for key in raw_keys:
        if key not in seen:
            seen.add(key)
            keys.append(key)

    # Не принимаем слишком длинные ключи, чтобы гарантированно попасть в VARCHAR(255).
    too_long = [key for key in keys if len(key) > 255]
    if too_long:
        await message.answer(
            f"❌ Найдено {len(too_long)} ключ(ей) длиннее 255 символов.\n"
            "Сократите их и отправьте список заново. База не изменена."
        )
        return

    existing_res = await session.execute(
        select(LicenseKey.key_string).where(LicenseKey.key_string.in_(keys))
    )
    dup_in_db = set(existing_res.scalars().all())

    new_keys = [key for key in keys if key not in dup_in_db]
    skipped_count = len(raw_keys) - len(new_keys)

    added_count = 0
    if new_keys:
        session.add_all(
            [LicenseKey(product_id=prod_id, key_string=key) for key in new_keys]
        )
        try:
            await session.commit()
            added_count = len(new_keys)
        except IntegrityError:
            await session.rollback()
            await message.answer(
                "⚠️ Не удалось сохранить ключи из-за конфликта данных.\n"
                "Ничего из этой загрузки не добавлено. Проверьте список и повторите."
            )
            return

    # Критически важно: после успешной обработки сбрасываем FSM,
    # иначе следующее сообщение админа снова попадёт сюда как «ключ».
    await state.clear()

    result_text = (
        f"✅ **Загрузка завершена**\n"
        f"📦 Тариф: `{escape_md(prod.duration)}`\n"
        f"➕ Добавлено: `{added_count}`\n"
    )
    if skipped_count > 0:
        result_text += f"♻️ Пропущено дубликатов: `{skipped_count}`\n"

    # Даём администратору точную сводку по остатку.
    available_now = await session.scalar(
        select(func.count(LicenseKey.id)).where(
            LicenseKey.product_id == prod_id,
            LicenseKey.is_sold == False,
        )
    ) or 0
    result_text += f"📊 Сейчас в наличии: `{available_now}`"

    if added_count > 0:
        subscriber_ids = list((await session.execute(
            select(RestockSubscription.user_id).where(RestockSubscription.product_id == prod_id)
        )).scalars().all())
        sw = await session.get(Software, prod.software_id)
        sw_name = sw.name if sw else "Товар"
        if subscriber_ids:
            await session.execute(
                delete(RestockSubscription).where(RestockSubscription.product_id == prod_id)
            )
            await session.commit()
            spawn_background(_run_restock_notifications(bot, subscriber_ids, sw_name, prod.duration))

    await message.answer(
        result_text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="📦 Склад", callback_data="admin_stock"),
                InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel"),
            ]]
        ),
        parse_mode="Markdown",
    )


# ==========================================
# ЗАГЛУШКА ДЛЯ "ПОТЕРЯННЫХ" СООБЩЕНИЙ
# ==========================================
# НОВОЕ: если сообщение пришло вне активного FSM-состояния (например, состояние
# уже слетело — MemoryStorage не переживает перезапуск процесса на Render Free,
# либо админ ввёл значение до того, как нажал нужную кнопку), раньше бот
# просто молчал, что выглядело как "бот игнорит". Теперь явно отвечаем и
# подсказываем, что делать — это ловит ЛЮБОЙ текст без активного состояния,
# поэтому регистрируется самым последним.
@router.message(F.text, F.from_user.id.in_(ADMIN_IDS))
async def fallback_admin_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return  # сюда не должны попадать активные состояния — но на всякий случай не мешаем им
    await message.answer(
        "⚠️ Не понял это сообщение — активное действие не найдено "
        "(возможно, сессия сбросилась из-за перезапуска бота).\n\n"
        "Откройте панель администратора и начните действие заново:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛠 Панель администратора", callback_data="admin_panel")]])
    )

@router.message(F.text)
async def fallback_user_message(message: Message, state: FSMContext, session: AsyncSession):
    current_state = await state.get_state()
    if current_state is not None:
        return
    user = await session.get(User, message.from_user.id)
    if user:
        await send_main_menu(message, user)

# ==========================================
# HTTP-ЗАГЛУШКА ДЛЯ RENDER FREE
# ==========================================
# Render Free Web Service "усыпляет" сервис и считает деплой неуспешным,
# если процесс не слушает $PORT и не отвечает на HTTP. Бот работает через
# long polling и сам по себе порт не открывает, поэтому поднимаем рядом
# лёгкий aiohttp-сервер с простой HTML-страницей и /health для healthcheck.
RENDER_STUB_HTML = (
    "<!DOCTYPE html>"
    "<html lang='ru'><head><meta charset='utf-8'>"
    "<title>Kranin Shop Bot</title>"
    "<style>"
    "body{font-family:sans-serif;background:#0f172a;color:#e2e8f0;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}"
    "h1{font-size:1.4rem;font-weight:500;}"
    "</style></head>"
    "<body><h1>🤖 Kranin Shop Bot работает</h1></body></html>"
)


async def health_check(request: web.Request) -> web.Response:
    """Liveness: процесс жив, даже если внешняя БД временно недоступна."""
    return web.json_response({"status": "ok", "version": APP_VERSION})


async def readiness_check(request: web.Request) -> web.Response:
    """Readiness: отдельно проверяет доступность базы данных."""
    try:
        async with async_session_maker() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3.0)
        return web.json_response({"status": "ready", "database": "ok", "version": APP_VERSION})
    except Exception:
        logger.exception("Readiness check: база данных недоступна")
        return web.json_response({"status": "not_ready", "database": "unavailable"}, status=503)


async def index_page(request: web.Request) -> web.Response:
    return web.Response(text=RENDER_STUB_HTML, content_type="text/html")


async def run_stub_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", index_page)
    app.router.add_get("/health", health_check)
    app.router.add_get("/ready", readiness_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("HTTP health server запущен на порту %s", PORT)
    return runner


ACCOUNTS_PLATFORM_NAME = "Аккаунты • Taiwan"
ACCOUNTS_SOFTWARE_NAME = "Аккаунты • Taiwan"
ACCOUNTS_PRODUCT_DURATION = "1 аккаунт"
ACCOUNTS_PRODUCT_PRICE = 60.0
ACCOUNTS_DESCRIPTION = (
    "Персональный аккаунт PUBG MOBILE Taiwan: 12-й уровень Metro и 12-й "
    "уровень Classic. На аккаунте не будет других пользователей. "
    "Версия Taiwan лучше всего подходит для игры с "
    "нашими продуктами: условия использования заметно безопаснее, чем в "
    "других версиях."
    "‼️ВХОД ЧЕРЕЗ FACEBOOK‼️"
)


async def seed_platforms(session_pool) -> None:
    # Гарантируем наличие Android, iOS и служебной категории для игр без ОС.
    # Выбор делается по имени платформы, поэтому код не зависит от их ID.
    async with session_pool() as session:
        existing_res = await session.execute(select(Platform.name))
        existing_names = set(existing_res.scalars().all())

        to_add = []
        if "Android" not in existing_names:
            to_add.append(Platform(name="Android"))
        if "iOS" not in existing_names:
            to_add.append(Platform(name="iOS"))
        if NO_OS_PLATFORM_NAME not in existing_names:
            to_add.append(Platform(name=NO_OS_PLATFORM_NAME))
        if ACCOUNTS_PLATFORM_NAME not in existing_names:
            to_add.append(Platform(name=ACCOUNTS_PLATFORM_NAME))

        if to_add:
            session.add_all(to_add)
            await session.commit()
            logger.info(f"Добавлены базовые платформы: {[p.name for p in to_add]}")

        # НОВОЕ: раздел каталога "Аккаунты • Taiwan" -> софт "PUBG MOBILE" ->
        # тариф "1 аккаунт" по 60 грн, с описанием как в референсном скриншоте.
        accounts_platform_res = await session.execute(
            select(Platform).filter_by(name=ACCOUNTS_PLATFORM_NAME)
        )
        accounts_platform = accounts_platform_res.scalar_one_or_none()
        if accounts_platform:
            game = await session.scalar(
                select(Game).where(func.lower(func.trim(Game.name)) == ACCOUNTS_SOFTWARE_NAME.casefold())
            )
            if not game:
                game = Game(name=ACCOUNTS_SOFTWARE_NAME)
                session.add(game)
                await session.flush()

            sw_res = await session.execute(
                select(Software).filter_by(platform_id=accounts_platform.id, name=ACCOUNTS_SOFTWARE_NAME)
            )
            sw = sw_res.scalar_one_or_none()
            if not sw:
                sw = Software(
                    game_id=game.id,
                    platform_id=accounts_platform.id,
                    name=ACCOUNTS_SOFTWARE_NAME,
                    description=ACCOUNTS_DESCRIPTION,
                )
                session.add(sw)
                await session.commit()
                logger.info(f"Добавлен софт '{ACCOUNTS_SOFTWARE_NAME}' в раздел '{ACCOUNTS_PLATFORM_NAME}'.")
            elif sw.game_id is None:
                sw.game_id = game.id
                await session.commit()

            prod_res = await session.execute(
                select(Product).filter_by(software_id=sw.id, duration=ACCOUNTS_PRODUCT_DURATION)
            )
            prod = prod_res.scalar_one_or_none()
            if not prod:
                session.add(Product(
                    software_id=sw.id,
                    duration=ACCOUNTS_PRODUCT_DURATION,
                    price=ACCOUNTS_PRODUCT_PRICE,
                ))
                await session.commit()
                logger.info(f"Добавлен тариф '{ACCOUNTS_PRODUCT_DURATION}' ({ACCOUNTS_PRODUCT_PRICE} грн) для '{ACCOUNTS_SOFTWARE_NAME}'.")


async def run_migrations() -> None:
    # Каталог теперь хранит игру отдельно от софта. create_all не добавляет
    # колонку в существующую таблицу softwares, поэтому мигрируем её явно.
    try:
        async with engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                await conn.execute(text(
                    "ALTER TABLE softwares ADD COLUMN IF NOT EXISTS game_id INTEGER REFERENCES games(id)"
                ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_softwares_game_id ON softwares(game_id)"
                ))
            elif conn.dialect.name == "sqlite":
                columns = await conn.run_sync(
                    lambda sync_conn: {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(softwares)").fetchall()}
                )
                if "game_id" not in columns:
                    await conn.execute(text("ALTER TABLE softwares ADD COLUMN game_id INTEGER REFERENCES games(id)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_softwares_game_id ON softwares(game_id)"))

        # Старые записи автоматически превращаются в игры с тем же названием,
        # чтобы действующие продукты и ключи остались привязаны к прежнему софту.
        async with async_session_maker() as session:
            games_result = await session.execute(select(Game))
            game_map = {(g.name or "").strip().casefold(): g for g in games_result.scalars().all() if (g.name or "").strip()}
            old_softwares_result = await session.execute(select(Software).where(Software.game_id.is_(None)))
            changed = 0
            for software in old_softwares_result.scalars().all():
                title = (software.name or "").strip()
                if not title:
                    continue
                key = title.casefold()
                game = game_map.get(key)
                if game is None:
                    game = Game(name=title)
                    session.add(game)
                    await session.flush()
                    game_map[key] = game
                software.game_id = game.id
                changed += 1
            if changed:
                await session.commit()
                logger.info("Миграция каталога: привязано старых позиций к играм: %s", changed)
    except Exception as e:
        logger.exception("Миграция отдельной таблицы игр не выполнена: %s", e)

    # Миграции версии 3.0 для обеих поддерживаемых СУБД. create_all сам
    # не добавляет колонки в уже существующие таблицы.
    new_columns = {
        "deposit_requests": {
            "payment_card": "VARCHAR(100)",
            "receipt_file_id": "VARCHAR(255)",
            "receipt_type": "VARCHAR(20)",
            "processed_by": "BIGINT",
            "processed_at": "TIMESTAMP WITH TIME ZONE" if DATABASE_URL.startswith("postgresql") else "DATETIME",
        },
        "withdrawal_requests": {
            "processed_by": "BIGINT",
            "processed_at": "TIMESTAMP WITH TIME ZONE" if DATABASE_URL.startswith("postgresql") else "DATETIME",
        },
    }
    try:
        async with engine.begin() as conn:
            for table_name, columns in new_columns.items():
                if conn.dialect.name == "postgresql":
                    for column_name, column_type in columns.items():
                        await conn.execute(text(
                            f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
                        ))
                else:
                    existing_columns = await conn.run_sync(
                        lambda sync_conn, table=table_name: {
                            row[1]
                            for row in sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                        }
                    )
                    for column_name, column_type in columns.items():
                        if column_name not in existing_columns:
                            await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))

            # Старые дубли подписок удаляются перед созданием уникального индекса.
            await conn.execute(text(
                "DELETE FROM restock_subscriptions WHERE id NOT IN "
                "(SELECT MIN(id) FROM restock_subscriptions GROUP BY user_id, product_id)"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_restock_user_product "
                "ON restock_subscriptions(user_id, product_id)"
            ))
        logger.info("Миграции 3.0: реквизиты чеков, аудит заявок и подписки обновлены.")
    except Exception as e:
        raise RuntimeError(f"Критическая миграция 3.0 не выполнена: {e}") from e

    if not DATABASE_URL.startswith("postgresql"):
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS ix_license_keys_product_sold ON license_keys(product_id, is_sold)",
            "CREATE INDEX IF NOT EXISTS ix_purchases_user_purchased ON purchases(user_id, purchased_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_deposit_user_status ON deposit_requests(user_id, status)",
        ):
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(idx_sql))
            except Exception as e:
                logger.warning("Создание SQLite-индекса не выполнено: %s", e)
        return
    try:
        async with engine.begin() as conn:
            # related_id раньше был INTEGER (int32) — падал с DataError
            # "value out of int32 range" при записи туда Telegram user_id
            # (например, в реферальных бонусах и ручной корректировке баланса).
            await conn.execute(text("ALTER TABLE balance_logs ALTER COLUMN related_id TYPE BIGINT"))
        logger.info("Миграция: balance_logs.related_id приведён к BIGINT.")
    except Exception as e:
        logger.warning(f"Миграция related_id -> BIGINT не выполнена (возможно, таблицы ещё нет): {e}")

    try:
        async with engine.begin() as conn:
            # НОВОЕ: фото и описание софта (для раздела "Аккаунты" и фото у Flexnote и т.д.)
            await conn.execute(text("ALTER TABLE softwares ADD COLUMN IF NOT EXISTS photo_file_id VARCHAR(255)"))
            await conn.execute(text("ALTER TABLE softwares ADD COLUMN IF NOT EXISTS description VARCHAR(1024)"))
            await conn.execute(text("ALTER TABLE softwares ADD COLUMN IF NOT EXISTS files_link VARCHAR(500)"))
        logger.info("Миграция: softwares.photo_file_id / description / files_link добавлены.")
    except Exception as e:
        logger.warning(f"Миграция softwares photo/description/files_link не выполнена: {e}")

    # НОВОЕ: синхронизация sequence автоинкремента с реальными данными в
    # таблицах, куда пишет seed_platforms(). На проде sequence "platforms_id_seq"
    # оказался рассинхронизирован с данными (стоял на 1, хотя строка с id=1 уже
    # существовала) — вероятно, из-за ручных INSERT с явным id в прошлом. Из-за
    # этого INSERT новой платформы падал с UniqueViolationError на platforms_pkey.
    # setval выставляет sequence на MAX(id), поэтому следующий nextval() всегда
    # выдаст свободный id, вне зависимости от того, как разъехался счётчик.
    try:
        async with engine.begin() as conn:
            # НОВОЕ: спин колеса фортуны теперь может быть привязан к отзыву,
            # а не только к покупке — purchase_id стал необязательным,
            # добавлена колонка review_id с уникальностью (один спин на отзыв).
            await conn.execute(text("ALTER TABLE wheel_spins ALTER COLUMN purchase_id DROP NOT NULL"))
            await conn.execute(text("ALTER TABLE wheel_spins ADD COLUMN IF NOT EXISTS review_id INTEGER REFERENCES reviews(id)"))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS wheel_spins_review_id_uniq ON wheel_spins(review_id) WHERE review_id IS NOT NULL"
            ))
        logger.info("Миграция: wheel_spins.review_id добавлен, purchase_id стал nullable.")
    except Exception as e:
        logger.warning(f"Миграция wheel_spins.review_id не выполнена: {e}")

    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES products(id)"))
            await conn.execute(text("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS price_paid DOUBLE PRECISION NOT NULL DEFAULT 0"))
            await conn.execute(text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES products(id)"))
            await conn.execute(text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS purchase_id INTEGER REFERENCES purchases(id)"))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS reviews_user_product_uniq "
                "ON reviews(user_id, product_id) WHERE product_id IS NOT NULL"
            ))
        logger.info("Миграция: product_id/price_paid и review links добавлены.")
    except Exception as e:
        logger.warning(f"Миграция purchase/review metadata не выполнена: {e}")

    # Миграция расширенной системы промокодов.
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS promo_type VARCHAR(20) NOT NULL DEFAULT 'balance'"
            ))
            await conn.execute(text(
                "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS discount_percent DOUBLE PRECISION"
            ))
            await conn.execute(text(
                "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES products(id)"
            ))
            await conn.execute(text(
                "ALTER TABLE promo_code_usages ADD COLUMN IF NOT EXISTS discount_amount DOUBLE PRECISION"
            ))
            await conn.execute(text(
                "ALTER TABLE promo_code_usages ADD COLUMN IF NOT EXISTS final_price DOUBLE PRECISION"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_promo_codes_product ON promo_codes(product_id)"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS promo_usages_promo_user_uniq ON promo_code_usages(promo_id, user_id)"
            ))
        logger.info("Миграция: скидочные промокоды и привязка к товарам добавлены.")
    except Exception as e:
        logger.warning(f"Миграция промокодов не выполнена: {e}")

    # Backfill старых покупок: по историческому имени и сроку восстанавливаем
    # product_id и цену, чтобы профиль и статистика работали и для старых продаж.
    try:
        async with async_session_maker() as session:
            products_res = await session.execute(
                select(Product.id, Product.price, Product.duration, Software.name)
                .join(Software, Product.software_id == Software.id)
            )
            product_map = {
                (name, duration): (pid, float(price))
                for pid, price, duration, name in products_res.all()
            }
            purchases_res = await session.execute(select(Purchase).where(Purchase.product_id.is_(None)))
            changed = 0
            for purchase in purchases_res.scalars().all():
                for (name, duration), (product_id, price) in product_map.items():
                    if purchase.product_name == f"{name} ({duration})" and purchase.duration == duration:
                        purchase.product_id = product_id
                        purchase.price_paid = price
                        changed += 1
                        break
            if changed:
                await session.commit()
                logger.info(f"Backfill purchases: восстановлено {changed} исторических покупок.")
    except Exception as e:
        logger.warning(f"Backfill исторических покупок не выполнен: {e}")

    try:
        async with engine.begin() as conn:
            # НОВОЕ: модерация отзывов — статус по умолчанию "pending" для новых
            # записей. Проверяем, существовала ли колонка ДО этого запуска: если
            # нет — это первое включение модерации, и все уже накопленные отзывы
            # (которые раньше публиковались мгновенно, без модерации) одноразово
            # помечаем "approved", чтобы не потерять их из раздела "⭐ Отзывы".
            # На всех последующих запусках колонка уже существует, поэтому этот
            # backfill не выполняется повторно и не "одобряет" новые pending-отзывы.
            col_exists_res = await conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'reviews' AND column_name = 'status'"
            ))
            column_already_existed = col_exists_res.first() is not None

            await conn.execute(text(
                "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'pending'"
            ))

            if not column_already_existed:
                await conn.execute(text("UPDATE reviews SET status = 'approved'"))
        logger.info("Миграция: reviews.status добавлен (старые отзывы помечены approved).")
    except Exception as e:
        logger.warning(f"Миграция reviews.status не выполнена: {e}")

    # НОВОЕ: индексы под самые частые запросы (остаток ключей по тарифу, история
    # покупок пользователя, подписки на пополнение). create_all не добавляет
    # индексы к уже существующим таблицам, поэтому создаём вручную, идемпотентно.
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS ix_license_keys_product_sold ON license_keys(product_id, is_sold)",
        "CREATE INDEX IF NOT EXISTS ix_purchases_user_purchased ON purchases(user_id, purchased_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_purchases_product ON purchases(product_id)",
        "CREATE INDEX IF NOT EXISTS ix_restock_product ON restock_subscriptions(product_id)",
        "CREATE INDEX IF NOT EXISTS ix_deposit_user_status ON deposit_requests(user_id, status)",
    ):
        try:
            async with engine.begin() as conn:
                await conn.execute(text(idx_sql))
        except Exception as e:
            logger.warning(f"Создание индекса не выполнено ({idx_sql[:60]}...): {e}")

    for table_name in ("platforms", "softwares", "products"):
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table_name}), 1), "
                    f"(SELECT MAX(id) IS NOT NULL FROM {table_name}))"
                ))
            logger.info(f"Миграция: sequence {table_name}.id синхронизирован с данными.")
        except Exception as e:
            logger.warning(f"Синхронизация sequence {table_name}.id не выполнена: {e}")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await run_migrations()
    await seed_platforms(async_session_maker)

    storage = MemoryStorage()
    if REDIS_URL:
        try:
            from aiogram.fsm.storage.redis import RedisStorage

            storage = RedisStorage.from_url(REDIS_URL)
            logger.info("FSM: подключено постоянное хранилище Redis")
        except (ImportError, ValueError) as exc:
            logger.warning("Redis FSM недоступен (%s); используется MemoryStorage", exc)
    else:
        logger.info("FSM: MemoryStorage; для сохранения сессий между рестартами задайте REDIS_URL")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=storage)
    dp.update.middleware(DbSessionMiddleware(session_pool=async_session_maker))
    dp.callback_query.outer_middleware(ThrottleMiddleware(interval=0.35))
    dp.include_router(router)

    runner = None
    try:
        runner = await run_stub_server()
        await bot.set_my_commands([
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="help", description="Помощь и покупки"),
        ])
        await bot.delete_webhook(drop_pending_updates=DROP_PENDING_UPDATES)
        logger.info("KRANIN SHOP %s запущен; polling начат", APP_VERSION)
        await dp.start_polling(bot)
    finally:
        for task in list(_background_tasks):
            task.cancel()
        if _background_tasks:
            await asyncio.gather(*list(_background_tasks), return_exceptions=True)
        with suppress(Exception):
            await storage.close()
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
