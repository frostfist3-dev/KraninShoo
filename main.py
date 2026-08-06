import asyncio
import logging
import os
import random
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import CommandObject, CommandStart
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

async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
router = Router()

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
            return await handler(event, data)

class AdminStates(StatesGroup):
    waiting_for_software_platform = State()
    waiting_for_software_name = State()
    waiting_for_sw_id = State()
    waiting_for_duration = State()
    waiting_for_price = State()
    waiting_for_keys = State()

class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_receipt = State()

class WithdrawalStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_card = State()

# ==========================================
# ГЛАВНОЕ МЕНЮ И СТАРТ
# ==========================================
def get_main_menu_kb(user_id: int):
    keyboard = [
        [InlineKeyboardButton(text="🛒 Открыть магазин", callback_data="shop")],
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="deposit")],
        [InlineKeyboardButton(text="🤝 Реферальная система", callback_data="ref_program")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, session: AsyncSession):
    try:
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
                referred_by=referrer_id
            )
            session.add(user)
            await session.commit()
        else:
            if user.username != message.from_user.username:
                user.username = message.from_user.username
                await session.commit()

        await message.answer(
            f"👋 Главное меню Kranin Shop\n\n💰 Ваш баланс: `{user.balance:.2f} грн`\n\nВыберите раздел:",
            reply_markup=get_main_menu_kb(message.from_user.id),
            parse_mode="Markdown"
        )
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка в cmd_start: {e}")
        await message.answer("⚠️ Произошла ошибка. Попробуйте снова.")

@router.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery, session: AsyncSession):
    user = await session.get(User, callback.from_user.id)
    balance = user.balance if user else 0.0
    await callback.message.edit_text(
        f"👋 Главное меню Kranin Shop\n\n💰 Ваш баланс: `{balance:.2f} грн`\n\nВыберите раздел:",
        reply_markup=get_main_menu_kb(callback.from_user.id),
        parse_mode="Markdown"
    )
    await callback.answer()

# ==========================================
# КАТАЛОГ И ПОКУПКИ
# ==========================================
@router.callback_query(F.data == "shop")
async def show_platforms(callback: CallbackQuery, session: AsyncSession):
    result = await session.execute(select(Platform))
    platforms = result.scalars().all()

    text_msg = "🛒 **Kranin Shop | Выбор платформы**\n\n📱 Выберите устройство, для которого покупаете софт:"
    buttons = []
    for p in platforms:
        icon = "🤖" if "android" in p.name.lower() else "🍏"
        buttons.append([InlineKeyboardButton(text=f"{icon} {p.name}", callback_data=f"platform_{p.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")])
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
        text_msg = f"🛒 **Kranin Shop — {platform.name if platform else 'Платформа'}**\n\n❌ На данную платформу софт пока не добавлен."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к платформам", callback_data="shop")]])
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()
        return

    text_msg = f"🛒 **Kranin Shop — {platform.name}**\n\n⚡️ Выберите нужный софт:"
    buttons = []
    for sw in softwares:
        buttons.append([InlineKeyboardButton(text=f"🛡 {sw.name}", callback_data=f"software_{sw.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад к платформам", callback_data="shop")])
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
        text_msg = f"🛒 **{software.name}**\n\n❌ Тарифы для этого софта пока отсутствуют."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к софту", callback_data=f"platform_{software.platform_id}")]])
        await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()
        return

    text_msg = f"🛒 **{software.name}**\n\n📌 Выберите тариф:"
    buttons = []
    for p in products:
        keys_res = await session.execute(select(LicenseKey).filter_by(product_id=p.id, is_sold=False))
        available_keys = len(keys_res.scalars().all())

        status = "🟢" if available_keys > 0 else "🔴"
        buttons.append([InlineKeyboardButton(text=f"{status} {p.duration} — {p.price:.2f} грн", callback_data=f"buy_product_{p.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"platform_{software.platform_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

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

    # FOR UPDATE предотвращает продажу одного и того же ключа двум покупателям
    # при одновременных запросах (на SQLite игнорируется, на Postgres блокирует строку)
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
    )
    session.add(purchase)
    await session.commit()

    success_text = (
        f"✅ **Покупка успешно завершена!**\n\n"
        f"📦 Товар: `{sw_name} — {product.duration}`\n"
        f"💳 Списано: `{product.price:.2f} грн`\n"
        f"🔑 Ваш ключ:\n`{license_key.key_string}`\n\n"
        f"📌 Нажмите на ключ, чтобы скопировать."
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")]])
    await callback.message.edit_text(success_text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

# ==========================================
# РЕФЕРАЛЬНАЯ СИСТЕМА И ВЫВОД СРЕДСТВ
# ==========================================
@router.callback_query(F.data == "ref_program")
async def show_ref_program(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    user_id = callback.from_user.id
    user = await session.get(User, user_id)
    balance = user.balance if user else 0.0

    ref_count_res = await session.execute(select(func.count(User.id)).where(User.referred_by == user_id))
    total_refs = ref_count_res.scalar() or 0

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"

    text_msg = (
        f"🤝 **Реферальная программа**\n\n"
        f"Приглашайте друзей и получайте **5%** от каждой их покупки!\n"
        f"Эти деньги поступают на ваш общий баланс. Вы можете использовать их для покупки софта или **вывести на карту** (от 500 грн).\n\n"
        f"👥 Приглашено рефералов: `{total_refs}`\n"
        f"💰 Ваш баланс: `{balance:.2f} грн`\n\n"
        f"🔗 Ваша реферальная ссылка:\n`{ref_link}`"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Вывести средства (от 500 грн)", callback_data="start_withdrawal")],
            [InlineKeyboardButton(text="🛒 Купить софт за баланс", callback_data="shop")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")]
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
        f"📤 **Вывод средств**\n\nВаш доступный баланс: `{balance:.2f} грн`\n"
        f"Введите сумму вывода (минимум 500 грн):",
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(WithdrawalStates.waiting_for_amount)
async def process_withdrawal_amount(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer("❌ Отправьте сумму текстом (числом), например: 500")
        return
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите корректную сумму числом!")
        return

    user = await session.get(User, message.from_user.id)
    balance = user.balance if user else 0.0

    if amount < 500:
        await message.answer("❌ Минимальная сумма вывода — 500 грн. Введите сумму повторно:")
        return
    if amount > balance:
        await message.answer(f"❌ У вас недостаточно средств! Доступно: `{balance:.2f} грн`. Введите сумму:", parse_mode="Markdown")
        return

    await state.update_data(amount=amount)
    await state.set_state(WithdrawalStates.waiting_for_card)
    await message.answer("💳 Введите номер банковской карты для получения выплаты:")

@router.message(WithdrawalStates.waiting_for_card)
async def process_withdrawal_card(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not message.text:
        await message.answer("❌ Отправьте номер карты текстом.")
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

    await message.answer(
        f"✅ **Заявка на вывод создана!**\n\n"
        f"💰 Сумма: `{amount:.2f} грн`\n"
        f"💳 Карта: `{card}`\n\n"
        f"⏳ Ожидайте обработки администратором.",
        parse_mode="Markdown"
    )

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить выплату", callback_data=f"approve_wdr_{req.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_wdr_{req.id}")
        ]]
    )

    delivered = False
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📤 **Заявка на вывод средств #{req.id}**\n\n"
                    f"👤 Пользователь: `{user_id}` (@{message.from_user.username or 'без_юзернейма'})\n"
                    f"💰 Сумма: `{amount:.2f} грн`\n"
                    f"💳 Карта: `{card}`"
                ),
                reply_markup=admin_kb,
                parse_mode="Markdown"
            )
            delivered = True
        except Exception as e:
            logger.exception(f"Не удалось отправить уведомление о выводе админу {admin_id}: {e}")

    if not delivered:
        logger.error(
            f"Заявка на вывод #{req.id} создана, но НИ ОДНОМУ админу не доставлено уведомление. "
            f"Проверь ADMIN_IDS={ADMIN_IDS} и что админ(ы) нажали /start боту."
        )
        await message.answer(
            "⚠️ Заявка сохранена, но не удалось уведомить администратора автоматически. "
            f"Свяжитесь с поддержкой и укажите номер заявки: `#{req.id}`",
            parse_mode="Markdown"
        )

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
    await callback.message.edit_text(text=current_text + "\n\n✅ **ВЫПЛАЧЕНО**", reply_markup=None)
    try:
        await bot.send_message(
            req.user_id,
            f"🎉 **Ваша заявка на вывод `{req.amount:.2f} грн` успешно обработана!**\nСредства отправлены на карту `{req.card}`.",
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
    await callback.message.edit_text(text=current_text + "\n\n❌ **ОТКЛОНЕНО (Средства возвращены)**", reply_markup=None)
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
# ПОПОЛНЕНИЕ БАЛАНСА И ПРИЕМ ЧЕКОВ (ФОТО / ФАЙЛЫ)
# ==========================================
@router.callback_query(F.data == "deposit")
async def start_deposit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DepositStates.waiting_for_amount)
    await callback.message.edit_text(
        "💳 **Пополнение баланса**\n\nВведите сумму пополнения в гривнах числом:",
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Отправьте сумму текстом (числом), например: 300")
        return
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную сумму числом!")
        return

    selected_card = random.choice(CARDS)
    await state.update_data(amount=amount, card=selected_card)
    await state.set_state(DepositStates.waiting_for_receipt)

    text_msg = (
        f"💳 **Оплата по реквизитам**\n\n"
        f"Сумма: `{amount:.2f} грн`\n\n"
        f"Реквизиты:\n`{selected_card}`\n\n"
        f"📌 Переведите указанную сумму и **отправьте сюда фото или файл чека**."
    )
    await message.answer(text_msg, parse_mode="Markdown")

@router.message(DepositStates.waiting_for_receipt, F.photo | F.document)
async def process_receipt(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    amount = data.get("amount", 0.0)
    user_id = message.from_user.id

    req = DepositRequest(user_id=user_id, amount=amount)
    session.add(req)
    await session.commit()

    await state.clear()
    await message.answer("⏳ **Заявка отправлена!** Ожидайте подтверждения администратора.", parse_mode="Markdown")

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_dep_{req.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_dep_{req.id}"),
        ]]
    )

    caption_text = (
        f"📥 **Заявка на пополнение #{req.id}**\n\n"
        f"👤 Пользователь: `{user_id}` (@{message.from_user.username or 'без_юзернейма'})\n"
        f"💰 Сумма: `{amount:.2f} грн`"
    )

    delivered = False
    for admin_id in ADMIN_IDS:
        try:
            if message.photo:
                photo_id = message.photo[-1].file_id
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=photo_id,
                    caption=caption_text,
                    reply_markup=admin_kb,
                    parse_mode="Markdown"
                )
            elif message.document:
                doc_id = message.document.file_id
                await bot.send_document(
                    chat_id=admin_id,
                    document=doc_id,
                    caption=caption_text,
                    reply_markup=admin_kb,
                    parse_mode="Markdown"
                )
            delivered = True
        except Exception as e:
            logger.exception(f"Не удалось отправить чек администратору {admin_id}: {e}")

    if not delivered:
        logger.error(
            f"Заявка на пополнение #{req.id} создана, но НИ ОДНОМУ админу не доставлено уведомление. "
            f"Проверь ADMIN_IDS={ADMIN_IDS} и что админ(ы) нажали /start боту."
        )
        await message.answer(
            "⚠️ Заявка сохранена, но не удалось уведомить администратора автоматически. "
            "Свяжитесь с поддержкой и укажите этот номер заявки: "
            f"`#{req.id}`",
            parse_mode="Markdown"
        )

# Фолбэк: пользователь в состоянии ожидания чека, но прислал не фото/файл (например текст)
@router.message(DepositStates.waiting_for_receipt)
async def process_receipt_wrong_type(message: Message):
    await message.answer("📌 Пожалуйста, отправьте **фото или файл** чека об оплате.", parse_mode="Markdown")

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
        new_caption = (callback.message.caption or "") + "\n\n✅ **ПОДТВЕРЖДЕНО**"
        await callback.message.edit_caption(caption=new_caption, reply_markup=None)
    else:
        new_text = (callback.message.text or "") + "\n\n✅ **ПОДТВЕРЖДЕНО**"
        await callback.message.edit_text(text=new_text, reply_markup=None)

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
        new_caption = (callback.message.caption or "") + "\n\n❌ **ОТКЛОНЕНО**"
        await callback.message.edit_caption(caption=new_caption, reply_markup=None)
    else:
        new_text = (callback.message.text or "") + "\n\n❌ **ОТКЛОНЕНО**"
        await callback.message.edit_text(text=new_text, reply_markup=None)

    try:
        await bot.send_message(req.user_id, "❌ Ваша заявка на пополнение была отклонена.")
    except Exception:
        pass
    await callback.answer()

# ==========================================
# ПАНЕЛЬ АДМИНИСТРАТОРА
# ==========================================
@router.callback_query(F.data == "admin_panel", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu(callback: CallbackQuery):
    text_msg = "🛠 **Панель администратора**\n\nВыберите действие:"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить софт", callback_data="admin_add_sw")],
            [InlineKeyboardButton(text="➕ Создать тариф", callback_data="admin_add_prod")],
            [InlineKeyboardButton(text="📥 Загрузить ключи", callback_data="admin_add_keys_select")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")],
        ]
    )
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
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

    text_msg = "📥 **Загрузка ключей | Выберите тариф**\n\nВыберите товар, для которого хотите загрузить ключи:"
    buttons = []
    for p in products:
        sw = await session.get(Software, p.software_id)
        sw_name = sw.name if sw else "Софт"
        buttons.append([InlineKeyboardButton(text=f"🔑 {sw_name} ({p.duration}) — {p.price:.2f} грн", callback_data=f"key_prod_sel_{p.id}")])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")])
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
        f"📥 **Загрузка ключей для:** `{sw_name} — {product.duration}`\n\n"
        f"Отправьте ключи **текстовым сообщением** (каждый с новой строки) "
        f"или прикрепите **.txt файл** с ключами:"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]])
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
        await state.clear()

        result_msg = f"✅ **Успешно загружено ключей: {added}**"
        if duplicates > 0:
            result_msg += f"\n⚠️ Пропущено дубликатов: `{duplicates}`"

        await message.answer(result_msg, parse_mode="Markdown")

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
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]])
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
            f"✅ Софт `{name}` успешно добавлен!\n🔑 ID софта: `{new_sw.id}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка при сохранении софта: {e}")
        await message.answer("⚠️ Не удалось сохранить софт. Ошибка БД.")

@router.callback_query(F.data == "admin_add_prod", F.from_user.id.in_(ADMIN_IDS))
async def add_prod_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_sw_id)
    text_msg = "➕ **Создание тарифа (Шаг 1/3)**\n\nВведите **ID софта**:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]])
    await callback.message.edit_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_sw_id, F.from_user.id.in_(ADMIN_IDS))
async def process_sw_id(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID должен быть числом. Попробуйте снова.")
        return

    await state.update_data(software_id=int(message.text))
    await state.set_state(AdminStates.waiting_for_duration)

    text_msg = "➕ **Создание тарифа (Шаг 2/3)**\n\nВыберите длительность:"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ 1 день", callback_data="duration_1 день")],
            [InlineKeyboardButton(text="⏳ 7 дней", callback_data="duration_7 дней")],
            [InlineKeyboardButton(text="⏳ 30 дней", callback_data="duration_30 дней")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
        ]
    )
    await message.answer(text_msg, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("duration_"), F.from_user.id.in_(ADMIN_IDS))
async def process_duration(callback: CallbackQuery, state: FSMContext):
    duration = callback.data.split("_", 1)[1]
    await state.update_data(duration=duration)
    await state.set_state(AdminStates.waiting_for_price)

    text_msg = f"➕ **Создание тарифа (Шаг 3/3)**\n\nВыбран срок: `{duration}`\n\nОтправьте **цену в грн** (число):"
    await callback.message.edit_text(text_msg, parse_mode="Markdown")
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
                # Переводим все datetime-колонки на TIMESTAMP WITH TIME ZONE,
                # т.к. в коде используются offset-aware значения (datetime.now(timezone.utc)),
                # а asyncpg не умеет писать их в TIMESTAMP WITHOUT TIME ZONE.
                await conn.execute(text("ALTER TABLE users ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';"))
                await conn.execute(text("ALTER TABLE purchases ALTER COLUMN purchased_at TYPE TIMESTAMPTZ USING purchased_at AT TIME ZONE 'UTC';"))
                await conn.execute(text("ALTER TABLE deposit_requests ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';"))
                await conn.execute(text("ALTER TABLE withdrawal_requests ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';"))
            except Exception as e:
                logger.info(f"Миграция timestamptz пропущена/уже выполнена: {e}")

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
