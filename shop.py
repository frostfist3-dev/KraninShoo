import random
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database.models import DepositRequest, LicenseKey, Platform, Product, Purchase, Software, User

router = Router()

# Твои реквизиты для оплаты
CARDS = [
    "4441 1111 5555 7352 (Моно)",
    "4323 3473 8685 7285 (А-Банк)",
    "5232 4410 4407 1160 (Альянс)",
]

# ID админа, куда будут прилетать чеки на проверку
ADMIN_ID = 8479717148


class DepositStates(StatesGroup):
  waiting_for_amount = State()
  waiting_for_receipt = State()


# --- ПОПОЛНЕНИЕ БАЛАНСА ---
@router.callback_query(F.data == "deposit")
async def start_deposit(callback: CallbackQuery, state: FSMContext):
  await state.set_state(DepositStates.waiting_for_amount)
  await callback.message.edit_text(
      "💳 **Пополнение баланса**\n\nВведите сумму пополнения в рублях (или"
      " грн):",
      parse_mode="Markdown",
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
      f"📌 Переведите точную сумму на карту выше, а затем **отправьте сюда фото"
      f" чека или скриншот перевода**."
  )
  await message.answer(text, parse_mode="Markdown")


@router.message(DepositStates.waiting_for_receipt, F.photo)
async def process_receipt(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
):
  data = await state.get_data()
  amount = data["amount"]
  user_id = message.from_user.id
  photo_id = message.photo[-1].file_id

  # Создаем запись заявки
  req = DepositRequest(user_id=user_id, amount=amount)
  session.add(req)
  await session.commit()

  await state.clear()
  await message.answer(
      "⏳ **Заявка отправлена!**\nАдминистратор проверит перевод и зачислит"
      " баланс.",
      parse_mode="Markdown",
  )

  admin_kb = InlineKeyboardMarkup(
      inline_keyboard=[[
          InlineKeyboardButton(
              text="✅ Подтвердить", callback_data=f"approve_dep_{req.id}"
          ),
          InlineKeyboardButton(
              text="❌ Отклонить", callback_data=f"reject_dep_{req.id}"
          ),
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
      parse_mode="Markdown",
  )


# --- КАТАЛОГ И ПОКУПКА ---
@router.callback_query(F.data == "shop")
async def show_platforms(callback: CallbackQuery, session: AsyncSession):
  result = await session.execute(select(Platform))
  platforms = result.scalars().all()

  text = (
      "🛒 **Kranin Shop | Выбор платформы**\n\n📱 Выберите устройство, для"
      " которого покупаете софт:"
  )

  buttons = []
  for p in platforms:
    icon = "🤖" if "android" in p.name.lower() else "🍏"
    buttons.append([
        InlineKeyboardButton(
            text=f"{icon} {p.name}", callback_data=f"platform_{p.id}"
        )
    ])

  buttons.append(
      [InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")]
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

  await callback.message.edit_text(
      text, reply_markup=keyboard, parse_mode="Markdown"
  )
  await callback.answer()


@router.callback_query(F.data.startswith("platform_"))
async def show_softwares(callback: CallbackQuery, session: AsyncSession):
  platform_id = int(callback.data.split("_")[1])
  platform = await session.get(Platform, platform_id)

  result = await session.execute(
      select(Software).filter_by(platform_id=platform_id)
  )
  softwares = result.scalars().all()

  if not softwares:
    text = (
        f"🛒 **Kranin Shop — {platform.name}**\n\n❌ На данную платформу софт"
        " пока не добавлен."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Назад к платформам", callback_data="shop"
                )
            ]
        ]
    )
    await callback.message.edit_text(
        text, reply_markup=keyboard, parse_mode="Markdown"
    )
    return

  text = f"🛒 **Kranin Shop — {platform.name}**\n\n⚡️ Выберите нужный софт:"

  buttons = []
  for sw in softwares:
    buttons.append([
        InlineKeyboardButton(
            text=f"🛡 {sw.name}", callback_data=f"software_{sw.id}"
        )
    ])

  buttons.append(
      [InlineKeyboardButton(text="🔙 Назад к платформам", callback_data="shop")]
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

  await callback.message.edit_text(
      text, reply_markup=keyboard, parse_mode="Markdown"
  )
  await callback.answer()


@router.callback_query(F.data.startswith("software_"))
async def show_products(callback: CallbackQuery, session: AsyncSession):
  software_id = int(callback.data.split("_")[1])
  software = await session.get(Software, software_id)

  result = await session.execute(
      select(Product).filter_by(software_id=software_id)
  )
  products = result.scalars().all()

  if not products:
    text = (
        f"🛒 **{software.name}**\n\n❌ Тарифы для этого софта пока отсутствуют."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Назад к софту",
                    callback_data=f"platform_{software.platform_id}",
                )
            ]
        ]
    )
    await callback.message.edit_text(
        text, reply_markup=keyboard, parse_mode="Markdown"
    )
    return

  text = f"🛒 **{software.name}**\n\n📌 Выберите тариф:"

  buttons = []
  for p in products:
    keys_res = await session.execute(
        select(LicenseKey).filter_by(product_id=p.id, is_sold=False)
    )
    available_keys = len(keys_res.scalars().all())

    status = "🟢" if available_keys > 0 else "🔴"
    buttons.append([
        InlineKeyboardButton(
            text=f"{status} {p.duration} — {p.price} RUB",
            callback_data=f"buy_product_{p.id}",
        )
    ])

  buttons.append([
      InlineKeyboardButton(
          text="🔙 Назад", callback_data=f"platform_{software.platform_id}"
      )
  ])
  keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

  await callback.message.edit_text(
      text, reply_markup=keyboard, parse_mode="Markdown"
  )
  await callback.answer()


@router.callback_query(F.data.startswith("buy_product_"))
async def process_purchase(callback: CallbackQuery, session: AsyncSession):
  product_id = int(callback.data.split("_")[2])
  user_id = callback.from_user.id

  product = await session.get(Product, product_id)
  user = await session.get(User, user_id)
  software = await session.get(Software, product.software_id)

  if user.balance < product.price:
    await callback.answer(
        "❌ Недостаточно средств на балансе! Пополните кошелек.", show_alert=True
    )
    return

  key_res = await session.execute(
      select(LicenseKey)
      .filter_by(product_id=product.id, is_sold=False)
      .limit(1)
  )
  license_key = key_res.scalars().first()

  if not license_key:
    await callback.answer(
        "❌ Ключи этого номинала временно закончились!", show_alert=True
    )
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

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🔙 В главное меню", callback_data="main_menu"
              )
          ]
      ]
  )

  await callback.message.edit_text(
      success_text, reply_markup=keyboard, parse_mode="Markdown"
  )

