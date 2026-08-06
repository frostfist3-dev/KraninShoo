import asyncio
import os
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import BigInteger, ForeignKey, String, Text, select
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ==========================================
# 1. КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

if not DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"
else:
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
    if "sslmode=" in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.split("?")[0] + "?ssl=require"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ==========================================
# 2. МОДЕЛИ БАЗЫ ДАННЫХ (PostgreSQL / SQLite)
# ==========================================
class Base(AsyncAttrs, DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    balance: Mapped[float] = mapped_column(default=0.0)

class Platform(Base):
    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    softwares: Mapped[list["Software"]] = relationship(back_populates="platform", cascade="all, delete-orphan")

class Software(Base):
    __tablename__ = "softwares"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"))
    platform: Mapped["Platform"] = relationship(back_populates="softwares")
    products: Mapped[list["Product"]] = relationship(back_populates="software", cascade="all, delete-orphan")

class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    price: Mapped[float] = mapped_column()
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    software_id: Mapped[int] = mapped_column(ForeignKey("softwares.id"))
    software: Mapped["Software"] = relationship(back_populates="products")
    keys: Mapped[list["LicenseKey"]] = relationship(back_populates="product", cascade="all, delete-orphan")

class LicenseKey(Base):
    __tablename__ = "license_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    key_data: Mapped[str] = mapped_column(Text)
    is_sold: Mapped[bool] = mapped_column(default=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product: Mapped["Product"] = relationship(back_populates="keys")

class DepositRequest(Base):
    __tablename__ = "deposit_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[float] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), default="pending")

# ==========================================
# 3. STATES (FSM)
# ==========================================
class AdminAddProduct(StatesGroup):
    name = State()
    price = State()
    software_id = State()

class AdminAddKey(StatesGroup):
    product_id = State()
    keys = State()

# ==========================================
# 4. КЛАВИАТУРЫ
# ==========================================
def main_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Каталог", callback_query_data="catalog")],
            [InlineKeyboardButton(text="👤 Профиль", callback_query_data="profile")],
        ]
    )

def admin_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить товар", callback_query_data="admin_add_product")],
            [InlineKeyboardButton(text="🔑 Загрузить ключи", callback_query_data="admin_add_keys")],
        ]
    )

# ==========================================
# 5. MIDDLEWARE
# ==========================================
async def db_session_middleware(handler, event, data):
    async with async_session() as session:
        data["session"] = session
        return await handler(event, data)

# ==========================================
# 6. РОУТЕР SHOP (Пользовательский)
# ==========================================
shop_router = Router()

@shop_router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession):
    stmt = select(User).where(User.telegram_id == message.from_user.id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        user = User(telegram_id=message.from_user.id, username=message.from_user.username)
        session.add(user)
        await session.commit()

    await message.answer("Добро пожаловать в магазин!", reply_markup=main_menu_kb())

@shop_router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery, session: AsyncSession):
    stmt = select(User).where(User.telegram_id == callback.from_user.id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    
    balance = user.balance if user else 0.0
    text = f"👤 Ваш профиль\nID: `{callback.from_user.id}`\nБаланс: {balance} грн."
    await callback.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

@shop_router.callback_query(F.data == "catalog")
async def show_catalog(callback: CallbackQuery, session: AsyncSession):
    stmt = select(Product)
    result = await session.execute(stmt)
    products = result.scalars().all()

    if not products:
        await callback.message.edit_text("Каталог пока пуст.", reply_markup=main_menu_kb())
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{p.name} - {p.price}грн", callback_query_data=f"buy_{p.id}")] for p in products
        ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_query_data="main_menu")]]
    )
    await callback.message.edit_text("Выберите товар:", reply_markup=kb)

@shop_router.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_kb())

# ==========================================
# 7. РОУТЕР ADMIN (Административный)
# ==========================================
admin_router = Router()

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        return
    await message.answer("Панель администратора:", reply_markup=admin_menu_kb())

# ==========================================
# 8. СТАРТ И ВЕБХУКИ
# ==========================================
async def on_startup(bot: Bot):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        print(f"Webhook успешно зарегистрирован: {webhook_url}")

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не установлен в переменных окружения!")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрация Middlewares
    dp.message.middleware(db_session_middleware)
    dp.callback_query.middleware(db_session_middleware)

    # Регистрация роутеров
    dp.include_router(shop_router)
    dp.include_router(admin_router)

    dp.startup.register(on_startup)

    app = web.Application()

    # Регистрация обработчика вебхука для Render
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
