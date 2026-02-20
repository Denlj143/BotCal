import logging
import re
import sqlite3
import os
from datetime import date

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

# States: Add (grams)
G_NAME, G_GRAMS, G_KCAL100 = range(3)

# States: Add (kcal)
K_NAME, K_KCAL = range(3, 5)

# Button labels
BTN_ADD_GRAMS = "Add (grams)"
BTN_ADD_KCAL = "Add (kcal)"
BTN_LIST = "List"
BTN_TOTAL = "Total"
BTN_RESET = "Reset"
BTN_CANCEL = "Cancel"

BUTTONS = {BTN_ADD_GRAMS, BTN_ADD_KCAL, BTN_LIST, BTN_TOTAL, BTN_RESET, BTN_CANCEL}

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


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    )
    return cur.fetchone() is not None


def get_columns(con: sqlite3.Connection, table: str) -> set[str]:
    cur = con.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    rows = cur.fetchall()
    return {r[1] for r in rows}  # column name is at index 1


def db_init_and_migrate():
    """
    Creates tables if missing and migrates existing DB by adding missing columns.
    This prevents errors like: "table entries has no column named mode".
    """
    with db_connect() as con:
        cur = con.cursor()

        # user_day
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_day (
                user_id INTEGER PRIMARY KEY,
                day TEXT NOT NULL
            )
        """)

        # entries base (new schema)
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

        # Migration: if DB existed with older schema, ensure required columns exist.
        cols = get_columns(con, "entries")

        # Add missing columns safely
        if "grams" not in cols:
            cur.execute("ALTER TABLE entries ADD COLUMN grams REAL")
        if "kcal100" not in cols:
            cur.execute("ALTER TABLE entries ADD COLUMN kcal100 REAL")
        if "mode" not in cols:
            cur.execute("ALTER TABLE entries ADD COLUMN mode TEXT NOT NULL DEFAULT 'grams'")

        con.commit()


def today_str() -> str:
    return date.today().isoformat()


def ensure_day_is_today(user_id: int) -> str:
    t = today_str()
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT day FROM user_day WHERE user_id=?", (user_id,))
        row = cur.fetchone()

        if row is None:
            cur.execute("INSERT INTO user_day(user_id, day) VALUES(?, ?)", (user_id, t))
            con.commit()
            return t

        saved_day = row[0]
        if saved_day != t:
            cur.execute("DELETE FROM entries WHERE user_id=? AND day=?", (user_id, saved_day))
            cur.execute("UPDATE user_day SET day=? WHERE user_id=?", (t, user_id))
            con.commit()

        return t


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


def clear_today(user_id: int, day: str):
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
            [KeyboardButton(BTN_LIST), KeyboardButton(BTN_TOTAL)],
            [KeyboardButton(BTN_RESET), KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


# -----------------------
# Robust number parsing
# -----------------------
def extract_number(text: str) -> float:
    t = text.replace("\u00A0", " ").strip()
    m = re.search(r"\d+(?:[.,]\d+)?", t)
    if not m:
        raise ValueError("No number found")
    return float(m.group(0).replace(",", "."))


def is_button_text(text: str) -> bool:
    return text.strip() in BUTTONS


# -----------------------
# BASIC COMMANDS
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_day_is_today(user_id)
    await update.message.reply_text(
        "Calorie bot is ready.\nChoose Add (grams) or Add (kcal).",
        reply_markup=main_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Buttons:\n"
        "Add (grams) - name -> grams -> kcal per 100g\n"
        "Add (kcal)   - name -> kcal for the portion\n"
        "List         - today's items\n"
        "Total        - today's total kcal\n"
        "Reset        - clear today\n"
        "Cancel       - cancel current input",
        reply_markup=main_keyboard()
    )


# -----------------------
# FLOW 1: Add (grams)
# -----------------------
async def grams_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_day_is_today(user_id)
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
    day = ensure_day_is_today(user_id)

    name = context.user_data["name"]
    grams = context.user_data["grams"]

    kcal = add_entry_grams(user_id, day, name, grams, kcal100)
    total = get_total(user_id, day)

    await update.message.reply_text(
        f"Added: {name}\n"
        f"Portion: {grams:g} g\n"
        f"kcal/100g: {kcal100:g}\n"
        f"Portion kcal: {kcal:.1f}\n"
        f"Today total: {total:.1f}",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# -----------------------
# FLOW 2: Add (kcal)
# -----------------------
async def kcal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_day_is_today(user_id)
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
    day = ensure_day_is_today(user_id)

    name = context.user_data["name"]
    add_entry_kcal(user_id, day, name, kcal)
    total = get_total(user_id, day)

    await update.message.reply_text(
        f"Added: {name}\n"
        f"Portion kcal: {kcal:.1f}\n"
        f"Today total: {total:.1f}",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# -----------------------
# LIST / TOTAL / RESET
# -----------------------
async def list_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day = ensure_day_is_today(user_id)
    rows = get_entries(user_id, day)

    if not rows:
        await update.message.reply_text("No entries today.", reply_markup=main_keyboard())
        return

    lines = [f"Today list ({day}):"]
    total = 0.0

    for i, (name, grams, kcal100, kcal, mode) in enumerate(rows, start=1):
        total += float(kcal)
        if mode == "grams":
            lines.append(f"{i}) {name} - {grams:g} g, {kcal:.1f} kcal (100g: {kcal100:g})")
        else:
            lines.append(f"{i}) {name} - {kcal:.1f} kcal (manual)")

    lines.append(f"Total: {total:.1f}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())


async def total_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day = ensure_day_is_today(user_id)
    total = get_total(user_id, day)
    await update.message.reply_text(f"Today total ({day}): {total:.1f}", reply_markup=main_keyboard())


async def reset_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    day = ensure_day_is_today(user_id)
    clear_today(user_id, day)
    await update.message.reply_text(f"Cleared for today ({day}).", reply_markup=main_keyboard())


# -----------------------
# CANCEL + ERROR HANDLER
# -----------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Canceled.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Server error. Check console.")
    except Exception:
        pass


# -----------------------
# ROUTER (outside flows)
# -----------------------
async def route_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == BTN_ADD_GRAMS:
        return await grams_start(update, context)
    if text == BTN_ADD_KCAL:
        return await kcal_start(update, context)
    if text == BTN_LIST:
        return await list_today(update, context)
    if text == BTN_TOTAL:
        return await total_today(update, context)
    if text == BTN_RESET:
        return await reset_today(update, context)
    if text == BTN_CANCEL:
        return await cancel(update, context)

    await update.message.reply_text("Use buttons or /help.", reply_markup=main_keyboard())


# -----------------------
# MAIN
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

    # IMPORTANT ORDER
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(conv_grams)
    app.add_handler(conv_kcal)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_buttons))
    app.add_error_handler(on_error)

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":

    main()
