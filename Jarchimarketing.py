"""
Marketing yangiliklari boti.
Har kuni bir marta ishga tushiring (cron / GitHub Actions).

O'rnatish:  pip install feedparser requests
Muhit o'zgaruvchilari:
  BOT_TOKEN          - @BotFather bergan token
  CHANNEL_ID         - masalan @mening_kanalim
  ANTHROPIC_API_KEY  - (ixtiyoriy) bo'lsa Claude eng sara yangilikni tanlab, o'zbekcha yozadi
"""
import os, json, html, time, requests, feedparser

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
                        "title": e.get("title", ""),
                        "link": e.get("link", ""),
                        "summary": html.unescape(e.get("summary", ""))[:400],
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
        "Har biri uchun o'zbek tilida (lotin) 2-3 gapli qisqa xulosa yoz. "
        'FAQAT JSON qaytar: [{"i": raqam, "title_uz": "...", "summary_uz": "..."}]\n\n'
        + listing
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": MODEL, "max_tokens": 1500,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].replace("```json", "").replace("```", "").strip()
    picks = json.loads(text)
    return [
        {**items[p["i"]], "title": p["title_uz"], "summary": p["summary_uz"]}
        for p in picks[:TOP_N]
    ]


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=30,
    )
    r.raise_for_status()


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
