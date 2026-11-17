import asyncio
from collections import defaultdict
import hashlib
import hmac
import json
import math
import os
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN в настройках Amvera!")

DB_DIR = "/data" if os.path.isdir("/data") else "."
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "oceangram_rubup.db")

OCEANGRAM_API = "https://oceanment.2-26-197-16.sslip.io"
WEBAPP_URL = "https://rubup-borohezh.mia0.amvera.tech"
OWNER_USERNAME = "rubupgift"
REQUIRED_CHANNEL = "@zelenskye"
REQUIRED_CHANNEL_URL = "https://t.me/zelenskye"

STARS_PER_GRAM = 100.0
X2_MAX_DEPOSIT_GRAM = 5000.0
MAX_SINGLE_WIN_GRAM = 25000.0

session = AiohttpSession(
    api=TelegramAPIServer.from_base(OCEANGRAM_API)
)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
ip_requests: dict[str, list[float]] = defaultdict(list)
last_action_ts: dict[int, float] = {}
active_mines: dict[int, dict] = {}


def make_uid_sig(uid: int) -> str:
    return hmac.new(BOT_TOKEN.encode(), f"uid:{uid}".encode(), hashlib.sha256).hexdigest()[:16]


def verify_uid_sig(uid: int, sig: str) -> bool:
    if not sig:
        return False
    return hmac.compare_digest(make_uid_sig(uid), str(sig))


def check_rate_limit(key: str, max_calls: int = 60, window: float = 10.0):
    now = time.monotonic()
    history = [t for t in ip_requests[key] if now - t < window]
    if len(history) >= max_calls:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Подожди секунду.")
    history.append(now)
    ip_requests[key] = history


async def check_channel_sub(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        status = getattr(member, "status", "")
        if status in ("member", "administrator", "creator", "restricted"):
            return True
        if status in ("left", "kicked"):
            return False
        return True
    except Exception:
        return True


class AdminStates(StatesGroup):
    waiting_chance = State()
    waiting_user_chance = State()
    waiting_broadcast = State()
    waiting_promo_data = State()
    waiting_balance_edit = State()
    waiting_new_admin = State()


def is_owner(user: types.User) -> bool:
    return bool(user and user.username and user.username.lower() == OWNER_USERNAME.lower())


async def is_admin(user: types.User) -> bool:
    if not user:
        return False
    if is_owner(user):
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user.id,)) as cur:
            if await cur.fetchone():
                return True
        if user.username:
            clean_u = user.username.lstrip("@").lower()
            async with db.execute("SELECT 1 FROM admins WHERE LOWER(REPLACE(username, '@', '')) = ?", (clean_u,)) as cur:
                if await cur.fetchone():
                    await db.execute("UPDATE admins SET user_id = ? WHERE LOWER(REPLACE(username, '@', '')) = ?", (user.id, clean_u))
                    await db.commit()
                    return True
    return False


async def notify_all_admins(text: str):
    chat_ids = set()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT val FROM settings WHERE key = 'admin_chat_id'") as cur:
            row = await cur.fetchone()
            if row and row[0].isdigit():
                chat_ids.add(int(row[0]))
        async with db.execute("SELECT user_id FROM admins WHERE user_id > 0") as cur:
            for (uid,) in await cur.fetchall():
                chat_ids.add(int(uid))
    for cid in chat_ids:
        try:
            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
        except Exception:
            pass


async def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users_gram (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT 'Игрок',
                balance REAL DEFAULT 0.0,
                wagered REAL DEFAULT 0.0,
                x2_dep_used INTEGER DEFAULT 0,
                custom_chance TEXT DEFAULT ''
            )
        """)
        for col_sql in [
            "ALTER TABLE users_gram ADD COLUMN username TEXT DEFAULT ''",
            "ALTER TABLE users_gram ADD COLUMN first_name TEXT DEFAULT 'Игрок'",
            "ALTER TABLE users_gram ADD COLUMN x2_dep_used INTEGER DEFAULT 0",
            "ALTER TABLE users_gram ADD COLUMN custom_chance TEXT DEFAULT ''",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER DEFAULT 0,
                username TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                currency TEXT,
                amount REAL,
                tx_details TEXT,
                status TEXT DEFAULT 'pending',
                created_at INTEGER
            )
        """)
        try:
            await db.execute("ALTER TABLE withdrawals ADD COLUMN status TEXT DEFAULT 'pending'")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                game TEXT,
                bet REAL,
                mult REAL,
                win REAL,
                created_at INTEGER
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
        await db.execute("INSERT OR IGNORE INTO settings (key, val) VALUES ('win_chance', 'mode_1')")
        await db.execute("INSERT OR IGNORE INTO settings (key, val) VALUES ('spin_count', '0')")
        await db.commit()


def format_chance_label(val: str) -> str:
    v = (val or "").strip()
    if not v or v == "default":
        return "Общий режим"
    if v == "mode_1":
        return "Режим 1 (Стандарт)"
    if v == "auto":
        return "Auto (Честный)"
    if v == "0":
        return "0% (Слив)"
    if v == "100":
        return "100% (Победа)"
    return f"{v}%"


async def is_first_two_eligible(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, x2_dep_used FROM users_gram ORDER BY ROWID ASC LIMIT 2") as cur:
            first_two = await cur.fetchall()
        for uid, used in first_two:
            if uid == user_id and int(used or 0) == 0:
                return True
    return False


async def record_game_event(user_id: int, game: str, bet: float, mult: float, win: float):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO game_history (user_id, game, bet, mult, win, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, game, round(bet, 2), round(mult, 2), round(win, 2), now)
        )
        await db.execute("DELETE FROM game_history WHERE id NOT IN (SELECT id FROM game_history ORDER BY id DESC LIMIT 1000)")
        await db.commit()


async def next_spin_number() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET val = CAST(CAST(val AS INTEGER) + 1 AS TEXT) WHERE key = 'spin_count'")
        await db.commit()
        async with db.execute("SELECT val FROM settings WHERE key = 'spin_count'") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 1


async def get_custom_chance() -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT val FROM settings WHERE key = 'win_chance'") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else "mode_1"


async def set_custom_chance(val: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, val) VALUES ('win_chance', ?)", (val,))
        await db.commit()


async def get_user_custom_chance(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT custom_chance FROM users_gram WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return (row[0] or "").strip() if row else ""


async def set_user_custom_chance(user_id: int, val: str):
    clean = "" if val in ("", "default") else val.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users_gram (user_id, balance, wagered, x2_dep_used, custom_chance) VALUES (?, 0.0, 0.0, 0, '')", (user_id,))
        await db.execute("UPDATE users_gram SET custom_chance = ? WHERE user_id = ?", (clean, user_id))
        await db.commit()


async def resolve_win_chance(standard_chance: float, user_id: int = 0) -> float:
    user_mode = await get_user_custom_chance(user_id) if user_id else ""
    mode = user_mode if user_mode else await get_custom_chance()

    if mode == "mode_1":
        spin_num = await next_spin_number()
        return standard_chance if (spin_num % 3 == 0) else 5.0
    elif mode == "auto":
        return standard_chance
    else:
        try:
            val = float(mode)
            return max(0.0, min(100.0, val))
        except ValueError:
            return standard_chance


async def ensure_user(user_id: int, username: str = "", first_name: str = "Игрок") -> dict:
    clean_uname = (username or "").strip().lstrip("@")
    clean_fname = (first_name or "").strip() or "Игрок"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users_gram (user_id, username, first_name, balance, wagered, x2_dep_used, custom_chance) VALUES (?, ?, ?, 0.0, 0.0, 0, '')",
            (user_id, clean_uname, clean_fname)
        )
        if clean_uname:
            await db.execute("UPDATE users_gram SET username = ? WHERE user_id = ?", (clean_uname, user_id))
        if clean_fname and clean_fname != "Игрок":
            await db.execute("UPDATE users_gram SET first_name = ? WHERE user_id = ?", (clean_fname, user_id))
        await db.commit()
        async with db.execute(
            "SELECT balance, wagered, username FROM users_gram WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return {
                "balance": round(row[0], 2),
                "wagered": round(row[1], 2),
                "username": row[2] or ""
            }


async def update_balance(user_id: int, delta: float, add_wager: float = 0.0) -> tuple[float, float]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users_gram (user_id, balance, wagered, x2_dep_used, custom_chance) VALUES (?, 0.0, 0.0, 0, '')", (user_id,))
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
    u = await ensure_user(user_id)
    return u["balance"], u["wagered"]


def extract_webapp_user(data: dict) -> dict:
    init_data = data.get("initData", "")
    if init_data:
        try:
            parsed = dict(parse_qsl(init_data))
            if "hash" in parsed and "user" in parsed:
                received_hash = parsed.pop("hash")
                data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
                secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
                calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
                if hmac.compare_digest(calculated_hash, received_hash):
                    return json.loads(parsed["user"])
        except Exception:
            pass

    fallback_user = data.get("user")
    if isinstance(fallback_user, dict) and str(fallback_user.get("id", "")).isdigit():
        if int(fallback_user["id"]) > 0:
            return fallback_user

    uid_raw = str(data.get("uid", ""))
    sig_raw = str(data.get("sig", ""))
    if uid_raw.isdigit() and int(uid_raw) > 0 and verify_uid_sig(int(uid_raw), sig_raw):
        return {
            "id": int(uid_raw),
            "username": "",
            "first_name": "Игрок"
        }

    raise HTTPException(status_code=401, detail="Отправь /start в боте и открой игру по своей кнопке!")


def admin_main_kb(owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💸 Ласт выводы", callback_data="adm_last_wd")],
        [InlineKeyboardButton(text="💰 Выдать / снять GRAM", callback_data="adm_bal_edit")],
        [InlineKeyboardButton(text="🎯 Статус X2 первых 2", callback_data="adm_x2_status")],
        [InlineKeyboardButton(text="⚙️ Режим работы и шансы", callback_data="adm_chances")],
        [InlineKeyboardButton(text="🎁 Создать промокод", callback_data="adm_promo_new")],
        [InlineKeyboardButton(text="📋 Активные промокоды", callback_data="adm_promo_list")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="🎲 История игр", callback_data="adm_game_history_1")],
        [InlineKeyboardButton(text="🧹 Сбросить балансы > 100k G", callback_data="adm_wipe_abuse")],
    ]
    if owner:
        rows.insert(1, [InlineKeyboardButton(text="👮 Управление админами", callback_data="adm_manage_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_chances_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Личные шансы игроков (список)", callback_data="adm_uch_list_1")],
        [InlineKeyboardButton(text="Режим 1 (Стандарт для всех)", callback_data="set_ch_mode_1")],
        [InlineKeyboardButton(text="Режим 2 (0% для всех)", callback_data="set_ch_0")],
        [InlineKeyboardButton(text="Режим 3 (100% победа для всех)", callback_data="set_ch_100")],
        [InlineKeyboardButton(text="Режим 4 (Auto для всех)", callback_data="set_ch_auto")],
        [InlineKeyboardButton(text="✏️ Свой % для всех", callback_data="set_ch_custom")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]
    ])


def sub_required_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📢 Подписаться на {REQUIRED_CHANNEL}", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub_btn")]
    ])


async def send_welcome_with_app(chat_id: int, user_id: int):
    sig = make_uid_sig(user_id)
    personal_url = f"{WEBAPP_URL}?uid={user_id}&sig={sig}"
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text="RubUP", web_app=WebAppInfo(url=personal_url))
        )
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть RubUP", web_app=WebAppInfo(url=personal_url))]
    ])
    await bot.send_message(
        chat_id=chat_id,
        text="✅ Подписка подтверждена!\nОткрой <b>RubUP</b> по своей персональной кнопке ниже:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    uname = (message.from_user.username or "").strip().lstrip("@")
    fname = (message.from_user.first_name or "Игрок").strip()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users_gram (user_id, username, first_name, balance, wagered, x2_dep_used, custom_chance) VALUES (?, ?, ?, 0.0, 0.0, 0, '')",
            (user_id, uname, fname)
        )
        await db.execute("UPDATE users_gram SET username = ?, first_name = ? WHERE user_id = ?", (uname, fname, user_id))
        await db.commit()

    if is_owner(message.from_user):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, val) VALUES ('admin_chat_id', ?)", (str(message.chat.id),))
            await db.commit()

    if not await check_channel_sub(user_id):
        await message.answer(
            f"🔒 <b>Доступ к RubUP закрыт</b>\n\nДля входа в игру подпишись на наш канал <b>{REQUIRED_CHANNEL}</b> и нажми кнопку проверки:",
            reply_markup=sub_required_kb(),
            parse_mode="HTML"
        )
        return

    await send_welcome_with_app(message.chat.id, user_id)


@dp.callback_query(F.data == "check_sub_btn")
async def cb_check_sub(call: types.CallbackQuery):
    user_id = call.from_user.id
    if not await check_channel_sub(user_id):
        await call.answer(f"❌ Ты ещё не подписался на {REQUIRED_CHANNEL}!", show_alert=True)
        return
    await call.answer("✅ Подписка подтверждена!")
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_welcome_with_app(call.message.chat.id, user_id)


async def redeem_promo_code(user_id: int, raw_code: str) -> tuple[bool, str, float, float]:
    code = raw_code.strip().upper()[:32]
    if not code:
        return False, "Введи промокод", 0.0, 0.0

    async with user_locks[user_id]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT amount, uses_left FROM promos WHERE code = ?", (code,)) as cursor:
                promo = await cursor.fetchone()
            if not promo or promo[1] <= 0:
                return False, "Промокод не найден или закончился", 0.0, 0.0
            async with db.execute("SELECT 1 FROM promo_uses WHERE user_id = ? AND code = ?", (user_id, code)) as cursor:
                if await cursor.fetchone():
                    return False, "Ты уже активировал этот промокод", 0.0, 0.0
            amount, _ = promo
            await db.execute("INSERT INTO promo_uses (user_id, code) VALUES (?, ?)", (user_id, code))
            await db.execute("UPDATE promos SET uses_left = uses_left - 1 WHERE code = ?", (code,))
            await db.commit()
        new_bal, _ = await update_balance(user_id, amount)
        return True, f"Начислено {amount} GRAM", amount, new_bal


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    await ensure_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "Игрок")
    if is_owner(message.from_user):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, val) VALUES ('admin_chat_id', ?)", (str(message.chat.id),))
            await db.commit()
    await state.clear()
    cur = await get_custom_chance()
    await message.answer(
        f"👑 <b>Панель управления RubUP</b>\n\nАктивный общий профиль: <b>{format_chance_label(cur)}</b>",
        reply_markup=admin_main_kb(owner=is_owner(message.from_user)),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    await state.clear()
    cur = await get_custom_chance()
    await call.message.edit_text(
        f"👑 <b>Панель управления RubUP</b>\n\nАктивный общий профиль: <b>{format_chance_label(cur)}</b>",
        reply_markup=admin_main_kb(owner=is_owner(call.from_user)),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_wipe_abuse")
async def cb_wipe_abuse(call: types.CallbackQuery):
    if not await is_admin(call.from_user):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE users_gram SET balance = 0.0 WHERE balance > 100000")
        affected = cur.rowcount
        await db.commit()
    await call.answer(f"Обнулено аккаунтов с балансом > 100k: {affected}", show_alert=True)


@dp.callback_query(F.data == "adm_x2_status")
async def cb_adm_x2_status(call: types.CallbackQuery):
    if not await is_admin(call.from_user):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, x2_dep_used FROM users_gram ORDER BY ROWID ASC LIMIT 2") as cur:
            rows = await cur.fetchall()
    if not rows:
        text = "🎯 <b>Первые 2 игрока</b>\n\nПока никто не заходил."
    else:
        lines = []
        for idx, (uid, uname, used) in enumerate(rows, 1):
            label = f"@{uname}" if uname else f"ID {uid}"
            st = "❌ Использован / сгорел" if int(used or 0) == 1 else "✅ Активен (следующий деп до 5000 G даст x2)"
            lines.append(f"{idx}. {label} — {st}")
        text = "🎯 <b>Бонус X2 на след. депозит (до 5 000 GRAM):</b>\n\n" + "\n".join(lines)
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_manage_admins")
async def cb_manage_admins(call: types.CallbackQuery):
    if not is_owner(call.from_user):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, user_id, username FROM admins ORDER BY id DESC") as cur:
            rows = await cur.fetchall()

    kb_rows = [[InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add_admin")]]
    if not rows:
        text = f"👮 <b>Управление админами</b>\n\nГлавный владелец: @{OWNER_USERNAME}\nДополнительных админов пока нет."
    else:
        lines = []
        for aid, uid, uname in rows:
            label = f"@{uname}" if uname else f"ID {uid}"
            if uname and uid:
                label = f"@{uname} (ID {uid})"
            lines.append(f"• {label}")
            kb_rows.append([InlineKeyboardButton(text=f"❌ Удалить {label}", callback_data=f"adm_del_admin_{aid}")])
        text = f"👮 <b>Управление админами</b>\n\nГлавный владелец: @{OWNER_USERNAME}\n\n<b>Назначенные админы:</b>\n" + "\n".join(lines)

    kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


@dp.callback_query(F.data == "adm_add_admin")
async def cb_add_admin(call: types.CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user):
        return
    await state.set_state(AdminStates.waiting_new_admin)
    await call.message.edit_text(
        "➕ <b>Добавление администратора</b>\n\nОтправь Telegram <b>ID</b> пользователя или его <b>@username</b>:\n\nПримеры:\n<code>123456789</code>\n<code>@username</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_manage_admins")]]),
        parse_mode="HTML"
    )


@dp.message(AdminStates.waiting_new_admin)
async def msg_add_admin(message: types.Message, state: FSMContext):
    if not is_owner(message.from_user):
        return
    raw = (message.text or "").strip().split()[0].lstrip("@")
    if not raw:
        await message.answer("❌ Введи корректный ID или @username.")
        return

    uid = int(raw) if raw.isdigit() else 0
    uname = "" if raw.isdigit() else raw

    async with aiosqlite.connect(DB_PATH) as db:
        if uid > 0:
            async with db.execute("SELECT username FROM users_gram WHERE user_id = ?", (uid,)) as cur:
                r = await cur.fetchone()
                if r and r[0]:
                    uname = r[0]
            async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (uid,)) as cur:
                if await cur.fetchone():
                    await state.clear()
                    await message.answer("⚠️ Этот пользователь уже является админом.", reply_markup=admin_main_kb(owner=True))
                    return
        else:
            async with db.execute("SELECT user_id FROM users_gram WHERE LOWER(REPLACE(username, '@', '')) = LOWER(?)", (uname,)) as cur:
                r = await cur.fetchone()
                if r:
                    uid = r[0]
            async with db.execute("SELECT 1 FROM admins WHERE LOWER(REPLACE(username, '@', '')) = LOWER(?)", (uname,)) as cur:
                if await cur.fetchone():
                    await state.clear()
                    await message.answer("⚠️ Этот пользователь уже является админом.", reply_markup=admin_main_kb(owner=True))
                    return

        await db.execute("INSERT INTO admins (user_id, username) VALUES (?, ?)", (uid, uname))
        await db.commit()

    await state.clear()
    added_label = f"@{uname}" if uname else f"ID {uid}"
    await message.answer(
        f"✅ Администратор <b>{added_label}</b> добавлен!\nТеперь он может открыть панель через <code>/admin</code>.",
        reply_markup=admin_main_kb(owner=True),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("adm_del_admin_"))
async def cb_del_admin(call: types.CallbackQuery):
    if not is_owner(call.from_user):
        return
    aid = int(call.data.replace("adm_del_admin_", ""))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE id = ?", (aid,))
        await db.commit()
    await call.answer("Админ удалён", show_alert=True)
    await cb_manage_admins(call)


@dp.callback_query(F.data == "adm_last_wd")
async def cb_adm_last_wd(call: types.CallbackQuery):
    if not await is_admin(call.from_user):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, username, currency, amount, tx_details, status FROM withdrawals ORDER BY id DESC LIMIT 12"
        ) as cursor:
            rows = await cursor.fetchall()

    kb_rows = []
    if not rows:
        text = "💸 <b>Ласт выводы</b>\n\nЗаявок на вывод пока нет."
    else:
        lines = []
        for wid, uid, uname, curr, amt, tx, st in rows:
            u_str = f"@{uname}" if uname and uname != "без_юза" else f"ID:{uid}"
            mark = "✅" if st == "paid" else "⏳"
            lines.append(f"{mark} <b>#{wid}</b> | {u_str} | <b>{amt} {curr}</b>\n└ Реквизиты: <code>{tx}</code>")
            if st != "paid":
                kb_rows.append([InlineKeyboardButton(text=f"✅ Выплачено #{wid} ({amt} {curr})", callback_data=f"pay_wd_{wid}")])
        text = "💸 <b>Последние выводы:</b>\n\n" + "\n\n".join(lines)

    kb_rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_last_wd")])
    kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


@dp.callback_query(F.data.startswith("pay_wd_"))
async def cb_pay_wd(call: types.CallbackQuery):
    if not await is_admin(call.from_user):
        return
    wid = int(call.data.replace("pay_wd_", ""))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, currency, amount FROM withdrawals WHERE id = ?", (wid,)) as cursor:
            row = await cursor.fetchone()
        await db.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (wid,))
        await db.commit()
    if row:
        try:
            await bot.send_message(
                chat_id=row[0],
                text=f"✅ Твоя заявка на вывод <b>#{wid}</b> ({row[2]} {row[1]}) успешно выполнена!",
                parse_mode="HTML"
            )
        except Exception:
            pass
    await call.answer(f"Заявка #{wid} отмечена выплаченной!", show_alert=True)
    await cb_adm_last_wd(call)


@dp.callback_query(F.data == "adm_bal_edit")
async def cb_adm_bal_edit(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    await state.set_state(AdminStates.waiting_balance_edit)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, balance FROM users_gram ORDER BY ROWID DESC LIMIT 10") as cur:
            recent = await cur.fetchall()
    recent_txt = ""
    if recent:
        lines = [f"• <code>{r[0]}</code> ({'@' + r[1] if r[1] else 'без юза'}) — {r[2]} G" for r in recent]
        recent_txt = "\n\n<b>Последние игроки в базе:</b>\n" + "\n".join(lines)

    await call.message.edit_text(
        "💰 <b>Выдача / списание GRAM</b>\n\nВведи ID игрока (или @username) и сумму через пробел:\n<code>123456789 50</code> (депозит)\n<code>@username -20</code> (списать)" + recent_txt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
        parse_mode="HTML"
    )


async def resolve_target_user_id(target_raw: str) -> int | None:
    clean = target_raw.strip().lstrip("@")
    if not clean:
        return None
    if clean.isdigit():
        return int(clean)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users_gram WHERE LOWER(REPLACE(TRIM(username), '@', '')) = LOWER(?)", (clean,)) as cur:
            r = await cur.fetchone()
            if r:
                return int(r[0])
        async with db.execute("SELECT user_id FROM withdrawals WHERE LOWER(REPLACE(TRIM(username), '@', '')) = LOWER(?) LIMIT 1", (clean,)) as cur:
            r = await cur.fetchone()
            if r:
                return int(r[0])
        async with db.execute("SELECT user_id FROM admins WHERE LOWER(REPLACE(TRIM(username), '@', '')) = LOWER(?) AND user_id > 0", (clean,)) as cur:
            r = await cur.fetchone()
            if r:
                return int(r[0])

    try:
        chat = await bot.get_chat(f"@{clean}")
        if chat and chat.id:
            await ensure_user(chat.id, clean, chat.first_name or "Игрок")
            return int(chat.id)
    except Exception:
        pass

    return None


@dp.message(AdminStates.waiting_balance_edit)
async def msg_bal_edit(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("❌ Формат: <code>ID СУММА</code> или <code>@username СУММА</code>", parse_mode="HTML")
        return

    try:
        delta = round(float(parts[1].replace(",", ".")), 2)
        if not math.isfinite(delta) or delta == 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return

    uid = await resolve_target_user_id(parts[0])
    if not uid:
        await message.answer(
            "❌ Игрок с таким @username не найден.\nПопроси его нажать <code>/start</code> в боте или введи его цифровой Telegram ID.",
            parse_mode="HTML"
        )
        return

    async with user_locks[uid]:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users_gram (user_id, balance, wagered, x2_dep_used, custom_chance) VALUES (?, 0.0, 0.0, 0, '')", (uid,))
            await db.commit()

        x2_note = ""
        actual_delta = delta
        if delta > 0:
            eligible = await is_first_two_eligible(uid)
            if eligible:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE users_gram SET x2_dep_used = 1 WHERE user_id = ?", (uid,))
                    await db.commit()
                if delta <= X2_MAX_DEPOSIT_GRAM:
                    actual_delta = round(delta * 2, 2)
                    x2_note = f"\n🔥 Сработал бонус <b>X2</b> для первых 2 игроков! Начислено <b>{actual_delta} GRAM</b> вместо {delta} GRAM."
                else:
                    x2_note = f"\n⚠️ Депозит превысил лимит {X2_MAX_DEPOSIT_GRAM} GRAM — бонус X2 не применён и сгорел."

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users_gram SET balance = MAX(0, round(balance + ?, 2)) WHERE user_id = ?", (actual_delta, uid))
            await db.commit()
            async with db.execute("SELECT balance FROM users_gram WHERE user_id = ?", (uid,)) as cur:
                new_b = (await cur.fetchone())[0]

    if actual_delta > 0:
        try:
            bonus_txt = " (с бонусом <b>X2</b>!)" if actual_delta > delta else ""
            await bot.send_message(
                chat_id=uid,
                text=f"💎 Твой баланс пополнен на <b>{actual_delta} GRAM</b>{bonus_txt}\nТекущий баланс: <b>{new_b} GRAM</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await state.clear()
    await message.answer(
        f"✅ Баланс игрока <code>{uid}</code> обновлён!{x2_note}\nНовый баланс: <b>{new_b} GRAM</b>",
        reply_markup=admin_main_kb(owner=is_owner(message.from_user)),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_chances")
async def cb_adm_chances(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    await state.clear()
    cur = await get_custom_chance()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users_gram WHERE custom_chance != '' AND custom_chance IS NOT NULL") as c:
            personal_cnt = (await c.fetchone())[0]
    await call.message.edit_text(
        f"⚙️ <b>Настройка режимов и шансов</b>\n\n"
        f"Общий режим для всех: <b>{format_chance_label(cur)}</b>\n"
        f"Игроков с личным шансом: <b>{personal_cnt}</b>",
        reply_markup=admin_chances_kb(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("set_ch_"))
async def cb_set_chance(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    val = call.data.replace("set_ch_", "")
    if val == "custom":
        await state.set_state(AdminStates.waiting_chance)
        await call.message.edit_text(
            "✏️ Введи общий шанс победы для всех игроков от <b>0</b> до <b>100</b>:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_chances")]]),
            parse_mode="HTML"
        )
        return
    await set_custom_chance(val)
    await call.answer(f"Общий режим: {format_chance_label(val)}", show_alert=True)
    await cb_adm_chances(call, state)


@dp.message(AdminStates.waiting_chance)
async def msg_custom_chance(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    try:
        val = float((message.text or "").replace(",", "."))
        if math.isfinite(val) and 0 <= val <= 100:
            await set_custom_chance(str(val))
            await state.clear()
            await message.answer(
                f"✅ Общий шанс установлен: <b>{val}%</b>.",
                reply_markup=admin_main_kb(owner=is_owner(message.from_user)),
                parse_mode="HTML"
            )
        else:
            await message.answer("Введи число от 0 до 100:")
    except ValueError:
        await message.answer("Неверный формат. Введи число:")


# --- СПИСОК ИГРОКОВ ДЛЯ ПЕРСОНАЛЬНЫХ ШАНСОВ ---
@dp.callback_query(F.data.startswith("adm_uch_list_"))
async def cb_adm_uch_list(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    await state.clear()
    try:
        page = max(1, int(call.data.replace("adm_uch_list_", "")))
    except ValueError:
        page = 1

    per_page = 8
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users_gram") as cur:
            total_users = (await cur.fetchone())[0]
        total_pages = max(1, math.ceil(total_users / per_page))
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        async with db.execute("""
            SELECT user_id, username, first_name, balance, custom_chance
            FROM users_gram
            ORDER BY ROWID DESC LIMIT ? OFFSET ?
        """, (per_page, offset)) as cur:
            users = await cur.fetchall()

    kb_rows = []
    if not users:
        text = "👤 <b>Личные шансы игроков</b>\n\nВ базе пока нет игроков."
    else:
        lines = []
        for uid, uname, fname, bal, c_ch in users:
            u_label = f"@{uname}" if uname else f"{fname} ({uid})"
            ch_str = format_chance_label(c_ch)
            lines.append(f"• <b>{u_label}</b> — {bal} G\n  └ Шанс: <b>{ch_str}</b>")
            btn_name = f"@{uname}" if uname else f"ID {uid}"
            short_ch = c_ch if c_ch else "общий"
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"👤 {btn_name} [{short_ch}]",
                    callback_data=f"adm_uch_user_{uid}_{page}"
                )
            ])
        text = (
            f"👤 <b>Личные шансы игроков (Стр. {page}/{total_pages})</b>\n\n"
            + "\n".join(lines)
            + "\n\nВыбери игрока из списка, чтобы изменить его шанс:"
        )

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_uch_list_{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data=f"adm_uch_list_{page}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"adm_uch_list_{page + 1}"))
    if nav_row:
        kb_rows.append(nav_row)

    kb_rows.append([InlineKeyboardButton(text="🔙 Назад к режимам", callback_data="adm_chances")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_uch_user_"))
async def cb_adm_uch_user(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    await state.clear()
    parts = call.data.replace("adm_uch_user_", "").split("_")
    target_uid = int(parts[0])
    page = int(parts[1]) if len(parts) > 1 else 1

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, first_name, balance, wagered, custom_chance FROM users_gram WHERE user_id = ?",
            (target_uid,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await call.answer("Игрок не найден", show_alert=True)
        return

    uname, fname, bal, wag, c_ch = row
    global_ch = await get_custom_chance()
    u_title = f"@{uname} (ID: <code>{target_uid}</code>)" if uname else f"{fname} (ID: <code>{target_uid}</code>)"

    text = (
        f"🎯 <b>Настройка шансов игрока</b>\n\n"
        f"Игрок: {u_title}\n"
        f"Баланс: <b>{bal} GRAM</b> | Оборот: <b>{wag} GRAM</b>\n\n"
        f"Текущий личный шанс: <b>{format_chance_label(c_ch)}</b>\n"
        f"<i>(Общий режим сервера: {format_chance_label(global_ch)})</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 100% (Победа)", callback_data=f"set_uch_{target_uid}_100_{page}"),
            InlineKeyboardButton(text="❌ 0% (Слив)", callback_data=f"set_uch_{target_uid}_0_{page}")
        ],
        [
            InlineKeyboardButton(text="⚖️ Auto (Честный)", callback_data=f"set_uch_{target_uid}_auto_{page}"),
            InlineKeyboardButton(text="✏️ Свой %", callback_data=f"set_uch_{target_uid}_custom_{page}")
        ],
        [InlineKeyboardButton(text="🔄 Сбросить на общий режим", callback_data=f"set_uch_{target_uid}_default_{page}")],
        [InlineKeyboardButton(text="🔙 К списку игроков", callback_data=f"adm_uch_list_{page}")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("set_uch_"))
async def cb_set_user_chance(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    parts = call.data.replace("set_uch_", "").split("_")
    target_uid = int(parts[0])
    val = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 1

    if val == "custom":
        await state.set_state(AdminStates.waiting_user_chance)
        await state.update_data(target_uid=target_uid, return_page=page)
        await call.message.edit_text(
            f"✏️ Введи персональный шанс победы для игрока <code>{target_uid}</code> (число от <b>0</b> до <b>100</b>):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"adm_uch_user_{target_uid}_{page}")]
            ]),
            parse_mode="HTML"
        )
        return

    await set_user_custom_chance(target_uid, val)
    await call.answer(f"Установлено: {format_chance_label(val)}", show_alert=True)
    call.data = f"adm_uch_user_{target_uid}_{page}"
    await cb_adm_uch_user(call, state)


@dp.message(AdminStates.waiting_user_chance)
async def msg_user_custom_chance(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    data = await state.get_data()
    target_uid = int(data.get("target_uid", 0))
    page = int(data.get("return_page", 1))
    try:
        val = float((message.text or "").replace(",", "."))
        if math.isfinite(val) and 0 <= val <= 100:
            await set_user_custom_chance(target_uid, str(val))
            await state.clear()
            await message.answer(
                f"✅ Личный шанс для игрока <code>{target_uid}</code> установлен: <b>{val}%</b>.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👤 Открыть карточку игрока", callback_data=f"adm_uch_user_{target_uid}_{page}")],
                    [InlineKeyboardButton(text="🔙 К списку игроков", callback_data=f"adm_uch_list_{page}")]
                ]),
                parse_mode="HTML"
            )
        else:
            await message.answer("Введи число от 0 до 100:")
    except ValueError:
        await message.answer("Неверный формат. Введи число от 0 до 100:")


@dp.callback_query(F.data == "adm_promo_new")
async def cb_promo_new(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    await state.set_state(AdminStates.waiting_promo_data)
    await call.message.edit_text(
        "🎁 <b>Создание промокода (в GRAM)</b>\n\nФормат:\n<code>КОД СУММА_GRAM КОЛ_ВО_АКТИВАЦИЙ</code>\n\nПример:\n<code>GRAM5 5 50</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
        parse_mode="HTML"
    )


@dp.message(AdminStates.waiting_promo_data)
async def msg_promo_data(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
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
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO promos (code, amount, uses_left) VALUES (?, ?, ?)", (code, amount, uses))
            await db.commit()
        await state.clear()
        await message.answer(
            f"✅ Промокод <b>{code}</b> создан!\nСумма: <b>{amount} GRAM</b>\nАктиваций: <b>{uses}</b>",
            reply_markup=admin_main_kb(owner=is_owner(message.from_user)),
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Сумма и количество активаций должны быть больше 0.")


@dp.callback_query(F.data == "adm_promo_list")
async def cb_promo_list(call: types.CallbackQuery):
    if not await is_admin(call.from_user):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT code, amount, uses_left FROM promos WHERE uses_left > 0") as cursor:
            rows = await cursor.fetchall()
    text = "📋 Активных промокодов пока нет." if not rows else (
        "📋 <b>Активные промокоды:</b>\n\n" + "\n".join(f"• <code>{r[0]}</code> — <b>{r[1]} GRAM</b> (осталось: {r[2]})" for r in rows)
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_broadcast")
async def cb_broadcast(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user):
        return
    await state.set_state(AdminStates.waiting_broadcast)
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\nОтправь текст (или фото с подписью) для рассылки всем игрокам:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]]),
        parse_mode="HTML"
    )


@dp.message(AdminStates.waiting_broadcast)
async def msg_broadcast(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user):
        return
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM users_gram WHERE user_id > 0") as cursor:
            users = await cursor.fetchall()

    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ Отправка ({len(users)} чел.)...")

    text_content = message.html_text if (message.text or message.caption) else ""
    photo_id = message.photo[-1].file_id if message.photo else None
    video_id = message.video.file_id if message.video else None

    for (uid,) in users:
        try:
            if photo_id:
                await bot.send_photo(chat_id=uid, photo=photo_id, caption=text_content or None, parse_mode="HTML")
            elif video_id:
                await bot.send_video(chat_id=uid, video=video_id, caption=text_content or None, parse_mode="HTML")
            elif text_content:
                await bot.send_message(chat_id=uid, text=text_content, parse_mode="HTML")
            else:
                await message.copy_to(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.06)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\nДоставлено: <b>{sent}</b>\nОшибок: <b>{failed}</b>",
        reply_markup=admin_main_kb(owner=is_owner(message.from_user)),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_stats")
async def cb_stats(call: types.CallbackQuery):
    if not await is_admin(call.from_user):
        return
    async with aiosqlite.connect(DB_PATH) as db:
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


@dp.callback_query(F.data.startswith("adm_game_history_"))
async def cb_adm_game_history(call: types.CallbackQuery):
    if not await is_admin(call.from_user):
        return
    try:
        page = max(1, int(call.data.replace("adm_game_history_", "")))
    except ValueError:
        page = 1

    per_page = 10
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM game_history") as cur:
            total_count = (await cur.fetchone())[0]
        total_pages = max(1, math.ceil(total_count / per_page))
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        async with db.execute("""
            SELECT g.id, g.user_id, u.username, g.game, g.bet, g.mult, g.win
            FROM game_history g
            LEFT JOIN users_gram u ON u.user_id = g.user_id
            ORDER BY g.id DESC LIMIT ? OFFSET ?
        """, (per_page, offset)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        text = "🎲 <b>История игр</b>\n\nСтавок пока не было."
    else:
        lines = []
        for gid, uid, uname, game, bet, mult, win in rows:
            u_str = f"@{uname}" if uname else f"ID:{uid}"
            res_str = f"✅ +{win} G" if win > 0 else f"❌ -{bet} G"
            lines.append(f"<b>#{gid}</b> | {u_str} | {game} (×{mult})\n└ Ставка: <b>{bet} G</b> → {res_str}")
        text = f"🎲 <b>История игр (Стр. {page} из {total_pages}, всего {total_count}):</b>\n\n" + "\n\n".join(lines)

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_game_history_{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data=f"adm_game_history_{page}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"adm_game_history_{page + 1}"))

    kb_rows = [
        nav_row,
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"adm_game_history_{page}")],
        [InlineKeyboardButton(text="🔙 В меню админки", callback_data="adm_back")]
    ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


@app.get("/", response_class=HTMLResponse)
@app.get("/rubup", response_class=HTMLResponse)
async def serve_webapp(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"web_{client_ip}", max_calls=60, window=10.0)
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/user")
async def api_user(request: Request):
    data = await request.json()
    user_obj = extract_webapp_user(data)
    uid = int(user_obj["id"])

    if not await check_channel_sub(uid):
        raise HTTPException(
            status_code=403,
            detail=f"Подпишись на канал {REQUIRED_CHANNEL}, чтобы играть в RubUP!"
        )

    u = await ensure_user(uid, user_obj.get("username", ""), user_obj.get("first_name", "Игрок"))

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, currency, amount, tx_details, status FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 10", (uid,)) as cur:
            my_wds = [{"id": r[0], "currency": r[1], "amount": r[2], "tx": r[3], "status": r[4]} for r in await cur.fetchall()]
        async with db.execute("SELECT game, bet, mult, win FROM game_history WHERE user_id = ? ORDER BY id DESC LIMIT 15", (uid,)) as cur:
            my_games = [{"game": r[0], "bet": r[1], "mult": r[2], "win": r[3]} for r in await cur.fetchall()]

    return {
        "user_id": uid,
        "sig": make_uid_sig(uid),
        "balance": u["balance"],
        "wagered": u["wagered"],
        "withdrawals": my_wds,
        "history": my_games,
        "admin_username": OWNER_USERNAME
    }


@app.post("/api/promo")
async def api_promo(request: Request):
    data = await request.json()
    user_obj = extract_webapp_user(data)
    uid = int(user_obj["id"])
    if not await check_channel_sub(uid):
        raise HTTPException(status_code=403, detail=f"Подпишись на {REQUIRED_CHANNEL}!")
    ok, msg, amount, new_bal = await redeem_promo_code(uid, str(data.get("code", "")))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg, "balance": new_bal}


@app.post("/api/withdraw")
async def api_withdraw(request: Request):
    data = await request.json()
    user_obj = extract_webapp_user(data)
    uid = int(user_obj["id"])
    if not await check_channel_sub(uid):
        raise HTTPException(status_code=403, detail=f"Подпишись на {REQUIRED_CHANNEL}!")

    u_info = await ensure_user(uid, user_obj.get("username", ""), user_obj.get("first_name", "Игрок"))
    username = user_obj.get("username") or u_info["username"] or "без_юза"

    currency = str(data.get("currency", "GRAM")).upper()
    if currency not in ("GRAM", "STARS"):
        raise HTTPException(status_code=400, detail="Неверная валюта")

    try:
        amount = round(float(data.get("amount", 0)), 2)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Некорректная сумма")

    tx_details = str(data.get("tx_details", "")).strip()[:120]
    if not tx_details:
        raise HTTPException(status_code=400, detail="Впиши номер транзакции / реквизиты")
    if not math.isfinite(amount) or amount <= 0:
        raise HTTPException(status_code=400, detail="Укажи корректную сумму")

    gram_cost = amount if currency == "GRAM" else round(amount / STARS_PER_GRAM, 2)
    if gram_cost < 0.1:
        raise HTTPException(status_code=400, detail="Минимум: 0.1 GRAM или 10 Stars")

    async with user_locks[uid]:
        u = await ensure_user(uid)
        if u["balance"] < gram_cost:
            raise HTTPException(status_code=400, detail="Недостаточно GRAM на балансе")
        new_bal, _ = await update_balance(uid, -gram_cost)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO withdrawals (user_id, username, currency, amount, tx_details, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (uid, username, currency, amount, tx_details, int(time.time()))
            )
            wd_id = cursor.lastrowid
            await db.commit()

    await notify_all_admins(
        f"💸 <b>Новая заявка на вывод #{wd_id}!</b>\n\n"
        f"Игрок: @{username} (ID: <code>{uid}</code>)\n"
        f"Сумма: <b>{amount} {currency}</b> (списано {gram_cost} GRAM)\n"
        f"Транзакция / реквизиты: <code>{tx_details}</code>\n"
        f"Баланс игрока: <b>{new_bal} GRAM</b>"
    )

    return {
        "ok": True,
        "wd_id": wd_id,
        "balance": new_bal,
        "currency": currency,
        "amount": amount,
        "tx_details": tx_details,
        "admin_username": OWNER_USERNAME
    }


@app.post("/api/upgrade")
async def api_upgrade(request: Request):
    data = await request.json()
    user_obj = extract_webapp_user(data)
    uid = int(user_obj["id"])
    if not await check_channel_sub(uid):
        raise HTTPException(status_code=403, detail=f"Подпишись на {REQUIRED_CHANNEL}!")
    fname = user_obj.get("first_name", "Игрок")

    now = time.monotonic()
    if now - last_action_ts.get(uid, 0) < 0.7:
        raise HTTPException(status_code=429, detail="Подожди секунду")
    last_action_ts[uid] = now

    try:
        bet = round(float(data.get("bet", 0)), 2)
        multiplier = round(float(data.get("multiplier", 2.0)), 2)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Некорректные данные")

    if not math.isfinite(bet) or not math.isfinite(multiplier) or bet < 0.05 or bet > 50000 or multiplier < 1.1 or multiplier > 20:
        raise HTTPException(status_code=400, detail="Некорректная ставка или множитель")

    async with user_locks[uid]:
        u = await ensure_user(uid, user_obj.get("username", ""), fname)
        if bet > u["balance"]:
            raise HTTPException(status_code=400, detail="Недостаточно GRAM")

        win_chance = await resolve_win_chance((100.0 / multiplier) * 0.95, user_id=uid)
        roll = secrets.randbelow(10000) / 100.0

        if win_chance >= 100.0 or (win_chance > 0 and roll < win_chance):
            win_amount = min(MAX_SINGLE_WIN_GRAM, round(bet * multiplier, 2))
            profit = round(win_amount - bet, 2)
            new_bal, new_wag = await update_balance(uid, profit, add_wager=bet)
            await record_game_event(uid, "Апгрейд", bet, multiplier, win_amount)
            return {"win": True, "balance": new_bal, "wagered": new_wag, "win_amount": win_amount}
        else:
            new_bal, new_wag = await update_balance(uid, -bet, add_wager=bet)
            await record_game_event(uid, "Апгрейд", bet, multiplier, 0.0)
            return {"win": False, "balance": new_bal, "wagered": new_wag, "win_amount": 0.0}


@app.post("/api/crash")
async def api_crash(request: Request):
    data = await request.json()
    user_obj = extract_webapp_user(data)
    uid = int(user_obj["id"])
    if not await check_channel_sub(uid):
        raise HTTPException(status_code=403, detail=f"Подпишись на {REQUIRED_CHANNEL}!")
    fname = user_obj.get("first_name", "Игрок")

    now = time.monotonic()
    if now - last_action_ts.get(uid, 0) < 0.7:
        raise HTTPException(status_code=429, detail="Подожди секунду")
    last_action_ts[uid] = now

    try:
        bet = round(float(data.get("bet", 0)), 2)
        target_mult = round(float(data.get("target_mult", 2.0)), 2)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Некорректные данные")

    if not math.isfinite(bet) or not math.isfinite(target_mult) or bet < 0.05 or bet > 50000 or target_mult < 1.1 or target_mult > 20:
        raise HTTPException(status_code=400, detail="Некорректная ставка или цель (макс 20x)")

    async with user_locks[uid]:
        u = await ensure_user(uid, user_obj.get("username", ""), fname)
        if bet > u["balance"]:
            raise HTTPException(status_code=400, detail="Недостаточно GRAM")

        win_chance = await resolve_win_chance((100.0 / target_mult) * 0.95, user_id=uid)
        roll = secrets.randbelow(10000) / 100.0

        if win_chance >= 100.0 or (win_chance > 0 and roll < win_chance):
            crash_point = round(target_mult + (secrets.randbelow(250) / 100.0), 2)
            win_amount = min(MAX_SINGLE_WIN_GRAM, round(bet * target_mult, 2))
            profit = round(win_amount - bet, 2)
            new_bal, new_wag = await update_balance(uid, profit, add_wager=bet)
            await record_game_event(uid, "Ракетка", bet, target_mult, win_amount)
            return {"win": True, "crash_point": crash_point, "balance": new_bal, "wagered": new_wag, "win_amount": win_amount}
        else:
            max_fail = max(100, int((target_mult - 0.05) * 100))
            crash_point = round(secrets.randbelow(max_fail) / 100.0, 2)
            if crash_point < 1.0:
                crash_point = 1.0
            new_bal, new_wag = await update_balance(uid, -bet, add_wager=bet)
            await record_game_event(uid, "Ракетка", bet, target_mult, 0.0)
            return {"win": False, "crash_point": crash_point, "balance": new_bal, "wagered": new_wag, "win_amount": 0.0}


def calc_mines_mult(safe_opened: int, bombs: int) -> float:
    if safe_opened <= 0:
        return 1.0
    prob = 1.0
    for i in range(safe_opened):
        prob *= (25 - bombs - i) / (25 - i)
    return round((0.95 / prob), 2)


@app.post("/api/mines/start")
async def api_mines_start(request: Request):
    data = await request.json()
    user_obj = extract_webapp_user(data)
    uid = int(user_obj["id"])
    if not await check_channel_sub(uid):
        raise HTTPException(status_code=403, detail=f"Подпишись на {REQUIRED_CHANNEL}!")
    fname = user_obj.get("first_name", "Игрок")

    now = time.monotonic()
    if now - last_action_ts.get(uid, 0) < 0.5:
        raise HTTPException(status_code=429, detail="Подожди секунду")
    last_action_ts[uid] = now

    try:
        bet = round(float(data.get("bet", 0)), 2)
        bombs = int(data.get("bombs", 3))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Некорректные данные")

    if bet < 0.05 or bet > 50000 or bombs not in (2, 3, 5, 10, 15, 20):
        raise HTTPException(status_code=400, detail="Неверная ставка или число мин")

    async with user_locks[uid]:
        if uid in active_mines:
            raise HTTPException(status_code=400, detail="У тебя уже есть активная игра в Бомбы")
        u = await ensure_user(uid, user_obj.get("username", ""), fname)
        if bet > u["balance"]:
            raise HTTPException(status_code=400, detail="Недостаточно GRAM")
        new_bal, new_wag = await update_balance(uid, -bet, add_wager=bet)

        cells = list(range(25))
        bomb_positions = set()
        while len(bomb_positions) < bombs:
            bomb_positions.add(secrets.choice(cells))

        active_mines[uid] = {
            "bet": bet,
            "bombs": bombs,
            "bomb_set": bomb_positions,
            "opened": []
        }
    return {"ok": True, "balance": new_bal, "wagered": new_wag, "next_mult": calc_mines_mult(1, bombs)}


@app.post("/api/mines/step")
async def api_mines_step(request: Request):
    data = await request.json()
    user_obj = extract_webapp_user(data)
    uid = int(user_obj["id"])
    cell = int(data.get("cell", -1))

    async with user_locks[uid]:
        game = active_mines.get(uid)
        if not game or cell < 0 or cell >= 25 or cell in game["opened"]:
            raise HTTPException(status_code=400, detail="Неверный ход")

        user_mode = await get_user_custom_chance(uid)
        mode = user_mode if user_mode else await get_custom_chance()
        if mode == "0":
            game["bomb_set"].add(cell)

        if cell in game["bomb_set"]:
            bombs_list = list(game["bomb_set"])
            del active_mines[uid]
            await record_game_event(uid, "Бомбы", game["bet"], 0.0, 0.0)
            u = await ensure_user(uid)
            return {"boom": True, "bombs": bombs_list, "balance": u["balance"]}

        game["opened"].append(cell)
        cur_mult = calc_mines_mult(len(game["opened"]), game["bombs"])
        next_mult = calc_mines_mult(len(game["opened"]) + 1, game["bombs"])
        win_now = min(MAX_SINGLE_WIN_GRAM, round(game["bet"] * cur_mult, 2))

        if len(game["opened"]) >= (25 - game["bombs"]):
            bombs_list = list(game["bomb_set"])
            del active_mines[uid]
            new_bal, _ = await update_balance(uid, win_now)
            await record_game_event(uid, "Бомбы", game["bet"], cur_mult, win_now)
            return {"boom": False, "finished": True, "bombs": bombs_list, "mult": cur_mult, "win_amount": win_now, "balance": new_bal}

        return {"boom": False, "finished": False, "mult": cur_mult, "next_mult": next_mult, "win_amount": win_now}


@app.post("/api/mines/cashout")
async def api_mines_cashout(request: Request):
    data = await request.json()
    user_obj = extract_webapp_user(data)
    uid = int(user_obj["id"])

    async with user_locks[uid]:
        game = active_mines.get(uid)
        if not game or len(game["opened"]) == 0:
            raise HTTPException(status_code=400, detail="Открой хотя бы одну ячейку")
        cur_mult = calc_mines_mult(len(game["opened"]), game["bombs"])
        win_now = min(MAX_SINGLE_WIN_GRAM, round(game["bet"] * cur_mult, 2))
        bombs_list = list(game["bomb_set"])
        del active_mines[uid]
        new_bal, _ = await update_balance(uid, win_now)
        await record_game_event(uid, "Бомбы", game["bet"], cur_mult, win_now)
        return {"ok": True, "bombs": bombs_list, "mult": cur_mult, "win_amount": win_now, "balance": new_bal}


async def run_polling_forever():
    await asyncio.sleep(2.0)
    while True:
        try:
            await dp.start_polling(bot, drop_pending_updates=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[POLLING RECONNECT] {e}")
            await asyncio.sleep(3.0)


async def main():
    await init_db()
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(
        server.serve(),
        run_polling_forever()
    )


if __name__ == "__main__":
    asyncio.run(main())
