import os
import json
import time
import urllib.parse
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.getenv("CHANNEL_ID", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")
API = f"https://api.telegram.org/bot{TOKEN}/"

def call(method, **data):
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(API + method, data=body, timeout=40) as r:
        return json.load(r)

def send(chat_id, text):
    call("sendMessage", chat_id=chat_id, text=text)

offset = 0
print("Abshode Khezri bot started", flush=True)

while True:
    try:
        updates = call("getUpdates", offset=offset, timeout=25).get("result", [])
        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            user_id = str(msg.get("from", {}).get("id", ""))
            text = msg.get("text", "")
            if not chat_id:
                continue
            if text.startswith("/start"):
                send(chat_id, "به ربات آبشده خضری خوش آمدید!\nبرای دریافت شناسه خود /id را بفرستید.")
            elif text.startswith("/id"):
                send(chat_id, f"Your Telegram ID: {user_id}")
            elif text.startswith("/post "):
                if not ADMIN_ID or user_id != ADMIN_ID:
                    send(chat_id, "دسترسی انتشار ندارید.")
                elif not CHANNEL:
                    send(chat_id, "CHANNEL_ID تنظیم نشده است.")
                else:
                    message = text.split(" ", 1)[1]
                    send(CHANNEL, message)
                    send(chat_id, "پیام در کانال ارسال شد.")
    except Exception as e:
        print("Bot error:", str(e), flush=True)
        time.sleep(5)
