import asyncio
import os
import random
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import web
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ==========================================
# КОНФИГУРАЦИЯ И ПОДКЛЮЧЕНИЕ К БД
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"
    engine = create_async_engine(DATABASE_URL, echo=False)
else:
    # Приводим протокол к postgresql+asyncpg
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://")
    
    # Очищаем URL от параметров (?sslmode=..., ?channel_binding=...), которые ломают asyncpg
    if "?" in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.split("?")[0]
        
    # Включаем SSL безопасным способом через connect_args
    engine = create_async_engine(
        DATABASE_URL, 
        echo=False, 
        connect_args={"ssl": "require"}
    )

# Реквизиты для оплаты
CARDS = [
    "4441 1111 5555 7352 (Моно)",
    "4323 3473 8685 7285 (А-Банк)",
    "5232 4410 4407 1160 (Альянс)",
]

ADMIN_ID = 8479717148  # Замени на свой настоящий Telegram ID
ADMIN_IDS = [ADMIN_ID]

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
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    purchased_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class DepositRequest(Base):
    __tablename__ = "deposit_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

# ==========================================
# MIDDLEWARE
# ==========================================
class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_pool):
        super().__init__()
        self.session_pool = session_pool

    async def __call__(self, handler, event, data):
        async with self.session_pool() as session:
            data["session"] = session
            return await handler(event, data)

# ==========================================
# СОСТОЯНИЯ (FSM)
# ==========================================
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

# ==========================================
# ОСНОВНОЕ МЕНЮ И РЕГИСТРАЦИЯ
# ==========================================
def get_main_menu_kb(user_id: int):
    keyboard = [
        [InlineKeyboardButton(text="🛒 Открыть магазин", callback_data="shop")],
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="deposit")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession):
    user = await session.get(User, message.from_user.id)
    if not user:
        user = User(id=message.from_user.id, username=message.from_user.username)
        session.add(user)
        await session.commit()

    await message.answer(
        "👋 Главное меню Kranin Shop\n\nВыберите раздел:",
        reply_markup=get_main_menu_kb(message.from_user.id)
    )

@router.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 Главное меню Kranin Shop\n\nВыберите раздел:",
        reply_markup=get_main_menu_kb(callback.from_user.id)
    )
    await callback.answer()

# ==========================================
# КАТАЛОГ И ПОКУПКА
# ==========================================
@router.callback_query(F.data == "shop")
async def show_platforms(callback: CallbackQuery, session: AsyncSession):
    result = await session.execute(select(Platform))
    platforms = result.scalars().all()

    text = "🛒 **Kranin Shop | Выбор платформы**\n\n📱 Выберите устройство, для которого покупаете софт:"
    buttons = []
    for p in platforms:
        icon = "🤖" if "android" in p.name.lower() else "🍏"
        buttons.append([InlineKeyboardButton(text=f"{icon} {p.name}", callback_data=f"platform_{p.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("platform_"))
async def show_softwares(callback: CallbackQuery, session: AsyncSession):
    platform_id = int(callback.data.split("_")[1])
    platform = await session.get(Platform, platform_id)

    result = await session.execute(select(Software).filter_by(platform_id=platform_id))
    softwares = result.scalars().all()

    if not softwares:
        text = f"🛒 **Kranin Shop — {platform.name if platform else 'Платформа'}**\n\n❌ На данную платформу софт пока не добавлен."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к платформам", callback_data="shop")]])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    text = f"🛒 **Kranin Shop — {platform.name}**\n\n⚡️ Выберите нужный софт:"
    buttons = []
    for sw in softwares:
        buttons.append([InlineKeyboardButton(text=f"🛡 {sw.name}", callback_data=f"software_{sw.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад к платформам", callback_data="shop")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("software_"))
async def show_products(callback: CallbackQuery, session: AsyncSession):
    software_id = int(callback.data.split("_")[1])
    software = await session.get(Software, software_id)

    result = await session.execute(select(Product).filter_by(software_id=software_id))
    products = result.scalars().all()

    if not products:
        text = f"🛒 **{software.name}**\n\n❌ Тарифы для этого софта пока отсутствуют."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к софту", callback_data=f"platform_{software.platform_id}")]])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    text = f"🛒 **{software.name}**\n\n📌 Выберите тариф:"
    buttons = []
    for p in products:
        keys_res = await session.execute(select(LicenseKey).filter_by(product_id=p.id, is_sold=False))
        available_keys = len(keys_res.scalars().all())

        status = "🟢" if available_keys > 0 else "🔴"
        buttons.append([InlineKeyboardButton(text=f"{status} {p.duration} — {p.price} RUB", callback_data=f"buy_product_{p.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"platform_{software.platform_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("buy_product_"))
async def process_purchase(callback: CallbackQuery, session: AsyncSession):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    product = await session.get(Product, product_id)
    user = await session.get(User, user_id)
    software = await session.get(Software, product.software_id)

    if user.balance < product.price:
        await callback.answer("❌ Недостаточно средств на балансе! Пополните кошелек.", show_alert=True)
        return

    key_res = await session.execute(select(LicenseKey).filter_by(product_id=product.id, is_sold=False).limit(1))
    license_key = key_res.scalars().first()

    if not license_key:
        await callback.answer("❌ Ключи этого номинала временно закончились!", show_alert=True)
        return

    user.balance -= product.price
    license_key.is_sold = True

    purchase = Purchase(
        user_id=user.id,
        product_name=f"{software.name} ({product.duration})",
        key_issued=license_key.key_string,
    )
    session.add(purchase)
    await session.commit()

    success_text = (
        f"✅ **Покупка успешно завершена!**\n\n"
        f"📦 Товар: `{software.name} — {product.duration}`\n"
        f"🔑 Ваш лицензионный ключ:\n`{license_key.key_string}`\n\n"
        f"📌 *Скопируйте ключ кликом по нему.*"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")]])
    await callback.message.edit_text(success_text, reply_markup=keyboard, parse_mode="Markdown")

# ==========================================
# ПОПОЛНЕНИЕ БАЛАНСА
# ==========================================
@router.callback_query(F.data == "deposit")
async def start_deposit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DepositStates.waiting_for_amount)
    await callback.message.edit_text(
        "💳 **Пополнение баланса**\n\nВведите сумму пополнения в рублях (или грн):",
        parse_mode="Markdown"
    )

@router.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
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

    text = (
        f"💳 **Оплата по реквизитам**\n\n"
        f"Сумма к оплате: `{amount}`\n\n"
        f"Реквизиты для перевода:\n`{selected_card}`\n\n"
        f"📌 Переведите точную сумму на карту выше, а затем **отправьте сюда фото чека или скриншот перевода**."
    )
    await message.answer(text, parse_mode="Markdown")

@router.message(DepositStates.waiting_for_receipt, F.photo)
async def process_receipt(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    amount = data["amount"]
    user_id = message.from_user.id
    photo_id = message.photo[-1].file_id

    req = DepositRequest(user_id=user_id, amount=amount)
    session.add(req)
    await session.commit()

    await state.clear()
    await message.answer(
        "⏳ **Заявка отправлена!**\nАдминистратор проверит перевод и зачислит баланс.",
        parse_mode="Markdown"
    )

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_dep_{req.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_dep_{req.id}"),
        ]]
    )

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo_id,
        caption=(
            f"📥 **Новая заявка на пополнение #{req.id}**\n\n"
            f"👤 Пользователь: `{user_id}` (@{message.from_user.username})\n"
            f"💰 Сумма: `{amount} RUB`"
        ),
        reply_markup=admin_kb,
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

    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n✅ **ПОДТВЕРЖДЕНО**",
        reply_markup=None
    )

    try:
        await bot.send_message(req.user_id, f"🎉 **Баланс успешно пополнен на {req.amount} RUB!**\nТеперь вы можете купить ключ в магазине.", parse_mode="Markdown")
    except Exception:
        pass

@router.callback_query(F.data.startswith("reject_dep_"), F.from_user.id.in_(ADMIN_IDS))
async def reject_deposit(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    req_id = int(callback.data.split("_")[2])
    req = await session.get(DepositRequest, req_id)

    if not req or req.status != "pending":
        await callback.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    req.status = "rejected"
    await session.commit()

    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n❌ **ОТКЛОНЕНО**",
        reply_markup=None
    )

    try:
        await bot.send_message(req.user_id, "❌ Ваша заявка на пополнение была отклонена администратором.")
    except Exception:
        pass

# ==========================================
# ПАНЕЛЬ АДМИНИСТРАТОРА
# ==========================================
@router.callback_query(F.data == "admin_panel", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu(callback: CallbackQuery):
    text = "🛠 **Kranin Shop | Панель администратора**\n\nВыберите действие:"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить софт", callback_data="admin_add_sw")],
            [InlineKeyboardButton(text="➕ Создать тариф", callback_data="admin_add_prod")],
            [InlineKeyboardButton(text="📥 Загрузить ключи", callback_data="admin_add_keys")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "admin_add_sw", F.from_user.id.in_(ADMIN_IDS))
async def add_sw_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_software_platform)
    text = "➕ **Добавление софта (Шаг 1/2)**\n\nВыберите платформу, для которой создается софт:"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Android (ID: 1)", callback_data="sw_platform_1")],
            [InlineKeyboardButton(text="🍏 iOS (ID: 2)", callback_data="sw_platform_2")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("sw_platform_"), F.from_user.id.in_(ADMIN_IDS))
async def add_sw_platform_chosen(callback: CallbackQuery, state: FSMContext):
    platform_id = int(callback.data.split("_")[2])
    await state.update_data(platform_id=platform_id)
    await state.set_state(AdminStates.waiting_for_software_name)

    text = "➕ **Добавление софта (Шаг 2/2)**\n\nВведите название софта (например: `Flexnote`):"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_software_name, F.from_user.id.in_(ADMIN_IDS))
async def save_software(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    platform_id = data["platform_id"]
    name = message.text.strip()

    new_sw = Software(platform_id=platform_id, name=name)
    session.add(new_sw)
    await session.commit()
    await state.clear()

    await message.answer(f"✅ Софт `{name}` успешно добавлен!\n🔑 ID нового софта: `{new_sw.id}`", parse_mode="Markdown")

@router.callback_query(F.data == "admin_add_prod", F.from_user.id.in_(ADMIN_IDS))
async def add_prod_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_sw_id)
    text = "➕ **Создание тарифа (Шаг 1/3)**\n\nВведите **ID софта**, к которому добавляем тариф:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.message(AdminStates.waiting_for_sw_id, F.from_user.id.in_(ADMIN_IDS))
async def process_sw_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID должен быть числом. Попробуйте снова.")
        return

    await state.update_data(software_id=int(message.text))
    await state.set_state(AdminStates.waiting_for_duration)

    text = "➕ **Создание тарифа (Шаг 2/3)**\n\nВыберите длительность ключа:"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ 1 день", callback_data="duration_1 день")],
            [InlineKeyboardButton(text="⏳ 7 дней", callback_data="duration_7 дней")],
            [InlineKeyboardButton(text="⏳ 30 дней", callback_data="duration_30 дней")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
        ]
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("duration_"), F.from_user.id.in_(ADMIN_IDS))
async def process_duration(callback: CallbackQuery, state: FSMContext):
    duration = callback.data.split("_", 1)[1]
    await state.update_data(duration=duration)
    await state.set_state(AdminStates.waiting_for_price)

    text = f"➕ **Создание тарифа (Шаг 3/3)**\n\nВыбран срок: `{duration}`\n\nТеперь отправьте **цену** (например: `450` или `1500.0`):"
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_price, F.from_user.id.in_(ADMIN_IDS))
async def process_price_and_save(message: Message, state: FSMContext, session: AsyncSession):
    try:
        price = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверный формат цены. Введите число (например: 500)")
        return

    data = await state.get_data()
    software_id = data["software_id"]
    duration = data["duration"]

    new_product = Product(software_id=software_id, duration=duration, price=price)
    session.add(new_product)
    await session.commit()
    await state.clear()

    await message.answer(
        f"✅ **Тариф успешно создан!**\n\n"
        f"• ID товара для загрузки ключей: `{new_product.id}`\n"
        f"• Срок: `{duration}`\n"
        f"• Цена: `{price} RUB`\n\n"
        f"Загрузите ключи через раздел «📥 Загрузить ключи», используя ID `{new_product.id}`.",
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "admin_add_keys", F.from_user.id.in_(ADMIN_IDS))
async def add_keys_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_keys)
    text = (
        "📥 **Загрузка ключей**\n\nОтправьте сообщение, где:\n1️⃣ Первая строка — **ID товара (тарифа)**\n"
        "2️⃣ Со второй строки — сами ключи (каждый с новой строки).\n\nПример:\n`3`\n`KEY-1`\n`KEY-2`"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.message(AdminStates.waiting_for_keys, F.from_user.id.in_(ADMIN_IDS))
async def save_keys(message: Message, state: FSMContext, session: AsyncSession):
    lines = message.text.strip().split("\n")
    if len(lines) < 2:
        await message.answer("❌ Слишком мало строк.")
        return

    try:
        product_id = int(lines[0].strip())
        keys = [k.strip() for k in lines[1:] if k.strip()]
    except ValueError:
        await message.answer("❌ Первая строка должна быть ID товара.")
        return

    added = 0
    for k in keys:
        exists = await session.execute(select(LicenseKey).filter_by(key_string=k))
        if not exists.scalars().first():
            session.add(LicenseKey(product_id=product_id, key_string=k, is_sold=False))
            added += 1

    await session.commit()
    await state.clear()

    await message.answer(f"✅ Загружено новых ключей: `{added}` для товара ID `{product_id}`", parse_mode="Markdown")

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

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(DbSessionMiddleware(async_session_maker))
    dp.include_router(router)

    await start_web_server()

    # Автоматическое удаление старого Webhook перед началом Long Polling
    await bot.delete_webhook(drop_pending_updates=True)

    print("Бот и веб-заглушка успешно запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
