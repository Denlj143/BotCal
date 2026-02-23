import logging
import re
import sqlite3
import os
from datetime import date, timedelta

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

# -----------------------
# SETTINGS
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # set in hosting env variables
DB_PATH = "calories.db"

# States: Add (grams)
G_NAME, G_GRAMS, G_KCAL100 = range(3)

# States: Add (kcal)
K_NAME, K_KCAL = range(3, 5)

# States: pick date
D_DATE = 5

# Buttons
BTN_ADD_GRAMS = "Add (grams)"
BTN_ADD_KCAL = "Add (kcal)"
BTN_TODAY_LIST = "Today list"
BTN_TOTAL = "Total"
BTN_RESET = "Reset"
BTN_CANCEL = "Cancel"

BTN_YESTERDAY = "Yesterday"
BTN_WEEK = "Last 7 days"
BTN_PICK_DATE = "Choose date"

BUTTONS = {
    BTN_ADD_GRAMS, BTN_ADD_KCAL, BTN_TODAY_LIST, BTN_TOTAL, BTN_RESET, BTN_CANCEL,
    BTN_YESTERDAY, BTN_WEEK, BTN_PICK_DATE
}

# -----------------------
# LOGGING
# -----------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# -----------------------
# DB
# -----------------------
def db_connect():
    return sqlite3.connect(DB_PATH)


def get_columns(con: sqlite3.Connection, table: str) -> set[str]:
    cur = con.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}


def db_init_and_migrate():
    """
    Keeps all days (no auto-delete). Creates/migrates schema safely.
    """
    with db_connect() as con:
        cur = con.cursor()

        # entries
        cur.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                name TEXT NOT NULL,
                grams REAL,
                kcal100 REAL,
                kcal REAL NOT NULL,
                mode TEXT NOT NULL
            )
        """)
        con.commit()

        cols = get_columns(con, "entries")

        # Add missing columns (for older DBs)
        if "grams" not in cols:
            cur.execute("ALTER TABLE entries ADD COLUMN grams REAL")
        if "kcal100" not in cols:
            cur.execute("ALTER TABLE entries ADD COLUMN kcal100 REAL")
        if "mode" not in cols:
            cur.execute("ALTER TABLE entries ADD COLUMN mode TEXT NOT NULL DEFAULT 'grams'")

        con.commit()


def today_str() -> str:
    return date.today().isoformat()


def add_entry_grams(user_id: int, day: str, name: str, grams: float, kcal100: float) -> float:
    kcal = grams * kcal100 / 100.0
    with db_connect() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO entries(user_id, day, name, grams, kcal100, kcal, mode) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (user_id, day, name, grams, kcal100, kcal, "grams")
        )
        con.commit()
    return kcal


def add_entry_kcal(user_id: int, day: str, name: str, kcal: float) -> float:
    with db_connect() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO entries(user_id, day, name, grams, kcal100, kcal, mode) VALUES(?, ?, ?, NULL, NULL, ?, ?)",
            (user_id, day, name, kcal, "kcal")
        )
        con.commit()
    return kcal


def get_total(user_id: int, day: str) -> float:
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT COALESCE(SUM(kcal), 0) FROM entries WHERE user_id=? AND day=?", (user_id, day))
        return float(cur.fetchone()[0])


def get_entries(user_id: int, day: str):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT name, grams, kcal100, kcal, mode
            FROM entries
            WHERE user_id=? AND day=?
            ORDER BY id ASC
        """, (user_id, day))
        return cur.fetchall()


def clear_day(user_id: int, day: str):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM entries WHERE user_id=? AND day=?", (user_id, day))
        con.commit()


# -----------------------
# UI
# -----------------------
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_ADD_GRAMS), KeyboardButton(BTN_ADD_KCAL)],
            [KeyboardButton(BTN_TODAY_LIST), KeyboardButton(BTN_TOTAL)],
            [KeyboardButton(BTN_YESTERDAY), KeyboardButton(BTN_WEEK)],
            [KeyboardButton(BTN_PICK_DATE), KeyboardButton(BTN_RESET)],
            [KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


# -----------------------
# Helpers
# -----------------------
def extract_number(text: str) -> float:
    """
    Extract first number from text, supports comma/dot decimals, ignores hidden spaces.
    """
    t = text.replace("\u00A0", " ").strip()
    m = re.search(r"\d+(?:[.,]\d+)?", t)
    if not m:
        raise ValueError("No number found")
    return float(m.group(0).replace(",", "."))


def is_button_text(text: str) -> bool:
    return text.strip() in BUTTONS


def format_day_report(day: str, rows, total: float) -> str:
    if not rows:
        return f"No entries for {day}\nTotal: 0.0"

    lines = [f"Entries for {day}:"]
    for i, (name, grams, kcal100, kcal, mode) in enumerate(rows, start=1):
        if mode == "grams":
            lines.append(f"{i}) {name} - {grams:g} g, {kcal:.1f} kcal (100g: {kcal100:g})")
        else:
            lines.append(f"{i}) {name} - {kcal:.1f} kcal (manual)")
    lines.append(f"Total: {total:.1f}")
    return "\n".join(lines)


# -----------------------
# BASIC COMMANDS
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Calorie bot is ready.",
        reply_markup=main_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Buttons:\n"
        "Add (grams) - name -> grams -> kcal per 100g\n"
        "Add (kcal)  - name -> kcal for the portion\n"
        "Today list  - list for today\n"
        "Total       - today's total\n"
        "Yesterday   - yesterday's total\n"
        "Last 7 days - sum for last 7 days\n"
        "Choose date - pick any date YYYY-MM-DD\n"
        "Reset       - clear today's entries\n"
        "Cancel      - cancel current input",
        reply_markup=main_keyboard()
    )


# -----------------------
# CANCEL (works everywhere)
# -----------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Canceled.", reply_markup=main_keyboard())
    return ConversationHandler.END


# -----------------------
# FLOW 1: Add (grams)
# -----------------------
async def grams_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Enter product name:")
    return G_NAME


async def grams_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or is_button_text(name):
        await update.message.reply_text("Enter product name (text):")
        return G_NAME

    context.user_data["name"] = name
    await update.message.reply_text("Enter grams (e.g. 120):")
    return G_GRAMS


async def grams_grams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if is_button_text(txt):
        await update.message.reply_text("Enter a number (grams) or press Cancel.")
        return G_GRAMS

    try:
        grams = extract_number(txt)
        if grams <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Grams must be a number > 0. Example: 120")
        return G_GRAMS

    context.user_data["grams"] = grams
    await update.message.reply_text("Enter kcal per 100g (e.g. 89):")
    return G_KCAL100


async def grams_kcal100(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if is_button_text(txt):
        await update.message.reply_text("Enter a number (kcal per 100g) or press Cancel.")
        return G_KCAL100

    try:
        kcal100 = extract_number(txt)
        if kcal100 < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("kcal/100g must be a number >= 0. Example: 89")
        return G_KCAL100

    user_id = update.effective_user.id
    day = today_str()

    name = context.user_data["name"]
    grams = context.user_data["grams"]

    kcal = add_entry_grams(user_id, day, name, grams, kcal100)
    total = get_total(user_id, day)

    await update.message.reply_text(
        f"Added: {name}\nPortion kcal: {kcal:.1f}\nToday total: {total:.1f}",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# -----------------------
# FLOW 2: Add (kcal)
# -----------------------
async def kcal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Enter product name:")
    return K_NAME


async def kcal_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or is_button_text(name):
        await update.message.reply_text("Enter product name (text):")
        return K_NAME

    context.user_data["name"] = name
    await update.message.reply_text("Enter portion kcal (e.g. 250):")
    return K_KCAL


async def kcal_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if is_button_text(txt):
        await update.message.reply_text("Enter a number (portion kcal) or press Cancel.")
        return K_KCAL

    try:
        kcal = extract_number(txt)
        if kcal < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("kcal must be a number >= 0. Example: 250")
        return K_KCAL

    user_id = update.effective_user.id
    day = today_str()

    name = context.user_data["name"]
    add_entry_kcal(user_id, day, name, kcal)
    total = get_total(user_id, day)

    await update.message.reply_text(
        f"Added: {name}\nPortion kcal: {kcal:.1f}\nToday total: {total:.1f}",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# -----------------------
# VIEW: today list / total / yesterday / week / pick date
# -----------------------
async def today_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day = today_str()
    rows = get_entries(user_id, day)
    total = get_total(user_id, day)
    await update.message.reply_text(format_day_report(day, rows, total), reply_markup=main_keyboard())


async def total_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day = today_str()
    total = get_total(user_id, day)
    await update.message.reply_text(f"Today total ({day}): {total:.1f}", reply_markup=main_keyboard())


async def yesterday_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day = (date.today() - timedelta(days=1)).isoformat()
    total = get_total(user_id, day)
    await update.message.reply_text(f"Yesterday total ({day}): {total:.1f}", reply_markup=main_keyboard())


async def week_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total = 0.0
    today = date.today()
    for i in range(7):
        day = (today - timedelta(days=i)).isoformat()
        total += get_total(user_id, day)

    await update.message.reply_text(f"Total for last 7 days: {total:.1f}", reply_markup=main_keyboard())


async def reset_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day = today_str()
    clear_day(user_id, day)
    await update.message.reply_text(f"Cleared entries for today ({day}).", reply_markup=main_keyboard())


# Pick date conversation
async def pick_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Enter date in format YYYY-MM-DD:")
    return D_DATE


async def pick_date_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if is_button_text(txt):
        await update.message.reply_text("Please enter a date (YYYY-MM-DD) or press Cancel.")
        return D_DATE

    try:
        picked = date.fromisoformat(txt).isoformat()
    except ValueError:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD (example: 2026-02-23)")
        return D_DATE

    user_id = update.effective_user.id
    rows = get_entries(user_id, picked)
    total = get_total(user_id, picked)

    await update.message.reply_text(format_day_report(picked, rows, total), reply_markup=main_keyboard())
    return ConversationHandler.END


# -----------------------
# ERROR HANDLER
# -----------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Server error. Check logs.")
    except Exception:
        pass


# -----------------------
# MAIN (SYNC - avoids asyncio loop closing issues on Python 3.13+)
# -----------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in environment variables.")

    db_init_and_migrate()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_grams = ConversationHandler(
        entry_points=[
            CommandHandler("add_grams", grams_start),
            MessageHandler(filters.Regex(rf"^{re.escape(BTN_ADD_GRAMS)}$"), grams_start),
        ],
        states={
            G_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, grams_name)],
            G_GRAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, grams_grams)],
            G_KCAL100: [MessageHandler(filters.TEXT & ~filters.COMMAND, grams_kcal100)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(rf"^{re.escape(BTN_CANCEL)}$"), cancel),
        ],
        allow_reentry=True,
    )

    conv_kcal = ConversationHandler(
        entry_points=[
            CommandHandler("add_kcal", kcal_start),
            MessageHandler(filters.Regex(rf"^{re.escape(BTN_ADD_KCAL)}$"), kcal_start),
        ],
        states={
            K_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, kcal_name)],
            K_KCAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, kcal_value)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(rf"^{re.escape(BTN_CANCEL)}$"), cancel),
        ],
        allow_reentry=True,
    )

    conv_pick_date = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(rf"^{re.escape(BTN_PICK_DATE)}$"), pick_date_start),
            CommandHandler("date", pick_date_start),
        ],
        states={
            D_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pick_date_value)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(rf"^{re.escape(BTN_CANCEL)}$"), cancel),
        ],
        allow_reentry=True,
    )

    # Handlers order
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # Cancel works everywhere
    app.add_handler(MessageHandler(filters.Regex(rf"^{re.escape(BTN_CANCEL)}$"), cancel))

    # View / utility buttons
    app.add_handler(MessageHandler(filters.Regex(rf"^{re.escape(BTN_TODAY_LIST)}$"), today_list))
    app.add_handler(MessageHandler(filters.Regex(rf"^{re.escape(BTN_TOTAL)}$"), total_today))
    app.add_handler(MessageHandler(filters.Regex(rf"^{re.escape(BTN_YESTERDAY)}$"), yesterday_total))
    app.add_handler(MessageHandler(filters.Regex(rf"^{re.escape(BTN_WEEK)}$"), week_total))
    app.add_handler(MessageHandler(filters.Regex(rf"^{re.escape(BTN_RESET)}$"), reset_today))

    # Conversations
    app.add_handler(conv_grams)
    app.add_handler(conv_kcal)
    app.add_handler(conv_pick_date)

    app.add_error_handler(on_error)

    print("Bot is running...")
    app.run_polling()  # DO NOT wrap in asyncio.run on Python 3.13+


if __name__ == "__main__":
    main()
