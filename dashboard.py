"""
Marketing Pulse — kunlik dashboard boti.

Kuniga 4 marta (08:00, 13:00, 18:00, 21:00 Toshkent vaqti) kanalga:
  - USD/RUB/EUR/CNY -> UZS rasmiy kurslari (O'zbekiston Markaziy banki)
  - Google Trends — global TOP-5 (qisqa izoh bilan)
  - O'zbekistonda eng ko'p qidirilgan TOP-5
yuboradi. Yangi dashboard chiqqanda eski dashboard xabari o'chiriladi.

Muhit o'zgaruvchilari:
  BOT_TOKEN, CHANNEL_ID, ANTHROPIC_API_KEY — asosiy botdagi bilan bir xil.
  ALLRATES_API_KEY — allratestoday.com'dan bepul kalit (CBU kurslari uchun).
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
ALLRATES_KEY = os.environ.get("ALLRATES_API_KEY")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

STATE_FILE = "dashboard_state.json"
TASHKENT = ZoneInfo("Asia/Tashkent")

ALLRATES_URL = "https://allratestoday.com/api/v1/central-bank/cbu/latest"
CBU_DIRECT_URL = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

TRENDS_GLOBAL_RSS = "https://trends.google.com/trending/rss?geo=US"
TRENDS_UZ_RSS = "https://trends.google.com/trending/rss?geo=UZ"

CURRENCIES = ["USD", "RUB", "EUR", "CNY"]
CCY_FLAG = {"USD": "🇺🇸", "RUB": "🇷🇺", "EUR": "🇪🇺", "CNY": "🇨🇳"}


# ---------------------------------------------------------------------------
# Valyuta kurslari — O'zbekiston Markaziy banki rasmiy kurslari
# ---------------------------------------------------------------------------
def _to_float(value):
    try:
        return float(str(value).replace(" ", "").replace(",", ""))
    except Exception:
        return None


def parse_rates_payload(data):
    """Turli JSON ko'rinishlarini {valyuta: kurs} ga aylantiradi."""
    rate_date = None
    rows = data
    if isinstance(data, dict):
        rate_date = data.get("rate_date") or data.get("date")
        rows = data
        for key in ("data", "rates", "table"):
            if isinstance(rows, dict) and key in rows:
                inner = rows[key]
                if isinstance(inner, dict) and not rate_date:
                    rate_date = inner.get("rate_date") or inner.get("date")
                rows = inner

    out = {}
    if isinstance(rows, dict):
        for code, val in rows.items():
            if code in CURRENCIES:
                num = val.get("rate") if isinstance(val, dict) else val
                num = _to_float(num)
                if num:
                    out[code] = num
    elif isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            quote = row.get("quote") or row.get("target")
            if quote and quote != "UZS":
                continue
            code = (row.get("currency") or row.get("code") or row.get("Ccy")
                    or row.get("char_code") or row.get("source") or row.get("base"))
            if code in CURRENCIES:
                num = _to_float(
                    row.get("value") or row.get("rate") or row.get("Rate")
                    or row.get("uzs_per_unit")
                )
                if num:
                    out[code] = num
    return out, rate_date


def fetch_rates_allrates():
    if not ALLRATES_KEY:
        raise RuntimeError("ALLRATES_API_KEY o'rnatilmagan")
    r = requests.get(
        ALLRATES_URL,
        headers={**HEADERS, "Authorization": f"Bearer {ALLRATES_KEY}"},
        timeout=30,
    )
    print(f"AllRates javob: status {r.status_code}, boshi: {r.text[:300]!r}")
    r.raise_for_status()
    rates, rate_date = parse_rates_payload(r.json())
    if not rates:
        raise RuntimeError("AllRates javobidan kurslar ajratib olinmadi")
    return rates, rate_date


def fetch_rates_oanor():
    key = os.environ.get("OANOR_API_KEY")
    if not key:
        raise RuntimeError("OANOR_API_KEY o'rnatilmagan")
    r = requests.get(
        "https://api.oanor.com/cbu-api/v1/rates",
        headers={**HEADERS, "x-oanor-key": key},
        timeout=30,
    )
    print(f"Oanor javob: status {r.status_code}, boshi: {r.text[:300]!r}")
    r.raise_for_status()
    rates, rate_date = parse_rates_payload(r.json())
    if not rates:
        raise RuntimeError("Oanor javobidan kurslar ajratib olinmadi")
    return rates, rate_date


def fetch_rates_cbu_direct():
    r = requests.get(CBU_DIRECT_URL, headers=HEADERS, timeout=8)
    r.raise_for_status()
    rates, rate_date = parse_rates_payload(r.json())
    if not rates:
        raise RuntimeError("CBU javobi bo'sh")
    return rates, rate_date


def get_currency_rates(state):
    """Barcha manbalarni sinab, eng yangi sanali kursni tanlaydi.
    Qaytaradi: ({valyuta: {rate, diff}}, kurs_sanasi)."""
    candidates, error = [], None
    for fetcher in (fetch_rates_allrates, fetch_rates_oanor, fetch_rates_cbu_direct):
        try:
            rates, rate_date = fetcher()
            candidates.append((rate_date or "", rates))
        except Exception as ex:
            error = ex
            print(f"{fetcher.__name__} xato:", ex)

    if not candidates:
        cached = state.get("rates")
        if cached:
            print("Yangi kurs olinmadi, saqlangan kurslar ishlatiladi")
            prev = state.get("prev_rates", {})
            data = {c: {"rate": v, "diff": v - prev.get(c, v)} for c, v in cached.items()}
            return data, state.get("rates_date")
        raise error or RuntimeError("Valyuta kurslari olinmadi")

    rate_date, fresh = max(candidates, key=lambda x: x[0])
    rate_date = rate_date or datetime.now(TASHKENT).strftime("%Y-%m-%d")

    if rate_date != state.get("rates_date"):
        state["prev_rates"] = state.get("rates", {})
        state["rates_date"] = rate_date
    state["rates"] = fresh

    prev = state.get("prev_rates", {})
    data = {
        c: {"rate": v, "diff": (v - prev[c]) if c in prev else 0.0}
        for c, v in fresh.items()
    }
    return data, rate_date


def format_currency_block(rates, rate_date=None):
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
    if rate_date:
        try:
            age = (datetime.now(TASHKENT).date()
                   - datetime.strptime(rate_date, "%Y-%m-%d").date()).days
        except ValueError:
            age = 0
        if age > 2:
            lines.append(f"<i>⚠️ Kurs sanasi: {rate_date}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Google Trends
# ---------------------------------------------------------------------------
def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def get_trends(url, limit=5):
    parsed = feedparser.parse(url, request_headers=HEADERS)
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
        if r.status_code != 200:
            raise RuntimeError(f"API {r.status_code}: {r.text[:400]}")
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

    state = load_state()

    try:
        rates, rate_date = get_currency_rates(state)
        currency_block = format_currency_block(rates, rate_date)
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

    delete_old_message(state)

    new_id = send_dashboard(message)
    state["message_id"] = new_id
    state["chat_id"] = CHANNEL_ID
    save_state(state)
    print("Dashboard yuborildi, message_id:", new_id)


if __name__ == "__main__":
    main()
