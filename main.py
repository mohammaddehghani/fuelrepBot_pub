import os
from flask import Flask, request
import sqlite3
import csv
import requests

from dotenv import load_dotenv
load_dotenv()

# ---- پارامترها را از محیط بگیر (secret!) ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH")
ADMIN_CHAT_IDS = os.getenv("ADMIN_CHAT_IDS")

# لیست تکی یا چندتایی آی‌دی ادمین (مثلاً 12345 یا 12345,67890)
if ADMIN_CHAT_IDS:
    ADMIN_CHAT_IDS = [int(i.strip()) for i in ADMIN_CHAT_IDS.split(",") if i.strip()]
else:
    ADMIN_CHAT_IDS = []

MAIN_MENU = [
    ['ثبت سوختگیری 🚗', 'ثبت ساعتکاری 🕒'],
    ['دریافت نمودار مصرف 📊', '📦 بکاپ سوختگیری']
]

app = Flask(__name__)

def send_message(chat_id, text, buttons=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if buttons:
        data["reply_markup"] = {"keyboard": buttons, "resize_keyboard": True}
    requests.post(url, json=data)

def send_document(chat_id, file_path, caption=""):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendDocument'
    with open(file_path, 'rb') as f:
        files = {'document': f}
        data = {'chat_id': chat_id, 'caption': caption}
        requests.post(url, files=files, data=data)

def create_fuel_backup():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    # نام جدول و ستون‌ها رو با دیتابیس خودت ست کن
    cursor.execute('SELECT date, km, liters, note FROM fuel_logs')
    rows = cursor.fetchall()
    headers = [desc[0] for desc in cursor.description]
    filename = "fuel_backup.csv"
    with open(filename, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    conn.close()
    return filename

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if "message" not in data:
        return "ok"
    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")
    # فقط ادمین‌ها دسترسی به بکاپ دارند (یا خط بعدی رو کامنت کن برای دسترسی آزاد)
    if text == "📦 بکاپ سوختگیری":
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            send_message(chat_id, "دسترسی ندارید!", MAIN_MENU)
            return "ok"
        try:
            file_path = create_fuel_backup()
            send_document(chat_id, file_path, caption="📦 بکاپ سوختگیری (CSV)")
            os.remove(file_path)
        except Exception as e:
            send_message(chat_id, f"❌ خطا در تهیه بکاپ: {e}")
        return "ok"
    if text == "/start":
        send_message(chat_id, "به بات خوش آمدی! ⛽️", MAIN_MENU)
        return "ok"
    # سایر دکمه‌ها و فرمان‌ها
    return "ok"

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=80)
