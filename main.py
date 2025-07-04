import os, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        # Отвечаем на HEAD тем же кодом, но без тела
        self.send_response(200)
        self.end_headers()

def start_server():
    port = int(os.getenv("PORT", "8000"))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()

# Запускаем сервер в фоне, не мешает основному коду
threading.Thread(target=start_server, daemon=True).start()

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ChatMemberHandler,
)
from datetime import datetime, timedelta
import os
import json

# Настройки
BOT_TOKEN = os.environ['BOT_TOKEN']
GROUP_CHAT_ID = int(os.environ['GROUP_CHAT_ID'])

bookings = {}
bookingsDB = {}

DB_FILE = "bookings.json"

TIME_SLOTS = [
    "08:00–09:30", "09:30–11:00", "11:00–12:30",
    "12:30–14:00", "14:00–15:30", "15:30–17:00",
    "17:00–18:30", "18:30–20:00", "20:00–21:30"
]

def get_date_string(offset):
    return (datetime.now() + timedelta(days=offset)).strftime("%d/%m/%Y")

def is_taken(day, time):
    return time in bookingsDB.get(day, {})

def save_db():
    with open(DB_FILE, "w") as f:
        json.dump(bookingsDB, f)

def load_db():
    global bookingsDB
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            bookingsDB = json.load(f)

def set_booking(day, time, data):
    if day not in bookingsDB:
        bookingsDB[day] = {}
    bookingsDB[day][time] = data
    save_db()

def cleanup_old_bookings():
    today = datetime.now().date()
    to_delete = [day for day in bookingsDB if datetime.strptime(day, "%d/%m/%Y").date() < today]
    for d in to_delete:
        del bookingsDB[d]
    save_db()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    bookings[chat_id] = {}
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    await update.message.reply_text(
        "🎾 ¡Reserva tu pista aquí!\n\nPulsa /start para iniciar el proceso.\n\nTodas las reservas se publican aquí automáticamente 👇",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    username = update.message.from_user.username or update.message.from_user.first_name
    state = bookings.get(chat_id, {})

    if "cancel_options" in context.user_data:
        options = context.user_data.get("cancel_options", [])
        for day, time in options:
            if text == f"{day} - {time}":
                if day in bookingsDB and time in bookingsDB[day]:
                    del bookingsDB[day][time]
                    save_db()
                    await update.message.reply_text("❌ Reserva cancelada.", reply_markup=ReplyKeyboardRemove())
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=f"❌ Reserva cancelada:\n📅 {day}\n🕒 {time}\n👤 Usuario: @{username}"
                    )
                    context.user_data["cancel_options"] = []
                    return

    if text.startswith("🎾"):
        labels = [
            f"Hoy ({get_date_string(0)})",
            f"Mañana ({get_date_string(1)})",
            f"Pasado mañana ({get_date_string(2)})"
        ]
        keyboard = [labels]
        await update.message.reply_text(
            "📅 ¿Para qué día quieres reservar?",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return

    if text.startswith("❌"):
        await cancelar(update, context)
        return

    if not state.get("day") and any(text.startswith(p) for p in ["Hoy", "Mañana", "Pasado mañana"]):
        if text.startswith("Hoy"):
            day = get_date_string(0)
        elif text.startswith("Mañana"):
            day = get_date_string(1)
        else:
            day = get_date_string(2)

        bookings[chat_id] = {"day": day}
        keyboard = []
        for slot in TIME_SLOTS:
            if is_taken(day, slot):
                keyboard.append([f"🟥 {slot}"])
            else:
                keyboard.append([f"🟩 {slot}"])

        await update.message.reply_text(
            "🕒 Elige una hora:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return

    if state.get("day") and not state.get("time"):
        clean_text = text.replace("🟩", "").replace("🟥", "").strip()
        if is_taken(state["day"], clean_text):
            await update.message.reply_text("⛔ Esa hora ya está reservada.")
            return
        elif clean_text in TIME_SLOTS:
            bookings[chat_id]["time"] = clean_text
            await update.message.reply_text("🏠 ¿Cuál es tu piso? (ej: 2B o 3A)")
            return

    if state.get("day") and state.get("time") and not state.get("floor"):
        piso = text
        day = state["day"]
        time = state["time"]

        if is_taken(day, time):
            await update.message.reply_text("⛔ Esa hora ya está reservada.")
            bookings.pop(chat_id, None)
            return

        set_booking(day, time, {"username": username, "piso": piso})
        await update.message.reply_text(f"✅ ¡Reservado!\n📅 Día: {day}\n🕒 Hora: {time}\n🏠 Piso: {piso}")
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"📢 Nueva reserva:\n📅 {day}\n🕒 {time}\n🏠 Piso: {piso}\n👤 Usuario: @{username}"
        )
        bookings.pop(chat_id, None)
        return

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    username = update.message.from_user.username or update.message.from_user.first_name

    user_bookings = []
    for day, slots in bookingsDB.items():
        for time, info in slots.items():
            if info.get("username") == username:
                user_bookings.append((day, time))

    if not user_bookings:
        await update.message.reply_text("🔎 No tienes reservas activas.")
        return

    context.user_data["cancel_options"] = user_bookings
    keyboard = [[f"{d} - {t}"] for d, t in user_bookings]
    await update.message.reply_text(
        "❓ ¿Cuál reserva quieres cancelar?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Escribe /start para comenzar")

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

if __name__ == '__main__':
    load_db()
    cleanup_old_bookings()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))

    # Синхронно удаляем все webhooks, чтобы не было конфликта getUpdates
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        app.bot.delete_webhook(drop_pending_updates=True)
    )
    print("✅ Webhook deleted, ready for polling")

    print("✅ Bot listo...")
    app.run_polling()
