"""Auto-post recent gold rates from two public Telegram sources.

Never publish invented, expired, or duplicate rates. GitHub Actions supplies
BOT_TOKEN and CHANNEL_ID; state is saved between scheduled runs.
"""
import datetime as datetime
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

SOURCES = ("valaneia_gold", "alizadeyazd")
DESTINATION = os.getenv("CHANNEL_ID", "@khezriabshodeh")
TOKEN = os.getenv("BOT_TOKEN", "")
CHECK_SECONDS = 10
MAX_AGE_SECONDS = 300
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "270"))
STATE_FILE = ".bot_state.json"
IRAN_TZ = ZoneInfo("Asia/Tehran")
DAY_START_HOUR = 8
DAY_END_HOUR = 1
CHANNEL_LINK = '<b><a href="https://t.me/khezriabshodeh">@khezriabshodeh</a></b>'

RATE_WORDS = re.compile(r"آب\s*[‌-]?\s*شده|مظنه|مثقال|(?:۱۸|18)\s*عیار")
RATE_NUMBERS = re.compile(r"\d[\d,.\u066b\u066c،]{3,}")


def state_load():
    result = {
        "last_time": "", "last_signature": "", "seen": [],
        "daily": {}, "morning_sent": [], "night_sent": [],
    }
    try:
        with open(STATE_FILE, encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            return result
        for field in result:
            if field in data and isinstance(data[field], type(result[field])):
                result[field] = data[field]
        result["seen"] = result["seen"][-80:]
        return result
    except (FileNotFoundError, ValueError, OSError):
        return result


def state_save(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


def fetch_source(source):
    req = urllib.request.Request(
        f"https://t.me/s/{source}?v={int(time.time())}",
        headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        html = response.read(2_000_000).decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    now = datetime.datetime.now(datetime.timezone.utc)
    results = []
    for post in soup.select(".tgme_widget_message[data-post]"):
        timestamp = post.select_one("time[datetime]")
        message = post.select_one(".tgme_widget_message_text")
        if timestamp is None or message is None:
            continue
        text = message.get_text("\n", strip=True)
        if not RATE_WORDS.search(text) or not RATE_NUMBERS.search(text):
            continue
        try:
            posted_at = datetime.datetime.fromisoformat(timestamp["datetime"].replace("Z", "+00:00"))
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=datetime.timezone.utc)
            posted_at = posted_at.astimezone(datetime.timezone.utc)
        except (ValueError, KeyError):
            continue
        age = (now - posted_at).total_seconds()
        if not 0 <= age <= MAX_AGE_SECONDS:
            continue
        post_id = post.get("data-post", "")
        if not post_id.startswith(source + "/"):
            continue
        numbers = [re.sub(r"\D", "", number) for number in RATE_NUMBERS.findall(text)]
        signature = ":".join(numbers[:8])
        results.append((posted_at, post_id, text, source, signature))
    return results


def format_rate_message(raw_text):
    """Compact branded rates with western bold digits and no source footer."""
    digit_map = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٬٫",
        "01234567890123456789,.",
    )
    lines = []
    pending_marker = ""
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Never publish source identity, source timestamps, or attribution.
        if re.match(r"^(?:منبع|source)\s*[:：]", line, re.IGNORECASE):
            continue
        if re.match(r"^(?:زمان\s*اعلام|زمان\s*منبع|تاریخ\s*اعلام|time\s*of|source\s*time|UTC\b)", line, re.IGNORECASE):
            continue
        if re.match(r"^(?:زمان|تاریخ|ساعت|date|time)\s*[:：]", line, re.IGNORECASE):
            continue
        line = re.sub(r"https?://(?:t\.me|telegram\.me)/\S+", "", line, flags=re.IGNORECASE)
        line = re.sub(r"@[A-Za-z][A-Za-z0-9_]*", "", line)
        line = re.sub(r"\b(?:valaneia_gold|alizadeyazd)\b", "", line, flags=re.IGNORECASE)
        line = line.translate(digit_map)
        line = re.sub(r"\s*[:：]\s*", ": ", line).strip()
        if not line:
            continue
        if line in ("💰", "◀️", "◀", "▶️", "▶", "🟢", "🔴", "🔵", "🟡", "🟠", "⚪️"):
            pending_marker = line
            continue
        if pending_marker:
            line = f"{pending_marker} {line}"
            pending_marker = ""
        # Telegram supports colored emoji, but not custom font colors.
        side_line = re.sub(r"^(?:🟢|🔴|🟩|🟥)\s*", "", line)
        side = re.match(r"^(فروش|خرید)\s*:", side_line)
        if side:
            side_name = side.group(1)
            icon = "🟩" if side_name == "فروش" else "🟥"
            line = f"{icon} <b>{side_name}</b>:" + html.escape(side_line[side.end():])
        else:
            line = html.escape(line)

        line = re.sub(
            r"(?<!\w)\d+(?:[,.]\d+)*",
            lambda match: f"<b>{match.group(0)}</b>",
            line,
        )
        lines.append(line)
    if not lines:
        return ""
    return "📊 نرخ بازار\n" + "\n".join(lines) + '\n<b><a href="https://t.me/khezriabshodeh">@khezriabshodeh</a></b>'


def _telegram_send(text, formatted=True):
    payload = {
        "chat_id": DESTINATION,
        "text": text,
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    if formatted:
        payload["parse_mode"] = "HTML"
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        # Log Telegram's actual reason rather than an opaque HTTP 400.
        try:
            detail = json.loads(error.read().decode("utf-8")).get("description", "unknown reason")
        except (ValueError, UnicodeDecodeError):
            detail = str(error.reason)
        raise RuntimeError(f"Telegram HTTP {error.code}: {detail}") from error
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram rejected sendMessage: {reply.get('description', 'unknown reason')}")


def send_rate(text):
    try:
        _telegram_send(text, formatted=True)
    except RuntimeError as error:
        # A malformed HTML entity must not block a real, verified price.
        reason = str(error).lower()
        if "can't parse entities" not in reason and "message is too long" not in reason:
            raise
        plain = html.unescape(re.sub(r"</?(?:b|i|a)(?:\\s+[^>]*)?>", "", text))
        print(f"Telegram HTML rejected; retrying verified price as plain text: {error}", flush=True)
        _telegram_send(plain[:3900], formatted=False)



def iran_time():
    return datetime.datetime.now(IRAN_TZ)


def in_price_hours(at):
    """08:00 through 00:59 Iran time; closed 01:00-07:59."""
    return at.hour >= DAY_START_HOUR or at.hour < DAY_END_HOUR


def session_day(at):
    """A session starts 08:00 and ends at 01:00 on the following date."""
    day = at.date()
    if at.hour < DAY_START_HOUR:
        day -= datetime.timedelta(days=1)
    return day.isoformat()


def parse_observations(raw_text):
    """Only extract explicit labeled buy/sell prices; never guess rates."""
    if re.search(r"پس[\s‌-]*فردای", raw_text):
        market = "پس‌فردایی"
    elif re.search(r"فردای", raw_text):
        market = "فردایی"
    elif re.search(r"نقدی|نقد", raw_text):
        market = "نقدی"
    else:
        market = "نوع معامله نامشخص"
    unit = None
    observations = {}
    digit_map = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٬٫", "01234567890123456789,.")
    for source_line in raw_text.splitlines():
        line = source_line.translate(digit_map)
        if re.search(r"هر\s*مثقال|مظنه", line):
            unit = "مثقال"
        elif re.search(r"هر\s*گرم|گرم\s*(?:18|عیار)", line):
            unit = "گرم"
        elif re.search(r"آب\s*[‌-]?\s*شده", line) and re.search(r"خرید|فروش", line):
            unit = "آبشده"
        match = re.search(r"(فروش|خرید)\s*[:：]\s*([0-9][0-9,.]{3,})", line)
        if not match or not unit:
            continue
        digits = match.group(2).replace(",", "")
        try:
            price = int(digits)
        except ValueError:
            continue
        if price <= 0:
            continue
        key = f"{market} | {unit} | {match.group(1)}"
        observations[key] = price
    return observations


def record_observations(state, posted_at, raw_text):
    day = session_day(posted_at.astimezone(IRAN_TZ))
    observed = parse_observations(raw_text)
    if not observed:
        return
    today = state.setdefault("daily", {}).setdefault(day, {})
    for name, price in observed.items():
        item = today.setdefault(name, {"high": price, "low": price, "first": price, "last": price, "count": 0})
        item["high"] = max(item["high"], price)
        item["low"] = min(item["low"], price)
        item["last"] = price
        item["count"] += 1
    for old in sorted(state["daily"])[:-4]:
        del state["daily"][old]


def night_summary(state, day):
    rates = state.get("daily", {}).get(day, {})
    parts = ["🌙 <b>شب بخیر | جمع‌بندی روزانه</b>"]
    if not rates:
        parts.append("امروز نرخ کافی برای محاسبه سقف و کف ثبت نشد.")
    else:
        parts.append("📊 سقف و کف نرخ‌های ثبت‌شده توسط ربات:")
        for name, item in sorted(rates.items())[:12]:
            parts.append(f"◀️ <b>{html.escape(name)}</b>")
            parts.append(f"⬆️ بالاترین: <b>{item['high']:,}</b> | ⬇️ پایین‌ترین: <b>{item['low']:,}</b>")
            if item["count"] > 1:
                diff = item["last"] - item["first"]
                direction = "📈" if diff > 0 else "📉" if diff < 0 else "➖"
                parts.append(f"{direction} تغییر نسبت به اولین نرخ ثبت‌شده: <b>{diff:+,}</b>")
        if len(rates) > 12:
            parts.append("سایر نرخ‌ها برای کوتاه ماندن گزارش نمایش داده نشدند.")
        parts.append("این اعداد فقط از نرخ‌هایی هستند که ربات ثبت کرده، نه کل معاملات بازار.")
    parts.append(CHANNEL_LINK)
    return "\n".join(parts)[:4000]


def send_scheduled_messages(state):
    now = iran_time()
    day = now.date().isoformat()
    if now.hour == DAY_START_HOUR and day not in state["morning_sent"]:
        morning = "☀️ <b>صبح بخیر</b>\nروزتون پرسود و پربرکت 🌷\n📊 انتشار نرخ‌های تازه آغاز شد.\n" + CHANNEL_LINK
        send_rate(morning)
        state["morning_sent"] = (state["morning_sent"] + [day])[-7:]
        state_save(state)
        print(f"Morning greeting sent for {day}", flush=True)
    if now.hour == DAY_END_HOUR:
        previous_day = (now.date() - datetime.timedelta(days=1)).isoformat()
        if previous_day not in state["night_sent"]:
            send_rate(night_summary(state, previous_day))
            state["night_sent"] = (state["night_sent"] + [previous_day])[-7:]
            state_save(state)
            print(f"Night summary sent for {previous_day}", flush=True)


def check_once(state, executor):
    if not in_price_hours(iran_time()):
        return
    tasks = {source: executor.submit(fetch_source, source) for source in SOURCES}
    posts = []
    for source, task in tasks.items():
        try:
            posts.extend(task.result())
        except Exception as error:
            print(f"Source @{source} unavailable: {type(error).__name__}: {error}", flush=True)
    if not posts:
        print("No recent valid rate on either public preview (possibly delayed); publishing nothing", flush=True)
        return
    posts.sort(key=lambda p: (p[0], p[3] == SOURCES[0]), reverse=True)
    posted_at, post_id, text, source, signature = posts[0]
    date_key = posted_at.isoformat()
    if post_id in state["seen"] or date_key <= state["last_time"]:
        return
    # A second source may repeat the same figures with slightly different text.
    if signature == state["last_signature"]:
        state["last_time"] = date_key
        state["seen"] = (state["seen"] + [post_id])[-80:]
        state_save(state)
        return
    message = format_rate_message(text[:2400])
    if not message:
        print("Source text contained no publishable rate after cleanup", flush=True)
        return
    try:
        send_rate(message)
    except Exception as error:
        print(f"Publishing failed; state unchanged: {type(error).__name__}: {error}", flush=True)
        return
    state["last_time"] = date_key
    state["last_signature"] = signature
    state["seen"] = (state["seen"] + [post_id])[-80:]
    record_observations(state, posted_at, text)
    state_save(state)
    print(f"Published verified recent source post: {post_id}", flush=True)


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not configured in GitHub secrets")
    state = state_load()
    deadline = time.monotonic() + RUN_SECONDS
    print("Abshode Khezri price relay started", flush=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        while time.monotonic() < deadline:
            start = time.monotonic()
            try:
                send_scheduled_messages(state)
            except Exception as error:
                print(f"Scheduled message error: {type(error).__name__}: {error}", flush=True)
            check_once(state, executor)
            wait = CHECK_SECONDS - (time.monotonic() - start)
            if wait > 0:
                time.sleep(min(wait, max(0, deadline - time.monotonic())))
    print("Price relay scheduled window completed", flush=True)


if __name__ == "__main__":
    main()
