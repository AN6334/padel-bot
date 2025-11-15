# -*- coding: utf-8 -*-
import os
import threading
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
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from datetime import datetime, time, timedelta
import pytz
import redis  # Redis síncrono (Upstash)

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

def generate_siesta_slots_for_day(day_str: str) -> list[str]:
    day_date = datetime.strptime(day_str, "%d/%m/%Y").date()
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)
    open_dt  = datetime.combine(day_date, time(14, 0))
    close_dt = datetime.combine(day_date, time(18, 0))
    delta = timedelta(minutes=60)
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

def get_today_tomorrow_labels() -> list[str]:
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz).date()
    today = now
    tomorrow = today + timedelta(days=1)
    today_label = today.strftime("%d/%m/%Y")
    tomorrow_label = tomorrow.strftime("%d/%m/%Y")
    return [today_label, tomorrow_label]

def get_future_days_labels() -> list[str]:
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz).date()
    days = []
    for i in range(2, 7):
        d = now + timedelta(days=i)
        days.append(d.strftime("%d/%m/%Y"))
    return days

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

def is_slot_booked(day: str, slot: str) -> bool:
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

def get_booking(day, slot):
    if r:
        raw = r.get(booking_key(day, slot))
        if not raw:
            return None
        return json.loads(raw)
    return bookingsDB.get(day, {}).get(slot)

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

# ---- Utilidades de teclado ----
def main_menu_keyboard():
    keyboard = [
        ["🎾 Reservar pista", "❌ Cancelar reserva"],
        ["🛏️ Reservar siesta"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def dates_keyboard():
    today_label, tomorrow_label = get_today_tomorrow_labels()
    future_days = get_future_days_labels()
    row1 = [f"📅 {today_label}", f"📅 {tomorrow_label}"]
    row2 = [f"📅 {d}" for d in future_days[:3]]
    row3 = [f"📅 {d}" for d in future_days[3:]]
    return ReplyKeyboardMarkup([row1, row2, row3, ["⬅️ Volver al menú"]], resize_keyboard=True)

def slots_keyboard(day: str):
    slots = generate_time_slots_for_day(day)
    rows = []
    row = []
    for s in slots:
        row.append(s)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(["⬅️ Volver al menú"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def siesta_slots_keyboard(day: str):
    slots = generate_siesta_slots_for_day(day)
    rows = []
    row = []
    for s in slots:
        row.append(s)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(["⬅️ Volver al menú"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

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
    label_today, label_tomorrow = get_today_tomorrow_labels()
    future_days = get_future_days_labels()
    keyboard = [
        [f"📅 {label_today}", f"📅 {label_tomorrow}"],
        [f"📅 {d}" for d in future_days[:3]],
        [f"📅 {d}" for d in future_days[3:]],
        ["⬅️ Volver al menú"],
    ]
    await update.message.reply_text(
        "📅 ¿Para qué día quieres reservar?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    username = update.effective_user.username
    if not username:
        await update.message.reply_text("Necesitas tener un nombre de usuario (@username) en Telegram para cancelar reservas.")
        return

    user_bookings = list_user_bookings(username)
    if not user_bookings:
        await update.message.reply_text("No tienes reservas activas.")
        return

    keyboard = []
    for day, slot in user_bookings:
        keyboard.append([f"{day} {slot}"])
    keyboard.append(["⬅️ Volver al menú"])

    bookings[chat_id] = {"cancel_mode": True}
    await update.message.reply_text(
        "Selecciona la reserva que quieres cancelar:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)
    msg = f"Bot activo. Hora del servidor (Madrid): {now.strftime('%Y-%m-%d %H:%M:%S')}"
    await update.message.reply_text(msg)

async def on_siesta_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bookings[chat_id] = {"siesta": True}
    label_today, label_tomorrow = get_today_tomorrow_labels()
    future_days = get_future_days_labels()
    keyboard = [
        [f"📅 {label_today}", f"📅 {label_tomorrow}"],
        [f"📅 {d}" for d in future_days[:3]],
        [f"📅 {d}" for d in future_days[3:]],
        ["⬅️ Volver al menú"],
    ]
    await update.message.reply_text(
        "😴 ¿Para qué día quieres reservar la siesta?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Menú principal:",
        reply_markup=main_menu_keyboard()
    )

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    state = bookings.get(chat_id, {})

    if text == "⬅️ Volver al menú":
        bookings[chat_id] = {}
        await send_main_menu(update, context)
        return

    if text == "🎾 Reservar pista":
        await reservar(update, context)
        return

    if text == "❌ Cancelar reserva":
        await cancelar(update, context)
        return

    if text == "🛏️ Reservar siesta":
        await on_siesta_choice(update, context)
        return

    if state.get("cancel_mode"):
        username = update.effective_user.username
        if not username:
            await update.message.reply_text("Necesitas tener un nombre de usuario (@username) en Telegram para cancelar reservas.")
            return
        parts = text.split(" ", 1)
        if len(parts) != 2:
            await update.message.reply_text("Formato no válido. Usa el botón de la reserva.")
            return
        day, slot = parts
        booking = get_booking(day, slot)
        if not booking or booking.get("username") != username:
            await update.message.reply_text("No se encontró la reserva seleccionada o no te pertenece.")
            return
        delete_booking(day, slot)
        await update.message.reply_text(f"Reserva cancelada: {day} {slot}")
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"❌ Cancelación de reserva:\n\nDía: {day}\nHora: {slot}\nUsuario: @{username}"
        )
        bookings[chat_id] = {}
        await send_main_menu(update, context)
        return

    if text.startswith("📅 "):
        day_label = text.replace("📅 ", "").strip()
        tz = pytz.timezone("Europe/Madrid")
        now = datetime.now(tz)

        try:
            day_date = datetime.strptime(day_label, "%d/%m/%Y").date()
        except ValueError:
            await update.message.reply_text("Fecha inválida. Usa los botones.")
            return

        today = now.date()
        max_day = today + timedelta(days=6)
        if day_date < today or day_date > max_day:
            await update.message.reply_text("Solo puedes reservar dentro de los próximos 7 días.")
            return

        bookings[chat_id]["day"] = day_label

        if bookings[chat_id].get("siesta"):
            keyboard = siesta_slots_keyboard(day_label)
            await update.message.reply_text(
                f"😴 Elige una franja para la siesta el {day_label}:",
                reply_markup=keyboard
            )
        else:
            keyboard = slots_keyboard(day_label)
            await update.message.reply_text(
                f"📅 Elige una franja horaria para el {day_label}:",
                reply_markup=keyboard
            )
        return

    state = bookings.get(chat_id, {})
    if "day" in state and ("siesta" in state or "day" in state):
        day = state["day"]
        slot = text

        if not slot or " - " not in slot:
            await update.message.reply_text("Selecciona una franja horaria válida.")
            return

        if is_slot_booked(day, slot):
            await update.message.reply_text("Esa franja ya está reservada. Elige otra.")
            return

        username = update.effective_user.username or update.effective_user.full_name
        data = {
            "username": username,
            "day": day,
            "slot": slot,
            "siesta": state.get("siesta", False)
        }

        if not set_booking(day, slot, data):
            await update.message.reply_text("Esa franja ya fue reservada justo ahora. Elige otra.")
            return

        tipo = "siesta" if state.get("siesta") else "pista"
        await update.message.reply_text(
            f"✅ Reserva de {tipo} confirmada para el {day} en la franja {slot}.",
            reply_markup=main_menu_keyboard()
        )

        icon = "😴" if state.get("siesta") else "🎾"
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"{icon} Nueva reserva de {tipo}:\n\nDía: {day}\nHora: {slot}\nUsuario: @{username}"
        )

        bookings[chat_id] = {}
        return

    await send_main_menu(update, context)
    return

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "No entiendo ese comando. Usa los botones del menú."
    )

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    new_chat_member = result.new_chat_member
    if new_chat_member.status == "member":
        user = new_chat_member.user
        msg = f"👋 Bienvenido, {user.full_name}!"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)

# ---- Arranque: webhook + FastAPI para Render ----

# Configuración de webhook (usar variables de entorno en Render)
BASE_URL = os.environ.get("BASE_URL")  # p.ej. "https://tu-servicio.onrender.com"
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
        load_db_file()
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
