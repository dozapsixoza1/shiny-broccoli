import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict, field

import aiohttp
from aiogram import Dispatcher, Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== КОНФИГ ====================
BOT_TOKEN = "8663442498:AAEvRdeDMzqJ7wjtoSoiJrIzSzK71v4p7oE"
ADMIN_IDS = [8675927241]  # Добавьте ID админов
CRYPTOBOT_API_TOKEN = "588537:AAau2AOcmKEyh9f3H9fvlne4FvhtImZS2k6"
CRYPTOBOT_BASE_URL = "https://pay.crypt.bot/api"

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== МОДЕЛИ ДАННЫХ ====================
@dataclass
class Product:
    code: str
    name: str
    price: int
    numbers: List[str] = field(default_factory=list)
    
    @property
    def stock(self) -> int:
        return len(self.numbers)

@dataclass
class PromoCode:
    code: str
    discount_percent: int
    usage_limit: int
    used_count: int = 0
    
    @property
    def is_active(self) -> bool:
        return self.used_count < self.usage_limit

@dataclass
class Order:
    order_id: str
    user_id: int
    product_code: str
    quantity: int
    total_amount: int
    promo_code: Optional[str]
    status: str  # "pending", "paid", "cancelled"
    created_at: str
    invoice_id: Optional[int] = None
    numbers: List[str] = field(default_factory=list)

# ==================== ФСМ СОСТОЯНИЯ ====================
class AdminStates(StatesGroup):
    main_menu = State()
    create_promo_code = State()
    create_promo_code_discount = State()
    create_promo_code_limit = State()
    manage_stock = State()
    manage_stock_quantity = State()
    add_numbers = State()
    add_numbers_input = State()
    send_message = State()
    send_message_confirm = State()

class UserStates(StatesGroup):
    in_catalog = State()
    in_cart = State()

# ==================== БАЗА ДАННЫХ В ПАМЯТИ ====================
class Database:
    def __init__(self):
        self.products: Dict[str, Product] = {
            "usa": Product("usa", "🇺🇸 +1 США", 70, []),
            "canada": Product("canada", "🇨🇦 +1 Канада", 70, []),
            "egypt": Product("egypt", "🇪🇬 +20 Египет", 50, []),
            "myanmar": Product("myanmar", "🇲🇲 +95 Мьянма", 35, []),
            "random": Product("random", "🎲 Random Number", 25, [])
        }
        self.cart: Dict[int, Dict[str, int]] = {}
        self.promo_codes: Dict[str, PromoCode] = {}
        self.orders: List[Order] = []
        self.active_users: set = set()
        self.invoice_mapping: Dict[int, tuple] = {}
        self.active_payments: Dict[str, bool] = {}  # Отслеживание активных платежей

    def get_cart_total(self, user_id: int) -> int:
        total = 0
        if user_id in self.cart:
            for product_code, quantity in self.cart[user_id].items():
                if product_code in self.products:
                    total += self.products[product_code].price * quantity
        return total

    def apply_promo(self, user_id: int, code: str) -> tuple:
        """Применить промокод. Возвращает (успех, сообщение, новая сумма)"""
        if code not in self.promo_codes:
            return False, "❌ Промокод не найден", self.get_cart_total(user_id)
        
        promo = self.promo_codes[code]
        if not promo.is_active:
            return False, f"❌ Промокод исчерпан ({promo.used_count}/{promo.usage_limit})", self.get_cart_total(user_id)
        
        original_total = self.get_cart_total(user_id)
        discount = int(original_total * promo.discount_percent / 100)
        new_total = original_total - discount
        
        return True, f"✅ Промокод применён! Скидка: {promo.discount_percent}% (-{discount} руб)", new_total

    def save_backup(self) -> str:
        """Сохранить резервную копию в JSON"""
        backup = {
            "products": {code: asdict(product) for code, product in self.products.items()},
            "promo_codes": {code: asdict(promo) for code, promo in self.promo_codes.items()},
            "orders": [asdict(order) for order in self.orders[-100:]],
            "active_users": list(self.active_users),
            "timestamp": datetime.now().isoformat()
        }
        return json.dumps(backup, ensure_ascii=False, indent=2)

db = Database()

# ==================== CRYPTOBOT API ====================
async def crypto_api_request(method: str, endpoint: str, timeout: int = 5, **kwargs) -> Optional[dict]:
    """Универсальная функция для запросов к CryptoBot API"""
    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_API_TOKEN,
        "Content-Type": "application/json"
    }
    url = f"{CRYPTOBOT_BASE_URL}/{endpoint}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, 
                headers=headers, 
                json=kwargs if kwargs else None,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"CryptoBot error: {response.status}")
                    return None
    except asyncio.TimeoutError:
        logger.error(f"CryptoBot timeout for {endpoint}")
        return None
    except Exception as e:
        logger.error(f"CryptoBot request error: {e}")
        return None

async def create_invoice(user_id: int, amount: int, order_id: str) -> Optional[int]:
    """Создать инвойс в CryptoBot"""
    response = await crypto_api_request(
        "POST",
        "invoices/create",
        amount=str(amount),
        currency_type="fiat",
        fiat="RUB",
        accepted_assets=["USDT", "TON"],
        expires_in=600,
        description=f"Order #{order_id}",
        payload={"user_id": user_id, "order_id": order_id}
    )
    
    if response and response.get("ok"):
        invoice_id = response["result"]["invoice_id"]
        db.invoice_mapping[invoice_id] = (user_id, order_id)
        return invoice_id
    return None

async def check_invoice_status(invoice_id: int) -> Optional[str]:
    """Проверить статус инвойса"""
    response = await crypto_api_request("GET", f"invoices/info?invoice_id={invoice_id}", timeout=3)
    if response and response.get("ok"):
        return response["result"]["status"]
    return None

async def wait_for_payment(bot: Bot, user_id: int, order_id: str, invoice_id: int, timeout: int = 600):
    """Ожидать оплату инвойса (асинхронно, опрос каждые 5 секунд)"""
    if order_id in db.active_payments:
        return
    
    db.active_payments[order_id] = True
    start_time = asyncio.get_event_loop().time()
    
    try:
        while asyncio.get_event_loop().time() - start_time < timeout:
            try:
                status = await check_invoice_status(invoice_id)
                
                if status == "paid":
                    order = next((o for o in db.orders if o.order_id == order_id), None)
                    if order:
                        # Выдаём номера
                        for product_code, quantity in db.cart.get(user_id, {}).items():
                            if product_code in db.products:
                                product = db.products[product_code]
                                issued_numbers = product.numbers[:quantity]
                                order.numbers = issued_numbers
                                product.numbers = product.numbers[quantity:]
                        
                        order.status = "paid"
                        
                        # Отправляем номера пользователю
                        if order.numbers:
                            numbers_text = "\n".join(order.numbers)
                            try:
                                await bot.send_message(
                                    user_id,
                                    f"✅ <b>Оплата успешна!</b>\n\n"
                                    f"Заказ #{order_id}\n"
                                    f"Ваши номера:\n\n<code>{numbers_text}</code>\n\n"
                                    f"Сумма: {order.total_amount} RUB",
                                    parse_mode="HTML"
                                )
                            except Exception as e:
                                logger.error(f"Error sending numbers: {e}")
                        
                        # Очищаем корзину
                        if user_id in db.cart:
                            db.cart[user_id].clear()
                    
                    return
                
                elif status == "expired":
                    order = next((o for o in db.orders if o.order_id == order_id), None)
                    if order:
                        order.status = "cancelled"
                        try:
                            await bot.send_message(user_id, "⏱️ Время оплаты истекло. Заказ отменён.")
                        except:
                            pass
                    return
                
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Payment check error: {e}")
                await asyncio.sleep(5)
        
        # Таймаут
        order = next((o for o in db.orders if o.order_id == order_id), None)
        if order:
            order.status = "cancelled"
            try:
                await bot.send_message(user_id, "⏱️ Время оплаты истекло. Заказ отменён.")
            except:
                pass
    finally:
        db.active_payments.pop(order_id, None)

# ==================== КЛАВИАТУРЫ ====================
def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏪 Каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="📞 Поддержка", url="https://t.me/GeoShopSupport")]
    ])

def catalog_keyboard():
    buttons = []
    for code, product in db.products.items():
        stock_text = f"{product.stock}шт" if product.stock > 0 else "Нет"
        buttons.append([InlineKeyboardButton(
            text=f"{product.name} ({product.price}₽) | {stock_text}",
            callback_data=f"product_{code}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def product_details_keyboard(product_code: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в корзину", callback_data=f"add_cart_{product_code}")],
        [InlineKeyboardButton(text="◀️ К каталогу", callback_data="catalog")]
    ])

def cart_keyboard(user_id: int):
    buttons = []
    if user_id in db.cart and db.cart[user_id]:
        buttons.append([InlineKeyboardButton(text="🗑️ Очистить корзину", callback_data="clear_cart")])
        buttons.append([InlineKeyboardButton(text="💳 Оформить заказ", callback_data="checkout")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_create_promo")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_list_promo")],
        [InlineKeyboardButton(text="📦 Управление стоком", callback_data="admin_manage_stock")],
        [InlineKeyboardButton(text="➕ Пополнить номера", callback_data="admin_add_numbers")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_send_message")],
        [InlineKeyboardButton(text="📊 Заказы", callback_data="admin_orders")],
        [InlineKeyboardButton(text="💾 Резервная копия", callback_data="admin_backup")],
        [InlineKeyboardButton(text="◀️ Выход", callback_data="admin_exit")]
    ])

def products_select_keyboard(for_stock: bool = False):
    buttons = []
    for code, product in db.products.items():
        action = "stock" if for_stock else "addnum"
        buttons.append([InlineKeyboardButton(text=product.name, callback_data=f"admin_{action}_{code}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="confirm_no")]
    ])

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    db.active_users.add(user_id)
    
    await message.answer(
        f"🎉 Добро пожаловать в <b>GeoShop</b>!\n\n"
        f"Здесь вы можете купить физические номера из разных стран.\n\n"
        f"Выберите действие:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет доступа к админ-панели.")
        return
    
    await state.set_state(AdminStates.main_menu)
    await message.answer(
        "<b>👨‍💼 Админ-панель GeoShop</b>",
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard()
    )

@router.message(Command("addnums"))
async def cmd_addnums(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("Использование: /addnums <код_товара> номер1,номер2,...")
            return
        
        product_code = parts[1].lower()
        numbers_text = parts[2]
        
        if product_code not in db.products:
            await message.answer(f"❌ Товар '{product_code}' не найден.")
            return
        
        numbers = [n.strip() for n in numbers_text.split(",") if n.strip()]
        db.products[product_code].numbers.extend(numbers)
        
        await message.answer(
            f"✅ Добавлено {len(numbers)} номеров в '{db.products[product_code].name}'\n"
            f"Новый сток: {db.products[product_code].stock}"
        )
    except Exception as e:
        logger.error(f"addnums error: {e}")
        await message.answer(f"❌ Ошибка: {e}")

@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    text = message.text.replace("/send ", "", 1).strip()
    if not text:
        await message.answer("Использование: /send <текст рассылки>")
        return
    
    await state.set_state(AdminStates.send_message_confirm)
    await state.update_data(send_text=text)
    
    await message.answer(
        f"<b>Подтверждение рассылки</b>\n\nТекст:\n{text}\n\n"
        f"Отправить {len(db.active_users)} пользователям?",
        parse_mode="HTML",
        reply_markup=confirm_keyboard()
    )

@router.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        backup_data = db.save_backup()
        
        with open("/tmp/backup.json", "w") as f:
            f.write(backup_data)
        
        await message.answer_document(
            InputFile("/tmp/backup.json"),
            caption="💾 Резервная копия GeoShop"
        )
    except Exception as e:
        logger.error(f"backup error: {e}")
        await message.answer(f"❌ Ошибка создания резервной копии: {e}")

@router.message(Command("promo"))
async def cmd_promo(message: Message):
    try:
        code = message.text.replace("/promo ", "").strip().upper()
        user_id = message.from_user.id
        
        if user_id not in db.cart or not db.cart[user_id]:
            await message.answer("❌ Корзина пуста")
            return
        
        success, msg, new_total = db.apply_promo(user_id, code)
        
        if success:
            promo = db.promo_codes[code]
            promo.used_count += 1
        
        await message.answer(msg)
    except Exception as e:
        logger.error(f"promo error: {e}")
        await message.answer(f"❌ Ошибка: {e}")

# ==================== CALLBACK ОБРАБОТЧИКИ ====================
@router.callback_query(F.data == "back_to_menu")
async def callback_back_to_menu(query: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await query.message.edit_text(
            "🎉 GeoShop\n\nВыберите действие:",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"back_to_menu error: {e}")
    await query.answer()

@router.callback_query(F.data == "catalog")
async def callback_catalog(query: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await query.message.edit_text(
            "<b>🏪 Каталог товаров</b>\n\nВыберите номер для подробной информации:",
            parse_mode="HTML",
            reply_markup=catalog_keyboard()
        )
    except Exception as e:
        logger.error(f"catalog error: {e}")
    await query.answer()

@router.callback_query(F.data.startswith("product_"))
async def callback_product_details(query: CallbackQuery):
    product_code = query.data.replace("product_", "")
    if product_code not in db.products:
        await query.answer("❌ Товар не найден", show_alert=True)
        return
    
    product = db.products[product_code]
    text = (
        f"<b>{product.name}</b>\n\n"
        f"💰 Цена: {product.price} RUB\n"
        f"📦 В наличии: {product.stock} шт\n\n"
        f"Высокая скорость доставки номеров после оплаты."
    )
    
    try:
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=product_details_keyboard(product_code)
        )
    except Exception as e:
        logger.error(f"product_details error: {e}")
    await query.answer()

@router.callback_query(F.data.startswith("add_cart_"))
async def callback_add_to_cart(query: CallbackQuery):
    product_code = query.data.replace("add_cart_", "")
    user_id = query.from_user.id
    
    if product_code not in db.products:
        await query.answer("❌ Товар не найден", show_alert=True)
        return
    
    if db.products[product_code].stock == 0:
        await query.answer("❌ Товар закончился", show_alert=True)
        return
    
    if user_id not in db.cart:
        db.cart[user_id] = {}
    
    if product_code in db.cart[user_id]:
        db.cart[user_id][product_code] += 1
    else:
        db.cart[user_id][product_code] = 1
    
    await query.answer(f"✅ Добавлено в корзину!")
    
    product = db.products[product_code]
    try:
        await query.message.edit_text(
            f"<b>{product.name}</b>\n\n"
            f"💰 Цена: {product.price} RUB\n"
            f"📦 В наличии: {product.stock} шт\n\n"
            f"✅ Добавлено в корзину!",
            parse_mode="HTML",
            reply_markup=product_details_keyboard(product_code)
        )
    except Exception as e:
        logger.error(f"add_to_cart error: {e}")

@router.callback_query(F.data == "cart")
async def callback_cart(query: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = query.from_user.id
    
    if user_id not in db.cart or not db.cart[user_id]:
        try:
            await query.message.edit_text(
                "🛒 <b>Корзина пуста</b>",
                parse_mode="HTML",
                reply_markup=cart_keyboard(user_id)
            )
        except Exception as e:
            logger.error(f"empty_cart error: {e}")
        await query.answer()
        return
    
    cart_items = db.cart[user_id]
    text = "<b>🛒 Ваша корзина</b>\n\n"
    
    for product_code, quantity in cart_items.items():
        if product_code in db.products:
            product = db.products[product_code]
            total = product.price * quantity
            text += f"{product.name}\n"
            text += f"  Кол-во: {quantity} x {product.price}₽ = {total}₽\n\n"
    
    total_sum = db.get_cart_total(user_id)
    text += f"<b>Итого: {total_sum}₽</b>\n\n"
    text += "💡 Используйте /promo КОД для применения промокода"
    
    try:
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=cart_keyboard(user_id)
        )
    except Exception as e:
        logger.error(f"cart error: {e}")
    await query.answer()

@router.callback_query(F.data == "clear_cart")
async def callback_clear_cart(query: CallbackQuery):
    user_id = query.from_user.id
    if user_id in db.cart:
        db.cart[user_id].clear()
    
    await query.answer("✅ Корзина очищена")
    await callback_cart(query, FSMContext(MemoryStorage(), None, None))

@router.callback_query(F.data == "checkout")
async def callback_checkout(query: CallbackQuery):
    user_id = query.from_user.id
    
    if user_id not in db.cart or not db.cart[user_id]:
        await query.answer("❌ Корзина пуста", show_alert=True)
        return
    
    total_amount = db.get_cart_total(user_id)
    
    # Создаём заказ
    order_id = f"{user_id}_{int(datetime.now().timestamp())}"
    order = Order(
        order_id=order_id,
        user_id=user_id,
        product_code="",
        quantity=len(db.cart[user_id]),
        total_amount=total_amount,
        promo_code=None,
        status="pending",
        created_at=datetime.now().isoformat()
    )
    db.orders.append(order)
    
    # Создаём инвойс в CryptoBot
    invoice_id = await create_invoice(user_id, total_amount, order_id)
    
    if not invoice_id:
        await query.answer("❌ Ошибка создания платежа. Попробуйте позже.", show_alert=True)
        db.orders.remove(order)
        return
    
    order.invoice_id = invoice_id
    invoice_link = f"https://pay.crypt.bot/invoice/{invoice_id}"
    
    try:
        await query.message.edit_text(
            f"<b>💳 Оформление заказа</b>\n\n"
            f"Сумма: {total_amount} RUB\n"
            f"Заказ: #{order_id}\n\n"
            f"<a href='{invoice_link}'>👉 Перейти к оплате</a>\n\n"
            f"Ожидаем оплату (10 минут)...",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"checkout error: {e}")
    
    await query.answer()
    
    # Запускаем фоновую задачу проверки оплаты
    asyncio.create_task(wait_for_payment(query.bot, user_id, order_id, invoice_id))

# ==================== АДМИН-ПАНЕЛЬ ====================
@router.callback_query(F.data == "admin_create_promo")
async def callback_admin_create_promo(query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.create_promo_code)
    try:
        await query.message.edit_text(
            "Введите код промокода:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_back")]
            ])
        )
    except Exception as e:
        logger.error(f"create_promo error: {e}")
    await query.answer()

@router.message(AdminStates.create_promo_code)
async def process_promo_code_input(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    code = message.text.upper()
    
    if code in db.promo_codes:
        await message.answer("❌ Промокод уже существует")
        return
    
    await state.update_data(promo_code=code)
    await state.set_state(AdminStates.create_promo_code_discount)
    await message.answer(
        f"✅ Код: {code}\n\nВведите процент скидки (1-99):"
    )

@router.message(AdminStates.create_promo_code_discount)
async def process_promo_discount_input(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        discount = int(message.text)
        if not (1 <= discount <= 99):
            await message.answer("❌ Скидка должна быть от 1 до 99%")
            return
        
        await state.update_data(discount=discount)
        await state.set_state(AdminStates.create_promo_code_limit)
        await message.answer(
            f"✅ Скидка: {discount}%\n\nВведите лимит использований:"
        )
    except:
        await message.answer("❌ Введите число")

@router.message(AdminStates.create_promo_code_limit)
async def process_promo_limit_input(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        limit = int(message.text)
        if limit < 1:
            await message.answer("❌ Лимит должен быть >= 1")
            return
        
        data = await state.get_data()
        code = data.get("promo_code")
        discount = data.get("discount")
        
        db.promo_codes[code] = PromoCode(code, discount, limit)
        
        await state.set_state(AdminStates.main_menu)
        await message.answer(
            f"✅ Промокод создан!\n"
            f"Код: {code}\n"
            f"Скидка: {discount}%\n"
            f"Лимит: {limit}",
            reply_markup=admin_menu_keyboard()
        )
    except:
        await message.answer("❌ Введите число")

@router.callback_query(F.data == "admin_list_promo")
async def callback_admin_list_promo(query: CallbackQuery):
    if not db.promo_codes:
        try:
            await query.message.edit_text(
                "📋 <b>Промокоды</b>\n\nНет активных промокодов",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
                ])
            )
        except Exception as e:
            logger.error(f"list_promo error: {e}")
        await query.answer()
        return
    
    text = "<b>📋 Промокоды</b>\n\n"
    buttons = []
    
    for code, promo in db.promo_codes.items():
        status = "✅ Активен" if promo.is_active else "❌ Исчерпан"
        text += f"<code>{code}</code> | {promo.discount_percent}% | {promo.used_count}/{promo.usage_limit} {status}\n"
        buttons.append([InlineKeyboardButton(text=f"🗑️ {code}", callback_data=f"admin_delete_promo_{code}")])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    
    try:
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    except Exception as e:
        logger.error(f"list_promo edit error: {e}")
    await query.answer()

@router.callback_query(F.data.startswith("admin_delete_promo_"))
async def callback_delete_promo(query: CallbackQuery):
    code = query.data.replace("admin_delete_promo_", "")
    if code in db.promo_codes:
        del db.promo_codes[code]
        await query.answer(f"✅ Промокод {code} удален")
    else:
        await query.answer(f"❌ Промокод не найден", show_alert=True)
    
    await callback_admin_list_promo(query)

@router.callback_query(F.data == "admin_manage_stock")
async def callback_admin_manage_stock(query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.manage_stock)
    try:
        await query.message.edit_text(
            "📦 <b>Управление стоком</b>\n\nВыберите товар:",
            parse_mode="HTML",
            reply_markup=products_select_keyboard(for_stock=True)
        )
    except Exception as e:
        logger.error(f"manage_stock error: {e}")
    await query.answer()

@router.callback_query(F.data.startswith("admin_stock_"))
async def callback_select_stock_product(query: CallbackQuery, state: FSMContext):
    product_code = query.data.replace("admin_stock_", "")
    await state.update_data(selected_product=product_code)
    await state.set_state(AdminStates.manage_stock_quantity)
    
    product = db.products[product_code]
    try:
        await query.message.edit_text(
            f"Выбран: <b>{product.name}</b>\n"
            f"Текущий сток: {product.stock}\n\n"
            f"Введите новое количество номеров:",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"select_stock_product error: {e}")
    await query.answer()

@router.message(AdminStates.manage_stock_quantity)
async def process_stock_quantity(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        quantity = int(message.text)
        data = await state.get_data()
        product_code = data.get("selected_product")
        product = db.products[product_code]
        
        if quantity < len(product.numbers):
            product.numbers = product.numbers[:quantity]
        
        await state.set_state(AdminStates.main_menu)
        await message.answer(
            f"✅ Сток обновлён для '{product.name}'\n"
            f"Текущее количество номеров: {len(product.numbers)}",
            reply_markup=admin_menu_keyboard()
        )
    except:
        await message.answer("❌ Введите число")

@router.callback_query(F.data == "admin_add_numbers")
async def callback_admin_add_numbers(query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_numbers)
    try:
        await query.message.edit_text(
            "➕ <b>Пополнение номеров</b>\n\nВыберите товар:",
            parse_mode="HTML",
            reply_markup=products_select_keyboard(for_stock=False)
        )
    except Exception as e:
        logger.error(f"add_numbers error: {e}")
    await query.answer()

@router.callback_query(F.data.startswith("admin_addnum_"))
async def callback_select_addnum_product(query: CallbackQuery, state: FSMContext):
    product_code = query.data.replace("admin_addnum_", "")
    await state.update_data(selected_product=product_code)
    await state.set_state(AdminStates.add_numbers_input)
    
    product = db.products[product_code]
    try:
        await query.message.edit_text(
            f"Выбран: <b>{product.name}</b>\n"
            f"Текущий сток: {product.stock}\n\n"
            f"Введите номера через запятую:",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"select_addnum_product error: {e}")
    await query.answer()

@router.message(AdminStates.add_numbers_input)
async def process_add_numbers(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    data = await state.get_data()
    product_code = data.get("selected_product")
    
    numbers = [n.strip() for n in message.text.split(",") if n.strip()]
    db.products[product_code].numbers.extend(numbers)
    
    await state.set_state(AdminStates.main_menu)
    product = db.products[product_code]
    await message.answer(
        f"✅ Добавлено {len(numbers)} номеров\n"
        f"{product.name}: {product.stock} номеров в наличии",
        reply_markup=admin_menu_keyboard()
    )

@router.callback_query(F.data == "admin_send_message")
async def callback_admin_send_message(query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.send_message)
    try:
        await query.message.edit_text(
            "📢 <b>Рассылка сообщений</b>\n\nВведите текст рассылки (поддерживается HTML):",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"send_message error: {e}")
    await query.answer()

@router.message(AdminStates.send_message)
async def process_send_message_text(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    text = message.text
    await state.update_data(send_text=text)
    await state.set_state(AdminStates.send_message_confirm)
    
    await message.answer(
        f"<b>Подтверждение рассылки</b>\n\n{text}\n\n"
        f"Отправить {len(db.active_users)} пользователям?",
        parse_mode="HTML",
        reply_markup=confirm_keyboard()
    )

@router.callback_query(F.data == "confirm_yes")
async def callback_confirm_send(query: CallbackQuery, state: FSMContext):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    data = await state.get_data()
    text = data.get("send_text", "")
    
    count = 0
    failed = 0
    for user_id in db.active_users:
        try:
            await query.bot.send_message(user_id, text, parse_mode="HTML")
            count += 1
        except Exception as e:
            logger.error(f"Message send error to {user_id}: {e}")
            failed += 1
    
    await state.set_state(AdminStates.main_menu)
    try:
        await query.message.edit_text(
            f"✅ Рассылка завершена!\n\n"
            f"Отправлено: {count}\n"
            f"Ошибок: {failed}",
            reply_markup=admin_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"confirm_send error: {e}")
    await query.answer()

@router.callback_query(F.data == "confirm_no")
async def callback_cancel_send(query: CallbackQuery, state: FSMContext):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    await state.set_state(AdminStates.main_menu)
    try:
        await query.message.edit_text(
            "❌ Рассылка отменена",
            reply_markup=admin_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"cancel_send error: {e}")
    await query.answer()

@router.callback_query(F.data == "admin_orders")
async def callback_admin_orders(query: CallbackQuery):
    if not db.orders:
        try:
            await query.message.edit_text(
                "📊 <b>Заказы</b>\n\nНет заказов",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
                ])
            )
        except Exception as e:
            logger.error(f"orders error: {e}")
        await query.answer()
        return
    
    text = "<b>📊 Последние 10 заказов</b>\n\n"
    
    for order in db.orders[-10:]:
        text += f"<code>{order.order_id}</code>\n"
        text += f"👤 User: {order.user_id}\n"
        text += f"💰 Сумма: {order.total_amount} RUB\n"
        text += f"📌 Статус: {order.status}\n\n"
    
    try:
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
            ])
        )
    except Exception as e:
        logger.error(f"orders edit error: {e}")
    await query.answer()

@router.callback_query(F.data == "admin_backup")
async def callback_admin_backup(query: CallbackQuery):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    try:
        backup_data = db.save_backup()
        
        with open("/tmp/backup_geoshop.json", "w") as f:
            f.write(backup_data)
        
        await query.message.answer_document(
            InputFile("/tmp/backup_geoshop.json"),
            caption="💾 Резервная копия данных GeoShop"
        )
        
        await query.answer("✅ Резервная копия создана")
    except Exception as e:
        logger.error(f"backup error: {e}")
        await query.answer(f"❌ Ошибка: {e}", show_alert=True)

@router.callback_query(F.data == "admin_back")
async def callback_admin_back(query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.main_menu)
    try:
        await query.message.edit_text(
            "<b>👨‍💼 Админ-панель GeoShop</b>",
            parse_mode="HTML",
            reply_markup=admin_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"admin_back error: {e}")
    await query.answer()

@router.callback_query(F.data == "admin_exit")
async def callback_admin_exit(query: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await query.message.edit_text(
            "👋 Вы вышли из админ-панели",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"admin_exit error: {e}")
    await query.answer()

# ==================== ОСНОВНАЯ ФУНКЦИЯ ====================
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    
    dp.include_router(router)
    
    logger.info("🚀 Бот GeoShop запущен")
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logger.error(f"Bot error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
