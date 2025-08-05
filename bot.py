import re
import telebot
import logging
import os 
import dotenv
import fitz
import requests
from collections import defaultdict

# ✅ Simple logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.info('Starting Bot...')

# Load .env for token
dotenv.load_dotenv()
token = str(os.getenv("tk"))  # Ensure .env has tk=YOUR_TELEGRAM_BOT_TOKEN
bot = telebot.TeleBot(token=token)

# ✅ Track users who want to store codes
store_enabled_users = set()

# ------------------ COMMANDS ------------------ #

@bot.message_handler(commands=['start'])
def start(message):
    user = message.from_user
    logging.info(f"User started bot: {user.first_name} @{user.username} (ID: {user.id})")
    bot.send_message(message.chat.id, "👋 Hello! I extract PSN gift card codes.\nUse /help to see how.")

@bot.message_handler(commands=['help'])
def help(message):
    bot.send_message(message.chat.id, "📋 *Commands:*\n"
                                      "`/p <text>` - Paste text to extract\n"
                                      "`/w <pastebin link>` - Fetch from Pastebin\n"
                                      "`/store` - Toggle auto-saving of your codes\n"
                                      "`/getstore` - Download your stored codes\n"
                                      "`/clearstore` - Delete your stored codes\n"
                                      "📄 Upload a PDF - I’ll extract from it too!",
                     parse_mode="Markdown")

@bot.message_handler(commands=['store'])
def toggle_store(message):
    user_id = message.from_user.id
    if user_id in store_enabled_users:
        store_enabled_users.remove(user_id)
        bot.send_message(message.chat.id, "🛑 Code storing disabled.")
    else:
        store_enabled_users.add(user_id)
        bot.send_message(message.chat.id, "✅ Code storing enabled. All codes you send will be saved.")

@bot.message_handler(commands=['getstore'])
def get_stored_codes(message):
    user_id = message.from_user.id
    filename = f"stored_{user_id}.txt"

    if not os.path.exists(filename):
        bot.send_message(message.chat.id, "📂 You have no stored codes yet.")
        return

    # ✅ First send the full stored file
    with open(filename, 'rb') as f:
        bot.send_document(message.chat.id, f, caption="📦 Your stored codes (all)")

    # ✅ Now load and group by denomination
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()[1:]  # Skip header

        results = []
        for line in lines:
            line = line.strip()
            if line:
                parts = line.split(",")
                if len(parts) == 3:
                    results.append(tuple(parts))

        files = generate_txt_by_denom(results)
        for file, count in files:
            with open(file, 'rb') as f:
                bot.send_document(message.chat.id, f, caption=f"📄 {file} — {count} codes")
            os.remove(file)

    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error parsing stored file: {e}")

@bot.message_handler(commands=['clearstore'])
def clear_stored_codes(message):
    user_id = message.from_user.id
    filename = f"stored_{user_id}.txt"
    if os.path.exists(filename):
        os.remove(filename)
        bot.send_message(message.chat.id, "🗑️ Your stored codes have been deleted.")
    else:
        bot.send_message(message.chat.id, "📂 You have no stored codes to delete.")

# ------------------ UTILITIES ------------------ #

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

    files = []
    for denom, entries in grouped.items():
        number_match = re.search(r'\d+(?:,\d{3})*(?:\.\d{2})?', denom)
        if number_match:
            number = number_match.group().replace(",", "").split(".")[0]
        else:
            number = "unknown"

        filename = f"output_{number}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("CODE,DENOMINATION,VALIDITY\n")
            for code, d, valid in entries:
                f.write(f"{code},{d},{valid}\n")
            # f.write(f"\nTotal Unique Codes: {len(entries)}")
        files.append((filename, len(entries)))
    return files

def store_user_codes(user_id, code_tuples):
    filename = f"stored_{user_id}.txt"

    existing_entries = set()
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            existing_entries = set(line.strip() for line in f if line.strip() and not line.startswith("CODE,"))

    new_lines = []
    for code, denom, valid in code_tuples:
        line = f"{code},{denom},{valid}"
        if line not in existing_entries:
            existing_entries.add(line)
            new_lines.append(line)

    if new_lines:
        write_header = not os.path.exists(filename)
        with open(filename, 'a', encoding='utf-8') as f:
            if write_header:
                f.write("CODE,DENOMINATION,VALIDITY\n")
            for line in new_lines:
                f.write(line + "\n")

# ------------------ HANDLERS ------------------ #

@bot.message_handler(commands=['p'])
def handle_pasted_text(message):
    bot.send_message(message.chat.id, "📋 Processing pasted content...")
    text = message.text.replace("/p", "", 1).strip()
    results = extract_data(text)

    if not results:
        bot.send_message(message.chat.id, "⚠️ No valid codes found.")
        return

    if message.from_user.id in store_enabled_users:
        store_user_codes(message.from_user.id, results)

    files = generate_txt_by_denom(results)
    for filename, count in files:
        with open(filename, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"✅ Total Unique Codes: {count}")
        os.remove(filename)

@bot.message_handler(commands=['w'])
def handle_web_paste(message):
    bot.send_message(message.chat.id, "🌐 Fetching Pastebin content...")
    url = message.text.replace("/w", "", 1).strip()

    if "pastebin.com/" in url and "/raw/" not in url:
        paste_id = url.split("/")[-1]
        url = f"https://pastebin.com/raw/{paste_id}"

    try:
        response = requests.get(url)
        if response.status_code != 200:
            raise Exception(f"Status code: {response.status_code}")
        text = response.text
        results = extract_data(text)

        if not results:
            bot.send_message(message.chat.id, 
                             "⚠️ No valid codes found in the Pastebin content.\n\n"
                             "📋 How to use:\n"
                             "1. Copy your Gmail content\n"
                             "2. Go to https://pastebin.com\n"
                             "3. Paste the content & create the paste\n"
                             "4. Send the link using /w <link>")
            return

        if message.from_user.id in store_enabled_users:
            store_user_codes(message.from_user.id, results)

        files = generate_txt_by_denom(results)
        for filename, count in files:
            with open(filename, 'rb') as f:
                bot.send_document(message.chat.id, f, caption=f"✅ Total Unique Codes: {count}")
            os.remove(filename)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error fetching paste: {e}")

@bot.message_handler(content_types=['document'])
def handle_pdf_upload(message):
    bot.send_message(message.chat.id, "📄 Processing PDF file...")
    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    file_path = message.document.file_name
    with open(file_path, 'wb') as f:
        f.write(downloaded_file)

    text = ""
    with fitz.open(file_path) as doc:
        for page in doc:
            text += page.get_text()

    results = extract_data(text)

    if not results:
        bot.send_message(message.chat.id, "⚠️ No valid codes found in the PDF.")
        os.remove(file_path)
        return

    if message.from_user.id in store_enabled_users:
        store_user_codes(message.from_user.id, results)

    files = generate_txt_by_denom(results)
    for filename, count in files:
        with open(filename, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"✅ Total Unique Codes: {count}")
        os.remove(filename)

    os.remove(file_path)

# ------------------ START BOT ------------------ #

bot.polling()
