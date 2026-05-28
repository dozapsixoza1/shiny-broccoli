import asyncio, logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ---------- КОНФИГ ----------
BOT_TOKEN = "ВСТАВЬ_ТОКЕН_БОТА"
ADMIN_IDS = [123456789]           # ID админов

# ---------- ИНИТ ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ---------- МОК-БАЗА ДАННЫХ ----------
PRODUCTS = {
    "tg_phy_1": {"name": "TG Аккаунт (физич. номер, РФ)", "price": 450, "stock": 28, "desc": "Прогретый 30 дней."},
    "tg_phy_2": {"name": "TG Аккаунт (UA номер, физ.)", "price": 420, "stock": 15, "desc": "Чистый, 0 диалогов."},
    "sim_rent":   {"name": "Аренда номера (1 час)", "price": 120, "stock": 999, "desc": "Приём СМС, реальная SIM."},
    "sim_buy":    {"name": "Покупка номера (навсегда)", "price": 1800, "stock": 8, "desc": "Физическая SIM, eSIM по запросу."}
}

# user_id -> {product_id: quantity, "promo": code, "discount": percent}
cart = {}

# promo_code -> {"discount": 15, "max_uses": 50, "used_by": set(), "active": True}
promo_codes = {}

# все ID, кто запускал бота (для рассылки)
active_users = set()

# заказы (заглушка) - order_id -> {user_id, items, total, status}
orders = []
order_id_counter = 0

# ---------- СОСТОЯНИЯ FSM ----------
class PromoCreate(StatesGroup):
    waiting_for_code = State()
    waiting_for_discount = State()
    waiting_for_max_uses = State()

class Broadcast(StatesGroup):
    waiting_for_text = State()
    confirm = State()

# ---------- КЛАВИАТУРЫ ----------
def main_menu_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏪 Каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="📞 Поддержка", url="https://t.me/GeoShopSupport")]
    ])
    return kb

def catalog_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['name']} - {v['price']}₽", callback_data=f"prod_{k}")] for k, v in PRODUCTS.items()
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]])
    return kb

def product_detail_kb(product_id: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в корзину", callback_data=f"add_{product_id}")],
        [InlineKeyboardButton(text="🔙 К каталогу", callback_data="catalog")]
    ])
    return kb

def cart_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout")],
        [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="clear_cart")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
    ])
    return kb

def admin_menu_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_create_promo")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_list_promo")],
        [InlineKeyboardButton(text="✏️ Изменить сток", callback_data="admin_edit_stock")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📦 Заказы", callback_data="admin_orders")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
    ])
    return kb

# ---------- ХЭНДЛЕРЫ ----------

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    active_users.add(msg.from_user.id)
    await msg.answer(
        "[⚡] GMODE\n\nДобро пожаловать в GeoShop — магазин физических номеров и аккаунтов Telegram. Выберите действие:",
        reply_markup=main_menu_kb()
    )

# --- Главное меню (колбэк) ---
@dp.callback_query(F.data == "main_menu")
async def back_to_main(call: CallbackQuery):
    await call.message.edit_text("Главное меню GeoShop:", reply_markup=main_menu_kb())
    await call.answer()

# --- Каталог ---
@dp.callback_query(F.data == "catalog")
async def show_catalog(call: CallbackQuery):
    await call.message.edit_text("Доступные позиции:", reply_markup=catalog_kb())
    await call.answer()

# --- Детали товара ---
@dp.callback_query(F.data.startswith("prod_"))
async def show_product(call: CallbackQuery):
    prod_id = call.data.split("_", 1)[1]
    product = PRODUCTS.get(prod_id)
    if not product:
        await call.answer("Товар не найден", show_alert=True)
        return
    text = f"<b>{product['name']}</b>\n\n{product['desc']}\nЦена: {product['price']}₽\nВ наличии: {product['stock']} шт."
    await call.message.edit_text(text, reply_markup=product_detail_kb(prod_id), parse_mode="HTML")
    await call.answer()

# --- Добавление в корзину ---
@dp.callback_query(F.data.startswith("add_"))
async def add_to_cart(call: CallbackQuery):
    prod_id = call.data.split("_", 1)[1]
    user_id = call.from_user.id
    if user_id not in cart:
        cart[user_id] = {}
    if "items" not in cart[user_id]:
        cart[user_id]["items"] = {}
    cart[user_id]["items"][prod_id] = cart[user_id]["items"].get(prod_id, 0) + 1
    await call.answer("Товар добавлен в корзину!", show_alert=False)
    await call.message.edit_text("Добавлено. Продолжайте покупки:", reply_markup=catalog_kb())

# --- Применение промокода ---
@dp.message(Command("promo"))
async def apply_promo(msg: Message):
    user_id = msg.from_user.id
    args = msg.text.split()
    if len(args) != 2:
        await msg.answer("Используйте: /promo КОД")
        return
    code = args[1].upper()
    promo = promo_codes.get(code)
    if not promo or not promo.get("active", False):
        await msg.answer("Промокод недействителен.")
        return
    if len(promo.get("used_by", set())) >= promo.get("max_uses", 1):
        await msg.answer("Лимит использований исчерпан.")
        return
    if user_id in promo.get("used_by", set()):
        await msg.answer("Вы уже использовали этот промокод.")
        return
    # Применяем к корзине
    if user_id not in cart:
        cart[user_id] = {}
    cart[user_id]["promo"] = code
    cart[user_id]["discount"] = promo["discount"]  # процент
    await msg.answer(f"Промокод {code} применён! Скидка {promo['discount']}% будет учтена при оформлении.")
    # Помечаем использование позже при успешном заказе

# --- Корзина ---
@dp.callback_query(F.data == "cart")
async def show_cart(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in cart or not cart[user_id].get("items"):
        await call.message.edit_text("Ваша корзина пуста.", reply_markup=main_menu_kb())
        await call.answer()
        return
    items = cart[user_id]["items"]
    total = sum(PRODUCTS[pid]["price"] * qty for pid, qty in items.items())
    discount = cart[user_id].get("discount", 0)
    if discount:
        total = int(total * (100 - discount) / 100)
    text_lines = [f"{PRODUCTS[pid]['name']} x{qty} — {PRODUCTS[pid]['price'] * qty}₽" for pid, qty in items.items()]
    text = "🛒 <b>Корзина:</b>\n\n" + "\n".join(text_lines)
    if discount:
        text += f"\n\nСкидка по промокоду: {discount}%"
    text += f"\n\n<b>Итого: {total}₽</b>"
    text += "\n\n<i>Применить промокод: /promo КОД</i>"
    await call.message.edit_text(text, reply_markup=cart_kb(), parse_mode="HTML")
    await call.answer()

# --- Очистка корзины ---
@dp.callback_query(F.data == "clear_cart")
async def clear_cart_handler(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id in cart:
        cart[user_id] = {"items": {}}
    await call.message.edit_text("Корзина очищена.", reply_markup=main_menu_kb())
    await call.answer()

# --- Оформление заказа ---
@dp.callback_query(F.data == "checkout")
async def checkout(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in cart or not cart[user_id].get("items"):
        await call.answer("Корзина пуста", show_alert=True)
        return
    items = cart[user_id]["items"]
    total = sum(PRODUCTS[pid]["price"] * qty for pid, qty in items.items())
    discount = cart[user_id].get("discount", 0)
    promo_code = cart[user_id].get("promo")
    if discount and promo_code:
        total = int(total * (100 - discount) / 100)
        # Списать использование промокода
        promo = promo_codes.get(promo_code)
        if promo:
            promo.setdefault("used_by", set()).add(user_id)
            if len(promo["used_by"]) >= promo.get("max_uses", 1):
                promo["active"] = False
    # Заглушка создания заказа
    global order_id_counter
    order_id_counter += 1
    orders.append({"id": order_id_counter, "user_id": user_id, "items": items, "total": total, "status": "ожидает оплаты"})
    # Очищаем корзину
    cart[user_id] = {"items": {}}
    await call.message.edit_text(
        f"Заказ #{order_id_counter} создан на сумму {total}₽. Оплатите через поддержку: @GeoShopSupport",
        reply_markup=main_menu_kb()
    )
    await call.answer()

# --- Баланс (заглушка) ---
@dp.callback_query(F.data == "balance")
async def show_balance(call: CallbackQuery):
    await call.message.edit_text("Ваш баланс: <b>0 ₽</b>\n\nПополнение через поддержку.", reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()

# ========== АДМИН-ПАНЕЛЬ ==========
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer("Доступ запрещён.")
        return
    await msg.answer("[⚡] GMODE Admin Panel", reply_markup=admin_menu_kb())

# --- Создание промокода (FSM) ---
@dp.callback_query(F.data == "admin_create_promo")
async def start_create_promo(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    await call.message.answer("Введите код промокода (например, SALE20):")
    await state.set_state(PromoCreate.waiting_for_code)
    await call.answer()

@dp.message(PromoCreate.waiting_for_code)
async def process_promo_code(msg: Message, state: FSMContext):
    code = msg.text.strip().upper()
    if code in promo_codes:
        await msg.answer("Такой код уже существует. Введите другой.")
        return
    await state.update_data(code=code)
    await msg.answer("Введите процент скидки (только число, например 15):")
    await state.set_state(PromoCreate.waiting_for_discount)

@dp.message(PromoCreate.waiting_for_discount)
async def process_discount(msg: Message, state: FSMContext):
    try:
        discount = int(msg.text)
        if not 1 <= discount <= 99:
            raise ValueError
    except:
        await msg.answer("Неверный процент. Введите число от 1 до 99.")
        return
    await state.update_data(discount=discount)
    await msg.answer("Введите максимальное количество использований (целое число):")
    await state.set_state(PromoCreate.waiting_for_max_uses)

@dp.message(PromoCreate.waiting_for_max_uses)
async def process_max_uses(msg: Message, state: FSMContext):
    try:
        max_uses = int(msg.text)
        if max_uses <= 0:
            raise ValueError
    except:
        await msg.answer("Неверное число. Введите положительное целое.")
        return
    data = await state.get_data()
    promo_codes[data['code']] = {
        "discount": data['discount'],
        "max_uses": max_uses,
        "used_by": set(),
        "active": True
    }
    await msg.answer(f"Промокод {data['code']} создан! Скидка {data['discount']}%, лимит {max_uses}.")
    await state.clear()

# --- Список промокодов ---
@dp.callback_query(F.data == "admin_list_promo")
async def list_promos(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    if not promo_codes:
        await call.message.edit_text("Промокодов нет.", reply_markup=admin_menu_kb())
        await call.answer()
        return
    text = "📋 <b>Промокоды:</b>\n\n"
    for code, p in promo_codes.items():
        used = len(p.get("used_by", set()))
        status = "✅ активен" if p.get("active") else "❌ исчерпан"
        text += f"<b>{code}</b> — скидка {p['discount']}%, лимит {p['max_uses']} (исп. {used}), {status}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ Удалить {c}", callback_data=f"delpromo_{c}")] for c in promo_codes
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

# --- Удаление промокода ---
@dp.callback_query(F.data.startswith("delpromo_"))
async def delete_promo(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    code = call.data.split("_", 1)[1]
    if code in promo_codes:
        del promo_codes[code]
        await call.answer(f"Промокод {code} удалён.", show_alert=True)
    else:
        await call.answer("Не найден", show_alert=True)
    await list_promos(call)  # обновим список

# --- Рассылка (через FSM) ---
@dp.callback_query(F.data == "admin_broadcast")
async def start_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    await call.message.answer("Введите текст для рассылки (можно HTML):")
    await state.set_state(Broadcast.waiting_for_text)
    await call.answer()

@dp.message(Broadcast.waiting_for_text)
async def process_broadcast_text(msg: Message, state: FSMContext):
    await state.update_data(text=msg.html_text)
    await msg.answer("Подтвердите отправку сообщения всем пользователям бота (да/нет):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отправить", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")]
    ]))
    await state.set_state(Broadcast.confirm)

@dp.callback_query(F.data == "confirm_broadcast", Broadcast.confirm)
async def confirm_broadcast(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data['text']
    count = 0
    for uid in active_users:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            count += 1
        except:
            pass
    await call.message.edit_text(f"Рассылка завершена. Отправлено {count} пользователям.", reply_markup=admin_menu_kb())
    await state.clear()
    await call.answer()

@dp.callback_query(F.data == "cancel_broadcast", Broadcast.confirm)
async def cancel_broadcast(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Рассылка отменена.", reply_markup=admin_menu_kb())
    await state.clear()
    await call.answer()

# Также админы могут инициировать рассылку командой /send
@dp.message(Command("send"))
async def broadcast_cmd(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer("Доступ запрещён.")
        return
    if not msg.text or len(msg.text) <= 5:
        await msg.answer("Используйте: /send <текст>")
        return
    text = msg.text[5:].strip()
    if not text:
        await msg.answer("Текст не может быть пустым.")
        return
    # сразу спрашиваем подтверждение
    await state.update_data(text=text)
    await msg.answer("Подтвердите отправку сообщения всем пользователям бота (да/нет):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="cancel_broadcast")]
    ]))
    await state.set_state(Broadcast.confirm)

# --- Изменение стока (админ) ---
@dp.callback_query(F.data == "admin_edit_stock")
async def edit_stock_menu(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['name']} (остаток {v['stock']})", callback_data=f"stock_{k}")] for k, v in PRODUCTS.items()
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin")]])
    await call.message.edit_text("Выберите товар для изменения остатка:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("stock_"))
async def ask_new_stock(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    prod_id = call.data.split("_", 1)[1]
    await state.update_data(edit_prod=prod_id)
    await call.message.answer(f"Текущий остаток {PRODUCTS[prod_id]['stock']}. Введите новое количество:")
    await state.set_state("edit_stock_value")
    await call.answer()

@dp.message(F.state == "edit_stock_value")
async def set_new_stock(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        new_stock = int(msg.text)
        if new_stock < 0:
            raise ValueError
    except:
        await msg.answer("Неверное число. Введите целое ≥ 0.")
        return
    data = await state.get_data()
    prod_id = data['edit_prod']
    PRODUCTS[prod_id]['stock'] = new_stock
    await msg.answer(f"Остаток {PRODUCTS[prod_id]['name']} обновлён до {new_stock}.", reply_markup=admin_menu_kb())
    await state.clear()

# --- Заказы (заглушка) ---
@dp.callback_query(F.data == "admin_orders")
async def show_orders(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    if not orders:
        await call.message.edit_text("Заказов пока нет.", reply_markup=admin_menu_kb())
        await call.answer()
        return
    text = "📦 <b>Заказы:</b>\n\n"
    for o in orders[-10:]:  # последние 10
        text += f"#{o['id']} | User {o['user_id']} | {o['total']}₽ | {o['status']}\n"
    await call.message.edit_text(text, reply_markup=admin_menu_kb(), parse_mode="HTML")
    await call.answer()

# ========== ЗАПУСК ==========
async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
