import os
import io
import csv
import matplotlib.pyplot as plt
import pandas as pd
import psycopg2
from urllib.parse import urlparse
from flask import Flask, request
import requests
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

# تنظیم مسیر موقت برای matplotlib
os.environ['MPLCONFIGDIR'] = '/tmp'

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_CHAT_IDS = [int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip()]

app = Flask(__name__)

# حافظه وضعیت کاربران
user_steps = {}
user_buffers = {}

MAIN_MENU = [
    ["ثبت سوختگیری ⛽️"],
    ["📦 بکاپ سوختگیری", "📊 نمودار مصرف"],
    ["🗃️ مدیریت داده"]
]

# زیر منوی مدیریت داده
DATA_MENU = [["📥 وارد کردن داده"], ["🗑️ حذف داده"], ["بازگشت"]]

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


def get_postgres_connection():
    url = urlparse(DATABASE_URL)
    return psycopg2.connect(
        dbname=url.path[1:],
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port
    )


def init_db():
    conn = get_postgres_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS fuel_logs (
            id SERIAL PRIMARY KEY,
            km REAL,
            liter REAL,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()


def insert_log(km, liter):
    conn = get_postgres_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO fuel_logs (km, liter, timestamp) VALUES (%s, %s, %s)",
        (km, liter, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def generate_csv():
    conn = get_postgres_connection()
    df = pd.read_sql_query("SELECT * FROM fuel_logs ORDER BY id", conn)
    conn.close()
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return io.BytesIO(output.read().encode("utf-8"))


def generate_chart():
    conn = get_postgres_connection()
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
    scatter = plt.scatter(
        reliable_df['Kilometer'], reliable_df['fuel_per_100km'],
        s=marker_sizes, c=reliable_df['Liter'], cmap='Blues', alpha=0.8
    )
    if len(noisy_df):
        plt.scatter(
            noisy_df['Kilometer'], noisy_df['fuel_per_100km'],
            s=noisy_df['Liter'] * 7, c='red', alpha=0.6, marker='x'
        )
    plt.plot(reliable_df['Kilometer'], reliable_df['ma_small'], linewidth=1.3)
    plt.plot(reliable_df['Kilometer'], reliable_df['ma_large'], linewidth=1.5)
    plt.axhline(avg, linestyle='--', linewidth=1)
    last_points = reliable_df[reliable_df['is_last']]
    labels = list(range(1, 6))[::-1]
    for i, (idx, row) in enumerate(last_points[::-1].iterrows()):
        plt.text(row['Kilometer'], row['fuel_per_100km'], str(labels[i]), ha='center')
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

    # دستور شروع
    if text == "/start":
        user_steps[chat_id] = None
        send_message(chat_id, "به بات خوش آمدی! ⛽️", MAIN_MENU)
        return "ok"

    # ثبت سوختگیری
    if text == "ثبت سوختگیری ⛽️":
        user_steps[chat_id] = "ask_km"
        send_message(chat_id, "لطفاً کیلومتر فعلی را وارد کن:")
        return "ok"

    # دریافت کیلومتر
    if step == "ask_km":
        try:
            km = float(text)
            buffer['km'] = km
            user_steps[chat_id] = "ask_liter"
            send_message(chat_id, "حالا مقدار لیتر را وارد کن:")
        except:
            send_message(chat_id, "⛔️ لطفاً عدد معتبر وارد کن:")
        return "ok"

    # دریافت لیتر و تایید
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

    # نهایی کردن یا لغو
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
            send_message(chat_id, "لطفاً گزینه مناسب را انتخاب کن:", [["✅ بله", "❌ خیر"]])
        return "ok"

    # بکاپ CSV
    if text == "📦 بکاپ سوختگیری":
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            send_message(chat_id, "⛔️ شما دسترسی ندارید.", MAIN_MENU)
            return "ok"
        file_obj = generate_csv()
        send_document(chat_id, file_obj, "fuel_backup.csv", caption="📦 بکاپ اطلاعات سوخت‌گیری")
        return "ok"

    # نمودار مصرف
    if text == "📊 نمودار مصرف":
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            send_message(chat_id, "⛔️ شما دسترسی ندارید.", MAIN_MENU)
            return "ok"
        try:
            chart_buf = generate_chart()
            if chart_buf:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
                files = {'photo': ('chart.png', chart_buf)}
                data = {'chat_id': chat_id, 'caption': '📊 نمودار مصرف سوخت'}
                requests.post(url, files=files, data=data)
            else:
                send_message(chat_id, "❗️ داده کافی برای رسم نمودار وجود ندارد.")
        except Exception as e:
            send_message(chat_id, f"❌ خطا در تولید نمودار: {e}")
        return "ok"

    # مدیریت داده
    if text == "🗃️ مدیریت داده":
        user_steps[chat_id] = "data_menu"
        send_message(chat_id, "مدیریت داده‌ها:", DATA_MENU)
        return "ok"

    # زیر منوی مدیریت داده
    if user_steps.get(chat_id) == "data_menu":
        if text == "📥 وارد کردن داده":
            user_steps[chat_id] = "awaiting_csv"
            send_message(chat_id, "لطفاً فایل CSV خود را ارسال کن.")
        elif text == "🗑️ حذف داده":
            user_steps[chat_id] = "awaiting_delete_id"
            send_message(chat_id, "لطفاً آیدی ردیف مورد نظر برای حذف را وارد کن.")
        elif text == "بازگشت":
            user_steps[chat_id] = None
            send_message(chat_id, "بازگشت به منوی اصلی.", MAIN_MENU)
        else:
            send_message(chat_id, "لطفاً گزینه‌ای از منو انتخاب کن.", DATA_MENU)
        return "ok"

    # دریافت فایل CSV برای وارد کردن داده
    if user_steps.get(chat_id) == "awaiting_csv":
        doc = data['message'].get('document')
        if doc:
            file_id = doc['file_id']
            # دانلود فایل CSV
            file_info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}").json()
            file_path = file_info['result']['file_path']
            file_content = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}").content
            try:
                df = pd.read_csv(io.BytesIO(file_content))
                inserted = 0
                conn = get_postgres_connection()
                c = conn.cursor()
                for _, row in df.iterrows():
                    c.execute("INSERT INTO fuel_logs (km, liter, timestamp) VALUES (%s,%s,%s)",
                              (row['km'], row['liter'], datetime.now().isoformat()))
                    inserted += 1
                conn.commit()
                conn.close()
                send_message(chat_id, f"✅ تعداد {inserted} رکورد وارد شد.", MAIN_MENU)
                user_steps[chat_id] = None
                user_buffers[chat_id] = {}
            except Exception as e:
                send_message(chat_id, f"❌ خطا در خواندن CSV: {e}", DATA_MENU)
        else:
            send_message(chat_id, "⛔️ لطفاً یک فایل CSV ارسال کن.", DATA_MENU)
        return "ok"

    # حذف رکورد با آیدی
    if user_steps.get(chat_id) == "awaiting_delete_id":
        try:
            rec_id = int(text)
            conn = get_postgres_connection()
            c = conn.cursor()
            c.execute("DELETE FROM fuel_logs WHERE id = %s", (rec_id,))
            deleted = c.rowcount
            conn.commit()
            conn.close()
            if deleted:
                send_message(chat_id, f"✅ رکورد با آیدی {rec_id} حذف شد.", MAIN_MENU)
            else:
                send_message(chat_id, f"⚠️ رکوردی با آیدی {rec_id} یافت نشد.", DATA_MENU)
        except Exception as e:
            send_message(chat_id, f"❌ ورودی نامعتبر یا خطا: {e}", DATA_MENU)
        user_steps[chat_id] = None
        return "ok"

    # دستور ناشناخته
    send_message(chat_id, "دستور ناشناخته. لطفاً از منو استفاده کن.", MAIN_MENU)
    return "ok"

if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=80)
