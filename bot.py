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
from aiogram.filters import CommandStart
from aiogram.types import LabeledPrice, MenuButtonWebApp, WebAppInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

BOT_TOKEN = "1780246830:TqjCrlBhmwucsYl-bfkJmY5bUr5r52bdOlD"
OCEANGRAM_API = "https://oceanment.2-26-197-16.sslip.io"
WEBAPP_URL = "https://твой-домен.onrender.com"
STARS_TO_RUB_RATE = 1.5
HIGH_ROLL_LIMIT_RUB = 10000 * STARS_TO_RUB_RATE

session = AiohttpSession(
    api=TelegramAPIServer.from_base(OCEANGRAM_API)
)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

async def init_db():
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                val INTEGER DEFAULT 0
            )
        """)
        await db.execute("INSERT OR IGNORE INTO stats (key, val) VALUES ('high_roll_count', 0)")
        await db.commit()

async def next_high_roll_number() -> int:
    async with aiosqlite.connect("oceangram_rubup.db") as db:
        await db.execute("UPDATE stats SET val = val + 1 WHERE key = 'high_roll_count'")
        await db.commit()
        async with db.execute("SELECT val FROM stats WHERE key = 'high_roll_count'") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1

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
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🚀 Открыть Руб Ап", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer("Жми кнопку ниже, чтобы открыть апгрейды:", reply_markup=kb)

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
    potential_win = bet * multiplier

    if bet > HIGH_ROLL_LIMIT_RUB or potential_win > HIGH_ROLL_LIMIT_RUB:
        attempt_num = await next_high_roll_number()
        if attempt_num % 3 == 0:
            win_chance = standard_chance
        else:
            win_chance = 5.0
    else:
        win_chance = standard_chance

    roll = secrets.randbelow(10000) / 100.0

    if roll <= win_chance:
        win_amount = round(potential_win, 2)
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
