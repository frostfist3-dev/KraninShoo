import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from database.models import Base
from handlers import admin, shop
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"
else:
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(DATABASE_URL, echo=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)

from aiogram.dispatcher.middlewares.base import BaseMiddleware

class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_pool):
        super().__init__()
        self.session_pool = session_pool

    async def __call__(self, handler, event, data):
        async with self.session_pool() as session:
            data["session"] = session
            return await handler(event, data)

# Заглушка для Render Web Service (отвечает на веб-запросы)
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
    dp.include_router(admin.router)
    dp.include_router(shop.router)

    @dp.callback_query(lambda c: c.data == "main_menu")
    async def main_menu_handler(callback):
        await callback.message.edit_text(
            "👋 Главное меню Kranin Shop\n\nВыберите раздел:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="🛒 Открыть магазин", callback_data="shop")
                ]]
            ),
        )
        await callback.answer()

    # Запускаем и веб-сервер для Render, и поллинг бота одновременно
    await start_web_server()
    print("Бот и веб-заглушка успешно запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
