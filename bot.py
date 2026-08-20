# ============================================================
# ربات دانلودر اورجینال اسپاتیفای - نسخه Render
# بدون تبدیل فرمت و استخراج مستقیم استریم خام
# ============================================================

import os
import re
import json
import time
import threading
import yt_dlp
from flask import Flask
import telebot
from telebot import types

# ============================================================
# تنظیمات اصلی
# ============================================================
TOKEN = "8135900333:AAH2MTWecY7q3le28GZPppbJhnVwq276xfY"
DATA_FILE = "audio_bot_db.json"
VERSION = "1.0"

# ============================================================
# سرور Flask برای زنده ماندن در Render
# ============================================================
app = Flask('')
@app.route('/')
def home():
    return "Spotify Audio Downloader Bot is Online!"

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

# ============================================================
# ربات تلگرام
# ============================================================
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=False)

# دیتابیس ساده برای ذخیره تنظیمات فرمت کاربران
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"users": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

db = load_data()

def get_user(uid):
    uid = str(uid)
    if uid not in db["users"]:
        # فرمت پیش‌فرض: بهترین کیفیت موجود بدون تبدیل (Auto)
        db["users"][uid] = {"format": "auto"}
        save_data(db)
    return db["users"][uid]

# ============================================================
# کیبوردها
# ============================================================
def settings_keyboard(uid):
    user_format = get_user(uid).get("format", "auto")
    
    # استایل دکمه‌ها بر اساس انتخاب کاربر
    btn_auto = "✅ Auto (بهترین)" if user_format == "auto" else "Auto (بهترین)"
    btn_m4a = "✅ M4A (AAC)" if user_format == "m4a" else "M4A (AAC)"
    btn_webm = "✅ WEBM (Opus)" if user_format == "webm" else "WEBM (Opus)"

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(btn_auto, callback_data="set_fmt_auto"))
    kb.add(types.InlineKeyboardButton(btn_m4a, callback_data="set_fmt_m4a"))
    kb.add(types.InlineKeyboardButton(btn_webm, callback_data="set_fmt_webm"))
    kb.add(types.InlineKeyboardButton("بستن ❌", callback_data="close_panel"))
    return kb

def main_reply_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(types.KeyboardButton("⚙️ تنظیمات فرمت"))
    return kb

# ============================================================
# هندلرهای دستورات
# ============================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    uid = message.from_user.id
    get_user(uid)  # ثبت کاربر
    
    text = (
        f"سلام <b>{message.from_user.first_name}</b> 👋\n\n"
        "من ربات دانلود <b>مستقیم و اورجینال</b> فایل‌های صوتی اسپاتیفای هستم.\n"
        "فایل‌ها <u>بدون هیچ‌گونه تبدیل فرمت یا افت کیفیت</u>، مستقیماً از استریم خام دریافت و برای شما ارسال می‌شوند.\n\n"
        "🔗 <b>لطفاً لینک آهنگ اسپاتیفای خود را بفرستید:</b>"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_reply_keyboard())

@bot.message_handler(func=lambda m: m.text == "⚙️ تنظیمات فرمت" or m.text == "/settings")
def show_settings(message):
    uid = message.from_user.id
    text = (
        "⚙️ <b>تنظیمات فرمت فایل خروجی</b>\n\n"
        "از آنجا که ما فایل‌ها را تبدیل (Convert) نمی‌کنیم، می‌توانید انتخاب کنید کدام استریم خام دانلود شود:\n\n"
        "🔹 <b>Auto:</b> بالاترین کیفیت استریم موجود.\n"
        "🔹 <b>M4A:</b> فرمت استاندارد AAC (پشتیبانی عالی در آیفون و تلگرام).\n"
        "🔹 <b>WEBM:</b> فرمت Opus (کیفیت بسیار بالا اما پشتیبانی کمتر در پلیرهای قدیمی)."
    )
    bot.send_message(message.chat.id, text, reply_markup=settings_keyboard(uid))

# ============================================================
# هندلر دکمه‌های شیشه‌ای
# ============================================================
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    chat_id = call.message.chat.id
    mid = call.message.message_id
    uid = str(call.from_user.id)

    if call.data == "close_panel":
        bot.delete_message(chat_id, mid)
        return

    if call.data.startswith("set_fmt_"):
        new_fmt = call.data.split("_")[2]
        db["users"][uid]["format"] = new_fmt
        save_data(db)
        
        try:
            bot.edit_message_reply_markup(chat_id, mid, reply_markup=settings_keyboard(uid))
            bot.answer_callback_query(call.id, f"فرمت به {new_fmt.upper()} تغییر یافت ✅")
        except:
            pass

# ============================================================
# هسته دانلودر بدون تبدیل
# ============================================================
@bot.message_handler(func=lambda m: "spotify.com" in m.text)
def handle_spotify_link(message):
    chat_id = message.chat.id
    uid = str(message.from_user.id)
    url = message.text.strip()

    status_msg = bot.send_message(chat_id, "📥 <b>در حال استخراج استریم اورجینال...</b>\n<i>لطفاً چند لحظه صبر کنید.</i>")
    
    # خواندن تنظیمات کاربر
    user_fmt = get_user(uid).get("format", "auto")
    
    # منطق انتخاب فرمت خام در yt-dlp بدون افت کیفیت
    if user_fmt == "m4a":
        format_selector = "bestaudio[ext=m4a]/bestaudio"
    elif user_fmt == "webm":
        format_selector = "bestaudio[ext=webm]/bestaudio"
    else:
        format_selector = "bestaudio/best"

    ydl_opts = {
        'format': format_selector,
        'outtmpl': f'%(title)s - %(uploader)s_{chat_id}.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        # هیچ postprocessor (مثل FFmpeg) استفاده نمی‌کنیم تا تبدیل انجام نشود
    }

    downloaded_file = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # استخراج اطلاعات و دانلود
            info_dict = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info_dict)
            
            title = info_dict.get('title', 'Unknown Title')
            artist = info_dict.get('uploader', 'Unknown Artist')

        # تغییر وضعیت به در حال آپلود
        bot.edit_message_text("📤 <b>در حال آپلود فایل صوتی اورجینال...</b>", chat_id, status_msg.message_id)

        # ارسال فایل صوتی در تلگرام
        with open(downloaded_file, 'rb') as audio_file:
            caption = f"🎵 <b>{title}</b>\n🎤 {artist}\n\n💎 <i>فایل اورجینال - بدون افت کیفیت</i>"
            bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                caption=caption,
                title=title,
                performer=artist
            )
            
        # پاک کردن پیام پردازش
        bot.delete_message(chat_id, status_msg.message_id)

    except Exception as e:
        print(f"[DOWNLOAD ERROR] {e}")
        bot.edit_message_text("❌ متأسفانه دریافت فایل اصلی با مشکل مواجه شد. لطفاً لینک دیگری را امتحان کنید.", chat_id, status_msg.message_id)
        
    finally:
        # پاکسازی فایل محلی از سرور Render
        if downloaded_file and os.path.exists(downloaded_file):
            os.remove(downloaded_file)

if __name__ == "__main__":
    print(f"Audio Bot V{VERSION} Started!")
    bot.infinity_polling(skip_pending=True, none_stop=True)


