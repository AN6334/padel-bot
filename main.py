# -*- coding: utf-8 -*-
import os
import json
from datetime import datetime, time, timedelta

import pytz
import redis
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# ---- Configuración ----
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID")
REDIS_URL = os.environ.get("REDIS_URL")
BASE_URL = os.environ.get("BASE_URL")  # p.ej. "https://padel-bot-v77e.onrender.com"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "padel_webhook_secret")

if not BOT_TOKEN or not GROUP_CHAT_ID:
    raise Exception("No están definidas las variables de entorno BOT_TOKEN o GROUP_CHAT_ID")

if not BASE_URL:
    raise Exception("No está definida la variable de entorno BASE_URL")

GROUP_CHAT_ID = int(GROUP_CHAT_ID)

# ---- Conexión Redis (Upstash) ----
if not REDIS_URL:
    raise Exception("No está definida la variable de entorno REDIS_URL")

try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    print("✅ Connected to Redis")
except Exception as e:
    raise Exception(f"❌ Redis connection failed: {e}")

# ---- Slots y fechas ----
def generate_time_slots_for_day(day_str: str) -> list[str]:
    """Genera slots de 1h30 entre 10:00 y 22:00, ocultando ya pasados para hoy."""
    day_date = datetime.strptime(day_str, "%d/%m/%Y").date()
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)

    open_dt = datetime.combine(day_date, time(10, 0))
    close_dt = datetime.combine(day_date, time(22, 0))
    delta = timedelta(hours=1, minutes=30)

    slots = []
    cur = open_dt
    while cur + delta <= close_dt:
        end = cur + delta
        # скрывать прошедшие слоты только для сегодня
        if day_date == now.date() and cur < now.replace(tzinfo=None):
            cur = end
            continue
        slots.append(f"{cur.strftime('%H:%M')}–{end.strftime('%H:%M')}")
        cur = end
    return slots


def get_date_string(offset: int) -> str:
    tz = pytz.timezone("Europe/Madrid")
    return (datetime.now(tz) + timedelta(days=offset)).strftime("%d/%m/%Y")


# ---- Almacenamiento en Redis ----
def booking_key(day: str, slot: str) -> str:
    return f"booking:{day}:{slot}"


def is_taken(day: str, time_slot: str) -> bool:
    return r.exists(booking_key(day, time_slot)) == 1


def set_booking(day: str, slot: str, data: dict) -> bool:
    """Intenta crear reserva; True si creada, False si ya existe."""
    ok = r.set(booking_key(day, slot), json.dumps(data, ensure_ascii=False), nx=True)
    return bool(ok)


def delete_booking(day: str, slot: str):
    r.delete(booking_key(day, slot))


def list_user_bookings(username: str):
    """Возвращает список (day, slot) для пользователя."""
    result = []
    for key in r.scan_iter("booking:*"):
        raw = r.get(key)
        if not raw:
            continue
        info = json.loads(raw)
        if info.get("username") == username:
            _, day, slot = key.split(":", 2)
            result.append((day, slot))
    return result


def cleanup_old_bookings():
    """Удаляет брони прошлых дней."""
    tz = pytz.timezone("Europe/Madrid")
    today = datetime.now(tz).date()

    for key in r.scan_iter("booking:*"):
        try:
            _, day, slot = key.split(":", 2)
            if datetime.strptime(day, "%d/%m/%Y").date() < today:
                r.delete(key)
        except Exception:
            # не ломаемся из-за кривого ключа
            pass


# ---- Menú principal ----
async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    await update.message.reply_text(
        "Elige una opción 👇",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )


# ---- Handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    await update.message.reply_text(
        "🎾 ¡Reserva tu pista aquí!\n\nPulsa /start para iniciar el proceso.\n\n"
        "Todas las reservas se publican aquí automáticamente 👇",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )


async def reservar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    labels = [f"Hoy ({get_date_string(0)})", f"Mañana ({get_date_string(1)})"]
    keyboard = [labels]
    await update.message.reply_text(
        "📅 ¿Para qué día quieres reservar?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_old_bookings()
    username = update.message.from_user.username or update.message.from_user.first_name
    user_bookings = list_user_bookings(username)

    if not user_bookings:
        await update.message.reply_text("🔎 No tienes reservas activas.")
        await send_main_menu(update, context)
        return

    context.user_data.clear()
    context.user_data["cancel_options"] = user_bookings
    keyboard = [[f"{d} - {t}"] for d, t in user_bookings]
    await update.message.reply_text(
        "❓ ¿Qué reserva quieres cancelar?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )


async def on_siesta_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Lo siento, no es posible reservar este horario debido a la siesta. Por favor, elige otro."
    )
    await send_main_menu(update, context)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Escribe /start para comenzar.")
    await send_main_menu(update, context)


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_status = update.chat_member.old_chat_member.status
    new_status = update.chat_member.new_chat_member.status
    if old_status in ("left", "kicked") and new_status in ("member", "administrator"):
        await context.bot.send_message(
            chat_id=update.chat_member.chat.id,
            text=(
                "🎾 ¡Reserva tu pista aquí!\n\n"
                "Pulsa /start para iniciar el proceso.\n\n"
                "Todas las reservas se publican aquí automáticamente 👇"
            ),
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = pytz.timezone("Europe/Madrid")
    today = datetime.now(tz).strftime("%d/%m/%Y")
    count = 0
    for _ in r.scan_iter(f"booking:{today}:*"):
        count += 1
    await update.message.reply_text(f"Fuente: Redis\nHoy ({today}) reservas: {count}")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    username = update.message.from_user.username or update.message.from_user.first_name
    state = context.user_data

    # --- Cancelación ---
    if "cancel_options" in state and state.get("cancel_options"):
        options = state.get("cancel_options", [])
        for day, slot in options:
            if text == f"{day} - {slot}":
                delete_booking(day, slot)
                await update.message.reply_text("❌ Reserva cancelada.", reply_markup=ReplyKeyboardRemove())
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=f"❌ Reserva cancelada:\n📅 {day}\n🕒 {slot}\n👤 Usuario: @{username}",
                )
                state.clear()
                await send_main_menu(update, context)
                return

    # --- Iniciar reserva через кнопку ---
    if text.startswith("🎾"):
        state.clear()
        labels = [f"Hoy ({get_date_string(0)})", f"Mañana ({get_date_string(1)})"]
        keyboard = [labels]
        await update.message.reply_text(
            "📅 ¿Para qué día quieres reservar?",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        )
        return

    # --- Перейти в режим отмены через кнопку ---
    if text.startswith("❌"):
        await cancelar(update, context)
        return

    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)

    # --- Elección del día ---
    if not state.get("day"):
        if text.startswith("Hoy"):
            day = get_date_string(0)
        elif text.startswith("Mañana"):
            day = get_date_string(1)
        else:
            await send_main_menu(update, context)
            return

        day_date = tz.localize(datetime.strptime(day, "%d/%m/%Y"))
        today = now.date()

        if day_date.date() == today:
            allowed_from = day_date.replace(hour=0, minute=0, second=0, microsecond=0)
            allowed_to = day_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif day_date.date() == (today + timedelta(days=1)):
            allowed_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
            allowed_to = day_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            await send_main_menu(update, context)
            return

        if not (allowed_from <= now <= allowed_to):
            await update.message.reply_text(
                "⏳ Solo puedes reservar una pista desde las 00:00 del día anterior "
                "(hora de Madrid). ¡Inténtalo más tarde!"
            )
            await send_main_menu(update, context)
            return

        state["day"] = day

        slots = generate_time_slots_for_day(day)
        keyboard = []
        for slot in slots:
            start_h, start_m = map(int, slot.split("–")[0].split(":"))
            st = time(start_h, start_m)
            if is_taken(day, slot):
                keyboard.append([f"🟥 {slot}"])
            elif time(14, 30) <= st < time(17, 30):
                keyboard.append([f"🛏️ {slot}"])
            else:
                keyboard.append([f"🟩 {slot}"])

        await update.message.reply_text(
            "🕒 Elige una hora:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        )
        return

    # --- Elección de hora ---
    if state.get("day") and not state.get("time"):
        clean_text = (
            text.replace("🟩", "")
            .replace("🟥", "")
            .replace("🛏️", "")
            .strip()
        )

        try:
            start_str = clean_text.split("–")[0]
            start_h, start_m = map(int, start_str.split(":"))
            st = time(start_h, start_m)
        except Exception:
            await send_main_menu(update, context)
            return

        # Жёсткий запрет на сиесту
        if time(14, 30) <= st < time(17, 30):
            await on_siesta_choice(update, context)
            state.clear()
            return

        if is_taken(state["day"], clean_text):
            await update.message.reply_text("⛔ Esta hora ya está reservada.")
            state.clear()
            await send_main_menu(update, context)
            return
        elif clean_text in generate_time_slots_for_day(state["day"]):
            state["time"] = clean_text
            await update.message.reply_text("🏠 ¿Cuál es tu piso? (ej: 2B o 3A)")
            return
        else:
            await send_main_menu(update, context)
            return

    # --- Piso ---
    if state.get("day") and state.get("time") and not state.get("floor"):
        state["floor"] = text
        await update.message.reply_text("👤 ¿Cuál es tu nombre?")
        return

    # --- Nombre + creación de reserva ---
    if state.get("day") and state.get("time") and state.get("floor") and not state.get("name"):
        name = text
        day = state["day"]
        slot = state["time"]
        piso = state["floor"]

        ok = set_booking(day, slot, {"username": username, "piso": piso, "name": name})
        if not ok:
            await update.message.reply_text("⛔ Esta hora ya está reservada.")
            state.clear()
            await send_main_menu(update, context)
            return

        await update.message.reply_text(
            f"✅ ¡Reservado!\n\n📅 Día: {day}\n🕒 Hora: {slot}\n🏠 Piso: {piso}\n👤 Nombre: {name}",
            reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"📢 Nueva reserva\n📅 Día: {day}\n🕒 Hora: {slot}\n🏠 Piso: {piso}\n👤 Nombre: {name}",
        )
        state.clear()
        await send_main_menu(update, context)
        return

    # Cualquier otra cosa → menú
    await send_main_menu(update, context)
    return


# ---- Configuración de webhook (Render + FastAPI) ----
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = BASE_URL.rstrip("/") + WEBHOOK_PATH

telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("reservar", reservar))
telegram_app.add_handler(CommandHandler("cancelar", cancelar))
telegram_app.add_handler(CommandHandler("status", status))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & filters.Regex(r"^🛏️"), on_siesta_choice)
)
telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle)
)
telegram_app.add_handler(
    MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, unknown)
)
telegram_app.add_handler(
    ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER)
)

app = FastAPI()


@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@app.get("/health", response_class=PlainTextResponse)
async def health():
    # простой health-check, не зависит от Redis
    return "OK"


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return PlainTextResponse("OK")


@app.on_event("startup")
async def on_startup():
    cleanup_old_bookings()

    print("✅ Iniciando bot con webhook...")
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=False,  # не теряем накопленные апдейты
    )
    print(f"✅ Webhook configurado en {WEBHOOK_URL}")


@app.on_event("shutdown")
async def on_shutdown():
    print("🛑 Apagando aplicación")
    try:
        await telegram_app.stop()
        await telegram_app.shutdown()
    except Exception as e:
        print(f"Error al apagar telegram_app: {e}")
