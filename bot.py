import logging
import re
import sqlite3
import os
import asyncio
from datetime import date, timedelta

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

# -----------------------
# SETTINGS
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "calories.db"

# States
G_NAME, G_GRAMS, G_KCAL100 = range(3)
K_NAME, K_KCAL = range(3, 5)
D_DATE = 5  # for manual date selection

# Buttons
BTN_ADD_GRAMS = "Add (grams)"
BTN_ADD_KCAL = "Add (kcal)"
BTN_LIST = "Today"
BTN_TOTAL = "Total"
BTN_RESET = "Reset"
BTN_CANCEL = "Cancel"
BTN_YESTERDAY = "Yesterday"
BTN_WEEK = "Last 7 days"
BTN_PICK_DATE = "Choose date"

BUTTONS = {
    BTN_ADD_GRAMS, BTN_ADD_KCAL, BTN_LIST, BTN_TOTAL,
    BTN_RESET, BTN_CANCEL, BTN_YESTERDAY, BTN_WEEK, BTN_PICK_DATE
}

# -----------------------
# LOGGING
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------
# DB
# -----------------------
def db_connect():
    return sqlite3.connect(DB_PATH)

def db_init():
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                day TEXT,
                name TEXT,
                grams REAL,
                kcal100 REAL,
                kcal REAL,
                mode TEXT
            )
        """)
        con.commit()

def today_str():
    return date.today().isoformat()

# -----------------------
# DB FUNCTIONS
# -----------------------
def add_entry_grams(user_id, day, name, grams, kcal100):
    kcal = grams * kcal100 / 100
    with db_connect() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO entries VALUES(NULL, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, day, name, grams, kcal100, kcal, "grams")
        )
        con.commit()
    return kcal

def add_entry_kcal(user_id, day, name, kcal):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO entries VALUES(NULL, ?, ?, ?, NULL, NULL, ?, ?)",
            (user_id, day, name, kcal, "kcal")
        )
        con.commit()

def get_total(user_id, day):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT COALESCE(SUM(kcal),0) FROM entries WHERE user_id=? AND day=?", (user_id, day))
        return float(cur.fetchone()[0])

def get_entries(user_id, day):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT name, kcal FROM entries WHERE user_id=? AND day=?", (user_id, day))
        return cur.fetchall()

# -----------------------
# UI
# -----------------------
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_ADD_GRAMS, BTN_ADD_KCAL],
            [BTN_LIST, BTN_YESTERDAY],
            [BTN_WEEK, BTN_PICK_DATE],
            [BTN_RESET, BTN_CANCEL]
        ],
        resize_keyboard=True
    )

# -----------------------
# BASIC COMMANDS
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Calorie Bot Ready.", reply_markup=main_keyboard())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Canceled.", reply_markup=main_keyboard())
    return ConversationHandler.END

# -----------------------
# ADD GRAMS
# -----------------------
async def grams_start(update, context):
    await update.message.reply_text("Enter product name:")
    return G_NAME

async def grams_name(update, context):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Enter grams:")
    return G_GRAMS

async def grams_grams(update, context):
    context.user_data["grams"] = float(update.message.text.replace(",", "."))
    await update.message.reply_text("Enter kcal per 100g:")
    return G_KCAL100

async def grams_kcal100(update, context):
    user = update.effective_user.id
    day = today_str()
    name = context.user_data["name"]
    grams = context.user_data["grams"]
    kcal100 = float(update.message.text.replace(",", "."))
    kcal = add_entry_grams(user, day, name, grams, kcal100)
    total = get_total(user, day)
    await update.message.reply_text(f"Added {kcal:.1f} kcal\nTotal today: {total:.1f}", reply_markup=main_keyboard())
    return ConversationHandler.END

# -----------------------
# ADD KCAL
# -----------------------
async def kcal_start(update, context):
    await update.message.reply_text("Enter product name:")
    return K_NAME

async def kcal_name(update, context):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Enter kcal:")
    return K_KCAL

async def kcal_value(update, context):
    user = update.effective_user.id
    day = today_str()
    kcal = float(update.message.text.replace(",", "."))
    add_entry_kcal(user, day, context.user_data["name"], kcal)
    total = get_total(user, day)
    await update.message.reply_text(f"Added {kcal:.1f} kcal\nTotal today: {total:.1f}", reply_markup=main_keyboard())
    return ConversationHandler.END

# -----------------------
# VIEWING
# -----------------------
async def today_total(update, context):
    day = today_str()
    total = get_total(update.effective_user.id, day)
    await update.message.reply_text(f"Today ({day}): {total:.1f}", reply_markup=main_keyboard())

async def yesterday_total(update, context):
    day = (date.today() - timedelta(days=1)).isoformat()
    total = get_total(update.effective_user.id, day)
    await update.message.reply_text(f"Yesterday ({day}): {total:.1f}", reply_markup=main_keyboard())

async def week_total(update, context):
    user = update.effective_user.id
    total = 0
    for i in range(7):
        d = (date.today() - timedelta(days=i)).isoformat()
        total += get_total(user, d)
    await update.message.reply_text(f"Last 7 days total: {total:.1f}", reply_markup=main_keyboard())

async def pick_date_start(update, context):
    await update.message.reply_text("Enter date YYYY-MM-DD:")
    return D_DATE

async def pick_date_value(update, context):
    try:
        day = update.message.text.strip()
        date.fromisoformat(day)
    except:
        await update.message.reply_text("Invalid format. Use YYYY-MM-DD")
        return D_DATE

    total = get_total(update.effective_user.id, day)
    rows = get_entries(update.effective_user.id, day)

    if not rows:
        text = f"No entries for {day}"
    else:
        text = f"{day}\n"
        for name, kcal in rows:
            text += f"{name}: {kcal:.1f}\n"
        text += f"\nTotal: {total:.1f}"

    await update.message.reply_text(text, reply_markup=main_keyboard())
    return ConversationHandler.END

# -----------------------
# MAIN
# -----------------------
async def main():
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    conv_grams = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_ADD_GRAMS}$"), grams_start)],
        states={
            G_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, grams_name)],
            G_GRAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, grams_grams)],
            G_KCAL100: [MessageHandler(filters.TEXT & ~filters.COMMAND, grams_kcal100)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel)],
    )

    conv_kcal = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_ADD_KCAL}$"), kcal_start)],
        states={
            K_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, kcal_name)],
            K_KCAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, kcal_value)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel)],
    )

    conv_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_PICK_DATE}$"), pick_date_start)],
        states={
            D_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pick_date_value)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST}$"), today_total))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_YESTERDAY}$"), yesterday_total))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_WEEK}$"), week_total))

    app.add_handler(conv_grams)
    app.add_handler(conv_kcal)
    app.add_handler(conv_date)

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
