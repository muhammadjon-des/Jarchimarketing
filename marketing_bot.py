"""
Marketing yangiliklari boti — v2 (to'liq pipeline arxitekturasi)

Bosqichlar:
  10-20 manba -> RSS -> Dublikat filter -> Marketing relevance ->
  Importance filter -> AI izchillik tekshiruvi -> O'zbekcha moslashtirish ->
  Marketing insight -> Rasm qidirish -> Telegram formatter -> Kanal

Har bir ishga tushirishda FAQAT BITTA post yuboriladi (kuniga 3 marta —
09:00, 13:00, 20:00 — workflow jadvali orqali chaqiriladi).

Muhit o'zgaruvchilari:
  BOT_TOKEN          - @BotFather bergan token
  CHANNEL_ID          - masalan @mening_kanalim
  ANTHROPIC_API_KEY   - Claude uchun (majburiy, bo'lmasa post chiqmaydi)
"""
import os
import re
import json
import html
import time
import difflib
import requests
import feedparser

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
AUTHOR_NAME = os.environ.get("AUTHOR_NAME", "Muhammadjon")

HOURS_WINDOW = 30           # so'nggi necha soatlik maqolalarni ko'rib chiqamiz
MAX_CANDIDATES_TO_AI = 40   # Claude'ga yuboriladigan maksimal maqolalar soni
DUP_TITLE_THRESHOLD = 0.72  # sarlavha o'xshashligi bo'yicha dublikat chegarasi
SENT_FILE = "sent.json"

# ---------------------------------------------------------------------------
# 1) MANBALAR (10-20 ta). Har biri (nom, rss_url, og'irlik).
#    Og'irlik — manba nufuzi, muhimlik bahosida yordam beradi (1=oddiy, 3=yirik).
# ---------------------------------------------------------------------------
FEEDS = [
    # --- Xalqaro (inglizcha) ---
    ("Marketing Dive", "https://www.marketingdive.com/feeds/news/", 3),
    ("Search Engine Journal", "https://www.searchenginejournal.com/feed/", 2),
    ("Social Media Today", "https://www.socialmediatoday.com/feeds/news/", 2),
    ("HubSpot Marketing Blog", "https://blog.hubspot.com/marketing/rss.xml", 3),
    ("Content Marketing Institute", "https://contentmarketinginstitute.com/feed/", 2),
    ("Marketing Week", "https://www.marketingweek.com/feed/", 3),
    ("MarTech", "https://martech.org/feed/", 2),
    ("Adweek", "https://www.adweek.com/feed/", 2),
    # --- Rus tilida (MDH bozori uchun foydali) ---
    ("Cossa", "https://www.cossa.ru/rss/", 3),
    ("Sostav.ru", "https://www.sostav.ru/rss/news.xml", 2),
    ("AdIndex", "https://adindex.ru/rss.xml", 2),
    ("VC.ru", "https://vc.ru/rss/all", 2),
]
# ESLATMA: rus manbalaridan ba'zilarining RSS manzili vaqti-vaqti bilan
# o'zgarishi mumkin. Agar bot logida "Feed xato" ko'rinsa, o'sha manbani
# saytdan yangi RSS havolasi bilan almashtiring yoki ro'yxatdan olib tashlang
# — qolgan manbalar baribir ishlashda davom etadi.

CATEGORY_EMOJI = {
    "smm": "📱",
    "seo": "🔍",
    "reklama": "🎯",
    "brending": "🎨",
    "kontent marketing": "✍️",
    "email marketing": "📧",
    "e-commerce": "🛒",
    "ai marketing": "🤖",
    "pr": "📣",
    "analitika": "📊",
}
DEFAULT_EMOJI = "📈"


# ---------------------------------------------------------------------------
# Yordamchi funksiyalar
# ---------------------------------------------------------------------------
def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_image(entry):
    """Feed elementidan rasm URL'ini topishga harakat qiladi."""
    try:
        if "media_content" in entry and entry.media_content:
            url = entry.media_content[0].get("url")
            if url:
                return url
    except Exception:
        pass
    try:
        if "media_thumbnail" in entry and entry.media_thumbnail:
            url = entry.media_thumbnail[0].get("url")
            if url:
                return url
    except Exception:
        pass
    try:
        for link in entry.get("links", []):
            if link.get("type", "").startswith("image"):
                return link.get("href")
    except Exception:
        pass
    raw = entry.get("summary", "") or entry.get("description", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', raw)
    if m:
        return m.group(1)
    return None


def load_sent():
    try:
        return set(json.load(open(SENT_FILE)))
    except Exception:
        return set()


def save_sent(s):
    json.dump(list(s)[-1000:], open(SENT_FILE, "w"))


# ---------------------------------------------------------------------------
# BOSQICH: RSS yig'ish
# ---------------------------------------------------------------------------
def fetch_all():
    cutoff = time.time() - HOURS_WINDOW * 3600
    items = []
    for source_name, url, weight in FEEDS:
        try:
            parsed = feedparser.parse(url)
            for e in parsed.entries:
                t = e.get("published_parsed") or e.get("updated_parsed")
                if not t or time.mktime(t) < cutoff:
                    continue
                items.append({
                    "source": source_name,
                    "weight": weight,
                    "title": clean_text(e.get("title", "")),
                    "link": e.get("link", ""),
                    "summary": clean_text(e.get("summary", "") or e.get("description", ""))[:600],
                    "image": extract_image(e),
                })
        except Exception as ex:
            print(f"Feed xato ({source_name}):", ex)
    return items


# ---------------------------------------------------------------------------
# BOSQICH: Dublikat filter (aniq link + sarlavha o'xshashligi)
# ---------------------------------------------------------------------------
def dedupe(items):
    seen_links = set()
    unique = []
    for it in items:
        if it["link"] in seen_links or not it["link"]:
            continue
        seen_links.add(it["link"])
        unique.append(it)

    # sarlavha o'xshashligi bo'yicha filtrlash (bir xil voqea, boshqa manba)
    result = []
    for it in sorted(unique, key=lambda x: -x["weight"]):
        is_dup = False
        for kept in result:
            ratio = difflib.SequenceMatcher(
                None, it["title"].lower(), kept["title"].lower()
            ).ratio()
            if ratio >= DUP_TITLE_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            result.append(it)
    return result


# ---------------------------------------------------------------------------
# BOSQICH: Claude — relevance + importance skoring
# ---------------------------------------------------------------------------
def call_claude(prompt, max_tokens=4000):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"API {r.status_code}: {r.text[:300]}")
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def extract_json(text, kind="["):
    start = text.find(kind)
    end = text.rfind("]" if kind == "[" else "}")
    if start == -1 or end == -1:
        raise RuntimeError("JSON topilmadi: " + text[:200])
    return json.loads(text[start:end + 1])


def rank_candidates(items):
    listing = "\n".join(
        f"{i}. [{x['source']}] {x['title']} — {x['summary'][:250]}"
        for i, x in enumerate(items)
    )
    prompt = (
        "Quyida so'nggi marketing yangiliklari ro'yxati. Vazifang:\n"
        "1. Faqat MARKETING, REKLAMA, SMM, SEO, BREND, E-COMMERCE mavzusiga oid "
        "bo'lganlarini qoldir (aloqasizlarini chiqarib tashla).\n"
        "2. Har biriga 1-10 ball ber, mezonlar: ta'sir doirasi, amaliylik, "
        "yangilik darajasi, manba nufuzi, O'zbekiston/MDH bozoriga aloqadorlik.\n"
        "3. Har biriga bitta kategoriya belgila: SMM, SEO, Reklama, Brending, "
        "Kontent marketing, Email marketing, E-commerce, AI marketing, PR, Analitika.\n"
        "4. Natijani ball bo'yicha KAMAYISH tartibida, FAQAT JSON array qaytar:\n"
        '[{"i": raqam, "score": son, "category": "..."}]\n'
        "Boshqa hech qanday matn yozma.\n\n"
        + listing
    )
    text = call_claude(prompt, max_tokens=3000)
    ranked = extract_json(text, "[")
    ranked.sort(key=lambda x: -x.get("score", 0))
    return ranked


# ---------------------------------------------------------------------------
# BOSQICH: izchillik tekshiruvi + o'zbekcha moslashtirish + marketing insight
# ---------------------------------------------------------------------------
def generate_uzbek_content(item):
    prompt = (
        "Sen tajribali marketing muharririsiz. Quyidagi maqola asosida "
        "o'zbek tilida (lotin yozuvida) post tayyorla.\n\n"
        f"SARLAVHA: {item['title']}\n"
        f"MATN: {item['summary']}\n\n"
        "Talablar:\n"
        "1. title_uz — jozibali, aniq o'zbekcha sarlavha (so'zma-so'z tarjima emas).\n"
        "2. summary_uz — yangilikning mohiyatini 2-3 gapda tushuntir (oddiy tarjima emas, "
        "moslashtirilgan bayon).\n"
        "3. insight_uz — bu yangilik MARKETING MUTAXASSISLARI va BIZNES EGALARI uchun "
        "aynan nimasi bilan foydali/aloqador ekanini 2-3 gapda yoz: qanday amaliy xulosa "
        "chiqarish mumkin, qanday harakat qilish tavsiya etiladi.\n"
        "4. asl_matnga_mos — true/false: summary_uz va insight_uz FAQAT yuqoridagi MATNda "
        "bor faktlarga asoslanganmi (o'zingdan raqam yoki fakt qo'shmagan bo'lsang true).\n\n"
        'FAQAT JSON qaytar: {"title_uz": "...", "summary_uz": "...", '
        '"insight_uz": "...", "asl_matnga_mos": true}'
    )
    text = call_claude(prompt, max_tokens=1500)
    return extract_json(text, "{")


# ---------------------------------------------------------------------------
# BOSQICH: Telegram formatter va yuborish
# ---------------------------------------------------------------------------
def format_message(item, content, category):
    emoji = CATEGORY_EMOJI.get(category.strip().lower(), DEFAULT_EMOJI)
    return (
        f"{emoji} <b>{html.escape(content['title_uz'])}</b>\n\n"
        f"📰 {html.escape(content['summary_uz'])}\n\n"
        f"💡 <b>Nima uchun muhim:</b>\n{html.escape(content['insight_uz'])}\n\n"
        f'🔗 <a href="{item["link"]}">Manba: {html.escape(item["source"])}</a>\n\n'
        f"✍️ {html.escape(AUTHOR_NAME)}"
    )


def send_to_telegram(text, image_url):
    if image_url:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            json={
                "chat_id": CHANNEL_ID,
                "photo": image_url,
                "caption": text,
                "parse_mode": "HTML",
            },
            timeout=30,
        )
        if r.status_code == 200:
            return
        print("Rasm bilan yuborishda xato, matn bilan qayta urinilmoqda:", r.text[:200])

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


# ---------------------------------------------------------------------------
# ASOSIY OQIM
# ---------------------------------------------------------------------------
def main():
    if not ANTHROPIC_KEY:
        print("ANTHROPIC_API_KEY yo'q — sifat nazoratisiz post chiqarilmaydi, to'xtatildi.")
        return

    sent = load_sent()

    raw_items = fetch_all()
    print(f"Yig'ildi: {len(raw_items)} ta maqola")

    items = [x for x in raw_items if x["link"] not in sent]
    items = dedupe(items)
    print(f"Dublikatlar tozalangach: {len(items)} ta")

    if not items:
        print("Yangi maqola topilmadi")
        return

    candidates = items[:MAX_CANDIDATES_TO_AI]

    try:
        ranked = rank_candidates(candidates)
    except Exception as ex:
        print("Ranklashda xato, to'xtatildi:", ex)
        return

    # Eng yuqori balldan boshlab, izchillik tekshiruvidan o'tgan birinchisini tanlaymiz
    for r in ranked[:8]:
        idx = r.get("i")
        if idx is None or idx >= len(candidates):
            continue
        picked = candidates[idx]
        category = r.get("category", "")

        try:
            content = generate_uzbek_content(picked)
        except Exception as ex:
            print(f"Kontent generatsiyasida xato (index {idx}):", ex)
            continue

        if not content.get("asl_matnga_mos", False):
            print(f"Izchillik tekshiruvidan o'tmadi (index {idx}), keyingisiga o'tilmoqda")
            continue

        message = format_message(picked, content, category)
        try:
            send_to_telegram(message, picked.get("image"))
        except Exception as ex:
            print("Telegram xato:", ex)
            return

        sent.add(picked["link"])
        save_sent(sent)
        print("Yuborildi:", content["title_uz"])
        return

    print("Mos nomzod topilmadi (hammasi filtrdan o'tmadi)")


if __name__ == "__main__":
    main()
