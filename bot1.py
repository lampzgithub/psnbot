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
# LOGGING CONFIG (Moderate Verbosity)
# ---------------------------------------------------------

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO   # No debug spam from telebot
)
logger = logging.getLogger(__name__)

telebot_logger = logging.getLogger("telebot")
telebot_logger.setLevel(logging.WARNING)

logger.info("🔵 Bot is starting...")

# ---------------------------------------------------------
# LOAD ENV + INIT BOT
# ---------------------------------------------------------

dotenv.load_dotenv()
token = str(os.getenv("tk"))
bot = telebot.TeleBot(token=token)

ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}

def is_admin(uid):
    return uid in ADMIN_IDS

logger.info(f"Loaded Admins: {ADMIN_IDS}")

# Railway Persistent Path
TEMP_DIR = "/app/temp_files"
os.makedirs(TEMP_DIR, exist_ok=True)

# ---------------------------------------------------------
# USER TRACKING
# ---------------------------------------------------------

USER_TRACK_FILE = os.path.join(TEMP_DIR, "users.json")

if os.path.exists(USER_TRACK_FILE):
    with open(USER_TRACK_FILE, "r") as f:
        known_users = set(json.load(f))
else:
    known_users = set()

def save_users():
    with open(USER_TRACK_FILE, "w") as f:
        json.dump(list(known_users), f)

# ---------------------------------------------------------
# GLOBAL DUPLICATE REGISTRY
# ---------------------------------------------------------

GLOBAL_CODES_FILE = os.path.join(TEMP_DIR, "global_codes.json")

if os.path.exists(GLOBAL_CODES_FILE):
    with open(GLOBAL_CODES_FILE, "r") as f:
        GLOBAL_CODES = json.load(f)
else:
    GLOBAL_CODES = {}

# ---------------------------------------------------------
# BAN SYSTEM
# ---------------------------------------------------------

BANNED_FILE = os.path.join(TEMP_DIR, "banned.json")

if os.path.exists(BANNED_FILE):
    with open(BANNED_FILE, "r") as f:
        BANNED_USERS = set(json.load(f))
else:
    BANNED_USERS = set()

def save_bans():
    with open(BANNED_FILE, "w") as f:
        json.dump(list(BANNED_USERS), f)

def is_banned(uid):
    return uid in BANNED_USERS

# ---------------------------------------------------------
# BACKGROUND CLEANER
# ---------------------------------------------------------

DELETE_AFTER_SECONDS = 7 * 24 * 60 * 60
pending_user_codes = {}

def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for fname in os.listdir(TEMP_DIR):
                path = os.path.join(TEMP_DIR, fname)
                if os.path.isfile(path):
                    if now - os.path.getmtime(path) > DELETE_AFTER_SECONDS:
                        os.remove(path)
                        logger.info(f"Deleted old file: {fname}")
            time.sleep(3600)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            time.sleep(60)

threading.Thread(target=cleanup_old_files, daemon=True).start()

# ---------------------------------------------------------
# CODE PATTERNS
# ---------------------------------------------------------

CODE_PATTERNS = [
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b", re.I),
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{12}-[A-Z0-9]{6}\b", re.I),
]

code_pattern = r'\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b'
denom_pattern = r'₹\s?\d{1,4}(?:,\d{3})*(?:\.\d{2})?'
validity_pattern = r'Expires on (\d{2} \w{3} \d{4})'

def extract_data(text):
    results = []
    seen = set()
    for match in re.finditer(code_pattern, text):
        code = match.group()

        if code in seen:
            continue

        seen.add(code)
        start = max(0, match.start() - 100)
        end = min(len(text), match.end() + 100)
        snippet = text[start:end]

        denom_match = re.search(denom_pattern, snippet)
        valid_match = re.search(validity_pattern, snippet)

        denom = denom_match.group().strip() if denom_match else "N/A"
        valid = valid_match.group().strip() if valid_match else "N/A"

        results.append((code, denom, valid))
    return results


def generate_txt_by_denom(results):
    grouped = defaultdict(list)
    for code, denom, valid in results:
        grouped[denom].append((code, denom, valid))

    out_files = []
    timestamp = int(time.time())

    for denom, entries in grouped.items():
        number_match = re.search(r"\d+(?:,\d{3})*(?:\.\d{2})?", denom)
        number = number_match.group().replace(",", "").split(".")[0] if number_match else "unknown"

        filename = os.path.join(TEMP_DIR, f"{number}_{timestamp}.txt")
        with open(filename, "w") as f:
            for code, d, valid in entries:
                f.write(code + "\n")

        out_files.append((filename, len(entries)))
    return out_files

# ---------------------------------------------------------
# NORMALIZATION & DUPLICATE LOGIC
# ---------------------------------------------------------

def is_long_code(code):
    parts = code.split("-")
    return len(parts) == 4 and len(parts[2]) == 12 and len(parts[3]) == 6

def normalize_code(code):
    code = code.upper().strip()
    if is_long_code(code):
        cleaned = re.sub(r"-", "", code)
        return cleaned[:12]
    return code.replace("-", "")

def to_display(code):
    return normalize_code(code)

def is_duplicate_global(code):
    return normalize_code(code) in GLOBAL_CODES

def save_to_global_registry(code, uid):
    GLOBAL_CODES[normalize_code(code)] = uid
    with open(GLOBAL_CODES_FILE, "w") as f:
        json.dump(GLOBAL_CODES, f)

# ---------------------------------------------------------
# STORE USER CODES
# ---------------------------------------------------------

def store_user_codes(uid, code_tuples):
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    existing = set()
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            next(f, None)
            for line in f:
                existing.add(line.strip())

    new_lines = []
    for code, denom, valid in code_tuples:
        norm = normalize_code(code)
        if norm in GLOBAL_CODES:
            continue

        entry = f"{code},{denom},{valid}"
        if entry not in existing:
            new_lines.append(entry)
            save_to_global_registry(code, uid)

    if new_lines:
        write_header = not os.path.exists(filepath)
        with open(filepath, "a") as f:
            if write_header:
                f.write("CODE,DENOMINATION,VALIDITY\n")
            for line in new_lines:
                f.write(line + "\n")

# ---------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------

@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned from using this bot.")

    known_users.add(uid)
    save_users()

    bot.send_message(message.chat.id, "👋 Welcome! Send PSN codes or use /help")


@bot.message_handler(commands=['help'])
def help_cmd(message):
    if is_banned(message.from_user.id):
        return bot.send_message(message.chat.id, "❌ You are banned from using this bot.")

    bot.send_message(message.chat.id,
                     """📘 *Commands*
/w <pastebin> – Extract codes from Pastebin
/getstore – Download denomination-sorted TXT files
/clearstore – Delete your stored codes
/stats – View your stats
/remove <code> – Remove a saved code
You can also upload a PDF.
""", parse_mode="Markdown")

# ---------------------------------------------------------
# /getstore (User + Admin My Codes)
# ---------------------------------------------------------

@bot.message_handler(commands=['getstore'])
def cmd_getstore(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned from using this bot.")

    # Admin special menu
    if is_admin(uid):
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(
            telebot.types.InlineKeyboardButton("📁 My Codes", callback_data="adm_get_my"),
            telebot.types.InlineKeyboardButton("🌍 Global Codes", callback_data="adm_get_global")
        )
        return bot.send_message(message.chat.id, "Choose an option:", reply_markup=kb)

    # User normal mode
    return send_user_codes(uid, message.chat.id)


def send_user_codes(uid, chat_id):
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    if not os.path.exists(filepath):
        return bot.send_message(chat_id, "📂 You have no stored codes.")

    results = []
    with open(filepath, "r") as f:
        next(f)
        for line in f:
            parts = line.strip().split(",")
            if len(parts) == 3:
                results.append(parts)

    grouped = defaultdict(list)
    for code, denom, valid in results:
        grouped[denom].append(code)

    timestamp = int(time.time())

    for denom, codes in grouped.items():
        number_match = re.search(r"\d+(?:,\d{3})*", denom)
        number = number_match.group() if number_match else "unknown"

        out_path = os.path.join(TEMP_DIR, f"{number}_{uid}_{timestamp}.txt")

        with open(out_path, "w") as f:
            for c in codes:
                f.write(c + "\n")

        with open(out_path, "rb") as f:
            bot.send_document(chat_id, f, caption=f"{denom} — {len(codes)} codes")

# ---------------------------------------------------------
# ADMIN GETSTORE OPTIONS
# ---------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data in ["adm_get_my", "adm_get_global"])
def admin_get(call):
    uid = call.from_user.id
    if not is_admin(uid):
        return bot.answer_callback_query(call.id, "Not admin")

    if call.data == "adm_get_my":
        return send_user_codes(uid, call.message.chat.id)

    if call.data == "adm_get_global":
        return send_global_codes(call.message.chat.id)


def send_global_codes(chat_id):
    grouped = defaultdict(list)

    for uid in known_users:
        filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")
        if not os.path.exists(filepath):
            continue

        with open(filepath, "r") as f:
            next(f)
            for line in f:
                parts = line.strip().split(",")
                if len(parts) == 3:
                    code, denom, valid = parts
                    norm = normalize_code(code)
                    if norm in GLOBAL_CODES:
                        grouped[denom].append(code)

    timestamp = int(time.time())

    for denom, codes in grouped.items():
        number_match = re.search(r"\d+(?:,\d{3})*", denom)
        number = number_match.group() if number_match else "unknown"

        out_path = os.path.join(TEMP_DIR, f"global_{number}_{timestamp}.txt")
        with open(out_path, "w") as f:
            for c in codes:
                f.write(c + "\n")

        with open(out_path, "rb") as f:
            bot.send_document(chat_id, f, caption=f"🌍 {denom} — {len(codes)} global codes")

    with open(GLOBAL_CODES_FILE, "rb") as f:
        bot.send_document(chat_id, f, caption="🌍 Raw Global Registry (JSON)")

# ---------------------------------------------------------
# BAN / UNBAN / LIST BANNED
# ---------------------------------------------------------

@bot.message_handler(commands=['ban'])
def ban_cmd(message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Usage: /ban <id>")

    try:
        target = int(parts[1])
    except:
        return bot.send_message(message.chat.id, "Invalid ID.")

    if target in ADMIN_IDS:
        return bot.send_message(message.chat.id, "❌ Cannot ban an admin.")

    BANNED_USERS.add(target)
    save_bans()

    bot.send_message(message.chat.id, f"🚫 User {target} banned permanently.")


@bot.message_handler(commands=['unban'])
def unban_cmd(message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Usage: /unban <id>")

    try:
        target = int(parts[1])
    except:
        return bot.send_message(message.chat.id, "Invalid ID.")

    if target in BANNED_USERS:
        BANNED_USERS.remove(target)
        save_bans()
        return bot.send_message(message.chat.id, f"✅ User {target} unbanned.")

    bot.send_message(message.chat.id, "User was not banned.")


@bot.message_handler(commands=['banned'])
def banned_list(message):
    if not is_admin(message.from_user.id):
        return

    if not BANNED_USERS:
        return bot.send_message(message.chat.id, "No banned users.")

    out = "🚫 *Banned Users:*\n\n"
    for u in BANNED_USERS:
        out += f"• {u}\n"

    bot.send_message(message.chat.id, out, parse_mode="Markdown")

# ---------------------------------------------------------
# REMOVE CODE
# ---------------------------------------------------------

@bot.message_handler(commands=['remove'])
def remove_cmd(message):
    uid = message.from_user.id
    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Usage: /remove <code>")

    code = parts[1]
    norm = normalize_code(code)

    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")
    if not os.path.exists(filepath):
        return bot.send_message(message.chat.id, "You have no codes stored.")

    removed = False

    with open(filepath, "r") as f:
        lines = f.readlines()

    with open(filepath, "w") as f:
        for line in lines:
            if norm in normalize_code(line) and not removed:
                removed = True
                continue
            f.write(line)

    if removed:
        GLOBAL_CODES.pop(norm, None)
        with open(GLOBAL_CODES_FILE, "w") as f:
            json.dump(GLOBAL_CODES, f)
        return bot.send_message(message.chat.id, "✔ Code removed.")

    bot.send_message(message.chat.id, "❌ Code not found.")

# ---------------------------------------------------------
# CLEAR STORE
# ---------------------------------------------------------

@bot.message_handler(commands=['clearstore'])
def clearstore_cmd(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    if os.path.exists(filepath):
        # Remove from global registry
        with open(filepath, "r") as f:
            next(f)
            for line in f:
                code = line.split(",")[0]
                GLOBAL_CODES.pop(normalize_code(code), None)

        with open(GLOBAL_CODES_FILE, "w") as f:
            json.dump(GLOBAL_CODES, f)

        os.remove(filepath)
        return bot.send_message(message.chat.id, "🗑 All your codes deleted.")

    bot.send_message(message.chat.id, "You have no stored codes.")

# ---------------------------------------------------------
# STATS
# ---------------------------------------------------------

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    uid = message.from_user.id
    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")
    if not os.path.exists(filepath):
        return bot.send_message(message.chat.id, "📊 No stored codes.")

    stats = defaultdict(int)

    with open(filepath, "r") as f:
        next(f)
        for line in f:
            parts = line.strip().split(",")
            stats[parts[1]] += 1

    msg = "📊 *Your Stats:*\n\n"
    for denom, count in stats.items():
        msg += f"{denom}: **{count}**\n"

    msg += f"\nTotal: **{sum(stats.values())}**"

    bot.send_message(message.chat.id, msg, parse_mode="Markdown")

# ---------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------

@bot.message_handler(commands=['admin'])
def admin_cmd(message):
    if not is_admin(message.from_user.id):
        return

    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton("👥 Users Count", callback_data="adm_users"),
        telebot.types.InlineKeyboardButton("🔢 Total Codes", callback_data="adm_codes")
    )
    kb.add(telebot.types.InlineKeyboardButton("🗑 Wipe All", callback_data="adm_wipe"))
    kb.add(telebot.types.InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"))

    bot.send_message(message.chat.id, "🛠 *Admin Panel*", reply_markup=kb, parse_mode="Markdown")


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_handler(call):
    uid = call.from_user.id
    if not is_admin(uid):
        return bot.answer_callback_query(call.id, "Not admin")

    if call.data == "adm_users":
        bot.send_message(call.message.chat.id, f"👥 Users: {len(known_users)}")

    elif call.data == "adm_codes":
        bot.send_message(call.message.chat.id, f"🔢 Global Unique Codes: {len(GLOBAL_CODES)}")

    elif call.data == "adm_wipe":
        for fname in os.listdir(TEMP_DIR):
            if fname.startswith("stored_"):
                os.remove(os.path.join(TEMP_DIR, fname))
        GLOBAL_CODES.clear()
        with open(GLOBAL_CODES_FILE, "w") as f:
            json.dump(GLOBAL_CODES, f)
        bot.send_message(call.message.chat.id, "🗑 Wiped all user code data.")

    elif call.data == "adm_broadcast":
        bot.send_message(call.message.chat.id, "Use:\n/broadcast <message>")


@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if not is_admin(message.from_user.id):
        return

    msg = message.text.replace("/broadcast", "", 1).strip()
    if not msg:
        return bot.send_message(message.chat.id, "Usage: /broadcast <msg>")

    sent = 0
    for u in known_users:
        try:
            bot.send_message(u, f"📢 *Broadcast:*\n{msg}", parse_mode="Markdown")
            sent += 1
        except:
            pass

    bot.send_message(message.chat.id, f"Sent to {sent} users.")

# ---------------------------------------------------------
# AUTO-DETECT TEXT PSN CODE
# ---------------------------------------------------------

@bot.message_handler(func=lambda m: m.content_type == "text" and not m.text.startswith("/"))
def auto_detect(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    text = message.text
    found = set()

    for pat in CODE_PATTERNS:
        found |= set(pat.findall(text.upper()))

    if not found:
        return

    unique = []
    duplicates = []

    for code in found:
        if is_duplicate_global(code):
            duplicates.append(code)
        else:
            unique.append(code)

    if duplicates:
        bot.send_message(message.chat.id,
                         "⚠ Already saved:\n" +
                         "\n".join(f"• `{to_display(c)}`" for c in duplicates),
                         parse_mode="Markdown")

    if not unique:
        return

    pending_user_codes[uid] = unique

    kb = telebot.types.InlineKeyboardMarkup()
    for d in ["₹1000", "₹2000", "₹3000", "₹4000", "₹5000"]:
        kb.add(telebot.types.InlineKeyboardButton(d, callback_data=f"denom_{d}"))

    bot.send_message(message.chat.id,
                     "🎉 *New Codes Found!*\nChoose denomination:",
                     parse_mode="Markdown",
                     reply_markup=kb)

# ---------------------------------------------------------
# DENOMINATION SELECTION
# ---------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data.startswith("denom_"))
def denom_callback(call):
    uid = call.from_user.id

    if uid not in pending_user_codes:
        return bot.answer_callback_query(call.id, "No pending codes.")

    denom = call.data.replace("denom_", "")
    codes = pending_user_codes.pop(uid)

    store_user_codes(uid, [(c, denom, "N/A") for c in codes])

    bot.edit_message_text(f"✔ Saved {len(codes)} codes under {denom}.",
                          call.message.chat.id,
                          call.message.message_id)

# ---------------------------------------------------------
# PDF IMPORT
# ---------------------------------------------------------

@bot.message_handler(content_types=['document'])
def pdf_handler(message):
    uid = message.from_user.id
    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    fileinfo = bot.get_file(message.document.file_id)
    data = bot.download_file(fileinfo.file_path)

    pdf_path = os.path.join(TEMP_DIR, message.document.file_name)
    with open(pdf_path, "wb") as f:
        f.write(data)

    text = ""
    try:
        with fitz.open(pdf_path) as doc:
            for p in doc:
                text += p.get_text()
    except:
        return bot.send_message(message.chat.id, "❌ Invalid PDF.")

    os.remove(pdf_path)

    extracted = extract_data(text)
    if not extracted:
        return bot.send_message(message.chat.id, "⚠ No PSN codes found.")

    unique = []
    duplicates = []

    for code, denom, valid in extracted:
        if is_duplicate_global(code):
            duplicates.append(code)
        else:
            unique.append((code, denom, valid))

    if duplicates:
        bot.send_message(message.chat.id,
                         "⚠ Duplicate codes ignored:\n" +
                         "\n".join(f"`{to_display(c)}`" for c in duplicates),
                         parse_mode="Markdown")

    if not unique:
        return bot.send_message(message.chat.id, "⚠ No new unique codes found.")

    store_user_codes(uid, unique)

    # produce denom-based txt files
    files = generate_txt_by_denom(unique)

    for file_path, count in files:
        with open(file_path, "rb") as f:
            bot.send_document(message.chat.id, f, caption=f"Saved: {count} new codes")

# ---------------------------------------------------------
# PASTEBIN
# ---------------------------------------------------------

@bot.message_handler(commands=['w'])
def pastebin_cmd(message):
    uid = message.from_user.id
    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    url = message.text.replace("/w", "", 1).strip()

    if "pastebin.com" in url and "/raw/" not in url:
        url = f"https://pastebin.com/raw/{url.split('/')[-1]}"

    try:
        text = requests.get(url).text
    except:
        return bot.send_message(message.chat.id, "❌ Error loading Pastebin.")

    found = set()
    for pat in CODE_PATTERNS:
        found |= set(pat.findall(text.upper()))

    unique = []
    duplicates = []

    for code in found:
        if is_duplicate_global(code):
            duplicates.append(code)
        else:
            unique.append((code, "N/A", "N/A"))

    if duplicates:
        bot.send_message(message.chat.id,
                         "⚠ Already saved:\n" +
                         "\n".join(f"`{to_display(c)}`" for c in duplicates),
                         parse_mode="Markdown")

    if unique:
        store_user_codes(uid, unique)
        bot.send_message(message.chat.id, f"✔ Saved {len(unique)} new codes.")
    else:
        bot.send_message(message.chat.id, "⚠ No new codes found.")

# ---------------------------------------------------------
# START BOT
# ---------------------------------------------------------

logger.info("🔥 Polling started...")

while True:
    try:
        bot.polling(non_stop=True, timeout=30)
    except Exception as e:
        logger.error(f"Polling crashed: {e}")
        time.sleep(5)
