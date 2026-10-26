# اجرای ۲۴ ساعته آبشده خضری روی Ubuntu

این فایل‌ها فقط آماده‌سازی هستند؛ هنوز سرور ساخته یا فعال نشده است.

1. یک ماشین Ubuntu 24.04 رایگان Always Free در Oracle Cloud بسازید و SSH key را امن نگه دارید.
2. از طریق SSH به ماشین وصل شوید. در Ubuntu با کاربر `ubuntu`:
   ```bash
   sudo apt update && sudo apt install -y python3-venv python3-pip git
   sudo git clone https://github.com/brdya0686-a11y/abshode-khezri-bot.git /opt/abshode-khezri-bot
   sudo chown -R ubuntu:ubuntu /opt/abshode-khezri-bot
   cd /opt/abshode-khezri-bot
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. روی سرور فایل خصوصی `/etc/abshode-khezri.env` را با مقادیر زیر بسازید؛ توکن را در GitHub یا گفتگو منتشر نکنید:
   ```ini
   BOT_TOKEN=REPLACE_WITH_REAL_TOKEN
   CHANNEL_ID=@khezriabshodeh
   ```
   دسترسی فایل: `sudo chmod 600 /etc/abshode-khezri.env`.
4. `sudo cp /opt/abshode-khezri-bot/deploy/abshode-khezri.service /etc/systemd/system/`
5. **قبل از فعال کردن سرویس** زمان‌بندی و اجرای خودکار workflow موجود در GitHub Actions را غیرفعال کنید؛ اجرای هم‌زمان دو نسخه می‌تواند نرخ تکراری منتشر کند و فایل state مشترک نیست.
6. `sudo systemctl daemon-reload && sudo systemctl enable --now abshode-khezri`
7. بررسی: `sudo systemctl status abshode-khezri` و `sudo journalctl -u abshode-khezri -f`.

تنظیمات سرویس: بررسی هر ۱۰ ثانیه، اجرای پیوسته (`RUN_SECONDS=0`)، بررسی نرخ در تمام ساعات (`PRICE_24H=1`)، راه‌اندازی مجدد خودکار. نرخ تنها در صورت تازه بودن پیام یکی از دو منبع ارسال می‌شود. پیام‌های صبحگاهی و گزارش شبانه همچنان در ساعات تعیین‌شده ارسال می‌شوند.

توجه: state اولیه باید از نسخه GitHub قبل از خاموش کردن workflow منتقل شود؛ وگرنه ممکن است اولین نرخ معتبر مجدداً ارسال شود. فایل `.bot_state.json` داخل مخزن وجود دارد اما باید آخرین نسخه‌اش را پیش از شروع سرویس pull کرد.

Oracle Always Free نیازمند ثبت‌نام و احتمالاً تأیید کارت است؛ منابع ممکن است موجود نباشند و دسترس‌پذیری ۱۰۰٪ تضمین نیست.
