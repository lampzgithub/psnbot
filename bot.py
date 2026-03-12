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
    level=logging.INFO  # Prevent telebot debug spam
)
logger = logging.getLogger(__name__)

telebot_logger = logging.getLogger("telebot")
telebot_logger.setLevel(logging.WARNING)

logger.info("🔵 Bot starting...")

# ---------------------------------------------------------
# LOAD ENV + INIT BOT
# ---------------------------------------------------------

dotenv.load_dotenv()
token = str(os.getenv("tk"))
bot = telebot.TeleBot(token=token)

# Admin system (multiple admins)
ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}

def is_admin(uid):
    return uid in ADMIN_IDS

logger.info(f"Admins loaded: {ADMIN_IDS}")

# ---------------------------------------------------------
# RAILWAY STORAGE DIRECTORY
# ---------------------------------------------------------

TEMP_DIR = "/app/temp_files"
os.makedirs(TEMP_DIR, exist_ok=True)

# ---------------------------------------------------------
# USER TRACKING (users.json)
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
# CLEANUP THREAD
# ---------------------------------------------------------

DELETE_AFTER_SECONDS = 7 * 24 * 60 * 60  # 7 days
pending_user_codes = {}  # uid → list of codes requiring manual denomination
pending_parse_review = {}  # uid -> pending parse payload for confirm/edit flow
admin_browse_state = {}  # admin_uid -> pagination state

def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for fname in os.listdir(TEMP_DIR):
                path = os.path.join(TEMP_DIR, fname)
                if os.path.isfile(path):
                    if now - os.path.getmtime(path) > DELETE_AFTER_SECONDS:
                        os.remove(path)
                        logger.info(f"🗑 Deleted old temp file: {fname}")
            time.sleep(3600)
        except Exception as e:
            logger.error(f"[Cleanup Error] {e}")
            time.sleep(60)

threading.Thread(target=cleanup_old_files, daemon=True).start()

# ---------------------------------------------------------
# CODE PATTERNS (SHORT + LONG)
# ---------------------------------------------------------

CODE_PATTERNS = [
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b", re.I),
    re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{12}-[A-Z0-9]{6}\b", re.I),
]

# OLD PARSER (PDF) PATTERNS
code_pattern = r'\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b'
denom_pattern = r'₹\s?\d{1,4}(?:,\d{3})*(?:\.\d{2})?'
validity_pattern = r'Expires on (\d{2} \w{3} \d{4})'

# ---------------------------------------------------------
# PDF EXTRACTION USING YOUR OLD LOGIC
# ---------------------------------------------------------

def extract_data(text):
    results = []
    seen = set()

    for match in re.finditer(code_pattern, text):
        code = match.group()

        if code in seen:
            continue
        seen.add(code)

        # Context window for price/validity
        start = max(0, match.start() - 100)
        end = min(len(text), match.end() + 100)
        snippet = text[start:end]

        denom_match = re.search(denom_pattern, snippet)
        valid_match = re.search(validity_pattern, snippet)

        denom = denom_match.group().strip() if denom_match else "N/A"
        valid = valid_match.group().strip() if valid_match else "N/A"

        results.append((code, denom, valid))

    return results
# ---------------------------------------------------------
# TXT FILE GENERATOR (PER DENOMINATION)
# ---------------------------------------------------------

def generate_txt_by_denom(results):
    os.makedirs(TEMP_DIR, exist_ok=True)

    grouped = defaultdict(list)
    for code, denom, valid in results:
        grouped[denom].append((code, denom, valid))

    output_files = []
    timestamp = int(time.time())

    for denom, entries in grouped.items():
        number_match = re.search(r"\d+(?:,\d{3})*", denom)
        number = number_match.group().replace(",", "") if number_match else "unknown"

        filepath = os.path.join(TEMP_DIR, f"{number}_{timestamp}.txt")

        with open(filepath, "w") as f:
            for code, d, valid in entries:
                f.write(f"{code}\n")

        output_files.append((filepath, len(entries)))

    return output_files

# ---------------------------------------------------------
# NORMALIZATION / DUPLICATE BLOCKING
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
# CODE STORAGE PER USER
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

def detect_codes_with_positions(text):
    """
    Returns sorted list of tuples: (code, start_index, end_index)
    preserving appearance order and removing duplicates by normalized code.
    """
    candidates = []
    for pat in CODE_PATTERNS:
        for m in pat.finditer(text.upper()):
            candidates.append((m.group(0), m.start(), m.end()))

    candidates.sort(key=lambda x: x[1])

    unique = []
    seen = set()
    for code, start, end in candidates:
        norm = normalize_code(code)
        if norm in seen:
            continue
        seen.add(norm)
        unique.append((code, start, end))

    return unique


def detect_denom_from_snippet(snippet):
    m = re.search(r"₹\s?(\d{4,5})", snippet)
    if m:
        return m.group(1)

    m = re.search(r"\b(1000|2000|3000|4000|5000)\b", snippet)
    if m:
        return m.group(1)

    m = re.search(r"\b([1-5])k\b", snippet, re.I)
    if m:
        return str(int(m.group(1)) * 1000)

    return None


def parse_code_entries_from_text(text):
    """
    Parse all code entries from free text while preserving order.
    Each entry includes a best-effort denomination detection from local snippet.
    """
    entries = []
    for code, start, end in detect_codes_with_positions(text):
        snippet_start = max(0, start - 80)
        snippet_end = min(len(text), end + 80)
        snippet = text[snippet_start:snippet_end]
        denom = detect_denom_from_snippet(snippet)
        entries.append({"code": code, "denom": denom, "status": "pending"})
    return entries


def render_parse_review_text(entries, title="🧾 *Review parsed codes*"):
    lines = [title, ""]
    for idx, item in enumerate(entries, start=1):
        denom = f"₹{item['denom']}" if item.get("denom") else "❓"
        lines.append(f"{idx}. `{to_display(item['code'])}` — {denom}")
    lines.append("\nChoose *Confirm* to save or *Edit* to fix.")
    return "\n".join(lines)


def parse_edited_lines(raw_text):
    """Expected format per line: CODE 1k|2k|... OR CODE 1000|2000|..."""
    cleaned = raw_text.strip()
    if not cleaned:
        return None, "Empty input."

    result = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue

        code_match = None
        for pat in CODE_PATTERNS:
            code_match = pat.search(line.upper())
            if code_match:
                break

        if not code_match:
            return None, f"Couldn't find a code in line: {line}"

        code = code_match.group(0)
        denom = detect_denom_from_snippet(line)
        result.append({"code": code, "denom": denom, "status": "pending"})

    if not result:
        return None, "No valid lines found."

    return result, None


def delete_user_store(uid):
    """Delete only one user's stored file and detach its global ownership entries."""
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")
    if not os.path.exists(filepath):
        return 0

    removed_count = 0
    with open(filepath, "r") as f:
        next(f, None)
        for line in f:
            code = line.split(",")[0].strip()
            norm = normalize_code(code)
            if GLOBAL_CODES.get(norm) == uid:
                GLOBAL_CODES.pop(norm, None)
            removed_count += 1

    with open(GLOBAL_CODES_FILE, "w") as f:
        json.dump(GLOBAL_CODES, f)

    os.remove(filepath)
    return removed_count


def get_user_code_count(uid):
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")
    if not os.path.exists(filepath):
        return 0
    with open(filepath, "r") as f:
        next(f, None)
        return sum(1 for _ in f)


def build_admin_dashboard_text(page=0, per_page=8):
    users = sorted(known_users)
    total_users = len(users)
    total_codes = len(GLOBAL_CODES)
    start = page * per_page
    subset = users[start:start + per_page]

    lines = [
        "🛠 *Admin Dashboard*",
        f"👥 Users: *{total_users}*",
        f"🔢 Global unique codes: *{total_codes}*",
        "",
        "Select a user to manage codes:"
    ]

    for u in subset:
        lines.append(f"• `{u}` — {get_user_code_count(u)} codes")

    if not subset:
        lines.append("(No users found on this page)")

    return "\n".join(lines)


def build_admin_dashboard_kb(page=0, per_page=8):
    kb = telebot.types.InlineKeyboardMarkup()
    users = sorted(known_users)
    start = page * per_page
    subset = users[start:start + per_page]

    for uid in subset:
        kb.add(telebot.types.InlineKeyboardButton(f"👤 {uid}", callback_data=f"adm_user_{uid}"))

    nav = []
    if page > 0:
        nav.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_dash_{page - 1}"))
    if start + per_page < len(users):
        nav.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"adm_dash_{page + 1}"))
    if nav:
        kb.row(*nav)

    kb.add(telebot.types.InlineKeyboardButton("🔄 Refresh", callback_data=f"adm_dash_{page}"))
    return kb


# ---------------------------------------------------------
# START COMMAND
# ---------------------------------------------------------

@bot.message_handler(commands=['start'])
def start_cmd(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    known_users.add(uid)
    save_users()

    bot.send_message(message.chat.id,
                     "👋 Welcome! Send PSN codes or use /help")

# ---------------------------------------------------------
# HELP COMMAND
# ---------------------------------------------------------

@bot.message_handler(commands=['help'])
def help_cmd(message):
    uid = message.from_user.id
    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    bot.send_message(message.chat.id,
        """📘 *Commands*
/w <pastebin> – Extract codes from Pastebin
/getstore – Download denomination-split TXT files
/clearstore – Delete your codes
/stats – View your counts
/remove <code> – Remove one code
You may also upload PDFs.
""",
        parse_mode="Markdown")

# ---------------------------------------------------------
# GETSTORE (USER)
# ---------------------------------------------------------

def send_user_codes(uid, chat_id):
    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")

    if not os.path.exists(filepath):
        return bot.send_message(chat_id, "📂 No stored codes found.")

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
            bot.send_document(chat_id, f,
                              caption=f"{denom} — {len(codes)} codes")

# ---------------------------------------------------------
# GETSTORE COMMAND + ADMIN EXTENDED
# ---------------------------------------------------------

@bot.message_handler(commands=['getstore'])
def cmd_getstore(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    if is_admin(uid):
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(
            telebot.types.InlineKeyboardButton("📁 My Codes", callback_data="adm_get_my"),
            telebot.types.InlineKeyboardButton("🌍 Global Codes", callback_data="adm_get_global")
        )
        return bot.send_message(message.chat.id, "Select:", reply_markup=kb)

    # Normal user flow:
    return send_user_codes(uid, message.chat.id)
# ---------------------------------------------------------
# ADMIN GETSTORE HANDLING
# ---------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data in ["adm_get_my", "adm_get_global"])
def admin_getstore_handler(call):
    uid = call.from_user.id

    if not is_admin(uid):
        return bot.answer_callback_query(call.id, "Not admin")

    if call.data == "adm_get_my":
        return send_user_codes(uid, call.message.chat.id)

    if call.data == "adm_get_global":
        return send_global_codes(call.message.chat.id)

# ---------------------------------------------------------
# SEND GLOBAL CODE FILES TO ADMIN
# ---------------------------------------------------------

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

        filename = os.path.join(TEMP_DIR, f"global_{number}_{timestamp}.txt")
        with open(filename, "w") as f:
            for c in codes:
                f.write(c + "\n")

        with open(filename, "rb") as f:
            bot.send_document(chat_id, f,
                              caption=f"🌍 {denom} — {len(codes)} global codes")

    # Raw registry for debugging
    with open(GLOBAL_CODES_FILE, "rb") as f:
        bot.send_document(chat_id, f, caption="🌍 Raw Global Registry (JSON)")

# ---------------------------------------------------------
# BAN / UNBAN / BANNED LIST
# ---------------------------------------------------------

@bot.message_handler(commands=['ban'])
def ban_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Usage: /ban <userid>")

    try:
        target = int(parts[1])
    except:
        return bot.send_message(message.chat.id, "Invalid user ID.")

    if target in ADMIN_IDS:
        return bot.send_message(message.chat.id, "❌ Cannot ban another admin.")

    BANNED_USERS.add(target)
    save_bans()

    bot.send_message(message.chat.id, f"🚫 User {target} has been banned.")


@bot.message_handler(commands=['unban'])
def unban_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return bot.send_message(message.chat.id, "Usage: /unban <userid>")

    try:
        target = int(parts[1])
    except:
        return bot.send_message(message.chat.id, "Invalid ID.")

    if target not in BANNED_USERS:
        return bot.send_message(message.chat.id, "User is not banned.")

    BANNED_USERS.remove(target)
    save_bans()

    bot.send_message(message.chat.id, f"✅ User {target} unbanned.")


@bot.message_handler(commands=['banned'])
def banned_list(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    if not BANNED_USERS:
        return bot.send_message(message.chat.id, "No banned users.")

    msg = "🚫 *Banned Users:*\n\n"
    for u in BANNED_USERS:
        msg += f"• {u}\n"

    bot.send_message(message.chat.id, msg, parse_mode="Markdown")

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

    raw_code = parts[1].strip()
    norm = normalize_code(raw_code)

    filepath = os.path.join(TEMP_DIR, f"stored_{uid}.txt")
    if not os.path.exists(filepath):
        return bot.send_message(message.chat.id, "You have no stored codes.")

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
# CLEARSTORE COMMAND
# ---------------------------------------------------------

@bot.message_handler(commands=['clearstore'])
def clearstore_cmd(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    removed = delete_user_store(uid)
    if removed:
        return bot.send_message(message.chat.id, f"🗑 Your stored codes were deleted ({removed} removed).")
    return bot.send_message(message.chat.id, "📂 You have no stored codes.")


# ---------------------------------------------------------
# STATS COMMAND
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
            if len(parts) == 3:
                stats[parts[1]] += 1

    msg = "📊 *Your Statistics:*\n\n"
    total = 0

    for denom, count in stats.items():
        msg += f"{denom}: **{count}** codes\n"
        total += count

    msg += f"\nTotal saved codes: **{total}**"

    bot.send_message(message.chat.id, msg, parse_mode="Markdown")


# ---------------------------------------------------------
# FULL ADMIN PANEL
# ---------------------------------------------------------

@bot.message_handler(commands=['admin'])
def admin_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    page = 0
    admin_browse_state[uid] = page
    bot.send_message(
        message.chat.id,
        build_admin_dashboard_text(page=page),
        reply_markup=build_admin_dashboard_kb(page=page),
        parse_mode="Markdown"
    )


# ---------------------------------------------------------
# ADMIN CALLBACK HANDLER
# ---------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_panel_handler(call):
    uid = call.from_user.id

    if not is_admin(uid):
        return bot.answer_callback_query(call.id, "Not admin")

    action = call.data

    if action == "adm_users":
        return bot.send_message(call.message.chat.id, f"👥 Total users: {len(known_users)}")

    if action == "adm_codes":
        return bot.send_message(call.message.chat.id, f"🔢 Total unique global codes: {len(GLOBAL_CODES)}")

    if action.startswith("adm_dash_"):
        try:
            page = int(action.split("_")[-1])
        except ValueError:
            page = 0
        admin_browse_state[uid] = max(0, page)
        return bot.edit_message_text(
            build_admin_dashboard_text(page=admin_browse_state[uid]),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=build_admin_dashboard_kb(page=admin_browse_state[uid])
        )

    if action.startswith("adm_user_") and action.count("_") == 2:
        target_uid = int(action.split("_")[-1])
        count = get_user_code_count(target_uid)
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("📁 Download store", callback_data=f"adm_user_get_{target_uid}"))
        kb.add(telebot.types.InlineKeyboardButton("🗑 Clear this user only", callback_data=f"adm_user_clear_{target_uid}"))
        kb.add(telebot.types.InlineKeyboardButton("⬅️ Back", callback_data=f"adm_dash_{admin_browse_state.get(uid, 0)}"))
        return bot.edit_message_text(
            f"👤 *User:* `{target_uid}`\n📦 *Codes:* {count}\n\nChoose an action:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=kb
        )

    if action.startswith("adm_user_get_"):
        target_uid = int(action.split("_")[-1])
        send_user_codes(target_uid, call.message.chat.id)
        return bot.answer_callback_query(call.id, "Store sent")

    if action.startswith("adm_user_clear_"):
        target_uid = int(action.split("_")[-1])
        removed = delete_user_store(target_uid)
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("⬅️ Back to dashboard", callback_data=f"adm_dash_{admin_browse_state.get(uid, 0)}"))
        return bot.edit_message_text(
            f"🗑 Cleared user `{target_uid}` only. Removed {removed} codes.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=kb
        )

    if action == "adm_broadcast":
        return bot.send_message(call.message.chat.id, "Use:\n/broadcast <your message>")

# ---------------------------------------------------------
# BROADCAST COMMAND
# ---------------------------------------------------------

@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(message):
    uid = message.from_user.id

    if not is_admin(uid):
        return

    msg = message.text.replace("/broadcast", "", 1).strip()
    if not msg:
        return bot.send_message(message.chat.id, "Usage: /broadcast <message>")

    sent = 0

    for user in list(known_users):
        try:
            bot.send_message(user, f"📢 *Broadcast:*\n{msg}", parse_mode="Markdown")
            sent += 1
        except:
            pass

    bot.send_message(message.chat.id, f"Sent to {sent} users.")


# ---------------------------------------------------------
# AUTO-DETECTION: FINDING CODES IN TEXT MESSAGES
# ---------------------------------------------------------

def detect_codes_in_text(text):
    """
    Returns a list of all PSN codes found using short + long patterns.
    """
    found = set()
    for pat in CODE_PATTERNS:
        found |= set(pat.findall(text.upper()))
    return list(found)


# ---------------------------------------------------------
# LOCAL DENOMINATION DETECTION (OPTION B — strict)
# ---------------------------------------------------------

def detect_denom_near_code(full_text, code):
    """
    OPTION B (as the user selected):
    Detect denomination ONLY from snippet near each code.
    Never infer using global message amounts.
    """

    idx = full_text.upper().find(code.upper())
    if idx == -1:
        return None

    start = max(0, idx - 100)
    end = min(len(full_text), idx + len(code) + 100)
    snippet = full_text[start:end]

    # Match ₹1000, 2000, etc
    m = re.search(r"₹\s?(\d{4,5})", snippet)
    if m:
        return m.group(1)

    # Match raw number like 1000, 2000
    m = re.search(r"\b(1000|2000|3000|4000|5000)\b", snippet)
    if m:
        return m.group(1)

    # Match 1k 2k 5k
    m = re.search(r"\b([1-5])k\b", snippet, re.I)
    if m:
        return str(int(m.group(1)) * 1000)

    return None
# ---------------------------------------------------------
# MAIN TEXT HANDLER (AUTO-DETECT CODES)
# ---------------------------------------------------------

@bot.message_handler(func=lambda m: m.content_type == "text" and m.from_user.id in pending_parse_review and pending_parse_review[m.from_user.id].get("editing") and not m.text.startswith("/"))
def parse_edit_text_handler(message):
    uid = message.from_user.id
    pending = pending_parse_review.get(uid)
    if not pending:
        return

    edited, err = parse_edited_lines(message.text)
    if err:
        return bot.send_message(message.chat.id, f"❌ {err}")

    pending["entries"] = edited
    pending["editing"] = False

    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton("✅ Confirm", callback_data="parse_confirm"),
        telebot.types.InlineKeyboardButton("✏️ Edit", callback_data="parse_edit")
    )
    return bot.send_message(
        message.chat.id,
        render_parse_review_text(edited, title="🧾 *Updated parsed codes*"),
        parse_mode="Markdown",
        reply_markup=kb
    )


@bot.message_handler(func=lambda m: m.content_type == "text" and not m.text.startswith("/"))
def auto_detect_text(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    text = message.text.strip()
    found_entries = parse_code_entries_from_text(text)

    if not found_entries:
        return

    logger.info(f"[AUTO-DETECT] Found codes from user {uid}: {[e['code'] for e in found_entries]}")

    review_entries = []
    dupes = []
    for entry in found_entries:
        code = entry["code"]
        norm = normalize_code(code)
        if norm in GLOBAL_CODES:
            dupes.append(code)
            continue
        review_entries.append(entry)

    if dupes:
        bot.send_message(
            message.chat.id,
            "⚠ Already saved (ignored):\n" + "\n".join(f"• `{to_display(c)}`" for c in dupes),
            parse_mode="Markdown"
        )

    if not review_entries:
        return bot.send_message(message.chat.id, "⚠ No new unique codes found.")

    pending_parse_review[uid] = {
        "entries": review_entries,
        "chat_id": message.chat.id,
        "source_msg_id": message.message_id,
    }

    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton("✅ Confirm", callback_data="parse_confirm"),
        telebot.types.InlineKeyboardButton("✏️ Edit", callback_data="parse_edit")
    )

    bot.send_message(
        message.chat.id,
        render_parse_review_text(review_entries),
        parse_mode="Markdown",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data in ["parse_confirm", "parse_edit"])
def parse_review_handler(call):
    uid = call.from_user.id
    pending = pending_parse_review.get(uid)

    if not pending:
        return bot.answer_callback_query(call.id, "No pending parse review.")

    if call.data == "parse_edit":
        pending["editing"] = True
        bot.answer_callback_query(call.id, "Send corrected lines now.")
        return bot.send_message(
            call.message.chat.id,
            "✏️ Send corrected lines.\n"
            "Format per line: `CODE 1k` or `CODE 1000`\n"
            "Example:\n`8KEX-5KKJ-3H7B 3000`",
            parse_mode="Markdown"
        )

    entries = pending["entries"]
    unresolved = [e["code"] for e in entries if not e.get("denom")]
    resolved = [(e["code"], e["denom"]) for e in entries if e.get("denom")]

    if resolved:
        store_user_codes(uid, [(c, f"₹{d}", "N/A") for c, d in resolved])

    if unresolved:
        pending_user_codes[uid] = unresolved
        kb = telebot.types.InlineKeyboardMarkup()
        for amt in ["₹1000", "₹2000", "₹3000", "₹4000", "₹5000"]:
            kb.add(telebot.types.InlineKeyboardButton(amt, callback_data=f"denom_{amt}"))
        display = "\n".join(f"• `{to_display(c)}`" for c in unresolved)
        bot.send_message(
            call.message.chat.id,
            f"🎯 *Need denomination for these codes:*\n\n{display}",
            parse_mode="Markdown",
            reply_markup=kb
        )

    pending_parse_review.pop(uid, None)
    return bot.edit_message_text(
        f"✅ Saved {len(resolved)} parsed codes."
        + (" Remaining codes need denomination selection." if unresolved else ""),
        call.message.chat.id,
        call.message.message_id
    )


# ---------------------------------------------------------
# DENOMINATION CALLBACK FOR REMAINING CODES
# ---------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data.startswith("denom_"))
def denom_choice_handler(call):
    uid = call.from_user.id

    if uid not in pending_user_codes:
        return bot.answer_callback_query(call.id, "No pending codes.")

    denom = call.data.replace("denom_", "")
    codes = pending_user_codes.pop(uid)

    logger.info(f"[CHOICE] User {uid} selected {denom} for {codes}")

    store_user_codes(uid, [(c, denom, "N/A") for c in codes])

    bot.edit_message_text(
        f"✔ Saved {len(codes)} codes under {denom}.",
        call.message.chat.id,
        call.message.message_id
    )


# ---------------------------------------------------------
# PDF HANDLER (OLD PARSER + GLOBAL DUP BLOCK)
# ---------------------------------------------------------

@bot.message_handler(content_types=['document'])
def pdf_handler(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    file_info = bot.get_file(message.document.file_id)
    pdf_data = bot.download_file(file_info.file_path)

    pdf_path = os.path.join(TEMP_DIR, message.document.file_name)

    with open(pdf_path, "wb") as f:
        f.write(pdf_data)

    # Extract text using PyMuPDF
    text = ""
    try:
        with fitz.open(pdf_path) as doc:
            for page in doc:
                text += page.get_text()
    except Exception as e:
        logger.error(f"[PDF ERROR] {e}")
        os.remove(pdf_path)
        return bot.send_message(message.chat.id, "❌ Error reading PDF file.")

    os.remove(pdf_path)

    extracted = extract_data(text)

    if not extracted:
        return bot.send_message(message.chat.id, "⚠ No PSN codes found in PDF.")

    unique = []
    duplicates = []

    for code, denom, valid in extracted:
        norm = normalize_code(code)
        if norm in GLOBAL_CODES:
            duplicates.append(code)
        else:
            unique.append((code, denom, valid))

    if duplicates:
        bot.send_message(
            message.chat.id,
            "⚠ Duplicate codes ignored:\n" +
            "\n".join(f"`{to_display(c)}`" for c in duplicates),
            parse_mode="Markdown"
        )

    if not unique:
        return bot.send_message(message.chat.id, "⚠ No new unique codes found.")

    store_user_codes(uid, unique)

    # Group-Split PDF Extracted Codes by Denomination
    files = generate_txt_by_denom(unique)

    for fp, count in files:
        with open(fp, "rb") as f:
            bot.send_document(message.chat.id, f, caption=f"📄 {count} new codes saved")


# ---------------------------------------------------------
# PASTEBIN HANDLER
# ---------------------------------------------------------

@bot.message_handler(commands=['w'])
def pastebin_handler(message):
    uid = message.from_user.id

    if is_banned(uid):
        return bot.send_message(message.chat.id, "❌ You are banned.")

    url = message.text.replace("/w", "", 1).strip()

    if "pastebin.com" in url and "/raw/" not in url:
        url = f"https://pastebin.com/raw/{url.split('/')[-1]}"

    try:
        text = requests.get(url).text
    except:
        return bot.send_message(message.chat.id, "❌ Could not fetch Pastebin link.")

    found_codes = detect_codes_in_text(text)

    if not found_codes:
        return bot.send_message(message.chat.id, "⚠ No codes found in Pastebin.")

    unique = []
    duplicates = []

    for code in found_codes:
        norm = normalize_code(code)
        if norm in GLOBAL_CODES:
            duplicates.append(code)
        else:
            unique.append((code, "N/A", "N/A"))

    if duplicates:
        bot.send_message(
            message.chat.id,
            "⚠ Already saved (ignored):\n" +
            "\n".join(f"`{to_display(c)}`" for c in duplicates),
            parse_mode="Markdown"
        )

    if unique:
        store_user_codes(uid, unique)
        return bot.send_message(message.chat.id, f"✔ Saved {len(unique)} new codes.")

    bot.send_message(message.chat.id, "⚠ No new codes found.")
# ---------------------------------------------------------
# BOT POLLING LOOP (SAFE RESTART)
# ---------------------------------------------------------

logger.info("🔥 Bot polling started...")

while True:
    try:
        bot.polling(non_stop=True, timeout=30, interval=0)
    except Exception as e:
        logger.error(f"[POLLING CRASHED] {e}")
        time.sleep(5)
