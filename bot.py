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
# LOGGING CONFIG (VERBOSE MODE)
# ---------------------------------------------------------

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.DEBUG  # Enable INFO + DEBUG
)
logger = logging.getLogger(__name__)

logger.info("🔵 Bot is starting with VERBOSE logging...")

# ---------------------------------------------------------
# LOAD ENV + INIT BOT
# ---------------------------------------------------------

dotenv.load_dotenv()
token = str(os.getenv("tk"))
bot = telebot.TeleBot(token=token)

ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}

def is_admin(uid):
    return uid in ADMIN_IDS

logger.debug(f"Loaded admin IDs: {ADMIN_IDS}")

# Railway Persistent Path
TEMP_DIR = "/app/temp_files"
os.makedirs(TEMP_DIR, exist_ok=True)
logger.debug(f"Persistent storage path: {TEMP_DIR}")

# Track known users
USER_TRACK_FILE = os.path.join(TEMP_DIR, "users.json")

if os.path.exists(USER_TRACK_FILE):
    with open(USER_TRACK_FILE, "r") as f:
        known_users = set(json.load(f))
else:
    known_users = set()

def save_users():
    with open(USER_TRACK_FILE, "w") as f:
        json.dump(list(known_users), f)
    logger.debug("User list saved to disk.")

logger.debug(f"Loaded known users: {known_users}")

# Global Duplicate Registry
GLOBAL_CODES_FILE = os.path.join(TEMP_DIR, "global_codes.json")

if os.path.exists(GLOBAL_CODES_FILE):
    with open(GLOBAL_CODES_FILE, "r") as f:
        GLOBAL_CODES = json.load(f)
else:
    GLOBAL_CODES = {}

logger.debug(f"Loaded global codes registry with {len(GLOBAL_CODES)} entries.")

# ---------------------------------------------------------
# CLEANUP THREAD
# ---------------------------------------------------------

DELETE_AFTER_SECONDS = 7 * 24 * 60 * 60  # 7 days
pending_user_codes = {}

def cleanup_old_files():
    logger.info("🧹 Cleanup thread started")
    while True:
        try:
            now = time.time()
            for fname in os.listdir(TEMP_DIR):
                path = os.path.join(TEMP_DIR, fname)
                if os.path.isfile(path):
                    age = now - os.path.getmtime(path)
                    if age > DELETE_AFTER_SECONDS:
                        logger.info(f"Deleting old file: {fname}")
                        os.remove(path)
            time.sleep(3600)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            time.sleep(60)

threading.Thread(target=cleanup_old_files, daemon=True).start()

# ---------------------------------------------------------
# CODE PATTERNS & NORMALIZATION
# ---------------------------------------------------------

CODE_PATTERNS = [
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b", re.I),
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{12}-[A-Z0-9]{6}\b", re.I),
]

def is_long_code(code):
    parts = code.split("-")
    return len(parts) == 4 and len(parts[2]) == 12 and len(parts[3]) == 6

def normalize_code(code):
    code = code.upper().strip()
    if is_long_code(code):
        cleaned = re.sub(r"-", "", code)
        short = cleaned[:12]
        logger.debug(f"Normalized long code {code} → {short}")
        return short
    short = code.replace("-", "")
    logger.debug(f"Normalized short code {code} → {short}")
    return short

def to_display(code):
    code = code.upper().strip()
    if is_long_code(code):
        cleaned = re.sub(r"-", "", code)
        disp = cleaned[:12]
        return disp
    return code.replace("-", "")

# ---------------------------------------------------------
# GLOBAL DUPLICATE BLOCKING
# ---------------------------------------------------------

def is_duplicate_global(code):
    norm = normalize_code(code)
    dup = norm in GLOBAL_CODES
    logger.debug(f"Duplicate check for {norm}: {dup}")
    return dup

def save_to_global_registry(code, uid):
    norm = normalize_code(code)
    GLOBAL_CODES[norm] = uid
    with open(GLOBAL_CODES_FILE, "w") as f:
        json.dump(GLOBAL_CODES, f)
    logger.info(f"Global registry updated: {norm} saved for user {uid}")

# ---------------------------------------------------------
# FILE-BASED STORAGE
# ---------------------------------------------------------

def store_user_codes(uid, code_tuples):
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")
    logger.info(f"Storing {len(code_tuples)} codes for user {uid}")

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
            logger.warning(f"Duplicate blocked globally: {code}")
            continue

        entry = f"{code},{denom},{valid}"
        if entry not in existing:
            logger.debug(f"Adding new user code: {entry}")
            new_lines.append(entry)
            save_to_global_registry(code, uid)

    if new_lines:
        write_header = not os.path.exists(filepath)
        with open(filepath, "a") as f:
            if write_header:
                f.write("CODE,DENOMINATION,VALIDITY\n")
            for line in new_lines:
                f.write(line + "\n")
        logger.info(f"Saved {len(new_lines)} new codes for user {uid}")
    else:
        logger.info("No new codes to save (all duplicates).")

def count_codes(filepath):
    if not os.path.exists(filepath):
        return {}
    stats = defaultdict(int)
    with open(filepath) as f:
        next(f)
        for line in f:
            parts = line.strip().split(",")
            if len(parts) == 3:
                stats[parts[1]] += 1
    logger.debug(f"Counted stats: {stats}")
    return stats

def remove_code_from_file(filepath, code):
    if not os.path.exists(filepath):
        return False

    norm = normalize_code(code)
    removed = False
    logger.debug(f"Attempting to remove {norm} from {filepath}")

    with open(filepath, "r") as f:
        lines = f.readlines()

    with open(filepath, "w") as f:
        for line in lines:
            if norm in normalize_code(line) and not removed:
                removed = True
                logger.info(f"Removed code {norm} from user file")
                continue
            f.write(line)

    if removed and norm in GLOBAL_CODES:
        del GLOBAL_CODES[norm]
        with open(GLOBAL_CODES_FILE, "w") as f:
            json.dump(GLOBAL_CODES, f)
        logger.info(f"Removed {norm} from global registry")

    return removed

# ---------------------------------------------------------
# COMMAND: /start
# ---------------------------------------------------------

@bot.message_handler(commands=['start'])
def start_cmd(message):
    uid = message.from_user.id
    known_users.add(uid)
    save_users()
    logger.info(f"User {uid} started the bot.")
    bot.send_message(message.chat.id, "👋 Welcome! Send PSN codes or use /help")

# ---------------------------------------------------------
# COMMAND: /help
# ---------------------------------------------------------

@bot.message_handler(commands=['help'])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        """📘 *Commands*
/w <pastebin> – Extract codes from Pastebin
/getstore – Download your stored codes
/clearstore – Delete your stored codes
/stats – View your statistics
/remove <code> – Remove one saved code
(Everything else works automatically)
""",
        parse_mode="Markdown"
    )


# ---------------------------------------------------------
# COMMAND: /getstore (User’s Own Data Only)
# ---------------------------------------------------------

@bot.message_handler(commands=['getstore'])
def cmd_getstore(message):
    uid = message.from_user.id
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    logger.info(f"[GETSTORE] User {uid} requested their stored codes.")

    if not os.path.exists(filepath):
        bot.send_message(message.chat.id, "📂 You have no stored codes.")
        return

    # Send full raw file
    with open(filepath, "rb") as f:
        bot.send_document(message.chat.id, f, caption="📦 Your stored codes (FULL FILE)")
        logger.debug(f"[GETSTORE] Sent raw stored file for user {uid}")

    # Prepare denom-separated output files
    results = []
    with open(filepath, "r") as f:
        next(f)  # skip header
        for line in f:
            parts = line.strip().split(",")
            if len(parts) == 3:
                results.append(tuple(parts))

    files = defaultdict(list)
    for code, denom, valid in results:
        files[denom].append(code)

    # Create TXT files for each denomination
    for denom, codes in files.items():
        normalized = re.sub(r"\D", "", denom) or "unknown"
        out_path = os.path.join(TEMP_DIR, f"output_{normalized}_{uid}.txt")

        with open(out_path, "w") as f:
            for code in codes:
                f.write(code + "\n")

        with open(out_path, "rb") as f:
            bot.send_document(message.chat.id, f, caption=f"{denom} — {len(codes)} codes")
            logger.debug(f"[GETSTORE] Sent denom file {out_path} for user {uid}")


# ---------------------------------------------------------
# COMMAND: /clearstore
# ---------------------------------------------------------

@bot.message_handler(commands=['clearstore'])
def cmd_clearstore(message):
    uid = message.from_user.id
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    logger.info(f"[CLEARSTORE] User {uid} requested deletion of stored codes.")

    if os.path.exists(filepath):
        # Remove user-specific codes from GLOBAL registry
        try:
            with open(filepath, "r") as f:
                next(f)
                for line in f:
                    code = line.split(",")[0]
                    norm = normalize_code(code)
                    if norm in GLOBAL_CODES:
                        del GLOBAL_CODES[norm]
        except:
            pass

        with open(GLOBAL_CODES_FILE, "w") as f:
            json.dump(GLOBAL_CODES, f)

        os.remove(filepath)
        bot.send_message(message.chat.id, "🗑 Your stored codes were deleted.")
        logger.info(f"[CLEARSTORE] User {uid} code file deleted.")

    else:
        bot.send_message(message.chat.id, "📂 You have no stored codes.")


# ---------------------------------------------------------
# COMMAND: /remove <code>
# ---------------------------------------------------------

@bot.message_handler(commands=['remove'])
def cmd_remove(message):
    uid = message.from_user.id
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Usage: /remove <code>")

    code = parts[1].strip()

    logger.info(f"[REMOVE] User {uid} attempting to remove code {code}")

    if remove_code_from_file(filepath, code):
        bot.send_message(message.chat.id, "✔ Code removed.")
        logger.info(f"[REMOVE] Code {code} removed for user {uid}")
    else:
        bot.send_message(message.chat.id, "❌ Code not found.")
        logger.warning(f"[REMOVE] Code {code} not found for user {uid}")


# ---------------------------------------------------------
# COMMAND: /stats
# ---------------------------------------------------------

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    uid = message.from_user.id
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")
    logger.info(f"[STATS] User {uid} requested stats.")

    stats = count_codes(filepath)

    if not stats:
        return bot.send_message(message.chat.id, "📊 You have no stored codes.")

    total = sum(stats.values())
    out = "📊 *Your Stats:*\n\n"

    for denom, count in stats.items():
        out += f"{denom}: **{count}** codes\n"

    out += f"\nTotal codes saved: **{total}**"

    bot.send_message(message.chat.id, out, parse_mode="Markdown")


# ---------------------------------------------------------
# ADMIN PANEL (Hidden)
# ---------------------------------------------------------

@bot.message_handler(commands=['admin'])
def admin_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid):
        logger.warning(f"⚠ Unauthorized admin attempt by {uid}")
        return

    logger.info(f"[ADMIN] Admin {uid} opened panel.")

    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton("👥 Users Count", callback_data="adm_users"),
        telebot.types.InlineKeyboardButton("🔢 Total Codes", callback_data="adm_codes")
    )
    kb.add(
        telebot.types.InlineKeyboardButton("🗑 Wipe All", callback_data="adm_wipe")
    )
    kb.add(
        telebot.types.InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")
    )

    bot.send_message(message.chat.id, "🛠 *Admin Panel*", reply_markup=kb, parse_mode="Markdown")


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_handler(call):
    uid = call.from_user.id
    if not is_admin(uid):
        return bot.answer_callback_query(call.id, "Not admin")

    action = call.data
    logger.info(f"[ADMIN ACTION] {uid} triggered: {action}")

    if action == "adm_users":
        bot.send_message(call.message.chat.id, f"👥 Total users: {len(known_users)}")

    elif action == "adm_codes":
        bot.send_message(call.message.chat.id, f"🔢 Total unique global codes: {len(GLOBAL_CODES)}")

    elif action == "adm_wipe":
        logger.warning("⚠ ADMIN WIPED ALL DATA!")
        for fname in os.listdir(TEMP_DIR):
            if fname.startswith("stored_"):
                os.remove(os.path.join(TEMP_DIR, fname))

        GLOBAL_CODES.clear()
        with open(GLOBAL_CODES_FILE, "w") as f:
            json.dump(GLOBAL_CODES, f)

        bot.send_message(call.message.chat.id, "🗑 All user data wiped.")

    elif action == "adm_broadcast":
        bot.send_message(call.message.chat.id, "Send broadcast with:\n/broadcast <message>")


@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    msg = message.text.replace("/broadcast", "", 1).strip()

    if not msg:
        return bot.send_message(message.chat.id, "Usage: /broadcast <message>")

    logger.info(f"[BROADCAST] Admin {uid} broadcasting: {msg}")

    sent = 0
    for user in known_users:
        try:
            bot.send_message(user, f"📢 *Broadcast:*\n{msg}", parse_mode="Markdown")
            sent += 1
        except:
            pass

    bot.send_message(message.chat.id, f"Sent to {sent} users.")


# ---------------------------------------------------------
# AUTO-DETECT CODES IN NORMAL MESSAGES
# ---------------------------------------------------------

@bot.message_handler(func=lambda m: m.content_type == "text" and not m.text.startswith("/"))
def auto_detect(message):
    uid = message.from_user.id
    text = message.text

    logger.info(f"[DETECT] Received message from {uid}: {text[:50]}...")

    found = set()
    for pat in CODE_PATTERNS:
        found.update(pat.findall(text.upper()))

    if not found:
        logger.debug("[DETECT] No codes found.")
        return

    logger.info(f"[DETECT] Found codes: {found}")

    unique = []
    duplicates = []

    for code in found:
        if is_duplicate_global(code):
            duplicates.append(code)
        else:
            unique.append(code)

    if duplicates:
        bot.send_message(
            message.chat.id,
            "⚠ Already saved (ignored):\n" + "\n".join(f"• `{to_display(c)}`" for c in duplicates),
            parse_mode="Markdown"
        )
        logger.info(f"[DETECT] Duplicates ignored: {duplicates}")

    if not unique:
        return

    pending_user_codes[uid] = unique
    logger.debug(f"[PENDING] User {uid} pending codes: {unique}")

    kb = telebot.types.InlineKeyboardMarkup()
    for amt in ["₹1000", "₹2000", "₹3000", "₹4000", "₹5000"]:
        kb.add(telebot.types.InlineKeyboardButton(amt, callback_data=f"denom_{amt}"))

    display = "\n".join(f"• `{to_display(c)}`" for c in unique)

    bot.send_message(
        message.chat.id,
        f"🎉 *New PSN Codes Detected!*\n\n{display}\n\nChoose denomination:",
        parse_mode="Markdown",
        reply_markup=kb
    )


# ---------------------------------------------------------
# DENOMINATION SELECTION
# ---------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data.startswith("denom_"))
def denom_pick(call):
    uid = call.from_user.id
    denom = call.data.replace("denom_", "")

    logger.info(f"[DENOM] User {uid} chose denomination: {denom}")

    if uid not in pending_user_codes:
        logger.warning(f"[DENOM] No pending codes for {uid}")
        return bot.answer_callback_query(call.id, "No pending codes.")

    codes = pending_user_codes.pop(uid)
    store_user_codes(uid, [(code, denom, "N/A") for code in codes])

    bot.edit_message_text(
        f"✔ Saved {len(codes)} codes under *{denom}*.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown"
    )


# ---------------------------------------------------------
# PDF EXTRACTION
# ---------------------------------------------------------

@bot.message_handler(content_types=['document'])
def pdf_extract(message):
    uid = message.from_user.id
    logger.info(f"[PDF] PDF received from {uid}")

    fileinfo = bot.get_file(message.document.file_id)
    data = bot.download_file(fileinfo.file_path)

    pdf_path = os.path.join(TEMP_DIR, message.document.file_name)
    with open(pdf_path, "wb") as f:
        f.write(data)
    logger.debug(f"[PDF] Saved temp PDF at {pdf_path}")

    text = ""
    try:
        with fitz.open(pdf_path) as doc:
            for page in doc:
                text += page.get_text()
    except:
        logger.error("[PDF] Invalid PDF file")
        return bot.send_message(message.chat.id, "❌ Invalid PDF")

    found = set()
    for pat in CODE_PATTERNS:
        found.update(pat.findall(text.upper()))

    logger.info(f"[PDF] Extracted codes: {found}")

    unique = []
    duplicates = []

    for code in found:
        if is_duplicate_global(code):
            duplicates.append(code)
        else:
            unique.append((code, "N/A", "N/A"))

    if duplicates:
        bot.send_message(
            message.chat.id,
            "⚠ Duplicate codes ignored from PDF:\n" +
            "\n".join(f"• `{to_display(c)}`" for c in duplicates),
            parse_mode="Markdown"
        )

    if unique:
        store_user_codes(uid, unique)
        bot.send_message(message.chat.id, f"✔ Saved {len(unique)} new codes.")
    else:
        bot.send_message(message.chat.id, "⚠ No new codes found.")

    os.remove(pdf_path)
    logger.debug(f"[PDF] Removed temp PDF: {pdf_path}")


# ---------------------------------------------------------
# /w – Pastebin Code Extraction
# ---------------------------------------------------------

@bot.message_handler(commands=['w'])
def pastebin_extract(message):
    uid = message.from_user.id
    logger.info(f"[PASTEBIN] Request from {uid}: {message.text}")

    url = message.text.replace("/w", "", 1).strip()

    if "pastebin.com" in url and "/raw/" not in url:
        paste = url.split("/")[-1]
        url = f"https://pastebin.com/raw/{paste}"

    try:
        text = requests.get(url).text
        logger.debug("[PASTEBIN] Content fetched successfully.")
    except:
        logger.error("[PASTEBIN] Fetch failed.")
        return bot.send_message(message.chat.id, "❌ Error fetching Pastebin link.")

    found = set()
    for pat in CODE_PATTERNS:
        found.update(pat.findall(text.upper()))

    logger.info(f"[PASTEBIN] Extracted codes: {found}")

    unique = []
    duplicates = []

    for code in found:
        if is_duplicate_global(code):
            duplicates.append(code)
        else:
            unique.append((code, "N/A", "N/A"))

    if duplicates:
        bot.send_message(
            message.chat.id,
            "⚠ Already saved (ignored):\n" +
            "\n".join(f"• `{to_display(c)}`" for c in duplicates),
            parse_mode="Markdown"
        )

    if unique:
        store_user_codes(uid, unique)
        bot.send_message(message.chat.id, f"✔ Saved {len(unique)} new codes.")
    else:
        bot.send_message(message.chat.id, "⚠ No new codes found.")


# ---------------------------------------------------------
# START BOT POLLING
# ---------------------------------------------------------

logger.info("🔥 Bot polling started.")

while True:
    try:
        bot.polling(non_stop=True, interval=0, timeout=20)
    except Exception as e:
        logger.error(f"Polling crashed: {e}")
        time.sleep(5)

