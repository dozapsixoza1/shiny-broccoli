import asyncio, logging, aiohttp, random
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ---------- КОНФИГ ----------
BOT_TOKEN = "8663442498:AAEvRdeDMzqJ7wjtoSoiJrIzSzK71v4p7oE"
CRYPTO_BOT_TOKEN = "588537:AAau2AOcmKEyh9f3H9fvlne4FvhtImZS2k6"
ADMIN_IDS = [8675927241]   # ID админов
CRYPTO_API_URL = "https://testnet-pay.crypt.bot/api"  # тестовая сеть, для боя замени на https://pay.crypt.bot/api

# ---------- ИНИТ ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ---------- ТОВАРЫ (физические номера) ----------
PRODUCTS = {
    "usa":      {"name": "+1 США",                     "price": 70,  "stock": 0, "numbers": []},
    "canada":   {"name": "+1 Канада",                  "price": 70,  "stock": 0, "numbers": []},
    "egypt":    {"name": "+20 Египет",                 "price": 50,  "stock": 0, "numbers": []},
    "myanmar":  {"name": "+95 Мьянма",                 "price": 35,  "stock": 0, "numbers": []},
    "random":   {"name": "Random Number (все номера)", "price": 25,  "stock": 0, "numbers": []}   # отдельный пул случайных номеров
}

# ID товаров для удобства
PRODUCT_IDS = list(PRODUCTS.keys())

# ---------- ДАННЫЕ ПОЛЬЗОВАТЕЛЕЙ ----------
cart = {}                     # user_id -> {"items": {prod_id: qty}, "promo": код, "discount": процент}
promo_codes = {}              # promo_code -> {"discount": int, "max_uses": int, "used_by": set, "active": bool}
active_users = set()          # для рассылки
orders = []                   # заглушка истории заказов
order_id_counter = 0

# ---------- СОСТОЯНИЯ FSM ----------
class PromoCreate(StatesGroup):
    waiting_for_code = State()
    waiting_for_discount = State()
    waiting_for_max_uses = State()

class Broadcast(StatesGroup):
    waiting_for_text = State()
    confirm = State()

class AddNumbers(StatesGroup):
    waiting_for_product = State()
    waiting_for_numbers = State()

# ---------- КЛАВИАТУРЫ ----------
def main_menu_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏪 Каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="📞 Поддержка", url="https://t.me/GeoShopSupport")]  # можешь сменить
    ])
    return kb

def catalog_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['name']} — {v['price']}₽", callback_data=f"prod_{k}")] for k, v in PRODUCTS.items()
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
        [InlineKeyboardButton(text="✏️ Изменить сток (вручную)", callback_data="admin_edit_stock")],
        [InlineKeyboardButton(text="📥 Пополнить номера", callback_data="admin_add_numbers")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📦 Заказы", callback_data="admin_orders")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
    ])
    return kb

# ---------- ИНТЕГРАЦИЯ CRYPTOBOT ----------
async def crypto_api_request(method: str, endpoint: str, params: dict = None, json_data: dict = None) -> dict:
    """Универсальный запрос к CryptoBot API"""
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    url = f"{CRYPTO_API_URL}/{endpoint}"
    async with aiohttp.ClientSession() as session:
        try:
            if method.upper() == "GET":
                async with session.get(url, headers=headers, params=params) as resp:
                    return await resp.json()
            elif method.upper() == "POST":
                async with session.post(url, headers=headers, json=json_data) as resp:
                    return await resp.json()
        except Exception as e:
            logging.error(f"CryptoBot API error: {e}")
            return {"ok": False, "error": str(e)}

async def create_invoice(amount: float, currency_type: str = "fiat", fiat: str = "RUB") -> dict:
    """
    Создание счёта на оплату.
    Возвращает полный ответ API, где result содержит invoice_id и pay_url.
    """
    params = {
        "asset": "USDT",           # или "TON", "BTC" и т.д., в тестовой сети обычно USDT
        "amount": str(amount),     # API требует строку
        "description": "Оплата заказа в GeoShop",
        "hidden_message": "Спасибо за покупку!",
        "paid_btn_name": "openBot",
        "paid_btn_url": "https://t.me/GeoShopBot",  # ссылка на бота
        "allow_anonymous": False,
        "expires_in": 600
    }
    # Примечание: для фиатной цены используется convert, но в тестовой сети fiat не работает, указываем amount в USDT
    return await crypto_api_request("POST", "createInvoice", json_data=params)

async def get_invoice_status(invoice_id: int) -> dict:
    """Получить статус конкретного инвойса"""
    data = await crypto_api_request("GET", "getInvoices", params={"invoice_ids": str(invoice_id)})
    return data

async def wait_for_payment(invoice_id: int, user_id: int, total: int, product_ids: list, quantities: dict, discount: float, promo_code: str):
    """
    Фоновая задача: опрашивает статус инвойса каждые 5 секунд в течение 10 минут.
    При оплате — списывает товары и выдаёт их пользователю.
    """
    deadline = asyncio.get_event_loop().time() + 600  # 10 минут
    while asyncio.get_event_loop().time() < deadline:
        status_resp = await get_invoice_status(invoice_id)
        if status_resp.get("ok") and status_resp["result"]["items"]:
            inv = status_resp["result"]["items"][0]
            if inv["status"] == "paid":
                # Выдача товара
                delivered = []
                for prod_id, qty in quantities.items():
                    for _ in range(qty):
                        if PRODUCTS[prod_id]["numbers"]:
                            number = PRODUCTS[prod_id]["numbers"].pop(0)  # берём первый номер
                            PRODUCTS[prod_id]["stock"] -= 1
                            delivered.append(f"{PRODUCTS[prod_id]['name']}: {number}")
                        else:
                            # на случай нехватки — сообщим в поддержку
                            delivered.append(f"{PRODUCTS[prod_id]['name']}: закончились, обратитесь в поддержку")
                text = "✅ Оплата получена!\n\nВыданные номера:\n" + "\n".join(delivered)
                await bot.send_message(user_id, text)

                # Если был применён промокод — фиксируем использование
                if promo_code and promo_code in promo_codes:
                    promo = promo_codes[promo_code]
                    promo.setdefault("used_by", set()).add(user_id)
                    if len(promo["used_by"]) >= promo.get("max_uses", 1):
                        promo["active"] = False

                # Добавляем запись в заказы
                global order_id_counter
                order_id_counter += 1
                orders.append({
                    "id": order_id_counter,
                    "user_id": user_id,
                    "items": {pid: qty for pid, qty in quantities.items()},
                    "total": total,
                    "status": "оплачен",
                    "delivered": delivered
                })
                return True
        await asyncio.sleep(5)
    # Тайм-аут
    await bot.send_message(user_id, "❌ Время оплаты истекло. Заказ отменён.")
    return False

# ---------- ХЭНДЛЕРЫ ----------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    active_users.add(msg.from_user.id)
    await msg.answer(
        "Добро пожаловать в GeoShop — магазин физических номеров. Выберите действие:",
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
    # обновим стоки перед показом (на случай ручных изменений)
    for pid in PRODUCT_IDS:
        PRODUCTS[pid]["stock"] = len(PRODUCTS[pid]["numbers"])
    await call.message.edit_text("Доступные номера:", reply_markup=catalog_kb())
    await call.answer()

# --- Детали товара ---
@dp.callback_query(F.data.startswith("prod_"))
async def show_product(call: CallbackQuery):
    prod_id = call.data.split("_", 1)[1]
    product = PRODUCTS.get(prod_id)
    if not product:
        await call.answer("Товар не найден", show_alert=True)
        return
    # обновим сток
    product["stock"] = len(product["numbers"])
    text = f"<b>{product['name']}</b>\n\nЦена: {product['price']}₽\nВ наличии: {product['stock']} шт."
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
    await call.answer("Добавлено!", show_alert=False)
    await call.message.edit_text("Товар добавлен в корзину. Продолжайте покупки:", reply_markup=catalog_kb())

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
    if user_id not in cart:
        cart[user_id] = {}
    cart[user_id]["promo"] = code
    cart[user_id]["discount"] = promo["discount"]  # процент
    await msg.answer(f"Промокод {code} применён! Скидка {promo['discount']}% будет учтена при оформлении.")

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

# --- Оформление заказа через CryptoBot ---
@dp.callback_query(F.data == "checkout")
async def checkout(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in cart or not cart[user_id].get("items"):
        await call.answer("Корзина пуста", show_alert=True)
        return
    items = cart[user_id]["items"]
    # Проверка наличия всех товаров
    for pid, qty in items.items():
        if len(PRODUCTS[pid]["numbers"]) < qty:
            await call.answer(f"Не хватает номеров: {PRODUCTS[pid]['name']}", show_alert=True)
            return
    total = sum(PRODUCTS[pid]["price"] * qty for pid, qty in items.items())
    discount = cart[user_id].get("discount", 0)
    promo_code = cart[user_id].get("promo")
    if discount and promo_code:
        total = int(total * (100 - discount) / 100)

    # Создаём инвойс в CryptoBot
    # Сумма в USDT, нужно конвертировать: курс USDT к RUB примерно 90, можно зафиксировать или запрашивать динамически.
    # Для теста используем примерное соотношение 1 USDT = 90 RUB
    usdt_amount = round(total / 90, 2)
    invoice_resp = await create_invoice(usdt_amount)
    if not invoice_resp.get("ok"):
        await call.message.edit_text("Ошибка создания платежа. Попробуйте позже.", reply_markup=main_menu_kb())
        await call.answer()
        return

    invoice_id = invoice_resp["result"]["invoice_id"]
    pay_url = invoice_resp["result"]["pay_url"]

    # Очищаем корзину сразу, чтобы избежать повторной покупки (но можно и после оплаты, решай сам)
    # Безопаснее оставить до подтверждения, но тогда надо блокировать товары.
    # Для простоты пока очищаем, но при отмене заказа нужно возвращать номера обратно в пул.
    # Пока реализуем так: просто убираем из корзины.
    cart[user_id]["items"] = {}

    # Запускаем фоновый опрос статуса платежа
    asyncio.create_task(
        wait_for_payment(invoice_id, user_id, total, list(items.keys()), items, discount, promo_code)
    )

    # Отправляем ссылку на оплату
    await call.message.edit_text(
        f"🧾 <b>Заказ на сумму {total}₽</b> (к оплате ~{usdt_amount} USDT)\n\n"
        f"Для оплаты перейдите по ссылке и следуйте инструкциям CryptoBot:\n{pay_url}\n\n"
        "После успешной оплаты номера будут выданы автоматически.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_url)],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
        ])
    )
    await call.answer()

# ---------- АДМИН-ПАНЕЛЬ ----------
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer("Доступ запрещён.")
        return
    await msg.answer("Админ-панель GeoShop", reply_markup=admin_menu_kb())

# --- Создание промокода (FSM) ---
@dp.callback_query(F.data == "admin_create_promo")
async def start_create_promo(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    await call.message.answer("Введите код промокода (заглавными буквами):")
    await state.set_state(PromoCreate.waiting_for_code)
    await call.answer()

@dp.message(PromoCreate.waiting_for_code)
async def process_promo_code(msg: Message, state: FSMContext):
    code = msg.text.strip().upper()
    if not code:
        await msg.answer("Код не может быть пустым.")
        return
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
    await msg.answer(f"Промокод {data['code']} создан! Скидка {data['discount']}%, лимит {max_uses}.", reply_markup=admin_menu_kb())
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
    await list_promos(call)

# --- Пополнение номеров (FSM) ---
@dp.callback_query(F.data == "admin_add_numbers")
async def add_numbers_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    # Показываем клавиатуру с товарами
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=v["name"], callback_data=f"addnumsel_{k}")] for k, v in PRODUCTS.items()
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin")]])
    await call.message.edit_text("Выберите товар для пополнения номеров:", reply_markup=kb)
    await call.answer()
    await state.set_state(AddNumbers.waiting_for_product)

@dp.callback_query(AddNumbers.waiting_for_product, F.data.startswith("addnumsel_"))
async def product_selected_for_nums(call: CallbackQuery, state: FSMContext):
    prod_id = call.data.split("_", 1)[1]
    await state.update_data(prod_id=prod_id)
    await call.message.answer(f"Выбран {PRODUCTS[prod_id]['name']}. Введите номера через запятую (например, +1234567890,+1987654321):")
    await state.set_state(AddNumbers.waiting_for_numbers)
    await call.answer()

@dp.message(AddNumbers.waiting_for_numbers)
async def numbers_entered(msg: Message, state: FSMContext):
    data = await state.get_data()
    prod_id = data['prod_id']
    raw = msg.text.strip()
    numbers = [n.strip() for n in raw.split(",") if n.strip()]
    if not numbers:
        await msg.answer("Номера не введены. Отмена.")
        await state.clear()
        return
    PRODUCTS[prod_id]["numbers"].extend(numbers)
    PRODUCTS[prod_id]["stock"] = len(PRODUCTS[prod_id]["numbers"])
    await msg.answer(f"Добавлено {len(numbers)} номеров в {PRODUCTS[prod_id]['name']}. Всего: {PRODUCTS[prod_id]['stock']}", reply_markup=admin_menu_kb())
    await state.clear()

# Также можно добавить команду /addnums для быстрого пополнения
@dp.message(Command("addnums"))
async def addnums_cmd(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    args = msg.text.split(maxsplit=2)
    if len(args) < 3:
        await msg.answer("Использование: /addnums <код_товара> номер1,номер2,...\nКоды: usa, canada, egypt, myanmar, random")
        return
    prod_id = args[1].lower()
    if prod_id not in PRODUCTS:
        await msg.answer("Неверный код товара.")
        return
    numbers = [n.strip() for n in args[2].split(",") if n.strip()]
    if not numbers:
        await msg.answer("Номера не указаны.")
        return
    PRODUCTS[prod_id]["numbers"].extend(numbers)
    PRODUCTS[prod_id]["stock"] = len(PRODUCTS[prod_id]["numbers"])
    await msg.answer(f"Добавлено {len(numbers)} номеров в {PRODUCTS[prod_id]['name']}. Всего: {PRODUCTS[prod_id]['stock']}")

# --- Рассылка ---
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
    await msg.answer("Подтвердите отправку сообщения всем пользователям (да/нет):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="confirm_broadcast")],
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
        return
    if not msg.text or len(msg.text) <= 5:
        await msg.answer("Используйте: /send <текст>")
        return
    text = msg.text[5:].strip()
    if not text:
        await msg.answer("Текст не может быть пустым.")
        return
    await state.update_data(text=text)
    await msg.answer("Подтвердите отправку сообщения всем пользователям (да/нет):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="cancel_broadcast")]
    ]))
    await state.set_state(Broadcast.confirm)

# --- Изменение стока (ручное) ---
@dp.callback_query(F.data == "admin_edit_stock")
async def edit_stock_menu(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['name']} (номенов: {len(v['numbers'])})", callback_data=f"stock_{k}")] for k, v in PRODUCTS.items()
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin")]])
    await call.message.edit_text("Выберите товар, чтобы изменить количество номеров (удалить/добавить через список):", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("stock_"))
async def ask_new_stock(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    prod_id = call.data.split("_", 1)[1]
    await state.update_data(edit_prod=prod_id)
    current = len(PRODUCTS[prod_id]["numbers"])
    await call.message.answer(f"Текущее количество номеров: {current}. Введите новое количество (излишки номеров будут удалены с начала списка, нехватка — просто установит ноль):")
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
    numbers = PRODUCTS[prod_id]["numbers"]
    if new_stock < len(numbers):
        PRODUCTS[prod_id]["numbers"] = numbers[:new_stock]  # обрезаем
    else:
        # если больше, то добавлять нечего, просто ничего не делаем
        pass
    PRODUCTS[prod_id]["stock"] = len(PRODUCTS[prod_id]["numbers"])
    await msg.answer(f"Количество номеров для {PRODUCTS[prod_id]['name']} изменено до {PRODUCTS[prod_id]['stock']}.", reply_markup=admin_menu_kb())
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
    text = "📦 <b>Последние заказы:</b>\n\n"
    for o in orders[-10:]:
        text += f"#{o['id']} | User {o['user_id']} | {o['total']}₽ | {o['status']}\n"
    await call.message.edit_text(text, reply_markup=admin_menu_kb(), parse_mode="HTML")
    await call.answer()

# ---------- ЗАПУСК ----------
async def main():
    logging.basicConfig(level=logging.INFO)
    # Инициализируем стоки как пустые списки (при старте)
    for pid in PRODUCT_IDS:
        PRODUCTS[pid]["stock"] = len(PRODUCTS[pid]["numbers"])
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
