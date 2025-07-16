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
token = str(os.getenv("tk"))  # make sure your .env has tk=YOUR_TOKEN
bot = telebot.TeleBot(token=token)

# Start and Help Commands
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Hello! I am a bot that extracts PSN gift card codes.\nType /help to see what I can do.")

@bot.message_handler(commands=['help'])
def help(message):
    bot.send_message(message.chat.id, "I can help you extract PSN codes from:\n"
                     "- Text (/p <your content>)\n"
                     "- Pastebin link (/w <link>)\n"
                     "- PDF file upload\n")

# Regex Patterns
code_pattern = r'\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b'
denom_pattern = r'₹\s?\d{1,4}(?:,\d{3})*(?:\.\d{2})?'
validity_pattern = r'Expires on (\d{2} \w{3} \d{4})'

# Extraction logic
def extract_data(text):
    codes = re.findall(code_pattern, text)
    denominations = re.findall(denom_pattern, text)
    validities = re.findall(validity_pattern, text)

    seen = set()
    results = []
    for i, code in enumerate(codes):
        if code not in seen and i < len(denominations) and i < len(validities):
            seen.add(code)
            results.append(f"{code},{denominations[i]},{validities[i]}")
    return results

# Group and save per denomination
def generate_txt_by_denom(results):
    grouped = defaultdict(list)
    for row in results:
        code, denom, valid = row.split(",")
        grouped[denom.strip()].append(f"{code},{denom},{valid}")

    files = []
    for denom, rows in grouped.items():
        clean_denom = denom.replace("₹", "Rs").replace(",", "").replace(".", "")
        filename = f"output_{clean_denom}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("CODE,DENOMINATION,VALIDITY\n")
            f.write("\n".join(rows))
            f.write(f"\n\nTotal Unique Codes: {len(rows)}")
        files.append((filename, len(rows)))
    return files

# /p - handle pasted text
@bot.message_handler(commands=['p'])
def handle_pasted_text(message):
    bot.send_message(message.chat.id, "Processing pasted content...")
    text = message.text.replace("/p", "", 1).strip()
    results = extract_data(text)

    if not results:
        bot.send_message(message.chat.id, "No valid codes found.")
        return

    files = generate_txt_by_denom(results)
    for filename, count in files:
        with open(filename, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"Total Unique Codes: {count}")
        os.remove(filename)

# /w - handle Pastebin link
@bot.message_handler(commands=['w'])
def handle_web_paste(message):
    bot.send_message(message.chat.id, "Fetching Pastebin content...")
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
            bot.send_message(message.chat.id, "No valid codes found in the Pastebin content.")
            return
        files = generate_txt_by_denom(results)
        for filename, count in files:
            with open(filename, 'rb') as f:
                bot.send_document(message.chat.id, f, caption=f"Total Unique Codes: {count}")
            os.remove(filename)
    except Exception as e:
        bot.send_message(message.chat.id, f"""Error fetching paste: {e}\n
                         Copy all gmail content \n
Go to pastebin.com \n
create a new paste \n
After entering details like title etc\n
Tap on create a new paste \n
After the paste has been created copy its link \n
Use /w link in the bot""")

# Handle PDF file upload
@bot.message_handler(content_types=['document'])
def handle_pdf_upload(message):
    bot.send_message(message.chat.id, "Processing PDF file...")
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
        bot.send_message(message.chat.id, "No valid codes found in the PDF.")
        os.remove(file_path)
        return

    files = generate_txt_by_denom(results)
    for filename, count in files:
        with open(filename, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"Total Unique Codes: {count}")
        os.remove(filename)

    os.remove(file_path)

# Start polling
bot.polling()
