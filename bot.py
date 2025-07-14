import re
import telebot
import logging
import os 
import dotenv
import fitz
import requests



logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.info('Starting Bot...')
logging.basicConfig(filename='runnerlogs.log',
                    filemode='w',
                    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.DEBUG)

dotenv.load_dotenv()
token = str(os.getenv("token"))

bot=telebot.TeleBot(token=token)
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Hello! I am a bot that can help you with various tasks. Type /help to see what I can do.")
code_pattern = r'\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b'
denom_pattern = r'₹\s?\d{1,4}(?:,\d{3})*(?:\.\d{2})?'
validity_pattern = r'Expires on (\d{2} \w{3} \d{4})'

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

def generate_txt(results, output_file="output_codes.txt"):
    lines = ["CODE,DENOMINATION,VALIDITY"]
    lines.extend(results)
    # lines.append(f"\nTotal Unique Codes: {len(results)}")

    with open(output_file, 'w',encoding='utf-8') as f:
        f.write("\n".join(lines))
    return output_file

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
        txt_file = generate_txt(results)
        with open(txt_file, 'rb') as f:
            bot.send_document(message.chat.id, f,caption=f"Total Unique Codes: {len(results)}")
        os.remove(txt_file)
    except Exception as e:
        bot.send_message(message.chat.id, f"Error fetching paste: {e}")


@bot.message_handler(commands=['p'])
def handle_pasted_text(message):
    bot.send_message(message.chat.id, "Processing pasted content...")
    text = message.text.replace("/p", "", 1).strip()
    results = extract_data(text)

    if not results:
        bot.send_message(message.chat.id, "No valid codes found.")
        return

    txt_file = generate_txt(results)
    with open(txt_file, 'rb') as f:
        bot.send_document(message.chat.id, f,caption=f"Total Unique Codes: {len(results)}")
    os.remove(txt_file)

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

    txt_file = generate_txt(results)
    with open(txt_file, 'rb') as f:
        bot.send_document(message.chat.id, f,caption=f"Total Unique Codes: {len(results)}")

    os.remove(file_path)
    os.remove(txt_file)

bot.polling()
