# -*- coding: utf-8 -*-
import os
import json
from datetime import datetime, time, timedelta

import pytz
import redis
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# -------------------------------------------------
# Конфиг
# -------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID")
REDIS_URL = os.environ.get("REDIS_URL")
BASE_URL = os.environ.get("BASE_URL")  # например: https://padel-bot-v77e.onrender.com

if not BOT_TOKEN or not GROUP_CHAT_ID:
    raise Exception("No están definidas las variables de entorno BOT_TOKEN o GROUP_CHAT_ID")

GROUP_CHAT_ID = int(GROUP_CHAT_ID)

if not BASE_URL:
    raise Exception(
        "La variable de entorno BASE_URL no está definida. "
        "Pon, por ejemplo: https://padel-bot-v77e.onrender.com"
    )

# -------------------------------------------------
# Redis + fallback en archivo
# -------------------------------------------------
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

DB_FILE = "bookings.json"   # fallback a archivo
bookingsDB: dict = {}       # { day: { slot: {username, piso, name} } }

# estado de conversación en memoria
bookings: dict = {}         # { chat_id: {day, time, floor, name} }


# -------------------------------------------------
# Utilidades de almacenamiento
# -------------------------------------------------
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


def is_taken(day: str, time_slot: str) -> bool:
    if r:
        return r.exists(booking_key(day, time_slot)) == 1
    return time_slot in bookingsDB.get(day, {})


def set_booking(day: str, slot: str, data: dict) -> bool:
    """Devuelve True si se pudo guardar la reserva, False si ya estaba ocupada."""
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


def delete_booking(day: str, slot: str) -> None:
    if r:
        r.delete(booking_key(day, slot))
        return
    if day in bookingsDB and slot in bookingsDB[day]:
        del bookingsDB[day][slot]
        if not bookingsDB[day]:
            del bookingsDB[day]
        save_db_file()


def list_user_bookings(username: str):
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
    today = datetime.now(tz).date()
    if r:
        for key in r.scan_iter("booking:*"):
            try:
                _, day, slot = key.split(":", 2)
                if datetime.strptime(day, "%d/%m/%Y").date() < today:
                    r.delete(key)
            except Exception:
                pass
    else:
        to_delete = [
            d for d in bookingsDB
            if datetime.strptime(d, "%d/%m/%Y").date() < today
        ]
        for d in to_delete:
            del bookingsDB[d]
        save_db_file()


# -------------------------------------------------
# Slots y fechas
# -------------------------------------------------
def generate_time_slots_for_day(day_str: str) -> list[str]:
    day_date = datetime.strptime(day_str, "%d/%m/%Y").date()
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)
    open_dt = datetime.combine(day_date, time(10, 0))
    close_dt = datetime.combine(day_date, time(22, 0))
    delta = timedelta(hours=1, minutes=30)
    slots: list[str] = []
    cur = open_dt

    while cur + delta <= close_dt:
        end = cur + delta
        # no permitir horas ya pasadas hoy
        if day_date == now.date() and cur < now.replace(tzinfo=None):
            cur = end
            continue
        slots.append(f"{cur.strftime('%H:%M')}–{end.strftime('%H:%M')}")
        cur = end

    return slots


def get_date_string(offset: int) -> str:
    tz = pytz.timezone("Europe/Madrid")
    return (datetime.now(tz) + timedelta(days=offset)).strftime("%d/%m/%Y")


# -------------------------------------------------
# Menú
# -------------------------------------------------
async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    await update.message.reply_text(
        "Elige una opción 👇",
        reply_markup=ReplyKeyboardMarkup(
            keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )


# -------------------------------------------------
# Handlers
# -------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bookings[chat_id] = {}
    keyboard = [["🎾 Reservar pista", "❌ Cancelar reserva"]]
    await update.message.reply_text(
        "🎾 ¡Reserva tu pista aquí!\n\n"
        "Pulsa /start para iniciar el proceso.\n\n"
        "Todas las reservas se publican aquí automáticamente 👇",
        reply_markup=ReplyKeyboardMarkup(
            keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )


async def reservar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bookings[chat_id] = {}
    labels = [f"Hoy ({get_date_string(0)})", f"Mañana ({get_date_string(1)})"]
    keyboard = [labels]
    await update.message.reply_text(
        "📅 ¿Para qué día quieres reservar?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
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
        reply_markup=ReplyKeyboardMarkup(
            keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )


async def on_siesta_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Lo siento, este horario no está disponible debido a la siesta. "
        "Por favor, elige otro horario."
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
    src = "Redis" if r else "archivo"
    tz = pytz.timezone("Europe/Madrid")
    today = datetime.now(tz).strftime("%d/%m/%Y")
    count = 0
    if r:
        for _ in r.scan_iter(f"booking:{today}:*"):
            count += 1
    else:
        count = len(bookingsDB.get(today, {}))
    await update.message.reply_text(f"Fuente: {src}\nHoy ({today}) reservas: {count}")


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
                await update.message.reply_text(
                    "❌ Reserva cancelada.", reply_markup=ReplyKeyboardRemove()
                )
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=(
                        "❌ Reserva cancelada:\n"
                        f"📅 {day}\n"
                        f"🕒 {slot}\n"
                        f"👤 Usuario: @{username}"
                    ),
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
            reply_markup=ReplyKeyboardMarkup(
                keyboard, one_time_keyboard=True, resize_keyboard=True
            ),
        )
        bookings[chat_id] = {}
        return

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
            allowed_from = day_date.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            allowed_to = day_date.replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
        elif day_date.date() == (today + timedelta(days=1)):
            allowed_from = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            allowed_to = day_date.replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
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
            reply_markup=ReplyKeyboardMarkup(
                keyboard, one_time_keyboard=True, resize_keyboard=True
            ),
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
        if is_taken(state["day"], clean_text):
            await update.message.reply_text("⛔ Esta hora ya está reservada.")
            await send_main_menu(update, context)
            return
        elif clean_text in generate_time_slots_for_day(state["day"]):
            bookings[chat_id]["time"] = clean_text
            await update.message.reply_text("🏠 ¿Cuál es tu piso? (ej: 2B o 3A)")
            return
        else:
            await send_main_menu(update, context)
            return

    # --- Piso / Nombre ---
    if state.get("day") and state.get("time") and not state.get("floor"):
        bookings[chat_id]["floor"] = text
        await update.message.reply_text("👤 ¿Cuál es tu nombre?")
        return

    if (
        state.get("day")
        and state.get("time")
        and state.get("floor")
        and not state.get("name")
    ):
        name = text
        day = state["day"]
        slot = state["time"]
        piso = state["floor"]

        ok = set_booking(day, slot, {"username": username, "piso": piso, "name": name})
        if not ok:
            await update.message.reply_text("⛔ Esta hora ya está reservada.")
            bookings.pop(chat_id, None)
            await send_main_menu(update, context)
            return

        await update.message.reply_text(
            "✅ ¡Reservado!\n\n"
            f"📅 Día: {day}\n"
            f"🕒 Hora: {slot}\n"
            f"🏠 Piso: {piso}\n"
            f"👤 Nombre: {name}",
            reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=(
                "📢 Nueva reserva\n"
                f"📅 Día: {day}\n"
                f"🕒 Hora: {slot}\n"
                f"🏠 Piso: {piso}\n"
                f"👤 Nombre: {name}"
            ),
        )
        bookings.pop(chat_id, None)
        await send_main_menu(update, context)
        return

    # cualquier otra cosa → menú
    await send_main_menu(update, context)
    return


# -------------------------------------------------
# Construcción de la aplicación
# -------------------------------------------------
def build_application():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reservar", reservar))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^🛏️"), on_siesta_choice))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle
        )
    )
    app.add_handler(
        MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, unknown)
    )
    app.add_handler(
        ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER)
    )

    return app


# -------------------------------------------------
# Arranque con WEBHOOK
# -------------------------------------------------
if __name__ == "__main__":
    if not r:
        load_db_file()
    cleanup_old_bookings()

    application = build_application()

    # puerto que da Render
    port = int(os.environ.get("PORT", "8000"))

    # путь и полный URL вебхука
    webhook_path = f"/webhook/{BOT_TOKEN}"
    webhook_url = BASE_URL.rstrip("/") + webhook_path

    print(f"🌍 Starting webhook on 0.0.0.0:{port}")
    print(f"🔗 Webhook URL: {webhook_url}")

    # run_webhook сам поставит webhook в Telegram
    from telegram.ext import Application

if __name__ == "__main__":
    if not r:
        load_db_file()
    cleanup_old_bookings()

    application = build_application()

    # --- Устанавливаем webhook вручную ---
    async def set_webhook():
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_webhook(url=webhook_url)

    import asyncio
    asyncio.get_event_loop().run_until_complete(set_webhook())

    print(f"🌍 Starting webhook listener on 0.0.0.0:{port}")
    print(f"🔗 Webhook URL: {webhook_url}")

    # --- Запускаем встроенный webserver ---
    application.run_webhook(
        port=port,
        webhook_url=webhook_url,
        webhook_path=webhook_path,
    )
