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

from bs4 import BeautifulSoup

SOURCES = ("valaneia_gold", "alizadeyazd")
DESTINATION = os.getenv("CHANNEL_ID", "@khezriabshodeh")
TOKEN = os.getenv("BOT_TOKEN", "")
CHECK_SECONDS = 10
MAX_AGE_SECONDS = 300
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "240"))
STATE_FILE = ".bot_state.json"

RATE_WORDS = re.compile(r"آب\s*[‌-]?\s*شده|مظنه|مثقال|(?:۱۸|18)\s*عیار")
RATE_NUMBERS = re.compile(r"\d[\d,.\u066b\u066c،]{3,}")


def state_load():
    try:
        with open(STATE_FILE, encoding="utf-8") as fp:
            data = json.load(fp)
        return {"last_time": data.get("last_time", ""), "last_signature": data.get("last_signature", ""), "seen": data.get("seen", [])[-80:]}
    except (FileNotFoundError, ValueError, OSError):
        return {"last_time": "", "last_signature": "", "seen": []}


def state_save(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


def fetch_source(source):
    req = urllib.request.Request(
        f"https://t.me/s/{source}",
        headers={"User-Agent": "Mozilla/5.0"},
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
        line = html.escape(line)
        line = re.sub(
            r"(?<!\w)\d+(?:[,.]\d+)*",
            lambda match: f"<b>{match.group(0)}</b>",
            line,
        )
        lines.append(line)
    if not lines:
        return ""
    return "📊 نرخ بازار | آبشده خضری\n" + "\n".join(lines)


def send_rate(text):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": DESTINATION, "text": text, "parse_mode": "HTML"}).encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        reply = json.load(response)
    if not reply.get("ok"):
        raise RuntimeError("Telegram rejected sendMessage")


def check_once(state, executor):
    tasks = {source: executor.submit(fetch_source, source) for source in SOURCES}
    posts = []
    for source, task in tasks.items():
        try:
            posts.extend(task.result())
        except Exception as error:
            print(f"Source @{source} unavailable: {type(error).__name__}: {error}", flush=True)
    if not posts:
        print("No recent valid rate found; publishing nothing", flush=True)
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
            check_once(state, executor)
            wait = CHECK_SECONDS - (time.monotonic() - start)
            if wait > 0:
                time.sleep(min(wait, max(0, deadline - time.monotonic())))
    print("Price relay scheduled window completed", flush=True)


if __name__ == "__main__":
    main()
