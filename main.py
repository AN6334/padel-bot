# -*- coding: utf-8 -*-
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    ChatMemberHandler,
)
from datetime import datetime, timedelta
import os
import json

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

def start(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    bookings[chat_id] = {}
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    update.message.reply_text(
        "🎾 ¡Reserva tu pista aquí!"

Pulsa /start para iniciar el proceso.

Todas las reservas se publican aquí automáticamente 👇",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

def handle(update: Update, context: CallbackContext):
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
                    update.message.reply_text("❌ Reserva cancelada.", reply_markup=ReplyKeyboardRemove())
                    context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=f"❌ Reserva cancelada:
📅 {day}
🕒 {time}
👤 Usuario: @{username}"
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
        update.message.reply_text(
            "📅 ¿Para qué día quieres reservar?",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return

    if text.startswith("❌"):
        cancelar(update, context)
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

        update.message.reply_text(
            "🕒 Elige una hora:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return

    if state.get("day") and not state.get("time"):
        clean_text = text.replace("🟩", "").replace("🟥", "").strip()
        if is_taken(state["day"], clean_text):
            update.message.reply_text("⛔ Esa hora ya está reservada.")
            return
        elif clean_text in TIME_SLOTS:
            bookings[chat_id]["time"] = clean_text
            update.message.reply_text("🏠 ¿Cuál es tu piso? (ej: 2B o 3A)")
            return

    if state.get("day") and state.get("time") and not state.get("floor"):
        piso = text
        day = state["day"]
        time = state["time"]

        if is_taken(day, time):
            update.message.reply_text("⛔ Esa hora ya está reservada.")
            bookings.pop(chat_id, None)
            return

        set_booking(day, time, {"username": username, "piso": piso})
        update.message.reply_text(f"✅ ¡Reservado!
📅 Día: {day}
🕒 Hora: {time}
🏠 Piso: {piso}")
        context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"📢 Nueva reserva:
📅 {day}
🕒 {time}
🏠 Piso: {piso}
👤 Usuario: @{username}"
        )
        bookings.pop(chat_id, None)
        return

def cancelar(update: Update, context: CallbackContext):
    chat_id = update.message.chat_id
    username = update.message.from_user.username or update.message.from_user.first_name

    user_bookings = []
    for day, slots in bookingsDB.items():
        for time, info in slots.items():
            if info.get("username") == username:
                user_bookings.append((day, time))

    if not user_bookings:
        update.message.reply_text("🔎 No tienes reservas activas.")
        return

    context.user_data["cancel_options"] = user_bookings
    keyboard = [[f"{d} - {t}"] for d, t in user_bookings]
    update.message.reply_text(
        "❓ ¿Cuál reserva quieres cancelar?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

def unknown(update: Update, context: CallbackContext):
    update.message.reply_text("Escribe /start para comenzar")

def main():
    load_db()
    cleanup_old_bookings()
    updater = Updater(BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("cancelar", cancelar))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle))
    dispatcher.add_handler(MessageHandler(Filters.command, unknown))

    print("✅ Bot listo...")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
