import re
import telebot
import logging
import os
import dotenv
import fitz  # PyMuPDF
import requests
import time
import threading
import json
from collections import defaultdict

# ---------------------------------------------------------
#                  LOAD ENV + BASIC CONFIG
# ---------------------------------------------------------

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

dotenv.load_dotenv()
token = str(os.getenv("tk"))
bot = telebot.TeleBot(token=token)

# Multiple Admins
ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}

def is_admin(uid):
    return uid in ADMIN_IDS

# Persistent directory for Railway
TEMP_DIR = "/app/temp_files"

if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)

# User tracking
USER_TRACK_FILE = os.path.join(TEMP_DIR, "users.json")
if os.path.exists(USER_TRACK_FILE):
    with open(USER_TRACK_FILE, "r") as f:
        known_users = set(json.load(f))
else:
    known_users = set()

def save_users():
    with open(USER_TRACK_FILE, "w") as f:
        json.dump(list(known_users), f)

# Global duplicate registry
GLOBAL_CODES_FILE = os.path.join(TEMP_DIR, "global_codes.json")

if os.path.exists(GLOBAL_CODES_FILE):
    with open(GLOBAL_CODES_FILE, "r") as f:
        GLOBAL_CODES = json.load(f)
else:
    GLOBAL_CODES = {}  # normalized_code → user_id


# ---------------------------------------------------------
#                  CLEANUP THREAD
# ---------------------------------------------------------

DELETE_AFTER_SECONDS = 7 * 24 * 60 * 60  # 7 days
store_enabled_users = set()
pending_user_codes = {}  # temporary holding until denomination chosen


def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for filename in os.listdir(TEMP_DIR):
                file_path = os.path.join(TEMP_DIR, filename)
                if os.path.isfile(file_path):
                    if now - os.path.getmtime(file_path) > DELETE_AFTER_SECONDS:
                        os.remove(file_path)
                        logger.info(f"🗑 Deleted old file: {filename}")
            time.sleep(3600)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


# ---------------------------------------------------------
#                  CODE PATTERNS (short + long)
# ---------------------------------------------------------

CODE_PATTERNS = [
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b", re.I),
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{12}-[A-Z0-9]{6}\b", re.I),
]


def is_long_code(code: str):
    parts = code.split("-")
    return len(parts) == 4 and len(parts[2]) == 12 and len(parts[3]) == 6


def normalize_code(code: str):
    code = code.upper().strip()
    if is_long_code(code):
        cleaned = re.sub(r"-", "", code)
        return cleaned[:12]  # normalized long → short form
    return code.replace("-", "")  # normalized short code


# ---------------------------------------------------------
#                  GLOBAL DUPLICATE SYSTEM
# ---------------------------------------------------------

def is_duplicate_global(code: str):
    norm = normalize_code(code)
    return norm in GLOBAL_CODES


def save_to_global_registry(code: str, user_id: int):
    norm = normalize_code(code)
    GLOBAL_CODES[norm] = user_id
    with open(GLOBAL_CODES_FILE, "w") as f:
        json.dump(GLOBAL_CODES, f)


# ---------------------------------------------------------
#                     STORAGE UTILITIES
# ---------------------------------------------------------

def store_user_codes(user_id, code_tuples):
    filepath = os.path.join(TEMP_DIR, f"stored_{user_id}.txt")
    existing = set()

    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            for line in f:
                if "," in line and not line.startswith("CODE,"):
                    existing.add(line.strip())

    new_lines = []

    for code, denom, valid in code_tuples:
        norm = normalize_code(code)
        if norm in GLOBAL_CODES:
            continue  # STRICT MODE: Skip globally existing duplicates

        line = f"{code},{denom},{valid}"
        if line not in existing:
            new_lines.append(line)
            existing.add(line)
            save_to_global_registry(code, user_id)

    if new_lines:
        write_header = not os.path.exists(filepath)
        with open(filepath, "a") as f:
            if write_header:
                f.write("CODE,DENOMINATION,VALIDITY\n")
            for line in new_lines:
                f.write(line + "\n")


def generate_txt_by_denom(results):
    grouped = defaultdict(list)
    for c, d, v in results:
        grouped[d].append((c, d, v))

    files = []
    ts = int(time.time())

    for denom, items in grouped.items():
        num = re.sub(r"\D", "", denom) or "unknown"
        fname = os.path.join(TEMP_DIR, f"output_{num}_{ts}.txt")

        with open(fname, "w") as f:
            for code, _, _ in items:
                f.write(code + "\n")

        files.append((fname, len(items)))

    return files


def count_codes(filepath):
    if not os.path.exists(filepath):
        return {}
    denom_count = defaultdict(int)
    with open(filepath) as f:
        next(f)
        for line in f:
            parts = line.strip().split(",")
            if len(parts) == 3:
                denom_count[parts[1]] += 1
    return denom_count


def remove_code_from_file(filepath, code):
    if not os.path.exists(filepath):
        return False

    remaining = []
    removed = False

    norm = normalize_code(code)

    with open(filepath) as f:
        lines = f.readlines()

    with open(filepath, "w") as f:
        for line in lines:
            if norm in normalize_code(line) and not removed:
                removed = True
                continue
            f.write(line)

    if removed:
        if norm in GLOBAL_CODES:
            del GLOBAL_CODES[norm]
            with open(GLOBAL_CODES_FILE, "w") as f:
                json.dump(GLOBAL_CODES, f)

    return removed


# ---------------------------------------------------------
#                     COMMAND HANDLERS
# ---------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id
    known_users.add(uid)
    save_users()
    bot.send_message(message.chat.id, "👋 Welcome! Send PSN codes or use /help")


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id,
    """📘 *Commands*
/p <text> – Extract codes
/w <pastebin> – Extract from Pastebin
/store – Auto-store toggle
/getstore – Download stored codes
/clearstore – Delete your stored codes
/stats – Show statistics
/remove <code> – Remove a saved code
/admin – Admin control panel""",
    parse_mode="Markdown")


@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    uid = message.from_user.id
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    stats = count_codes(filepath)
    if not stats:
        return bot.send_message(message.chat.id, "📊 No stats available.")

    msg = "📊 *Your Code Stats:*\n\n"
    total = 0

    for denom, count in stats.items():
        msg += f"{denom} → {count}\n"
        total += count

    msg += f"\nTotal codes: *{total}*"

    bot.send_message(message.chat.id, msg, parse_mode="Markdown")


@bot.message_handler(commands=['remove'])
def cmd_remove(message):
    uid = message.from_user.id
    parts = message.text.split(" ", 1)

    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Usage: /remove <code>")

    code = parts[1].strip()
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    if remove_code_from_file(filepath, code):
        bot.send_message(message.chat.id, "✔ Code removed.")
    else:
        bot.send_message(message.chat.id, "❌ Code not found.")


# ---------------------------------------------------------
#                        ADMIN PANEL
# ---------------------------------------------------------

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return bot.send_message(message.chat.id, "❌ Not an admin.")

    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.add(
        telebot.types.InlineKeyboardButton("👥 Users", callback_data="admin_users"),
        telebot.types.InlineKeyboardButton("🔢 Total Codes", callback_data="admin_total")
    )
    keyboard.add(
        telebot.types.InlineKeyboardButton("🗑 Wipe All", callback_data="admin_wipe"),
    )
    keyboard.add(
        telebot.types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
    )

    bot.send_message(message.chat.id, "🛠 *Admin Panel*", reply_markup=keyboard, parse_mode="Markdown")


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def admin_handler(call):
    action = call.data

    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Not admin")

    if action == "admin_users":
        bot.send_message(call.message.chat.id, f"👥 Total users: {len(known_users)}")

    elif action == "admin_total":
        count = len(GLOBAL_CODES)
        bot.send_message(call.message.chat.id, f"🔢 Total unique codes stored globally: {count}")

    elif action == "admin_wipe":
        for fname in os.listdir(TEMP_DIR):
            if fname.startswith("stored_"):
                os.remove(os.path.join(TEMP_DIR, fname))

        GLOBAL_CODES.clear()
        with open(GLOBAL_CODES_FILE, "w") as f:
            json.dump(GLOBAL_CODES, f)

        bot.send_message(call.message.chat.id, "🗑 All data wiped.")

    elif action == "admin_broadcast":
        bot.send_message(call.message.chat.id, "Use:\n/broadcast <your message>")


@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Usage: /broadcast <text>")

    msg = parts[1]
    sent = 0

    for uid in known_users:
        try:
            bot.send_message(uid, f"📢 *Broadcast:*\n{msg}", parse_mode="Markdown")
            sent += 1
        except:
            pass

    bot.send_message(message.chat.id, f"Broadcast sent to {sent} users.")


# ---------------------------------------------------------
#                AUTO-DETECT NORMAL MESSAGES
# ---------------------------------------------------------

@bot.message_handler(func=lambda m: m.content_type == "text" and not m.text.startswith("/"))
def auto_extract(message):
    text = message.text
    found = set()

    for pat in CODE_PATTERNS:
        for match in pat.findall(text):
            found.add(match.upper())

    # Remove globally saved duplicates
    unique_found = []
    duplicates = []

    for code in found:
        if is_duplicate_global(code):
            duplicates.append(code)
        else:
            unique_found.append(code)

    if duplicates:
        bot.send_message(
            message.chat.id,
            "⚠ These codes already exist in the system and were ignored:\n" +
            "\n".join(f"• `{to_display(c)}`" for c in duplicates),
            parse_mode="Markdown"
        )

    if not unique_found:
        return

    pending_user_codes[message.from_user.id] = unique_found

    display = "\n".join(f"• `{to_display(c)}`" for c in unique_found)

    keyboard = telebot.types.InlineKeyboardMarkup()
    for amount in ["₹1000", "₹2000", "₹3000", "₹4000", "₹5000"]:
        keyboard.add(
            telebot.types.InlineKeyboardButton(amount, callback_data=f"denom_{amount}")
        )

    bot.send_message(
        message.chat.id,
        f"🎉 *New PSN Codes Detected:*\n\n{display}\n\nSelect denomination:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("denom_"))
def choose_denom(call):
    denom = call.data.replace("denom_", "")
    uid = call.from_user.id

    if uid not in pending_user_codes:
        return bot.answer_callback_query(call.id, "No pending codes.")
    codes = pending_user_codes.pop(uid)

    tuples = [(code, denom, "N/A") for code in codes]
    store_user_codes(uid, tuples)

    bot.edit_message_text(
        f"✔ Saved {len(codes)} codes under *{denom}*.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown"
    )


# ---------------------------------------------------------
#                   /p, /w, PDF handlers
# ---------------------------------------------------------

@bot.message_handler(commands=["p"])
def cmd_p(message):
    text = message.text.replace("/p", "", 1).strip()
    extracted = set()

    for pat in CODE_PATTERNS:
        extracted.update(pat.findall(text.upper()))

    unique = []
    for code in extracted:
        if not is_duplicate_global(code):
            unique.append((code, "N/A", "N/A"))

    if not unique:
        return bot.send_message(message.chat.id, "⚠ All codes are duplicates.")

    store_user_codes(message.from_user.id, unique)
    bot.send_message(message.chat.id, f"✔ Saved {len(unique)} codes.")


@bot.message_handler(commands=["w"])
def cmd_w(message):
    url = message.text.replace("/w", "", 1).strip()
    if "pastebin.com" in url and "/raw/" not in url:
        paste = url.split("/")[-1]
        url = f"https://pastebin.com/raw/{paste}"

    try:
        text = requests.get(url).text
    except:
        return bot.send_message(message.chat.id, "❌ Error fetching Pastebin.")

    extracted = set()
    for pat in CODE_PATTERNS:
        extracted.update(pat.findall(text.upper()))

    unique = []
    for code in extracted:
        if not is_duplicate_global(code):
            unique.append((code, "N/A", "N/A"))

    if not unique:
        return bot.send_message(message.chat.id, "⚠ All codes are duplicates.")

    store_user_codes(message.from_user.id, unique)
    bot.send_message(message.chat.id, f"✔ Saved {len(unique)} new codes.")


@bot.message_handler(content_types=['document'])
def pdf_handler(message):
    fileinfo = bot.get_file(message.document.file_id)
    data = bot.download_file(fileinfo.file_path)

    pdf_path = os.path.join(TEMP_DIR, message.document.file_name)
    with open(pdf_path, "wb") as f:
        f.write(data)

    text = ""
    try:
        with fitz.open(pdf_path) as doc:
            for page in doc:
                text += page.get_text()
    except:
        return bot.send_message(message.chat.id, "Invalid PDF.")

    extracted = set()
    for pat in CODE_PATTERNS:
        extracted.update(pat.findall(text.upper()))

    unique = []
    for code in extracted:
        if not is_duplicate_global(code):
            unique.append((code, "N/A", "N/A"))

    if not unique:
        return bot.send_message(message.chat.id, "⚠ All codes are duplicates.")

    store_user_codes(message.from_user.id, unique)
    bot.send_message(message.chat.id, f"✔ Saved {len(unique)} codes.")

    os.remove(pdf_path)


# ---------------------------------------------------------
#                  START BOT POLLING
# ---------------------------------------------------------

logger.info("Bot started.")

while True:
    try:
        bot.polling(non_stop=True, interval=0, timeout=20)
    except Exception as e:
        logger.error(f"Polling crashed: {e}")
        time.sleep(5)
