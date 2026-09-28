"""
Marketing yangiliklari boti (yangilangan to'liq versiya).
Har kuni bir marta ishga tushiring (GitHub Actions).

Muhit o'zgaruvchilari:
  BOT_TOKEN          - @BotFather bergan token
  CHANNEL_ID         - masalan @mening_kanalim
  ANTHROPIC_API_KEY  - (ixtiyoriy) Claude tarjima va tanlash uchun
"""
import os, json, html, time, re, requests, feedparser

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
TOP_N = 3
HOURS = 24

FEEDS = [
    "https://www.marketingdive.com/feeds/news/",
    "https://www.searchenginejournal.com/feed/",
    "https://www.socialmediatoday.com/feeds/news/",
    "https://blog.hubspot.com/marketing/rss.xml",
    "https://contentmarketinginstitute.com/feed/",
    "https://www.marketingweek.com/feed/",
]

SENT_FILE = "sent.json"


def clean(text):
    """HTML teglarni olib tashlaydi va matnni tozalaydi."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def load_sent():
    try:
        return set(json.load(open(SENT_FILE)))
    except Exception:
        return set()


def save_sent(s):
    json.dump(list(s)[-500:], open(SENT_FILE, "w"))


def fetch_recent():
    cutoff = time.time() - HOURS * 3600
    items = []
    for url in FEEDS:
        try:
            for e in feedparser.parse(url).entries:
                t = e.get("published_parsed") or e.get("updated_parsed")
                if t and time.mktime(t) >= cutoff:
                    items.append({
                        "title": clean(e.get("title", "")),
                        "link": e.get("link", ""),
                        "summary": clean(e.get("summary", ""))[:400],
                    })
        except Exception as ex:
            print("Feed xato:", url, ex)
    return items


def pick_with_claude(items):
    listing = "\n".join(
        f"{i}. {x['title']} — {x['summary']}" for i, x in enumerate(items)
    )
    prompt = (
        f"Quyida marketing yangiliklari ro'yxati. Eng muhim va foydali {TOP_N} tasini tanla. "
        "Har biri uchun o'zbek tilida (lotin yozuvida) sarlavha va 2-3 gapli qisqa xulosa yoz. "
        'FAQAT JSON qaytar, boshqa hech narsa yozma: '
        '[{"i": raqam, "title_uz": "...", "summary_uz": "..."}]\n\n'
        + listing
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"API {r.status_code}: {r.text[:300]}")

    blocks = r.json().get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise RuntimeError("JSON topilmadi: " + text[:200])
    picks = json.loads(text[start:end + 1])

    return [
        {**items[p["i"]], "title": p["title_uz"], "summary": p["summary_uz"]}
        for p in picks[:TOP_N]
    ]


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:300]}")


def main():
    sent = load_sent()
    items = [x for x in fetch_recent() if x["link"] not in sent]
    if not items:
        print("Yangi maqola topilmadi")
        return

    if ANTHROPIC_KEY:
        try:
            chosen = pick_with_claude(items[:40])
        except Exception as ex:
            print("Claude xato, oddiy rejimga o'tildi:", ex)
            chosen = items[:TOP_N]
    else:
        chosen = items[:TOP_N]

    for x in chosen:
        msg = (
            f"📈 <b>{html.escape(x['title'])}</b>\n\n"
            f"{html.escape(x['summary'])}\n\n"
            f'🔗 <a href="{x["link"]}">Batafsil o\'qish</a>'
        )
        send(msg)
        sent.add(x["link"])
        time.sleep(2)
    save_sent(sent)


if __name__ == "__main__":
    main()
