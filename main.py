# -*- coding: utf-8 -*-
import os
import json
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ChatMemberHandler,
)
from datetime import datetime, time, timedelta
import pytz
import redis  # Upstash Redis

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

# ---- Configuración ----
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID")
REDIS_URL = os.environ.get("REDIS_URL")

if not BOT_TOKEN or not GROUP_CHAT_ID:
    raise Exception("No están definidas las variables de entorno BOT_TOKEN o GROUP_CHAT_ID")
GROUP_CHAT_ID = int(GROUP_CHAT_ID)

# Conexión Redis (Upstash) con log claro
r = None
if not REDIS_URL:
    print("⚠️ REDIS_URL no está configurado. Usando almacenamiento en archivo (no persistente).")
else:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        print("✅ Connected to Redis")
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        r = None

# Estado de conversación por chat
bookings = {}

# ---- Slots y fechas ----

def generate_time_slots_for_day(day_str: str) -> list[str]:
    day_date = datetime.strptime(day_str, "%d/%m/%Y").date()
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)
    open_dt  = datetime.combine(day_date, time(10, 0))
    close_dt = datetime.combine(day_date, time(22, 0))
    delta = timedelta(minutes=30)

    slots = []
    current = open_dt
    while current < close_dt:
        slot_label = current.strftime("%H:%M") + " - " + (current + delta).strftime("%H:%M")
        slot_end = current + delta
        if day_date == now.date():
            if slot_end <= now:
                current += delta
                continue
        slots.append(slot_label)
        current += delta
    return slots

def get_date_string(offset_days: int) -> str:
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz).date()
    target = now + timedelta(days=offset_days)
    return target.strftime("%d/%m/%Y")

# ---- Almacenamiento de reservas ----

DB_FILE = "bookings.json"
bookingsDB = {}

def load_db_file():
    global bookingsDB
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                bookingsDB = json.load(f)
        except Exception:
            bookingsDB = {}
    else:
        bookingsDB = {}

def save_db_file():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(bookingsDB, f, ensure_ascii=False)

def booking_key(day: str, slot: str) -> str:
    return f"booking:{day}:{slot}"

def is_taken(day: str, slot: str) -> bool:
    if r:
        return r.exists(booking_key(day, slot)) == 1
    return slot in bookingsDB.get(day, {})

def set_booking(day, slot, data) -> bool:
    if r:
        ok = r.set(booking_key(day, slot), json.dumps(data, ensure_ascii=False), nx=True)
        return bool(ok)

    if day not in bookingsDB:
        bookingsDB[day] = {}
    if slot in bookingsDB[day]:
        return False
    bookingsDB[day][slot] = data
    save_db_file()
    return True

def delete_booking(day, slot):
    if r:
        r.delete(booking_key(day, slot))
        return
    if day in bookingsDB and slot in bookingsDB[day]:
        del bookingsDB[day][slot]
        if not bookingsDB[day]:
            del bookingsDB[day]
        save_db_file()

def list_user_bookings(username):
    result = []
    if r:
        for key in r.scan_iter("booking:*"):
            raw = r.get(key)
            if not raw:
                continue
            info = json.loads(raw)
            if info.get("username") == username:
                _, day, slot = key.split(":", 2)
                result.append((day, slot))
        return result
    for day, slots in bookingsDB.items():
        for slot, info in slots.items():
            if info.get("username") == username:
                result.append((day, slot))
    return result

def cleanup_old_bookings():
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)

    if r:
        keys_to_delete = []
        for key in r.scan_iter("booking:*"):
            raw = r.get(key)
            if not raw:
                keys_to_delete.append(key)
                continue
            info = json.loads(raw)
            day = info.get("day")
            slot = info.get("slot")
            if not day or not slot:
                keys_to_delete.append(key)
                continue
            try:
                day_date = datetime.strptime(day, "%d/%m/%Y")
            except ValueError:
                keys_to_delete.append(key)
                continue
            slot_start_str = slot.split(" - ")[0]
            try:
                slot_time = datetime.strptime(slot_start_str, "%H:%M").time()
            except ValueError:
                keys_to_delete.append(key)
                continue
            dt = pytz.timezone("Europe/Madrid").localize(datetime.combine(day_date, slot_time))
            if dt < now - timedelta(hours=12):
                keys_to_delete.append(key)
        for k in keys_to_delete:
            r.delete(k)
        return

    global bookingsDB
    new_db = {}
    for day, slots in bookingsDB.items():
        day_date = datetime.strptime(day, "%d/%m/%Y")
        for slot, info in slots.items():
            slot_start_str = slot.split(" - ")[0]
            slot_time = datetime.strptime(slot_start_str, "%H:%M").time()
            dt = datetime.combine(day_date, slot_time)
            dt = pytz.timezone("Europe/Madrid").localize(dt)
            if dt >= now - timedelta(hours=12):
                if day not in new_db:
                    new_db[day] = {}
                new_db[day][slot] = info
    bookingsDB = new_db
    save_db_file()

# ---- Menú principal ----

async def send_main_menu(update, context):
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    await update.message.reply_text(
        "Elige una opción 👇",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

# ---- Handlers ----

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bookings[chat_id] = {}
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    await update.message.reply_text(
        "🎾 ¡Reserva tu pista aquí!\n\nPulsa /start para iniciar el proceso.\n\nTodas las reservas se publican aquí automáticamente 👇",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

async def reservar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bookings[chat_id] = {}
    labels = [f"Hoy ({get_date_string(0)})", f"Mañana ({get_date_string(1)})"]
    keyboard = [labels]
    await update.message.reply_text(
        "📅 ¿Para qué día quieres reservar?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_old_bookings()
    chat_id = update.effective_chat.id
    username = update.message.from_user.username or update.message.from_user.first_name
    user_bookings = list_user_bookings(username)
    if not user_bookings:
        await update.message.reply_text("🔎 No tienes reservas activas.")
        await send_main_menu(update, context)
        return
    context.user_data["cancel_options"] = user_bookings
    keyboard = [[f"{d} - {t}"] for d, t in user_bookings]
    await update.message.reply_text(
        "❓ ¿Qué reserva quieres cancelar?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)
    msg = f"Bot activo. Hora del servidor (Madrid): {now.strftime('%Y-%m-%d %H:%M:%S')}"
    await update.message.reply_text(msg)

async def on_siesta_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Lo siento, este horario no está disponible debido a la siesta. Por favor, elige otro horario."
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
            text=("🎾 ¡Reserva tu pista aquí!\n\n"
                  "Pulsa /start para iniciar el proceso.\n\n"
                  "Todas las reservas se publican aquí automáticamente 👇")
        )

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    username = update.message.from_user.username or update.message.from_user.first_name
    state = bookings.get(chat_id, {})

    # --- Cancelación ---
    if "cancel_options" in context.user_data:
        options = context.user_data.get("cancel_options", [])
        for day, slot in options:
            if text == f"{day} - {slot}":
                delete_booking(day, slot)
                await update.message.reply_text("❌ Reserva cancelada.", reply_markup=ReplyKeyboardRemove())
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=f"❌ Reserva cancelada:\n📅 {day}\n🕒 {slot}\n👤 Usuario: @{username}"
                )
                await send_main_menu(update, context)
                context.user_data["cancel_options"] = []
                return

    # --- Iniciar reserva ---
    if text.startswith("🎾"):
        labels = [f"Hoy ({get_date_string(0)})", f"Mañana ({get_date_string(1)})"]
        keyboard = [labels]
        await update.message.reply_text(
            "📅 ¿Para qué día quieres reservar?",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        bookings[chat_id] = {}
        return

    # --- Elección de día ---
    if not state.get("day"):
        tz = pytz.timezone("Europe/Madrid")
        now = datetime.now(tz)
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
                "⏳ Solo puedes reservar una pista desde las 00:00 del día anterior (hora de Madrid). ¡Inténtalo más tarde!"
            )
            await send_main_menu(update, context)
            return

        bookings[chat_id] = {"day": day}
        day_str = bookings[chat_id]["day"]
        slots = generate_time_slots_for_day(day_str)

        keyboard = []
        for slot in slots:
            start_str = slot.split(" - ")[0]
            st = datetime.strptime(start_str, "%H:%M").time()
            if is_taken(day_str, slot):
                keyboard.append([f"🟥 {slot}"])
            elif time(14, 30) <= st < time(17, 30):
                keyboard.append([f"🛏️ {slot}"])
            else:
                keyboard.append([f"🟩 {slot}"])

        await update.message.reply_text(
            "🕒 Elige una hora:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return

    # --- Elección de hora ---
    if state.get("day") and not state.get("time"):
        clean_text = text.replace("🟩", "").replace("🟥", "").replace("🛏️", "").strip()
        if is_taken(state["day"], clean_text):
            await update.message.reply_text("⛔ Esta hora ya está reservada.")
            await send_main_menu(update, context)
            bookings.pop(chat_id, None)
            return

        start_str = clean_text.split(" - ")[0]
        st = datetime.strptime(start_str, "%H:%M").time()
        if time(14, 30) <= st < time(17, 30):
            # Siesta: no se permite reservar
            await on_siesta_choice(update, context)
            bookings.pop(chat_id, None)
            return

        bookings[chat_id]["time"] = clean_text
        await update.message.reply_text(
            "🏠 Indica tu piso (por ejemplo: 2B):",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    # --- Piso ---
    if state.get("day") and state.get("time") and not state.get("piso"):
        bookings[chat_id]["piso"] = text
        await update.message.reply_text("👤 Escribe tu nombre para la reserva:")
        return

    # --- Nombre y confirmación ---
    if state.get("day") and state.get("time") and state.get("piso") and not state.get("name"):
        bookings[chat_id]["name"] = text
        day = bookings[chat_id]["day"]
        slot = bookings[chat_id]["time"]
        piso = bookings[chat_id]["piso"]
        name = bookings[chat_id]["name"]

        data = {
            "username": username,
            "day": day,
            "slot": slot,
            "piso": piso,
            "name": name,
        }

        if not set_booking(day, slot, data):
            await update.message.reply_text("⛔ Esta hora ya fue reservada justo ahora por otra persona.")
            await send_main_menu(update, context)
            bookings.pop(chat_id, None)
            return

        await update.message.reply_text(
            f"✅ Reserva confirmada:\n📅 {day}\n🕒 {slot}\n🏠 Piso: {piso}\n👤 Nombre: {name}",
            reply_markup=ReplyKeyboardRemove()
        )
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"📢 Nueva reserva\n📅 Día: {day}\n🕒 Hora: {slot}\n🏠 Piso: {piso}\n👤 Nombre: {name}"
        )
        bookings.pop(chat_id, None)
        await send_main_menu(update, context)
        return

    # cualquier otra cosa → menú
    await send_main_menu(update, context)
    return

# ---- Arranque con webhook (FastAPI + Render) ----

# Configuración de webhook
BASE_URL = os.environ.get("BASE_URL")  # p.ej. "https://padel-bot-v77e.onrender.com"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "padel_webhook_secret")

if not BASE_URL:
    raise Exception("No está definida la variable de entorno BASE_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = BASE_URL.rstrip("/") + WEBHOOK_PATH

# Aplicación de Telegram
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("reservar", reservar))
telegram_app.add_handler(CommandHandler("cancelar", cancelar))
telegram_app.add_handler(CommandHandler("status", status))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & filters.Regex(r"^🛏️"), on_siesta_choice)
)
telegram_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle,
    )
)
telegram_app.add_handler(
    MessageHandler(
        filters.COMMAND & filters.ChatType.PRIVATE,
        unknown,
    )
)
telegram_app.add_handler(
    ChatMemberHandler(
        welcome_new_member,
        ChatMemberHandler.CHAT_MEMBER,
    )
)

# FastAPI app para Render
app = FastAPI()

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"

@app.get("/health", response_class=PlainTextResponse)
async def health():
    if r:
        try:
            r.ping()
        except Exception as e:
            print(f"❌ Redis ping error en /health: {e}")
            return PlainTextResponse("Redis error", status_code=500)
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
    if not r:
        load_db_file()  # fallback sólo si no hay Redis
    cleanup_old_bookings()
    print("✅ Iniciando bot con webhook...")

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
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
