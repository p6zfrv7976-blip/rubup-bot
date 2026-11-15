import asyncio
import hashlib
import hmac
import json
import secrets
from urllib.parse import parse_qsl

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, MenuButtonWebApp, WebAppInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

BOT_TOKEN = "1780246830:TqjCrlBhmwucsYl-bfkJmY5bUr5r52bdOlD"
OCEANGRAM_API = "https://oceanment.2-26-197-16.sslip.io"
WEBAPP_URL = "https://твой-домен.amvera.io"
ADMIN_USERNAME = "rubupgift"
STARS_TO_RUB_RATE = 1.5

session = AiohttpSession(
    api=TelegramAPIServer.from_base(OCEANGRAM_API)
)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class AdminStates(StatesGroup):
    waiting_chance = State()
    waiting_broadcast = State()
    waiting_promo_data = State()

def is_admin(user: types.User) -> bool:
    return (user.username or "").lower() == ADMIN_USERNAME.lower()

async def init_db():
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                val TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                amount REAL,
                uses_left INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_uses (
                user_id INTEGER,
                code TEXT,
                PRIMARY KEY (user_id, code)
            )
        """)
        await db.execute("INSERT OR IGNORE INTO settings (key, val) VALUES ('win_chance', 'cycle')")
        await db.execute("INSERT OR IGNORE INTO settings (key, val) VALUES ('spin_count', '0')")
        await db.commit()

async def next_spin_number() -> int:
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("UPDATE settings SET val = CAST(CAST(val AS INTEGER) + 1 AS TEXT) WHERE key = 'spin_count'")
        await db.commit()
        async with db.execute("SELECT val FROM settings WHERE key = 'spin_count'") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 1

async def get_custom_chance() -> str:
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT val FROM settings WHERE key = 'win_chance'") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else "cycle"

async def set_custom_chance(val: str):
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, val) VALUES ('win_chance', ?)", (val,))
        await db.commit()

async def get_balance(user_id: int) -> float:
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0.0)", (user_id,))
        await db.commit()
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return round(row[0], 2) if row else 0.0

async def update_balance(user_id: int, delta: float) -> float:
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0.0)", (user_id,))
        await db.execute("UPDATE users SET balance = round(balance + ?, 2) WHERE user_id = ?", (delta, user_id))
        await db.commit()
    return await get_balance(user_id)

def verify_telegram_webapp(init_data: str) -> int:
    parsed = dict(parse_qsl(init_data))
    if "hash" not in parsed:
        raise HTTPException(status_code=401, detail="Нет подписи initData")
    received_hash = parsed.pop("hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if calculated_hash != received_hash:
        raise HTTPException(status_code=403, detail="Неверная подпись")
    user_data = json.loads(parsed["user"])
    return int(user_data["id"])

def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Подкрутка шансов", callback_data="adm_chances")],
        [InlineKeyboardButton(text="🎁 Создать промокод", callback_data="adm_promo_new")],
        [InlineKeyboardButton(text="📋 Активные промокоды", callback_data="adm_promo_list")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")]
    ])

def admin_chances_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Цикл (2 слив 5% / 3 норм)", callback_data="set_ch_cycle")],
        [InlineKeyboardButton(text="💀 Полный слив (0%)", callback_data="set_ch_0")],
        [InlineKeyboardButton(text="🔥 Подкрутка (85%)", callback_data="set_ch_85")],
        [InlineKeyboardButton(text="⚖️ Честный шанс (Auto)", callback_data="set_ch_auto")],
        [InlineKeyboardButton(text="✏️ Ввести свой %", callback_data="set_ch_custom")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]
    ])

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await get_balance(message.from_user.id)
    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(text="Открыть Руб Ап", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception:
        pass
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Руб Ап", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        "Жми кнопку ниже, чтобы открыть апгрейды:\n\n"
        "💡 Активировать промокод: <code>/promo КОД</code>",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.message(Command("promo"))
async def cmd_promo(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Введи промокод так: <code>/promo КОД</code>", parse_mode="HTML")
        return

    code = parts[1].strip().upper()
    user_id = message.from_user.id

    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT amount, uses_left FROM promos WHERE code = ?", (code,)) as cursor:
            promo = await cursor.fetchone()

        if not promo or promo[1] <= 0:
            await message.answer("❌ Промокод не найден или активации закончились.")
            return

        async with db.execute("SELECT 1 FROM promo_uses WHERE user_id = ? AND code = ?", (user_id, code)) as cursor:
            already_used = await cursor.fetchone()

        if already_used:
            await message.answer("⚠️ Ты уже активировал этот промокод.")
            return

        amount, uses_left = promo
        await db.execute("INSERT INTO promo_uses (user_id, code) VALUES (?, ?)", (user_id, code))
        await db.execute("UPDATE promos SET uses_left = uses_left - 1 WHERE code = ?", (code,))
        await db.commit()

    new_bal = await update_balance(user_id, amount)
    await message.answer(f"✅ Промокод <b>{code}</b> активирован!\nНачислено: <b>{amount} ₽</b>\nБаланс: <b>{new_bal} ₽</b>", parse_mode="HTML")

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    cur = await get_custom_chance()
    await message.answer(
        f"👑 <b>Админ-панель Руб Ап</b>\n\nТекущий режим шансов: <b>{cur}</b>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        return
    await state.clear()
    cur = await get_custom_chance()
    await call.message.edit_text(
        f"👑 <b>Админ-панель Руб Ап</b>\n\nТекущий режим шансов: <b>{cur}</b>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_chances")
async def cb_adm_chances(call: types.CallbackQuery):
    if not is_admin(call.from_user):
        return
    cur = await get_custom_chance()
    await call.message.edit_text(
        f"🎰 <b>Управление шансами</b>\n\nСейчас стоит: <b>{cur}</b>\nВыбери нужный режим:",
        reply_markup=admin_chances_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("set_ch_"))
async def cb_set_chance(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        return
    val = call.data.replace("set_ch_", "")
    if val == "custom":
        await state.set_state(AdminStates.waiting_chance)
        await call.message.edit_text(
            "✏️ Введи число от <b>0</b> до <b>100</b> (процент шанса на выигрыш для всех ставок):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
            parse_mode="HTML"
        )
        return

    await set_custom_chance(val)
    await call.answer(f"Установлено: {val}", show_alert=True)
    await call.message.edit_text(
        f"🎰 <b>Управление шансами</b>\n\nСейчас стоит: <b>{val}</b>\nВыбери нужный режим:",
        reply_markup=admin_chances_kb(),
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_chance)
async def msg_custom_chance(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    try:
        val = float(message.text.replace(",", "."))
        if 0 <= val <= 100:
            await set_custom_chance(str(val))
            await state.clear()
            await message.answer(
                f"✅ Шанс на залет установлен на <b>{val}%</b>.",
                reply_markup=admin_main_kb(),
                parse_mode="HTML"
            )
        else:
            await message.answer("Введи число от 0 до 100:")
    except ValueError:
        await message.answer("Неверный формат. Введи просто число, например 15:")

@dp.callback_query(F.data == "adm_promo_new")
async def cb_promo_new(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        return
    await state.set_state(AdminStates.waiting_promo_data)
    await call.message.edit_text(
        "🎁 <b>Создание промокода</b>\n\nОтправь данные через пробел в формате:\n<code>КОД СУММА КОЛ_ВО_АКТИВАЦИЙ</code>\n\nПример:\n<code>START100 100 50</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_promo_data)
async def msg_promo_data(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❌ Неверный формат. Напиши так: <code>START100 100 50</code>", parse_mode="HTML")
        return
    try:
        code = parts[0].upper()
        amount = float(parts[1].replace(",", "."))
        uses = int(parts[2])
        if amount <= 0 or uses <= 0:
            raise ValueError
        async with aiosqlite.connect("oceangram_rubup.db") as db:
            await db.execute("INSERT OR REPLACE INTO promos (code, amount, uses_left) VALUES (?, ?, ?)", (code, amount, uses))
            await db.commit()
        await state.clear()
        await message.answer(
            f"✅ Промокод <b>{code}</b> создан!\nСумма: <b>{amount} ₽</b>\nАктиваций: <b>{uses}</b>",
            reply_markup=admin_main_kb(),
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Сумма и количество активаций должны быть числами больше 0.")

@dp.callback_query(F.data == "adm_promo_list")
async def cb_promo_list(call: types.CallbackQuery):
    if not is_admin(call.from_user):
        return
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT code, amount, uses_left FROM promos WHERE uses_left > 0") as cursor:
            rows = await cursor.fetchall()

    if not rows:
        text = "📋 Активных промокодов пока нет."
    else:
        lines = [f"• <code>{r[0]}</code> — <b>{r[1]} ₽</b> (осталось: {r[2]})" for r in rows]
        text = "📋 <b>Активные промокоды:</b>\n\n" + "\n".join(lines)

    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_broadcast")
async def cb_broadcast(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        return
    await state.set_state(AdminStates.waiting_broadcast)
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\nОтправь сообщение (текст, картинку или пост), которое нужно разослать всем пользователям бота:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_broadcast)
async def msg_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

    sent = 0
    failed = 0
    status_msg = await message.answer(f"⏳ Начинаю рассылку на {len(users)} чел...")

    for (uid,) in users:
        try:
            await message.copy_to(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\nДоставлено: <b>{sent}</b>\nОшибок/блокировок: <b>{failed}</b>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_stats")
async def cb_stats(call: types.CallbackQuery):
    if not is_admin(call.from_user):
        return
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM users") as cursor:
            total_users, total_bal = await cursor.fetchone()
    await call.message.edit_text(
        f"📊 <b>Статистика проекта</b>\n\n"
        f"Всего игроков: <b>{total_users}</b>\n"
        f"Общий баланс игроков: <b>{round(total_bal, 2)} ₽</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: types.PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    stars_paid = message.successful_payment.total_amount
    rub_to_add = round(stars_paid * STARS_TO_RUB_RATE, 2)
    new_bal = await update_balance(message.from_user.id, rub_to_add)
    await message.answer(f"✅ Оплата {stars_paid} ⭐️ прошла! Начислено {rub_to_add} ₽. Баланс: {new_bal} ₽")

@app.get("/", response_class=HTMLResponse)
async def serve_webapp():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/user")
async def api_user(request: Request):
    data = await request.json()
    user_id = verify_telegram_webapp(data.get("initData", ""))
    bal = await get_balance(user_id)
    return {"user_id": user_id, "balance": bal}

@app.post("/api/create_invoice")
async def api_create_invoice(request: Request):
    data = await request.json()
    verify_telegram_webapp(data.get("initData", ""))
    stars = int(data.get("stars", 10))
    if stars < 1:
        raise HTTPException(status_code=400, detail="Минимум 1 звезда")
    
    rub_amount = round(stars * STARS_TO_RUB_RATE, 2)
    invoice_link = await bot.create_invoice_link(
        title=f"Пополнение на {rub_amount} ₽",
        description=f"Зачисление {rub_amount} ₽ на баланс Руб Ап",
        payload=f"topup_{stars}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{rub_amount} ₽", amount=stars)]
    )
    return {"invoice_link": invoice_link}

@app.post("/api/upgrade")
async def api_upgrade(request: Request):
    data = await request.json()
    user_id = verify_telegram_webapp(data.get("initData", ""))
    bet = float(data.get("bet", 0))
    multiplier = float(data.get("multiplier", 2.0))

    if bet < 1 or multiplier < 1.1 or multiplier > 20:
        raise HTTPException(status_code=400, detail="Некорректная ставка или множитель")

    current_bal = await get_balance(user_id)
    if bet > current_bal:
        raise HTTPException(status_code=400, detail="Недостаточно рублей на балансе")

    standard_chance = (100.0 / multiplier) * 0.95
    mode = await get_custom_chance()

    if mode == "cycle":
        spin_num = await next_spin_number()
        if spin_num % 3 == 0:
            win_chance = standard_chance
        else:
            win_chance = 5.0
    elif mode == "auto":
        win_chance = standard_chance
    else:
        win_chance = float(mode)

    roll = secrets.randbelow(10000) / 100.0

    if win_chance > 0 and roll <= win_chance:
        win_amount = round(bet * multiplier, 2)
        profit = round(win_amount - bet, 2)
        new_bal = await update_balance(user_id, profit)
        return {"win": True, "balance": new_bal, "win_amount": win_amount, "chance": round(standard_chance, 1)}
    else:
        new_bal = await update_balance(user_id, -bet)
        return {"win": False, "balance": new_bal, "win_amount": 0, "chance": round(standard_chance, 1)}

async def main():
    await init_db()
    config = uvicorn.Config(app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)
    await asyncio.gather(
        server.serve(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
