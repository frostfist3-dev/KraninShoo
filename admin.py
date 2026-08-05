from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models import DepositRequest, LicenseKey, Product, Software, User
router = Router()

ADMIN_IDS = [8479717148]  # Твой Telegram ID


class AdminStates(StatesGroup):
  waiting_for_software_platform = State()
  waiting_for_software_name = State()
  waiting_for_sw_id = State()
  waiting_for_duration = State()
  waiting_for_price = State()
  waiting_for_keys = State()


# --- ОБРАБОТКА ПОПОЛНЕНИЙ ---
@router.callback_query(
    F.data.startswith("approve_dep_"), F.from_user.id.in_(ADMIN_IDS)
)
async def approve_deposit(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
):
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
      reply_markup=None,
  )

  try:
    await bot.send_message(
        req.user_id,
        f"🎉 **Баланс успешно пополнен на {req.amount} RUB!**\nТеперь вы можете"
        " купить ключ в магазине.",
        parse_mode="Markdown",
    )
  except Exception:
    pass


@router.callback_query(
    F.data.startswith("reject_dep_"), F.from_user.id.in_(ADMIN_IDS)
)
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
      reply_markup=None,
  )

  try:
    await bot.send_message(
        req.user_id,
        "❌ Ваша заявка на пополнение была отклонена администратором.",
    )
  except Exception:
    pass


# --- МЕНЮ АДМИНИСТРАТОРА ---
@router.callback_query(F.data == "admin_panel", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu(callback: CallbackQuery):
  text = "🛠 **Kranin Shop | Панель администратора**\n\nВыберите действие:"
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="➕ Добавить софт", callback_data="admin_add_sw"
              )
          ],
          [
              InlineKeyboardButton(
                  text="➕ Создать тариф", callback_data="admin_add_prod"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📥 Загрузить ключи", callback_data="admin_add_keys"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔙 В главное меню", callback_data="main_menu"
              )
          ],
      ]
  )
  await callback.message.edit_text(
      text, reply_markup=keyboard, parse_mode="Markdown"
  )


@router.callback_query(F.data == "admin_add_sw", F.from_user.id.in_(ADMIN_IDS))
async def add_sw_start(callback: CallbackQuery, state: FSMContext):
  await state.set_state(AdminStates.waiting_for_software_platform)
  text = (
      "➕ **Добавление софта (Шаг 1/2)**\n\nВыберите платформу, для которой"
      " создается софт:"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🤖 Android (ID: 1)", callback_data="sw_platform_1"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🍏 iOS (ID: 2)", callback_data="sw_platform_2"
              )
          ],
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
      ]
  )
  await callback.message.edit_text(
      text, reply_markup=keyboard, parse_mode="Markdown"
  )


@router.callback_query(
    F.data.startswith("sw_platform_"), F.from_user.id.in_(ADMIN_IDS)
)
async def add_sw_platform_chosen(callback: CallbackQuery, state: FSMContext):
  platform_id = int(callback.data.split("_")[2])
  await state.update_data(platform_id=platform_id)
  await state.set_state(AdminStates.waiting_for_software_name)

  text = (
      "➕ **Добавление софта (Шаг 2/2)**\n\nВведите название софта (например:"
      " `Flexnote`):"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
      ]
  )
  await callback.message.edit_text(
      text, reply_markup=keyboard, parse_mode="Markdown"
  )
  await callback.answer()


@router.message(
    AdminStates.waiting_for_software_name, F.from_user.id.in_(ADMIN_IDS)
)
async def save_software(
    message: Message, state: FSMContext, session: AsyncSession
):
  data = await state.get_data()
  platform_id = data["platform_id"]
  name = message.text.strip()

  new_sw = Software(platform_id=platform_id, name=name)
  session.add(new_sw)
  await session.commit()
  await state.clear()

  await message.answer(
      f"✅ Софт `{name}` успешно добавлен!\n🔑 ID нового софта: `{new_sw.id}`",
      parse_mode="Markdown",
  )


@router.callback_query(
    F.data == "admin_add_prod", F.from_user.id.in_(ADMIN_IDS)
)
async def add_prod_start(callback: CallbackQuery, state: FSMContext):
  await state.set_state(AdminStates.waiting_for_sw_id)
  text = (
      "➕ **Создание тарифа (Шаг 1/3)**\n\nВведите **ID софта**, к которому"
      " добавляем тариф:"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
      ]
  )
  await callback.message.edit_text(
      text, reply_markup=keyboard, parse_mode="Markdown"
  )


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
          [
              InlineKeyboardButton(
                  text="⏳ 1 день", callback_data="duration_1 день"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⏳ 7 дней", callback_data="duration_7 дней"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⏳ 30 дней", callback_data="duration_30 дней"
              )
          ],
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
      ]
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(
    F.data.startswith("duration_"), F.from_user.id.in_(ADMIN_IDS)
)
async def process_duration(callback: CallbackQuery, state: FSMContext):
  duration = callback.data.split("_", 1)[1]
  await state.update_data(duration=duration)
  await state.set_state(AdminStates.waiting_for_price)

  text = (
      f"➕ **Создание тарифа (Шаг 3/3)**\n\nВыбран срок: `{duration}`\n\nТеперь"
      " отправьте **цену** (например: `450` или `1500.0`):"
  )
  await callback.message.edit_text(text, parse_mode="Markdown")
  await callback.answer()


@router.message(AdminStates.waiting_for_price, F.from_user.id.in_(ADMIN_IDS))
async def process_price_and_save(
    message: Message, state: FSMContext, session: AsyncSession
):
  try:
    price = float(message.text.strip().replace(",", "."))
  except ValueError:
    await message.answer(
        "❌ Неверный формат цены. Введите число (например: 500)"
    )
    return

  data = await state.get_data()
  software_id = data["software_id"]
  duration = data["duration"]

  new_product = Product(
      software_id=software_id, duration=duration, price=price
  )
  session.add(new_product)
  await session.commit()
  await state.clear()

  await message.answer(
      f"✅ **Тариф успешно создан!**\n\n"
      f"• ID товара для загрузки ключей: `{new_product.id}`\n"
      f"• Срок: `{duration}`\n"
      f"• Цена: `{price} RUB`\n\n"
      f"Загрузите ключи через раздел «📥 Загрузить ключи», используя ID"
      f" `{new_product.id}`.",
      parse_mode="Markdown",
  )


@router.callback_query(F.data == "admin_add_keys", F.from_user.id.in_(ADMIN_IDS))
async def add_keys_start(callback: CallbackQuery, state: FSMContext):
  await state.set_state(AdminStates.waiting_for_keys)
  text = (
      "📥 **Загрузка ключей**\n\nОтправьте сообщение, где:\n1️⃣ Первая строка —"
      " **ID товара (тарифа)**\n2️⃣ Со второй строки — сами ключи (каждый с"
      " новой строки).\n\nПример:\n`3`\n`KEY-1`\n`KEY-2`"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
      ]
  )
  await callback.message.edit_text(
      text, reply_markup=keyboard, parse_mode="Markdown"
  )


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
      session.add(
          LicenseKey(product_id=product_id, key_string=k, is_sold=False)
      )
      added += 1

  await session.commit()
  await state.clear()

  await message.answer(
      f"✅ Загружено новых ключей: `{added}` для товара ID `{product_id}`",
      parse_mode="Markdown",
  )
