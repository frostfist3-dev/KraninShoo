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


async def db_session_middleware(handler, event, data):
  async with async_session() as session:
    data["session"] = session
    return await handler(event, data)


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
  # Держим веб-сервер активным
  await asyncio.Event().wait()


async def start_bot():
  if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

  bot = Bot(token=BOT_TOKEN)
  dp = Dispatcher(storage=MemoryStorage())

  dp.message.middleware(db_session_middleware)
  dp.callback_query.middleware(db_session_middleware)

  dp.include_router(shop.router)
  dp.include_router(admin.router)

  async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)

  # Сбрасываем старый вебхук и накопившиеся входящие апдейты
  await bot.delete_webhook(drop_pending_updates=True)
  print("Бот успешно запущен в режиме polling!")
  await dp.start_polling(bot)


async def main():
  # Запускаем параллельно веб-сервер и поллинг бота
  await asyncio.gather(start_web_server(), start_bot())


if __name__ == "__main__":
  asyncio.run(main())
