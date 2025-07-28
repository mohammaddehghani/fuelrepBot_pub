import os
import io
import matplotlib.pyplot as plt
import pandas as pd
import requests
import psycopg2
from urllib.parse import urlparse
from flask import Flask, request
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables\load_dotenv()

# Configure matplotlib temporary directory
os.environ['MPLCONFIGDIR'] = '/tmp'

# Bot and database configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_CHAT_IDS = [int(x) for x in os.getenv('ADMIN_CHAT_IDS','').split(',') if x.strip()]

app = Flask(__name__)

# In-memory user state
user_steps = {}
user_buffers = {}

# Keyboards
MAIN_MENU = [['ثبت سوختگیری ⛽️'], ['📦 بکاپ سوختگیری', '📊 نمودار مصرف'], ['🗃️ مدیریت داده']]
DATA_MENU = [['📥 وارد کردن داده'], ['🗑️ حذف داده'], ['بازگشت']]
CANCEL_COMMANDS = ['بازگشت','/menu','لغو']

# Helpers

def send_message(chat_id, text, buttons=None):
    payload = {'chat_id': chat_id, 'text': text}
    if buttons:
        payload['reply_markup'] = {'keyboard': buttons, 'resize_keyboard': True}
    requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', json=payload)


def send_document(chat_id, file_bytes, filename, caption=''):
    files = {'document': (filename, file_bytes)}
    data = {'chat_id': chat_id, 'caption': caption}
    requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendDocument', data=data, files=files)


def get_connection():
    url = urlparse(DATABASE_URL)
    return psycopg2.connect(
        dbname=url.path[1:], user=url.username,
        password=url.password, host=url.hostname, port=url.port
    )


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS fuel_logs (
        id SERIAL PRIMARY KEY,
        km REAL NOT NULL,
        liter REAL NOT NULL,
        timestamp TIMESTAMP NOT NULL
    )''')
    conn.commit()
    conn.close()


def insert_log(km, liter):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('INSERT INTO fuel_logs (km,liter,timestamp) VALUES (%s,%s,%s) RETURNING id',
                (km, liter, datetime.now()))
    new_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return new_id


def generate_csv():
    conn = get_connection()
    df = pd.read_sql('SELECT * FROM fuel_logs ORDER BY id', conn)
    conn.close()
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return io.BytesIO(buf.read().encode())


def generate_chart():
    conn = get_connection()
    df = pd.read_sql('SELECT km AS Kilometer, liter AS Liter FROM fuel_logs ORDER BY id', conn)
    conn.close()
    if len(df) < 5:
        return None
    df['distance'] = df['Kilometer'].diff()
    df['fuel_per_100km'] = (df['Liter'] / df['distance']) * 100
    df.dropna(inplace=True)
    df['is_reliable'] = df['Liter'] >= 12
    reliable = df[df['is_reliable']].copy()
    noisy = df[~df['is_reliable']]
    reliable['ma_small'] = reliable['fuel_per_100km'].rolling(5).mean()
    reliable['ma_large'] = reliable['fuel_per_100km'].rolling(15).mean()
    avg = reliable['fuel_per_100km'].mean()
    reliable['is_last'] = False
    reliable.loc[reliable.tail(5).index,'is_last'] = True

    plt.figure(figsize=(12,6))
    sc = plt.scatter(reliable['Kilometer'], reliable['fuel_per_100km'],
                     s=reliable['Liter']*7, c=reliable['Liter'], cmap='Blues', alpha=0.8)
    if not noisy.empty:
        plt.scatter(noisy['Kilometer'], noisy['fuel_per_100km'],
                    s=noisy['Liter']*7, c='red', marker='x', alpha=0.6)
    plt.plot(reliable['Kilometer'], reliable['ma_small'], label='MA 5')
    plt.plot(reliable['Kilometer'], reliable['ma_large'], label='MA 15')
    plt.axhline(avg, linestyle='--', label=f'Avg {avg:.1f}')
    for i,(idx,row) in enumerate(reliable[reliable['is_last']].iterrows(),1):
        plt.text(row['Kilometer'], row['fuel_per_100km'], str(i), ha='center')
    plt.colorbar(sc, label='Liters')
    plt.xlabel('Kilometer')
    plt.ylabel('L/100km')
    plt.title('Fuel Consumption Trend')
    plt.legend()
    plt.grid(alpha=0.3)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

# Webhook
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json() or {}
    msg = data.get('message')
    if not msg: return 'ok'
    chat_id = msg['chat']['id']
    text = msg.get('text','').strip()

    # Cancel
    if text in CANCEL_COMMANDS:
        user_steps.pop(chat_id,None)
        user_buffers.pop(chat_id,None)
        send_message(chat_id, 'بازگشت به منوی اصلی.', MAIN_MENU)
        return 'ok'

    step = user_steps.get(chat_id)
    buf = user_buffers.setdefault(chat_id,{})

    # Start or menu
    if text=='/start':
        user_steps[chat_id]=None
        send_message(chat_id,'به بات خوش آمدی! ⛽️',MAIN_MENU)
        return 'ok'

    # Register fuel
    if text=='ثبت سوختگیری ⛽️':
        user_steps[chat_id]='ask_km'
        send_message(chat_id,'لطفاً کیلومتر را وارد کنید:')
        return 'ok'
    if step=='ask_km':
        try:
            km=float(text); buf['km']=km
            user_steps[chat_id]='ask_liter'
            send_message(chat_id,'لطفاً لیتر را وارد کنید:')
        except:
            send_message(chat_id,'⛔️ عدد معتبر وارد کنید:')
        return 'ok'
    if step=='ask_liter':
        try:
            liter=float(text); buf['liter']=liter
            summary=f"✅ کیلومتر: {buf['km']}\nلیتر: {buf['liter']}"
            user_steps[chat_id]='await_confirm'
            send_message(chat_id, summary+"\nتایید؟", [['✅ بله','❌ خیر'], ['بازگشت']])
        except:
            send_message(chat_id,'⛔️ عدد معتبر وارد کنید:')
        return 'ok'
    if step=='await_confirm':
        if text=='✅ بله':
            fid=insert_log(buf['km'],buf['liter'])
            send_message(chat_id,f"✅ ثبت شد! (ID: {fid})",MAIN_MENU)
        else:
            send_message(chat_id,'عملیات لغو شد.',MAIN_MENU)
        user_steps.pop(chat_id,None)
        user_buffers.pop(chat_id,None)
        return 'ok'

    # Backup
    if text=='📦 بکاپ سوختگیری':
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            send_message(chat_id,'دسترسی ندارید.',MAIN_MENU); return 'ok'
        df=generate_csv(); send_document(chat_id,df,'backup.csv'); return 'ok'

    # Chart
    if text=='📊 نمودار مصرف':
        if ADMIN_CHAT_IDS and chat_id not in ADMIN_CHAT_IDS:
            send_message(chat_id,'دسترسی ندارید.',MAIN_MENU); return 'ok'
        try:
            chart=generate_chart()
            if chart: requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto',
                files={'photo':('chart.png',chart)}, data={'chat_id':chat_id})
            else: send_message(chat_id,'❗️داده کافی نیست.')
        except Exception as e: send_message(chat_id,f'❌ خطا: {e}')
        return 'ok'

    # Data management
    if text=='🗃️ مدیریت داده':
        user_steps[chat_id]='data_menu'
        send_message(chat_id,'مدیریت داده:',DATA_MENU)
        return 'ok'
    if step=='data_menu':
        if text=='📥 وارد کردن داده': user_steps[chat_id]='load_csv'; send_message(chat_id,'ارسال CSV');
        elif text=='🗑️ حذف داده': user_steps[chat_id]='del_id'; send_message(chat_id,'وارد کردن ID');
        else: send_message(chat_id,'بازگشت',MAIN_MENU); user_steps.pop(chat_id)
        return 'ok'
    if step=='load_csv':
        doc=msg.get('document')
        if doc:
            fid=doc['file_id']; info=requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={fid}').json()
            path=info['result']['file_path']; content=requests.get(f'https://api.telegram.org/file/bot{BOT_TOKEN}/{path}').content
            try:
                df=pd.read_csv(io.BytesIO(content)); conn=get_connection(); cur=conn.cursor(); cnt=0
                for _,r in df.iterrows(): cur.execute('INSERT INTO fuel_logs (km,liter,timestamp) VALUES (%s,%s,%s)',
                    (r['km'],r['liter'],datetime.now())); cnt+=1
                conn.commit(); conn.close(); send_message(chat_id,f'{cnt} رکورد وارد شد.',MAIN_MENU)
            except Exception as e: send_message(chat_id,f'خطا: {e}',DATA_MENU)
        else: send_message(chat_id,'ارسال فایل CSV',DATA_MENU)
        user_steps.pop(chat_id); return 'ok'
    if step=='del_id':
        try: rid=int(text); conn=get_connection(); cur=conn.cursor(); cur.execute('DELETE FROM fuel_logs WHERE id=%s',(rid,));
            if cur.rowcount: send_message(chat_id,f'{rid} حذف شد.',MAIN_MENU)
            else: send_message(chat_id,'پیدا نشد.',DATA_MENU)
            conn.commit(); conn.close()
        except Exception as e: send_message(chat_id,f'خطا: {e}',DATA_MENU)
        user_steps.pop(chat_id); return 'ok'

    # Unknown
    send_message(chat_id,'دستور نامشخص.',MAIN_MENU)
    return 'ok'

if __name__=='__main__':
    init_db()
    app.run(host='0.0.0.0',port=80)
