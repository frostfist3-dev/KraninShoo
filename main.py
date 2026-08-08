import asyncio
import logging
import math
import os
import random
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message
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
    func,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ==========================================
# ЛОГИРОВАНИЕ И КОНФИГУРАЦИЯ БД
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "KRANIN_SUPPORT")
REVIEWS_CHANNEL_URL = os.getenv("REVIEWS_CHANNEL_URL", "https://t.me/KRANIN_REVIEWS")

raw_admin_ids = os.getenv("ADMIN_IDS", "8479717148")
ADMIN_IDS = [int(x.strip()) for x in raw_admin_ids.split(",") if x.strip().isdigit()]

# Разумные пределы для финансовых сумм, чтобы отсечь inf/nan/переполнения
MAX_AMOUNT = 1_000_000.0
MIN_WITHDRAWAL = 500.0
MIN_DEPOSIT = float(os.getenv("MIN_DEPOSIT", "20"))

# Сколько заявок на пополнение в статусе "pending" может одновременно
# висеть у одного пользователя, прежде чем он сможет создать новую
MAX_PENDING_DEPOSITS = int(os.getenv("MAX_PENDING_DEPOSITS", "3"))

# НОВОЕ: порог остатка ключей, при пересечении которого админам приходит
# уведомление "товар заканчивается". Срабатывает ровно один раз при
# достижении этого количества (а не на каждой продаже ниже порога),
# плюс отдельное уведомление при полном обнулении остатка.
LOW_STOCK_THRESHOLD = int(os.getenv("LOW_STOCK_THRESHOLD", "3"))

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
PORT = int(os.getenv("PORT", "10000"))

if not DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"
    engine = create_async_engine(DATABASE_URL, echo=False)
else:
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://")
    if "?" in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.split("?")[0]

    connect_args = {}
    if any(domain in DATABASE_URL for domain in ["oregon-postgres", "frankfurt-postgres", "render.com"]):
        connect_args = {"ssl": "require"}

    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args=connect_args
    )

CARDS = [
    "4441 1111 5555 7352 (Моно)",
    "4323 3473 8685 7285 (А-Банк)",
    "5232 4410 4407 1160 (Альянс)",
]

DURATIONS = ["1 день", "7 дней", "30 дней"]

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
    # искать вручную в логах Render
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


async def safe_edit_text(
    callback: CallbackQuery, bot: Bot, text_msg: str, keyboard: InlineKeyboardMarkup, parse_mode: str = "Markdown"
) -> None:
    # НОВОЕ: после экрана софта с фото сообщение является фото-сообщением, и
    # обычный edit_text у него упадёт ("there is no text in the message to
    # edit"). Эта обёртка ловит такой случай и вместо правки шлёт новое
    # текстовое сообщение — используется на всех переходах, которые могут
    # быть первым шагом сразу после экрана с фото.
    try:
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode=parse_mode)
    except TelegramBadRequest:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await bot.send_message(callback.message.chat.id, text_msg, reply_markup=keyboard, parse_mode=parse_mode)


def user_display(message_or_callback) -> str:
    u = message_or_callback.from_user
    username = u.username
    if username:
        return f"@{escape_md(username)}"
    return "без юзернейма"


def parse_positive_amount(raw_text: str, max_amount: float = MAX_AMOUNT) -> float | None:
    # Безопасный парсинг положительной денежной суммы из текста.
    # Возвращает None, если значение некорректно (не число, <= 0, inf/nan, слишком большое).
    if raw_text is None:
        return None
    try:
        value = float(raw_text.strip().replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    if value <= 0:
        return None
    if value > max_amount:
        return None
    # Округляем до копеек, чтобы не плодить "грязные" значения из-за float
    return round(value, 2)


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
            amount=round(amount, 2),
            balance_after=round(balance_after, 2),
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
        "welcome": "💎 ✦ **KRANIN SHOP** ✦ 💎\n⚡️ Надійний сервіс цифрових ліцензій та софту\n━━━━━━━━━━━━━━━━━━━\n💰 **Баланс:** `{balance:.2f} грн`\n━━━━━━━━━━━━━━━━━━━\n\n👇 Оберіть розділ у меню нижче:",
        "banned": "❌ Ваш акаунт заблоковано в боті.",
        "lang_changed": "✅ Мову успішно змінено на українську.",
        "lang_btn": "🌐 Змінити мову / Сменить язык"
    },
    "ru": {
        "welcome": "💎 ✦ **KRANIN SHOP** ✦ 💎\n⚡️ Надежный сервис цифровых лицензий и софта\n━━━━━━━━━━━━━━━━━━━\n💰 **Баланс:** `{balance:.2f} грн`\n━━━━━━━━━━━━━━━━━━━\n\n👇 Выберите раздел в меню ниже:",
        "banned": "❌ Ваш аккаунт заблокирован в боте.",
        "lang_changed": "✅ Язык успешно изменен на русский.",
        "lang_btn": "🌐 Изменить язык / Змінити мову"
    }
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

class Platform(Base):
    __tablename__ = "platforms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    softwares = relationship("Software", back_populates="platform", cascade="all, delete-orphan")

class Software(Base):
    __tablename__ = "softwares"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"))
    name: Mapped[str] = mapped_column(String(100))
    # НОВОЕ: file_id фото Telegram, которое показывается над текстом при выборе софта
    photo_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # НОВОЕ: описание софта (например, текст про аккаунт), показывается над списком тарифов
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
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
    product_name: Mapped[str] = mapped_column(String(100))
    key_issued: Mapped[str] = mapped_column(String(255))
    duration: Mapped[str] = mapped_column(String(50), default="")
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

# НОВОЕ: колесо фортуны после покупки. Отдельная таблица (а не колонка в
# Purchase) специально для того, чтобы не трогать уже существующую в
# продакшене таблицу purchases — create_all создаёт только отсутствующие
# таблицы и не делает ALTER TABLE, поэтому новая таблица безопасно
# появится сама при следующем запуске без ручной миграции.
class WheelSpin(Base):
    __tablename__ = "wheel_spins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id"), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    result: Mapped[str] = mapped_column(String(20))  # key / balance / lose
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class DepositRequest(Base):
    __tablename__ = "deposit_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    amount: Mapped[float] = mapped_column(Float)
    card: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="pending")
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
    amount: Mapped[float] = mapped_column(Float)
    uses_left: Mapped[int] = mapped_column(Integer, default=1)


class PromoCodeUsage(Base):
    # НОВОЕ: фиксирует, что конкретный пользователь уже активировал конкретный
    # промокод — не даёт одному человеку в одиночку выесть весь uses_left,
    # повторно присылая один и тот же код.
    __tablename__ = "promo_code_usages"
    __table_args__ = (UniqueConstraint("promo_id", "user_id", name="uq_promo_user_once"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class RestockSubscription(Base):
    __tablename__ = "restock_subscriptions"
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

    async def __call__(self, handler, event, data):
        async with self.session_pool() as session:
            data["session"] = session
            user_obj = getattr(event, "from_user", None)
            if user_obj:
                user = await session.get(User, user_obj.id)
                if user and user.is_banned:
                    lang = user.language if user.language in LANGUAGES else "uk"
                    msg_text = LANGUAGES[lang]["banned"]
                    if isinstance(event, Message):
                        await event.answer(msg_text)
                    elif isinstance(event, CallbackQuery):
                        await event.answer(msg_text, show_alert=True)
                    return
            return await handler(event, data)

class AdminStates(StatesGroup):
    waiting_for_software_platform = State()
    waiting_for_software_name = State()
    waiting_for_duration = State()
    waiting_for_price = State()
    waiting_for_keys = State()
    waiting_for_software_media = State()
    waiting_for_promo_code = State()
    waiting_for_promo_amount = State()
    waiting_for_promo_uses = State()
    waiting_for_broadcast_text = State()
    waiting_for_user_id = State()
    waiting_for_user_balance_change = State()

class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_receipt = State()

class WithdrawalStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_card = State()

class UserStates(StatesGroup):
    waiting_for_promo_input = State()
    waiting_for_review_rating = State()
    waiting_for_review_text = State()

# ==========================================
# ГЛАВНОЕ МЕНЮ И СТАРТ
# ==========================================
def get_main_menu_kb(user_id: int, lang: str = "uk"):
    texts = LANGUAGES.get(lang, LANGUAGES["uk"])
    keyboard = [
        [InlineKeyboardButton(text="🛒 Каталог софта", callback_data="shop")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile"),
         InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="deposit")],
        [InlineKeyboardButton(text="🎟 Промокод", callback_data="enter_promo"),
         InlineKeyboardButton(text="⭐ Отзывы", callback_data="show_reviews")],
        [InlineKeyboardButton(text="🤝 Реферальная система", callback_data="ref_program")],
        [InlineKeyboardButton(text=texts["lang_btn"], callback_data="change_lang")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton(text="🛠 Панель администратора", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_main_menu_text(balance: float, lang: str = "uk") -> str:
    texts = LANGUAGES.get(lang, LANGUAGES["uk"])
    return texts["welcome"].format(balance=balance)


def cancel_kb(target: str = "main_menu", label: str = "❌ Отмена") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=target)]])


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

        lang = user.language if user and user.language in LANGUAGES else "uk"
        await message.answer(
            get_main_menu_text(user.balance, lang),
            reply_markup=get_main_menu_kb(message.from_user.id, lang),
            parse_mode="Markdown"
        )
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка в cmd_start: {e}")
        await message.answer("⚠️ Произошла ошибка. Попробуйте снова.")

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
    await state.clear()
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
async def change_lang_handler(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌐 **Виберіть мову / Выберите язык:**",
        reply_markup=get_language_kb(is_reg=False),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.in_({"lang_uk", "lang_ru"}))
async def set_language(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    await state.clear()
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
async def enter_promo_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_promo_input)
    await callback.message.edit_text(
        "🎟 **Активация промокода**\n\nВведите ваш промокод:",
        reply_markup=cancel_kb("main_menu"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(UserStates.waiting_for_promo_input)
async def process_promo_activation(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Отправьте промокод текстом.", reply_markup=cancel_kb("main_menu"))
        return
    code_text = message.text.strip()

    # ФИКС: используем блокировку строки промокода, чтобы два одновременных
    # использования не смогли оба пройти проверку uses_left > 0.
    res = await session.execute(
        select(PromoCode).filter_by(code=code_text).with_for_update()
    )
    promo = res.scalars().first()

    if not promo or promo.uses_left <= 0:
        await message.answer("❌ Промокод не найден или срок его активаций исчерпан!", reply_markup=cancel_kb("main_menu"))
        await state.clear()
        return

    # ФИКС: не даём одному пользователю активировать один и тот же промокод
    # повторно — раньше это позволяло в одиночку выесть весь uses_left.
    already_used_res = await session.execute(
        select(PromoCodeUsage).filter_by(promo_id=promo.id, user_id=message.from_user.id)
    )
    if already_used_res.scalars().first():
        await message.answer("❌ Вы уже активировали этот промокод ранее!", reply_markup=cancel_kb("main_menu"))
        await state.clear()
        return

    user_res = await session.execute(select(User).where(User.id == message.from_user.id).with_for_update())
    user = user_res.scalar_one_or_none()
    if not user:
        await state.clear()
        return

    user.balance += promo.amount
    promo.uses_left -= 1
    session.add(PromoCodeUsage(promo_id=promo.id, user_id=user.id))
    log_balance_change(session, user.id, promo.amount, "promo_code", user.balance, related_id=promo.id)

    try:
        await session.commit()
    except IntegrityError:
        # Подстраховка на случай гонки: уникальный constraint поймал
        # повторную активацию, которую не успела отсечь проверка выше
        await session.rollback()
        await message.answer("❌ Вы уже активировали этот промокод ранее!", reply_markup=cancel_kb("main_menu"))
        await state.clear()
        return

    await state.clear()

    lang = user.language if user and user.language in LANGUAGES else "uk"
    await message.answer(
        f"🎉 **Промокод успешно активирован!**\nВам начислено `{promo.amount:.2f} грн` на баланс.",
        reply_markup=get_main_menu_kb(message.from_user.id, lang),
        parse_mode="Markdown"
    )

# ==========================================
# ОТЗЫВЫ (С ВЫБОРОМ ОЦЕНКИ КНОПКАМИ ⭐)
# ==========================================
# ФИКС ГЛАВНОГО БАГА "ОТЗЫВЫ НЕ ПРИХОДЯТ": раньше раздел "⭐ Отзывы" был
# статичной заглушкой — он просто показывал ссылку на внешний канал и
# кнопку "Оставить отзыв", а сами отзывы, которые пользователи реально
# отправляли (таблица Review), нигде в боте не отображались. Они уходили
# только личным сообщением админу и "терялись" для остальных пользователей.
# Теперь раздел показывает настоящие отзывы из БД: рейтинг, текст, дату,
# с пагинацией и средней оценкой сверху.
@router.callback_query(F.data == "show_reviews")
@router.callback_query(F.data.startswith("reviews_page_"))
async def show_reviews_handler(callback: CallbackQuery, session: AsyncSession):
    page = 0
    if callback.data.startswith("reviews_page_"):
        page = int(callback.data.split("_")[2])

    stats_res = await session.execute(select(func.count(Review.id), func.avg(Review.rating)))
    total_reviews, avg_rating = stats_res.one()
    total_reviews = total_reviews or 0
    avg_rating = float(avg_rating) if avg_rating else 0.0

    per_page = 3
    total_pages = max(1, math.ceil(total_reviews / per_page))
    page = max(0, min(page, total_pages - 1))
    offset = page * per_page

    res = await session.execute(
        select(Review).order_by(Review.created_at.desc()).offset(offset).limit(per_page)
    )
    reviews = res.scalars().all()

    header = "🌟 **Отзывы наших клиентов**\n━━━━━━━━━━━━━━━━━━━\n"
    if total_reviews:
        stars_avg = "⭐" * round(avg_rating)
        header += f"{stars_avg}  **{avg_rating:.1f}/5**  ·  {total_reviews} отзывов\n━━━━━━━━━━━━━━━━━━━\n\n"
    else:
        header += "\n"

    if not reviews:
        body = "Пока отзывов нет — станьте первым! 👇"
    else:
        blocks = []
        for r in reviews:
            date_str = r.created_at.strftime("%d.%m.%Y")
            blocks.append(
                f"{'⭐' * r.rating}{'☆' * (5 - r.rating)}\n"
                f"_{escape_md(r.text)}_\n"
                f"🕒 {date_str}"
            )
        body = "\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n\n".join(blocks)

    text_msg = header + body

    buttons = []
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"reviews_page_{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"reviews_page_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="📢 Канал отзывов", url=REVIEWS_CHANNEL_URL)])
    buttons.append([InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="leave_review")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])

    await callback.message.edit_text(
        text_msg,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "leave_review")
async def leave_review_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    user_id = callback.from_user.id
    purchases_res = await session.execute(select(Purchase).where(Purchase.user_id == user_id))
    if not purchases_res.scalars().first():
        await callback.answer("❌ Вы можете оставлять отзывы только после совершения покупок в боте!", show_alert=True)
        return

    await state.set_state(UserStates.waiting_for_review_rating)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐⭐⭐⭐⭐ (5/5)", callback_data="review_stars_5")],
        [InlineKeyboardButton(text="⭐⭐⭐⭐ (4/5)", callback_data="review_stars_4")],
        [InlineKeyboardButton(text="⭐⭐⭐ (3/5)", callback_data="review_stars_3")],
        [InlineKeyboardButton(text="⭐⭐ (2/5)", callback_data="review_stars_2")],
        [InlineKeyboardButton(text="⭐ (1/5)", callback_data="review_stars_1")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="show_reviews")]
    ])

    await callback.message.edit_text(
        "⭐ **Оставить отзыв**\n\nПожалуйста, выберите вашу оценку от 1 до 5 звезд:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("review_stars_"))
async def process_review_rating(callback: CallbackQuery, state: FSMContext):
    rating = int(callback.data.split("_")[2])
    await state.update_data(rating=rating)
    await state.set_state(UserStates.waiting_for_review_text)

    stars_str = "⭐" * rating
    await callback.message.edit_text(
        f"✍️ **Ваша оценка:** {stars_str} ({rating}/5)\n\n"
        f"Напишите ваш отзыв текстом в ответ на это сообщение:",
        reply_markup=cancel_kb("show_reviews"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(UserStates.waiting_for_review_text)
async def save_review_handler(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not message.text:
        await message.answer("❌ Отправьте текст отзыва.", reply_markup=cancel_kb("show_reviews"))
        return

    text_val = message.text.strip()

    # ФИКС: колонка Review.text — String(500); без этой проверки текст
    # длиннее лимита падал бы с необработанным DataError на Postgres.
    if len(text_val) > MAX_REVIEW_LENGTH:
        await message.answer(
            f"❌ Отзыв слишком длинный ({len(text_val)} символов). "
            f"Максимум — {MAX_REVIEW_LENGTH} символов. Сократите текст:",
            reply_markup=cancel_kb("show_reviews")
        )
        return

    data = await state.get_data()
    rating = data.get("rating", 5)

    review = Review(user_id=message.from_user.id, rating=rating, text=text_val)
    session.add(review)
    await session.commit()
    await state.clear()

    user = await session.get(User, message.from_user.id)
    lang = user.language if user and user.language in LANGUAGES else "uk"

    admin_msg = (
        f"⭐ **Новый отзыв от покупателя!**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID отзыва: `{review.id}`\n"
        f"👤 Пользователь: `{message.from_user.id}` ({user_display(message)})\n"
        f"⭐ Оценка: {'⭐' * rating} ({rating}/5)\n"
        f"💬 Текст отзыва:\n_{escape_md(text_val)}_\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📢 Канал отзывов: {REVIEWS_CHANNEL_URL}"
    )

    admin_review_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗑 Удалить отзыв", callback_data=f"admin_del_review_confirm_{review.id}")]]
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=admin_msg, reply_markup=admin_review_kb, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось отправить отзыв администратору {admin_id}: {e}")

    await message.answer(
        "✅ **Спасибо за ваш отзыв!** Он передан администраторам.",
        reply_markup=get_main_menu_kb(message.from_user.id, lang),
        parse_mode="Markdown"
    )

# ==========================================
# ПРОФИЛЬ И КУПЛЕННЫЕ КЛЮЧИ
# ==========================================
@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    user = await session.get(User, user_id)
    balance = user.balance if user else 0.0

    total_res = await session.execute(select(func.count(Purchase.id)).where(Purchase.user_id == user_id))
    total_purchases = total_res.scalar() or 0

    text_msg = (
        f"👤 **Личный кабинет**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 **ID аккаунта:** `{user_id}`\n"
        f"💰 **Баланс:** `{balance:.2f} грн`\n"
        f"🛍 **Всего покупок:** `{total_purchases}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔑 **Ваши купленные ключи по категориям:**"
    )

    # ФИКС: раньше список категорий брался только из фиксированного DURATIONS,
    # и покупки с произвольным сроком (заданным вручную админом) не попадали
    # ни в одну кнопку. Теперь берём реальные различающиеся сроки из покупок пользователя.
    distinct_res = await session.execute(
        select(Purchase.duration)
        .where(Purchase.user_id == user_id)
        .distinct()
    )
    user_durations = [d for d in distinct_res.scalars().all() if d]
    # Сохраняем порядок из DURATIONS, добавляя нестандартные сроки в конец
    ordered_durations = [d for d in DURATIONS if d in user_durations]
    ordered_durations += [d for d in user_durations if d not in DURATIONS]

    buttons = []
    if not ordered_durations:
        text_msg += "\n\n_У вас пока нет покупок._"
    for d in ordered_durations:
        cnt_res = await session.execute(
            select(func.count(Purchase.id)).where(Purchase.user_id == user_id, Purchase.duration == d)
        )
        cnt = cnt_res.scalar() or 0
        # используем сам текст длительности как идентификатор (безопасно, т.к. он из БД пользователя)
        safe_token = d.replace(" ", "_")
        buttons.append([InlineKeyboardButton(text=f"📂 Срок: {d} (куплено: {cnt})", callback_data=f"profile_durv_{safe_token}")])

    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("profile_durv_"))
async def show_profile_keys(callback: CallbackQuery, session: AsyncSession):
    safe_token = callback.data[len("profile_durv_"):]
    duration = safe_token.replace("_", " ")

    user_id = callback.from_user.id
    res = await session.execute(
        select(Purchase)
        .where(Purchase.user_id == user_id, Purchase.duration == duration)
        .order_by(Purchase.purchased_at.desc())
        .limit(20)
    )
    purchases = res.scalars().all()

    if not purchases:
        text_msg = f"🔑 **Ключи ({escape_md(duration)})**\n\nУ вас пока нет купленных ключей в этой категории."
    else:
        lines = [f"🔑 **Ключи ({escape_md(duration)})** — последние покупки:\n"]
        for p in purchases:
            date_str = p.purchased_at.strftime("%d.%m.%Y %H:%M")
            lines.append(
                f"📦 *{escape_md(p.product_name)}*\n"
                f"🔐 `{escape_md(p.key_issued)}`\n"
                f"🕒 _{date_str}_\n"
                f"──────────────"
            )
        text_msg = "\n".join(lines)

    buttons = [
        [InlineKeyboardButton(text="🔙 Назад в профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ]
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

# ==========================================
# КАТАЛОГ И ПОКУПКИ С УВЕДОМЛЕНИЯМИ (RESTOCK)
# ==========================================
@router.callback_query(F.data == "shop")
async def show_platforms(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    await state.clear()
    result = await session.execute(select(Platform))
    platforms = result.scalars().all()

    text_msg = (
        "🛍 **МАГАЗИН ЛИЦЕНЗИЙ**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📱 Выберите платформу вашего устройства:"
    )
    buttons = []
    for p in platforms:
        name_lower = p.name.lower()
        if "android" in name_lower:
            icon = "🤖"
        elif "ios" in name_lower:
            icon = "🍏"
        elif "аккаунт" in name_lower:
            icon = "🎮"
        else:
            icon = "📦"
        buttons.append([InlineKeyboardButton(text=f"{icon} {p.name}", callback_data=f"platform_{p.id}")])

    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("platform_"))
async def show_softwares(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    platform_id = int(callback.data.split("_")[1])
    platform = await session.get(Platform, platform_id)

    result = await session.execute(select(Software).filter_by(platform_id=platform_id))
    softwares = result.scalars().all()

    if not softwares:
        text_msg = f"🛒 **Каталог — {escape_md(platform.name) if platform else 'Платформа'}**\n\n❌ На данную платформу софт пока не добавлен."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К выбору платформ", callback_data="shop")]])
        await safe_edit_text(callback, bot, text_msg, keyboard)
        await callback.answer()
        return

    text_msg = f"🛍 **{escape_md(platform.name)}**\n━━━━━━━━━━━━━━━━━━━\n⚡️ Выберите нужный софт:"
    buttons = []
    for sw in softwares:
        buttons.append([InlineKeyboardButton(text=f"🛡 {sw.name}", callback_data=f"software_{sw.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 К платформам", callback_data="shop")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await safe_edit_text(callback, bot, text_msg, keyboard)
    await callback.answer()

async def _render_software_screen(
    callback: CallbackQuery, bot: Bot, software: "Software", text_msg: str, keyboard: InlineKeyboardMarkup
) -> None:
    # НОВОЕ: если у софта задано фото (photo_file_id), показываем его как
    # фото-сообщение — в Telegram фото у такого сообщения располагается
    # НАД текстом (подписью), то есть ровно "фото над текстом", которое
    # просили. Обычное edit_text не может превратить текстовое сообщение в
    # фото, поэтому старое сообщение удаляется и отправляется новое.
    chat_id = callback.message.chat.id
    if software.photo_file_id:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await bot.send_photo(
            chat_id=chat_id,
            photo=software.photo_file_id,
            caption=text_msg,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

    try:
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    except TelegramBadRequest:
        # Сообщение было фото (например, при возврате назад) — редактировать
        # текстом нельзя, удаляем и отправляем новое текстовое сообщение.
        try:
            await callback.message.delete()
        except Exception:
            pass
        await bot.send_message(chat_id, text_msg, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data.startswith("software_"))
async def show_products(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    software_id = int(callback.data.split("_")[1])
    software = await session.get(Software, software_id)
    if not software:
        await callback.answer("❌ Софт не найден!", show_alert=True)
        return

    result = await session.execute(select(Product).filter_by(software_id=software_id))
    products = result.scalars().all()

    description_block = f"{escape_md(software.description)}\n\n" if software.description else ""

    if not products:
        text_msg = f"🛒 **{escape_md(software.name)}**\n━━━━━━━━━━━━━━━━━━━\n{description_block}❌ Тарифы для этого софта пока отсутствуют."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К списку софта", callback_data=f"platform_{software.platform_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ])
        await _render_software_screen(callback, bot, software, text_msg, keyboard)
        await callback.answer()
        return

    text_msg = f"🛡 **{escape_md(software.name)}**\n━━━━━━━━━━━━━━━━━━━\n{description_block}📌 Выберите продукт:"
    buttons = []
    for p in products:
        keys_res = await session.execute(select(func.count(LicenseKey.id)).filter_by(product_id=p.id, is_sold=False))
        available_keys = keys_res.scalar() or 0

        if available_keys > 0:
            status_text = f"🟢 {available_keys} шт."
            buttons.append([InlineKeyboardButton(
                text=f"⏳ {p.duration} — 💵 {p.price:.2f} грн  ({status_text})",
                callback_data=f"confirm_buy_{p.id}"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text=f"🔴 {p.duration} — {p.price:.2f} грн (нет в наличии, 🔔 уведомить)",
                callback_data=f"notify_restock_{p.id}"
            )])

    buttons.append([InlineKeyboardButton(text="🔙 К списку софта", callback_data=f"platform_{software.platform_id}")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await _render_software_screen(callback, bot, software, text_msg, keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("notify_restock_"))
async def notify_restock_handler(callback: CallbackQuery, session: AsyncSession):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    existing = await session.execute(select(RestockSubscription).filter_by(user_id=user_id, product_id=product_id))
    if existing.scalars().first():
        await callback.answer("🔔 Вы уже подписаны на уведомление об этом товаре!", show_alert=True)
        return

    sub = RestockSubscription(user_id=user_id, product_id=product_id)
    session.add(sub)
    await session.commit()
    await callback.answer("✅ Готово! Бот уведомит вас, когда ключи поступят в продажу.", show_alert=True)

# --- НОВОЕ: ШАГ ПОДТВЕРЖДЕНИЯ ПЕРЕД ПОКУПКОЙ ---
@router.callback_query(F.data.startswith("confirm_buy_"))
async def confirm_purchase_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot):
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

    # Проверяем, что ключи всё ещё есть в наличии на момент показа подтверждения
    avail_res = await session.execute(
        select(func.count(LicenseKey.id)).filter_by(product_id=product.id, is_sold=False)
    )
    available = avail_res.scalar() or 0

    if available <= 0:
        await callback.answer("❌ К сожалению, ключи только что закончились!", show_alert=True)
        return

    if balance < product.price:
        await callback.answer(f"❌ Недостаточно средств! Требуется {product.price:.2f} грн, у вас {balance:.2f} грн", show_alert=True)
        return

    text_msg = (
        f"🧾 **Подтверждение покупки**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **Товар:** `{escape_md(sw_name)}`\n"
        f"⏳ **Срок:** `{escape_md(product.duration)}`\n"
        f"💰 **Цена:** `{product.price:.2f} грн`\n"
        f"💳 **Ваш баланс:** `{balance:.2f} грн`\n"
        f"💳 **Баланс после покупки:** `{(balance - product.price):.2f} грн`\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"Подтвердите покупку — деньги будут списаны и вам сразу выдастся ключ."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить покупку", callback_data=f"buy_product_{product.id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"software_{product.software_id}")],
    ])
    await safe_edit_text(callback, bot, text_msg, keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("buy_product_"))
async def process_purchase(callback: CallbackQuery, session: AsyncSession):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    product = await session.get(Product, product_id)
    if not product:
        await callback.answer("❌ Товар не найден!", show_alert=True)
        return

    software = await session.get(Software, product.software_id)

    # ФИКС: блокируем строку пользователя на время транзакции, чтобы исключить
    # race condition при двойном/параллельном нажатии на "Купить" — иначе баланс
    # мог уйти в минус, т.к. обе транзакции проходили проверку balance до commit().
    user_res = await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = user_res.scalar_one_or_none()

    if not user or user.balance < product.price:
        await callback.answer(
            f"❌ Недостаточно средств! Требуется {product.price:.2f} грн", show_alert=True
        )
        return

    key_res = await session.execute(
        select(LicenseKey)
        .filter_by(product_id=product.id, is_sold=False)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    license_key = key_res.scalars().first()

    if not license_key:
        await callback.answer("❌ Ключи этого номинала закончились!", show_alert=True)
        return

    user.balance -= product.price
    license_key.is_sold = True
    log_balance_change(session, user.id, -product.price, "purchase", user.balance, related_id=product.id)

    # НОВОЕ: остаток ключей ПОСЛЕ этой продажи (autoflush применит is_sold=True
    # до выполнения этого SELECT, поэтому только что проданный ключ уже не
    # попадёт в подсчёт "в наличии")
    remaining_res = await session.execute(
        select(func.count(LicenseKey.id)).filter_by(product_id=product.id, is_sold=False)
    )
    remaining_after_sale = remaining_res.scalar() or 0

    if user.referred_by:
        referrer_res = await session.execute(
            select(User).where(User.id == user.referred_by).with_for_update()
        )
        referrer = referrer_res.scalar_one_or_none()
        if referrer:
            ref_bonus = round(product.price * 0.05, 2)
            referrer.balance += ref_bonus
            log_balance_change(session, referrer.id, ref_bonus, "referral_bonus", referrer.balance, related_id=user.id)
            try:
                await callback.bot.send_message(
                    referrer.id,
                    f"🎉 **Реферальный бонус!**\nВаш реферал совершил покупку. Вам начислено `{ref_bonus:.2f} грн` на баланс!",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление рефереру {referrer.id}: {e}")

    sw_name = software.name if software else "Товар"
    purchase = Purchase(
        user_id=user.id,
        product_name=f"{sw_name} ({product.duration})",
        key_issued=license_key.key_string,
        duration=product.duration,
    )
    session.add(purchase)
    await session.commit()

    # НОВОЕ: уведомление админам о заканчивающемся остатке ключей.
    # Срабатывает один раз ровно в момент пересечения порога (а не на
    # каждой продаже ниже него) плюс отдельно — когда остаток обнулился.
    if remaining_after_sale == LOW_STOCK_THRESHOLD or remaining_after_sale == 0:
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

    success_text = (
        f"✅ **Покупка успешно завершена!**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **Товар:** `{escape_md(sw_name)}` — `{escape_md(product.duration)}`\n"
        f"💳 **Списано с баланса:** `{product.price:.2f} грн`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 **Ваш лицензионный ключ:**\n`{escape_md(license_key.key_string)}`\n\n"
        f"📌 *Нажмите на ключ, чтобы скопировать.*\n"
        f"💾 *Ключ надёжно сохранён в разделе «👤 Мой профиль».*\n\n"
        f"🎰 **Каждая покупка — это шанс выиграть приз!**\n"
        f"Крутите колесо фортуны ниже 👇\n\n"
        f"⭐ **Будем благодарны за ваш отзыв о товаре!**"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Крутить колесо фортуны!", callback_data=f"spin_wheel_{purchase.id}")],
        [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="leave_review"),
         InlineKeyboardButton(text="📢 Канал отзывов", url=REVIEWS_CHANNEL_URL)],
        [InlineKeyboardButton(text="👤 Посмотреть мои ключи", callback_data="profile")],
        [InlineKeyboardButton(text="🛒 Купить еще софт", callback_data=f"platform_{software.platform_id}" if software else "shop")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await callback.message.edit_text(success_text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

# ==========================================
# 🎰 КОЛЕСО ФОРТУНЫ ПОСЛЕ ПОКУПКИ
# ==========================================
# Один спин на одну покупку (обеспечено уникальным purchase_id в WheelSpin +
# перехватом IntegrityError на случай двойного нажатия/гонки).
# Шансы: 5% — ключ на 1 день ЛЮБОГО СОФТА НА ВЫБОР пользователя,
# 5% — 50 грн на баланс, 90% — мимо.
@router.callback_query(F.data.startswith("spin_wheel_"))
async def spin_wheel_handler(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    purchase_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    purchase = await session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != user_id:
        await callback.answer("❌ Эта покупка вам не принадлежит.", show_alert=True)
        return

    already_res = await session.execute(select(WheelSpin).filter_by(purchase_id=purchase_id))
    if already_res.scalars().first():
        await callback.answer("🎰 Вы уже крутили колесо по этой покупке!", show_alert=True)
        return

    # Небольшая анимация "кручения" для эффекта — несколько быстрых кадров
    frames = ["🎰 🍒 ▪️ ▪️", "🎰 ▪️ 🔑 ▪️", "🎰 ▪️ ▪️ 💰", "🎰 ✨ ✨ ✨"]
    for frame in frames:
        try:
            await callback.message.edit_text(
                f"{frame}\n\n_Крутим колесо фортуны..._",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await asyncio.sleep(0.4)

    roll = random.random()
    prize_kind = "lose"
    if roll < WHEEL_KEY_CHANCE:
        prize_kind = "key"
    elif roll < WHEEL_KEY_CHANCE + WHEEL_BALANCE_CHANCE:
        prize_kind = "balance"

    # НОВОЕ: если выпал ключ — не выдаём случайный софт сразу, а даём
    # пользователю выбрать самому из софтов, у которых есть ключи на 1 день.
    # Чтобы нельзя было "перекрутить" колесо, пока выбираешь софт, факт
    # выигрыша фиксируется в БД сразу (status="key_pending") — выбор влияет
    # только на то, КАКОЙ именно ключ будет выдан, а не на сам факт приза.
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
            # Ключей на 1 день нигде нет в наличии — не обманываем ожидание,
            # заменяем приз на баланс
            prize_kind = "balance"
        else:
            spin = WheelSpin(purchase_id=purchase_id, user_id=user_id, result="key_pending")
            session.add(spin)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                await callback.message.edit_text(
                    "🎰 Вы уже крутили колесо по этой покупке!",
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
            await callback.message.edit_text(
                f"🎉 **ДЖЕКПОТ!** 🎉\n\n"
                f"Вы выиграли ключ на **{WHEEL_KEY_DURATION}**!\n"
                f"👇 Выберите, какой софт хотите получить:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="Markdown"
            )
            await callback.answer()
            return

    user_res = await session.execute(select(User).where(User.id == user_id).with_for_update())
    user = user_res.scalar_one_or_none()

    result_text = ""
    final_kind = "lose"

    if prize_kind == "balance":
        if user:
            user.balance += WHEEL_BALANCE_PRIZE
            log_balance_change(session, user.id, WHEEL_BALANCE_PRIZE, "wheel_bonus", user.balance, related_id=purchase_id)
        final_kind = "balance"
        result_text = (
            f"🎉 **Поздравляем!** 🎉\n\n"
            f"Вы выиграли **{WHEEL_BALANCE_PRIZE:.0f} грн** на баланс!\n"
            f"💰 Новый баланс: `{(user.balance if user else 0):.2f} грн`"
        )
    else:
        result_text = (
            f"🎲 **Почти!**\n\n"
            f"В этот раз не повезло — но колесо ждёт вас после следующей покупки! 🍀"
        )

    spin = WheelSpin(purchase_id=purchase_id, user_id=user_id, result=final_kind)
    session.add(spin)

    try:
        await session.commit()
    except IntegrityError:
        # Гонка: два спина по одной покупке почти одновременно —
        # первый уже зачёл приз, второй откатываем целиком.
        await session.rollback()
        await callback.message.edit_text(
            "🎰 Вы уже крутили колесо по этой покупке!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
            ])
        )
        await callback.answer()
        return

    await callback.message.edit_text(
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
                    f"🎰 Пользователь `{user_id}` выиграл приз в колесе фортуны: {WHEEL_BALANCE_PRIZE:.0f} грн",
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

    spin = await session.get(WheelSpin, spin_id)
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
        .with_for_update(skip_locked=True)
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
            user_res = await session.execute(select(User).where(User.id == user_id).with_for_update())
            user = user_res.scalar_one_or_none()
            if user:
                user.balance += WHEEL_BALANCE_PRIZE
                log_balance_change(session, user.id, WHEEL_BALANCE_PRIZE, "wheel_bonus", user.balance, related_id=spin.purchase_id)
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
        product_name=f"🎁 Приз колеса фортуны: {software.name}",
        key_issued=license_key.key_string,
        duration=WHEEL_KEY_DURATION,
    )
    session.add(bonus_purchase)
    spin.result = "key"
    await session.commit()

    await callback.message.edit_text(
        f"🎉 **Готово!** 🎉\n\n"
        f"📦 `{escape_md(software.name)}` — **{WHEEL_KEY_DURATION}**\n"
        f"🔑 `{escape_md(license_key.key_string)}`\n\n"
        f"💾 Ключ уже сохранён в разделе «👤 Мой профиль».",
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
    await state.clear()
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
        f"Приглашайте друзей по вашей ссылке и получайте **5%** от суммы каждой их покупки прямо на баланс!\n\n"
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
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
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

    card = message.text.strip()
    # Простая валидация номера карты: 13-19 цифр, допускаются пробелы
    digits_only = card.replace(" ", "")
    if not digits_only.isdigit() or not (13 <= len(digits_only) <= 19):
        await message.answer(
            "❌ Некорректный номер карты. Введите номер карты (13-19 цифр):",
            reply_markup=cancel_kb("ref_program")
        )
        return

    data = await state.get_data()
    amount = data.get("amount")
    if amount is None:
        await message.answer("⚠️ Сессия вывода устарела, начните заново через «Реферальная система».")
        await state.clear()
        return

    user_id = message.from_user.id

    # ФИКС: блокируем строку пользователя, чтобы исключить одновременное создание
    # нескольких заявок на вывод, суммарно превышающих реальный баланс.
    user_res = await session.execute(select(User).where(User.id == user_id).with_for_update())
    user = user_res.scalar_one_or_none()
    if not user or user.balance < amount:
        await message.answer("❌ Недостаточно средств на балансе.")
        await state.clear()
        return

    user.balance -= amount
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
    req = await session.get(WithdrawalRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    req.status = "approved"
    await session.commit()

    current_text = callback.message.text or ""
    await callback.message.edit_text(text=current_text + "\n\n✅ **ВЫПЛАЧЕНО**", reply_markup=None, parse_mode="Markdown")
    try:
        await bot.send_message(
            req.user_id,
            f"🎉 **Ваша заявка на вывод `{req.amount:.2f} грн` успешно обработана!**\nСредства отправлены на карту `{escape_md(req.card)}`.",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("reject_wdr_"), F.from_user.id.in_(ADMIN_IDS))
async def reject_withdrawal(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    req_id = int(callback.data.split("_")[2])
    req = await session.get(WithdrawalRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    req.status = "rejected"

    # ФИКС: блокировка строки пользователя при возврате средств
    user_res = await session.execute(select(User).where(User.id == req.user_id).with_for_update())
    user = user_res.scalar_one_or_none()
    if user:
        user.balance += req.amount
        log_balance_change(session, user.id, req.amount, "withdrawal_rejected_refund", user.balance, related_id=req.id)

    await session.commit()

    current_text = callback.message.text or ""
    await callback.message.edit_text(text=current_text + "\n\n❌ **ОТКЛОНЕНО (Средства возвращены)**", reply_markup=None, parse_mode="Markdown")
    try:
        await bot.send_message(
            req.user_id,
            f"❌ Ваша заявка на вывод `{req.amount:.2f} грн` была отклонена. Средства возвращены на ваш баланс.",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await callback.answer()

# ==========================================
# ПОПОЛНЕНИЕ БАЛАНСА И ПРИЕМ ЧЕКОВ
# ==========================================
@router.callback_query(F.data == "deposit")
async def start_deposit(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
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
    await callback.message.edit_text(
        f"💳 **Пополнение баланса**\n\nВведите сумму пополнения в гривнах числом (минимум {MIN_DEPOSIT:.0f} грн):",
        reply_markup=cancel_kb("main_menu"),
        parse_mode="Markdown"
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
    amount = data.get("amount", 0.0)
    user_id = message.from_user.id

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

    req = DepositRequest(user_id=user_id, amount=amount)
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
    req = await session.get(DepositRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    req.status = "approved"

    # ФИКС: блокировка строки пользователя при зачислении депозита
    user_res = await session.execute(select(User).where(User.id == req.user_id).with_for_update())
    user = user_res.scalar_one_or_none()
    if user:
        user.balance += req.amount
        log_balance_change(session, user.id, req.amount, "deposit_approved", user.balance, related_id=req.id)
    await session.commit()

    if callback.message.caption is not None:
        await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n✅ ПОДТВЕРЖДЕНО", reply_markup=None)
    else:
        await callback.message.edit_text(text=(callback.message.text or "") + "\n\n✅ ПОДТВЕРЖДЕНО", reply_markup=None)

    try:
        await bot.send_message(req.user_id, f"🎉 **Баланс успешно пополнен на {req.amount:.2f} грн!**", parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("reject_dep_"), F.from_user.id.in_(ADMIN_IDS))
async def reject_deposit(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    req_id = int(callback.data.split("_")[2])
    req = await session.get(DepositRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    req.status = "rejected"
    await session.commit()

    if callback.message.caption is not None:
        await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n❌ ОТКЛОНЕНО", reply_markup=None)
    else:
        await callback.message.edit_text(text=(callback.message.text or "") + "\n\n❌ ОТКЛОНЕНО", reply_markup=None)

    try:
        await bot.send_message(req.user_id, "❌ Ваша заявка на пополнение была отклонена.")
    except Exception:
        pass
    await callback.answer()

# ==========================================
# ПАНЕЛЬ АДМИНИСТРАТОРА И РАСШИРЕННЫЕ ФУНКЦИИ
# ==========================================
@router.callback_query(F.data == "admin_panel", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text_msg = (
        "🛠 **Панель администратора**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Выберите необходимое действие:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить софт", callback_data="admin_add_sw"),
             InlineKeyboardButton(text="🗑 Удалить софт", callback_data="admin_del_sw_select")],
            [InlineKeyboardButton(text="🖼 Фото/описание софта", callback_data="admin_sw_media_select")],
            [InlineKeyboardButton(text="➕ Создать тариф", callback_data="admin_add_prod"),
             InlineKeyboardButton(text="🗑 Удалить тариф", callback_data="admin_del_prod_select")],
            [InlineKeyboardButton(text="📥 Загрузить ключи", callback_data="admin_add_keys_select"),
             InlineKeyboardButton(text="🗑 Очистить ключи", callback_data="admin_del_keys_select")],
            [InlineKeyboardButton(text="🔋 Пополнение аккаунтов", callback_data="admin_topup_accounts")],
            [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin_create_promo"),
             InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_promo_list")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_start_broadcast")],
            [InlineKeyboardButton(text="🧾 Неподтвержденные заявки", callback_data="admin_pending_orders")],
            [InlineKeyboardButton(text="👤 Управление пользователем", callback_data="admin_user_manage")],
            [InlineKeyboardButton(text="⭐ Управление отзывами", callback_data="admin_reviews_manage")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ]
    )
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
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


@router.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    # Кнопка-индикатор страницы — ничего не делает, просто гасит "часики" на кнопке
    await callback.answer()


# --- МОДЕРАЦИЯ И УПРАВЛЕНИЕ ОТЗЫВАМИ ---
@router.callback_query(F.data == "admin_reviews_manage", F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data.startswith("admin_rev_page_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_reviews_list(callback: CallbackQuery, session: AsyncSession):
    page = 0
    if callback.data.startswith("admin_rev_page_"):
        page = int(callback.data.split("_")[3])

    per_page = 5
    offset = page * per_page

    total_res = await session.execute(select(func.count(Review.id)))
    total_reviews = total_res.scalar() or 0

    if total_reviews == 0:
        await callback.message.edit_text(
            "⭐ **Управление отзывами**\n\nВ базе данных пока нет отзывов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
            parse_mode="Markdown"
        )
        await callback.answer()
        return

    res = await session.execute(
        select(Review).order_by(Review.created_at.desc()).offset(offset).limit(per_page)
    )
    reviews = res.scalars().all()

    text_msg = f"⭐ **Управление отзывами** (Страница {page + 1})\nВсего отзывов: `{total_reviews}`\n━━━━━━━━━━━━━━━━━━━\n"
    buttons = []

    for r in reviews:
        user_res = await session.get(User, r.user_id)
        uname = f"@{user_res.username}" if user_res and user_res.username else f"ID: {r.user_id}"
        snippet = r.text[:35] + "..." if len(r.text) > 35 else r.text
        text_msg += f"🔹 **ID {r.id}** | {uname} | {'⭐' * r.rating}\n_{escape_md(snippet)}_\n──────────────\n"
        buttons.append([InlineKeyboardButton(text=f"🗑 Удалить отзыв #{r.id}", callback_data=f"admin_del_review_confirm_{r.id}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_rev_page_{page - 1}"))
    if offset + per_page < total_reviews:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_rev_page_{page + 1}"))

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        text_msg,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

# НОВОЕ: подтверждение перед удалением отзыва (раньше удалялся сразу без подтверждения)
@router.callback_query(F.data.startswith("admin_del_review_confirm_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_delete_review_confirm(callback: CallbackQuery, session: AsyncSession):
    review_id = int(callback.data.split("_")[4])
    review = await session.get(Review, review_id)
    if not review:
        await callback.answer("❌ Отзыв не найден или уже был удален!", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить отзыв", callback_data=f"admin_del_review_{review_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_reviews_manage")]
    ])
    await callback.answer()
    if callback.message.text and "Новый отзыв от покупателя" in callback.message.text:
        # Отзыв прислан прямо в личку админу — редактируем это же сообщение
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    else:
        await callback.message.edit_text(
            f"⚠️ Удалить отзыв #{review_id} безвозвратно?",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

@router.callback_query(F.data.startswith("admin_del_review_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_delete_review(callback: CallbackQuery, session: AsyncSession):
    review_id = int(callback.data.split("_")[3])
    review = await session.get(Review, review_id)

    if not review:
        await callback.answer("❌ Отзыв не найден или уже был удален!", show_alert=True)
        return

    await session.delete(review)
    await session.commit()

    await callback.answer("✅ Отзыв успешно удален!", show_alert=True)

    if callback.message.text and "Новый отзыв от покупателя" in callback.message.text:
        await callback.message.edit_text(
            callback.message.text + "\n\n🗑 **ОТЗЫВ УДАЛЕН АДМИНИСТРАТОРОМ**",
            reply_markup=None,
            parse_mode="Markdown"
        )
    else:
        await admin_reviews_list(callback, session)

# --- СОЗДАНИЕ ПРОМОКОДА АДМИНОМ ---
@router.callback_query(F.data == "admin_create_promo", F.from_user.id.in_(ADMIN_IDS))
async def admin_create_promo_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_promo_code)
    await callback.message.edit_text(
        "🎟 **Создание промокода (1/3)**\n\nВведите текстовый код промокода (например: `START2026`):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_promo_code, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_code_entered(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Введите текст промокода.")
        return
    code = message.text.strip()

    if len(code) > MAX_PROMO_CODE_LENGTH:
        await message.answer(
            f"❌ Код слишком длинный ({len(code)} символов). Максимум — {MAX_PROMO_CODE_LENGTH}. Введите другой:",
            reply_markup=cancel_kb("admin_panel")
        )
        return

    # ФИКС: проверяем уникальность кода заранее, чтобы дать админу понятную ошибку,
    # а не падать с необработанным IntegrityError на последнем шаге.
    existing = await session.execute(select(PromoCode).filter_by(code=code))
    if existing.scalars().first():
        await message.answer(
            f"❌ Промокод `{escape_md(code)}` уже существует. Введите другой код:",
            reply_markup=cancel_kb("admin_panel"),
            parse_mode="Markdown"
        )
        return

    await state.update_data(code=code)
    await state.set_state(AdminStates.waiting_for_promo_amount)
    await message.answer("🎟 **Создание промокода (2/3)**\n\nВведите бонусную сумму в гривнах (число):", reply_markup=cancel_kb("admin_panel"))

@router.message(AdminStates.waiting_for_promo_amount, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_amount_entered(message: Message, state: FSMContext):
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer("❌ Введите корректное положительное число для суммы.")
        return
    await state.update_data(amount=amount)
    await state.set_state(AdminStates.waiting_for_promo_uses)
    await message.answer("🎟 **Создание промокода (3/3)**\n\nВведите максимальное количество активаций (число):", reply_markup=cancel_kb("admin_panel"))

@router.message(AdminStates.waiting_for_promo_uses, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_uses_entered(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text or not message.text.strip().lstrip("-").isdigit():
        await message.answer("❌ Введите целое число для активаций.")
        return
    uses = int(message.text.strip())
    if uses <= 0:
        await message.answer("❌ Количество активаций должно быть больше нуля.")
        return

    data = await state.get_data()
    code = data.get("code")
    amount = data.get("amount")

    try:
        promo = PromoCode(code=code, amount=amount, uses_left=uses)
        session.add(promo)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await message.answer(
            f"❌ Промокод `{escape_md(code)}` уже существует (создан параллельно). Начните заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
            parse_mode="Markdown"
        )
        await state.clear()
        return

    await state.clear()
    await message.answer(
        f"✅ Промокод `{escape_md(code)}` на `{amount:.2f} грн` (активаций: `{uses}`) успешно создан!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
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

    total_res = await session.execute(select(func.count(PromoCode.id)))
    total_promos = total_res.scalar() or 0

    if total_promos == 0:
        await callback.message.edit_text(
            "📋 **Список промокодов**\n\nПромокодов пока не создано.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
            parse_mode="Markdown"
        )
        await callback.answer()
        return

    res = await session.execute(
        select(PromoCode).order_by(PromoCode.id.desc()).offset(offset).limit(per_page)
    )
    promos = res.scalars().all()

    text_msg = f"📋 **Список промокодов** (Страница {page + 1})\nВсего: `{total_promos}`\n━━━━━━━━━━━━━━━━━━━\n"
    buttons = []

    for p in promos:
        status_icon = "🟢" if p.uses_left > 0 else "🔴"
        text_msg += (
            f"{status_icon} **ID {p.id}** | `{escape_md(p.code)}` | "
            f"{p.amount:.2f} грн | осталось активаций: `{p.uses_left}`\n"
        )
        buttons.append([InlineKeyboardButton(text=f"🗑 Удалить «{p.code}»", callback_data=f"confirm_del_promo_{p.id}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_promo_page_{page - 1}"))
    if offset + per_page < total_promos:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_promo_page_{page + 1}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin_create_promo")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        text_msg,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
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
    await callback.message.edit_text(
        f"⚠️ **Подтверждение удаления**\n\n"
        f"Удалить промокод `{escape_md(promo.code)}` (`{promo.amount:.2f} грн`, "
        f"осталось активаций: `{promo.uses_left}`)?\n\n"
        f"*История уже совершённых активаций сохранится в логах баланса.*",
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

    code_name = promo.code
    # Сначала удаляем записи об активациях (нет каскада на уровне ORM-связи),
    # затем сам промокод, чтобы не упереться в FK-констрейнт.
    await session.execute(delete(PromoCodeUsage).where(PromoCodeUsage.promo_id == promo_id))
    await session.delete(promo)
    await session.commit()

    await callback.answer("✅ Промокод удален!", show_alert=True)
    await admin_promo_list_handler(callback, session)

# --- РАССЫЛКА СООБЩЕНИЙ ---
@router.callback_query(F.data == "admin_start_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    await callback.message.edit_text(
        "📢 **Рассылка сообщений**\n\nОтправьте текст или сообщение для рассылки всем пользователям бота:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_broadcast_text, F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_execute(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    await state.clear()
    users_res = await session.execute(select(User.id))
    user_ids = users_res.scalars().all()

    success = 0
    blocked = 0
    await message.answer(f"⏳ Рассылка началась по {len(user_ids)} пользователям...")

    for uid in user_ids:
        # ФИКС: раньше при повторном флуд-лимите на 2-й попытке цикл просто
        # заканчивался без break — пользователь не попадал ни в success,
        # ни в blocked, и итоговая статистика не билась с реальным числом
        # получателей. Теперь ограничиваем число ретраев и всегда считаем
        # исход (успех или "не доставлено").
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                await message.send_copy(chat_id=uid)
                success += 1
                await asyncio.sleep(0.05)
                break
            except TelegramRetryAfter as e:
                if attempt == max_attempts - 1:
                    blocked += 1
                    break
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramForbiddenError:
                blocked += 1
                break
            except Exception:
                blocked += 1
                break

    await message.answer(
        f"✅ **Рассылка завершена!**\n\n• Успешно доставлено: `{success}`\n• Не доставлено: `{blocked}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

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
    user_res = await session.execute(select(User).where(User.id == uid).with_for_update())
    user = user_res.scalar_one_or_none()
    if not user:
        await message.answer("❌ Пользователь не найден.")
        await state.clear()
        return

    old_balance = user.balance

    if is_relative:
        new_balance = round(user.balance + val, 2)
        if new_balance < 0:
            await message.answer(f"❌ Итоговый баланс не может быть отрицательным (получилось бы {new_balance:.2f} грн).")
            return
        user.balance = new_balance
    else:
        if val < 0:
            await message.answer("❌ Баланс не может быть отрицательным.")
            return
        user.balance = round(val, 2)

    # НОВОЕ: логируем ручную корректировку баланса администратором
    delta = round(user.balance - old_balance, 2)
    log_balance_change(session, user.id, delta, "admin_adjust", user.balance, related_id=message.from_user.id)

    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Баланс пользователя `{uid}` успешно изменен. Новый баланс: `{user.balance:.2f} грн`.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# --- СОЗДАНИЕ ТАРИФОВ И УПРАВЛЕНИЕ СОФТОМ ---
@router.callback_query(F.data == "admin_add_sw", F.from_user.id.in_(ADMIN_IDS))
async def add_sw_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.set_state(AdminStates.waiting_for_software_platform)
    text_msg = "➕ **Добавление софта (Шаг 1/2)**\n\nВыберите платформу:"

    # ИЗМЕНЕНО: список платформ берётся из БД динамически (а не хардкодится),
    # чтобы новые разделы (например "Аккаунты • Taiwan") появлялись тут сами.
    res = await session.execute(select(Platform))
    platforms = res.scalars().all()
    buttons = []
    for p in platforms:
        name_lower = p.name.lower()
        if "android" in name_lower:
            icon = "🤖"
        elif "ios" in name_lower:
            icon = "🍏"
        elif "аккаунт" in name_lower:
            icon = "🎮"
        else:
            icon = "📦"
        buttons.append([InlineKeyboardButton(text=f"{icon} {p.name}", callback_data=f"sw_platform_{p.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("sw_platform_"), F.from_user.id.in_(ADMIN_IDS))
async def add_sw_platform_chosen(callback: CallbackQuery, state: FSMContext):
    platform_id = int(callback.data.split("_")[2])
    await state.update_data(platform_id=platform_id)
    await state.set_state(AdminStates.waiting_for_software_name)

    text_msg = "➕ **Добавление софта (Шаг 2/2)**\n\nВведите название софта (например: `Flexnote`):"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_add_sw")]])
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_software_name, F.from_user.id.in_(ADMIN_IDS))
async def save_software(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Введите название софта текстом.")
        return

    name = message.text.strip()
    if len(name) > MAX_SOFTWARE_NAME_LENGTH:
        await message.answer(
            f"❌ Название слишком длинное ({len(name)} символов). Максимум — {MAX_SOFTWARE_NAME_LENGTH}. Введите другое:"
        )
        return

    try:
        data = await state.get_data()
        platform_id = data.get("platform_id")

        platform = await session.get(Platform, platform_id)
        if not platform:
            await message.answer(f"❌ Платформа ID `{platform_id}` не найдена в системе.")
            await state.clear()
            return

        new_sw = Software(platform_id=platform_id, name=name)
        session.add(new_sw)
        await session.commit()
        await state.clear()

        await message.answer(
            f"✅ Софт `{escape_md(name)}` успешно добавлен!\n🔑 ID софта: `{new_sw.id}`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
            parse_mode="Markdown"
        )
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка сохранения софта: {e}")
        await message.answer("⚠️ Не удалось сохранить софт.")

# --- ФОТО / ОПИСАНИЕ СОФТА ---
@router.callback_query(F.data == "admin_sw_media_select", F.from_user.id.in_(ADMIN_IDS))
async def admin_sw_media_select(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(select(Software))
    softwares = res.scalars().all()
    if not softwares:
        await callback.answer("❌ Сначала добавьте хотя бы один софт!", show_alert=True)
        return

    buttons = []
    for sw in softwares:
        mark = "🖼" if sw.photo_file_id else "🛡"
        buttons.append([InlineKeyboardButton(text=f"{mark} {sw.name}", callback_data=f"sw_media_sel_{sw.id}")])
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
    res = await session.execute(select(Software))
    softwares = res.scalars().all()
    if not softwares:
        await callback.answer("❌ Сначала добавьте хотя бы один софт!", show_alert=True)
        return

    buttons = []
    for sw in softwares:
        buttons.append([InlineKeyboardButton(text=f"🛡 {sw.name}", callback_data=f"prod_sw_sel_{sw.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        "➕ **Создание тарифа (1/3)**\n\nВыберите софт, к которому привязать тариф:",
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
        "➕ **Создание тарифа (2/3)**\n\nВыберите или введите длительность тарифа (например: `1 день`, `7 дней`, `30 дней`):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("prod_dur_sel_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_add_prod_duration_callback(callback: CallbackQuery, state: FSMContext):
    duration = callback.data.replace("prod_dur_sel_", "")
    await state.update_data(duration=duration)
    await state.set_state(AdminStates.waiting_for_price)

    await callback.message.edit_text(
        f"➕ **Создание тарифа (3/3)**\nВыбран срок: `{duration}`\n\nВведите цену в гривнах (числом):",
        reply_markup=cancel_kb("admin_panel"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_duration, F.from_user.id.in_(ADMIN_IDS))
async def admin_add_prod_duration_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Введите текстовое значение для срока.")
        return
    duration = message.text.strip()
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
    duration = data.get("duration")

    product = Product(software_id=sw_id, duration=duration, price=price)
    session.add(product)
    await session.commit()
    await state.clear()

    sw = await session.get(Software, sw_id)
    sw_name = sw.name if sw else "Софт"

    await message.answer(
        f"✅ Тариф `{escape_md(sw_name)}` — `{escape_md(duration)}` по цене `{price:.2f} грн` успешно создан!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# --- УДАЛЕНИЕ СОФТА И ТАРИФОВ ---
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

    await session.delete(prod)
    await session.commit()

    await callback.message.edit_text(
        "✅ Тариф и все связанные с ним ключи успешно удалены.",
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
        [InlineKeyboardButton(text=f"🔥 Удалить абсолютно ВСЕ ключи", callback_data=f"confirm_del_all_keys_{prod_id}")],
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
        await message.answer("❌ Отправьте ключи текстом.")
        return

    data = await state.get_data()
    prod_id = data.get("product_id")

    # Убираем дубликаты внутри самого списка, сохраняя порядок
    raw_keys = [k.strip() for k in message.text.strip().split("\n") if k.strip()]
    seen = set()
    keys = []
    for k in raw_keys:
        if k not in seen:
            seen.add(k)
            keys.append(k)

    # ФИКС: раньше при ошибке на одном ключе вызывался session.rollback(),
    # который откатывал ВСЮ транзакцию целиком (включая уже "успешно" добавленные
    # в этом же цикле ключи), но added_count при этом не корректировался —
    # в итоге бот мог отчитаться о добавлении ключей, которых нет в базе.
    # Теперь сначала находим уже существующие ключи одним запросом,
    # затем одной транзакцией добавляем только новые.
    dup_in_db = set()
    if keys:
        existing_res = await session.execute(
            select(LicenseKey.key_string).where(LicenseKey.key_string.in_(keys))
        )
        dup_in_db = set(existing_res.scalars().all())

    new_keys = [k for k in keys if k not in dup_in_db]
    skipped_count = len(raw_keys) - len(new_keys)

    added_count = 0
    if new_keys:
        session.add_all([LicenseKey(product_id=prod_id, key_string=k) for k in new_keys])
        try:
            await session.commit()
            added_count = len(new_keys)
        except IntegrityError:
            await session.rollback()
            await message.answer("⚠️ Не удалось сохранить ключи из-за конфликта данных. Попробуйте отправить их заново.")
            return
    else:
        await session.rollback()

    if added_count > 0:
        subs_res = await session.execute(select(RestockSubscription).where(RestockSubscription.product_id == prod_id))
        subs = subs_res.scalars().all()
        prod = await session.get(Product, prod_id)
        sw = await session.get(Software, prod.software_id) if prod else None
        sw_name = sw.name if sw else "Товар"

        for sub in subs:
            try:
                await bot.send_message(
                    sub.user_id,
                    f"🔔 **Пополнение товара!**\n\nТовар **{escape_md(sw_name)}** ({escape_md(prod.duration)}) снова в наличии!",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            await session.delete(sub)
        await session.commit()

    result_text = f"✅ Успешно добавлено `{added_count}` ключей!"
    if skipped_count > 0:
        result_text += f"\n⚠️ Пропущено дубликатов: `{skipped_count}`"

    await message.answer(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
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
    lang = user.language if user and user.language in LANGUAGES else "uk"
    balance = user.balance if user else 0.0
    await message.answer(
        get_main_menu_text(balance, lang),
        reply_markup=get_main_menu_kb(message.from_user.id, lang),
        parse_mode="Markdown"
    )

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
    return web.json_response({"status": "ok"})


async def index_page(request: web.Request) -> web.Response:
    return web.Response(text=RENDER_STUB_HTML, content_type="text/html")


async def run_stub_server() -> None:
    # Лёгкий HTTP-сервер только для того, чтобы Render Free видел открытый
    # порт и не считал сервис "упавшим". Никакой бизнес-логики здесь нет.
    app = web.Application()
    app.router.add_get("/", index_page)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"HTTP-заглушка запущена на порту {PORT}")


ACCOUNTS_PLATFORM_NAME = "Аккаунты • Taiwan"
ACCOUNTS_SOFTWARE_NAME = "PUBG MOBILE"
ACCOUNTS_PRODUCT_DURATION = "1 аккаунт"
ACCOUNTS_PRODUCT_PRICE = 60.0
ACCOUNTS_DESCRIPTION = (
    "Персональный аккаунт PUBG MOBILE Taiwan: 12-й уровень Metro и 12-й "
    "уровень Classic. На аккаунте не будет других пользователей. Данные "
    "аккаунта не передаются — вход выполняется через персональную "
    "QR-панель в профиле. Версия Taiwan лучше всего подходит для игры с "
    "нашими продуктами: условия использования заметно безопаснее, чем в "
    "других версиях."
)


async def seed_platforms(session_pool) -> None:
    # Гарантируем, что платформы Android (id=1) и iOS (id=2) существуют,
    # так как на них жёстко завязаны callback_data "sw_platform_1"/"sw_platform_2"
    # в разделе добавления софта администратором.
    async with session_pool() as session:
        existing_res = await session.execute(select(Platform.name))
        existing_names = set(existing_res.scalars().all())

        to_add = []
        if "Android" not in existing_names:
            to_add.append(Platform(name="Android"))
        if "iOS" not in existing_names:
            to_add.append(Platform(name="iOS"))
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
            sw_res = await session.execute(
                select(Software).filter_by(platform_id=accounts_platform.id, name=ACCOUNTS_SOFTWARE_NAME)
            )
            sw = sw_res.scalar_one_or_none()
            if not sw:
                sw = Software(
                    platform_id=accounts_platform.id,
                    name=ACCOUNTS_SOFTWARE_NAME,
                    description=ACCOUNTS_DESCRIPTION,
                )
                session.add(sw)
                await session.commit()
                logger.info(f"Добавлен софт '{ACCOUNTS_SOFTWARE_NAME}' в раздел '{ACCOUNTS_PLATFORM_NAME}'.")

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
    # НОВОЕ: ручные миграции для изменений схемы в уже существующих таблицах.
    # Base.metadata.create_all создаёт только отсутствующие таблицы и никогда
    # не делает ALTER TABLE для уже существующих — поэтому изменения типов
    # колонок нужно применять здесь вручную. Выполняется при каждом старте,
    # но идемпотентно: если колонка уже нужного типа, Postgres не ошибается.
    if not DATABASE_URL.startswith("postgresql"):
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
        logger.info("Миграция: softwares.photo_file_id / description добавлены.")
    except Exception as e:
        logger.warning(f"Миграция softwares photo/description не выполнена: {e}")


async def main() -> None:
    if not BOT_TOKEN:
        logger.error("Переменная окружения BOT_TOKEN не задана. Останавливаю запуск.")
        return

    # Создаём таблицы, если их ещё нет
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await run_migrations()
    await seed_platforms(async_session_maker)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DbSessionMiddleware(session_pool=async_session_maker))
    dp.include_router(router)

    # HTTP-заглушка нужна Render Free, чтобы не убивать процесс из-за
    # отсутствия открытого порта — поднимаем её параллельно с polling
    await run_stub_server()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен, начинаю polling...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
