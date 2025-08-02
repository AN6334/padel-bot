# -*- coding: utf-8 -*-
import os
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
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
import pytz  # Para zona horaria de Madrid

# ---- Ping server para Render ----
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
def start_server():
    port = int(os.getenv("PORT", "8000"))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()
threading.Thread(target=start_server, daemon=True).start()

# ---- Importes y configuración ----
BOT_TOKEN = os.environ.get('BOT_TOKEN')
GROUP_CHAT_ID = os.environ.get('GROUP_CHAT_ID')

if not BOT_TOKEN or not GROUP_CHAT_ID:
    raise Exception("No están definidas las variables de entorno BOT_TOKEN o GROUP_CHAT_ID")
GROUP_CHAT_ID = int(GROUP_CHAT_ID)
DB_FILE = "bookings.json"

bookings = {}
bookingsDB = {}

# ---- Funciones para reservas ----

def generate_time_slots_for_day(day_str: str) -> list[str]:
    day_date = datetime.strptime(day_str, "%d/%m/%Y").date()
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)
    open_dt      = datetime.combine(day_date, time(10, 0))
    siesta_start = datetime.combine(day_date, time(14, 30))
    siesta_end   = datetime.combine(day_date, time(17, 30))
    close_dt     = datetime.combine(day_date, time(22, 0))
    delta = timedelta(hours=1, minutes=30)
    slots = []
    cur = open_dt
    while cur + delta <= close_dt:
        end = cur + delta
        if day_date == now.date() and cur < now.replace(tzinfo=None):
            cur = end
            continue
        slots.append(f"{cur.strftime('%H:%M')}–{end.strftime('%H:%M')}")
        cur = end
    return slots

def get_date_string(offset):
    tz = pytz.timezone("Europe/Madrid")
    return (datetime.now(tz) + timedelta(days=offset)).strftime("%d/%m/%Y")

def is_taken(day, time_slot):
    return time_slot in bookingsDB.get(day, {})

def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(bookingsDB, f, ensure_ascii=False)

def load_db():
    global bookingsDB
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                bookingsDB = json.load(f)
        except Exception as e:
            bookingsDB = {}
            print("Error al cargar la base de datos:", e)

def set_booking(day, slot, data):
    if day not in bookingsDB:
        bookingsDB[day] = {}
    bookingsDB[day][slot] = data
    save_db()

def cleanup_old_bookings():
    tz = pytz.timezone("Europe/Madrid")
    today = datetime.now(tz).date()
    to_delete = [day for day in bookingsDB if datetime.strptime(day, "%d/%m/%Y").date() < today]
    for d in to_delete:
        del bookingsDB[d]
    save_db()

# ---- Главное меню ----

async def send_main_menu(update, context):
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    await update.message.reply_text(
        "Меню:",
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
    labels = [
        f"Hoy ({get_date_string(0)})",
        f"Mañana ({get_date_string(1)})"
    ]
    keyboard = [labels]
    await update.message.reply_text(
        "📅 ¿Para qué día quieres reservar?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_old_bookings()
    chat_id = update.effective_chat.id
    username = update.message.from_user.username or update.message.from_user.first_name
    user_bookings = []
    for day, slots in bookingsDB.items():
        for slot, info in slots.items():
            if info.get("username") == username:
                user_bookings.append((day, slot))
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
            text=(
                "🎾 ¡Reserva tu pista aquí!\n\n"
                "Pulsa /start para iniciar el proceso.\n\n"
                "Todas las reservas se publican aquí automáticamente 👇"
            )
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
                if day in bookingsDB and slot in bookingsDB[day]:
                    del bookingsDB[day][slot]
                    if not bookingsDB[day]:
                        del bookingsDB[day]
                    save_db()
                    await update.message.reply_text("❌ Reserva cancelada.", reply_markup=ReplyKeyboardRemove())
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=f"❌ Reserva cancelada:\n📅 {day}\n🕒 {slot}\n👤 Usuario: @{username}"
                    )
                    await send_main_menu(update, context)
                context.user_data["cancel_options"] = []
                return

    # --- Iniciar reserva по кнопке ---
    if text.startswith("🎾"):
        labels = [
            f"Hoy ({get_date_string(0)})",
            f"Mañana ({get_date_string(1)})"
        ]
        keyboard = [labels]
        await update.message.reply_text(
            "📅 ¿Para qué día quieres reservar?",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        bookings[chat_id] = {}
        return

    if text.startswith("❌"):
        await cancelar(update, context)
        return

    # --- Обработка выбора дня ---
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)

    if text.startswith("Hoy"):
        day = get_date_string(0)
    elif text.startswith("Mañana"):
        day = get_date_string(1)
    else:
        await update.message.reply_text("⛔ Solo puedes reservar para hoy o mañana.")
        await send_main_menu(update, context)
        return

    # Проверка разрешённого периода бронирования
    day_date = datetime.strptime(day, "%d/%m/%Y")
    day_date = tz.localize(day_date)
    allowed_from = day_date - timedelta(days=1)
    allowed_from = allowed_from.replace(hour=0, minute=0, second=0, microsecond=0)
    allowed_to = day_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    if not (allowed_from <= now <= allowed_to):
        await update.message.reply_text(
            "⏳ Solo puedes reservar una pista desde las 00:00 del día anterior (hora de Madrid). ¡Inténtalo más tarde!"
        )
        await send_main_menu(update, context)
        return

    if not state.get("day") and any(text.startswith(p) for p in ["Hoy", "Mañana"]):
        if text.startswith("Hoy"):
            day = get_date_string(0)
        elif text.startswith("Mañana"):
            day = get_date_string(1)
        bookings[chat_id] = {"day": day}

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
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return

    # --- Выбор времени ---
    if state.get("day") and not state.get("time"):
        clean_text = text.replace("🟩", "").replace("🟥", "").replace("🛏️", "").strip()
        if is_taken(state["day"], clean_text):
            await update.message.reply_text("⛔ Esta hora ya está reservada.")
            await send_main_menu(update, context)
            return
        elif clean_text in generate_time_slots_for_day(state["day"]):
            bookings[chat_id]["time"] = clean_text
            await update.message.reply_text("🏠 ¿Cuál es tu piso? (ej: 2B o 3A)")
            return

    if state.get("day") and state.get("time") and not state.get("floor"):
        piso = text
        bookings[chat_id]["floor"] = piso
        await update.message.reply_text("👤 ¿Cuál es tu nombre?")
        return

    if state.get("day") and state.get("time") and state.get("floor") and not state.get("name"):
        name = text
        day = state["day"]
        slot = state["time"]
        piso = state["floor"]
        if is_taken(day, slot):
            await update.message.reply_text("⛔ Esta hora ya está reservada.")
            bookings.pop(chat_id, None)
            await send_main_menu(update, context)
            return
        set_booking(day, slot, {"username": username, "piso": piso, "name": name})
        await update.message.reply_text(
            f"✅ ¡Reservado!\n\n📅 Día: {day}\n🕒 Hora: {slot}\n🏠 Piso: {piso}\n👤 Nombre: {name}",
            reply_markup=ReplyKeyboardRemove()
        )
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"📢 Nueva reserva\n📅 Día: {day}\n🕒 Hora: {slot}\n🏠 Piso: {piso}\n👤 Nombre: {name}"
        )
        bookings.pop(chat_id, None)
        await send_main_menu(update, context)
        return

# --- Запуск приложения ---

if __name__ == '__main__':
    load_db()
    cleanup_old_bookings()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reservar", reservar))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"^🛏️"),
            on_siesta_choice
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))

    # Eliminar webhooks para polling (Render)
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        app.bot.delete_webhook(drop_pending_updates=True)
    )
    print("✅ Webhook eliminado, listo para polling")
    print("✅ Bot listo...")
    app.run_polling(drop_pending_updates=True)
