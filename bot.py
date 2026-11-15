import asyncio
from collections import defaultdict
import hashlib
import hmac
import json
import math
import secrets
import time
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
WEBAPP_URL = "https://rubpro-borohezh.mia0.amvera.tech"
ADMIN_USERNAME = "rubupgift"
STARS_PER_GRAM = 100.0  # 100 звезд = 1 GRAM

NFT_CATALOG = [
    {"id": "heart_box", "name": "Сердце", "price": 0.15, "icon": "💝", "default_stock": True},
    {"id": "teddy_bear", "name": "Мишка", "price": 0.15, "icon": "🧸", "default_stock": True},
    {"id": "gift_box", "name": "Коробка", "price": 0.25, "icon": "🎁", "default_stock": True},
    {"id": "red_rose", "name": "Роза", "price": 0.25, "icon": "🌹", "default_stock": True},
    {"id": "cake_50", "name": "Торт", "price": 0.50, "icon": "🎂", "default_stock": True},
    {"id": "rocket_50", "name": "Ракета", "price": 0.50, "icon": "🚀", "default_stock": True},
    {"id": "trophy_100", "name": "Кубок", "price": 1.00, "icon": "🏆", "default_stock": True},
    {"id": "diamond_100", "name": "Алмаз", "price": 1.00, "icon": "💎", "default_stock": True},
    {"id": "ring_100", "name": "Кольцо", "price": 1.00, "icon": "💍", "default_stock": True},
    {"id": "voodoo_doll", "name": "Кукла Вуду", "price": 2.50, "icon": "🪆", "default_stock": True},
    {"id": "crystal_ball", "name": "Магический шар", "price": 3.50, "icon": "🔮", "default_stock": True},
    {"id": "love_potion", "name": "Зелье любви", "price": 4.00, "icon": "🧪", "default_stock": False},
    {"id": "skull_flower", "name": "Цветок-череп", "price": 5.00, "icon": "🌺", "default_stock": True},
    {"id": "vintage_cigar", "name": "Сигара", "price": 7.50, "icon": "🚬", "default_stock": False},
    {"id": "astral_shard", "name": "Астральный осколок", "price": 10.00, "icon": "💠", "default_stock": True},
    {"id": "neko_helmet", "name": "Шлем Неко", "price": 15.00, "icon": "🪖", "default_stock": False},
    {"id": "sharp_tongue", "name": "Губы (Sharp Tongue)", "price": 20.00, "icon": "👄", "default_stock": False},
    {"id": "black_cat", "name": "Чёрный кот", "price": 25.00, "icon": "🐈‍⬛", "default_stock": False},
    {"id": "ion_gem", "name": "Перстень с сапфиром", "price": 35.00, "icon": "💍", "default_stock": False},
    {"id": "birkin_bag", "name": "Сумка (Loot Bag)", "price": 50.00, "icon": "👜", "default_stock": False},
]

session = AiohttpSession(
    api=TelegramAPIServer.from_base(OCEANGRAM_API)
)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEBAPP_URL],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
ip_requests: dict[str, list[float]] = defaultdict(list)
last_spin_ts: dict[int, float] = {}
last_promo_ts: dict[int, float] = {}

def check_rate_limit(key: str, max_calls: int = 20, window: float = 10.0):
    now = time.monotonic()
    history = [t for t in ip_requests[key] if now - t < window]
    if len(history) >= max_calls:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Подожди немного.")
    history.append(now)
    ip_requests[key] = history

class AdminStates(StatesGroup):
    waiting_chance = State()
    waiting_broadcast = State()
    waiting_promo_data = State()

def is_admin(user: types.User) -> bool:
    return bool(user and user.username and user.username.lower() == ADMIN_USERNAME.lower())

async def init_db():
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users_gram (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0,
                wagered REAL DEFAULT 0.0,
                refs INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                nft_id TEXT
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nft_stock (
                nft_id TEXT PRIMARY KEY,
                in_stock INTEGER
            )
        """)
        await db.execute("INSERT OR IGNORE INTO settings (key, val) VALUES ('win_chance', 'mode_1')")
        await db.execute("INSERT OR IGNORE INTO settings (key, val) VALUES ('spin_count', '0')")
        for item in NFT_CATALOG:
            await db.execute(
                "INSERT OR IGNORE INTO nft_stock (nft_id, in_stock) VALUES (?, ?)",
                (item["id"], 1 if item["default_stock"] else 0)
            )
        await db.commit()

async def get_nft_list():
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT nft_id, in_stock FROM nft_stock") as cursor:
            stock_map = dict(await cursor.fetchall())
    result = []
    for item in NFT_CATALOG:
        result.append({
            "id": item["id"],
            "name": item["name"],
            "price": item["price"],
            "icon": item["icon"],
            "in_stock": bool(stock_map.get(item["id"], 0))
        })
    return result

async def get_user_inventory(user_id: int):
    nfts = {n["id"]: n for n in await get_nft_list()}
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT id, nft_id FROM inventory WHERE user_id = ? ORDER BY id DESC", (user_id,)) as cursor:
            rows = await cursor.fetchall()
    inv = []
    for row_id, nft_id in rows:
        if nft_id in nfts:
            item = dict(nfts[nft_id])
            item["inv_id"] = row_id
            inv.append(item)
    return inv

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
            return row[0] if row else "mode_1"

async def set_custom_chance(val: str):
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, val) VALUES ('win_chance', ?)", (val,))
        await db.commit()

async def get_user_data(user_id: int) -> tuple[float, float, int]:
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("INSERT OR IGNORE INTO users_gram (user_id, balance, wagered, refs) VALUES (?, 0.0, 0.0, 0)", (user_id,))
        await db.commit()
        async with db.execute("SELECT balance, wagered, refs FROM users_gram WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return round(row[0], 2), round(row[1], 2), int(row[2])
            return 0.0, 0.0, 0

async def update_balance(user_id: int, delta: float, add_wager: float = 0.0) -> tuple[float, float]:
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("INSERT OR IGNORE INTO users_gram (user_id, balance, wagered, refs) VALUES (?, 0.0, 0.0, 0)", (user_id,))
        if delta < 0:
            cursor = await db.execute(
                "UPDATE users_gram SET balance = round(balance + ?, 2), wagered = round(wagered + ?, 2) WHERE user_id = ? AND balance >= ?",
                (delta, add_wager, user_id, abs(delta))
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=400, detail="Недостаточно GRAM на балансе")
        else:
            await db.execute(
                "UPDATE users_gram SET balance = round(balance + ?, 2), wagered = round(wagered + ?, 2) WHERE user_id = ?",
                (delta, add_wager, user_id)
            )
        await db.commit()
    bal, wag, _ = await get_user_data(user_id)
    return bal, wag

def verify_telegram_webapp(init_data: str) -> dict:
    if not init_data or len(init_data) > 4096:
        raise HTTPException(status_code=401, detail="Ошибка авторизации")
    parsed = dict(parse_qsl(init_data))
    if "hash" not in parsed or "user" not in parsed:
        raise HTTPException(status_code=401, detail="Ошибка авторизации")
    received_hash = parsed.pop("hash")
    auth_date = int(parsed.get("auth_date", 0))
    if auth_date and (time.time() - auth_date > 86400):
        raise HTTPException(status_code=401, detail="Сессия устарела, перезапусти приложение")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=403, detail="Ошибка проверки подписи")
    return json.loads(parsed["user"])

def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Режим работы", callback_data="adm_chances")],
        [InlineKeyboardButton(text="🖼 Наличие NFT", callback_data="adm_nft")],
        [InlineKeyboardButton(text="🎁 Создать промокод", callback_data="adm_promo_new")],
        [InlineKeyboardButton(text="📋 Активные промокоды", callback_data="adm_promo_list")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")]
    ])

def admin_chances_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Режим 1 (Стандарт)", callback_data="set_ch_mode_1")],
        [InlineKeyboardButton(text="Режим 2 (0%)", callback_data="set_ch_0")],
        [InlineKeyboardButton(text="Режим 3 (85%)", callback_data="set_ch_85")],
        [InlineKeyboardButton(text="Режим 4 (Auto)", callback_data="set_ch_auto")],
        [InlineKeyboardButton(text="✏️ Свой %", callback_data="set_ch_custom")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]
    ])

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT 1 FROM users_gram WHERE user_id = ?", (user_id,)) as cursor:
            exists = await cursor.fetchone()
        if not exists:
            await db.execute("INSERT INTO users_gram (user_id, balance, wagered, refs) VALUES (?, 0.0, 0.0, 0)", (user_id,))
            parts = (message.text or "").split()
            if len(parts) > 1 and parts[1].startswith("ref_"):
                try:
                    ref_id = int(parts[1].replace("ref_", ""))
                    if ref_id != user_id:
                        await db.execute("UPDATE users_gram SET refs = refs + 1 WHERE user_id = ?", (ref_id,))
                except ValueError:
                    pass
            await db.commit()

    if is_admin(message.from_user):
        async with aiosqlite.connect("oceangram_rubup.db") as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, val) VALUES ('admin_chat_id', ?)", (str(message.chat.id),))
            await db.commit()
    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(text="Открыть RubUP", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception:
        pass
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть RubUP", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        "Жми кнопку ниже, чтобы открыть <b>RubUP</b>:",
        reply_markup=kb,
        parse_mode="HTML"
    )

async def redeem_promo_code(user_id: int, raw_code: str) -> tuple[bool, str, float, float]:
    code = raw_code.strip().upper()[:32]
    if not code:
        return False, "Введи промокод", 0.0, 0.0

    async with user_locks[user_id]:
        async with aiosqlite.connect("oceangram_rubup.db") as db:
            async with db.execute("SELECT amount, uses_left FROM promos WHERE code = ?", (code,)) as cursor:
                promo = await cursor.fetchone()

            if not promo or promo[1] <= 0:
                return False, "Промокод не найден или закончился", 0.0, 0.0

            async with db.execute("SELECT 1 FROM promo_uses WHERE user_id = ? AND code = ?", (user_id, code)) as cursor:
                already_used = await cursor.fetchone()

            if already_used:
                return False, "Ты уже активировал этот промокод", 0.0, 0.0

            amount, _ = promo
            await db.execute("INSERT INTO promo_uses (user_id, code) VALUES (?, ?)", (user_id, code))
            await db.execute("UPDATE promos SET uses_left = uses_left - 1 WHERE code = ?", (code,))
            await db.commit()

        new_bal, _ = await update_balance(user_id, amount)
        return True, f"Начислено {amount} GRAM", amount, new_bal

@dp.message(Command("promo"))
async def cmd_promo(message: types.Message):
    user_id = message.from_user.id
    now = time.monotonic()
    if now - last_promo_ts.get(user_id, 0) < 3.0:
        return
    last_promo_ts[user_id] = now

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Введи промокод так: <code>/promo КОД</code>", parse_mode="HTML")
        return

    ok, msg, amount, new_bal = await redeem_promo_code(user_id, parts[1])
    if ok:
        await message.answer(f"✅ Промокод активирован! {msg}\nБаланс: <b>{new_bal} GRAM</b>", parse_mode="HTML")
    else:
        await message.answer(f"❌ {msg}")

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, val) VALUES ('admin_chat_id', ?)", (str(message.chat.id),))
        await db.commit()
    await state.clear()
    cur = await get_custom_chance()
    await message.answer(
        f"👑 <b>Панель управления RubUP</b>\n\nАктивный профиль: <b>{cur}</b>",
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
        f"👑 <b>Панель управления RubUP</b>\n\nАктивный профиль: <b>{cur}</b>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_nft")
async def cb_adm_nft(call: types.CallbackQuery):
    if not is_admin(call.from_user):
        return
    nfts = await get_nft_list()
    rows = []
    for n in nfts:
        status = "✅" if n["in_stock"] else "❌"
        rows.append([InlineKeyboardButton(
            text=f"{status} {n['icon']} {n['name']} ({n['price']} GRAM)",
            callback_data=f"tog_nft_{n['id']}"
        )])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])
    await call.message.edit_text(
        "🖼 <b>Наличие NFT</b>\n\nНажми на позицию, чтобы изменить статус:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("tog_nft_"))
async def cb_tog_nft(call: types.CallbackQuery):
    if not is_admin(call.from_user):
        return
    nft_id = call.data.replace("tog_nft_", "")
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("UPDATE nft_stock SET in_stock = 1 - in_stock WHERE nft_id = ?", (nft_id,))
        await db.commit()
    await cb_adm_nft(call)

@dp.callback_query(F.data == "adm_chances")
async def cb_adm_chances(call: types.CallbackQuery):
    if not is_admin(call.from_user):
        return
    cur = await get_custom_chance()
    await call.message.edit_text(
        f"⚙️ <b>Выбор профиля</b>\n\nТекущий: <b>{cur}</b>",
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
            "✏️ Введи значение от <b>0</b> до <b>100</b>:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
            parse_mode="HTML"
        )
        return

    await set_custom_chance(val)
    await call.answer(f"Сохранено: {val}", show_alert=True)
    await call.message.edit_text(
        f"⚙️ <b>Выбор профиля</b>\n\nТекущий: <b>{val}</b>",
        reply_markup=admin_chances_kb(),
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_chance)
async def msg_custom_chance(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    try:
        val = float((message.text or "").replace(",", "."))
        if math.isfinite(val) and 0 <= val <= 100:
            await set_custom_chance(str(val))
            await state.clear()
            await message.answer(
                f"✅ Значение установлено: <b>{val}%</b>.",
                reply_markup=admin_main_kb(),
                parse_mode="HTML"
            )
        else:
            await message.answer("Введи число от 0 до 100:")
    except ValueError:
        await message.answer("Неверный формат. Введи число:")

@dp.callback_query(F.data == "adm_promo_new")
async def cb_promo_new(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        return
    await state.set_state(AdminStates.waiting_promo_data)
    await call.message.edit_text(
        "🎁 <b>Создание промокода (в GRAM)</b>\n\nФормат:\n<code>КОД СУММА_GRAM КОЛ_ВО_АКТИВАЦИЙ</code>\n\nПример:\n<code>GRAM5 5 50</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_promo_data)
async def msg_promo_data(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("❌ Неверный формат. Пример: <code>GRAM5 5 50</code>", parse_mode="HTML")
        return
    try:
        code = parts[0].upper()[:32]
        amount = float(parts[1].replace(",", "."))
        uses = int(parts[2])
        if not math.isfinite(amount) or amount <= 0 or uses <= 0:
            raise ValueError
        async with aiosqlite.connect("oceangram_rubup.db") as db:
            await db.execute("INSERT OR REPLACE INTO promos (code, amount, uses_left) VALUES (?, ?, ?)", (code, amount, uses))
            await db.commit()
        await state.clear()
        await message.answer(
            f"✅ Промокод <b>{code}</b> создан!\nСумма: <b>{amount} GRAM</b>\nАктиваций: <b>{uses}</b>",
            reply_markup=admin_main_kb(),
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Сумма и количество активаций должны быть больше 0.")

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
        lines = [f"• <code>{r[0]}</code> — <b>{r[1]} GRAM</b> (осталось: {r[2]})" for r in rows]
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
        "📢 <b>Рассылка</b>\n\nОтправь сообщение для рассылки всем пользователям:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_broadcast)
async def msg_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT user_id FROM users_gram") as cursor:
            users = await cursor.fetchall()

    sent = 0
    failed = 0
    status_msg = await message.answer(f"⏳ Отправка ({len(users)} чел.)...")

    for (uid,) in users:
        try:
            await message.copy_to(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\nДоставлено: <b>{sent}</b>\nОшибок: <b>{failed}</b>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_stats")
async def cb_stats(call: types.CallbackQuery):
    if not is_admin(call.from_user):
        return
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(balance), 0), COALESCE(SUM(wagered), 0) FROM users_gram") as cursor:
            total_users, total_bal, total_wag = await cursor.fetchone()
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"Игроков: <b>{total_users}</b>\n"
        f"Общий баланс: <b>{round(total_bal, 2)} GRAM</b>\n"
        f"Общий оборот: <b>{round(total_wag, 2)} GRAM</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]]),
        parse_mode="HTML"
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: types.PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    stars_paid = message.successful_payment.total_amount
    gram_to_add = round(stars_paid / STARS_PER_GRAM, 2)
    async with user_locks[message.from_user.id]:
        new_bal, _ = await update_balance(message.from_user.id, gram_to_add)
    await message.answer(f"✅ Оплата {stars_paid} ⭐️ прошла!\nНачислено: <b>{gram_to_add} GRAM</b>\nБаланс: <b>{new_bal} GRAM</b>", parse_mode="HTML")

@app.get("/", response_class=HTMLResponse)
async def serve_webapp(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"web_{client_ip}", max_calls=30, window=10.0)
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/user")
async def api_user(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"api_{client_ip}", max_calls=25, window=10.0)
    data = await request.json()
    user_obj = verify_telegram_webapp(data.get("initData", ""))
    user_id = int(user_obj["id"])
    bal, wagered, refs = await get_user_data(user_id)
    nfts = await get_nft_list()
    inv = await get_user_inventory(user_id)
    bot_me = await bot.get_me()
    return {
        "user_id": user_id,
        "balance": bal,
        "wagered": wagered,
        "refs": refs,
        "nfts": nfts,
        "inventory": inv,
        "admin_username": ADMIN_USERNAME,
        "bot_username": bot_me.username
    }

@app.post("/api/promo")
async def api_promo(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"prm_{client_ip}", max_calls=6, window=10.0)
    data = await request.json()
    user_obj = verify_telegram_webapp(data.get("initData", ""))
    user_id = int(user_obj["id"])
    code = str(data.get("code", ""))
    ok, msg, amount, new_bal = await redeem_promo_code(user_id, code)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg, "balance": new_bal}

@app.post("/api/withdraw_nft")
async def api_withdraw_nft(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"wd_{client_ip}", max_calls=6, window=10.0)
    data = await request.json()
    user_obj = verify_telegram_webapp(data.get("initData", ""))
    user_id = int(user_obj["id"])
    username = user_obj.get("username", "без_юза")
    nft_id = str(data.get("nft_id", ""))[:40]
    inv_id = data.get("inv_id")

    async with user_locks[user_id]:
        nfts = await get_nft_list()
        target = next((n for n in nfts if n["id"] == nft_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Позиция не найдена")
        if not target["in_stock"]:
            raise HTTPException(status_code=400, detail="Нет в наличии")

        if inv_id is not None:
            async with aiosqlite.connect("oceangram_rubup.db") as db:
                cursor = await db.execute("DELETE FROM inventory WHERE id = ? AND user_id = ?", (int(inv_id), user_id))
                if cursor.rowcount == 0:
                    raise HTTPException(status_code=400, detail="Предмет не найден в инвентаре")
                await db.commit()
            new_bal, _, _ = await get_user_data(user_id)
        else:
            current_bal, _, _ = await get_user_data(user_id)
            if current_bal < target["price"]:
                raise HTTPException(status_code=400, detail="Недостаточно GRAM")
            new_bal, _ = await update_balance(user_id, -target["price"])

    async with aiosqlite.connect("oceangram_rubup.db") as db:
        async with db.execute("SELECT val FROM settings WHERE key = 'admin_chat_id'") as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    await bot.send_message(
                        chat_id=int(row[0]),
                        text=(
                            f"🎁 <b>Новая заявка на вывод!</b>\n\n"
                            f"Игрок: @{username} (ID: <code>{user_id}</code>)\n"
                            f"Подарок: {target['icon']} <b>{target['name']}</b>\n"
                            f"Ценность: <b>{target['price']} GRAM</b>\n"
                            f"Баланс игрока: <b>{new_bal} GRAM</b>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

    inv = await get_user_inventory(user_id)
    return {
        "ok": True,
        "balance": new_bal,
        "inventory": inv,
        "nft_name": target["name"],
        "nft_icon": target["icon"],
        "price": target["price"],
        "admin_username": ADMIN_USERNAME
    }

@app.post("/api/create_invoice")
async def api_create_invoice(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"inv_{client_ip}", max_calls=8, window=10.0)
    data = await request.json()
    verify_telegram_webapp(data.get("initData", ""))
    try:
        stars = int(data.get("stars", 100))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Некорректная сумма")
    if stars < 1 or stars > 100000:
        raise HTTPException(status_code=400, detail="Некорректная сумма")
    
    gram_amount = round(stars / STARS_PER_GRAM, 2)
    invoice_link = await bot.create_invoice_link(
        title=f"Пополнение на {gram_amount} GRAM",
        description=f"Зачисление {gram_amount} GRAM на баланс RubUP",
        payload=f"topup_{stars}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{gram_amount} GRAM", amount=stars)]
    )
    return {"invoice_link": invoice_link}

@app.post("/api/upgrade")
async def api_upgrade(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"upg_{client_ip}", max_calls=12, window=10.0)
    data = await request.json()
    user_obj = verify_telegram_webapp(data.get("initData", ""))
    user_id = int(user_obj["id"])

    now = time.monotonic()
    if now - last_spin_ts.get(user_id, 0) < 1.2:
        raise HTTPException(status_code=429, detail="Подожди секунду перед следующим прокрутом")
    last_spin_ts[user_id] = now

    try:
        bet = round(float(data.get("bet", 0)), 2)
        multiplier = round(float(data.get("multiplier", 2.0)), 2)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Некорректные данные")

    target_nft_id = data.get("target_nft_id")

    if not math.isfinite(bet) or not math.isfinite(multiplier) or bet < 0.05 or bet > 1000000 or multiplier < 1.1 or multiplier > 20:
        raise HTTPException(status_code=400, detail="Некорректная ставка или множитель")

    async with user_locks[user_id]:
        current_bal, _, _ = await get_user_data(user_id)
        if bet > current_bal:
            raise HTTPException(status_code=400, detail="Недостаточно GRAM на балансе")

        standard_chance = (100.0 / multiplier) * 0.95
        mode = await get_custom_chance()

        if mode == "mode_1":
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
            if target_nft_id:
                new_bal, new_wag = await update_balance(user_id, -bet, add_wager=bet)
                async with aiosqlite.connect("oceangram_rubup.db") as db:
                    await db.execute("INSERT INTO inventory (user_id, nft_id) VALUES (?, ?)", (user_id, str(target_nft_id)))
                    await db.commit()
                inv = await get_user_inventory(user_id)
                return {"win": True, "balance": new_bal, "wagered": new_wag, "win_amount": win_amount, "inventory": inv}
            else:
                profit = round(win_amount - bet, 2)
                new_bal, new_wag = await update_balance(user_id, profit, add_wager=bet)
                return {"win": True, "balance": new_bal, "wagered": new_wag, "win_amount": win_amount}
        else:
            new_bal, new_wag = await update_balance(user_id, -bet, add_wager=bet)
            return {"win": False, "balance": new_bal, "wagered": new_wag, "win_amount": 0}

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
