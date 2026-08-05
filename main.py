import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Импорт роутеров и базы данных
import admin
import shop
from models import Base

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Обработка URL базы данных для asyncpg
if not DATABASE_URL:
  DATABASE_URL = "sqlite+aiosqlite:///bot.db"
else:
  # Заменяем префикс схемы на postgresql+asyncpg
  DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")
  DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

  # Заменяем sslmode на ssl, так как asyncpg не поддерживает sslmode
  if "sslmode=" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.split("?")[0] + "?ssl=require"

# Создание движка и фабрики сессий
engine = create_async_engine(DATABASE_URL, echo=True)
async_session = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


# Middleware для передачи сессии БД в хэндлеры
async def db_session_middleware(handler, event, data):
  async with async_session() as session:
    data["session"] = session
    return await handler(event, data)


async def main():
  if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения!")

  bot = Bot(token=BOT_TOKEN)
  dp = Dispatcher(storage=MemoryStorage())

  # Подключение middleware
  dp.message.middleware(db_session_middleware)
  dp.callback_query.middleware(db_session_middleware)

  # Регистрация роутеров
  dp.include_router(shop.router)
  dp.include_router(admin.router)

  # Инициализация таблиц базы данных
  async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)

  print("Бот успешно запущен!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
