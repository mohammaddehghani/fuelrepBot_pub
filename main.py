import os
os.environ['MPLCONFIGDIR'] = '/tmp'
import sqlite3
import csv
import io
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, request, send_file
import requests
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "fuel_logs.db")
ADMIN_CHAT_IDS = [int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip()]

app = Flask(__name__)

# حافظه وضعیت کاربران
user_steps = {}
user_buffers = {}

MAIN_MENU = [["ثبت سوختگیری ⛽️"], ["📦 بکاپ سوختگیری", "📊 نمودار مصرف"]]

def send_message(chat_id, text, buttons=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"keyboard": buttons, "resize_keyboard": True}
    requests.post(url, json=payload)

def send_document(chat_id, file_bytes, filename, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    files = {"document": (filename, file_bytes)}
    data = {"chat_id": chat_id, "caption": caption}
    requests.post(url, data=data, files=files)

def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS fuel_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        km REAL,
        liter REAL,
        timestamp TEXT
    )''')
    conn.commit()
    conn.close()

def insert_log(km, liter):
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO fuel_logs (km, liter, timestamp) VALUES (?, ?, ?)",
              (km, liter, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def generate_csv():
    conn = sqlite3.connect(DATABASE_PATH)
    df = pd.read_sql_query("SELECT * FROM fuel_logs", conn)
    conn.close()
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return io.BytesIO(output.read().encode("utf-8"))

def generate_chart():
    conn = sqlite3.connect(DATABASE_PATH)
    df = pd.read_sql_query("SELECT km AS Kilometer, liter AS Liter FROM fuel_logs ORDER BY id", conn)
    conn.close()

    if len(df) < 5:
        return None

    df['distance'] = df['Kilometer'].diff()
    df['fuel_per_100km'] = (df['Liter'] / df['distance']) * 100
    df = df.dropna().copy()
    df['is_reliable'] = df['Liter'] >= 12

    reliable_df = df[df['is_reliable']].copy()
    noisy_df = df[~df['is_reliable']].copy()
    reliable_df['ma_small'] = reliable_df['fuel_per_100km'].rolling(window=5).mean()
    reliable_df['ma_large'] = reliable_df['fuel_per_100km'].rolling(window=15).mean()
    avg = reliable_df['fuel_per_100km'].mean()
    reliable_df['is_last'] = False
    reliable_df.loc[reliable_df.tail(5).index, 'is_last'] = True
    marker_sizes = reliable_df['Liter'] * 7

    plt.figure(figsize=(13, 7))
    scatter = plt.scatter(reliable_df['Kilometer'], reliable_df['fuel_per_100km'],
                          s=marker_sizes, c=reliable_df['Liter'], cmap='Blues', alpha=0.8)
    if len(noisy_df):
        plt.scatter(noisy_df['Kilometer'], noisy_df['fuel_per_100km'],
                    s=noisy_df['Liter'] * 7, c='red', alpha=0.6, marker='x')
    plt.plot(reliable_df['Kilometer'], reliable_df['ma_small'], color='limegreen', linewidth=1.3)
    plt.plot(reliable_df['Kilometer'], reliable_df['ma_large'], color='coral', linewidth=1.5)
    plt.axhline(avg, color='goldenrod', linestyle='--', linewidth=1)
    last_points = reliable_df[reliable_df['is_last']]
    labels = list(range(1, 6))[::-1]
    for i, (idx, row) in enumerate(last_points[::-1].iterrows()):
        plt.text(row['Kilometer'], row['fuel_per_100km'], str(labels[i]), color='#1956ac', ha='center')
    plt.colorbar(scatter).set_label('Volume Refueled [Liters]')
    plt.xlabel('Kilometer')
    plt.ylabel('Fuel Consumption [L/100km]')
    plt.title('Fuel Consumption Trend')
    plt.grid(alpha=0.2)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if 'message' not in data:
        return 'ok'

    chat_id = data['message']['chat']['id']
    text = data['message'].get('text', '').strip()

    step = user_steps.get(chat_id)
    buffer = user_buffers.setdefault(chat_id, {})

    if text == "/start":
        user_steps[chat_id] = None
        send_message(chat_id, "به بات خوش آمدی! ⛽️", MAIN_MENU)
        return "ok"

    if text == "ثبت سوختگیری ⛽️":
        user_steps[chat_id] = "ask_km"
        send_message(chat_id, "لطفاً کیلومتر فعلی را وارد کن:")
        return "ok"

    if step == "ask_km":
        try:
            km = float(text)
            buffer['km'] = km
            user_steps[chat_id] = "ask_liter"
            send_message(chat_id, "حالا مقدار لیتر را وارد کن:")
        except:
            send_message(chat_id, "⛔️ لطفاً عدد معتبر وارد کن:")
        return "ok"

    if step == "ask_liter":
        try:
            liter = float(text)
            buffer['liter'] = liter
            summary = f"✅ اطلاعات:\nکیلومتر: {buffer['km']}\nلیتر: {buffer['liter']}"
            user_steps[chat_id] = "awaiting_confirmation"
            send_message(chat_id, summary + "\nآیا تأیید می‌کنی؟", [["✅ بله", "❌ خیر"]])
        except:
            send_message(chat_id, "⛔️ لطفاً عدد معتبر وارد کن:")
        return "ok"

    if step == "awaiting_confirmation":
        if text == "✅ بله":
            insert_log(buffer['km'], buffer['liter'])
            send_message(chat_id, "✅ با موفقیت ثبت شد!", MAIN_MENU)
            user_steps[chat_id] = None
            user_buffers[chat_id] = {}
        elif text == "❌ خیر":
            send_message(chat_id, "⛔️ عملیات لغو شد.", MAIN_MENU)
            user_steps[chat_id] = None
            user_buffers[chat_id] = {}
        else:
            send_message(chat_id, "لطفاً از گزینه‌های زیر انتخاب کن:", [["✅ بله", "❌ خیر"]])
        return "ok"

    if text == "📦 بکاپ سوختگیری":
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            send_message(chat_id, "⛔️ شما دسترسی ندارید.", MAIN_MENU)
            return "ok"
        file_obj = generate_csv()
        send_document(chat_id, file_obj, "fuel_backup.csv", caption="📦 بکاپ اطلاعات سوخت‌گیری")
        return "ok"

    if text == "📊 نمودار مصرف":
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            send_message(chat_id, "⛔️ شما دسترسی ندارید.", MAIN_MENU)
            return "ok"
        chart_buf = generate_chart()
        if chart_buf:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            files = {'photo': ('chart.png', chart_buf)}
            data = {'chat_id': chat_id, 'caption': '📊 نمودار مصرف سوخت'}
            requests.post(url, files=files, data=data)
        else:
            send_message(chat_id, "❗️داده کافی برای رسم نمودار وجود ندارد.")
        return "ok"

    send_message(chat_id, "دستور ناشناخته. لطفاً از منو استفاده کن.", MAIN_MENU)
    return "ok"

if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=80)
