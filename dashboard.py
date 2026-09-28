"""
Marketing Pulse — kunlik dashboard boti.

Kuniga 4 marta (08:00, 13:00, 18:00, 21:00 Toshkent vaqti) kanalga:
  - USD/RUB/EUR/CNY -> UZS rasmiy kurslari (O'zbekiston Markaziy banki)
  - Google Trends — global TOP-5 (qisqa izoh bilan)
  - O'zbekistonda eng ko'p qidirilgan TOP-5
yuboradi. Yangi dashboard chiqqanda eski dashboard xabari o'chiriladi.

Muhit o'zgaruvchilari:
  BOT_TOKEN, CHANNEL_ID, ANTHROPIC_API_KEY — asosiy botdagi bilan bir xil.
"""
import os
import re
import json
import html
import requests
import feedparser
from datetime import datetime
from zoneinfo import ZoneInfo

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

STATE_FILE = "dashboard_state.json"
TASHKENT = ZoneInfo("Asia/Tashkent")

CBU_URL = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/"
TRENDS_GLOBAL_RSS = "https://trends.google.com/trends/trendingsearches/daily/rss?geo=US"
TRENDS_UZ_RSS = "https://trends.google.com/trends/trendingsearches/daily/rss?geo=UZ"

CURRENCIES = ["USD", "RUB", "EUR", "CNY"]
CCY_FLAG = {"USD": "🇺🇸", "RUB": "🇷🇺", "EUR": "🇪🇺", "CNY": "🇨🇳"}


# ---------------------------------------------------------------------------
# Valyuta kurslari — O'zbekiston Markaziy banki rasmiy API'si
# ---------------------------------------------------------------------------
def get_currency_rates():
    r = requests.get(CBU_URL, timeout=30)
    r.raise_for_status()
    data = r.json()

    rates = {}
    for row in data:
        ccy = row.get("Ccy")
        if ccy in CURRENCIES:
            rate = float(str(row.get("Rate", "0")).replace(",", ""))
            diff = float(str(row.get("Diff", "0")).replace(",", ""))
            rates[ccy] = {"rate": rate, "diff": diff}
    return rates


def format_currency_block(rates):
    lines = ["💱 <b>Valyuta kurslari</b> <i>(O'zbekiston Markaziy banki)</i>"]
    for ccy in CURRENCIES:
        info = rates.get(ccy)
        if not info:
            continue
        if info["diff"] > 0:
            arrow = "🟢▲"
        elif info["diff"] < 0:
            arrow = "🔴▼"
        else:
            arrow = ""
        lines.append(
            f"{CCY_FLAG[ccy]} {ccy} → {info['rate']:,.2f} so'm {arrow}".replace(",", " ")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Google Trends
# ---------------------------------------------------------------------------
def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def get_trends(url, limit=5):
    parsed = feedparser.parse(url)
    items = []
    for e in parsed.entries[:limit]:
        items.append({
            "title": clean_text(e.get("title", "")),
            "snippet": clean_text(e.get("summary", ""))[:200],
        })
    return items


def add_short_comments(trend_items):
    """Har bir global trend uchun Claude orqali juda qisqa izoh yozadi."""
    if not ANTHROPIC_KEY or not trend_items:
        for t in trend_items:
            t["comment"] = ""
        return trend_items

    listing = "\n".join(
        f"{i}. {t['title']} — {t['snippet']}" for i, t in enumerate(trend_items)
    )
    prompt = (
        "Quyida Google Trends'dagi hozirgi top mavzular. Har biriga o'zbek tilida "
        "(lotin yozuvida) FAQAT 4-6 so'zdan iborat juda qisqa izoh yoz — bu mavzu "
        "nima haqida yoki nega trendda ekanini tushuntir.\n"
        'FAQAT JSON qaytar: [{"i": raqam, "comment": "..."}]\n\n' + listing
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        blocks = r.json().get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        start, end = text.find("["), text.rfind("]")
        comments = json.loads(text[start:end + 1])
        comment_map = {c["i"]: c["comment"] for c in comments}
        for i, t in enumerate(trend_items):
            t["comment"] = comment_map.get(i, "")
    except Exception as ex:
        print("Izoh yozishda xato:", ex)
        for t in trend_items:
            t["comment"] = ""
    return trend_items


def format_trends_block(title, items, with_comments=False):
    lines = [f"{title}"]
    for i, t in enumerate(items, 1):
        line = f"{i}. {html.escape(t['title'])}"
        if with_comments and t.get("comment"):
            line += f" — <i>{html.escape(t['comment'])}</i>"
        lines.append(line)
    if not items:
        lines.append("— ma'lumot topilmadi —")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram: eski dashboardni o'chirish + yangisini yuborish
# ---------------------------------------------------------------------------
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))


def delete_old_message(state):
    msg_id = state.get("message_id")
    if not msg_id:
        return
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
        json={"chat_id": CHANNEL_ID, "message_id": msg_id},
        timeout=30,
    )
    if r.status_code != 200:
        print("Eski dashboardni o'chirishda xato (e'tiborsiz qoldirildi):", r.text[:200])


def send_dashboard(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:300]}")
    return r.json()["result"]["message_id"]


def channel_link():
    if CHANNEL_ID.startswith("@"):
        return f"https://t.me/{CHANNEL_ID[1:]}"
    return None


# ---------------------------------------------------------------------------
# ASOSIY OQIM
# ---------------------------------------------------------------------------
def main():
    now = datetime.now(TASHKENT)
    header = (
        "🔥 <b>MARKETING PULSE</b>\n"
        f"🕐 {now.strftime('%d-%B, %Y')} | {now.strftime('%H:%M')} (Toshkent vaqti)"
    )

    try:
        rates = get_currency_rates()
        currency_block = format_currency_block(rates)
    except Exception as ex:
        print("Valyuta kurslarini olishda xato:", ex)
        currency_block = "💱 <b>Valyuta kurslari</b> — vaqtincha mavjud emas"

    try:
        global_trends = add_short_comments(get_trends(TRENDS_GLOBAL_RSS))
        global_block = format_trends_block("📈 <b>Google Trends TOP-5</b>", global_trends, with_comments=True)
    except Exception as ex:
        print("Global trendlarni olishda xato:", ex)
        global_block = "📈 <b>Google Trends TOP-5</b>\n— ma'lumot topilmadi —"

    try:
        uz_trends = get_trends(TRENDS_UZ_RSS)
        uz_block = format_trends_block("🇺🇿 <b>O'zbekistonda eng ko'p qidirilgan TOP-5</b>", uz_trends)
    except Exception as ex:
        print("O'zbekiston trendlarini olishda xato:", ex)
        uz_block = "🇺🇿 <b>O'zbekistonda eng ko'p qidirilgan TOP-5</b>\n— ma'lumot topilmadi —"

    parts = [header, currency_block, global_block, uz_block]

    link = channel_link()
    if link:
        parts.append(f"🔗 {link}")

    message = "\n\n".join(parts)

    state = load_state()
    delete_old_message(state)

    new_id = send_dashboard(message)
    save_state({"message_id": new_id, "chat_id": CHANNEL_ID})
    print("Dashboard yuborildi, message_id:", new_id)


if __name__ == "__main__":
    main()
