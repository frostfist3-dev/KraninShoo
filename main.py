import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Импортируем твои роутеры и базовый класс БД
from database.models import Base
from handlers import admin, shop  # предполагается, что файлы лежат в папке handlers или рядом

# Считываем токен бота и адрес базы из переменных окружения Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
  # Если запускаешь локально на ПК для тестов
  DATABASE_URL = "sqlite+aiosqlite:///bot.db"
else:
  # Исправляем префикс для работы SQLAlchemy с PostgreSQL на Render/Neon
  DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")
  DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

# Настройка движка базы данных и фабрики сессий
engine = create_async_engine(DATABASE_URL, echo=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


# Миддлварь, чтобы сессия БД прокидывалась в хендлеры (в admin.py и shop.py)
from aiogram.dispatcher.middlewares.base import BaseMiddleware


class DbSessionMiddleware(BaseMiddleware):

  def __init__(self, session_pool):
    super().__init__()
    self.session_pool = session_pool

  async def __call__(self, handler, event, data):
    async with self.session_pool() as session:
      data["session"] = session
      return await handler(event, data)


async def main():
  # Создаем таблицы в БД при запуске (если их еще нет)
  async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)

  bot = Bot(token=BOT_TOKEN)
  dp = Dispatcher(storage=MemoryStorage())

  # Регистрируем миддлварь для передачи сессии
  dp.update.middleware(DbSessionMiddleware(async_session_maker))

  # Подключаем роутеры из твоих файлов
  dp.include_router(admin.router)
  dp.include_router(shop.router)

  # Обработка кнопки «главное меню», чтобы бот не падал
  @dp.callback_query(lambda c: c.data == "main_menu")
  async def main_menu_handler(callback):
    await callback.message.edit_text(
        "👋 Главное меню Kranin Shop\n\nВыберите раздел:",
        reply_markup=aiogram.types.InlineKeyboardMarkup(
            inline_keyboard=[[
                aiogram.types.InlineKeyboardButton(
                    text="🛒 Открыть магазин", callback_data="shop"
                )
            ]]
        ),
    )
    await callback.answer()

  print("Бот успешно запущен!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
