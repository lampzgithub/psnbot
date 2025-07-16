import re
import telebot
import logging
import os 
import dotenv
import fitz
import requests
from collections import defaultdict

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.info('Starting Bot...')
logging.basicConfig(filename='runnerlogs.log',
                    filemode='w',
                    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.DEBUG)

# Load .env for token
dotenv.load_dotenv()
token = str(os.getenv("tk"))  # Ensure .env has tk=YOUR_TELEGRAM_BOT_TOKEN
bot = telebot.TeleBot(token=token)

# Commands
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "👋 Hello! I extract PSN gift card codes.\nUse /help to see how.")

@bot.message_handler(commands=['help'])
def help(message):
    bot.send_message(message.chat.id, "📋 *Commands:*\n"
                                      "`/p <text>` - Paste text to extract\n"
                                      "`/w <pastebin link>` - Fetch from Pastebin\n"
                                      "📄 Upload a PDF - I’ll extract from it too!",
                     parse_mode="Markdown")

# Regex patterns
code_pattern = r'\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b'
denom_pattern = r'₹\s?\d{1,4}(?:,\d{3})*(?:\.\d{2})?'
validity_pattern = r'Expires on (\d{2} \w{3} \d{4})'

# Extract code, denomination, validity
def extract_data(text):
    results = []
    seen = set()
    for match in re.finditer(code_pattern, text):
        code = match.group()
        if code in seen:
            continue
        seen.add(code)

        # Extract surrounding text (100 characters before & after the code)
        start = max(0, match.start() - 100)
        end = min(len(text), match.end() + 100)
        snippet = text[start:end]

        # Find denomination and validity within this window
        denom_match = re.search(denom_pattern, snippet)
        valid_match = re.search(validity_pattern, snippet)

        denom = denom_match.group().strip() if denom_match else "N/A"
        valid = valid_match.group().strip() if valid_match else "N/A"

        results.append((code, denom, valid))
    return results


# Group by denomination and create separate files
def generate_txt_by_denom(results):
    grouped = defaultdict(list)
    for code, denom, valid in results:
        grouped[denom].append((code, denom, valid))

    files = []
    for denom, entries in grouped.items():
        # Extract numeric value from ₹ 1,000.00 -> 1000
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

# /p handler for pasted text
@bot.message_handler(commands=['p'])
def handle_pasted_text(message):
    bot.send_message(message.chat.id, "📋 Processing pasted content...")
    text = message.text.replace("/p", "", 1).strip()
    results = extract_data(text)

    if not results:
        bot.send_message(message.chat.id, "⚠️ No valid codes found.")
        return

    files = generate_txt_by_denom(results)
    for filename, count in files:
        with open(filename, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"✅ Total Unique Codes: {count}")
        os.remove(filename)

# /w handler for pastebin links
@bot.message_handler(commands=['w'])
def handle_web_paste(message):
    bot.send_message(message.chat.id, "🌐 Fetching Pastebin content...")
    url = message.text.replace("/w", "", 1).strip()

    # Convert normal link to raw if needed
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
            bot.send_message(message.chat.id, """⚠️ No valid codes found in the Pastebin content. Error fetching paste: {e}\nCopy all gmail content \n
Go to pastebin.com \n
create a new paste \n
After entering details like title etc\n
Tap on create a new paste \n
After the paste has been created copy its link \n
Use /w link in the bot""")
            return
        files = generate_txt_by_denom(results)
        for filename, count in files:
            with open(filename, 'rb') as f:
                bot.send_document(message.chat.id, f, caption=f"✅ Total Unique Codes: {count}")
            os.remove(filename)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error fetching paste: {e}")

# PDF file upload handler
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

    files = generate_txt_by_denom(results)
    for filename, count in files:
        with open(filename, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"✅ Total Unique Codes: {count}")
        os.remove(filename)

    os.remove(file_path)

# Start the bot
bot.polling()
