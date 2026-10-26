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
DAY_START_MINUTE = 30
DAY_END_HOUR = 1
CHANNEL_LINK = '<b><a href="https://t.me/khezriabshodeh">@khezriabshodeh</a></b>'

RATE_WORDS = re.compile(r"مثقال|مظنه")
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
        # A new post with only gram rates is not publishable.
        if not RATE_WORDS.search(text) or not RATE_NUMBERS.search(text) or not parse_mesghal_quotes(text):
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
        # Ignore gram changes when deciding whether the mesghal quote is new.
        signature = json.dumps(parse_mesghal_quotes(text), ensure_ascii=False, sort_keys=True)
        results.append((posted_at, post_id, text, source, signature))
    return results


# Only relay explicitly labeled mesghal (or mazaneh) rates.
# All amounts are in toman. Do not convert gram prices into mesghal prices.
SELL_MARKUP_TOMAN = 250_000
BUY_DISCOUNT_TOMAN = 50_000
PERSIAN_ARABIC_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"
)
MESGHAL_UNIT = re.compile(r"مثقال|مظنه")
OTHER_UNIT = re.compile(r"گرم|عیار|آب\s*[‌-]?\s*شده")
SIDE_PRICE = re.compile(
    r"(?P<side>فروش|خرید)\s*[:：]\s*"
    r"(?P<price>[0-9۰-۹٠-٩][0-9۰-۹٠-٩,٬،.]{3,})"
)
MARKET_HEADING = re.compile(r"پس[\s‌-]*فردای|فردای|نقدی|نقد\s*(?:امروز|فردا)")


def parse_mesghal_quotes(raw_text):
    """Return market/price pairs only while in an explicit mesghal section.

    Each item is (market, {side: source_toman_price}). Other units, including
    gram, stop the mesghal section and cannot leak into the outgoing quote.
    """
    market = "نرخ مثقال"
    active_mesghal = False
    quotes = []
    current = None
    for raw_line in raw_text.splitlines():
        line = raw_line.translate(PERSIAN_ARABIC_DIGITS)
        market_hit = MARKET_HEADING.search(line)
        if market_hit:
            heading = market_hit.group(0)
            if re.search(r"پس[\s‌-]*فردای", heading):
                market = "پس‌فردایی"
            elif re.search(r"فردای", heading):
                market = "فردایی"
            else:
                market = "نقدی"
            active_mesghal = False
            current = None

        if MESGHAL_UNIT.search(line):
            active_mesghal = True
            current = {}
            quotes.append((market, current))
        elif OTHER_UNIT.search(line):
            active_mesghal = False
            current = None

        if not active_mesghal or current is None:
            continue
        for match in SIDE_PRICE.finditer(line):
            digits = re.sub(r"\D", "", match.group("price"))
            if not digits:
                continue
            value = int(digits)
            if value >= 1_000_000:
                current[match.group("side")] = value

    return [(market_name, prices) for market_name, prices in quotes if prices]


def adjust_all_market_rates(raw_text):
    """Publish mesghal only: sell source +250,000 and buy source -50,000."""
    output = []
    for market, prices in parse_mesghal_quotes(raw_text):
        lines = [market, "هر مثقال"]
        if "فروش" in prices:
            lines.append(f"فروش : {prices['فروش'] + SELL_MARKUP_TOMAN:,}")
        if "خرید" in prices:
            new_buy = prices["خرید"] - BUY_DISCOUNT_TOMAN
            if new_buy > 0:
                lines.append(f"خرید : {new_buy:,}")
        if len(lines) > 2:
            output.append("\n".join(lines))
    return "\n".join(output)


def format_rate_message(raw_text):
    """Clean, mesghal-only branded Telegram HTML; never include gram numbers."""
    quotes = parse_mesghal_quotes(raw_text)
    if not quotes:
        return ""
    parts = ["📊 <b>نرخ بازار آبشده خضری</b>"]
    for market, prices in quotes:
        parts.append(f"💰 <b>{html.escape(market)}</b>")
        parts.append("◀️ <b>هر مثقال (تومان)</b>")
        if "فروش" in prices:
            parts.append(f"🟩 <b>فروش:</b> <b>{prices['فروش']:,}</b>")
        if "خرید" in prices:
            parts.append(f"🟥 <b>خرید:</b> <b>{prices['خرید']:,}</b>")
    parts.append(CHANNEL_LINK)
    return "\n".join(parts)


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
        plain = html.unescape(re.sub(r"</?(?:b|i|a)(?:\s+[^>]*)?>", "", text))
        print(f"Telegram HTML rejected; retrying verified price as plain text: {error}", flush=True)
        _telegram_send(plain[:3900], formatted=False)



def iran_time():
    return datetime.datetime.now(IRAN_TZ)


def in_price_hours(at):
    """08:30 through 00:59 Iran time; closed 01:00-08:29."""
    return (at.hour > DAY_START_HOUR or (at.hour == DAY_START_HOUR and at.minute >= DAY_START_MINUTE) or at.hour < DAY_END_HOUR)


def session_day(at):
    """A session starts 08:30 and ends at 01:00 on the following date."""
    day = at.date()
    if at.hour < DAY_START_HOUR:
        day -= datetime.timedelta(days=1)
    return day.isoformat()


def parse_observations(raw_text):
    """Extract only explicit mesghal buy/sell values for daily reporting."""
    observations = {}
    for market, prices in parse_mesghal_quotes(raw_text):
        for side, price in prices.items():
            observations[f"{market} | مثقال | {side}"] = price
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
    rates = {name: item for name, item in state.get("daily", {}).get(day, {}).items() if " | مثقال | " in name}
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
    if now.hour == DAY_START_HOUR and now.minute >= DAY_START_MINUTE and day not in state["morning_sent"]:
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
    if os.getenv("PRICE_24H", "0") != "1" and not in_price_hours(iran_time()):
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
    quoted_text = adjust_all_market_rates(text[:2400])
    message = format_rate_message(quoted_text)
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
    record_observations(state, posted_at, quoted_text)
    state_save(state)
    print(f"Published verified recent source post: {post_id}", flush=True)


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not configured")
    state = state_load()
    deadline = time.monotonic() + RUN_SECONDS
    print("Abshode Khezri price relay started", flush=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        while RUN_SECONDS <= 0 or time.monotonic() < deadline:
            start = time.monotonic()
            try:
                send_scheduled_messages(state)
            except Exception as error:
                print(f"Scheduled message error: {type(error).__name__}: {error}", flush=True)
            check_once(state, executor)
            wait = CHECK_SECONDS - (time.monotonic() - start)
            if wait > 0:
                time.sleep(wait if RUN_SECONDS <= 0 else min(wait, max(0, deadline - time.monotonic())))
    print("Price relay scheduled window completed", flush=True)


if __name__ == "__main__":
    main()
