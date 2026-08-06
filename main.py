import asyncio
import logging
import os
import random
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import CommandObject, CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import web
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
    select,
    text,
)
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

raw_admin_ids = os.getenv("ADMIN_IDS", "8479717148")
ADMIN_IDS = [int(x.strip()) for x in raw_admin_ids.split(",") if x.strip().isdigit()]

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


def escape_md(value) -> str:
    if value is None:
        return ""
    value = str(value)
    for ch in ("\\", "_", "*", "`", "["):
        value = value.replace(ch, "\\" + ch)
    return value


def user_display(message_or_callback) -> str:
    u = message_or_callback.from_user
    username = u.username
    if username:
        return f"@{escape_md(username)}"
    return "без юзернейма"


# ==========================================
# ЛОКАЛИЗАЦИЯ И ЯЗЫКИ
# ==========================================
LANGUAGES = {
    "uk": {
        "welcome": "💎 **Ласкаво просимо в Kranin Shop!**\n⚡️ Надійний сервіс цифрових ліцензій та софту.\n\n💰 **Ваш баланс:** `{balance:.2f} грн`\n\n👇 Виберіть потрібний розділ у меню нижче:",
        "banned": "❌ Ваш акаунт заблоковано в боті.",
        "lang_changed": "✅ Мову успішно змінено на українську.",
        "lang_btn": "🌐 Змінити мову / Сменить язык"
    },
    "ru": {
        "welcome": "💎 **Добро пожаловать в Kranin Shop!**\n⚡️ Надежный сервис цифровых лицензий и софта.\n\n💰 **Ваш баланс:** `{balance:.2f} грн`\n\n👇 Выберите интересующий раздел в меню ниже:",
        "banned": "❌ Ваш аккаунт заблокирован в боте.",
        "lang_changed": "✅ Язык успешно изменен на русский.",
        "lang_btn": "🌐 Изменить язык / Змінити мову"
    }
}

def get_language_kb():
    keyboard = [
        [InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang_uk"),
         InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")]
    ]
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

class PromoCode(Base):
    __tablename__ = "promo_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    amount: Mapped[float] = mapped_column(Float)
    uses_left: Mapped[int] = mapped_column(Integer, default=1)

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
                    referrer_id = possible_ref

            user = User(
                id=message.from_user.id,
                username=message.from_user.username,
                referred_by=referrer_id,
                language="uk"
            )
            session.add(user)
            await session.commit()
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

@router.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await session.get(User, callback.from_user.id)
    balance = user.balance if user else 0.0
    lang = user.language if user and user.language in LANGUAGES else "uk"
    
    await callback.message.edit_text(
        get_main_menu_text(balance, lang),
        reply_markup=get_main_menu_kb(callback.from_user.id, lang),
        parse_mode="Markdown"
    )
    await callback.answer()

# ==========================================
# СМЕНА ЯЗЫКА
# ==========================================
@router.callback_query(F.data == "change_lang")
async def change_lang_handler(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌐 **Виберіть мову / Выберите язык:**",
        reply_markup=get_language_kb(),
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
    res = await session.execute(select(PromoCode).filter_by(code=code_text))
    promo = res.scalars().first()

    if not promo or promo.uses_left <= 0:
        await message.answer("❌ Промокод не найден или срок его активаций исчерпан!", reply_markup=cancel_kb("main_menu"))
        await state.clear()
        return

    user = await session.get(User, message.from_user.id)
    user.balance += promo.amount
    promo.uses_left -= 1
    await session.commit()
    await state.clear()

    lang = user.language if user and user.language in LANGUAGES else "uk"
    await message.answer(
        f"🎉 **Промокод успешно активирован!**\nВам начислено `{promo.amount:.2f} грн` на баланс.",
        reply_markup=get_main_menu_kb(message.from_user.id, lang),
        parse_mode="Markdown"
    )

# ==========================================
# ОТЗЫВЫ
# ==========================================
@router.callback_query(F.data == "show_reviews")
async def show_reviews_handler(callback: CallbackQuery, session: AsyncSession):
    res = await session.execute(select(Review).order_by(Review.created_at.desc()).limit(5))
    reviews = res.scalars().all()

    text_msg = "⭐ **Последние отзывы клиентов**\n━━━━━━━━━━━━━━━━━━━\n\n"
    if not reviews:
        text_msg += "Пока нет ни одного отзыва. Будьте первыми!"
    else:
        for r in reviews:
            stars = "⭐" * r.rating
            date_str = r.created_at.strftime("%d.%m.%Y")
            text_msg += f"{stars} от `ID {r.user_id}` ({date_str})\n💬 _{escape_md(r.text)}_\n──────────────\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="leave_review")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "leave_review")
async def leave_review_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    user_id = callback.from_user.id
    purchases_res = await session.execute(select(Purchase).where(Purchase.user_id == user_id))
    if not purchases_res.scalars().first():
        await callback.answer("❌ Вы можете оставлять отзывы только после совершения покупок в боте!", show_alert=True)
        return

    await state.set_state(UserStates.waiting_for_review_text)
    await callback.message.edit_text(
        "✍️ **Оставить отзыв**\n\nНапишите ваш текст отзыва (можно указать оценку от 1 до 5 в начале, например: `5 Отличный софт!`):",
        reply_markup=cancel_kb("show_reviews"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(UserStates.waiting_for_review_text)
async def save_review_handler(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Отправьте текст отзыва.", reply_markup=cancel_kb("show_reviews"))
        return
    text_val = message.text.strip()
    rating = 5
    if text_val[0].isdigit() and int(text_val[0]) in range(1, 6):
        rating = int(text_val[0])
        text_val = text_val[1:].strip()

    review = Review(user_id=message.from_user.id, rating=rating, text=text_val)
    session.add(review)
    await session.commit()
    await state.clear()

    user = await session.get(User, message.from_user.id)
    lang = user.language if user and user.language in LANGUAGES else "uk"

    await message.answer(
        "✅ **Спасибо за ваш отзыв!** Он опубликован в общем списке.",
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

    buttons = []
    for idx, d in enumerate(DURATIONS):
        cnt_res = await session.execute(
            select(func.count(Purchase.id)).where(Purchase.user_id == user_id, Purchase.duration == d)
        )
        cnt = cnt_res.scalar() or 0
        buttons.append([InlineKeyboardButton(text=f"📂 Срок: {d} (активных ключей: {cnt})", callback_data=f"profile_dur_{idx}")])

    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("profile_dur_"))
async def show_profile_keys(callback: CallbackQuery, session: AsyncSession):
    try:
        idx = int(callback.data.split("_")[2])
        duration = DURATIONS[idx]
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректная категория!", show_alert=True)
        return

    user_id = callback.from_user.id
    res = await session.execute(
        select(Purchase)
        .where(Purchase.user_id == user_id, Purchase.duration == duration)
        .order_by(Purchase.purchased_at.desc())
        .limit(20)
    )
    purchases = res.scalars().all()

    if not purchases:
        text_msg = f"🔑 **Ключи ({duration})**\n\nУ вас пока нет купленных ключей в этой категории."
    else:
        lines = [f"🔑 **Ключи ({duration})** — последние покупки:\n"]
        for p in purchases:
            date_str = p.purchased_at.strftime("%d.%m.%Y %H:%M")
            lines.append(
                f"📦 *{escape_md(p.product_name)}*\n"
                f"🔐 `{p.key_issued}`\n"
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
        "🛒 **Магазин лицензий**\n\n"
        "📱 Выберите платформу вашего устройства:"
    )
    buttons = []
    for p in platforms:
        icon = "🤖" if "android" in p.name.lower() else "🍏"
        buttons.append([InlineKeyboardButton(text=f"{icon} {p.name}", callback_data=f"platform_{p.id}")])

    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("platform_"))
async def show_softwares(callback: CallbackQuery, session: AsyncSession):
    platform_id = int(callback.data.split("_")[1])
    platform = await session.get(Platform, platform_id)

    result = await session.execute(select(Software).filter_by(platform_id=platform_id))
    softwares = result.scalars().all()

    if not softwares:
        text_msg = f"🛒 **Каталог — {escape_md(platform.name) if platform else 'Платформа'}**\n\n❌ На данную платформу софт пока не добавлен."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К выбору платформ", callback_data="shop")]])
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()
        return

    text_msg = f"🛒 **Каталог — {escape_md(platform.name)}**\n\n⚡️ Выберите нужный софт:"
    buttons = []
    for sw in softwares:
        buttons.append([InlineKeyboardButton(text=f"🛡 {sw.name}", callback_data=f"software_{sw.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 К платформам", callback_data="shop")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("software_"))
async def show_products(callback: CallbackQuery, session: AsyncSession):
    software_id = int(callback.data.split("_")[1])
    software = await session.get(Software, software_id)
    if not software:
        await callback.answer("❌ Софт не найден!", show_alert=True)
        return

    result = await session.execute(select(Product).filter_by(software_id=software_id))
    products = result.scalars().all()

    if not products:
        text_msg = f"🛒 **{escape_md(software.name)}**\n\n❌ Тарифы для этого софта пока отсутствуют."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К списку софта", callback_data=f"platform_{software.platform_id}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ])
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()
        return

    text_msg = f"🛒 **Софт:** `{escape_md(software.name)}`\n\n📌 Выберите тариф подписки:"
    buttons = []
    for p in products:
        keys_res = await session.execute(select(LicenseKey).filter_by(product_id=p.id, is_sold=False))
        available_keys = len(keys_res.scalars().all())

        if available_keys > 0:
            status_text = f"🟢 В наличии ({available_keys} шт.)"
            buttons.append([InlineKeyboardButton(
                text=f"{status_text} | {p.duration} — {p.price:.2f} грн",
                callback_data=f"buy_product_{p.id}"
            )])
        else:
            status_text = "🔴 Нет в наличии"
            buttons.append([InlineKeyboardButton(
                text=f"{status_text} | {p.duration} — {p.price:.2f} грн",
                callback_data=f"notify_restock_{p.id}"
            )])

    buttons.append([InlineKeyboardButton(text="🔙 К списку софта", callback_data=f"platform_{software.platform_id}")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
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

@router.callback_query(F.data.startswith("buy_product_"))
async def process_purchase(callback: CallbackQuery, session: AsyncSession):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    product = await session.get(Product, product_id)
    if not product:
        await callback.answer("❌ Товар не найден!", show_alert=True)
        return

    user = await session.get(User, user_id)
    software = await session.get(Software, product.software_id)

    if not user or user.balance < product.price:
        await callback.answer(f"❌ Недостаточно средств! Требуется {product.price:.2f} грн", show_alert=True)
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

    if user.referred_by:
        referrer = await session.get(User, user.referred_by)
        if referrer:
            ref_bonus = round(product.price * 0.05, 2)
            referrer.balance += ref_bonus
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

    success_text = (
        f"✅ **Покупка успешно завершена!**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **Товар:** `{escape_md(sw_name)}` — `{escape_md(product.duration)}`\n"
        f"💳 **Списано с баланса:** `{product.price:.2f} грн`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 **Ваш лицензионный ключ:**\n`{license_key.key_string}`\n\n"
        f"📌 *Нажмите на ключ, чтобы скопировать.*\n"
        f"💾 *Ключ надёжно сохранён в разделе «👤 Мой профиль».*"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Оставить отзыв", callback_data="leave_review")],
        [InlineKeyboardButton(text="👤 Посмотреть мои ключи", callback_data="profile")],
        [InlineKeyboardButton(text="🛒 Купить еще софт", callback_data=f"platform_{software.platform_id}" if software else "shop")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])
    await callback.message.edit_text(success_text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

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
            [InlineKeyboardButton(text="📤 Вывести средства (от 500 грн)", callback_data="start_withdrawal")],
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
    if balance < 500:
        await callback.answer(f"❌ Минимальная сумма вывода 500 грн. На вашем балансе: {balance:.2f} грн", show_alert=True)
        return

    await state.set_state(WithdrawalStates.waiting_for_amount)
    await callback.message.edit_text(
        f"📤 **Вывод средств**\n\n"
        f"💳 Ваш доступный баланс: `{balance:.2f} грн`\n\n"
        f"Введите сумму для вывода (минимум 500 грн):",
        reply_markup=cancel_kb("ref_program"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(WithdrawalStates.waiting_for_amount)
async def process_withdrawal_amount(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Отправьте сумму текстом (числом), например: 500", reply_markup=cancel_kb("ref_program"))
        return
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите корректную сумму числом!", reply_markup=cancel_kb("ref_program"))
        return

    user = await session.get(User, message.from_user.id)
    balance = user.balance if user else 0.0

    if amount < 500:
        await message.answer("❌ Минимальная сумма вывода — 500 грн. Введите сумму повторно:", reply_markup=cancel_kb("ref_program"))
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
    data = await state.get_data()
    amount = data.get("amount")
    if amount is None:
        await message.answer("⚠️ Сессия вывода устарела, начните заново через «Реферальная система».")
        await state.clear()
        return

    user_id = message.from_user.id
    user = await session.get(User, user_id)
    if not user or user.balance < amount:
        await message.answer("❌ Недостаточно средств на балансе.")
        await state.clear()
        return

    user.balance -= amount
    req = WithdrawalRequest(user_id=user_id, amount=amount, card=card)
    session.add(req)
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
    user = await session.get(User, req.user_id)
    if user:
        user.balance += req.amount

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
async def start_deposit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DepositStates.waiting_for_amount)
    await callback.message.edit_text(
        "💳 **Пополнение баланса**\n\nВведите сумму пополнения в гривнах числом:",
        reply_markup=cancel_kb("main_menu"),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Отправьте сумму текстом (числом), например: 300", reply_markup=cancel_kb("main_menu"))
        return
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную сумму числом!", reply_markup=cancel_kb("main_menu"))
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
    user = await session.get(User, req.user_id)
    if user:
        user.balance += req.amount
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
            [InlineKeyboardButton(text="➕ Создать тариф", callback_data="admin_add_prod"),
             InlineKeyboardButton(text="🗑 Удалить тариф", callback_data="admin_del_prod_select")],
            [InlineKeyboardButton(text="📥 Загрузить ключи", callback_data="admin_add_keys_select"),
             InlineKeyboardButton(text="🗑 Очистить ключи", callback_data="admin_del_keys_select")],
            [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin_create_promo"),
             InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_start_broadcast")],
            [InlineKeyboardButton(text="👤 Управление пользователем", callback_data="admin_user_manage")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ]
    )
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

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
async def admin_promo_code_entered(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Введите текст промокода.")
        return
    await state.update_data(code=message.text.strip())
    await state.set_state(AdminStates.waiting_for_promo_amount)
    await message.answer("🎟 **Создание промокода (2/3)**\n\nВведите бонусную сумму в гривнах (число):", reply_markup=cancel_kb("admin_panel"))

@router.message(AdminStates.waiting_for_promo_amount, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_amount_entered(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите корректное число для суммы.")
        return
    await state.update_data(amount=amount)
    await state.set_state(AdminStates.waiting_for_promo_uses)
    await message.answer("🎟 **Создание промокода (3/3)**\n\nВведите максимальное количество активаций (число):", reply_markup=cancel_kb("admin_panel"))

@router.message(AdminStates.waiting_for_promo_uses, F.from_user.id.in_(ADMIN_IDS))
async def admin_promo_uses_entered(message: Message, state: FSMContext, session: AsyncSession):
    try:
        uses = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число для активаций.")
        return
    data = await state.get_data()
    code = data.get("code")
    amount = data.get("amount")

    promo = PromoCode(code=code, amount=amount, uses_left=uses)
    session.add(promo)
    await session.commit()
    await state.clear()

    await message.answer(
        f"✅ Промокод `{escape_md(code)}` на `{amount:.2f} грн` (активаций: `{uses}`) успешно создан!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

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
        try:
            await message.send_copy(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            blocked += 1

    await message.answer(
        f"✅ **Рассылка завершена!**\n\n• Успешно доставлено: `{success}`\n• Заблокировали бота: `{blocked}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# --- УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЕМ ---
@router.callback_query(F.data == "admin_user_manage", F.from_user.id.in_(ADMIN_IDS))
async def admin_user_manage_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_user_id)
    await callback.message.edit_text(
        "👤 **Управление пользователем**\n\nВведите Telegram ID пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_user_id, F.from_user.id.in_(ADMIN_IDS))
async def admin_user_info_show(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите корректный ID числом.")
        return
    target_id = int(message.text.strip())
    user = await session.get(User, target_id)
    if not user:
        await message.answer("❌ Пользователь с таким ID не найден в базе данных.", reply_markup=cancel_kb("admin_panel"))
        return

    await state.update_data(target_user_id=target_id)
    purchases_cnt = await session.scalar(select(func.count(Purchase.id)).where(Purchase.user_id == target_id))

    info_text = (
        f"👤 **Информация о пользователе**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: `{user.id}`\n"
        f"👤 Username: @{escape_md(user.username) if user.username else 'отсутствует'}\n"
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
        [InlineKeyboardButton(text=ban_btn_text, callback_data=ban_action)],
        [InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]
    ])
    await state.clear()
    await message.answer(info_text, reply_markup=keyboard, parse_mode="Markdown")

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
        "💰 **Изменение баланса**\n\nВведите новое значение баланса или изменение (+100 / -50) числом:",
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
    try:
        val = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите корректное число.")
        return

    user = await session.get(User, uid)
    if user:
        user.balance = val
        await session.commit()
        await state.clear()
        await message.answer(f"✅ Баланс пользователя `{uid}` успешно изменен на `{val:.2f} грн`.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]))

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
async def confirm_delete_unsold_keys(callback: CallbackQuery):
    prod_id = callback.data.split("_")[3]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить непроданные", callback_data=f"do_del_unsold_{prod_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"clear_keys_prod_{prod_id}")]
    ])
    await callback.message.edit_text("⚠️ Вы действительно хотите удалить все **непроданные** ключи этого тарифа?", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("do_del_unsold_"), F.from_user.id.in_(ADMIN_IDS))
async def delete_unsold_keys(callback: CallbackQuery, session: AsyncSession):
    prod_id = int(callback.data.split("_")[3])
    res = await session.execute(select(LicenseKey).where(LicenseKey.product_id == prod_id, LicenseKey.is_sold == False))
    keys = res.scalars().all()
    count = len(keys)
    for k in keys:
        await session.delete(k)
    await session.commit()

    await callback.message.edit_text(
        f"✅ Успешно удалено непроданных ключей: `{count}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_del_all_keys_"), F.from_user.id.in_(ADMIN_IDS))
async def confirm_delete_all_keys(callback: CallbackQuery):
    prod_id = callback.data.split("_")[3]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 ДА, УДАЛИТЬ ВСЕ КЛЮЧИ", callback_data=f"do_del_all_keys_{prod_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"clear_keys_prod_{prod_id}")]
    ])
    await callback.message.edit_text("⚠️ **ВНИМАНИЕ!** Вы хотите удалить **ВСЕ** ключи (включая проданные). Это может нарушить историю выданных ключей покупателям. Продолжить?", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("do_del_all_keys_"), F.from_user.id.in_(ADMIN_IDS))
async def delete_all_keys(callback: CallbackQuery, session: AsyncSession):
    prod_id = int(callback.data.split("_")[3])
    res = await session.execute(select(LicenseKey).where(LicenseKey.product_id == prod_id))
    keys = res.scalars().all()
    count = len(keys)
    for k in keys:
        await session.delete(k)
    await session.commit()

    await callback.message.edit_text(
        f"✅ Успешно удалено ВСЕХ ключей: `{count}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "admin_add_keys_select", F.from_user.id.in_(ADMIN_IDS))
async def add_keys_select_product(callback: CallbackQuery, session: AsyncSession):
    products_res = await session.execute(select(Product))
    products = products_res.scalars().all()

    if not products:
        await callback.message.edit_text(
            "❌ Сначала создайте хотя бы один тариф товара!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]])
        )
        await callback.answer()
        return

    text_msg = "📥 **Загрузка ключей**\n\nВыберите тариф, для которого хотите загрузить ключи:"
    buttons = []
    for p in products:
        sw = await session.get(Software, p.software_id)
        sw_name = sw.name if sw else "Софт"
        buttons.append([InlineKeyboardButton(text=f"🔑 {sw_name} ({p.duration}) — {p.price:.2f} грн", callback_data=f"key_prod_sel_{p.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])
    await callback.message.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("key_prod_sel_"), F.from_user.id.in_(ADMIN_IDS))
async def start_key_upload(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    product_id = int(callback.data.split("_")[3])
    product = await session.get(Product, product_id)
    if not product:
        await callback.answer("❌ Тариф не найден!", show_alert=True)
        return

    software = await session.get(Software, product.software_id)
    sw_name = software.name if software else "Софт"

    await state.update_data(product_id=product_id)
    await state.set_state(AdminStates.waiting_for_keys)

    text_msg = (
        f"📥 **Загрузка ключей**\n"
        f"📦 Товар: `{escape_md(sw_name)} — {escape_md(product.duration)}`\n\n"
        f"Отправьте ключи **текстовым сообщением** (каждый с новой строки) "
        f"или прикрепите **.txt файл** с ключами:"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К выбору тарифа", callback_data="admin_add_keys_select")]])
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_keys, F.from_user.id.in_(ADMIN_IDS))
async def save_keys(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    product_id = data.get("product_id")

    keys_text = ""
    if message.document:
        if not (message.document.file_name and message.document.file_name.lower().endswith(".txt")):
            await message.answer("❌ Пожалуйста, отправьте файл в формате `.txt`.")
            return
        file = await bot.get_file(message.document.file_id)
        downloaded = await bot.download_file(file.file_path)
        keys_text = downloaded.getvalue().decode("utf-8")
    elif message.text:
        keys_text = message.text

    keys = [k.strip() for k in keys_text.strip().split("\n") if k.strip()]

    if not keys:
        await message.answer("❌ Сообщение или файл не содержат ключей!")
        return

    try:
        product = await session.get(Product, product_id)
        if not product:
            await message.answer("❌ Ошибка: Выбранный тариф не найден в БД.")
            await state.clear()
            return

        added = 0
        duplicates = 0
        for k in keys:
            exists = await session.execute(select(LicenseKey).filter_by(key_string=k))
            if not exists.scalars().first():
                session.add(LicenseKey(product_id=product_id, key_string=k, is_sold=False))
                added += 1
            else:
                duplicates += 1

        await session.commit()
        
        if added > 0:
            subs_res = await session.execute(select(RestockSubscription).where(RestockSubscription.product_id == product_id))
            subscriptions = subs_res.scalars().all()
            if subscriptions:
                software = await session.get(Software, product.software_id)
                sw_name = software.name if software else "Товар"
                for sub in subscriptions:
                    try:
                        await bot.send_message(
                            sub.user_id,
                            f"🔔 **Поступление товара!**\nДля товара `{escape_md(sw_name)}` (`{escape_md(product.duration)}`) появились новые ключи в наличии!",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                for sub in subscriptions:
                    await session.delete(sub)
                await session.commit()

        await state.clear()

        result_msg = f"✅ **Успешно загружено ключей: {added}**"
        if duplicates > 0:
            result_msg += f"\n⚠️ Пропущено дубликатов: `{duplicates}`"

        await message.answer(
            result_msg,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
            parse_mode="Markdown"
        )

    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка сохранения ключей: {e}")
        await message.answer("⚠️ Не удалось сохранить ключи. Ошибка БД.")

@router.callback_query(F.data == "admin_add_sw", F.from_user.id.in_(ADMIN_IDS))
async def add_sw_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_software_platform)
    text_msg = "➕ **Добавление софта (Шаг 1/2)**\n\nВыберите платформу:"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Android", callback_data="sw_platform_1")],
            [InlineKeyboardButton(text="🍏 iOS", callback_data="sw_platform_2")],
            [InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")],
        ]
    )
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
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
    try:
        data = await state.get_data()
        platform_id = data.get("platform_id")
        name = message.text.strip()

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
        logger.error(f"Ошибка при сохранении софта: {e}")
        await message.answer("⚠️ Не удалось сохранить софт. Ошибка БД.")

@router.callback_query(F.data == "admin_add_prod", F.from_user.id.in_(ADMIN_IDS))
async def add_prod_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.clear()
    res = await session.execute(select(Software))
    softwares = res.scalars().all()

    if not softwares:
        await callback.answer("❌ Сначала добавьте хотя бы один софт!", show_alert=True)
        return

    buttons = []
    for sw in softwares:
        buttons.append([InlineKeyboardButton(text=f"🛡 {sw.name} [ID: {sw.id}]", callback_data=f"prod_sw_sel_{sw.id}")])
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")])

    await callback.message.edit_text(
        "➕ **Создание тарифа (Шаг 1/3)**\n\nВыберите софт, для которого хотите создать тариф:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("prod_sw_sel_"), F.from_user.id.in_(ADMIN_IDS))
async def process_prod_software_selection(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    software_id = int(callback.data.split("_")[3])
    software = await session.get(Software, software_id)
    if not software:
        await callback.answer("❌ Софт не найден!", show_alert=True)
        return

    await state.update_data(software_id=software_id)
    await state.set_state(AdminStates.waiting_for_duration)

    text_msg = f"➕ **Создание тарифа (Шаг 2/3)**\n\nВыбран софт: `{escape_md(software.name)}`\n\nВыберите длительность:"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ 1 день", callback_data="duration_1 день")],
            [InlineKeyboardButton(text="⏳ 7 дней", callback_data="duration_7 дней")],
            [InlineKeyboardButton(text="⏳ 30 дней", callback_data="duration_30 дней")],
            [InlineKeyboardButton(text="🔙 К выбору софта", callback_data="admin_add_prod")],
        ]
    )
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("duration_"), F.from_user.id.in_(ADMIN_IDS))
async def process_duration(callback: CallbackQuery, state: FSMContext):
    duration = callback.data.split("_", 1)[1]
    await state.update_data(duration=duration)
    await state.set_state(AdminStates.waiting_for_price)

    text_msg = f"➕ **Создание тарифа (Шаг 3/3)**\n\nВыбран срок: `{duration}`\n\nОтправьте **цену в грн** (число):"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]])
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_price, F.from_user.id.in_(ADMIN_IDS))
async def process_price_and_save(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Отправьте цену текстом (числом).")
        return
    try:
        price = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверный формат цены. Введите число.")
        return

    data = await state.get_data()
    software_id = data.get("software_id")
    duration = data.get("duration")
    if software_id is None or duration is None:
        await message.answer("⚠️ Сессия создания тарифа устарела, начните заново.")
        await state.clear()
        return

    new_product = Product(software_id=software_id, duration=duration, price=price)
    session.add(new_product)
    await session.commit()
    await state.clear()

    await message.answer(
        f"✅ **Тариф успешно создан!**\n\n"
        f"• ID товара: `{new_product.id}`\n"
        f"• Срок: `{duration}`\n"
        f"• Цена: `{price:.2f} грн`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# ==========================================
# ФОЛЛБЕК ДЛЯ МЕДИА ВНЕ СОСТОЯНИЯ
# ==========================================
@router.message(F.photo | F.document)
async def photo_without_state(message: Message):
    await message.answer(
        "⚠️ Я получил ваш файл, но заявка на пополнение не активирована.\n\n"
        "Сначала перейдите в **«💳 Пополнить баланс»**, введите сумму и только после этого отправляйте чек!",
        reply_markup=cancel_kb("main_menu", "🏠 Главное меню"),
        parse_mode="Markdown"
    )

# ==========================================
# ЗАПУСК БОТА И ВЕБ-СЕРВЕРА
# ==========================================
async def handle_ping(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if "postgresql" in DATABASE_URL:
            try:
                await conn.execute(text("ALTER TABLE users ALTER COLUMN id TYPE BIGINT;"))
                await conn.execute(text("ALTER TABLE users ALTER COLUMN referred_by TYPE BIGINT;"))
                await conn.execute(text("ALTER TABLE purchases ALTER COLUMN user_id TYPE BIGINT;"))
                await conn.execute(text("ALTER TABLE deposit_requests ALTER COLUMN user_id TYPE BIGINT;"))
                await conn.execute(text("ALTER TABLE withdrawal_requests ALTER COLUMN user_id TYPE BIGINT;"))
            except Exception as e:
                logger.info(f"Миграция колонок пропущена/уже выполнена: {e}")

            try:
                await conn.execute(text("ALTER TABLE users ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';"))
                await conn.execute(text("ALTER TABLE purchases ALTER COLUMN purchased_at TYPE TIMESTAMPTZ USING purchased_at AT TIME ZONE 'UTC';"))
                await conn.execute(text("ALTER TABLE deposit_requests ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';"))
                await conn.execute(text("ALTER TABLE withdrawal_requests ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';"))
            except Exception as e:
                logger.info(f"Миграция timestamptz пропущена/уже выполнена: {e}")

        # Миграция для is_banned
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE;"))
            logger.info("Колонка users.is_banned добавлена/проверена.")
        except Exception as e:
            logger.info(f"Миграция is_banned пропущена: {e}")

        # Миграция для language (выбор языка)
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR(10) DEFAULT 'uk';"))
            logger.info("Колонка users.language добавлена/проверена.")
        except Exception as e:
            logger.info(f"Миграция language пропущена: {e}")

        try:
            await conn.execute(text("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS duration VARCHAR(50) DEFAULT ''"))
            logger.info("Колонка purchases.duration добавлена.")
        except Exception as e:
            logger.info(f"Колонка purchases.duration уже существует/миграция пропущена: {e}")

    async with async_session_maker() as session:
        result = await session.execute(select(Platform))
        platforms = result.scalars().all()
        if not platforms:
            session.add_all([
                Platform(id=1, name="Android"),
                Platform(id=2, name="iOS")
            ])
            await session.commit()
            logger.info("Базовые платформы Android и iOS успешно добавлены в БД.")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(DbSessionMiddleware(async_session_maker))
    dp.include_router(router)

    await start_web_server()

    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("Бот и веб-заглушка запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
