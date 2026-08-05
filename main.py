import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import admin
from models import Base
import shop

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Настройка подключения к БД
if not DATABASE_URL:
  DATABASE_URL = "sqlite+aiosqlite:///bot.db"
else:
  DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")
  DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

  if "sslmode=" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.split("?")[0] + "?ssl=require"

engine = create_async_engine(DATABASE_URL, echo=True)
async_session = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


# Middleware БД
async def db_session_middleware(handler, event, data):
  async with async_session() as session:
    data["session"] = session
    return await handler(event, data)


# Веб-сервер для прохождения Health Check на Render
async def handle_ping(request):
  return web.Response(text="Bot is running!")


async def start_web_server():
  app = web.Application()
  app.router.add_get("/", handle_ping)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 8080))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()


async def main():
  if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения!")

  bot = Bot(token=BOT_TOKEN)
  dp = Dispatcher(storage=MemoryStorage())

  dp.message.middleware(db_session_middleware)
  dp.callback_query.middleware(db_session_middleware)

  dp.include_router(shop.router)
  dp.include_router(admin.router)

  # Инициализация таблиц
  async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)

  # Запуск веб-сервера для Render
  await start_web_server()

  # Сброс вебхука и запуск поллинга
  await bot.delete_webhook(drop_pending_updates=True)
  print("Бот и веб-сервер успешно запущены!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
